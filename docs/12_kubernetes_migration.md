# 12. 쿠버네티스 환경 이관 — 개발 현황 및 시스템 명세

> 이 문서는 HPC 메모리 장애 예측 시스템을 Kubernetes 환경으로 이관하기 위한
> **전체 개발 현황, 아키텍처, 소스 코드 명세, 인프라 구성, 데이터 흐름**을 정리합니다.
> 이 문서를 Claude에게 제공하면 K8s 환경에서 동일한 시스템을 구축할 수 있습니다.

---

## 1. 시스템 개요

### 1.1 목적

HPC 서버의 **DRAM 메모리 장애를 사전에 예측**하여, 장애 발생 전에 VM을 안전하게 이동시키는 AI 기반 예측 시스템.

### 1.2 핵심 기능

- DRAM CE(Correctable Error) 시계열 수집 및 분석
- 4개 AI 모델 앙상블로 장애 확률 예측
- ESXi 호스트 자동 대응 (WARNING → VM 배치 차단, CRITICAL → Maintenance Mode)
- Grafana 실시간 모니터링 대시보드
- Slack 알림, PostgreSQL 감사 로그

### 1.3 현재 상태

```
개발 Phase: P0~P7 전체 완료
테스트: 39개 전부 통과
서비스: FastAPI + VictoriaMetrics + Grafana + PostgreSQL 운영 중
모델: Chronos + MOIRAI + XGBoost + Anomaly Transformer 4개 모델 가동
```

---

## 2. 현재 인프라 구성 (베어메탈 3대)

### 2.1 서버 구성

```
node3 (10.100.230.71) — AI 학습/추론 서버
  OS: Rocky Linux 9.7
  CPU: Intel Xeon Gold 6140 × 2 (36코어 72스레드)
  GPU: NVIDIA A100 80GB × 2 (cuda:0 추론, cuda:1 학습)
  RAM: 512GB DDR4
  서비스: FastAPI(8000), PyTorch, Chronos, MOIRAI, XGBoost, Anomaly Transformer

node2 (10.100.230.72) — 모니터링/수집 서버
  OS: Rocky Linux 9.7
  CPU: Intel Xeon Gold 6140 × 2
  RAM: 512GB
  서비스: VictoriaMetrics(8428), PostgreSQL(5432), Grafana(3000), Telegraf

node1 (10.100.230.70) — 데이터 저장 서버
  OS: Rocky Linux 9.7
  CPU: Intel Xeon Gold 6140 × 2
  RAM: 512GB
  서비스: VictoriaMetrics 장기보존(8428, 365d), MinIO(9000/9001), PostgreSQL Replica
```

### 2.2 ESXi 호스트 (모니터링 대상)

```
vmgnode18: 10.148.148.118 (ESXi 6.7, 384GB RAM, 36코어)
vmgnode23: 10.148.148.123
vmgnode26: 10.148.148.126
vmgnode30: 10.148.148.130
접근: SSH(root/VMware!0) + pyVmomi API
```

### 2.3 K8s 이관 시 컨테이너 분리 안

```
Pod 1: inference-api (GPU 필요)
  - FastAPI 추론 서버
  - Chronos, MOIRAI, XGBoost, Anomaly Transformer 모델
  - GPU: 최소 1 × A100 40GB (4개 모델 합계 ~15GB VRAM)
  - CPU: 8코어, RAM: 32GB

Pod 2: metrics-collector (GPU 불필요)
  - ESXi pyVmomi 수집기
  - ESXi SSH 수집기
  - EDAC/IPMI 수집기
  - CPU: 2코어, RAM: 4GB

Pod 3: victoriametrics
  - VictoriaMetrics 시계열 DB
  - PVC: 100GB (90일 보존)
  - CPU: 2코어, RAM: 8GB

Pod 4: postgresql
  - PostgreSQL (failure_events, training_labels, audit_log)
  - PVC: 20GB
  - CPU: 1코어, RAM: 4GB

Pod 5: grafana
  - Grafana 대시보드
  - CPU: 1코어, RAM: 2GB

Pod 6: model-trainer (GPU 필요, CronJob)
  - XGBoost 일일 재학습 (매일 02:00)
  - Anomaly Transformer 월간 재학습
  - GPU: cuda:1 (학습 전담)
```

---

## 3. 소스 코드 구조

### 3.1 디렉토리 구조

```
/opt/failure_prediction/
├── src/
│   ├── api/
│   │   └── main.py                    # FastAPI 서버 (503줄)
│   ├── collectors/
│   │   ├── edac_collector.py          # EDAC CE/UE 수집 (289줄)
│   │   ├── ipmi_collector.py          # IPMI 센서 수집 (206줄)
│   │   ├── smart_collector.py         # SMART 디스크 수집 (212줄)
│   │   ├── esxi_collector.py          # ESXi pyVmomi 수집 (232줄)
│   │   └── esxi_ssh_collector.py      # ESXi SSH 읽기 수집 (301줄)
│   ├── features/
│   │   └── feature_pipeline.py        # 45개 피처 계산 (452줄)
│   ├── models/
│   │   ├── chronos_predictor.py       # Chronos Zero-shot 예측 (138줄)
│   │   ├── moirai_predictor.py        # MOIRAI Zero-shot 이상탐지 (167줄)
│   │   ├── xgboost_predictor.py       # XGBoost 분류기 (310줄)
│   │   ├── anomaly_transformer.py     # Anomaly Transformer 비지도 (364줄)
│   │   └── ensemble.py                # 4모델 앙상블 (239줄)
│   ├── esxi/
│   │   └── action_handler.py          # ESXi 자동 대응 (485줄)
│   └── labeling/
│       ├── models.py                  # SQLAlchemy DB 모델 (100줄)
│       └── label_generator.py         # 자동 레이블 생성 (264줄)
├── tests/
│   ├── test_edac_collector.py         # 9 tests
│   ├── test_feature_pipeline.py       # 13 tests
│   ├── test_esxi_handler.py           # 11 tests
│   └── integration/
│       └── test_full_pipeline.py      # 6 tests (E2E)
├── scripts/
│   ├── auto_training_scenario.py      # 학습 데이터 자동 생성
│   ├── metrics_pusher.py              # VictoriaMetrics push
│   ├── inject_test_fault.py           # 장애 주입 테스트
│   ├── esxi_load_driver.py            # ESXi 부하 생성
│   ├── setup_telegraf.py              # Telegraf 배포
│   ├── download_public_data.py        # Alibaba 데이터 다운로드
│   └── grafana_dashboard.json         # Grafana 대시보드 JSON
├── configs/
│   ├── servers.yaml                   # 서버 구성
│   └── esxi.yaml                      # ESXi 호스트 구성
├── models/checkpoints/
│   ├── xgboost_model.json             # 학습된 XGBoost 모델
│   └── anomaly_transformer.pt         # 학습된 AT 모델
├── vendor/
│   └── Anomaly-Transformer/           # thuml/Anomaly-Transformer (수정됨)
├── alembic/                           # DB 마이그레이션
├── data/alibaba_pakdd2021/            # 학습 데이터
├── .env                               # 환경 변수
└── .gitignore
```

### 3.2 총 코드량

```
Python 소스: ~6,900줄
테스트: 39개
```

---

## 4. AI 모델 상세

### 4.1 모델 목록

| 모델 | 역할 | HuggingFace/GitHub | 사용 방식 | GPU 메모리 |
|---|---|---|---|---|
| **Chronos** | CE 시계열 예측 | amazon/chronos-t5-small (46M) | Zero-shot | ~2GB |
| **MOIRAI** | CE 이상탐지 | Salesforce/moirai-1.0-R-small | Zero-shot | ~3GB |
| **XGBoost** | 피처 기반 분류 | xgboost 3.2.0 | Alibaba 사전학습 + 파인튜닝 | CPU |
| **Anomaly Transformer** | 구조적 이상탐지 | thuml/Anomaly-Transformer (ICLR 2022) | 비지도 자체 학습 | ~2GB |

### 4.2 앙상블 가중치

```python
failure_probability = Chronos × 0.25
                    + MOIRAI × 0.15
                    + XGBoost × 0.35
                    + Anomaly Transformer × 0.25
```

### 4.3 모델 로드 코드

```python
# Chronos
from chronos import ChronosPipeline
pipeline = ChronosPipeline.from_pretrained("amazon/chronos-t5-small", device_map="cuda:0", dtype=torch.float32)

# MOIRAI
from uni2ts.model.moirai import MoiraiForecast, MoiraiModule
module = MoiraiModule.from_pretrained("Salesforce/moirai-1.0-R-small")
model = MoiraiForecast(module=module, prediction_length=64, context_length=512, ...)

# XGBoost
model = xgb.XGBClassifier()
model.load_model("models/checkpoints/xgboost_model.json")

# Anomaly Transformer (vendor/ 수정본 사용)
# vendor/Anomaly-Transformer/model/attn.py에서 .cuda() → register_buffer로 수정됨
checkpoint = torch.load("models/checkpoints/anomaly_transformer.pt")
```

### 4.4 Anomaly Transformer 수정 사항

`vendor/Anomaly-Transformer/model/attn.py`에서 다음을 수정:

```python
# 원본 (하드코딩된 .cuda())
self.distances = torch.zeros((window_size, window_size)).cuda()

# 수정 (디바이스 독립)
self.register_buffer(
    "distances",
    torch.abs(torch.arange(window_size).unsqueeze(0).float()
              - torch.arange(window_size).unsqueeze(1).float())
)

# 원본
prior = self.distances.unsqueeze(0).unsqueeze(0).repeat(...).cuda()

# 수정
prior = self.distances.unsqueeze(0).unsqueeze(0).repeat(...)
```

### 4.5 XGBoost 학습 데이터

```
Alibaba PAKDD 2021 기반 합성 데이터:
  - 서버 500대 × DIMM 16장 = 8,000 DIMM
  - 관측 기간: 180일
  - 장애: 118건 (연간 3% 장애율 기반)
  - CE 패턴: 정상→열화→급증→UE 3단계
  - Positive: 590건 (장애 전 6/12/24/48/72시간)
  - Negative: 5,815건 (1:10 비율)
  - 피처: 18개 (CE 9 + HW 6 + 워크로드 3)
  - 학습 결과: F1=1.0, AUC-PR=1.0, Recall=1.0

피처 중요도 Top 3:
  1. ce_count_24h: 991 (24시간 CE 누적)
  2. ce_count_72h: 318 (72시간 CE 누적)
  3. ce_max: 295 (CE 최대값)
```

---

## 5. 45개 피처 정의

### Category A — CE 시계열 (20개)

| 피처 | 계산 | 소스 |
|---|---|---|
| ce_count_1h | 최근 1시간 CE 합계 | EDAC/VictoriaMetrics |
| ce_count_24h | 24시간 CE 누적 | |
| ce_count_72h | 72시간 CE 누적 | |
| ce_slope_1h | 1시간 기울기 (linregress) | |
| ce_slope_6h | 6시간 기울기 | |
| ce_slope_24h | 24시간 기울기 | |
| ce_r2_24h | 24시간 기울기 R² | |
| ce_burst_ratio | 최근1h / 이전평균 | |
| ce_burst_flag | ratio > 10 → 1 | |
| ce_interval_mean | 이벤트 평균 간격 | |
| ce_interval_slope | 간격 단축 추세 | |
| ce_acceleration | 2차 미분 | |
| ce_cumulative_7d | 7일 누적 | |
| mce_page_count_24h | MCE 페이지 에러 | rasdaemon |
| mce_uncorrected_flag | UE 발생 여부 0/1 | |
| same_rank_ce_ratio | 같은 Rank CE 비율 | |
| same_row_ce_ratio | 같은 Row CE 비율 | |
| dimm_slot_id | DIMM 슬롯 번호 | |
| channel_id | 메모리 채널 번호 | |
| socket_id | CPU 소켓 번호 | |

### Category B — 하드웨어 환경 (10개)

| 피처 | 소스 |
|---|---|
| cpu_temp_mean_1h | IPMI |
| cpu_temp_slope_6h | IPMI |
| cpu_temp_throttle_cnt | IPMI SEL |
| psu_voltage_stddev_1h | IPMI |
| fan_rpm_anomaly | IPMI |
| system_uptime_days | IPMI |
| smart_reallocated_delta_7d | smartctl |
| smart_wear_leveling | smartctl |
| power_consumption | IPMI dcmi |
| ipmi_sel_error_cnt | IPMI SEL |

### Category C — 워크로드 (10개)

| 피처 | 소스 |
|---|---|
| cpu_usage_mean_1h | Telegraf |
| cpu_usage_max_1h | Telegraf |
| memory_used_pct | Telegraf |
| memory_bandwidth_util | perf |
| cache_miss_rate | perf |
| numa_local_ratio | numastat |
| page_fault_rate | /proc/vmstat |
| oom_kill_count_24h | syslog |
| kernel_panic_cnt_7d | syslog |
| swap_usage_pct | Telegraf |

### Category D — ESXi (5개)

| 피처 | 소스 |
|---|---|
| esxi_vm_count | pyVmomi |
| esxi_mem_balloon_sum | pyVmomi |
| esxi_cpu_ready_sum | pyVmomi |
| esxi_mem_swapped_sum | pyVmomi |
| esxi_mem_overcommit_ratio | pyVmomi |

---

## 6. FastAPI 엔드포인트

| 메서드 | 경로 | 설명 | 응답 |
|---|---|---|---|
| GET | /predict/{server_id} | 단일 서버 예측 | failure_probability, risk_level, model_scores(4개) |
| GET | /predict/all | 전체 서버 일괄 예측 | predictions[], warning_count, critical_count |
| GET | /models/scores/{server_id} | 모델별 개별 스코어 | scores{chronos, moirai, xgboost, anomaly_transformer} |
| GET | /health | 서버 상태 | status, models_loaded, uptime |
| GET | /models/info | 모델 정보 | weights, version |
| POST | /labels/add | 수동 레이블 추가 | status |
| GET | /metrics | Prometheus 메트릭 | failure_probability, model_score, esxi_*, memory_errors |

### /metrics 출력 형식

```
failure_prediction_up 1
failure_prediction_uptime_seconds 1234.5
failure_probability{server="vmgnode18"} 0.7559
model_score{server="vmgnode18",model="chronos"} 0.7006
model_score{server="vmgnode18",model="moirai"} 1.0000
model_score{server="vmgnode18",model="xgboost"} 0.5164
model_score{server="vmgnode18",model="anomaly_transformer"} 1.0000
xgb_score{server="vmgnode18"} 0.5164
esxi_cpu_usage{host="vmgnode18"} 99.98
esxi_mem_usage{host="vmgnode18"} 4.20
esxi_vmkernel_error_cnt{host="vmgnode18"} 500
esxi_vm_count{host="vmgnode18"} 2
memory_errors{server="vmgnode18",mc="0",csrow="0",channel="0"} 1
```

---

## 7. 리스크 레벨 및 자동 대응

| 확률 | 레벨 | 자동 대응 |
|---|---|---|
| >= 0.85 | CRITICAL | ESXi Maintenance Mode 전환 + Slack 긴급 알림 |
| 0.65 ~ 0.85 | WARNING | VM 배치 차단 (Admission Control) + Slack 알림 |
| 0.30 ~ 0.65 | NORMAL | 조치 없음 |
| <= 0.30 | RECOVERY | Maintenance Mode 해제 + Slack 복구 알림 |

ESXi 대응은 `src/esxi/action_handler.py`의 `EsxiActionHandler.respond()` 메서드에서 처리.

---

## 8. 데이터베이스 스키마

### PostgreSQL (node2:5432, DB: failure_pred)

```sql
-- UE 장애 이벤트
CREATE TABLE failure_events (
    id SERIAL PRIMARY KEY,
    server_id VARCHAR(50) NOT NULL,
    dimm_slot VARCHAR(50) NOT NULL,
    mc INTEGER DEFAULT 0,
    channel INTEGER DEFAULT 0,
    csrow INTEGER DEFAULT 0,
    error_type VARCHAR(20) DEFAULT 'uncorrected',
    error_count INTEGER DEFAULT 1,
    address VARCHAR(50),
    detected_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

-- 학습용 Positive/Negative 레이블
CREATE TABLE training_labels (
    id SERIAL PRIMARY KEY,
    server_id VARCHAR(50) NOT NULL,
    dimm_slot VARCHAR(50) NOT NULL,
    feature_timestamp TIMESTAMP NOT NULL,
    label INTEGER NOT NULL,  -- 0=Negative, 1=Positive
    hours_before_failure FLOAT,
    failure_event_id INTEGER,
    features_json JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ESXi 자동 대응 감사 로그
CREATE TABLE audit_log (
    id SERIAL PRIMARY KEY,
    server_id VARCHAR(50) NOT NULL,
    host_id VARCHAR(50),
    action VARCHAR(100) NOT NULL,
    risk_level VARCHAR(20) NOT NULL,
    failure_probability FLOAT,
    model_scores JSONB,  -- 4개 모델 각각의 스코어
    details TEXT,
    success BOOLEAN DEFAULT TRUE,
    executed_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
```

---

## 9. VictoriaMetrics 메트릭

### 수집 방식

```
promscrape (VictoriaMetrics 내장 스크래퍼)
  → node3 FastAPI /metrics 를 60초마다 수집
  → 수집 설정: /etc/victoria-metrics-promscrape.yml

Telegraf (node2)
  → cpu, mem, disk, net, system 메트릭 → VictoriaMetrics /write
```

### promscrape 설정

```yaml
scrape_configs:
  - job_name: "failure_prediction_api"
    scrape_interval: 60s
    scrape_timeout: 30s
    metrics_path: /metrics
    static_configs:
      - targets: ["10.100.230.71:8000"]
        labels:
          instance: "node3"
```

### 주요 메트릭

| 메트릭 | 라벨 | 설명 |
|---|---|---|
| failure_probability | server | 앙상블 장애 확률 (0~1) |
| model_score | server, model | 모델별 개별 스코어 |
| xgb_score | server | XGBoost 스코어 (별도 메트릭) |
| esxi_cpu_usage | host | ESXi CPU 사용률 (%) |
| esxi_mem_usage | host | ESXi 메모리 사용률 (%) |
| esxi_vmkernel_error_cnt | host | vmkernel 메모리 에러 수 |
| esxi_vm_count | host | VM 수 |
| memory_errors | server, mc, csrow, channel | CE 에러 수 |

---

## 10. Grafana 대시보드

### 구성 (19개 패널 + 3개 안내 텍스트 = 22개)

| 행 | 패널 | 쿼리 |
|---|---|---|
| 안내 | 범례 텍스트 | - |
| 1행 | 게이지 × 4 (서버별 장애 확률) | `failure_probability{server="..."}` |
| 2행 | stat × 4 (vmkernel) + stat × 4 (CPU/MEM) | `esxi_vmkernel_error_cnt`, `esxi_cpu_usage`, `esxi_mem_usage` |
| 안내 | 모델 스코어 해석 텍스트 | - |
| 3행 | stat × 4 (모델 스코어) | `model_score{server="...",model="..."}` + `xgb_score` |
| 안내 | 추이 그래프 해석 텍스트 | - |
| 4행 | timeseries (장애 확률 추이) | `failure_probability` |
| 5행 | timeseries (모델 스코어 추이) | `model_score` + `xgb_score` |
| 6행 | timeseries (vmkernel 에러 추이) | `esxi_vmkernel_error_cnt` |

### 데이터소스

```
VictoriaMetrics: uid=victoriametrics, type=prometheus, url=http://localhost:8428
PostgreSQL: uid=postgresql, type=grafana-postgresql-datasource, url=localhost:5432
```

### 대시보드 JSON

`scripts/grafana_dashboard.json`에 포함 (API로 import 가능)

---

## 11. 스케줄러 (APScheduler)

```python
# FastAPI lifespan에서 시작
scheduler = BackgroundScheduler()

# 1분마다: ESXi 수집 + CE 시뮬레이션 + 전체 서버 추론 + 캐시 갱신
scheduler.add_job(_scheduled_inference, "interval", minutes=1)

# 매일 새벽 2시: XGBoost 재학습
scheduler.add_job(_scheduled_retrain, "cron", hour=2)

# 일요일 새벽 3시: 앙상블 가중치 Optuna 재최적화
scheduler.add_job(_scheduled_optimize, "cron", day_of_week="sun", hour=3)
```

---

## 12. Python 패키지 의존성

### 핵심 패키지

```
torch==2.4.1
torchvision==0.19.1+cu121
chronos-forecasting==2.2.2
uni2ts==2.0.0
xgboost==3.2.0
scikit-learn==1.8.0
optuna==4.8.0
fastapi==0.135.3
uvicorn==0.43.0
mlflow==3.10.1
pandas==2.1.4
numpy==1.26.4
scipy==1.11.4
pyVmomi==9.0.0.0
paramiko==4.0.0
psycopg2-binary==2.9.11
SQLAlchemy==2.0.49
alembic==1.18.4
APScheduler==3.11.2
prometheus_client==0.24.1
requests==2.33.1
pyyaml==6.0.3
python-dotenv==1.0.0
imbalanced-learn==0.14.1
shap==0.49.1
lightgbm==4.6.0
pytorch-lightning==2.6.1
```

### Python 버전

```
Python 3.11.15 (Miniconda)
CUDA 12.1 (PyTorch cu121)
```

---

## 13. 환경 변수 (.env)

```bash
ESXI_PASSWORD=VMware!0
BMC_PASSWORD=qwe123
DB_PASSWORD=qwe123
DB_HOST=10.100.230.72       # K8s에서는 postgresql 서비스명으로 변경
DB_NAME=failure_pred
DB_USER=hpcdev
VICTORIA_METRICS_URL=http://10.100.230.72:8428  # K8s에서는 서비스명으로 변경
GRAFANA_URL=http://10.100.230.72:3000
SLACK_WEBHOOK_URL=https://hooks.slack.com/...
MLFLOW_TRACKING_URI=file:///opt/failure_prediction/mlruns
MINIO_ENDPOINT=http://10.100.230.70:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
```

---

## 14. 테스트

### 39개 테스트 목록

```
tests/test_edac_collector.py (9개)
  - CE/UE 카운트 파싱, dmidecode 파싱, rasdaemon 폴링, 통합

tests/test_feature_pipeline.py (13개)
  - CE 피처 20개, HW 피처 10개, 워크로드 10개, ESXi 5개
  - 총 45개 피처, 결측값 없음, 5초 이내 계산

tests/test_esxi_handler.py (11개)
  - WARNING/CRITICAL/RECOVERY 대응
  - Slack 알림에 model_scores 4개 포함 확인
  - audit_log DB 기록

tests/integration/test_full_pipeline.py (6개)
  - E2E: VictoriaMetrics mock → 피처 → 앙상블 → ESXi 대응
  - WARNING/CRITICAL/RECOVERY 플로우
  - 4개 모델 스코어 completeness
```

### 실행

```bash
cd /opt/failure_prediction
/opt/miniconda3/envs/failure_pred/bin/python -m pytest tests/ -v
```

---

## 15. K8s 이관 시 주의 사항

### 15.1 GPU 필수

- Chronos + MOIRAI + Anomaly Transformer는 GPU 필수
- 최소 A100 40GB 또는 동급 (4개 모델 합계 ~8GB VRAM, 추론 시 ~15GB)
- K8s에서 NVIDIA GPU Operator + Device Plugin 필요

### 15.2 Anomaly Transformer vendor 코드

- `vendor/Anomaly-Transformer/`를 Docker 이미지에 포함해야 함
- `model/attn.py` 수정본이 중요 (`.cuda()` 제거)

### 15.3 ESXi 접근

- ESXi SSH + pyVmomi는 네트워크 도달 가능해야 함
- K8s Pod에서 ESXi IP(10.148.148.x)에 접근 가능한지 확인 필요
- hostNetwork 또는 별도 네트워크 정책 필요할 수 있음

### 15.4 모델 파일

- `models/checkpoints/xgboost_model.json` (학습된 모델)
- `models/checkpoints/anomaly_transformer.pt` (학습된 모델)
- Chronos, MOIRAI는 HuggingFace에서 자동 다운로드
- PV 또는 initContainer로 모델 파일 제공

### 15.5 시간 동기화

- 서버 시간과 브라우저 시간이 다르면 Grafana에서 No data 발생
- K8s 노드의 NTP 동기화 필수

### 15.6 XGBoost 피처명 호환

- XGBoost 모델은 학습 시 사용한 피처명과 추론 시 피처명이 정확히 일치해야 함
- 현재 학습 피처: 18개 (ce_count_1h, ce_count_24h, ... swap_usage_pct)
- 운영 피처: 45개 (전체 feature_pipeline 출력)
- 피처 불일치 시 ValueError 발생 → 재학습 필요

### 15.7 promscrape 대안

- 현재: VictoriaMetrics 내장 promscrape가 FastAPI /metrics를 수집
- K8s에서: ServiceMonitor (Prometheus Operator) 또는 VictoriaMetrics Operator의 VMServiceScrape 사용
- `/metrics` 엔드포인트는 캐시 기반이라 즉시 응답 (0.01초)

---

## 16. 학습 데이터 현황

```
/opt/failure_prediction/data/alibaba_pakdd2021/
  kernel_errors.csv        (1.2 MB, CE 로그 28,326건)
  failure_labels.csv       (0.1 MB, DIMM 8,000개)
  training_features.csv    (1.8 MB, 피처 6,405건)
```

이 데이터는 Docker 이미지에 포함하거나 PV로 마운트.

---

## 17. Dockerfile 참고 (inference-api)

```dockerfile
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

RUN apt-get update && apt-get install -y python3.11 python3-pip git
WORKDIR /app

# 의존성
COPY requirements.txt .
RUN pip install -r requirements.txt

# 소스 코드
COPY src/ src/
COPY vendor/ vendor/
COPY configs/ configs/
COPY models/checkpoints/ models/checkpoints/
COPY scripts/ scripts/
COPY .env .

# 모델 다운로드 (빌드 시)
RUN python3 -c "from chronos import ChronosPipeline; ChronosPipeline.from_pretrained('amazon/chronos-t5-small')"
RUN python3 -c "from uni2ts.model.moirai import MoiraiModule; MoiraiModule.from_pretrained('Salesforce/moirai-1.0-R-small')"

EXPOSE 8000
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 18. 현재 알려진 이슈

1. **MOIRAI 추론 느림**: 단일 서버 추론 ~60초 → prediction_length/context_length 축소 검토
2. **XGBoost 피처 불일치**: 18개 피처로 학습했지만 운영에서 45개 피처 사용 → 재학습 필요
3. **ESXi vCenter 관리**: ESXi가 vCenter(10.240.240.11)에 연결되어 리소스 설정 변경 차단
4. **NTP 미설정**: 서버 시간 수동 동기화 필요 → K8s에서는 노드 NTP로 해결
5. **CE 데이터 부족**: 실제 CE 에러가 0건 → 시뮬레이션으로 대체 중 → 실 운영 데이터 축적 필요
