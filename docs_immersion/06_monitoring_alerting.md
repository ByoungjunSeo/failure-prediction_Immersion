# 06. 모니터링 및 알림

> Grafana 대시보드, Prometheus 알림규칙, Slack 알림 구성.

---

## Grafana 대시보드

### 접속 정보

| Grafana | URL | 비고 |
|---|---|---|
| 운영용 (monitoring NS) | http://10.100.230.130:31618 | 주 대시보드 |

### 대시보드 목록

| 대시보드 | 폴더 | 용도 |
|---|---|---|
| **PUE GPU Load Controller** | General | GPU 부하 제어 상태 (target/actual, batch size, 온도, 전력) |
| **AI 장애예측 점수** | Self-Monitoring | 5노드 앙상블 점수 시계열, 모델별 점수 |
| **클러스터 노드 종합 현황** | Self-Monitoring | CPU/메모리/디스크/네트워크 통합 뷰 |
| **NVIDIA DCGM Exporter** | Self-Monitoring | GPU 온도/전력/사용률/VRAM/PCIe 대역폭 |
| **Furiosa NPU** | Self-Monitoring | NPU 코어 사용률/전력/온도 |
| **Node Exporter Full** | Self-Monitoring | 시스템 메트릭 상세 (CPU, 메모리, I/O, 네트워크) |

### 대시보드 JSON 파일

대시보드 JSON은 `k8s/grafana/dashboards/`에 저장되어 있으며, ConfigMap으로 Grafana에 프로비저닝됩니다.

```
k8s/grafana/dashboards/
├── ai-prediction-scores.json    # AI 장애예측 점수
├── node-overview.json           # 클러스터 노드 종합 현황
├── pue-gpu-load.json            # PUE GPU Load Controller
├── dcgm-exporter.json           # NVIDIA DCGM
├── npu-furiosa.json             # Furiosa NPU
└── node-exporter-full.json      # Node Exporter Full
```

---

## 알림 체계

### 아키텍처

```
VictoriaMetrics (AI 점수)  ──► vmalert ──► Alertmanager ──► Slack
Prometheus (하드웨어 메트릭) ──► PrometheusRule ──► Alertmanager ──► Slack
```

### vmalert 규칙 (AI 점수 기반)

| 규칙 | 조건 | 심각도 | 설명 |
|---|---|---|---|
| FailurePredictionWarning | `failure_prediction_score{model="ensemble"} > 0.65` for 3m | warning | 장애 위험 경고 |
| FailurePredictionCritical | `failure_prediction_score{model="ensemble"} > 0.85` for 1m | critical | 긴급 장애 위험 |
| FailurePredictionRecovery | `failure_prediction_score{model="ensemble"} < 0.30` for 5m | info | 위험 해소 |

### PrometheusRule 규칙 (하드웨어 기반)

**하드웨어 그룹** (failure-prediction-hardware):

| 규칙 | 조건 | 설명 |
|---|---|---|
| GPUTemperatureHigh | `DCGM_FI_DEV_GPU_TEMP > 85` for 5m | GPU 과열 |
| GPUXidError | `DCGM_FI_DEV_XID_ERRORS > 0` | GPU 하드웨어 에러 |
| DiskHealthDegraded | `smartctl_device_smart_healthy == 0` | 디스크 이상 |
| NodeMemoryPressure | `node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes < 0.1` for 5m | 메모리 부족 |
| NodeDiskPressure | `node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.1` for 5m | 디스크 부족 |

**파이프라인 그룹** (failure-prediction-pipeline):

| 규칙 | 조건 | 설명 |
|---|---|---|
| RayServeDown | Ray head pod absent for 5m | 추론 서비스 중단 |
| VictoriaMetricsDown | VM pod absent for 5m | 시계열 DB 중단 |
| PredictionStale | 마지막 push > 5분 전 | 예측 데이터 정체 |
| XGBoostRetrainFailed | Job 실패 | 재학습 실패 |

### Slack 알림

| 채널 | 수신 알림 | 라우팅 |
|---|---|---|
| `#액침서버_장애예측_알람` | WARNING + CRITICAL | Alertmanager → Slack webhook |

알림 설정 파일:
- `k8s/monitoring/alerts/failure-pred-rules.yaml` — PrometheusRule
- `k8s/monitoring/alerts/alertmanager-slack.yaml` — AlertmanagerConfig
- `k8s/monitoring/alerts/vmalert.yaml` — vmalert Deployment

---

## 데이터소스 구성

### Prometheus (monitoring NS)

| 항목 | 값 |
|---|---|
| In-cluster URL | `monitoring-kube-prometheus-prometheus.monitoring:9090` |
| 수집 대상 | node-exporter, DCGM, Furiosa, smartctl, kube-state-metrics |
| 보존 기간 | 기본 (kube-prometheus-stack 설정) |

### VictoriaMetrics (failure-prediction NS)

| 항목 | 값 |
|---|---|
| In-cluster URL | `victoria-metrics-svc.failure-prediction:8428` |
| NodePort | 30171 |
| 수집 대상 | AI 예측 점수, CE 카운트 |
| Grafana datasource | `grafana-vm-datasource.yaml`로 등록 |

---

## ServiceMonitor 구성

GPU와 NPU 메트릭을 Prometheus가 자동 수집하도록 ServiceMonitor가 등록되어 있습니다.

```yaml
# k8s/monitoring/dcgm-exporter-servicemonitor.yaml
# DCGM Exporter → Prometheus 자동 scrape

# k8s/monitoring/furiosa-metrics-servicemonitor.yaml
# Furiosa Metrics → Prometheus 자동 scrape
```

---

## 운영 진단 명령어

```bash
# 알림 상태 확인
ssh newcluster-master "kubectl -n monitoring exec -it \
  $(kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o name | head -1) \
  -- amtool alert --alertmanager.url=http://localhost:9093"

# vmalert 규칙 상태
curl -s http://10.100.230.130:30171/api/v1/rules | python3 -m json.tool

# Prometheus 쿼리 (GPU 온도)
curl -s "http://10.100.230.130:31618/api/datasources/proxy/1/api/v1/query?query=DCGM_FI_DEV_GPU_TEMP"

# Grafana 대시보드 목록
curl -s -u admin:<password> http://10.100.230.130:31618/api/search?type=dash-db
```
