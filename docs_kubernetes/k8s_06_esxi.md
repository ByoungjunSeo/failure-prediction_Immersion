# K8s-06. ESXi 연동

## ESXi 대응 CronJob (1분마다 Ray Serve API → ESXi 자동 대응)

```yaml
# k8s/cronjobs/esxi-response.yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: esxi-response
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
            - name: responder
              image: 10.100.230.6:5000/failure-pred:cpu-latest
              command: ["python", "-m", "src.esxi.response_job"]
              env:
                - name: RAY_SERVE_URL
                  value: http://anomaly-service-serve-svc:8000
                - name: ESXI_PASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: esxi-credentials
                      key: password
                - name: SLACK_WEBHOOK_URL
                  valueFrom:
                    secretKeyRef:
                      name: slack-secret
                      key: webhook-url
                - name: POSTGRES_URI
                  value: postgresql://hpcdev:$(POSTGRES_PASSWORD)@postgresql-svc:5432/failure_pred
                - name: POSTGRES_PASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: pg-secret
                      key: password
              volumeMounts:
                - name: servers-config
                  mountPath: /app/configs
          volumes:
            - name: servers-config
              configMap:
                name: servers-config
```

---

## src/esxi/response_job.py

```python
"""
k8s CronJob: Ray Serve /predict/all 호출 → ESXi 자동 대응
기존 action_handler.py 재사용
"""
import os, logging, requests
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

RAY_URL = os.getenv("RAY_SERVE_URL", "http://anomaly-service-serve-svc:8000")

def main():
    try:
        resp = requests.get(f"{RAY_URL}/predict/all", timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Ray Serve 호출 실패: {e}")
        return

    from src.esxi.action_handler import ESXiActionHandler
    handler = ESXiActionHandler()

    for result in resp.json()["results"]:
        risk = result["risk_level"]
        sid  = result["server_id"]
        try:
            if   risk == "WARNING":  handler.warning_response(sid, result)
            elif risk == "CRITICAL": handler.critical_response(sid, result)
            elif risk == "RECOVERY": handler.recovery_response(sid, result)
        except Exception as e:
            logger.error(f"ESXi 대응 실패 {sid}: {e}")

if __name__ == "__main__":
    main()
```

---

## ESXi 접속 정보

| 호스트 | IP | BMC | 계정 |
|---|---|---|---|
| vmgnode18 | 10.148.148.118 | 172.31.201.118 | root / VMware!0 |
| vmgnode23 | 10.148.148.123 | 172.31.201.123 | root / VMware!0 |
| vmgnode26 | 10.148.148.126 | 172.31.201.126 | root / VMware!0 |
| vmgnode30 | 10.148.148.130 | 172.31.201.130 | root / VMware!0 |

> vmgnode17 제외 (접속 장애)
> SSH 읽기 허용 / 패키지 설치·설정 변경 절대 금지
