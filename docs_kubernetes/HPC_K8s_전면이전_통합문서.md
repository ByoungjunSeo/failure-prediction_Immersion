# HPC 서버 장애 예측 시스템 — Kubernetes 전면 이전
# Claude Code 개발 지시

> 기존 서버 3대의 모든 서비스를 Kubernetes 클러스터로 완전 이전
> Ray Serve 기반 Pod 형태로 장애 예측 서비스 운영
> 기존 서버(node3/18AFD199 등)는 이전 완료 후 역할 종료

---

## 🖥️ Kubernetes 클러스터 구성

| 역할 | Hostname | IP | BMC | 주요 HW |
|---|---|---|---|---|
| **Master** | node1 | 10.100.230.6 | 10.100.230.106 | Xeon Platinum 8558 (192코어), Tesla T4 16GB×4, NVMe 1.92TB×2 |
| Slave | node2 | 10.100.230.41 | 10.100.231.41 | Xeon Gold 6140 (36코어), RAM 64GB |
| Slave | node3 | 10.100.230.42 | 10.100.231.42 | Xeon Gold 6140 (36코어), RAM 64GB |
| Slave | node4 | 10.100.230.43 | 10.100.231.43 | Xeon Gold 6140 (36코어), RAM 64GB |
| Slave | node5 | 10.100.230.44 | 10.100.231.44 | Xeon Gold 6140 (36코어), RAM 64GB |
| Slave | node6 | 10.100.230.45 | 10.100.231.45 | Xeon Gold 6140 (36코어), RAM 64GB |

> GPU: node1에만 Tesla T4 16GB × 4장
> 공통 계정: root / qwe123, Rocky Linux 9.7

---

## 📡 Claude Code 실행 위치

```
Claude Code는 기존 서버(node3/18AFD199, 10.100.230.71)에서 실행
kubectl로 k8s Master(10.100.230.6)를 원격 제어
이전 완료 후 Claude Code도 k8s Pod(Jupyter/shell)로 이동 가능
```

---

## 🏗️ 전체 아키텍처 — 모든 서비스 k8s 이전

```
Namespace: failure-prediction
│
├── [인프라 레이어]
│   ├── VictoriaMetrics StatefulSet  (메트릭 저장, 포트 8428)
│   ├── PostgreSQL StatefulSet       (레이블 DB, 포트 5432)
│   ├── MinIO StatefulSet            (모델 아티팩트, 포트 9000)
│   ├── MLflow Deployment            (실험 추적, 포트 5000)
│   └── Grafana Deployment           (대시보드, 포트 3000)
│
├── [수집 레이어]
│   ├── ESXi Collector CronJob       (1분마다 ESXi 메트릭 수집)
│   └── EDAC/IPMI Collector CronJob  (기존 서버 모니터링 계속)
│
├── [AI 레이어 — Ray Serve on KubeRay]
│   ├── RayCluster Head Pod (node1, GPU)
│   │   ├── ChronosPredictor     (T4 GPU, num_gpus=0.5)
│   │   ├── MOIRAIPredictor      (T4 GPU, num_gpus=0.5)
│   │   ├── AnomalyTransformer   (T4 GPU, num_gpus=0.5)
│   │   └── AnomalyEnsemble API  (FastAPI ingress)
│   └── RayCluster Worker Pods (node2~6, CPU)
│       └── XGBoostPredictor     (num_replicas=2)
│
├── [대응 레이어]
│   ├── ESXi Response CronJob    (1분마다 /predict/all → ESXi 대응)
│   └── Retrain CronJob          (새벽 2시 XGBoost 재학습)
│
└── [스토리지]
    └── PersistentVolume (node1 NVMe 1.92TB × 2 활용)
```

---

## 🧠 모델 구성

```
Chronos (Amazon)       → T4 GPU  : CE 시계열 Zero-shot 예측
MOIRAI (Salesforce)    → T4 GPU  : Zero-shot 이상탐지
XGBoost (파인튜닝)     → CPU     : Alibaba PAKDD 사전학습 + 자체 데이터
Anomaly Transformer    → T4 GPU  : 비지도 이상탐지
앙상블: (Chronos+MOIRAI)×0.4 + XGBoost×0.35 + AnomalyT×0.25
```

---

## 🚦 리스크 대응

| 확률 | 레벨 | 대응 |
|---|---|---|
| 0.65~0.85 | WARNING | ESXi VM 배치 차단 + Slack |
| 0.85+ | CRITICAL | Maintenance Mode + 긴급 알림 |
| 0.30- | RECOVERY | Maintenance Mode 해제 |

---

## 📏 코드 규칙

- Ray Serve: `@serve.deployment` + `@serve.ingress(app)` 구조
- Secret: k8s Secret으로만 관리, 코드 하드코딩 금지
- ConfigMap: 서버 목록/설정 관리
- logging 모듈 사용, print() 금지
- 평가지표: Accuracy 금지 → F1, AUC-PR, Recall
- GPU 배치: `nodeSelector: kubernetes.io/hostname: node1`
- Namespace: `failure-prediction`

---

## 🔄 현재 개발 단계

```
현재 Phase : K-P0 — k8s 환경 셋업
완료 Phase  : 없음
업데이트    : 2025-04-08
```

---

## 📁 상세 문서

| 문서 | 경로 | 내용 |
|---|---|---|
| 환경 구성 | `docs/k8s_01_environment.md` | kubectl, namespace, KubeRay, StorageClass |
| 인프라 서비스 | `docs/k8s_02_infra.md` | VictoriaMetrics/PG/MinIO/MLflow/Grafana k8s 배포 |
| 컨테이너 이미지 | `docs/k8s_03_images.md` | Docker 이미지 빌드 전략 |
| Ray Serve | `docs/k8s_04_rayserve.md` | RayCluster/RayService YAML + 앱 코드 |
| 데이터 수집 | `docs/k8s_05_data_collection.md` | ESXi 수집 CronJob |
| ESXi 연동 | `docs/k8s_06_esxi.md` | ESXi 대응 CronJob |
| 이전 절차 | `docs/k8s_07_migration.md` | 기존 데이터 이전 방법 |
| Phase 지시 | `docs/k8s_08_phases.md` | Phase별 Claude Code 입력 지시문 |
| 제약/검증 | `docs/k8s_09_constraints.md` | 금지 사항, 검증 체크리스트 |
-e 

---

# K8s-01. 환경 구성

## kubectl 원격 접근 설정 (기존 node3/18AFD199에서)

```bash
mkdir -p ~/.kube
scp root@10.100.230.6:/etc/kubernetes/admin.conf ~/.kube/config
chmod 600 ~/.kube/config

# 클러스터 확인
kubectl get nodes -o wide
kubectl describe node node1 | grep -E "gpu|nvidia|tesla|Capacity" -i
```

---

## Namespace 생성

```bash
kubectl create namespace failure-prediction
kubectl create namespace ray-system
kubectl create namespace monitoring    # Grafana/Prometheus

# 기본 namespace 설정
kubectl config set-context --current --namespace=failure-prediction
```

---

## StorageClass — node1 NVMe 로컬 스토리지

node1에 NVMe 1.92TB × 2장이 있습니다. 로컬 PV로 활용합니다.

```bash
# node1에서 마운트 포인트 준비
ssh root@10.100.230.6 'mkdir -p /data/victoria-metrics /data/postgresql /data/minio /data/mlflow'
```

```yaml
# k8s/storage/storageclass-local.yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-nvme
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Retain
---
# VictoriaMetrics PV
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-victoria-metrics
spec:
  capacity:
    storage: 500Gi
  accessModes: [ReadWriteOnce]
  storageClassName: local-nvme
  persistentVolumeReclaimPolicy: Retain
  local:
    path: /data/victoria-metrics
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [node1]
---
# PostgreSQL PV
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-postgresql
spec:
  capacity:
    storage: 100Gi
  accessModes: [ReadWriteOnce]
  storageClassName: local-nvme
  persistentVolumeReclaimPolicy: Retain
  local:
    path: /data/postgresql
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [node1]
---
# MinIO PV
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-minio
spec:
  capacity:
    storage: 500Gi
  accessModes: [ReadWriteOnce]
  storageClassName: local-nvme
  persistentVolumeReclaimPolicy: Retain
  local:
    path: /data/minio
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [node1]
---
# MLflow PV
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-mlflow
spec:
  capacity:
    storage: 50Gi
  accessModes: [ReadWriteOnce]
  storageClassName: local-nvme
  persistentVolumeReclaimPolicy: Retain
  local:
    path: /data/mlflow
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [node1]
```

```bash
kubectl apply -f k8s/storage/storageclass-local.yaml
kubectl get pv
```

---

## KubeRay Operator 설치

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update
helm install kuberay-operator kuberay/kuberay-operator \
  --namespace ray-system --create-namespace --version 1.1.0
kubectl get pods -n ray-system
```

---

## GPU Operator 확인 (Kubespray 설치 시 완료)

```bash
kubectl get pods -n gpu-operator
kubectl get nodes -o json | jq '.items[] | select(.metadata.name=="node1") | .status.capacity'
# "nvidia.com/gpu": "4" 확인
```

---

## 로컬 컨테이너 레지스트리 (node1)

```bash
ssh root@10.100.230.6 \
  "docker run -d -p 5000:5000 --restart=always --name registry registry:2"

# 모든 노드에서 insecure-registry 허용 확인
# /etc/docker/daemon.json 에 "insecure-registries": ["10.100.230.6:5000"] 필요
for ip in 10.100.230.6 10.100.230.41 10.100.230.42 10.100.230.43 10.100.230.44 10.100.230.45; do
  ssh root@$ip 'cat /etc/docker/daemon.json'
done
```

---

## Secret 생성

```bash
# ESXi 접속
kubectl create secret generic esxi-credentials \
  --from-literal=password=VMware!0 \
  --from-literal=bmc-password=admin \
  -n failure-prediction

# Slack
kubectl create secret generic slack-secret \
  --from-literal=webhook-url=https://hooks.slack.com/... \
  -n failure-prediction

# MinIO (내부 서비스)
kubectl create secret generic minio-secret \
  --from-literal=access-key=minioadmin \
  --from-literal=secret-key=minioadmin \
  -n failure-prediction

# PostgreSQL
kubectl create secret generic pg-secret \
  --from-literal=password=pgpassword \
  -n failure-prediction
```

---

## ConfigMap — 서버/ESXi 목록

```yaml
# k8s/configmaps/servers-config.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: servers-config
  namespace: failure-prediction
data:
  servers.yaml: |
    # ESXi 장애 예측 대상 (4대)
    esxi_hosts:
      - { id: vmgnode18, ip: 10.148.148.118, bmc: 172.31.201.118 }
      - { id: vmgnode23, ip: 10.148.148.123, bmc: 172.31.201.123 }
      - { id: vmgnode26, ip: 10.148.148.126, bmc: 172.31.201.126 }
      - { id: vmgnode30, ip: 10.148.148.130, bmc: 172.31.201.130 }
    # vmgnode17: 접속 장애로 제외

    # k8s 내부 서비스 URL
    services:
      victoria_metrics: http://victoria-metrics-svc:8428
      postgresql:       postgresql://failure-prediction:5432/failure_pred
      minio:            http://minio-svc:9000
      mlflow:           http://mlflow-svc:5000
      grafana:          http://grafana-svc:3000
```

```bash
kubectl apply -f k8s/configmaps/servers-config.yaml
```

---

## 프로젝트 디렉토리 구조

```
/opt/k8s_migration/
├── CLAUDE.md
├── docs/
├── src/
│   ├── ray_serve/          # Ray Serve 앱 (신규)
│   ├── collectors/         # 기존 코드 복사
│   ├── features/           # 기존 코드 복사
│   ├── training/           # 기존 코드 복사
│   └── esxi/               # 기존 코드 복사
├── k8s/
│   ├── storage/            # PV/PVC/StorageClass
│   ├── infra/              # VictoriaMetrics, PG, MinIO, MLflow, Grafana
│   ├── configmaps/
│   ├── raycluster.yaml
│   ├── rayservice.yaml
│   └── cronjobs/
├── docker/
│   ├── Dockerfile.ray-gpu
│   └── Dockerfile.ray-cpu
└── requirements-ray-gpu.txt
```
-e 

---

# K8s-02. 인프라 서비스 k8s 배포

## 전체 인프라를 k8s로 이전합니다.

```
기존 서버          →  k8s Namespace: failure-prediction
─────────────────────────────────────────────────────
node2(18AFD226)
  VictoriaMetrics  →  StatefulSet (node1 PV)
  Telegraf         →  DaemonSet
  Grafana          →  Deployment
  Alertmanager     →  Deployment
  PostgreSQL       →  StatefulSet (node1 PV)

node1(18AFD201)
  VictoriaMetrics  →  (단일 인스턴스로 통합, 보존 기간 24개월)
  MinIO            →  StatefulSet (node1 PV)
  PostgreSQL       →  (위 PG에 통합)

node3(18AFD199)
  MLflow           →  Deployment
  FastAPI          →  Ray Serve로 대체
```

---

## 1. VictoriaMetrics StatefulSet

```yaml
# k8s/infra/victoria-metrics.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-victoria-metrics
  namespace: failure-prediction
spec:
  storageClassName: local-nvme
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 500Gi
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: victoria-metrics
  namespace: failure-prediction
spec:
  serviceName: victoria-metrics-svc
  replicas: 1
  selector:
    matchLabels:
      app: victoria-metrics
  template:
    metadata:
      labels:
        app: victoria-metrics
    spec:
      nodeSelector:
        kubernetes.io/hostname: node1    # NVMe 스토리지 노드
      containers:
        - name: victoria-metrics
          image: victoriametrics/victoria-metrics:latest
          args:
            - -storageDataPath=/storage
            - -retentionPeriod=24        # 24개월
            - -httpListenAddr=:8428
          ports:
            - containerPort: 8428
          volumeMounts:
            - name: storage
              mountPath: /storage
      volumes:
        - name: storage
          persistentVolumeClaim:
            claimName: pvc-victoria-metrics
---
apiVersion: v1
kind: Service
metadata:
  name: victoria-metrics-svc
  namespace: failure-prediction
spec:
  selector:
    app: victoria-metrics
  ports:
    - port: 8428
      targetPort: 8428
  type: ClusterIP
```

---

## 2. PostgreSQL StatefulSet

```yaml
# k8s/infra/postgresql.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-postgresql
  namespace: failure-prediction
spec:
  storageClassName: local-nvme
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 100Gi
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgresql
  namespace: failure-prediction
spec:
  serviceName: postgresql-svc
  replicas: 1
  selector:
    matchLabels:
      app: postgresql
  template:
    metadata:
      labels:
        app: postgresql
    spec:
      nodeSelector:
        kubernetes.io/hostname: node1
      containers:
        - name: postgresql
          image: postgres:15
          env:
            - name: POSTGRES_DB
              value: failure_pred
            - name: POSTGRES_USER
              value: hpcdev
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: pg-secret
                  key: password
          ports:
            - containerPort: 5432
          volumeMounts:
            - name: storage
              mountPath: /var/lib/postgresql/data
      volumes:
        - name: storage
          persistentVolumeClaim:
            claimName: pvc-postgresql
---
apiVersion: v1
kind: Service
metadata:
  name: postgresql-svc
  namespace: failure-prediction
spec:
  selector:
    app: postgresql
  ports:
    - port: 5432
      targetPort: 5432
```

---

## 3. MinIO StatefulSet

```yaml
# k8s/infra/minio.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: pvc-minio
  namespace: failure-prediction
spec:
  storageClassName: local-nvme
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 500Gi
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: minio
  namespace: failure-prediction
spec:
  serviceName: minio-svc
  replicas: 1
  selector:
    matchLabels:
      app: minio
  template:
    metadata:
      labels:
        app: minio
    spec:
      nodeSelector:
        kubernetes.io/hostname: node1
      containers:
        - name: minio
          image: minio/minio:latest
          command: ["minio", "server", "/data", "--console-address", ":9001"]
          env:
            - name: MINIO_ROOT_USER
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: access-key
            - name: MINIO_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: secret-key
          ports:
            - containerPort: 9000   # API
            - containerPort: 9001   # Console
          volumeMounts:
            - name: storage
              mountPath: /data
      volumes:
        - name: storage
          persistentVolumeClaim:
            claimName: pvc-minio
---
apiVersion: v1
kind: Service
metadata:
  name: minio-svc
  namespace: failure-prediction
spec:
  selector:
    app: minio
  ports:
    - name: api
      port: 9000
      targetPort: 9000
    - name: console
      port: 9001
      targetPort: 9001
```

---

## 4. MLflow Deployment

```yaml
# k8s/infra/mlflow.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mlflow
  namespace: failure-prediction
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mlflow
  template:
    metadata:
      labels:
        app: mlflow
    spec:
      containers:
        - name: mlflow
          image: ghcr.io/mlflow/mlflow:v2.9.0
          command:
            - mlflow
            - server
            - --backend-store-uri
            - postgresql+psycopg2://hpcdev:$(POSTGRES_PASSWORD)@postgresql-svc:5432/failure_pred
            - --default-artifact-root
            - s3://mlflow-artifacts/
            - --host
            - "0.0.0.0"
            - --port
            - "5000"
          env:
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: pg-secret
                  key: password
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
          ports:
            - containerPort: 5000
---
apiVersion: v1
kind: Service
metadata:
  name: mlflow-svc
  namespace: failure-prediction
spec:
  selector:
    app: mlflow
  ports:
    - port: 5000
      targetPort: 5000
```

---

## 5. Grafana Deployment

```yaml
# k8s/infra/grafana.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: grafana
  namespace: failure-prediction
spec:
  replicas: 1
  selector:
    matchLabels:
      app: grafana
  template:
    metadata:
      labels:
        app: grafana
    spec:
      containers:
        - name: grafana
          image: grafana/grafana:latest
          env:
            - name: GF_SECURITY_ADMIN_PASSWORD
              value: admin
          ports:
            - containerPort: 3000
          volumeMounts:
            - name: grafana-storage
              mountPath: /var/lib/grafana
      volumes:
        - name: grafana-storage
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: grafana-svc
  namespace: failure-prediction
spec:
  selector:
    app: grafana
  ports:
    - port: 3000
      targetPort: 3000
  type: NodePort    # 외부 접근용
  # NodePort를 통해 http://10.100.230.6:3xxxx 로 접근 가능
```

---

## 6. Telegraf DaemonSet (k8s 노드 메트릭 수집)

```yaml
# k8s/infra/telegraf-daemonset.yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: telegraf
  namespace: failure-prediction
spec:
  selector:
    matchLabels:
      app: telegraf
  template:
    metadata:
      labels:
        app: telegraf
    spec:
      hostNetwork: true
      hostPID: true
      containers:
        - name: telegraf
          image: telegraf:1.29
          securityContext:
            privileged: true
          volumeMounts:
            - name: telegraf-config
              mountPath: /etc/telegraf/telegraf.conf
              subPath: telegraf.conf
            - name: host-proc
              mountPath: /rootfs/proc
              readOnly: true
            - name: host-sys
              mountPath: /rootfs/sys
              readOnly: true
      volumes:
        - name: telegraf-config
          configMap:
            name: telegraf-config
        - name: host-proc
          hostPath:
            path: /proc
        - name: host-sys
          hostPath:
            path: /sys
```

---

## 전체 인프라 배포 순서

```bash
# 1. Storage
kubectl apply -f k8s/storage/

# 2. 인프라 서비스 (순서 중요)
kubectl apply -f k8s/infra/postgresql.yaml
kubectl apply -f k8s/infra/minio.yaml
kubectl apply -f k8s/infra/victoria-metrics.yaml

# MinIO 버킷 생성 (Pod 뜬 후)
kubectl exec -n failure-prediction \
  $(kubectl get pod -l app=minio -n failure-prediction -o jsonpath='{.items[0].metadata.name}') \
  -- mc alias set local http://localhost:9000 minioadmin minioadmin
kubectl exec ... -- mc mb local/mlflow-artifacts local/training-datasets

# 3. MLflow (MinIO, PG 준비 후)
kubectl apply -f k8s/infra/mlflow.yaml

# 4. Grafana
kubectl apply -f k8s/infra/grafana.yaml

# 5. Telegraf
kubectl apply -f k8s/infra/telegraf-daemonset.yaml

# 전체 확인
kubectl get all -n failure-prediction
```
-e 

---

# K8s-03. 컨테이너 이미지 빌드

## 이미지 구성

```
10.100.230.6:5000/failure-pred:gpu-latest  ← Head Pod (node1, T4 GPU)
  base: rayproject/ray:2.9.0-gpu
  포함: Chronos, MOIRAI, Anomaly Transformer, PyTorch+CUDA

10.100.230.6:5000/failure-pred:cpu-latest  ← Worker Pod (node2~6)
  base: rayproject/ray:2.9.0
  포함: XGBoost, pyVmomi, paramiko, 수집기, 피처 계산
```

---

## Dockerfile.ray-gpu

```dockerfile
FROM rayproject/ray:2.9.0-gpu

USER root
WORKDIR /app

RUN apt-get update && apt-get install -y \
    ipmitool openssh-client curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements-ray-gpu.txt .
RUN pip install --no-cache-dir -r requirements-ray-gpu.txt

COPY src/ ./src/
COPY k8s/configmaps/ ./configs/

USER ray
```

## requirements-ray-gpu.txt

```
ray[serve]==2.9.0
torch==2.1.0+cu121 --extra-index-url https://download.pytorch.org/whl/cu121
chronos-forecasting==1.3.0
uni2ts==1.1.0
git+https://github.com/thuml/Anomaly-Transformer
fastapi==0.108.0
uvicorn==0.27.1
mlflow==2.9.0
boto3==1.34.0
pandas==2.1.0
numpy==1.26.0
scipy==1.11.0
shap==0.44.0
prometheus-client==0.19.0
pyyaml==6.0.1
psycopg2-binary==2.9.9
```

---

## Dockerfile.ray-cpu

```dockerfile
FROM rayproject/ray:2.9.0

USER root
WORKDIR /app

RUN apt-get update && apt-get install -y \
    ipmitool openssh-client curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements-ray-cpu.txt .
RUN pip install --no-cache-dir -r requirements-ray-cpu.txt

COPY src/ ./src/
COPY k8s/configmaps/ ./configs/

USER ray
```

## requirements-ray-cpu.txt

```
ray[serve]==2.9.0
xgboost==2.0.0
scikit-learn==1.3.0
imbalanced-learn==0.11.0
pyVmomi==8.0.1
paramiko==3.4.0
mlflow==2.9.0
boto3==1.34.0
pandas==2.1.0
numpy==1.26.0
scipy==1.11.0
optuna==3.5.0
apscheduler==3.10.4
prometheus-client==0.19.0
pyyaml==6.0.1
psycopg2-binary==2.9.9
python-dotenv==1.0.0
```

---

## 이미지 빌드 및 푸시

```bash
cd /opt/k8s_migration

# 기존 소스 복사
cp -r /opt/failure_prediction/src/collectors src/
cp -r /opt/failure_prediction/src/features   src/
cp -r /opt/failure_prediction/src/training   src/
cp -r /opt/failure_prediction/src/esxi       src/

# CPU 이미지
docker build -f docker/Dockerfile.ray-cpu \
  -t 10.100.230.6:5000/failure-pred:cpu-latest .
docker push 10.100.230.6:5000/failure-pred:cpu-latest

# GPU 이미지 (시간 오래 걸림 — CUDA 패키지)
docker build -f docker/Dockerfile.ray-gpu \
  -t 10.100.230.6:5000/failure-pred:gpu-latest .
docker push 10.100.230.6:5000/failure-pred:gpu-latest

# 확인
curl http://10.100.230.6:5000/v2/failure-pred/tags/list
```
-e 

---

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
-e 

---

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
-e 

---

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
-e 

---

# K8s-07. 기존 데이터 이전 절차

## 이전 대상

| 기존 서버 | 데이터 | k8s 이전 위치 |
|---|---|---|
| node2/18AFD226 (10.100.230.72) | VictoriaMetrics 메트릭 (6개월) | k8s VictoriaMetrics PV |
| node1/18AFD201 (10.100.230.70) | VictoriaMetrics 메트릭 (장기) | k8s VictoriaMetrics PV (통합) |
| node1/18AFD201 (10.100.230.70) | MinIO 모델 아티팩트 | k8s MinIO PV |
| node2/18AFD226 (10.100.230.72) | PostgreSQL DB | k8s PostgreSQL PV |
| node3/18AFD199 (10.100.230.71) | MLflow 실험 기록 | k8s MLflow (MinIO 연동) |

---

## 1. VictoriaMetrics 데이터 이전

VictoriaMetrics는 스냅샷 기능을 제공합니다.

```bash
# 기존 node2에서 스냅샷 생성
curl http://10.100.230.72:8428/snapshot/create
# 응답: {"status":"ok","snapshot":"20250408T120000-xxxxxxxx"}

# 스냅샷 파일 위치
ssh root@10.100.230.72 \
  'ls /var/lib/victoriametrics/snapshots/'

# k8s node1으로 복사
rsync -avz root@10.100.230.72:/var/lib/victoriametrics/snapshots/ \
  root@10.100.230.6:/data/victoria-metrics/snapshots/

# 기존 node1(장기)도 동일하게
curl http://10.100.230.70:8429/snapshot/create
rsync -avz root@10.100.230.70:/var/lib/victoriametrics/snapshots/ \
  root@10.100.230.6:/data/victoria-metrics/snapshots/

# k8s VictoriaMetrics Pod에서 스냅샷 복원
kubectl exec -n failure-prediction \
  $(kubectl get pod -l app=victoria-metrics -n failure-prediction -o jsonpath='{.items[0].metadata.name}') \
  -- vmrestore -src=/storage/snapshots/<snapshot_name> -storageDataPath=/storage
```

---

## 2. MinIO 데이터 이전 (모델 아티팩트)

```bash
# 기존 node1(18AFD201)에서 k8s MinIO로 복사
# mc 클라이언트 설치 후
mc alias set old-minio http://10.100.230.70:9000 minioadmin minioadmin

# k8s MinIO 포트포워딩
kubectl port-forward svc/minio-svc 9000:9000 -n failure-prediction &

mc alias set new-minio http://localhost:9000 minioadmin minioadmin

# 버킷 생성
mc mb new-minio/mlflow-artifacts
mc mb new-minio/training-datasets

# 데이터 복사
mc mirror old-minio/mlflow-artifacts new-minio/mlflow-artifacts
mc mirror old-minio/training-datasets new-minio/training-datasets

# 확인
mc ls new-minio/mlflow-artifacts
```

---

## 3. PostgreSQL 데이터 이전

```bash
# 기존 node2(18AFD226)에서 덤프
ssh root@10.100.230.72 \
  'pg_dump -U hpcdev failure_pred > /tmp/failure_pred.sql'

# 로컬로 복사
scp root@10.100.230.72:/tmp/failure_pred.sql /tmp/

# k8s PostgreSQL에 복원
kubectl port-forward svc/postgresql-svc 5432:5432 -n failure-prediction &
psql -h localhost -U hpcdev -d failure_pred < /tmp/failure_pred.sql

# 확인
psql -h localhost -U hpcdev -d failure_pred -c '\dt'
```

---

## 4. 이전 완료 후 기존 서버 서비스 중단 순서

```bash
# 순서 중요: 검증 완료 후 중단

# 1단계: 기존 FastAPI 중단
ssh root@10.100.230.71 'pkill -f "uvicorn src.api.main"'

# 2단계: 기존 APScheduler 중단 (FastAPI와 함께 중단됨)

# 3단계: 기존 VictoriaMetrics 중단 (데이터 이전 후)
ssh root@10.100.230.72 'systemctl stop victoria-metrics'
ssh root@10.100.230.70 'systemctl stop victoria-metrics'

# 4단계: 기존 PostgreSQL 중단
ssh root@10.100.230.72 'systemctl stop postgresql'

# 5단계: 기존 MinIO 중단
ssh root@10.100.230.70 'systemctl stop minio'

# 6단계: 기존 MLflow 중단
ssh root@10.100.230.71 'systemctl stop mlflow'

# 7단계: 기존 Grafana 중단
ssh root@10.100.230.72 'systemctl stop grafana'
```

---

## 5. Grafana 대시보드 이전

```bash
# 기존 Grafana에서 대시보드 JSON 내보내기
curl -s http://admin:admin@10.100.230.72:3000/api/dashboards/home \
  | jq '.dashboard' > /tmp/dashboard_export.json

# k8s Grafana에 불러오기 (포트포워딩 후)
kubectl port-forward svc/grafana-svc 3001:3000 -n failure-prediction &
curl -X POST http://admin:admin@localhost:3001/api/dashboards/import \
  -H 'Content-Type: application/json' \
  -d @/tmp/dashboard_export.json
```

---

## 이전 검증 체크리스트

```
데이터 이전:
  [ ] VictoriaMetrics 스냅샷 복원 후 메트릭 조회 정상
  [ ] MinIO 버킷/아티팩트 복사 완료
  [ ] PostgreSQL failure_events, training_labels 테이블 확인
  [ ] MLflow 실험 기록 접근 가능

서비스 전환:
  [ ] Ray Serve /predict/all 정상 응답
  [ ] ESXi CronJob → Slack 알림 수신
  [ ] Grafana 대시보드 k8s 클러스터에서 표시
  [ ] 기존 서버 서비스 모두 중단 완료
```
-e 

---

# K8s-08. Phase별 Claude Code 지시문

> Claude Code는 기존 node3(18AFD199/10.100.230.71)에서 실행
> kubectl로 k8s 클러스터(node1, 10.100.230.6) 원격 제어

---

## K-P0 — k8s 환경 셋업 (Day 1)

```
다음을 순서대로 실행해줘:

1. kubectl 원격 설정:
   mkdir -p ~/.kube
   scp root@10.100.230.6:/etc/kubernetes/admin.conf ~/.kube/config
   chmod 600 ~/.kube/config
   kubectl get nodes -o wide

2. 클러스터 상태 확인:
   kubectl get pods --all-namespaces
   kubectl describe node node1 | grep -i "gpu\|nvidia\|tesla\|capacity" -A2

3. GPU 확인 (node1에 T4×4 인식 여부):
   kubectl get nodes -o json | \
     jq '.items[] | select(.metadata.name=="node1") | .status.capacity'
   # "nvidia.com/gpu": "4" 필수

4. KubeRay Operator 설치:
   helm repo add kuberay https://ray-project.github.io/kuberay-helm/
   helm repo update
   helm install kuberay-operator kuberay/kuberay-operator \
     --namespace ray-system --create-namespace --version 1.1.0
   kubectl get pods -n ray-system -w

5. Namespace 생성:
   kubectl create namespace failure-prediction

6. node1에 로컬 레지스트리 설치:
   ssh root@10.100.230.6 \
     "docker run -d -p 5000:5000 --restart=always --name registry registry:2"
   curl http://10.100.230.6:5000/v2/_catalog

7. node1 스토리지 디렉토리 준비:
   ssh root@10.100.230.6 \
     'mkdir -p /data/victoria-metrics /data/postgresql /data/minio /data/mlflow'

8. 프로젝트 디렉토리 생성 및 기존 코드 복사:
   mkdir -p /opt/k8s_migration/{src/ray_serve,k8s/{storage,infra,configmaps,cronjobs},docker,docs}
   cp -r /opt/failure_prediction/src/collectors /opt/k8s_migration/src/
   cp -r /opt/failure_prediction/src/features   /opt/k8s_migration/src/
   cp -r /opt/failure_prediction/src/training   /opt/k8s_migration/src/
   cp -r /opt/failure_prediction/src/esxi       /opt/k8s_migration/src/

완료 기준:
- kubectl get nodes에서 node1~6 모두 Ready
- node1 nvidia.com/gpu: 4 확인
- kuberay-operator Pod Running
- 로컬 레지스트리 http://10.100.230.6:5000 응답
```

---

## K-P1 — 스토리지 & 인프라 배포 (Day 2~3)

```
docs/k8s_01_environment.md, k8s_02_infra.md를 참고해서:

1. StorageClass 및 PV/PVC 생성:
   k8s/storage/storageclass-local.yaml 작성 후 apply
   kubectl get pv

2. Secret 생성 (실제 값으로):
   kubectl create secret generic esxi-credentials \
     --from-literal=password=VMware!0 -n failure-prediction
   kubectl create secret generic slack-secret \
     --from-literal=webhook-url=<실제URL> -n failure-prediction
   kubectl create secret generic minio-secret \
     --from-literal=access-key=minioadmin \
     --from-literal=secret-key=minioadmin -n failure-prediction
   kubectl create secret generic pg-secret \
     --from-literal=password=pgpassword -n failure-prediction

3. ConfigMap 생성:
   k8s/configmaps/servers-config.yaml 작성 후 apply

4. 인프라 서비스 배포 (순서대로):
   kubectl apply -f k8s/infra/postgresql.yaml
   kubectl apply -f k8s/infra/minio.yaml
   kubectl apply -f k8s/infra/victoria-metrics.yaml
   kubectl apply -f k8s/infra/mlflow.yaml
   kubectl apply -f k8s/infra/grafana.yaml
   kubectl get all -n failure-prediction

5. MinIO 버킷 생성:
   kubectl exec -n failure-prediction \
     $(kubectl get pod -l app=minio -n failure-prediction -o jsonpath='{.items[0].metadata.name}') \
     -- sh -c 'mc alias set local http://localhost:9000 minioadmin minioadmin && \
               mc mb local/mlflow-artifacts && mc mb local/training-datasets'

완료 기준:
- postgresql, minio, victoria-metrics, mlflow, grafana Pod 모두 Running
- MinIO 버킷 2개 생성 확인
- MLflow UI 접근: kubectl port-forward svc/mlflow-svc 5000:5000 -n failure-prediction
```

---

## K-P2 — 기존 데이터 이전 (Day 3~4)

```
docs/k8s_07_migration.md를 참고해서:

1. VictoriaMetrics 스냅샷 이전:
   curl http://10.100.230.72:8428/snapshot/create
   curl http://10.100.230.70:8429/snapshot/create
   rsync 명령으로 k8s node1(/data/victoria-metrics/)으로 복사

2. MinIO 데이터 이전:
   kubectl port-forward svc/minio-svc 9000:9000 -n failure-prediction &
   mc mirror old-minio/mlflow-artifacts new-minio/mlflow-artifacts
   mc mirror old-minio/training-datasets new-minio/training-datasets

3. PostgreSQL 데이터 이전:
   pg_dump -h 10.100.230.72 -U hpcdev failure_pred > /tmp/failure_pred.sql
   kubectl port-forward svc/postgresql-svc 5432:5432 -n failure-prediction &
   psql -h localhost -U hpcdev -d failure_pred < /tmp/failure_pred.sql

4. 이전 검증:
   kubectl port-forward svc/victoria-metrics-svc 8428:8428 -n failure-prediction &
   curl 'http://localhost:8428/api/v1/query?query=up' | jq '.data.result | length'
   psql -h localhost -U hpcdev -d failure_pred -c 'SELECT COUNT(*) FROM training_labels;'

완료 기준:
- VictoriaMetrics에 기존 메트릭 데이터 조회 가능
- PostgreSQL training_labels 건수 이전 전과 동일
- MLflow에서 기존 실험 기록 확인 가능
```

---

## K-P3 — Docker 이미지 빌드 (Day 4~5)

```
docs/k8s_03_images.md를 참고해서:

1. Dockerfile 파일 작성:
   docker/Dockerfile.ray-gpu 생성
   docker/Dockerfile.ray-cpu 생성
   requirements-ray-gpu.txt 생성
   requirements-ray-cpu.txt 생성

2. src/ray_serve/ensemble_app.py 작성:
   docs/k8s_04_rayserve.md 참고

3. CPU 이미지 빌드 먼저 (GPU보다 빠름):
   cd /opt/k8s_migration
   docker build -f docker/Dockerfile.ray-cpu \
     -t 10.100.230.6:5000/failure-pred:cpu-latest .
   docker push 10.100.230.6:5000/failure-pred:cpu-latest

4. GPU 이미지 빌드:
   docker build -f docker/Dockerfile.ray-gpu \
     -t 10.100.230.6:5000/failure-pred:gpu-latest .
   docker push 10.100.230.6:5000/failure-pred:gpu-latest

5. 모든 k8s 노드에서 이미지 pull 확인:
   for ip in 10.100.230.41 10.100.230.42 10.100.230.43; do
     ssh root@$ip "docker pull 10.100.230.6:5000/failure-pred:cpu-latest"
   done

완료 기준:
- 레지스트리에 cpu, gpu 태그 모두 확인
- k8s 노드에서 pull 성공
```

---

## K-P4 — Ray Serve 배포 (Day 5~6)

```
docs/k8s_04_rayserve.md를 참고해서:

1. RayService YAML 작성:
   k8s/rayservice.yaml 작성

2. RayService 배포:
   kubectl apply -f k8s/rayservice.yaml
   kubectl get rayservice -n failure-prediction -w
   kubectl get pods -n failure-prediction -w

3. Ray 대시보드 접근:
   kubectl port-forward svc/failure-pred-cluster-head-svc \
     8265:8265 -n failure-prediction &
   # http://localhost:8265 에서 Deployment 상태 확인

4. API 테스트:
   kubectl port-forward svc/anomaly-service-serve-svc \
     8000:8000 -n failure-prediction &
   curl http://localhost:8000/health
   curl http://localhost:8000/predict/vmgnode18
   curl http://localhost:8000/predict/all

완료 기준:
- RayService Running/Healthy 상태
- /predict/all 응답에 vmgnode18~30 결과 4개 포함
- model_scores 4개 모두 포함 (chronos, moirai, xgboost, anomaly_t)
- 응답 시간 < 500ms
```

---

## K-P5 — CronJob 배포 (Day 6~7)

```
docs/k8s_05_data_collection.md, k8s_06_esxi.md를 참고해서:

1. 수집기 Job 스크립트 작성:
   src/collectors/esxi_collector_job.py 작성
   src/esxi/response_job.py 작성

2. CronJob YAML 작성 및 배포:
   kubectl apply -f k8s/cronjobs/esxi-collector.yaml
   kubectl apply -f k8s/cronjobs/esxi-response.yaml
   kubectl apply -f k8s/cronjobs/retrain-cronjob.yaml
   kubectl apply -f k8s/cronjobs/weekly-tune.yaml

3. 수동 테스트 (1분 기다리지 않고 즉시 실행):
   kubectl create job esxi-collect-test \
     --from=cronjob/esxi-collector -n failure-prediction
   kubectl logs job/esxi-collect-test -n failure-prediction -f

   kubectl create job esxi-response-test \
     --from=cronjob/esxi-response -n failure-prediction
   kubectl logs job/esxi-response-test -n failure-prediction -f

완료 기준:
- esxi-collector: VictoriaMetrics에 메트릭 전송 확인
- esxi-response: Slack 알림 수신 확인
- audit_log DB에 대응 이력 기록 확인
```

---

## K-P6 — 기존 서비스 중단 및 전환 완료 (Day 7~8)

```
docs/k8s_07_migration.md를 참고해서:

1. 최종 검증 (기존 서비스 중단 전):
   - k8s Ray Serve API 정상 응답 확인
   - k8s CronJob 1분 주기 정상 동작 확인
   - k8s Grafana 대시보드 접근 확인
   - k8s PostgreSQL 데이터 정상 확인

2. 기존 서비스 중단 (순서대로):
   ssh root@10.100.230.71 'pkill -f "uvicorn src.api.main" || true'
   ssh root@10.100.230.72 'systemctl stop victoria-metrics grafana postgresql || true'
   ssh root@10.100.230.70 'systemctl stop victoria-metrics minio || true'
   ssh root@10.100.230.71 'systemctl stop mlflow || true'

3. Grafana 대시보드 이전:
   기존 Grafana에서 JSON 내보내기 → k8s Grafana에 import

4. Telegraf DaemonSet 배포:
   k8s/infra/telegraf-daemonset.yaml 작성 후 apply
   kubectl get pods -l app=telegraf -n failure-prediction

5. CLAUDE.md 업데이트:
   현재 Phase: K-P6 완료
   기존 서버: 이전 완료, 서비스 중단

완료 기준:
- 기존 서버 3대 서비스 모두 중단
- k8s Ray Serve 단독 운영 확인
- Slack CronJob 알림 정상
- Grafana 대시보드 k8s에서 표시
- kubectl get all -n failure-prediction 전체 Running
```
-e 

---

# K8s-09. 제약 사항 및 검증 체크리스트

## 절대 금지 사항

| 구분 | 금지 내용 |
|---|---|
| 🚫 ESXi SSH | 패키지 설치, 설정 변경, 파일 수정 |
| 🚫 k8s 직접 수정 | 노드에 직접 접속해 k8s 설정 변경 (kubectl만 사용) |
| 🚫 kube-system | kube-system namespace 직접 수정 |
| 🚫 하드코딩 | Secret 값을 코드/YAML에 직접 기재 |
| 🚫 print() | logging 모듈 사용, print() 금지 |
| 🚫 Accuracy | 평가지표로 Accuracy 사용 금지 |

---

## Ray Serve 코드 규칙

```
✅ @serve.deployment 데코레이터 필수
✅ 모델 초기화는 __init__에서만 (요청마다 재로드 금지)
✅ predict 메서드는 async def
✅ num_gpus / num_cpus 명시
✅ GPU 배치: nodeSelector: node1
✅ 환경변수로 서비스 URL 주입 (코드 내 IP 하드코딩 금지)
```

---

## GPU 할당 (node1 Tesla T4 × 4)

```
T4 GPU 0,1 공유: Chronos(0.5) + MOIRAI(0.5) + AnomalyT(0.5) = 1.5장
T4 GPU 3      : CronJob 재학습 (새벽 2시, CUDA_VISIBLE_DEVICES=3)
T4 GPU 2      : 예비 / 대규모 재학습 시 투입
```

---

## Phase별 완료 기준

| Phase | 기준 |
|---|---|
| K-P0 환경 셋업 | node1~6 Ready / GPU 4장 / KubeRay Running / 레지스트리 동작 |
| K-P1 인프라 배포 | PG/MinIO/VM/MLflow/Grafana 모두 Running / MinIO 버킷 생성 |
| K-P2 데이터 이전 | VictoriaMetrics 메트릭 조회 / PG 건수 동일 / MLflow 기록 확인 |
| K-P3 이미지 빌드 | cpu/gpu 이미지 레지스트리 Push / k8s 노드 Pull 성공 |
| K-P4 Ray Serve | RayService Healthy / /predict/all 4대 응답 / < 500ms |
| K-P5 CronJob | 3개 CronJob 정상 / Slack 수신 / VictoriaMetrics 메트릭 유입 |
| K-P6 전환 완료 | 기존 서버 서비스 중단 / k8s 단독 운영 / Grafana 표시 |

---

## 유용한 kubectl 명령어

```bash
# 전체 상태
kubectl get all -n failure-prediction

# Ray Serve 상태
kubectl get rayservice -n failure-prediction

# Pod 로그
kubectl logs -l app=anomaly-service -n failure-prediction --tail=100

# CronJob 수동 실행
kubectl create job <name>-test --from=cronjob/<name> -n failure-prediction

# 리소스 사용량
kubectl top pods -n failure-prediction
kubectl top nodes

# Ray 대시보드
kubectl port-forward svc/failure-pred-cluster-head-svc 8265:8265 -n failure-prediction

# API 접근
kubectl port-forward svc/anomaly-service-serve-svc 8000:8000 -n failure-prediction

# Grafana 접근
kubectl port-forward svc/grafana-svc 3000:3000 -n failure-prediction

# MLflow 접근
kubectl port-forward svc/mlflow-svc 5000:5000 -n failure-prediction
```

---

## 전체 서비스 내부 URL (k8s 클러스터 내부)

```
VictoriaMetrics : http://victoria-metrics-svc:8428
PostgreSQL      : postgresql-svc:5432
MinIO API       : http://minio-svc:9000
MLflow          : http://mlflow-svc:5000
Grafana         : http://grafana-svc:3000
Ray Serve API   : http://anomaly-service-serve-svc:8000
```
-e 

---

