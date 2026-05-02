# K8s-04. Ray Serve 구조

## RayCluster YAML

```yaml
# k8s/raycluster.yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: failure-pred-cluster
  namespace: failure-prediction
spec:
  rayVersion: "2.9.0"

  headGroupSpec:
    serviceType: ClusterIP
    rayStartParams:
      dashboard-host: "0.0.0.0"
      num-cpus: "8"
      num-gpus: "3"             # T4 3장: Chronos(0.5) + MOIRAI(0.5) + AnomalyT(0.5) + 여유
    template:
      spec:
        nodeSelector:
          kubernetes.io/hostname: node1
        containers:
          - name: ray-head
            image: 10.100.230.6:5000/failure-pred:gpu-latest
            imagePullPolicy: Always
            resources:
              requests:
                cpu: "8"
                memory: "32Gi"
                nvidia.com/gpu: "3"
              limits:
                cpu: "32"
                memory: "64Gi"
                nvidia.com/gpu: "3"
            env:
              - name: MLFLOW_TRACKING_URI
                value: http://mlflow-svc:5000
              - name: MLFLOW_S3_ENDPOINT_URL
                value: http://minio-svc:9000
              - name: AWS_ACCESS_KEY_ID
                valueFrom:
                  secretKeyRef:
                    name: minio-secret
                    key: access-key
              - name: AWS_SECRET_ACCESS_KEY
                valueFrom:
                  secretKeyRef:
                    name: minio-secret
                    key: secret-key
              - name: ESXI_PASSWORD
                valueFrom:
                  secretKeyRef:
                    name: esxi-credentials
                    key: password
              - name: VICTORIA_METRICS_URL
                value: http://victoria-metrics-svc:8428
              - name: SLACK_WEBHOOK_URL
                valueFrom:
                  secretKeyRef:
                    name: slack-secret
                    key: webhook-url
            volumeMounts:
              - name: servers-config
                mountPath: /app/configs
        volumes:
          - name: servers-config
            configMap:
              name: servers-config

  workerGroupSpecs:
    - groupName: cpu-workers
      replicas: 4
      minReplicas: 2
      maxReplicas: 5
      rayStartParams:
        num-cpus: "8"
      template:
        spec:
          affinity:
            podAntiAffinity:
              preferredDuringSchedulingIgnoredDuringExecution:
                - weight: 100
                  podAffinityTerm:
                    topologyKey: kubernetes.io/hostname
                    labelSelector:
                      matchLabels:
                        ray.io/cluster: failure-pred-cluster
          containers:
            - name: ray-worker
              image: 10.100.230.6:5000/failure-pred:cpu-latest
              imagePullPolicy: Always
              resources:
                requests:
                  cpu: "8"
                  memory: "16Gi"
                limits:
                  cpu: "16"
                  memory: "32Gi"
              env:
                - name: MLFLOW_TRACKING_URI
                  value: http://mlflow-svc:5000
                - name: MLFLOW_S3_ENDPOINT_URL
                  value: http://minio-svc:9000
                - name: AWS_ACCESS_KEY_ID
                  valueFrom:
                    secretKeyRef:
                      name: minio-secret
                      key: access-key
                - name: AWS_SECRET_ACCESS_KEY
                  valueFrom:
                    secretKeyRef:
                      name: minio-secret
                      key: secret-key
                - name: ESXI_PASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: esxi-credentials
                      key: password
                - name: VICTORIA_METRICS_URL
                  value: http://victoria-metrics-svc:8428
              volumeMounts:
                - name: servers-config
                  mountPath: /app/configs
          volumes:
            - name: servers-config
              configMap:
                name: servers-config
```

---

## Ray Serve 앱 — ensemble_app.py

```python
# src/ray_serve/ensemble_app.py
import os, logging
from typing import Dict, Any
from ray import serve
from fastapi import FastAPI

logger = logging.getLogger(__name__)
app = FastAPI(title="HPC 장애 예측 API", version="2.0.0")

VICTORIA_METRICS_URL = os.getenv("VICTORIA_METRICS_URL",
                                  "http://victoria-metrics-svc:8428")
MLFLOW_URI = os.getenv("MLFLOW_TRACKING_URI", "http://mlflow-svc:5000")


@serve.deployment(name="ChronosPredictor", num_replicas=1,
                  ray_actor_options={"num_gpus": 0.5, "num_cpus": 2})
class ChronosPredictor:
    def __init__(self):
        from chronos import ChronosPipeline
        import torch
        self.pipeline = ChronosPipeline.from_pretrained(
            "amazon/chronos-t5-small",
            device_map="cuda", torch_dtype=torch.float16)
        logger.info("Chronos 로드 완료")

    async def predict(self, ce_series: list) -> float:
        import torch
        ctx = torch.tensor(ce_series, dtype=torch.float32)
        fc  = self.pipeline.predict(ctx.unsqueeze(0), 1440, num_samples=20)
        cur = sum(ce_series[-60:]) / max(len(ce_series[-60:]), 1)
        pk  = fc[0].median(dim=0).values.max().item()
        return float(min(pk / (cur + 1e-9) / 10.0, 1.0))


@serve.deployment(name="MOIRAIPredictor", num_replicas=1,
                  ray_actor_options={"num_gpus": 0.5, "num_cpus": 2})
class MOIRAIPredictor:
    def __init__(self):
        from uni2ts.model.moirai import MoiraiForecast
        self.model = MoiraiForecast.from_pretrained(
            "Salesforce/moirai-1.0-R-small",
            prediction_length=1440, context_length=4320,
            patch_size=32, num_samples=20,
            target_dim=1, feat_dynamic_real_dim=0,
            past_feat_dynamic_real_dim=0).to("cuda")
        logger.info("MOIRAI 로드 완료")

    async def predict(self, ce_series: list) -> float:
        import numpy as np
        s = float(np.std(ce_series[-360:]))
        m = float(np.mean(ce_series[-360:])) + 1e-9
        return float(min(s / m, 1.0))


@serve.deployment(name="XGBoostPredictor", num_replicas=2,
                  ray_actor_options={"num_cpus": 2})
class XGBoostPredictor:
    def __init__(self):
        import mlflow
        mlflow.set_tracking_uri(MLFLOW_URI)
        self.model = mlflow.xgboost.load_model("models:/xgb_finetuned/Production")
        logger.info("XGBoost 로드 완료")

    async def predict(self, features: dict) -> float:
        import pandas as pd
        X = pd.DataFrame([features])
        return float(self.model.predict_proba(X)[0][1])


@serve.deployment(name="AnomalyTransformerPredictor", num_replicas=1,
                  ray_actor_options={"num_gpus": 0.5, "num_cpus": 2})
class AnomalyTransformerPredictor:
    def __init__(self):
        import mlflow, torch
        mlflow.set_tracking_uri(MLFLOW_URI)
        self.model = mlflow.pytorch.load_model("models:/anomaly_t/Production")
        self.model.eval().cuda()
        logger.info("Anomaly Transformer 로드 완료")

    async def predict(self, ce_series: list) -> float:
        import torch
        x = torch.tensor(ce_series, dtype=torch.float32).unsqueeze(0).unsqueeze(-1).cuda()
        with torch.no_grad():
            score = self.model(x).squeeze().mean().item()
        return float(min(abs(score), 1.0))


@serve.deployment(name="AnomalyEnsemble", num_replicas=1,
                  ray_actor_options={"num_cpus": 2})
@serve.ingress(app)
class AnomalyEnsemble:
    def __init__(self, chronos, moirai, xgboost, anomaly_t):
        self.chronos   = chronos
        self.moirai    = moirai
        self.xgboost   = xgboost
        self.anomaly_t = anomaly_t

    @app.get("/health")
    async def health(self):
        return {"status": "ok", "version": "2.0.0-k8s"}

    @app.get("/predict/{server_id}")
    async def predict(self, server_id: str):
        import asyncio
        from src.features.feature_pipeline import FeaturePipeline
        pipeline  = FeaturePipeline(victoria_url=VICTORIA_METRICS_URL)
        features  = await pipeline.build_feature_vector(server_id)
        ce_series = features.pop("ce_series_raw", [0.0] * 4320)

        s_ch, s_mo, s_xgb, s_at = await asyncio.gather(
            self.chronos.predict.remote(ce_series),
            self.moirai.predict.remote(ce_series),
            self.xgboost.predict.remote(features),
            self.anomaly_t.predict.remote(ce_series),
        )
        final = (s_ch + s_mo) * 0.4 + s_xgb * 0.35 + s_at * 0.25
        risk  = ("CRITICAL" if final >= 0.85 else
                 "WARNING"  if final >= 0.65 else
                 "RECOVERY" if final <= 0.30 else "NORMAL")
        return {
            "server_id": server_id,
            "failure_probability": round(final, 4),
            "risk_level": risk,
            "model_scores": {
                "chronos":   round(s_ch,  4),
                "moirai":    round(s_mo,  4),
                "xgboost":   round(s_xgb, 4),
                "anomaly_t": round(s_at,  4),
            },
        }

    @app.get("/predict/all")
    async def predict_all(self):
        import asyncio
        servers = ["vmgnode18", "vmgnode23", "vmgnode26", "vmgnode30"]
        results = await asyncio.gather(*[self.predict(s) for s in servers])
        return {"results": list(results)}

    @app.get("/metrics")
    async def metrics(self):
        from prometheus_client import generate_latest
        from starlette.responses import Response
        return Response(generate_latest(), media_type="text/plain")


# 엔트리포인트
chronos_h   = ChronosPredictor.bind()
moirai_h    = MOIRAIPredictor.bind()
xgboost_h   = XGBoostPredictor.bind()
anomaly_t_h = AnomalyTransformerPredictor.bind()
entrypoint  = AnomalyEnsemble.bind(chronos_h, moirai_h, xgboost_h, anomaly_t_h)
```

---

## RayService YAML

```yaml
# k8s/rayservice.yaml
apiVersion: ray.io/v1
kind: RayService
metadata:
  name: anomaly-service
  namespace: failure-prediction
spec:
  serviceUnhealthySecondThreshold: 600
  deploymentUnhealthySecondThreshold: 600
  serveConfigV2: |
    applications:
      - name: anomaly-app
        route_prefix: /
        import_path: src.ray_serve.ensemble_app:entrypoint
        runtime_env:
          working_dir: /app
        deployments:
          - name: AnomalyEnsemble
            num_replicas: 1
          - name: ChronosPredictor
            num_replicas: 1
            ray_actor_options: {num_gpus: 0.5}
          - name: MOIRAIPredictor
            num_replicas: 1
            ray_actor_options: {num_gpus: 0.5}
          - name: XGBoostPredictor
            num_replicas: 2
          - name: AnomalyTransformerPredictor
            num_replicas: 1
            ray_actor_options: {num_gpus: 0.5}
  rayClusterConfig:
    rayVersion: "2.9.0"
    headGroupSpec:
      serviceType: ClusterIP
      rayStartParams:
        dashboard-host: "0.0.0.0"
        num-cpus: "8"
        num-gpus: "3"
      template:
        spec:
          nodeSelector:
            kubernetes.io/hostname: node1
          containers:
            - name: ray-head
              image: 10.100.230.6:5000/failure-pred:gpu-latest
              resources:
                limits:
                  cpu: "32"
                  memory: "64Gi"
                  nvidia.com/gpu: "3"
    workerGroupSpecs:
      - groupName: cpu-workers
        replicas: 4
        minReplicas: 2
        maxReplicas: 5
        rayStartParams:
          num-cpus: "8"
        template:
          spec:
            containers:
              - name: ray-worker
                image: 10.100.230.6:5000/failure-pred:cpu-latest
                resources:
                  limits:
                    cpu: "16"
                    memory: "32Gi"
```

---

## 배포 및 확인

```bash
kubectl apply -f k8s/rayservice.yaml
kubectl get rayservice -n failure-prediction -w

# API 접근
kubectl port-forward svc/anomaly-service-serve-svc \
  8000:8000 -n failure-prediction

curl http://localhost:8000/health
curl http://localhost:8000/predict/vmgnode18
curl http://localhost:8000/predict/all
```
