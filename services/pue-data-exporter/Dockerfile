# PUE Data Exporter — 독립 이미지(선택). 운영 배포는 공유 이미지에 ConfigMap
# 마운트 방식(deploy/exporter/)을 사용하지만, 단독 빌드/이식용으로 유지한다.
#
#   nerdctl --namespace k8s.io build -t 10.100.230.130:5000/pue-data-exporter:latest .
#   nerdctl --namespace k8s.io push  10.100.230.130:5000/pue-data-exporter:latest
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY exporter.py app.py ./

ENV PORT=8080 \
    PROMETHEUS_URL=http://monitoring-kube-prometheus-prometheus.monitoring:9090 \
    VM_URL=http://victoria-metrics-svc:8428

EXPOSE 8080
CMD ["python3", "app.py"]
