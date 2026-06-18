# 08. 배포 가이드

> K8s 클러스터에 장애예측 시스템 전체를 배포하는 절차.

---

## 사전 요구사항

- **K8s 클러스터**: v1.27+ (5노드, GPU/NPU 지원)
- **GPU Operator**: NVIDIA GPU Operator (DCGM exporter 포함)
- **Furiosa SDK**: node5에 설치 (furiosa-device-plugin, furiosa-metrics-exporter)
- **kube-prometheus-stack**: Helm으로 사전 설치
- **Longhorn**: 분산 스토리지 (선택)
- **Container Registry**: node1에 로컬 레지스트리 (port 5000)

---

## 배포 순서

### 1단계: 네임스페이스 및 시크릿

```bash
# 네임스페이스 생성
kubectl create namespace failure-prediction

# ESXi 자격증명 (레거시, 현재 미사용)
kubectl create secret generic esxi-credentials \
  --from-literal=password='<ESXI_ROOT_PASSWORD>' \
  -n failure-prediction

# Slack webhook 시크릿
kubectl create secret generic slack-secret \
  --from-literal=webhook-url='https://hooks.slack.com/services/...' \
  -n failure-prediction

kubectl create secret generic slack-secret \
  --from-literal=webhook-url='https://hooks.slack.com/services/...' \
  -n monitoring
```

### 2단계: 인프라 Pod

```bash
# Container Registry (node1, bare Pod)
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: registry
  namespace: failure-prediction
  labels: {app: registry}
spec:
  nodeSelector:
    node-role.kubernetes.io/control-plane: ""
  tolerations:
  - key: node-role.kubernetes.io/control-plane
    operator: Exists
    effect: NoSchedule
  containers:
  - name: registry
    image: registry:2
    ports:
    - containerPort: 5000
      hostPort: 5000
    volumeMounts:
    - name: registry-data
      mountPath: /var/lib/registry
    resources:
      requests: {cpu: "100m", memory: "128Mi"}
  volumes:
  - name: registry-data
    hostPath: {path: /home/registry, type: DirectoryOrCreate}
  restartPolicy: Always
EOF

# VictoriaMetrics, PostgreSQL, MLflow, MinIO
# (별도 매니페스트 — 기존 클러스터에 이미 배포됨)
```

### 3단계: 컨테이너 이미지 빌드

```bash
# GPU 이미지 (Ray + 모든 모델 의존성)
ssh newcluster-master "cd /path/to/build && \
  nerdctl build -t 10.100.230.130:5000/failure-pred:gpu-latest \
    -f Dockerfile.gpu . && \
  nerdctl push 10.100.230.130:5000/failure-pred:gpu-latest"

# NPU 이미지 (Furiosa SDK + Qwen3-Embedding)
ssh newcluster-master "cd /path/to/build && \
  nerdctl build -t 10.100.230.130:5000/failure-pred:npu-latest \
    -f k8s/docker/Dockerfile.npu . && \
  nerdctl push 10.100.230.130:5000/failure-pred:npu-latest"
```

### 4단계: 모니터링 설정

```bash
# GPU 메트릭 ServiceMonitor
kubectl apply -f k8s/monitoring/dcgm-exporter-servicemonitor.yaml

# NPU 메트릭 ServiceMonitor
kubectl apply -f k8s/monitoring/furiosa-metrics-servicemonitor.yaml

# VictoriaMetrics Grafana datasource
kubectl apply -f k8s/monitoring/grafana-vm-datasource.yaml

# 알림 규칙
kubectl apply -f k8s/monitoring/alerts/failure-pred-rules.yaml
kubectl apply -f k8s/monitoring/alerts/alertmanager-slack.yaml
kubectl apply -f k8s/monitoring/alerts/vmalert.yaml
```

### 5단계: Grafana 대시보드

```bash
# 각 대시보드를 ConfigMap으로 등록
for f in k8s/grafana/dashboards/*.json; do
  name="dashboard-$(basename $f .json)"
  kubectl -n monitoring create configmap "$name" \
    --from-file="$(basename $f)=$f" \
    --dry-run=client -o yaml | \
    kubectl label --local -f - grafana_dashboard=1 -o yaml | \
    kubectl apply -f -
done
```

### 6단계: NPU 서비스

```bash
# NPU 아티팩트 PVC
kubectl apply -f k8s/infra/pvc-npu-artifacts.yaml

# NPU 아티팩트 빌드 (1회)
kubectl apply -f k8s/jobs/build-npu-artifact.yaml

# NPU embedding 서비스
kubectl apply -f k8s/infra/npu-embed.yaml
```

### 7단계: Ray Serve 클러스터

```bash
# 앙상블 코드 ConfigMap
kubectl create configmap ensemble-app \
  --from-file=ensemble_app.py=k8s/rayserve/ensemble_app.py \
  -n failure-prediction --dry-run=client -o yaml | kubectl apply -f -

# XGBoost 모델 ConfigMap
kubectl create configmap xgboost-model \
  --from-file=xgboost_model.json=models/checkpoints/xgboost_model.json \
  -n failure-prediction --dry-run=client -o yaml | kubectl apply -f -

# 서버 설정 ConfigMap
kubectl create configmap servers-config \
  --from-file=servers.yaml=configs/servers.yaml \
  -n failure-prediction --dry-run=client -o yaml | kubectl apply -f -

# Ray 클러스터 배포
kubectl apply -f k8s/rayserve/raycluster.yaml

# 배포 확인 (3-5분 소요)
kubectl -n failure-prediction get pods -l ray.io/cluster=failure-pred -w
```

### 8단계: CronJobs

```bash
kubectl apply -f k8s/cronjobs/self-pred-push.yaml
kubectl apply -f k8s/cronjobs/ce-simulator.yaml
kubectl apply -f k8s/cronjobs/retrain-xgboost.yaml
kubectl apply -f k8s/cronjobs/registry-gc.yaml
kubectl apply -f k8s/cronjobs/esxi-edac.yaml         # suspended
kubectl apply -f k8s/cronjobs/esxi-collector.yaml     # suspended
kubectl apply -f k8s/cronjobs/esxi-response.yaml      # suspended
```

### 9단계: PUE 부하 제어

```bash
# PUE GPU Load Controller
kubectl apply -f k8s/pue-load/deployment.yaml

# NPU Load Generator
kubectl apply -f k8s/pue-load/npu-load-generator.yaml

# Inference Watchdog
kubectl apply -f k8s/pue-load/inference-watchdog.yaml

# PUE Web UI
kubectl apply -f k8s/pue-load/web/deployment.yaml
```

### 10단계: 검증

```bash
# 전체 Pod 상태
kubectl -n failure-prediction get pods

# 추론 확인
curl -s http://10.100.230.130:31494/predict/node/all | python3 -m json.tool

# Grafana 접속
# http://10.100.230.130:31618

# PUE Web UI 접속
# http://10.100.230.130:31600

# PUE 상태 확인
./scripts/pue/_status.sh
```

---

## K8s 매니페스트 구조

```
k8s/
├── rayserve/
│   ├── raycluster.yaml          # Ray Serve 클러스터 (head + workers)
│   └── ensemble_app.py          # 앙상블 추론 코드
├── cronjobs/
│   ├── self-pred-push.yaml      # 1분 예측 push
│   ├── ce-simulator.yaml        # CE 시뮬레이터
│   ├── retrain-xgboost.yaml     # XGBoost 일일 재학습
│   ├── registry-gc.yaml         # Registry GC
│   ├── esxi-edac.yaml           # ESXi EDAC (suspended)
│   ├── esxi-collector.yaml      # ESXi collector (suspended)
│   └── esxi-response.yaml       # ESXi response (suspended)
├── pue-load/
│   ├── deployment.yaml          # GPU Load Controller
│   ├── pue_gpu_load.py          # PI 피드백 제어 코드
│   ├── npu-load-generator.yaml  # NPU 부하 생성기
│   ├── inference-watchdog.yaml  # 추론 watchdog
│   └── web/
│       ├── app.py               # Web UI 코드
│       └── deployment.yaml      # Web UI 배포
├── monitoring/
│   ├── alerts/
│   │   ├── failure-pred-rules.yaml   # PrometheusRule
│   │   ├── alertmanager-slack.yaml   # Slack 라우팅
│   │   └── vmalert.yaml              # vmalert
│   ├── dcgm-exporter-servicemonitor.yaml
│   ├── furiosa-metrics-servicemonitor.yaml
│   └── grafana-vm-datasource.yaml
├── grafana/dashboards/               # 6개 대시보드 JSON
├── infra/
│   ├── npu-embed.yaml                # NPU embedding 서비스
│   └── pvc-npu-artifacts.yaml        # NPU PVC
├── jobs/
│   └── build-npu-artifact.yaml       # NPU 아티팩트 빌드
└── docker/
    └── Dockerfile.npu                # NPU 이미지
```
