# 15. 시스템 최신 상태

> 새 세션에서 이어서 작업할 때 이 문서를 먼저 읽으세요.

**2026-04-29 운영 환경 변경:** Claude Code 실행 서버를 `node3 (10.100.230.71)` → **`node1 (10.100.230.6)`**로 이관함. 이제 Claude Code는 K8s 마스터에서 직접 동작. node3/node2/node1-old(10.100.230.70~72)는 더 이상 작업 대상 아님 — 운영은 전부 K8s 클러스터(10.100.230.6) 위에서 이루어짐.

**2026-05-02 실측 EDAC 수집 시작:** ESXi 4대(vmgnode18/23/26/30)의 `/var/log/vmkernel.log` MCE/메모리 에러를 SSH로 직접 수집하여 `memory_errors_*{source="real"}` 라벨로 push하는 `esxi-edac` CronJob 가동. 시뮬레이터는 `source="sim"` 라벨로 분리. ensemble은 `sum by (server)` 쿼리로 둘을 합산. 현재 4대 ESXi 모두 실 데이터 0(정상 호스트)이라 시뮬 패턴이 그대로 노출되지만, 실제 메모리 장애 발생 시 즉시 감지 가능.

---

## 1. 환경

```
Claude Code 실행 서버: node3/18AFD199 (10.100.230.71), root/qwe123
작업 경로: /opt/failure_prediction
K8s config: ~/.kube/config → https://10.100.230.6:6443

K8s 클러스터: 6노드 (node1 master + node2-6 worker)
  node1 (10.100.230.6): Master, Tesla T4 x4 GPU, nvidia.com/gpu: 4
  node2-6 (10.100.230.41-45): CPU Worker
```

---

## 2. K8s 리소스 현황 (failure-prediction namespace)

### 인프라 Pod (안정, 11일 가동)

| Pod | 상태 | 노드 |
|---|---|---|
| postgresql-0 | Running (11일) | worker |
| minio-0 | Running (11일) | node1 |
| victoria-metrics-0 | Running (5일) | node1 |
| mlflow | Running, 단일 pod (2026-05-02 8개 stale Failed pod 정리) | node1 |
| grafana | Running (11일) | node5 |
| registry | Running (10일) | node1 |

### Ray Serve (2026-04-21 안정화됨)

| Pod | 상태 | 비고 |
|---|---|---|
| failure-pred-head | Running, Ready 1/1, 재시작 0 | curl 기반 probe로 교체 |
| failure-pred-worker #1 | Running, Ready 1/1, 재시작 0 | 동일 |
| failure-pred-worker #2 | Running, Ready 1/1, 재시작 0 | 동일 |

**원인:** KubeRay 기본 probe가 `wget`을 사용하는데 커스텀 이미지에 wget 미설치 → 매 probe 실패 → Pod 반복 재시작.
**수정:** `k8s/rayserve/raycluster.yaml`에서 head/worker 컨테이너에 `readinessProbe`/`livenessProbe`를 명시하여 `curl -fs` 기반으로 교체. `periodSeconds`/`failureThreshold`도 완화.
**Serve 앱:** 4 predictor 전부 HEALTHY (Chronos, MOIRAI, XGBoost, AnomalyTransformer).

### CronJob (6개, 2026-05-02 기준)

| CronJob | 주기 | 역할 |
|---|---|---|
| esxi-collector | 1분 | ESXi 4대 cpu/mem/vm/vmkernel-error 수집 → VM |
| esxi-response | 1분 | 추론 결과 → VM push |
| **esxi-edac** (신규) | 1분 | **ESXi 4대 vmkernel.log MCE/메모리 에러 → `memory_errors_*{source="real"}`** |
| ce-simulator | 1분 | 시뮬 CE/UE → `memory_errors_*{source="sim"}` |
| retrain-xgboost | 매일 2시 | XGBoost 자동 재학습 + 모델 ConfigMap 갱신 |
| serve-deployer | 1분 | Serve 앱 상태 확인 + 자동 복구 |
| **registry-gc** (신규) | 매일 3시 | registry blob garbage collection (untagged 삭제, disk 누적 방지) |

### 서비스 (NodePort)

| 서비스 | URL | 용도 |
|---|---|---|
| Grafana | http://10.100.230.6:31618 | 대시보드 |
| VictoriaMetrics | http://10.100.230.6:30171/vmui | 메트릭 조회 |
| Ray Serve API | http://10.100.230.6:31494 | 추론 API (2026-04-21 안정화) |
| 구 Grafana (legacy) | http://10.100.230.72:3000 | datasource를 K8s VM으로 변경함 |

---

## 3. 모델 상태 (2026-04-21 CE 시뮬레이터 투입 후)

| 모델 | 스코어 예시 | 상태 | 이유 |
|---|---|---|---|
| **Chronos** | 0.06~0.10 | **GPU 실추론** | CE 시계열 소비, 저강도 예측 반영 |
| **AnomalyT** | 0.95~1.0 | **CPU 실추론** | reconstruction error 높음, threshold 재보정 필요 |
| MOIRAI | 0.5 | dummy | uni2ts 의존성 문제 (미해결) |
| XGBoost | 0.5 | dummy | 피처 dict가 비어있음 (CE 기반 피처 빌드 필요) |
| **앙상블** | **0.51~0.52** | **NORMAL** | CE 데이터 유입으로 WARNING → NORMAL 전환 |

**다음 개선:**
- AnomalyT threshold 재보정 (현재 거의 항상 1.0)
- XGBoost에 CE 통계 피처(mean/max/std/count) 전달
- MOIRAI 의존성 해결 or 가중치 0 전환

---

## 4. 이슈 상태

### 4.1 Ray Pod 반복 재시작 — **해결 (2026-04-21)**

**원인:** KubeRay 기본 probe가 `wget`을 쓰는데 커스텀 이미지에 wget 없음 → 매 probe 실패 → Pod 재시작 폭주 (head 99회, worker 1859회).
**수정:** `k8s/rayserve/raycluster.yaml`에서 `readinessProbe`/`livenessProbe`를 `curl -fs` 기반으로 명시, `failureThreshold: 20`로 완화.
**상태:** 재배포 후 전 Pod Ready 1/1, 재시작 없이 안정.

### 4.2 CE 데이터 없음 — **해결 (2026-04-21)**

**수정:**
- CE 시뮬레이터 CronJob 배포: `k8s/cronjobs/ce-simulator.yaml` — 1분마다 4개 서버의 `memory_errors_correctable` / `memory_errors_uncorrectable` push (vmgnode26는 시간에 따른 degradation ramp).
- `ensemble_app.py`의 `_get_ce_values` 버그 수정: `memory_errors` → `memory_errors_correctable`, `/api/v1/query` → `/api/v1/query_range`.
**상태:** Chronos/AnomalyT가 실제 CE 시계열 소비, 앙상블 WARNING → NORMAL 전환.

### 4.3 MOIRAI 의존성 — **해결 (2026-05-02)**

**원인 정정:** uni2ts 1.x ↔ 2.x 의존성 트리 동일. 진짜 원인은 GPU 이미지 Dockerfile에 `pip install --no-deps uni2ts` 처리 → jax/jaxtyping/hydra 미설치 → import 시 fail.
**수정:**
- `Dockerfile.gpu`에서 `pip install uni2ts==1.2.0` (deps 포함)으로 변경. PyTorch 2.1 + Ray 2.9 + protobuf 3.20과 충돌 없음 확인.
- 이미지 재빌드 + push (`localhost:5000/failure-pred:gpu-latest`).
- `ensemble_app.py` MOIRAIPredictor 코드는 그대로.
**결과:** 4서버 차별화 — vmgnode30(0.054) < vmgnode18(0.104) < vmgnode23(0.538) < vmgnode26(1.000).

### 4.4 XGBoost 피처 미연결 + 모델 placeholder — **해결 (2026-04-30)**

**진단:**
- `ensemble_app.py`가 빈 dict를 XGBoost에 전달 → 항상 dummy 0.5 fallback.
- 원본 `xgboost_model.json`은 placeholder (51 트리, 3 피처만 사용, root split `ce_count_24h<5`로 시뮬레이터 스케일과 격차) → 어떤 입력이든 0.5429 고정 출력.

**수정:**
- `ensemble_app.py`: CE 시계열 → 18 피처(`ce_count_*`, `ce_slope_24h`, `ce_burst_*`, `ce_max`, `ce_std`, `ce_acceleration` + 미수집 9개는 NaN) 계산 후 XGBoost에 전달, 컬럼 순서 booster.feature_names에 정렬.
- `scripts/retrain_xgboost.py`: K8s VictoriaMetrics에서 4서버 × 5일 × 30분 슬라이스(960 샘플) → `ce_count_24h` 50퍼센타일 초과 자가라벨링 → XGBoost 학습. 운영 분포 정합.
- 모델 배포: ConfigMap `xgboost-model` + RayCluster `subPath` mount (`k8s/rayserve/raycluster.yaml`).
- 일일 자동 재학습: `k8s/cronjobs/retrain-xgboost.yaml`이 학습 → ConfigMap update → pod 재시작 (ServiceAccount `xgb-retrainer`).

**결과:** 4서버 XGBoost prob 차별화 시작 (vmgnode18/30=0.4753, vmgnode23/26=0.5247). 한계는 시뮬레이터 4-discrete 분포 — 실측 EDAC 데이터 들어오면 fine-grained 분기 가능.

### 4.5 AnomalyT threshold 재보정 — **해결 (2026-05-02)**

**진단:** rec_error 스케일이 4서버 간 ~1000배 차이 (vmg18=0.97, vmg23=24, vmg26=2525, vmg30=1.29). 단순 linear threshold로는 동시 차별화 불가능 — 어떤 값으로 잡아도 1~2개 서버는 포화 또는 0에 수렴.

**수정:**
- `ensemble_app.py` AT score 계산식: `min(1, log1p(rec_error)/log1p(threshold))` — log scale로 압축.
- 환경변수 `AT_THRESHOLD`로 운영 시 오버라이드 가능 (`k8s/rayserve/raycluster.yaml`, head 컨테이너에 `2500` 주입).

**결과 (4서버 차별화):**
| 서버 | 이전 AT | 새 AT | 앙상블 이전 → 현재 |
|---|---|---|---|
| vmgnode18 | 0.94 (포화) | 0.067 | 0.50 → **0.27 (RECOVERY)** |
| vmgnode30 | 1.00 (포화) | 0.110 | 0.51 → **0.29 (RECOVERY)** |
| vmgnode23 | 1.00 (포화) | 0.422 | 0.52 → **0.38 (NORMAL)** |
| vmgnode26 | 1.00 (포화) | 0.998 | 0.53 → **0.53 (NORMAL)** |

vmgnode18/30이 처음으로 RECOVERY 구간 진입. 시뮬레이터 위험 랭킹과 일치.

### 4.6 구 node1/node2 서비스 미중지

**현상:** 10.100.230.70/72의 기존 서비스 가동 중 (구 Grafana는 K8s VM으로 datasource 포인트 변경 완료).
**조건:** K8s 독립 운영 1~2주 확인 후 중지.

---

## 5. Docker 이미지

```
레지스트리: 10.100.230.6:5000
이미지:
  failure-pred:cpu-latest — Python 3.11 + Ray 2.9.0 + XGBoost + pyVmomi + 수집기
  failure-pred:gpu-latest — Python 3.11 + CUDA 12.1 + PyTorch + Chronos + Ray 2.9.0

GPU 이미지 특이사항:
  - protobuf 3.20 고정 (Ray 2.9.0 호환)
  - lightning/pytorch-lightning 포함
  - MOIRAI (uni2ts) import 실패 → dummy 모드
  - AnomalyTransformer는 CPU 모드로 실행 (GPU 크래시 방지)
```

---

## 6. 주요 파일

```
/opt/failure_prediction/
├── k8s/rayserve/ensemble_app.py  — Ray Serve 앙상블 앱 (ConfigMap으로 K8s 배포)
├── src/                           — 소스 코드 전체
├── vendor/Anomaly-Transformer/    — AT 수정본
├── models/checkpoints/            — 학습된 모델 파일
├── docs/12_kubernetes_migration.md — K8s 이관 전체 명세
├── docs/13_k8s_migration_status.md — 이관 진행 현황 + 해결된 문제 8개
├── docs/14_k8s_remaining_issues.md — 남은 이슈 상세
├── docs/15_current_status.md       — ★ 이 문서 (최신 상태)
└── docs_kubernetes/                — K8s 이관 지시 문서 (원본)

~/.kube/config                      — K8s 접근 설정
```

---

## 7. 다음 세션에서 줄 지시문

```
/opt/failure_prediction 프로젝트에서 작업 중입니다.
docs/15_current_status.md를 읽고 현재 상태를 파악해주세요.

핵심 이슈:
1. Ray Pod 반복 재시작 (head 99회, worker 1859회) — readiness probe 또는 구조 변경 필요
2. CE 데이터 없음 → 모델 스코어 비정상 (1.0/0.5) → CE 시뮬레이션 CronJob 추가
3. MOIRAI 의존성 문제 → 별도 이미지 또는 3모델 앙상블로 변경

이 이슈들을 순서대로 해결해주세요.
```

---

## 8. 유용한 명령어

```bash
# K8s 전체 상태
kubectl get pods -n failure-prediction
kubectl get cronjob -n failure-prediction

# Ray head Pod 찾기
HEAD_POD=$(kubectl get pods -n failure-prediction -l ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}')

# Serve 수동 배포 (head Pod 재시작 후 필요)
kubectl exec $HEAD_POD -n failure-prediction -- bash -c '
cd /app/rayserve && python -c "
import ray, sys
sys.path.insert(0, \"/app\"); sys.path.insert(0, \"/app/rayserve\")
ray.init(address=\"auto\")
import importlib
spec = importlib.util.spec_from_file_location(\"ensemble_app\", \"/app/rayserve/ensemble_app.py\")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
from ray import serve
serve.run(mod.ensemble, name=\"ensemble\", route_prefix=\"/\", host=\"0.0.0.0\")
print(\"OK\")
"'

# API 테스트
kubectl exec $HEAD_POD -n failure-prediction -- curl -s http://localhost:8000/predict/all

# GPU 이미지 리빌드 (node1에서)
ssh root@10.100.230.6 'cd /home/failure_prediction_build && nerdctl build -t localhost:5000/failure-pred:gpu-latest -f Dockerfile.gpu .'
ssh root@10.100.230.6 'nerdctl push --insecure-registry localhost:5000/failure-pred:gpu-latest'

# ConfigMap 업데이트
kubectl delete configmap ensemble-app -n failure-prediction
kubectl create configmap ensemble-app --from-file=ensemble_app.py=/opt/failure_prediction/k8s/rayserve/ensemble_app.py -n failure-prediction
```
