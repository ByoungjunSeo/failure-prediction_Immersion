"""PUE GPU Load Controller — Continual AT Training + PI Feedback Control.

Architecture
============
Controller Pod (zero-resource Ray node, node2)
  -> 3 x GPUTrainer actors (node2-4, 0.35 GPU each)
  -> PI feedback loop (every CONTROL_INTERVAL seconds)
  -> Thermal guardrail (GPU > TEMP_LIMIT -> force reduce)
  -> VRAM guardrail (VRAM > 85% -> freeze batch increase)
  -> Xid error detection (auto-stop on GPU hardware error)

Safety mechanisms (post-incident hardening):
  1. Hard batch_size ceiling: clamp(MIN_BATCH, MAX_BATCH) always enforced
  2. VRAM guard: DCGM_FI_DEV_FB_USED / FB_TOTAL > 85% -> block batch increase
  3. CUDA OOM handler: catch, halve batch, empty_cache, continue
  4. PI output clamping: ±MAX_BATCH_DELTA per cycle
  5. Target util cap: max 85% (reject higher values)
  6. Soft start: initial batch at INITIAL_BATCH, warmup period
  7. Xid error detection: auto-shutdown on DCGM_FI_DEV_XID_ERRORS

node1 excluded: control-plane + inference only (GPU PCIe fault risk).

On/Off:
  kubectl -n failure-prediction scale deploy pue-gpu-load --replicas=1
  kubectl -n failure-prediction scale deploy pue-gpu-load --replicas=0

Target change:
  kubectl -n failure-prediction set env deploy/pue-gpu-load TARGET_GPU_UTIL=70
  (or edit ConfigMap pue-gpu-load-config -> auto-propagates via volume mount)
"""

import logging
import os
import signal
import sys
import time

import numpy as np
import ray

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pue")

# ─────────────────────────── Configuration ───────────────────────────

TARGET_GPU_UTIL  = int(os.getenv("TARGET_GPU_UTIL",  "80"))
GPU_TEMP_LIMIT   = int(os.getenv("GPU_TEMP_LIMIT",   "83"))
CONTROL_INTERVAL = int(os.getenv("CONTROL_INTERVAL",  "90"))

TRAIN_D_MODEL  = int(os.getenv("TRAIN_D_MODEL",  "512"))
TRAIN_N_HEADS  = int(os.getenv("TRAIN_N_HEADS",  "8"))
TRAIN_E_LAYERS = int(os.getenv("TRAIN_E_LAYERS", "6"))
TRAIN_D_FF     = int(os.getenv("TRAIN_D_FF",     "2048"))
TRAIN_WIN_SIZE = int(os.getenv("TRAIN_WIN_SIZE",  "200"))
INITIAL_BATCH  = int(os.getenv("INITIAL_BATCH",   "32"))   # soft start: low
MAX_BATCH      = int(os.getenv("MAX_BATCH",       "72"))    # HARD ceiling
MIN_BATCH      = 4
WARMUP_SECONDS = int(os.getenv("WARMUP_SECONDS",  "120"))
INITIAL_SLEEP  = float(os.getenv("INITIAL_SLEEP", "0.0"))

# Safety: target util hard cap — never exceed this
MAX_TARGET_UTIL = 85

PROM_URL = os.getenv(
    "PROMETHEUS_URL",
    "http://monitoring-kube-prometheus-prometheus.monitoring:9090",
)
VM_URL = os.getenv(
    "VICTORIA_METRICS_URL",
    "http://victoria-metrics-svc.failure-prediction:8428",
)

# node1 excluded: control-plane + inference only (GPU PCIe fault risk)
GPU_NODES = ["node2", "node3", "node4"]
NODE_INST = {f"node{i}": f"10.100.230.{129 + i}:9100" for i in range(1, 5)}


# ─────────────────────────── Prometheus helpers ───────────────────────────

def prom_query(query):
    """Instant PromQL -> [(metric_dict, float_value), ...]."""
    import requests
    try:
        r = requests.get(
            f"{PROM_URL}/api/v1/query",
            params={"query": query}, timeout=5,
        )
        return [
            (s["metric"], float(s["value"][1]))
            for s in r.json().get("data", {}).get("result", [])
        ]
    except Exception:
        return []


def prom_range(query, hours=24, step="60s"):
    """Range PromQL -> [float, ...]."""
    import requests
    now = int(time.time())
    try:
        r = requests.get(
            f"{PROM_URL}/api/v1/query_range",
            params={
                "query": query,
                "start": now - hours * 3600,
                "end": now,
                "step": step,
            },
            timeout=10,
        )
        res = r.json().get("data", {}).get("result", [])
        if res and "values" in res[0]:
            return [float(v[1]) for v in res[0]["values"]]
    except Exception:
        pass
    return []


def vm_push(lines):
    """Push Prometheus text lines to VictoriaMetrics /import/prometheus."""
    import requests
    try:
        requests.post(
            f"{VM_URL}/api/v1/import/prometheus",
            data="\n".join(lines) + "\n",
            timeout=3,
        )
    except Exception:
        pass


# ─────────────────────────── GPUTrainer Actor ───────────────────────────

@ray.remote(num_gpus=0.35, num_cpus=0.5, max_concurrency=2)
class GPUTrainer:
    """Continual AnomalyTransformer training on one GPU node.

    run_loop() trains autonomously. Controller calls set_params() to
    adjust batch_size / sleep_time for target GPU utilisation.
    """

    def __init__(self, node_name: str, model_cfg: dict):
        import torch

        sys.path.insert(0, "/app/vendor/Anomaly-Transformer")
        from model.AnomalyTransformer import AnomalyTransformer

        self.node = node_name
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        self.win_size = model_cfg["win_size"]

        self.model = AnomalyTransformer(
            win_size=model_cfg["win_size"],
            enc_in=1,
            c_out=1,
            d_model=model_cfg["d_model"],
            n_heads=model_cfg["n_heads"],
            e_layers=model_cfg["e_layers"],
            d_ff=model_cfg["d_ff"],
        ).to(self.device)

        self.optimizer = torch.optim.AdamW(
            self.model.parameters(), lr=3e-4, weight_decay=1e-5,
        )
        self.scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
            self.optimizer, T_0=500, T_mult=2,
        )
        self.criterion = torch.nn.MSELoss()

        nparams = sum(p.numel() for p in self.model.parameters())
        log.info(
            "[%s] AT train model: d=%d L=%d ff=%d -> %.1fM params, %s",
            node_name,
            model_cfg["d_model"],
            model_cfg["e_layers"],
            model_cfg["d_ff"],
            nparams / 1e6,
            self.device,
        )

        # Control knobs (mutable by controller)
        self.batch_size = model_cfg.get("initial_batch", 32)
        self.sleep_time = model_cfg.get("initial_sleep", 0.0)

        # State
        self.step = 0
        self.epoch = 0
        self.running = True
        self._data = None
        self._val = None
        self._last_refresh = 0.0
        self._best_val = float("inf")
        self._losses = []
        self._oom_ceiling = MAX_BATCH  # learned OOM ceiling
        self._oom_count = 0

    # ── data ──────────────────────────────────────────────────────

    def _refresh_data(self):
        """Fetch 24 h of real CPU-usage time-series from Prometheus."""
        inst = NODE_INST.get(self.node, "")
        series = prom_range(
            f'1 - avg by(instance)(rate(node_cpu_seconds_total'
            f'{{instance="{inst}",mode="idle"}}[1m]))',
            hours=24,
        )
        if len(series) < self.win_size * 2:
            # Fall back to synthetic noise so training keeps running
            series = list(
                np.random.randn(max(1440, self.win_size * 4)).astype(np.float32)
                * 0.1
            )
        arr = np.array(series, dtype=np.float32)
        mu, sigma = arr.mean(), arr.std() + 1e-8
        arr = (arr - mu) / sigma

        split = int(len(arr) * 0.85)
        self._data = arr[:split]
        self._val = arr[split:]
        self.epoch += 1
        self._last_refresh = time.time()
        log.info(
            "[%s] data refresh: train=%d val=%d epoch=%d",
            self.node, len(self._data), len(self._val), self.epoch,
        )

    def _make_batch(self, data, bs):
        import torch

        n = len(data) - self.win_size
        if n <= 0:
            return torch.randn(bs, self.win_size, 1, device=self.device)
        idx = np.random.randint(0, n, size=bs)
        windows = np.stack([data[i : i + self.win_size] for i in idx])
        return torch.from_numpy(windows).unsqueeze(-1).to(self.device)

    # ── training loop ─────────────────────────────────────────────

    async def run_loop(self):
        """Autonomous training loop.  Runs until stop() is called."""
        import asyncio
        import torch

        while self.running:
            # Refresh data every 30 min
            if time.time() - self._last_refresh > 1800 or self._data is None:
                self._refresh_data()

            # Forward + backward — with CUDA OOM guard (Safety #3)
            try:
                self.model.train()
                batch = self._make_batch(self._data, self.batch_size)
                self.optimizer.zero_grad()
                output, *_ = self.model(batch)
                loss = self.criterion(output, batch)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
                self.optimizer.step()
                self.scheduler.step()
                self.step += 1

                lv = loss.item()
                self._losses.append(lv)
                if len(self._losses) > 500:
                    self._losses = self._losses[-250:]

            except torch.cuda.OutOfMemoryError:
                torch.cuda.empty_cache()
                old_bs = self.batch_size
                self._oom_ceiling = max(MIN_BATCH, old_bs - 4)
                self.batch_size = max(MIN_BATCH, self.batch_size // 2)
                self._oom_count += 1
                log.warning(
                    "[%s] CUDA OOM at bs=%d -> halved to %d "
                    "(ceiling=%d, oom_count=%d)",
                    self.node, old_bs, self.batch_size,
                    self._oom_ceiling, self._oom_count,
                )
                await asyncio.sleep(2.0)
                continue

            except Exception as e:
                log.error("[%s] training error: %s", self.node, e)
                await asyncio.sleep(5.0)
                continue

            # Validation every 100 steps
            if self.step % 100 == 0 and self._val is not None:
                self.model.eval()
                with torch.no_grad():
                    vb = self._make_batch(self._val, min(32, self.batch_size))
                    vo, *_ = self.model(vb)
                    vl = self.criterion(vo, vb).item()
                if vl < self._best_val:
                    self._best_val = vl
                log.info(
                    "[%s] step=%d loss=%.4f val=%.4f best=%.4f bs=%d sl=%.3f",
                    self.node,
                    self.step,
                    lv,
                    vl,
                    self._best_val,
                    self.batch_size,
                    self.sleep_time,
                )

            # Inter-step sleep (controller's fine actuator)
            # Always yield to let get_status() / set_params() be served
            await asyncio.sleep(max(0.001, self.sleep_time))

        return f"{self.node} stopped at step {self.step}"

    # ── control interface ─────────────────────────────────────────

    def set_params(self, batch_size: int, sleep_time: float):
        """Called by controller to adjust load knobs. (Safety #1, #4)"""
        # Hard clamp: never exceed MAX_BATCH or learned OOM ceiling
        ceiling = min(MAX_BATCH, self._oom_ceiling)
        self.batch_size = max(MIN_BATCH, min(ceiling, int(batch_size)))
        self.sleep_time = max(0.0, min(10.0, float(sleep_time)))

    def get_status(self) -> dict:
        avg = float(np.mean(self._losses[-50:])) if self._losses else 0.0
        return dict(
            node=self.node,
            step=self.step,
            epoch=self.epoch,
            batch_size=self.batch_size,
            sleep_time=round(self.sleep_time, 4),
            avg_loss=round(avg, 6),
            best_val=round(self._best_val, 6),
            running=self.running,
            oom_ceiling=self._oom_ceiling,
            oom_count=self._oom_count,
        )

    def stop(self):
        self.running = False


# ─────────────────────────── Feedback Controller ───────────────────────────

class FeedbackController:
    """PI controller with thermal + VRAM + Xid guardrails, dead-band,
    and rate limiter."""

    DEADBAND = 10  # +/-10 % — wider band to reduce oscillation
    # Proportional + Integral gains (very conservative)
    KP_BATCH = 0.10
    KI_BATCH = 0.01
    KP_SLEEP = -0.0008
    KI_SLEEP = -0.0001
    # Rate limiter: maximum batch_size change per cycle (Safety #4)
    MAX_BATCH_DELTA = 4
    MAX_SLEEP_DELTA = 0.04
    # VRAM guard threshold (Safety #2)
    VRAM_WARN_PCT = 85.0

    def __init__(self, nodes, target, temp_limit):
        # Safety #5: cap target util
        self.target = min(target, MAX_TARGET_UTIL)
        if target > MAX_TARGET_UTIL:
            log.warning("TARGET %d%% capped to %d%% (safety limit)",
                        target, MAX_TARGET_UTIL)
        self.temp_limit = temp_limit
        self.nodes = nodes
        self.s = {}
        for n in nodes:
            self.s[n] = dict(
                batch_size=INITIAL_BATCH,
                sleep_time=INITIAL_SLEEP,
                integral=0.0,
                throttled=False,
                vram_warn=False,
                util=0.0,
                temp=0.0,
                power=0.0,
                vram_pct=0.0,
                warmup_until=time.time() + WARMUP_SECONDS,
            )

    # Max GPU TDP per node type
    NODE_TDP = {"node1": 180, "node2": 180, "node3": 180, "node4": 360}

    def read_dcgm(self):
        """Read GPU util / temp / power / VRAM / Xid for all nodes."""
        out = {}
        for m, v in prom_query("avg_over_time(DCGM_FI_DEV_GPU_UTIL[90s])"):
            out.setdefault(m.get("Hostname", ""), {})["dcgm_util"] = v
        for m, v in prom_query("DCGM_FI_DEV_GPU_TEMP"):
            out.setdefault(m.get("Hostname", ""), {})["temp"] = v
        for m, v in prom_query("avg_over_time(DCGM_FI_DEV_POWER_USAGE[90s])"):
            out.setdefault(m.get("Hostname", ""), {})["power"] = v
        # VRAM usage (Safety #2) — FB_TOTAL not available; use USED+FREE
        for m, v in prom_query("DCGM_FI_DEV_FB_USED"):
            out.setdefault(m.get("Hostname", ""), {})["vram_used"] = v
        for m, v in prom_query("DCGM_FI_DEV_FB_FREE"):
            out.setdefault(m.get("Hostname", ""), {})["vram_free"] = v
        # Xid errors (Safety #7)
        for m, v in prom_query("DCGM_FI_DEV_XID_ERRORS"):
            out.setdefault(m.get("Hostname", ""), {})["xid"] = v
        # Compute blended utilization per node
        for hn, d in out.items():
            tdp = self.NODE_TDP.get(hn, 180)
            power_util = min(100.0, d.get("power", 0) / tdp * 100)
            dcgm_util = d.get("dcgm_util", 0)
            d["util"] = 0.5 * dcgm_util + 0.5 * power_util
            # VRAM percentage (used / (used + free))
            vu = d.get("vram_used", 0)
            vf = d.get("vram_free", 0)
            vt = vu + vf
            if vt > 0:
                d["vram_pct"] = vu / vt * 100
            else:
                d["vram_pct"] = 0
        return out

    def step(self, dt: float):
        """One feedback cycle.  Returns {node: (batch_size, sleep_time)}.
        Returns None if Xid error detected (caller should emergency stop).
        """
        dcgm = self.read_dcgm()
        adj = {}

        for n in self.nodes:
            s = self.s[n]
            m = dcgm.get(n, {})
            u = m.get("util", s["util"])
            t = m.get("temp", s["temp"])
            p = m.get("power", s["power"])
            vram_pct = m.get("vram_pct", s["vram_pct"])
            xid = m.get("xid", 0)
            s["util"], s["temp"], s["power"] = u, t, p
            s["vram_pct"] = vram_pct

            # ── Safety #7: Xid error detection ──
            if xid > 0:
                log.error("[%s] XID ERROR %d detected! Emergency stop.",
                          n, int(xid))
                return None  # signal emergency shutdown

            # ── Thermal guardrail (overrides everything) ──
            if t > self.temp_limit:
                s["throttled"] = True
                s["batch_size"] = max(MIN_BATCH, s["batch_size"] // 2)
                s["sleep_time"] = min(10.0, s["sleep_time"] * 2 + 0.5)
                log.warning(
                    "[%s] THERMAL %.0f C > %d C -> bs=%d sl=%.2f",
                    n, t, self.temp_limit, s["batch_size"], s["sleep_time"],
                )
                adj[n] = (s["batch_size"], s["sleep_time"])
                continue

            if s["throttled"] and t < self.temp_limit - 5:
                s["throttled"] = False
                log.info("[%s] thermal clear (%.0f C)", n, t)
            if s["throttled"]:
                adj[n] = (s["batch_size"], s["sleep_time"])
                continue

            # ── Safety #2: VRAM guard ──
            if vram_pct > self.VRAM_WARN_PCT:
                if not s["vram_warn"]:
                    log.warning("[%s] VRAM %.1f%% > %.0f%% — batch increase "
                                "blocked", n, vram_pct, self.VRAM_WARN_PCT)
                    s["vram_warn"] = True
            else:
                s["vram_warn"] = False

            # ── Warmup: hold batch_size steady (Safety #6) ──
            if time.time() < s.get("warmup_until", 0):
                adj[n] = (s["batch_size"], s["sleep_time"])
                continue

            # ── PI control ──
            err = self.target - u  # positive = need more load
            if abs(err) <= self.DEADBAND:
                s["integral"] *= 0.9  # slow decay within dead-band
                adj[n] = (s["batch_size"], s["sleep_time"])
                continue

            s["integral"] = max(-200, min(200, s["integral"] + err * dt))

            db = self.KP_BATCH * err + self.KI_BATCH * s["integral"]
            ds = self.KP_SLEEP * err + self.KI_SLEEP * s["integral"]

            # Rate-limit: clamp delta per cycle (Safety #4)
            db = max(-self.MAX_BATCH_DELTA, min(self.MAX_BATCH_DELTA, db))
            ds = max(-self.MAX_SLEEP_DELTA, min(self.MAX_SLEEP_DELTA, ds))

            # VRAM guard: block batch *increase* if VRAM too high
            if s["vram_warn"] and db > 0:
                db = 0  # only allow decrease

            # Safety #1: hard clamp
            s["batch_size"] = max(MIN_BATCH, min(MAX_BATCH,
                                                 int(s["batch_size"] + db)))
            s["sleep_time"] = max(0.0, min(10.0, s["sleep_time"] + ds))

            adj[n] = (s["batch_size"], s["sleep_time"])

        return adj


# ─────────────────────────── Main ───────────────────────────

def _dcgm_uuid_to_hostname():
    """Query DCGM via Prometheus to build GPU-UUID -> K8s hostname map."""
    mapping = {}
    try:
        for m, _ in prom_query("DCGM_FI_DEV_GPU_UTIL"):
            uuid = m.get("UUID", "")
            hostname = m.get("Hostname", "")
            if uuid and hostname:
                mapping[uuid] = hostname
    except Exception as e:
        log.warning("Failed to build UUID->hostname map: %s", e)
    return mapping


def _probe_gpu_workers():
    """Probe each GPU Ray worker to find nodes where CUDA works.

    Returns list of (ray_node_id, pod_hostname, k8s_node) for healthy
    GPU workers.  The k8s_node is the actual K8s node name (matching
    DCGM Hostname label) -- resolved by matching GPU UUID against DCGM.
    Skips head pod (node1 = control-plane protected).
    """

    @ray.remote(num_gpus=0.1, num_cpus=0.1)
    class _CUDAProbe:
        def check(self):
            import torch
            import subprocess
            ok = torch.cuda.is_available()
            if ok:
                torch.randn(1, device="cuda:0")
            gpu_uuid = ""
            try:
                gpu_uuid = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=uuid",
                     "--format=csv,noheader"],
                    text=True, timeout=10,
                ).strip().split("\n")[0]
            except Exception:
                pass
            return ok, gpu_uuid

    # Build UUID -> K8s hostname map from DCGM
    uuid_map = _dcgm_uuid_to_hostname()
    if uuid_map:
        log.info("DCGM UUID map: %s",
                 {u[:12]: h for u, h in uuid_map.items()})

    gpu_workers = []
    for rn in ray.nodes():
        if not rn.get("Alive"):
            continue
        if rn.get("Resources", {}).get("GPU", 0) <= 0:
            continue
        # Skip head pod — node1 is control-plane only
        if "head" in rn.get("NodeManagerHostname", ""):
            log.info("Skipping head pod (control-plane protected)")
            continue
        gpu_workers.append(rn)

    healthy = []
    for rn in gpu_workers:
        nid = rn["NodeID"]
        hn = rn.get("NodeManagerHostname", "?")
        try:
            from ray.util.scheduling_strategies import (
                NodeAffinitySchedulingStrategy,
            )
            probe = _CUDAProbe.options(
                scheduling_strategy=NodeAffinitySchedulingStrategy(
                    node_id=nid, soft=False,
                ),
            ).remote()
            ok, gpu_uuid = ray.get(probe.check.remote(), timeout=45)
            ray.kill(probe)
            if ok:
                k8s_node = uuid_map.get(gpu_uuid, "")
                if not k8s_node:
                    log.warning("GPU worker %s: UUID %s not in DCGM map, "
                                "skipping", hn, gpu_uuid[:12])
                    continue
                healthy.append((nid, hn, k8s_node))
                log.info("GPU worker OK: %s -> K8s %s (UUID=%s)",
                         hn, k8s_node, gpu_uuid[:12])
            else:
                log.warning("GPU worker CUDA fail: %s", hn)
        except Exception as e:
            log.warning("GPU worker probe failed: %s (%s)",
                        hn, type(e).__name__)
            try:
                ray.kill(probe)
            except Exception:
                pass

    return healthy


def main():
    # Safety #5: enforce target cap at startup
    global TARGET_GPU_UTIL
    if TARGET_GPU_UTIL > MAX_TARGET_UTIL:
        log.warning("TARGET %d%% capped to %d%% (safety limit)",
                    TARGET_GPU_UTIL, MAX_TARGET_UTIL)
        TARGET_GPU_UTIL = MAX_TARGET_UTIL

    log.info("=" * 60)
    log.info("PUE GPU Load Controller (hardened)")
    log.info(
        "TARGET=%d%%  TEMP_LIMIT=%d C  INTERVAL=%ds  MAX_BATCH=%d",
        TARGET_GPU_UTIL, GPU_TEMP_LIMIT, CONTROL_INTERVAL, MAX_BATCH,
    )
    log.info(
        "Model: d=%d heads=%d layers=%d ff=%d win=%d init_batch=%d",
        TRAIN_D_MODEL, TRAIN_N_HEADS, TRAIN_E_LAYERS,
        TRAIN_D_FF, TRAIN_WIN_SIZE, INITIAL_BATCH,
    )
    log.info("Safety: MAX_TARGET=%d%% VRAM_WARN=85%% Xid=auto-stop",
             MAX_TARGET_UTIL)
    log.info("=" * 60)

    ray.init(address="auto", ignore_reinit_error=True,
             logging_level=logging.WARNING)
    log.info("Connected to Ray  (avail GPU=%.2f CPU=%.1f)",
             ray.available_resources().get("GPU", 0),
             ray.available_resources().get("CPU", 0))

    model_cfg = dict(
        d_model=TRAIN_D_MODEL,
        n_heads=TRAIN_N_HEADS,
        e_layers=TRAIN_E_LAYERS,
        d_ff=TRAIN_D_FF,
        win_size=TRAIN_WIN_SIZE,
        initial_batch=INITIAL_BATCH,
        initial_sleep=INITIAL_SLEEP,
    )

    # Clean up orphaned actors from a previous run
    for node in GPU_NODES:
        try:
            old = ray.get_actor(f"pue-{node}")
            ray.kill(old)
            log.info("Killed orphan actor pue-%s", node)
            time.sleep(1)
        except ValueError:
            pass

    # Probe GPU workers to find healthy nodes
    healthy_workers = _probe_gpu_workers()
    log.info("Healthy GPU workers: %d found", len(healthy_workers))

    if not healthy_workers:
        log.error("No healthy GPU workers found! Exiting.")
        ray.shutdown()
        sys.exit(1)

    # Create trainers on healthy workers whose K8s node is in GPU_NODES
    from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

    trainers = {}
    k8s_ray_map = {}
    active_nodes = []
    for nid, hn, k8s_node in healthy_workers:
        if k8s_node not in GPU_NODES:
            log.info("Skipping worker %s (K8s %s not in GPU_NODES)",
                     hn, k8s_node)
            continue
        active_nodes.append(k8s_node)
        k8s_ray_map[k8s_node] = nid
        opts = dict(
            name=f"pue-{k8s_node}",
            scheduling_strategy=NodeAffinitySchedulingStrategy(
                node_id=nid, soft=False,
            ),
        )
        log.info("Placing pue-%s -> %s (%s)", k8s_node, hn, nid[:12])
        actor = GPUTrainer.options(**opts).remote(k8s_node, model_cfg)
        trainers[k8s_node] = actor

    GPU_NODES_ACTIVE = active_nodes

    # Start autonomous training loops
    loops = {n: t.run_loop.remote() for n, t in trainers.items()}
    log.info("Training loops started on %d GPU nodes", len(loops))

    # Per-actor creation timestamp (for init grace period)
    actor_born = {n: time.time() for n in GPU_NODES_ACTIVE}
    actor_restarts = {n: 0 for n in GPU_NODES_ACTIVE}
    ACTOR_GRACE = 90
    MAX_RESTARTS = 3

    # Feedback controller
    ctrl = FeedbackController(GPU_NODES_ACTIVE, TARGET_GPU_UTIL, GPU_TEMP_LIMIT)

    # Graceful shutdown
    shutdown = [False]

    def _sig(signum, _frame):
        log.info("Signal %s received — shutting down", signum)
        shutdown[0] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    # ── Control loop ──
    cycle = 0
    try:
        while not shutdown[0]:
            t0 = time.time()

            # Re-read target from ConfigMap mount (auto-update)
            try:
                cfg_path = "/etc/pue-config/TARGET_GPU_UTIL"
                if os.path.isfile(cfg_path):
                    new_target = int(open(cfg_path).read().strip())
                    # Safety #5: enforce cap on runtime change
                    new_target = min(new_target, MAX_TARGET_UTIL)
                    if new_target != ctrl.target:
                        log.info(
                            "TARGET changed %d -> %d (ConfigMap)",
                            ctrl.target, new_target,
                        )
                        ctrl.target = new_target
            except Exception:
                pass

            # 1. Feedback control step
            adj = ctrl.step(dt=CONTROL_INTERVAL)

            # Safety #7: Xid detection — emergency shutdown
            if adj is None:
                log.error("XID ERROR — emergency shutdown! "
                          "Killing all trainers.")
                break

            # 2. Apply adjustments to actors
            for n, (bs, sl) in adj.items():
                if n in trainers:
                    trainers[n].set_params.remote(bs, sl)

            # 3. Collect actor status + health check
            sts = {}
            for n in list(trainers.keys()):
                t_actor = trainers[n]
                age = time.time() - actor_born.get(n, 0)
                try:
                    sts[n] = ray.get(t_actor.get_status.remote(), timeout=8)
                except Exception as e:
                    sts[n] = dict(step=0, avg_loss=0)
                    if age < ACTOR_GRACE:
                        log.info("[%s] actor init (%.0fs / %ds grace)",
                                 n, age, ACTOR_GRACE)
                        continue
                    if actor_restarts.get(n, 0) >= MAX_RESTARTS:
                        log.error(
                            "[%s] actor failed %d times, giving up",
                            n, actor_restarts[n])
                        continue
                    actor_restarts[n] = actor_restarts.get(n, 0) + 1
                    log.warning("[%s] actor unreachable (%s) after %.0fs, "
                                "restart %d/%d",
                                n, type(e).__name__, age,
                                actor_restarts[n], MAX_RESTARTS)
                    try:
                        ray.kill(t_actor)
                    except Exception:
                        pass
                    time.sleep(2)
                    nid2 = k8s_ray_map.get(n)
                    opts2 = dict(name=f"pue-{n}")
                    if nid2:
                        opts2["scheduling_strategy"] = \
                            NodeAffinitySchedulingStrategy(
                                node_id=nid2, soft=True)
                    else:
                        opts2["scheduling_strategy"] = "SPREAD"
                    new_actor = GPUTrainer.options(**opts2).remote(
                        n, model_cfg)
                    trainers[n] = new_actor
                    loops[n] = new_actor.run_loop.remote()
                    actor_born[n] = time.time()
                    ctrl.s[n] = dict(
                        batch_size=INITIAL_BATCH,
                        sleep_time=INITIAL_SLEEP,
                        integral=0.0, throttled=False,
                        vram_warn=False,
                        util=0.0, temp=0.0, power=0.0,
                        vram_pct=0.0,
                        warmup_until=time.time() + WARMUP_SECONDS,
                    )
                    log.info("[%s] actor restarted", n)

            # 4. Summary log
            parts = []
            for n in GPU_NODES_ACTIVE:
                s = ctrl.s[n]
                st = sts.get(n, {})
                parts.append(
                    "%s: u=%3.0f%% %2.0fC %3.0fW vram=%2.0f%% "
                    "bs=%-4d sl=%.3f stp=%d"
                    % (
                        n,
                        s["util"],
                        s["temp"],
                        s["power"],
                        s["vram_pct"],
                        s["batch_size"],
                        s["sleep_time"],
                        st.get("step", 0),
                    )
                )
            log.info("target=%d%% | %s", ctrl.target, " | ".join(parts))

            # 5. Push metrics to VictoriaMetrics
            cycle += 1
            if cycle % 3 == 0:
                ts_ms = int(time.time() * 1000)
                lines = [f"pue_target_gpu_util {ctrl.target} {ts_ms}"]
                for n in GPU_NODES_ACTIVE:
                    s = ctrl.s[n]
                    st = sts.get(n, {})
                    lines.extend(
                        [
                            f'pue_training_step{{node="{n}"}} '
                            f'{st.get("step", 0)} {ts_ms}',
                            f'pue_training_loss{{node="{n}"}} '
                            f'{st.get("avg_loss", 0)} {ts_ms}',
                            f'pue_training_batch_size{{node="{n}"}} '
                            f'{s["batch_size"]} {ts_ms}',
                            f'pue_training_sleep{{node="{n}"}} '
                            f'{s["sleep_time"]:.4f} {ts_ms}',
                            f'pue_gpu_vram_pct{{node="{n}"}} '
                            f'{s["vram_pct"]:.1f} {ts_ms}',
                        ]
                    )
                vm_push(lines)

            # Sleep remainder of control interval
            elapsed = time.time() - t0
            time.sleep(max(0.1, CONTROL_INTERVAL - elapsed))

    finally:
        log.info("Stopping %d trainers ...", len(trainers))
        for t_actor in trainers.values():
            try:
                t_actor.stop.remote()
            except Exception:
                pass
        time.sleep(3)
        for n in GPU_NODES_ACTIVE:
            try:
                ray.kill(ray.get_actor(f"pue-{n}"))
            except Exception:
                pass
        ray.shutdown()
        log.info("PUE GPU Load Controller stopped.")


if __name__ == "__main__":
    main()
