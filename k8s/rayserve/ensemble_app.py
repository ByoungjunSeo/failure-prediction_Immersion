"""Ray Serve 앙상블 추론 (K8s GPU — 전모델 실동작)."""

import logging
import os
import sys
import asyncio

import aiohttp
import numpy as np
import pandas as pd
import ray
from fastapi import FastAPI
from ray import serve

sys.path.insert(0, "/app")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="HPC Memory Failure Prediction API (K8s)")
VM_URL = os.getenv("VICTORIA_METRICS_URL", "http://victoria-metrics-svc.failure-prediction:8428")
PROM_URL = os.getenv(
    "PROMETHEUS_URL",
    "http://monitoring-kube-prometheus-prometheus.monitoring:9090",
)

# 자기-모니터링 대상 5 노드. instance 라벨(node-exporter)이 IP:9100 형식이라 매핑 보유.
SELF_NODES = ["node1", "node2", "node3", "node4", "node5"]
SELF_NODE_INSTANCE = {f"node{i}": f"10.100.230.{129 + i}:9100"
                      for i in range(1, 6)}


@serve.deployment(num_replicas=4, max_replicas_per_node=1,
                  ray_actor_options={"num_cpus": 1, "num_gpus": 0.25},
                  health_check_period_s=30, health_check_timeout_s=10)
class ChronosPredictor:
    def __init__(self):
        self.pipeline = None
        try:
            import torch
            from chronos import ChronosPipeline
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            self.pipeline = ChronosPipeline.from_pretrained(
                "amazon/chronos-t5-small", device_map=device, torch_dtype=torch.float32)
            logger.info("Chronos loaded: %s", device)
        except Exception as e:
            logger.warning("Chronos dummy: %s", e)

    async def predict(self, ce_values):
        if self.pipeline is None:
            return {"anomaly_score": 0.5, "model": "chronos"}
        try:
            import torch
            context = torch.tensor(ce_values[-4320:], dtype=torch.float32).unsqueeze(0)
            forecast = self.pipeline.predict(inputs=context, prediction_length=64,
                                             num_samples=20, limit_prediction_length=False)
            peak = float(forecast[0].median(dim=0)[0].cpu().numpy().max())
            mean = float(np.mean(ce_values[-60:])) if len(ce_values) >= 60 else max(float(np.mean(ce_values)), 1e-9)
            score = min(1.0, peak / (mean + 1e-9) / 20.0)
        except Exception:
            score = 0.5
        return {"anomaly_score": score, "model": "chronos"}


@serve.deployment(num_replicas=4, max_replicas_per_node=1,
                  ray_actor_options={"num_cpus": 1, "num_gpus": 0.25},
                  health_check_period_s=30, health_check_timeout_s=10)
class MOIRAIPredictor:
    def __init__(self):
        self.model = None
        try:
            import torch
            from uni2ts.model.moirai import MoiraiForecast, MoiraiModule
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            module = MoiraiModule.from_pretrained("Salesforce/moirai-1.0-R-small")
            self.model = MoiraiForecast(module=module, prediction_length=64, context_length=512,
                patch_size="auto", num_samples=20, target_dim=1,
                feat_dynamic_real_dim=0, past_feat_dynamic_real_dim=0).to(device)
            logger.info("MOIRAI loaded: %s", device)
        except Exception as e:
            logger.warning("MOIRAI dummy: %s", e)

    async def predict(self, ce_values):
        if self.model is None:
            return {"anomaly_score": 0.5, "model": "moirai"}
        try:
            from gluonts.dataset.pandas import PandasDataset
            ctx = pd.Series(ce_values[-512:], dtype=np.float64)
            ctx.index = pd.date_range(end=pd.Timestamp.now(), periods=len(ctx), freq="min")
            ctx.name = "target"
            forecasts = list(self.model.create_predictor(batch_size=1).predict(PandasDataset({"target": ctx})))
            std = float(forecasts[0].samples.std(axis=0).mean()) if forecasts else 0
            score = min(1.0, std / 10.0)
        except Exception:
            score = 0.5
        return {"anomaly_score": score, "model": "moirai"}


@serve.deployment(num_replicas=2, ray_actor_options={"num_cpus": 1},
                  health_check_period_s=30, health_check_timeout_s=10)
class XGBoostPredictor:
    def __init__(self):
        self.model = None
        self.feature_names = None
        try:
            import xgboost as xgb
            self.model = xgb.XGBClassifier()
            self.model.load_model("/app/models/checkpoints/xgboost_model.json")
            self.feature_names = self.model.get_booster().feature_names
            logger.info("XGBoost loaded: %d features", len(self.feature_names or []))
        except Exception as e:
            logger.warning("XGBoost dummy: %s", e)

    async def predict(self, features):
        if self.model is None or not features:
            return {"anomaly_score": 0.5, "model": "xgboost"}
        try:
            cols = self.feature_names or list(features.keys())
            df = pd.DataFrame([{c: features.get(c, float("nan")) for c in cols}], columns=cols)
            prob = float(self.model.predict_proba(df)[0][1])
        except Exception as e:
            logger.warning("XGBoost predict failed: %s", e)
            prob = 0.5
        return {"anomaly_score": prob, "model": "xgboost"}


@serve.deployment(num_replicas=4, max_replicas_per_node=1,
                  ray_actor_options={"num_cpus": 1, "num_gpus": 0.10},
                  health_check_period_s=30, health_check_timeout_s=10)
class AnomalyTransformerPredictor:
    def __init__(self):
        import math
        self._log1p = math.log1p
        self.at_model = None
        self.threshold = 1.0
        self.win_size = 100
        self.device = "cpu"
        try:
            import torch
            sys.path.insert(0, "/app/vendor/Anomaly-Transformer")
            from model.AnomalyTransformer import AnomalyTransformer
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            self.device = device
            ckpt = torch.load("/app/models/checkpoints/anomaly_transformer.pt",
                              map_location=device, weights_only=False)
            cfg = ckpt["config"]
            self.threshold = float(ckpt["threshold"])
            self.win_size = cfg["win_size"]
            self.at_model = AnomalyTransformer(win_size=cfg["win_size"], enc_in=1, c_out=1,
                d_model=cfg["d_model"], n_heads=cfg["n_heads"],
                e_layers=cfg["e_layers"], d_ff=cfg["d_ff"])
            self.at_model.load_state_dict(ckpt["model_state_dict"])
            self.at_model.to(device).eval()
            logger.info("AnomalyT loaded: %s (ckpt threshold=%.4f)", device, self.threshold)
        except Exception as e:
            logger.warning("AnomalyT dummy: %s", e)

        # 운영 시 ENV로 threshold 오버라이드 가능 (CE 분포 변동에 따라 재보정).
        override = os.getenv("AT_THRESHOLD")
        if override:
            try:
                self.threshold = float(override)
                logger.info("AnomalyT threshold override (AT_THRESHOLD=%s)", override)
            except ValueError:
                logger.warning("AT_THRESHOLD parse failed: %s", override)

    async def predict(self, ce_values):
        if self.at_model is None:
            return {"anomaly_score": 0.5, "model": "anomaly_transformer"}
        try:
            import torch
            data = np.array(ce_values[-self.win_size:], dtype=np.float32)
            if len(data) < self.win_size:
                data = np.pad(data, (self.win_size - len(data), 0))
            tensor = torch.tensor(data).unsqueeze(0).unsqueeze(-1).to(self.device)
            with torch.no_grad():
                output, *_ = self.at_model(tensor)
                rec_error = float(torch.mean((output - tensor) ** 2).item())
            # rec_error 스케일이 서버 간 ~1000배 차이가 나서 linear 비로는 포화됨.
            # log1p 변환으로 압축한 뒤 정규화하여 0..1 사이 부드러운 스코어를 얻는다.
            score = min(1.0, self._log1p(max(rec_error, 0.0)) /
                              (self._log1p(max(self.threshold, 1e-9)) + 1e-9))
        except Exception as e:
            logger.warning("AnomalyT predict failed: %s", e)
            score = 0.5
        return {"anomaly_score": score, "model": "anomaly_transformer"}


def _get_ce_values(server_id, window_minutes=4320):
    """CE 시계열을 가져온다.

    실측(`source="real"`)과 시뮬레이터(`source="sim"`) 양쪽 라벨을 모두 합산하여
    하나의 시계열로 돌려준다. 운영 EDAC 데이터가 0이면 시뮬레이터 패턴이 그대로
    드러나고, 실 데이터가 발생하면 그 값이 더해져 노출된다.
    """
    import requests, time
    try:
        now = int(time.time())
        query = (
            f'sum by (server) (memory_errors_correctable{{server="{server_id}"}})'
        )
        resp = requests.get(
            f"{VM_URL}/api/v1/query_range",
            params={"query": query, "start": now - window_minutes * 60,
                    "end": now, "step": "60s"},
            timeout=10,
        )
        data = resp.json().get("data", {}).get("result", [])
        if data and "values" in data[0]:
            return [float(v[1]) for v in data[0]["values"]]
    except Exception as e:
        logger.warning("CE query failed for %s: %s", server_id, e)
    return [0.0] * 100


def _prom_query_range(query, start, end, step="60s", timeout=10):
    """PromQL range query, returns list of (timestamp, value) per series."""
    import requests
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query_range",
                         params={"query": query, "start": start, "end": end, "step": step},
                         timeout=timeout)
        return r.json().get("data", {}).get("result", [])
    except Exception as e:
        logger.warning("Prom range query failed: %s", e)
        return []


def _prom_query(query, timeout=5):
    """PromQL instant query, returns single float (first series) or None."""
    import requests
    try:
        r = requests.get(f"{PROM_URL}/api/v1/query",
                         params={"query": query}, timeout=timeout)
        res = r.json().get("data", {}).get("result", [])
        if res:
            return float(res[0]["value"][1])
    except Exception as e:
        logger.warning("Prom query failed: %s", e)
    return None


def _get_node_cpu_series(node_name, window_minutes=60):
    """노드의 CPU 사용률 시계열(0.0~1.0) 반환. 1분 간격, length=window_minutes."""
    import time
    inst = SELF_NODE_INSTANCE.get(node_name)
    if not inst:
        return [0.0] * window_minutes
    now = int(time.time())
    # mode!="idle" 합 ÷ 전체 = 사용률. instance 매칭으로 한 노드만.
    query = (f'1 - avg by(instance)(rate(node_cpu_seconds_total'
             f'{{instance="{inst}",mode="idle"}}[1m]))')
    res = _prom_query_range(query, now - window_minutes * 60, now, "60s")
    if res and "values" in res[0]:
        return [float(v[1]) for v in res[0]["values"]]
    return [0.0] * window_minutes


def _compute_xgb_features(ce_values):
    """학습된 XGBoost 모델이 기대하는 18개 피처를 계산.

    CE 시계열에서 9개를 계산하고, 하드웨어/워크로드 9개는 NaN으로 둬서
    XGBoost가 학습 시 배운 missing-value routing을 그대로 사용하게 한다.
    """
    arr = np.asarray(ce_values, dtype=np.float64)
    n = len(arr)
    feats = {}

    # CE 누적
    feats["ce_count_1h"]  = float(arr[-60:].sum())   if n >= 1 else 0.0
    feats["ce_count_24h"] = float(arr[-1440:].sum()) if n >= 1 else 0.0
    feats["ce_count_72h"] = float(arr[-4320:].sum()) if n >= 1 else 0.0

    # 24시간 기울기 (선형회귀 slope)
    win24 = arr[-1440:]
    if len(win24) > 1 and win24.sum() > 0:
        x = np.arange(len(win24), dtype=np.float64)
        slope = float(np.polyfit(x, win24, 1)[0])
    else:
        slope = 0.0
    feats["ce_slope_24h"] = slope

    # Burst ratio: 최근 1h 평균 / 그 이전 평균
    recent = float(arr[-60:].mean()) if n >= 60 else (float(arr.mean()) if n else 0.0)
    base   = float(arr[:-60].mean()) if n > 60 else 0.0
    burst_ratio = recent / (base + 1e-9)
    feats["ce_burst_ratio"] = burst_ratio
    feats["ce_burst_flag"]  = 1 if burst_ratio > 10 else 0

    # 분포 통계
    feats["ce_max"] = float(arr.max()) if n else 0.0
    feats["ce_std"] = float(arr.std()) if n > 1 else 0.0

    # 가속도 (2차 차분 평균)
    feats["ce_acceleration"] = float(np.diff(arr, n=2).mean()) if n > 2 else 0.0

    # 미수집 피처는 NaN — XGBoost가 학습 때 학습한 default direction으로 분기
    for k in (
        "cpu_temp_mean_1h", "cpu_temp_slope_6h", "psu_voltage_stddev_1h",
        "fan_rpm_anomaly", "system_uptime_days", "power_consumption",
        "cpu_usage_mean_1h", "memory_used_pct", "swap_usage_pct",
    ):
        feats[k] = float("nan")

    return feats


@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 0.5},
                  health_check_period_s=60, health_check_timeout_s=15)
class LLMEmbeddingAnomalyPredictor:
    """CE 시계열을 텍스트로 요약 → Qwen3-Embedding(NPU) 임베딩 → baseline 거리 → anomaly score.

    NPU 추론은 별도 Deployment(`npu-embed-svc`)가 OpenAI 호환 HTTP API로 제공.
    Ray 액터는 HTTP 클라이언트만 들고 NPU를 직접 잡지 않으므로 GPU/CPU 워커 어디서나 실행.
    """

    BASELINE_TEXT = ("CE counts last 60 minutes: mean=0.00, max=0, std=0.00, "
                     "slope=0.00, nonzero_frac=0.00")

    def __init__(self):
        self.npu_url = os.getenv(
            "NPU_EMBED_URL",
            "http://npu-embed-svc.failure-prediction:8000/v1/embeddings")
        self.model_id = os.getenv("NPU_EMBED_MODEL", "qwen3-embed")
        self.timeout_s = float(os.getenv("NPU_EMBED_TIMEOUT_S", "3"))
        self._baseline = None
        self._session = None
        logger.info("LLM-embed predictor ready (NPU URL=%s)", self.npu_url)

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        return self._session

    @staticmethod
    def _series_to_text(ce_values):
        recent = np.asarray(ce_values[-60:], dtype=np.float32) if ce_values else np.zeros(60, dtype=np.float32)
        if recent.size == 0:
            recent = np.zeros(1, dtype=np.float32)
        slope = float(recent[-1] - recent[0]) if recent.size >= 2 else 0.0
        return (f"CE counts last 60 minutes: mean={float(recent.mean()):.2f}, "
                f"max={int(recent.max())}, std={float(recent.std()):.2f}, "
                f"slope={slope:.2f}, nonzero_frac={float((recent > 0).mean()):.2f}")

    async def _embed(self, text):
        session = await self._get_session()
        async with session.post(self.npu_url,
                                 json={"model": self.model_id, "input": text}) as r:
            r.raise_for_status()
            payload = await r.json()
        return np.asarray(payload["data"][0]["embedding"], dtype=np.float32)

    async def predict(self, ce_values):
        try:
            if self._baseline is None:
                self._baseline = await self._embed(self.BASELINE_TEXT)
            emb = await self._embed(self._series_to_text(ce_values))
            denom = float(np.linalg.norm(emb) * np.linalg.norm(self._baseline)) + 1e-9
            cos = float(np.dot(emb, self._baseline) / denom)
            score = max(0.0, min(1.0, 1.0 - cos))
        except Exception as e:
            logger.warning("LLM-embed predict fail: %s", e)
            score = 0.5
        return {"anomaly_score": score, "model": "llm_embedding"}


@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 1})
@serve.ingress(app)
class AnomalyEnsemble:
    def __init__(self, chronos, moirai, xgboost, anomaly_t, llm_embed):
        self.chronos = chronos
        self.moirai = moirai
        self.xgboost = xgboost
        self.anomaly_t = anomaly_t
        self.llm_embed = llm_embed
        # ESXi 추론 — 기존 4 predictor 가중치 (llm_embedding은 Phase A 로깅만).
        self.weights = {
            "chronos": 0.25, "moirai": 0.15, "xgboost": 0.35,
            "anomaly_transformer": 0.25, "llm_embedding": 0.0,
        }
        # 노드 self-monitoring 가중치 — XGBoost는 노드용 학습 모델 없어 0 (dummy).
        # 시계열 predictor들에 가중치 재배분, llm_embedding 검증 가중치 부여 시작.
        self.weights_node = {
            "chronos": 0.30, "moirai": 0.25,
            "anomaly_transformer": 0.30, "llm_embedding": 0.15,
            "xgboost": 0.0,
        }
        self.servers = ["vmgnode18", "vmgnode23", "vmgnode26", "vmgnode30"]
        self.nodes = list(SELF_NODES)

    @app.get("/health")
    async def health(self):
        return {"status": "healthy",
                "models": list(self.weights.keys()),
                "esxi_targets": self.servers,
                "node_targets": self.nodes,
                "platform": "k8s-ray-serve-gpu+npu"}

    # ---- ESXi 추론 (기존) ----
    @app.get("/predict/all")
    async def predict_all(self):
        predictions = []
        for srv in self.servers:
            r = await self._predict_single(srv)
            predictions.append(r)
        return {"predictions": predictions, "total": len(predictions),
                "warning_count": sum(1 for p in predictions if p["risk_level"] == "WARNING"),
                "critical_count": sum(1 for p in predictions if p["risk_level"] == "CRITICAL")}

    @app.get("/predict/esxi/all")
    async def predict_esxi_all(self):
        return await self.predict_all()

    @app.get("/predict/{server_id}")
    async def predict(self, server_id: str):
        return await self._predict_single(server_id)

    @app.get("/predict/esxi/{server_id}")
    async def predict_esxi(self, server_id: str):
        return await self._predict_single(server_id)

    # ---- 신규 5 노드 self-monitoring 추론 ----
    @app.get("/predict/node/all")
    async def predict_node_all(self):
        predictions = []
        for n in self.nodes:
            r = await self._predict_node_single(n)
            predictions.append(r)
        return {"predictions": predictions, "total": len(predictions),
                "target_type": "node",
                "warning_count": sum(1 for p in predictions if p["risk_level"] == "WARNING"),
                "critical_count": sum(1 for p in predictions if p["risk_level"] == "CRITICAL")}

    @app.get("/predict/node/{node_name}")
    async def predict_node(self, node_name: str):
        return await self._predict_node_single(node_name)

    async def _predict_single(self, server_id):
        ce_values = _get_ce_values(server_id)
        xgb_features = _compute_xgb_features(ce_values)
        results = await asyncio.gather(
            self.chronos.predict.remote(ce_values),
            self.moirai.predict.remote(ce_values),
            self.xgboost.predict.remote(xgb_features),
            self.anomaly_t.predict.remote(ce_values),
            self.llm_embed.predict.remote(ce_values),
        )
        scores = {}
        weighted_sum = 0.0
        for r in results:
            scores[r["model"]] = r["anomaly_score"]
            weighted_sum += self.weights.get(r["model"], 0) * r["anomaly_score"]
        risk = "CRITICAL" if weighted_sum >= 0.85 else "WARNING" if weighted_sum >= 0.65 else "NORMAL" if weighted_sum > 0.30 else "RECOVERY"
        return {"server_id": server_id, "failure_probability": round(weighted_sum, 4),
                "risk_level": risk, "model_scores": scores}

    async def _predict_node_single(self, node_name):
        """노드 자원사용률 기반 추론. 첫 단계는 CPU usage 시계열을 모든
        시계열 predictor에 전달, XGBoost는 dummy(weight 0)."""
        if node_name not in SELF_NODE_INSTANCE:
            return {"node": node_name, "error": "unknown node"}
        cpu_series = _get_node_cpu_series(node_name, 60)   # 60분, 1-min step
        results = await asyncio.gather(
            self.chronos.predict.remote(cpu_series),
            self.moirai.predict.remote(cpu_series),
            self.xgboost.predict.remote({}),               # 노드용 학습 모델 없음 — dummy
            self.anomaly_t.predict.remote(cpu_series),
            self.llm_embed.predict.remote(cpu_series),
        )
        scores = {}
        weighted_sum = 0.0
        for r in results:
            scores[r["model"]] = r["anomaly_score"]
            weighted_sum += self.weights_node.get(r["model"], 0) * r["anomaly_score"]
        risk = ("CRITICAL" if weighted_sum >= 0.85 else
                "WARNING"  if weighted_sum >= 0.65 else
                "NORMAL"   if weighted_sum >  0.30 else
                "RECOVERY")
        return {"node": node_name, "failure_probability": round(weighted_sum, 4),
                "risk_level": risk, "model_scores": scores,
                "cpu_used_pct_last": round(cpu_series[-1] * 100, 2) if cpu_series else None}

    @app.get("/metrics")
    async def metrics(self):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("failure_prediction_up 1\n", media_type="text/plain")


chronos = ChronosPredictor.bind()
moirai = MOIRAIPredictor.bind()
xgboost = XGBoostPredictor.bind()
anomaly_t = AnomalyTransformerPredictor.bind()
llm_embed = LLMEmbeddingAnomalyPredictor.bind()
ensemble = AnomalyEnsemble.bind(chronos, moirai, xgboost, anomaly_t, llm_embed)
