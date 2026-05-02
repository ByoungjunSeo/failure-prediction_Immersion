# 13. K8s 이관 진행 현황 및 재시작 가이드

> 작성일: 2026-04-09
> 작업 서버: node3/18AFD199 (10.100.230.71)
> 작업 경로: /opt/failure_prediction

---

## 1. 전체 진행 현황

### Phase별 상태

| Phase | 상태 | 내용 |
|---|---|---|
| **K-P0** | **완료** | 네임스페이스, StorageClass, PV 4개, 레지스트리, Secret 4개, ConfigMap 2개 |
| **K-P1** | **완료** | PostgreSQL, MinIO, VictoriaMetrics, MLflow, Grafana — 6 Pod Running |
| **K-P2** | **완료** | VictoriaMetrics 메트릭 104개 + PostgreSQL 테이블 3개 + Grafana 데이터소스 2개 이전 |
| **K-P3** | **완료** | cpu-latest + gpu-latest 이미지 빌드 및 레지스트리 push, 6노드 insecure registry 설정 |
| **K-P4** | **완료** | RayCluster 배포, 4개 모델 Serve 앱 배포, /health /predict API 응답 확인 |
| **K-P5** | **완료** | esxi-collector(1분), esxi-response(1분), retrain-xgboost(매일2시) CronJob 3개 배포 |
| **K-P6** | **진행 중** | Grafana 대시보드 import 완료, GPU 드라이버 설치 중 (재부팅 필요) |

---

## 2. 현재 K8s 리소스 상태

### 2.1 Pod (failure-prediction namespace)

| Pod | 상태 | 역할 |
|---|---|---|
| postgresql-0 | Running | PostgreSQL 15 (failure_pred DB) |
| minio-0 | Running | MinIO 오브젝트 스토리지 |
| victoria-metrics-0 | Running | VictoriaMetrics 시계열 DB |
| mlflow-* | Running | MLflow 실험 추적 (sqlite 모드) |
| grafana-* | Running | Grafana 대시보드 |
| registry | Running | 로컬 Docker 레지스트리 (5000) |
| failure-pred-head-* | Running | Ray 클러스터 Head (Serve 앱) |
| failure-pred-cpu-workers-* (x2) | Running | Ray 클러스터 Worker |
| esxi-collector-* | 1분마다 | ESXi 4대 메트릭 수집 CronJob |
| esxi-response-* | 1분마다 | ESXi 자동 대응 CronJob |

### 2.2 서비스 (NodePort)

| 서비스 | NodePort | 접속 URL |
|---|---|---|
| Grafana | 31618 | http://10.100.230.6:31618 |
| VictoriaMetrics | 30171 | http://10.100.230.6:30171/vmui |
| Ray Serve API | 31494 | http://10.100.230.6:31494 (port-forward 권장) |

### 2.3 레지스트리

```
주소: 10.100.230.6:5000
이미지:
  - failure-pred:cpu-latest  (Python 3.11 + XGBoost + pyVmomi + 수집기)
  - failure-pred:gpu-latest  (CUDA 12.1 + PyTorch + Chronos + MOIRAI + XGBoost)
```

### 2.4 Secret / ConfigMap

```
Secret: esxi-credentials, pg-secret, minio-secret, slack-secret
ConfigMap: servers-config (ESXi 호스트 + 서비스 URL), ensemble-app (Serve 앱 코드)
```

---

## 3. 해결된 문제들

### 3.1 PostgreSQL Bus Error

**문제:** node1에 hugepages 32GB가 할당되어 PostgreSQL initdb가 Bus error로 크래시.

**해결:** PostgreSQL을 worker 노드에 emptyDir로 배포. hugepages가 없는 노드에서 정상 동작.

```yaml
# node1 대신 worker 노드 사용 + emptyDir
volumes:
- name: pg-data
  emptyDir:
    sizeLimit: 50Gi
- name: dshm
  emptyDir:
    medium: Memory
    sizeLimit: 256Mi
```

### 3.2 MLflow psycopg2 없음

**문제:** MLflow 공식 이미지에 PostgreSQL 드라이버(psycopg2)가 없어서 PG 연결 실패.

**해결:** SQLite 백엔드로 변경. PVC에 /mlflow/mlflow.db 저장.

```yaml
args:
- --backend-store-uri=sqlite:///mlflow/mlflow.db
- --default-artifact-root=/mlflow/artifacts
```

### 3.3 이미지 Pull 실패 (ImagePullBackOff)

**문제:** worker 노드에서 로컬 레지스트리(10.100.230.6:5000) 이미지를 pull 못함.

**해결:** 6개 노드 전부에 containerd insecure registry 설정 + 재시작.

```
# 각 노드의 /etc/containerd/config.toml에 추가:
[plugins."io.containerd.grpc.v1.cri".registry.mirrors."10.100.230.6:5000"]
  endpoint = ["http://10.100.230.6:5000"]
```

### 3.4 Ray Serve @serve.deployment 파라미터

**문제:** Ray 2.9.0에서 `num_cpus`가 `@serve.deployment` 직접 인자로 지원 안 됨.

**해결:** `ray_actor_options`로 변경.

```python
# 변경 전 (에러)
@serve.deployment(num_replicas=1, num_cpus=2)

# 변경 후 (정상)
@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 2})
```

### 3.5 RayService Pod 반복 재시작

**문제:** RayService가 Serve 앱 배포 실패 시 head Pod을 계속 재시작 (CrashLoopBackOff).

**해결:** RayService 대신 **RayCluster + 수동 Serve 배포** 방식으로 변경. 안정적으로 동작.

```bash
# RayCluster 배포 후 수동 Serve 앱 배포
kubectl exec $HEAD_POD -n failure-prediction -- python -c "
import ray; ray.init(address='auto')
from ray import serve
# ensemble_app 모듈 로드 후
serve.run(mod.ensemble, name='ensemble', route_prefix='/', host='0.0.0.0')
"
```

### 3.6 Ray Serve 외부 접근 불가 (Connection Refused)

**문제:** head Pod 내부에서 curl은 되지만 다른 Pod에서 접근 시 Connection Refused.

**원인:** Ray Serve가 기본적으로 `127.0.0.1:8000`에만 바인딩.

**해결:** `RAY_SERVE_HTTP_HOST=0.0.0.0` 환경변수 + `serve.run(..., host="0.0.0.0")`.

```yaml
# RayCluster YAML
env:
- name: RAY_SERVE_HTTP_HOST
  value: "0.0.0.0"
```

### 3.7 GPU 모델 torch import 실패

**문제:** CPU 이미지에 PyTorch가 없어서 Chronos/MOIRAI/AnomalyTransformer가 import 실패.

**해결:** `try/except`로 감싸서 PyTorch 없으면 dummy 모드(score=0.5)로 동작. GPU 이미지 배포 후 실제 모델 사용 가능.

```python
HAS_TORCH = False
try:
    import torch
    HAS_TORCH = True
except ImportError:
    logger.warning("PyTorch 없음 — GPU 모델 dummy 모드")
```

### 3.8 control-plane taint

**문제:** node1(master)에 control-plane taint가 있어서 일반 Pod이 스케줄링 안 됨.

**해결:** 인프라 Pod에 toleration 추가.

```yaml
tolerations:
- key: node-role.kubernetes.io/control-plane
  operator: Exists
  effect: NoSchedule
```

---

## 4. 미해결 이슈 (재부팅 후 진행 필요)

### 4.1 GPU 드라이버 — **설치 완료, 디스크 문제로 GPU 이미지 미사용**

**완료된 것:**
- NVIDIA 드라이버 595.58.03 설치, nvidia-smi T4 x 4 인식
- NVIDIA Container Toolkit 설치, containerd nvidia runtime 설정
- K8s Device Plugin → `nvidia.com/gpu: 4` 등록

**미해결:**
- node1의 `/` 파티션이 70GB (74% 사용)로 GPU 이미지 pull 시 disk-pressure 발생
- `/home`은 1.7TB 여유 — containerd root를 `/home`으로 이전하면 해결
- 현재는 CPU 이미지로 head Pod 운영 중 (GPU 모델 dummy 모드)

**해결 완료:** containerd root를 `/home/containerd`(1.7TB)로 이전 완료. 디스크 문제 해결.

**해결 완료:** GPU 이미지를 Python 3.11 + protobuf 3.20으로 리빌드. Chronos + AnomalyT가 Tesla T4에서 실제 추론 동작 확인.

**이전 문제 (해결됨):** GPU 이미지에서 Ray 2.9.0 + protobuf 호환성 에러.
- `MessageToDict() got unexpected keyword argument 'including_default_value_fields'`
- 원인: Ray 2.9.0의 serve 모듈이 최신 protobuf API와 비호환
- 해결 방법: GPU Dockerfile에서 `protobuf==3.20.3` 고정 + Ray serve 코드 패치, 또는 Ray 전체를 2.10+로 업그레이드 (CPU/GPU 이미지 모두 리빌드 필요)

```dockerfile
# GPU Dockerfile에 추가
RUN pip install 'protobuf==3.20.3'
# 또는 Ray serve 코드에서 including_default_value_fields → always_print_fields_with_no_presence 로 패치
```

**재부팅 후 확인:**
```bash
ssh root@10.100.230.6 'nvidia-smi'
# 기대: Tesla T4 x 4 표시
```

**재부팅 후 작업:**
1. nvidia-smi 확인
2. K8s GPU 리소스 등록 확인: `kubectl describe node node1 | grep nvidia.com/gpu`
3. RayCluster를 GPU 이미지로 재배포
4. Serve 앱에서 실제 모델 로드 (dummy → 실제)

### 4.2 /predict/all 라우트 순서

**문제:** FastAPI에서 `/predict/all`이 `/predict/{server_id}`에 의해 `server_id="all"`로 매칭됨.

**해결 방법:** ensemble_app.py에서 `/predict/all` 라우트를 `/predict/{server_id}` 위에 선언.

```python
# 현재 (문제)
@app.get("/predict/{server_id}")
@app.get("/predict/all")  # server_id="all"로 매칭됨

# 수정 (라우트 순서 변경)
@app.get("/predict/all")  # 먼저 선언
@app.get("/predict/{server_id}")
```

### 4.3 Ray Serve 자동 배포

**현재:** RayCluster 배포 후 수동으로 `serve.run()` 실행 필요.

**해결 방안:**
- RayCluster head의 `postStart` lifecycle hook에 Serve 배포 스크립트 추가
- 또는 별도 Job으로 Serve 앱 배포

### 4.4 CronJob 완료 Pod 정리

**현재:** 1분마다 CronJob이 실행되어 Completed Pod이 계속 쌓임.

**해결:** CronJob에 `successfulJobsHistoryLimit: 3`, `failedJobsHistoryLimit: 1` 설정.

### 4.5 기존 서비스 중지

**현재:** 기존 node2(10.100.230.72), node1(10.100.230.70)의 VictoriaMetrics, PostgreSQL, Grafana가 아직 가동 중.

**GPU 활성화 + 전체 검증 후 중지:**
```bash
# node2
ssh root@10.100.230.72 'systemctl stop victoria-metrics grafana-server postgresql telegraf'

# node1 (기존 저장 서버)
ssh root@10.100.230.70 'systemctl stop victoria-metrics minio postgresql'
```

---

## 5. 재부팅 후 Claude Code 재시작 가이드

### 5.1 작업 경로

```
서버: node3/18AFD199 (10.100.230.71)
경로: /opt/failure_prediction
```

이 경로에서 Claude Code를 실행하면 됩니다.

### 5.2 node1 재부팅 절차

```bash
# 1. node1 재부팅
ssh root@10.100.230.6 'reboot'

# 2. 5분 대기 후 접속 확인
ssh root@10.100.230.6 'hostname'

# 3. K8s 클러스터 확인
kubectl get nodes

# 4. GPU 확인
ssh root@10.100.230.6 'nvidia-smi'

# 5. Pod 복구 확인
kubectl get pods -n failure-prediction
```

### 5.3 재부팅 후 해야 할 작업

```
1. GPU 확인: nvidia-smi → T4 x 4 표시되는지
2. K8s GPU 리소스: kubectl describe node node1 | grep nvidia.com/gpu
3. RayCluster GPU 이미지로 재배포 (gpu-latest)
4. Serve 앱 재배포 (수동)
5. /predict/all 라우트 순서 수정
6. CronJob historyLimit 설정
7. 전체 검증 후 기존 서비스 중지
```

### 5.4 Claude Code에 줄 지시문

```
node1(10.100.230.6) 재부팅 완료 후, K8s 이관 남은 작업을 진행해줘:

1. nvidia-smi 확인 → T4 GPU 4장 인식 확인
2. K8s에서 GPU 리소스(nvidia.com/gpu) 등록 확인
3. RayCluster를 GPU 이미지(10.100.230.6:5000/failure-pred:gpu-latest)로 재배포
4. ensemble_app.py의 /predict/all 라우트 순서 수정
5. Serve 앱 재배포 (실제 모델 로드)
6. CronJob historyLimit 설정
7. 전체 검증 (API, Grafana, CronJob)
8. 검증 완료 후 기존 서비스(node2, 기존 node1) 중지

작업 경로: /opt/failure_prediction
K8s config: ~/.kube/config (10.100.230.6:6443)
```

---

## 6. K8s 클러스터 구성도

```
K8s Cluster (6 nodes)
├── node1 (10.100.230.6) — Master + GPU (T4 x 4)
│   ├── kube-apiserver, etcd, controller-manager
│   ├── registry Pod (5000)
│   ├── victoria-metrics-0 (8428 → NodePort 30171)
│   ├── minio-0 (9000/9001)
│   ├── mlflow (5000)
│   └── [재부팅 후] GPU 사용 가능
│
├── node2 (10.100.230.41) — Worker
├── node3 (10.100.230.42) — Worker
│   ├── failure-pred-head (Ray Head + Serve API 8000)
│   └── grafana (3000 → NodePort 31618)
├── node4 (10.100.230.43) — Worker
│   └── failure-pred-cpu-worker
├── node5 (10.100.230.44) — Worker
│   └── postgresql-0 (5432)
└── node6 (10.100.230.45) — Worker
    └── failure-pred-cpu-worker

External:
├── ESXi vmgnode18 (10.148.148.118) — 모니터링 대상
├── ESXi vmgnode23 (10.148.148.123)
├── ESXi vmgnode26 (10.148.148.126)
└── ESXi vmgnode30 (10.148.148.130)

기존 서버 (이관 후 중지 예정):
├── 기존 node3/18AFD199 (10.100.230.71) — Claude Code 실행 서버
├── 기존 node2/18AFD226 (10.100.230.72) — 기존 모니터링
└── 기존 node1/18AFD201 (10.100.230.70) — 기존 저장
```

---

## 7. 주요 파일 경로

```
/opt/failure_prediction/
├── k8s/rayserve/ensemble_app.py   — Ray Serve 앙상블 앱 (ConfigMap으로 배포)
├── src/                            — 소스 코드 전체 (Docker 이미지에 포함)
├── vendor/Anomaly-Transformer/     — AT 수정본 (Docker 이미지에 포함)
├── models/checkpoints/             — 학습된 모델 (Docker 이미지에 포함)
├── configs/                        — 서버 설정 (ConfigMap으로 배포)
├── scripts/                        — 스크립트
├── docs/                           — 문서
└── docs_kubernetes/                — K8s 이관 지시 문서

~/.kube/config                      — K8s 접근 설정 (10.100.230.6:6443)
```
