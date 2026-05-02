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
