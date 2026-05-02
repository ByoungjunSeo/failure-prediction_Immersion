# K8s-05. 데이터 수집

## ESXi 수집 CronJob (1분마다)

```yaml
# k8s/cronjobs/esxi-collector.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: esxi-collector
  namespace: failure-prediction
spec:
  schedule: "* * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: collector
              image: 10.100.230.6:5000/failure-pred:cpu-latest
              command: ["python", "-m", "src.collectors.esxi_collector_job"]
              env:
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

## src/collectors/esxi_collector_job.py

```python
"""1분마다 CronJob으로 실행 — ESXi 메트릭 → VictoriaMetrics"""
import os, logging, yaml, requests, time
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VM_URL = os.getenv("VICTORIA_METRICS_URL", "http://victoria-metrics-svc:8428")

def main():
    with open("/app/configs/servers.yaml") as f:
        cfg = yaml.safe_load(f)

    for host in cfg["esxi_hosts"]:
        try:
            from src.collectors.esxi_collector import ESXiCollector
            metrics = ESXiCollector(host["ip"]).collect()
            _push(metrics, host["id"])

            if int(time.time()) % 300 < 60:   # 5분마다 SSH 수집
                from src.collectors.esxi_ssh_collector import ESXiSSHCollector
                errors = ESXiSSHCollector(host["ip"]).get_vmkernel_memory_errors()
                _push({"esxi_vmkernel_error_cnt": len(errors)}, host["id"])
        except Exception as e:
            logger.error(f"수집 실패 {host['id']}: {e}")

def _push(metrics: dict, host_id: str):
    lines = [f'{k}{{host="{host_id}"}} {v}' for k, v in metrics.items()]
    requests.post(f"{VM_URL}/api/v1/import/prometheus",
                  data="\n".join(lines), timeout=10)

if __name__ == "__main__":
    main()
```

---

## XGBoost 재학습 CronJob (새벽 2시)

```yaml
# k8s/cronjobs/retrain-cronjob.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: xgboost-retrain
  namespace: failure-prediction
spec:
  schedule: "0 2 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          nodeSelector:
            kubernetes.io/hostname: node1
          restartPolicy: OnFailure
          containers:
            - name: retrain
              image: 10.100.230.6:5000/failure-pred:gpu-latest
              command: ["python", "-m", "src.training.train_pipeline",
                        "--stage", "xgboost", "--days", "90"]
              resources:
                limits:
                  nvidia.com/gpu: "1"
                  cpu: "8"
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
                - name: VICTORIA_METRICS_URL
                  value: http://victoria-metrics-svc:8428
                - name: POSTGRES_URI
                  value: postgresql://hpcdev:$(POSTGRES_PASSWORD)@postgresql-svc:5432/failure_pred
                - name: POSTGRES_PASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: pg-secret
                      key: password
                - name: CUDA_VISIBLE_DEVICES
                  value: "3"    # T4 4번째 GPU (나머지는 추론용)
```

---

## 앙상블 가중치 최적화 CronJob (매주 일요일)

```yaml
# k8s/cronjobs/weekly-tune.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: weekly-ensemble-tune
  namespace: failure-prediction
spec:
  schedule: "0 3 * * 0"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: tune
              image: 10.100.230.6:5000/failure-pred:cpu-latest
              command: ["python", "-m", "src.training.ensemble_optimizer",
                        "--trials", "50"]
              env:
                - name: MLFLOW_TRACKING_URI
                  value: http://mlflow-svc:5000
```
