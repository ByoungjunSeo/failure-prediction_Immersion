#!/usr/bin/env python3
"""PUE GPU Load Controller — Web UI.

Zero-dependency web interface (Python stdlib only).
Runs on the existing failure-pred image — no pip install needed.

Features:
  - Target GPU utilization slider (0-85%)
  - Per-node GPU status (util / temp / power / VRAM)
  - Emergency stop (scale deployment to 0)
  - Confirmation modal for all changes
  - Auto-refresh every 10 s

Security:
  - ServiceAccount RBAC: ConfigMap patch + Deployment scale only
  - NodePort 31618 (office-only network)
  - Input validation server-side (0-85 integer)
  - Confirmation required for every change
"""

import http.server
import json
import logging
import os
import socketserver
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pue-web")

# ── Configuration ──────────────────────────────────────────────
NAMESPACE = "failure-prediction"
CONFIGMAP_NAME = "pue-gpu-load-config"
DEPLOYMENT_NAME = "pue-gpu-load"
PROMETHEUS_URL = os.getenv(
    "PROMETHEUS_URL",
    "http://monitoring-kube-prometheus-prometheus.monitoring:9090",
)
MAX_TARGET = 85
GPU_NODES = ["node2", "node3", "node4"]
NODE_TDP = {"node2": 180, "node3": 180, "node4": 360}
NODE_GPU = {"node2": "RTX 5060 Ti", "node3": "RTX 5060 Ti", "node4": "RTX 5080"}

# K8s in-cluster API
K8S_HOST = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
K8S_PORT = os.getenv("KUBERNETES_SERVICE_PORT", "443")
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


# ── K8s API helpers ────────────────────────────────────────────
def _k8s_ssl_ctx():
    ctx = ssl.create_default_context()
    if os.path.isfile(SA_CA_PATH):
        ctx.load_verify_locations(SA_CA_PATH)
    return ctx


def _k8s_token():
    if os.path.isfile(SA_TOKEN_PATH):
        return open(SA_TOKEN_PATH).read().strip()
    return ""


def k8s_api(method, path, body=None, content_type=None):
    """Make a K8s API request using the in-cluster service account."""
    url = f"https://{K8S_HOST}:{K8S_PORT}{path}"
    token = _k8s_token()
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = (
            content_type or "application/strategic-merge-patch+json"
        )
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, context=_k8s_ssl_ctx(), timeout=10)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_err = e.read().decode(errors="replace")
        log.error("K8s API %s %s -> %d: %s", method, path, e.code, body_err[:200])
        raise
    except Exception as e:
        log.error("K8s API %s %s -> %s", method, path, e)
        raise


def get_configmap():
    path = f"/api/v1/namespaces/{NAMESPACE}/configmaps/{CONFIGMAP_NAME}"
    try:
        cm = k8s_api("GET", path)
        return cm.get("data", {})
    except Exception:
        return {}


def patch_configmap(data_patch):
    path = f"/api/v1/namespaces/{NAMESPACE}/configmaps/{CONFIGMAP_NAME}"
    return k8s_api("PATCH", path, {"data": data_patch})


def get_deployment():
    path = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{DEPLOYMENT_NAME}"
    try:
        dep = k8s_api("GET", path)
        spec = dep.get("spec", {})
        status = dep.get("status", {})
        return {
            "replicas": spec.get("replicas", 0),
            "ready": status.get("readyReplicas", 0),
            "available": status.get("availableReplicas", 0),
        }
    except Exception:
        return {"replicas": 0, "ready": 0, "available": 0}


def scale_deployment(replicas):
    path = (
        f"/apis/apps/v1/namespaces/{NAMESPACE}"
        f"/deployments/{DEPLOYMENT_NAME}/scale"
    )
    return k8s_api("PATCH", path, {"spec": {"replicas": replicas}})


# ── Prometheus helpers ─────────────────────────────────────────
def prom_query(query):
    try:
        url = f"{PROMETHEUS_URL}/api/v1/query"
        qs = urllib.parse.urlencode({"query": query})
        req = urllib.request.Request(f"{url}?{qs}")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        return [
            (s["metric"], float(s["value"][1]))
            for s in data.get("data", {}).get("result", [])
        ]
    except Exception:
        return []


def get_gpu_status():
    nodes = {}
    for m, v in prom_query("avg_over_time(DCGM_FI_DEV_GPU_UTIL[90s])"):
        hn = m.get("Hostname", "")
        if hn in GPU_NODES:
            nodes.setdefault(hn, {})["util_dcgm"] = round(v, 1)
    for m, v in prom_query("DCGM_FI_DEV_GPU_TEMP"):
        hn = m.get("Hostname", "")
        if hn in GPU_NODES:
            nodes.setdefault(hn, {})["temp"] = round(v, 1)
    for m, v in prom_query("avg_over_time(DCGM_FI_DEV_POWER_USAGE[90s])"):
        hn = m.get("Hostname", "")
        if hn in GPU_NODES:
            nodes.setdefault(hn, {})["power"] = round(v, 1)
    for m, v in prom_query("DCGM_FI_DEV_FB_USED"):
        hn = m.get("Hostname", "")
        if hn in GPU_NODES:
            nodes.setdefault(hn, {})["vram_used"] = round(v, 1)
    for m, v in prom_query("DCGM_FI_DEV_FB_FREE"):
        hn = m.get("Hostname", "")
        if hn in GPU_NODES:
            nodes.setdefault(hn, {})["vram_free"] = round(v, 1)

    for hn, d in nodes.items():
        tdp = NODE_TDP.get(hn, 180)
        power_util = min(100.0, d.get("power", 0) / tdp * 100)
        d["util_blended"] = round(
            (d.get("util_dcgm", 0) + power_util) / 2, 1
        )
        vu = d.get("vram_used", 0)
        vf = d.get("vram_free", 0)
        vt = vu + vf
        d["vram_pct"] = round(vu / vt * 100, 1) if vt > 0 else 0
        d["gpu_model"] = NODE_GPU.get(hn, "")
    return nodes


# ── HTTP Handler ───────────────────────────────────────────────
class PUEHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the PUE web UI."""

    server_version = "PUE-Web/1.0"

    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    # ── Routes ──

    def do_GET(self):
        if self.path == "/":
            self._send_html(HTML_PAGE)
        elif self.path == "/api/status":
            self._handle_status()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/target":
            self._handle_set_target()
        else:
            self.send_error(404)

    # ── Handlers ──

    def _handle_status(self):
        cm = get_configmap()
        dep = get_deployment()
        gpu = get_gpu_status()
        self._send_json(
            {
                "target": int(cm.get("TARGET_GPU_UTIL", "0")),
                "max_target": MAX_TARGET,
                "deployment": dep,
                "gpu_nodes": gpu,
                "config": {
                    "control_interval": cm.get("CONTROL_INTERVAL", "90"),
                    "temp_limit": cm.get("GPU_TEMP_LIMIT", "83"),
                    "max_batch": cm.get("MAX_BATCH", "72"),
                    "initial_batch": cm.get("INITIAL_BATCH", "32"),
                },
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    def _handle_set_target(self):
        try:
            body = self._read_body()
        except Exception:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if not body.get("confirm"):
            self._send_json({"error": "Confirmation required"}, 400)
            return

        raw = body.get("target")
        try:
            target = int(raw)
        except (TypeError, ValueError):
            self._send_json(
                {"error": f"Invalid value: {raw} (integer required)"}, 400
            )
            return

        if target < 0 or target > MAX_TARGET:
            self._send_json(
                {"error": f"Target must be 0-{MAX_TARGET}% (got {target}%)"}, 400
            )
            return

        # 0% = stop
        if target == 0:
            try:
                scale_deployment(0)
                log.info("WEB: STOP — scaled to 0 replicas")
                self._send_json(
                    {
                        "status": "stopped",
                        "target": 0,
                        "message": "PUE load stopped (replicas=0)",
                    }
                )
            except Exception as e:
                self._send_json({"error": f"Failed to stop: {e}"}, 500)
            return

        # Set target
        try:
            patch_configmap({"TARGET_GPU_UTIL": str(target)})
            log.info("WEB: TARGET changed to %d%%", target)

            # Ensure controller is running
            dep = get_deployment()
            msg = f"Target set to {target}%"
            if dep["replicas"] == 0:
                scale_deployment(1)
                msg += " (controller started)"
                log.info("WEB: scaled to 1 replica (was stopped)")

            self._send_json({"status": "ok", "target": target, "message": msg})
        except Exception as e:
            self._send_json({"error": f"Failed: {e}"}, 500)


# ── Threaded server ────────────────────────────────────────────
class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ── HTML Page ──────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PUE GPU Load Controller</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:#0f1923;color:#c7d5e0;min-height:100vh}
.container{max-width:960px;margin:0 auto;padding:20px}
header{display:flex;justify-content:space-between;align-items:center;
  padding:16px 0;border-bottom:1px solid #1e2d3d;margin-bottom:20px}
header h1{font-size:22px;font-weight:600;color:#e8eaed}
.badge{padding:6px 14px;border-radius:20px;font-size:13px;font-weight:600}
.badge-run{background:#1b5e20;color:#81c784}
.badge-stop{background:#b71c1c33;color:#ef5350}
.badge-warn{background:#e65100;color:#ffb74d}

/* GPU Cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-bottom:24px}
.card{background:#1a2733;border:1px solid #263545;border-radius:10px;padding:16px;
  transition:border-color .2s}
.card:hover{border-color:#4fc3f7}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.card-title{font-size:15px;font-weight:600;color:#e8eaed}
.card-subtitle{font-size:11px;color:#78909c}
.metric{display:flex;justify-content:space-between;align-items:center;
  padding:5px 0;font-size:13px}
.metric-label{color:#90a4ae}
.metric-value{font-weight:600;font-variant-numeric:tabular-nums}
.bar-bg{height:6px;background:#263545;border-radius:3px;margin-top:4px;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;transition:width .5s ease}

/* Control Panel */
.control{background:#1a2733;border:1px solid #263545;border-radius:10px;padding:24px;margin-bottom:24px}
.control h2{font-size:16px;color:#e8eaed;margin-bottom:16px}
.target-display{text-align:center;margin-bottom:16px}
.target-num{font-size:56px;font-weight:700;color:#4fc3f7;font-variant-numeric:tabular-nums}
.target-unit{font-size:24px;color:#78909c;margin-left:2px}
.target-label{font-size:13px;color:#78909c;margin-top:2px}
.slider-row{display:flex;align-items:center;gap:12px;margin-bottom:20px}
.slider-row input[type=range]{flex:1;-webkit-appearance:none;height:8px;
  background:#263545;border-radius:4px;outline:none}
.slider-row input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;
  width:22px;height:22px;background:#4fc3f7;border-radius:50%;cursor:pointer;
  border:2px solid #0f1923}
.slider-row input[type=number]{width:70px;padding:8px;background:#0f1923;
  border:1px solid #263545;border-radius:6px;color:#e8eaed;font-size:16px;
  text-align:center;font-weight:600}
.slider-row input[type=number]:focus{border-color:#4fc3f7;outline:none}
.safety-note{font-size:12px;color:#78909c;text-align:center;margin-bottom:16px}
.btn-row{display:flex;gap:12px;justify-content:center}
.btn{padding:10px 28px;border:none;border-radius:8px;font-size:14px;
  font-weight:600;cursor:pointer;transition:all .15s}
.btn:active{transform:scale(.97)}
.btn-apply{background:#1565c0;color:#fff}
.btn-apply:hover{background:#1976d2}
.btn-apply:disabled{background:#263545;color:#546e7a;cursor:not-allowed}
.btn-stop{background:#b71c1c;color:#fff}
.btn-stop:hover{background:#c62828}

/* Config bar */
.config-bar{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:#546e7a;
  justify-content:center;margin-bottom:8px}

/* Modal */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);
  z-index:100;justify-content:center;align-items:center}
.modal-overlay.show{display:flex}
.modal{background:#1a2733;border:1px solid #263545;border-radius:12px;
  padding:28px;max-width:400px;width:90%}
.modal h3{font-size:18px;color:#e8eaed;margin-bottom:12px}
.modal p{font-size:14px;color:#b0bec5;margin-bottom:20px;line-height:1.5}
.modal .highlight{color:#4fc3f7;font-weight:700;font-size:18px}
.modal .warn{color:#ef5350;font-weight:700}
.modal .btn-row{justify-content:flex-end}

/* Footer */
.footer{text-align:center;font-size:11px;color:#37474f;padding:12px 0}
.update-time{font-size:12px;color:#546e7a;text-align:center;margin-bottom:8px}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>PUE GPU Load Controller</h1>
    <span class="badge badge-stop" id="status-badge">Loading...</span>
  </header>

  <!-- GPU Node Cards -->
  <div class="cards" id="gpu-cards"></div>

  <!-- Control Panel -->
  <div class="control">
    <h2>Target GPU Utilization</h2>
    <div class="target-display">
      <span class="target-num" id="current-target">--</span><span class="target-unit">%</span>
      <div class="target-label">current target</div>
    </div>
    <div class="slider-row">
      <span style="font-size:12px;color:#546e7a">0%</span>
      <input type="range" id="slider" min="0" max="85" value="60">
      <span style="font-size:12px;color:#546e7a">85%</span>
      <input type="number" id="num-input" min="0" max="85" value="60">
    </div>
    <div class="safety-note">
      Max 85% (safety limit) &middot; 0% = Stop all GPU load
    </div>
    <div class="btn-row">
      <button class="btn btn-apply" id="btn-apply">Apply Target</button>
      <button class="btn btn-stop" id="btn-stop">Emergency Stop</button>
    </div>
  </div>

  <div class="config-bar" id="config-bar"></div>
  <div class="update-time" id="update-time"></div>
  <div class="footer">
    PUE GPU Load Controller &middot; node1 excluded (GPU fault)
    &middot; node2-4 only
  </div>
</div>

<!-- Confirmation Modal -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <h3 id="modal-title">Confirm</h3>
    <p id="modal-body"></p>
    <div class="btn-row">
      <button class="btn" style="background:#263545;color:#b0bec5"
              id="modal-cancel">Cancel</button>
      <button class="btn btn-apply" id="modal-confirm">Confirm</button>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const slider = $('#slider');
const numInput = $('#num-input');
const btnApply = $('#btn-apply');
const btnStop = $('#btn-stop');
const modal = $('#modal');
let pendingAction = null;

// Sync slider <-> input
slider.addEventListener('input', () => { numInput.value = slider.value; });
numInput.addEventListener('input', () => {
  let v = parseInt(numInput.value) || 0;
  if (v > 85) { v = 85; numInput.value = v; }
  if (v < 0) { v = 0; numInput.value = v; }
  slider.value = v;
});

// Apply button
btnApply.addEventListener('click', () => {
  const target = parseInt(numInput.value);
  if (isNaN(target) || target < 0 || target > 85) {
    alert('0-85% range only');
    return;
  }
  if (target === 0) {
    showModal('Stop GPU Load?',
      'This will <span class="warn">stop ALL GPU load</span> ' +
      'on node2, node3, node4.',
      () => sendTarget(0));
  } else {
    showModal('Change Target?',
      `Set GPU utilization target to <span class="highlight">${target}%</span>?` +
      `<br><br>The controller will adjust batch size over 2-3 cycles (~5 min).`,
      () => sendTarget(target));
  }
});

// Emergency stop
btnStop.addEventListener('click', () => {
  showModal('Emergency Stop',
    '<span class="warn">STOP all GPU load immediately?</span>' +
    '<br><br>This scales the controller to 0 replicas.',
    () => sendTarget(0), true);
});

// Modal
function showModal(title, body, onConfirm, danger) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = body;
  const confirmBtn = $('#modal-confirm');
  if (danger) {
    confirmBtn.style.background = '#b71c1c';
    confirmBtn.textContent = 'Stop Now';
  } else {
    confirmBtn.style.background = '#1565c0';
    confirmBtn.textContent = 'Confirm';
  }
  pendingAction = onConfirm;
  modal.classList.add('show');
}
$('#modal-cancel').addEventListener('click', () => modal.classList.remove('show'));
$('#modal-confirm').addEventListener('click', () => {
  modal.classList.remove('show');
  if (pendingAction) pendingAction();
  pendingAction = null;
});

// API call
async function sendTarget(target) {
  btnApply.disabled = true;
  btnApply.textContent = 'Applying...';
  try {
    const r = await fetch('/api/target', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({target, confirm: true}),
    });
    const d = await r.json();
    if (!r.ok) {
      alert('Error: ' + (d.error || r.statusText));
    } else {
      // flash success
      btnApply.textContent = d.message || 'OK';
      setTimeout(fetchStatus, 2000);
    }
  } catch(e) {
    alert('Network error: ' + e);
  }
  setTimeout(() => {
    btnApply.disabled = false;
    btnApply.textContent = 'Apply Target';
  }, 3000);
}

// Status polling
async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    renderStatus(d);
  } catch(e) {
    $('#status-badge').textContent = 'Connection Error';
    $('#status-badge').className = 'badge badge-warn';
  }
}

function barColor(pct) {
  if (pct >= 90) return '#ef5350';
  if (pct >= 75) return '#ffb74d';
  if (pct >= 50) return '#4fc3f7';
  return '#66bb6a';
}

function tempColor(t) {
  if (t >= 83) return '#ef5350';
  if (t >= 75) return '#ffb74d';
  if (t >= 60) return '#4fc3f7';
  return '#66bb6a';
}

function renderStatus(d) {
  // Badge
  const dep = d.deployment;
  const badge = $('#status-badge');
  if (dep.ready > 0) {
    badge.textContent = 'Running';
    badge.className = 'badge badge-run';
  } else if (dep.replicas > 0) {
    badge.textContent = 'Starting...';
    badge.className = 'badge badge-warn';
  } else {
    badge.textContent = 'Stopped';
    badge.className = 'badge badge-stop';
  }

  // Current target
  $('#current-target').textContent = d.target;

  // Sync slider if user isn't dragging
  if (document.activeElement !== slider && document.activeElement !== numInput) {
    slider.value = d.target;
    numInput.value = d.target;
  }

  // GPU cards
  const cardsEl = $('#gpu-cards');
  let html = '';
  const nodes = ['node2','node3','node4'];
  for (const n of nodes) {
    const g = (d.gpu_nodes || {})[n] || {};
    const util = g.util_blended || 0;
    const utilDcgm = g.util_dcgm || 0;
    const temp = g.temp || 0;
    const power = g.power || 0;
    const vram = g.vram_pct || 0;
    const model = g.gpu_model || '';
    html += `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">${n}</div>
          <div class="card-subtitle">${model}</div>
        </div>
        <div style="font-size:28px;font-weight:700;color:${barColor(util)};
                    font-variant-numeric:tabular-nums">${util.toFixed(0)}%</div>
      </div>
      <div class="bar-bg"><div class="bar-fill" style="width:${util}%;background:${barColor(util)}"></div></div>
      <div style="height:10px"></div>
      <div class="metric">
        <span class="metric-label">DCGM Util</span>
        <span class="metric-value">${utilDcgm.toFixed(0)}%</span>
      </div>
      <div class="metric">
        <span class="metric-label">Temperature</span>
        <span class="metric-value" style="color:${tempColor(temp)}">${temp.toFixed(0)} C</span>
      </div>
      <div class="metric">
        <span class="metric-label">Power</span>
        <span class="metric-value">${power.toFixed(0)} W</span>
      </div>
      <div class="metric">
        <span class="metric-label">VRAM</span>
        <span class="metric-value" style="color:${vram>85?'#ef5350':vram>70?'#ffb74d':'#66bb6a'}">${vram.toFixed(0)}%</span>
      </div>
    </div>`;
  }
  cardsEl.innerHTML = html;

  // Config bar
  const c = d.config || {};
  $('#config-bar').innerHTML =
    `interval=${c.control_interval}s &middot; temp_limit=${c.temp_limit} C` +
    ` &middot; max_batch=${c.max_batch} &middot; init_batch=${c.initial_batch}`;

  // Timestamp
  $('#update-time').textContent = `Last updated: ${d.timestamp}`;
}

// Initial load + auto-refresh
fetchStatus();
setInterval(fetchStatus, 10000);
</script>
</body>
</html>
"""

# ── Main ───────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    server = ThreadedHTTPServer(("0.0.0.0", port), PUEHandler)
    log.info("PUE Web UI listening on :%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()
