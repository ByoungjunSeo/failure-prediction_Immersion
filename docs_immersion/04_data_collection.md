# 04. 데이터 수집 및 메트릭

> 자기 클러스터 5노드의 하드웨어/소프트웨어 메트릭을 다중 채널로 수집.

---

## 수집 채널 개요

| 수집원 | 대상 | 주기 | 저장소 | 방법 |
|---|---|---|---|---|
| **Node Exporter** | CPU, 메모리, 디스크, 네트워크 | 15s | Prometheus | DaemonSet (kube-prometheus-stack) |
| **DCGM Exporter** | GPU 온도/전력/사용률/VRAM/Xid | 15s | Prometheus | DaemonSet (gpu-operator) |
| **Furiosa Metrics** | NPU 코어 사용률/전력/온도 | 15s | Prometheus | DaemonSet (furiosa-system) |
| **smartctl Exporter** | 디스크 S.M.A.R.T. 건강 상태 | 60s | Prometheus | DaemonSet |
| **CE Simulator** | 합성 CE 데이터 (학습용) | 1min | VictoriaMetrics | CronJob |
| **Self-Pred Push** | AI 예측 점수 + 노드 메트릭 | 1min | VictoriaMetrics | CronJob |
| **EDAC (ESXi)** | ESXi 메모리 ECC 에러 | 1min | VictoriaMetrics | CronJob (현재 suspended) |
| **ESXi Collector** | ESXi CPU/MEM/VM 메트릭 | 1min | VictoriaMetrics | CronJob (현재 suspended) |

---

## CronJob 상세

### self-pred-push (1분마다)

K8s 5노드의 system 메트릭을 Prometheus에서 가져와 AI 추론 API에 전달하고, 결과를 VictoriaMetrics에 push합니다.

```yaml
# k8s/cronjobs/self-pred-push.yaml
schedule: "* * * * *"
```

**수집 메트릭** (Prometheus → PromQL):
- `node_memory_MemAvailable_bytes` — 가용 메모리
- `node_cpu_seconds_total` — CPU 사용률
- `node_filesystem_avail_bytes` — 디스크 가용 공간
- `DCGM_FI_DEV_GPU_UTIL` — GPU 사용률
- `DCGM_FI_DEV_GPU_TEMP` — GPU 온도
- `furiosa_npu_core_utilization` — NPU 사용률

**AI 예측 호출**: `http://failure-pred-head-svc:8000/predict/node/all`

**결과 Push** (VictoriaMetrics):
```
failure_prediction_score{server="node1", model="ensemble"} 0.127
failure_prediction_score{server="node1", model="chronos"} 0.042
failure_prediction_score{server="node1", model="moirai"} 0.089
...
```

### ce-simulator (1분마다, suspended)

학습 데이터 생성을 위한 합성 CE 시뮬레이터. 정상/이상 패턴의 CE 카운트를 VictoriaMetrics에 push합니다.

```yaml
# k8s/cronjobs/ce-simulator.yaml
schedule: "* * * * *"
suspend: true   # 필요 시 활성화
```

**Push 메트릭**:
```
memory_errors_correctable{server="node1", source="sim"} 5
memory_errors_correctable{server="node2", source="sim"} 12
```

### retrain-xgboost (매일 02:00)

VictoriaMetrics에서 CE 시계열을 슬라이싱하여 자가 라벨링 후 XGBoost를 재학습합니다.

```yaml
# k8s/cronjobs/retrain-xgboost.yaml
schedule: "0 2 * * *"
```

### registry-gc (매일 03:00)

로컬 Container Registry의 미사용 이미지 가비지 컬렉션.

```yaml
# k8s/cronjobs/registry-gc.yaml
schedule: "0 3 * * *"
```

### serve-deployer (1분마다)

Ray Serve 앙상블 앱 배포 상태를 확인하고, 미배포 시 자동 배포합니다.

---

## Prometheus 메트릭 (주요)

### GPU 메트릭 (DCGM Exporter)

| 메트릭 | 설명 | 단위 |
|---|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | GPU 코어 사용률 | % |
| `DCGM_FI_DEV_GPU_TEMP` | GPU 온도 | °C |
| `DCGM_FI_DEV_POWER_USAGE` | GPU 전력 소비 | W |
| `DCGM_FI_DEV_FB_USED` | 사용 중인 VRAM | MiB |
| `DCGM_FI_DEV_FB_FREE` | 가용 VRAM | MiB |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | 메모리 복사 사용률 | % |
| `DCGM_FI_DEV_XID_ERRORS` | GPU Xid 에러 카운트 | count |

### NPU 메트릭 (Furiosa Metrics Exporter)

| 메트릭 | 설명 | 단위 |
|---|---|---|
| `furiosa_npu_core_utilization` | NPU 코어 사용률 | % |
| `furiosa_npu_hw_power` | NPU 전력 소비 | W |
| `furiosa_npu_hw_temperature` | NPU 온도 | °C |

### 시스템 메트릭 (Node Exporter)

| 메트릭 | 설명 |
|---|---|
| `node_cpu_seconds_total` | CPU 시간 |
| `node_memory_MemTotal_bytes` | 총 메모리 |
| `node_memory_MemAvailable_bytes` | 가용 메모리 |
| `node_filesystem_avail_bytes` | 디스크 가용 공간 |
| `node_network_receive_bytes_total` | 네트워크 수신 바이트 |

---

## VictoriaMetrics

AI 예측 점수 전용 시계열 DB. Prometheus와 분리하여 장기 보존합니다.

| 항목 | 값 |
|---|---|
| In-cluster URL | `http://victoria-metrics-svc.failure-prediction:8428` |
| NodePort URL | `http://10.100.230.130:30171` |
| 저장 메트릭 | `failure_prediction_score`, `memory_errors_correctable` |
| 보존 기간 | 기본 1개월 |

### 데이터 조회 예시

```bash
# 최근 1시간 앙상블 점수
curl "http://10.100.230.130:30171/api/v1/query_range?query=failure_prediction_score{model='ensemble'}&start=$(date -d '1 hour ago' +%s)&end=$(date +%s)&step=60"
```

---

## 메트릭 라벨 컨벤션

| 라벨 | 값 | 설명 |
|---|---|---|
| `server` | node1, node2, ... | 노드 식별자 |
| `source` | `sim`, `real` | CE 데이터 소스 (시뮬/실측) |
| `model` | chronos, moirai, xgboost, anomaly_transformer, ensemble | AI 모델 식별 |
| `Hostname` | node1, node2, ... | DCGM 메트릭의 노드명 |
| `instance` | 10.100.230.131:9100 | Node Exporter 인스턴스 |
