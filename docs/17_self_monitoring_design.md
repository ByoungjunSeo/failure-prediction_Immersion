# 17. Self-monitoring & Failure Prediction (신규 클러스터 자기-감시)

> 작성: 2026-05-14. 기존 ESXi 4대 추론은 병행 유지하고, 신규 K8s 5노드(10.100.230.130~134) 자체 자원사용률 모니터링·장애예측을 추가.

---

## 1. 목표

1. **실시간 가시화** — 5 노드의 CPU/메모리/디스크/네트워크/GPU/NPU/K8s 상태를 Grafana로 즉시 조회 가능
2. **장애 예측 4종 동시 수행:**
   - (a) **자원 고갈** — CPU/Mem/Disk/Net 시계열 forecast → 임박한 포화 알림
   - (b) **GPU/NPU 하드웨어 이상** — 온도·전력·ECC error·PE status 다변량 이상탐지
   - (c) **Pod/노드 안정성** — restart 패턴, OOMKilled 빈도, NotReady 전조
   - (d) **디스크 실패** — Longhorn replica health + SMART 데이터
3. **기존 ESXi 추론과 병행** — 두 시스템 동시 가동, ensemble app은 하나로 통합 (/predict/esxi/* + /predict/node/* 두 경로)
4. **GPU 4대 + NPU 1대 모두 활용** — Ray Serve fractional GPU allocation으로 5 predictor 분산, NPU는 LLM-embedding으로 사용 (기존 그대로)

---

## 2. 자원 현황 vs. 부족분

### 이미 가동 중 (활용)
| 컴포넌트 | NS | 역할 |
|---|---|---|
| `monitoring-prometheus-node-exporter` (DaemonSet) | monitoring | CPU/Mem/Disk/Net (5/5 노드) |
| `nvidia-dcgm-exporter` (DaemonSet) | gpu-operator | GPU util/mem/temp/power/ECC (4 GPU 노드) |
| `monitoring-kube-state-metrics` | monitoring | Pod restart, OOMKilled, Pending, NotReady |
| `monitoring-kube-prometheus-prometheus` | monitoring | 메인 Prometheus (scrape + storage) |
| `monitoring-kube-prometheus-alertmanager` | monitoring | 알람 라우팅 |
| `monitoring-grafana` | monitoring | 대시보드 (기본 K8s 보드 다수 내장) |
| **VictoriaMetrics** | failure-prediction | AI 예측 점수 push 대상 (기존 그대로 유지) |

### Phase 0에서 추가 필요
| 컴포넌트 | 어디 | 역할 |
|---|---|---|
| `furiosa-metrics-exporter` | node5 (DaemonSet 또는 systemd) | NPU util/mem/temp/power/PE status |
| `smartctl_exporter` | 5/5 노드 (DaemonSet, hostPath /dev) | 디스크 SMART (reallocated sectors, pending, end-to-end errors) |
| ServiceMonitor CRD ×2 | monitoring | Prometheus에 위 2종을 scrape 등록 |

---

## 3. 메트릭 백엔드 — 양쪽 다 활용 (사용자 결정)

```
표준 인프라 메트릭 (node-exporter, dcgm, kube-state, furiosa, smart)
  └─ Prometheus (monitoring NS) — 14일 retention, Grafana 메인 소스
        ↓
        AI 예측기가 PromQL로 쿼리해서 feature 추출
        ↓
  AI 예측 점수 (chronos/moirai/at/xgboost/llm_embedding) per 노드
  └─ VictoriaMetrics push (기존 위치 유지)
        ↓
        Grafana "AI 예측" 대시보드의 datasource = VM
        Alertmanager → Slack 라우팅
```

이유: ESXi 추론은 이미 VM에 push 중 → 동일 백엔드로 self-monitoring 점수도 모음. 둘 다 같은 대시보드에서 비교 가능. 표준 시스템 메트릭은 Prom에 그대로 두는 게 chart 호환성·dashboard 즉시 활용 측면에서 합리적.

---

## 4. 예측 모델 ↔ 신호 매핑

기존 5 predictor를 그대로 활용. 입력 시계열만 ESXi CE에서 자원사용률로 교체.

| Predictor | 입력 시계열 (per 노드) | 출력 |
|---|---|---|
| **ChronosPredictor** | CPU usage 5min avg, Memory used%, Disk used%, Network bytes | 6시간 후 자원 고갈 확률 |
| **MOIRAIPredictor** | 동일 다변량 | 같은 forecast (앙상블 다양성) |
| **AnomalyTransformerPredictor** | GPU/NPU 온도·전력·util·ECC | 하드웨어 이상 score |
| **XGBoostPredictor** | Pod restart 24h, OOMKilled 1h, NotReady 빈도, SMART 위험 피처 | 다음 1시간 장애 분류 |
| **LLMEmbeddingAnomalyPredictor** | 노드 메트릭 snapshot 텍스트화 | 베이스라인 임베딩 거리 (NPU) |
| **AnomalyEnsemble** | 위 5개 weighted sum | NORMAL / WARNING / CRITICAL / RECOVERY |

가중치 초기값(self-monitoring 노드용):
```python
weights_node = {
    "chronos": 0.20, "moirai": 0.15,
    "anomaly_transformer": 0.30,    # GPU/NPU 이상이 가장 critical
    "xgboost": 0.30, "llm_embedding": 0.05,
}
weights_esxi = (기존 그대로 유지)
```

---

## 5. ensemble_app.py 확장 설계

### 5.1 새 엔드포인트
```
GET /predict/esxi/all                    (기존 /predict/all 별칭)
GET /predict/esxi/{server_id}            (기존 /predict/{server_id})
GET /predict/node/all                    (5 노드 self-monitoring)
GET /predict/node/{node_name}            (node1 ~ node5)
GET /health                              (기존)
GET /metrics                             (기존)
```

기존 `/predict/all`, `/predict/{server_id}`는 deprecate 경고 + 새 경로로 alias.

### 5.2 Feature pipeline 분리
```python
class FeatureExtractor:
    def for_esxi(self, server_id) -> EsxiFeatures: ...    # 기존 _get_ce_values + _compute_xgb_features
    def for_node(self, node_name) -> NodeFeatures: ...    # 신규 — Prom PromQL queries
```

`NodeFeatures` 내용:
- `cpu_series`: rate(node_cpu_seconds_total{mode!="idle"}) 60분
- `mem_series`: (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) 60분
- `disk_used_pct`: max by(device)(node_filesystem_size_bytes - node_filesystem_avail_bytes) / size
- `net_series`: rate(node_network_receive_bytes_total + node_network_transmit_bytes_total)
- `gpu_metrics`: DCGM_FI_DEV_GPU_TEMP, DCGM_FI_DEV_POWER_USAGE, DCGM_FI_DEV_GPU_UTIL, DCGM_FI_DEV_ECC_DBE_VOL_TOTAL (다변량 시계열)
- `npu_metrics`: (furiosa-metrics-exporter 후속에서 보강) furiosa_npu_temperature, furiosa_npu_power, furiosa_pe_busy_ratio
- `pod_health`: count by(node)(kube_pod_container_status_restarts_total[1h]), sum by(node)(kube_pod_status_phase{phase="Pending"})
- `smart_health`: smartmon_*_raw_value 핵심 4종 (reallocated_sector_ct, current_pending_sector, end_to_end_error, offline_uncorrectable)
- `snapshot_text`: 위를 자연어로 요약 (LLM-embed 용)

### 5.3 Predictor 입력 분기
```python
class ChronosPredictor:
    async def predict(self, signal):  # signal: 1D 시계열 또는 다변량 1차원
        ...

class AnomalyTransformerPredictor:
    async def predict(self, signal):  # 입력 차원이 ESXi(CE 1ch) vs Node(GPU 5ch)
        # win_size, n_channels를 signal.shape에서 추론
```

기존 모델 재사용을 위해 입력은 1D로 평탄화하거나, 노드용은 별도 multivariate 어댑터 둠. 첫 단계는 단순화 — **각 신호별로 별도 호출**(여러 차원을 개별 시계열로 평가 후 ensemble 내부에서 합산).

---

## 6. Grafana 대시보드 4종

| 보드 | datasource | 주 패널 |
|---|---|---|
| A. 노드 자원 (5노드) | Prometheus | CPU 사용률(top 5 프로세스), 메모리, 디스크 IO, 네트워크, 디스크 채움률 (per 노드, 분당 갱신) |
| B. GPU/NPU | Prometheus | GPU util/mem/temp/power per device, NPU util/temp/PE status, ECC error count, fan speed |
| C. K8s 안정성 | Prometheus | Pod restart heatmap, OOMKilled timeline, NotReady duration, Pending pods, Job 실패율 |
| D. AI 예측 점수 | VictoriaMetrics | 9 타깃(5 노드 + 4 ESXi) 시계열 위험도, model_scores 분해, weighted_sum trend, risk_level state |

기본 K8s 보드 import (Grafana.com IDs):
- 1860 (Node Exporter Full)
- 12239 (NVIDIA DCGM Exporter)
- 13332 (kube-state-metrics)
- 신규 작성: B 패널 NPU 부분 + D 보드 전체

---

## 7. Alertmanager 룰 (Phase 3)

PrometheusRule CRD 신규 작성:
```yaml
groups:
- name: failure-pred-self-monitoring
  rules:
  - alert: NodeFailurePredictionCritical
    expr: failure_pred_score{target_type="node"} >= 0.85
    for: 10m
    labels: {severity: critical}
    annotations:
      summary: "{{ $labels.target_id }} 노드 장애 위험 CRITICAL"
  - alert: NodeFailurePredictionWarning
    expr: failure_pred_score{target_type="node"} >= 0.65
    for: 15m
    labels: {severity: warning}
  - alert: GPUNPUHardwareAnomaly
    expr: failure_pred_at_score{target_type="node"} >= 0.95
    for: 5m
    labels: {severity: warning}
  - alert: DiskFailureImminent
    expr: failure_pred_xgb_score{target_type="node",dim="disk"} >= 0.80
    for: 30m
    labels: {severity: critical}
```
라우팅: 기존 `slack-secret`의 webhook으로 전송.

---

## 8. 구현 순서 (Phase별)

| Phase | 산출물 | 검증 |
|---|---|---|
| **0a** | furiosa-metrics-exporter DaemonSet + ServiceMonitor | `curl :9000/metrics`로 `furiosa_*` 메트릭 노출 |
| **0b** | smartctl_exporter DaemonSet + ServiceMonitor | Prom에서 `smartmon_attribute_raw_value` 쿼리 성공 |
| **1a** | Grafana A/B/C 보드 — 기본 import + NPU 패널 추가 | 대시보드 UI에서 5 노드 메트릭 보임 |
| **1b** | Grafana D 보드 — VM datasource 추가, AI 점수 패널 (ESXi/노드 합쳐 9 타깃) | 대시보드에 점수 시계열 표시 |
| **2a** | ensemble_app.py FeatureExtractor.for_node + 새 엔드포인트 추가 (predictor 그대로) | `curl /predict/node/node1` JSON 응답 |
| **2b** | Push job/CronJob — predict/node/all 결과를 VM에 1분마다 push | `failure_pred_score{target_type=node}` 시계열 존재 |
| **3** | PrometheusRule + Alertmanager → Slack | Slack 채널에 테스트 알람 도착 |
| 5 | (옵션) registry → Deployment+PVC | 정전 회복력 |

각 Phase 끝나면 사용자에게 보고 후 다음 단계 진행.

---

## 9. 산출물 트리 (예정)

```
k8s/
  monitoring/                          ← 신규 디렉토리
    furiosa-metrics-exporter.yaml      ← DaemonSet + ServiceMonitor
    smartctl-exporter.yaml             ← DaemonSet + ServiceMonitor
    alerts/
      failure-pred-rules.yaml          ← PrometheusRule
  grafana/
    dashboards/
      a-node-resources.json
      b-gpu-npu.json
      c-k8s-stability.json
      d-ai-prediction-scores.json
  rayserve/
    ensemble_app.py                    ← FeatureExtractor.for_node + 새 엔드포인트 추가
  cronjobs/
    self-pred-push.yaml                ← 1분마다 /predict/node/all → VM push (기존 esxi-response 패턴 재사용)
docs/
  17_self_monitoring_design.md         ← 이 문서
```
