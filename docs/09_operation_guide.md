# 09. 운영 가이드 — HPC 메모리 장애 예측 시스템

> 최종 업데이트: 2026-04-08

---

## 0. 시스템 접속 정보

### 웹 접속 URL

| 서비스 | URL | 용도 | 비고 |
|---|---|---|---|
| **Grafana 대시보드** | http://10.100.230.72:3000 | 전체 모니터링 | 로그인 불필요 (anonymous 접근) |
| **추론 API 문서** | http://10.100.230.71:8000/docs | API Swagger UI | |
| **VictoriaMetrics UI** | http://10.100.230.72:8428/vmui | 메트릭 직접 조회 | 디버깅용 |
| **MinIO Console** | http://10.100.230.70:9001 | 오브젝트 스토리지 | minioadmin / minioadmin |

### 방화벽 허용 필요 포트

| 서버 | IP | 포트 | 서비스 |
|---|---|---|---|
| node2 (모니터링) | 10.100.230.72 | 3000/tcp | Grafana |
| node2 (모니터링) | 10.100.230.72 | 8428/tcp | VictoriaMetrics |
| node3 (AI 서버) | 10.100.230.71 | 8000/tcp | FastAPI 추론 API |
| node1 (저장) | 10.100.230.70 | 9001/tcp | MinIO Console |

### 서버 SSH 접속

| 서버 | 접속 명령 | 계정 |
|---|---|---|
| node3 (AI) | 로컬 | root / qwe123 |
| node2 (모니터링) | `ssh root@10.100.230.72` | root / qwe123 |
| node1 (저장) | `ssh root@10.100.230.70` | root / qwe123 |
| ESXi 4대 | `ssh root@10.148.148.{118,123,126,130}` | root / VMware!0 |

### 서버 역할 분리

```
node3 (10.100.230.71) — AI 학습/추론
  FastAPI (8000), PyTorch, 4모델(Chronos/MOIRAI/XGBoost/AnomalyT)
  GPU: A100 80GB × 2 (cuda:0 추론, cuda:1 학습)

node2 (10.100.230.72) — 모니터링/수집
  VictoriaMetrics (8428), PostgreSQL (5432), Grafana (3000), Telegraf

node1 (10.100.230.70) — 데이터 저장
  VictoriaMetrics 장기보존 365일 (8428), MinIO (9000/9001), PostgreSQL Replica
```

---

## 1. Grafana 대시보드 사용법

### 접속

`http://10.100.230.72:3000` 접속하면 **HPC Server Overview** 대시보드가 홈 화면으로 표시됩니다. 로그인 없이 바로 볼 수 있습니다.

### 대시보드 구성 (19개 패널)

| 행 | 패널 | 설명 |
|---|---|---|
| **1행** | 서버별 장애 확률 게이지 (4대) | vmgnode18/23/26/30 각각의 failure_probability |
| **2행** | vmkernel 에러 수 + CPU/MEM | 호스트별 에러 건수, CPU/메모리 사용률 |
| **3행** | 모델별 스코어 (4개 모델) | Chronos, MOIRAI, **XGBoost**, AnomalyT |
| **4행** | 장애 확률 추이 그래프 | 시간에 따른 failure_probability 변화 |
| **5행** | 모델 스코어 추이 그래프 | 4개 모델 스코어 시계열 (XGBoost 포함) |
| **6행** | vmkernel 에러 추이 그래프 | 호스트별 에러 추세 |

### 게이지 색상 의미

| 색상 | 확률 범위 | 의미 |
|---|---|---|
| 🟢 초록 | 0 ~ 0.30 | 정상 / RECOVERY |
| 🟡 노랑 | 0.30 ~ 0.65 | 정상 범위 |
| 🟠 주황 | 0.65 ~ 0.85 | WARNING — 장애 징후 |
| 🔴 빨강 | 0.85 ~ 1.0 | CRITICAL — 장애 임박 |

---

## 2. 핵심 지표: failure_probability (장애 확률)

4개 모델(Chronos, MOIRAI, XGBoost, Anomaly Transformer)의 예측을 가중 합산한 **최종 장애 확률**입니다.

### 리스크 레벨 판단 기준

| 확률 범위 | 레벨 | 의미 | 자동 대응 |
|---|---|---|---|
| **0.85 이상** | CRITICAL | 장애 임박 | ESXi Maintenance Mode 전환 + 긴급 Slack 알림 |
| **0.65 ~ 0.85** | WARNING | 장애 징후 감지 | VM 신규 배치 차단 + Slack 알림 |
| **0.30 ~ 0.65** | NORMAL | 정상 | 조치 불필요, 모니터링 지속 |
| **0.30 이하** | RECOVERY | 위험 해소 | Maintenance Mode 자동 해제 |

### 그래프 읽는 법

```
확률 1.0 ┤
         │  ████████████████  ← 0.88: CRITICAL (자동 대응 실행됨)
    0.85 ┤- - - - - - - - - - CRITICAL 라인
         │
    0.65 ┤- - - - - - - - - - WARNING 라인
         │
    0.30 ┤- - - - - - - - - - RECOVERY 라인
         │
    0.0  ┤
         └──────────────────→ 시간
```

**주의 패턴:**
- 확률이 **급격히 상승** → 메모리 에러 급증, 즉시 확인
- 확률이 **서서히 올라감** → DIMM 열화 진행 중, 교체 계획 수립
- 여러 서버가 **동시에 상승** → 환경적 요인 (온도, 전압) 의심

---

## 3. model_score — 모델별 개별 스코어

4개 모델이 각각 다른 관점에서 이상을 탐지합니다.

### 모델별 역할과 해석

| 모델 | 대시보드 표시 | 스코어 의미 | 높을 때 의미 |
|---|---|---|---|
| **Chronos** | Chronos | CE 시계열 기반 향후 24시간 예측 | 가까운 미래에 CE 급증 예상 |
| **MOIRAI** | MOIRAI | CE 패턴 이상 구간 탐지 | 현재 CE 패턴이 비정상적 |
| **XGBoost** | XGBoost | 45개 피처 종합 분석 | 복합적 장애 징후 (온도+CE+워크로드) |
| **Anomaly Transformer** | AnomalyT | CE 시계열 구조적 이상 | 이전에 본 적 없는 CE 패턴 |

### 모델 스코어 조합으로 원인 추정

| 상황 | Chronos | MOIRAI | XGBoost | AnomalyT | 추정 원인 |
|---|---|---|---|---|---|
| CE 급증 | 높음 | 높음 | 높음 | 높음 | DIMM 물리적 결함 → 교체 필요 |
| CE 서서히 증가 | 보통 | 높음 | 높음 | 보통 | DIMM 열화 진행 중 |
| 간헐적 CE 스파이크 | 낮음 | 높음 | 보통 | 높음 | 환경 요인 (온도/전압 불안정) |
| 피처만 이상 | 낮음 | 낮음 | 높음 | 낮음 | 워크로드/환경 문제 (CE는 정상) |

### 판단 규칙

```
3개 이상 모델 WARNING 이상 → 장애 확실, 즉시 대응
2개 모델 WARNING 이상     → 주의 관찰, 추세 확인
1개 모델만 높음            → 해당 모델 특성 고려하여 판단
```

---

## 4. esxi_vmkernel_error_cnt — ESXi 메모리 에러

ESXi 호스트의 vmkernel 로그에서 메모리 관련 에러 수입니다.

| 에러 수 | 색상 | 판단 |
|---|---|---|
| 0~4 | 초록 | 정상 |
| 5~14 | 노랑 | 주의 — 에러 누적 중 |
| 15 이상 | 빨강 | 점검 필요 |

**주의 패턴:**
- 특정 호스트에서 **지속적으로 증가** → 해당 호스트 DIMM/스토리지 문제
- **갑자기 급증** → UE(Uncorrectable Error) 직전 신호
- 0이었다가 **갑자기 발생** → 새로운 에러 시작, 추세 관찰

---

## 5. 일상 운영 체크리스트

### 매일 아침 확인 (5분)

1. **Grafana 대시보드** 열기 (http://10.100.230.72:3000)
2. **1행 게이지** 확인
   - 모든 서버 초록(0.3 이하) → 정상
   - 주황/빨강 서버 있음 → 3행 모델 스코어 확인
3. **2행 vmkernel 에러** 확인
   - 전날 대비 증가한 호스트 있는지
4. Slack 알림 확인
   - 밤사이 WARNING/CRITICAL 알림이 있었는지

### WARNING 발생 시

1. 대시보드에서 해당 서버의 **모델 스코어** 확인
2. 어떤 모델이 높은지로 원인 추정 (위 표 참조)
3. vmkernel 에러 증가 여부 확인
4. **추세 관찰**: 1~2시간 모니터링
   - 계속 상승 → DIMM 교체 계획 수립
   - 안정화 → 일시적 현상, 계속 관찰

### CRITICAL 발생 시

1. **자동 대응 확인**: ESXi Maintenance Mode 전환 여부
2. Slack 긴급 알림 확인
3. 해당 호스트의 VM을 다른 호스트로 수동 마이그레이션(vMotion)
4. DIMM 교체 작업 요청
5. 교체 후 failure_probability가 0.30 이하로 떨어지면 → RECOVERY 자동 복구

---

## 6. 서비스 관리

### 서비스 상태 확인

```bash
# node3 (AI 서버)
ps aux | grep uvicorn                     # FastAPI 확인
curl http://localhost:8000/health          # API 상태

# node2 (모니터링)
ssh root@10.100.230.72
systemctl status victoria-metrics          # VictoriaMetrics
systemctl status postgresql                # PostgreSQL
systemctl status grafana-server            # Grafana
systemctl status telegraf                  # Telegraf

# node1 (저장)
ssh root@10.100.230.70
systemctl status victoria-metrics          # VM 장기보존
systemctl status minio                     # MinIO
systemctl status postgresql                # PG Replica
```

### 서비스 재시작

```bash
# FastAPI 추론 서버 (node3)
kill $(ps aux | grep "[u]vicorn" | awk '{print $2}')
cd /opt/failure_prediction
/opt/miniconda3/envs/failure_pred/bin/python -m uvicorn src.api.main:app \
  --host 0.0.0.0 --port 8000 --log-level info > /tmp/api_server.log 2>&1 &

# VictoriaMetrics (node2)
ssh root@10.100.230.72 'systemctl restart victoria-metrics'

# Grafana (node2)
ssh root@10.100.230.72 'systemctl restart grafana-server'

# 메트릭 수집기 (node3) — ESXi + 추론 결과 push
cd /opt/failure_prediction
/opt/miniconda3/envs/failure_pred/bin/python scripts/metrics_pusher.py --daemon \
  > /tmp/metrics_pusher.log 2>&1 &
```

### 시간 동기화 (중요!)

서버 시간이 사무실 PC 시간과 다르면 Grafana에서 데이터가 안 보입니다.

```bash
# 3대 서버 시간 확인
date                                                    # node3
ssh root@10.100.230.72 'date'                          # node2
ssh root@10.100.230.70 'date'                          # node1

# 시간이 다르면 수동 동기화 (예: 오후 3시)
timedatectl set-time "2026-04-08 15:00:00"
ssh root@10.100.230.72 'timedatectl set-time "2026-04-08 15:00:00"'
ssh root@10.100.230.70 'timedatectl set-time "2026-04-08 15:00:00"'
```

---

## 7. 알림 채널

| 채널 | 용도 |
|---|---|
| Slack | WARNING/CRITICAL/RECOVERY 실시간 알림 |
| Grafana 대시보드 | 전체 현황 모니터링 (http://10.100.230.72:3000) |
| FastAPI `/predict/all` | API로 전체 서버 상태 조회 (http://10.100.230.71:8000/predict/all) |
| audit_log (PostgreSQL) | 과거 대응 이력 조회 (node2 PostgreSQL) |

---

## 8. 자주 묻는 질문

### Q: 대시보드에서 데이터가 No data로 나와요
1. **서버 시간 확인** — 사무실 PC와 서버 시간이 같은지 확인 (섹션 6 참조)
2. **FastAPI 서버 확인** — `curl http://10.100.230.71:8000/health` 응답 확인
3. **VictoriaMetrics 확인** — `curl http://10.100.230.72:8428/health` 응답 확인
4. 서비스가 죽었으면 섹션 6의 재시작 명령 실행

### Q: 확률이 높은데 실제 장애가 안 났어요?
CE 데이터가 아직 충분히 쌓이지 않아서 모델이 보수적으로 높은 스코어를 출력합니다.
데이터가 2~4주 쌓이면 정상 구간의 패턴을 학습하여 정확도가 높아집니다.

### Q: 모델별 스코어가 서로 많이 다른데?
각 모델은 다른 관점에서 판단하므로 정상입니다. 최종 판단은 앙상블 확률(failure_probability)로 합니다.

### Q: XGBoost가 대시보드에서 안 보여요
XGBoost는 `xgb_score`라는 별도 메트릭명으로 수집됩니다. 대시보드 모델 스코어 패널에서 XGBoost로 표시됩니다. 안 보이면 FastAPI 서버 재시작 후 2~3분 기다려주세요.

### Q: RECOVERY 상태인데 Maintenance Mode가 해제 안 됐어요?
자동 해제 조건은 확률이 0.30 이하일 때입니다. 수동 해제가 필요하면 ESXi에서 직접 해제하세요.

### Q: 새 서버를 추가하려면?
1. `configs/servers.yaml`과 `configs/esxi.yaml`에 서버 정보 추가
2. `src/api/main.py`의 ESXI_SERVERS 리스트에 추가
3. FastAPI 서버 재시작

---

## 9. 모델 아키텍처 상세

### 전체 구조

```
                   ┌─────────────────────────────────────────┐
  CE 72h 시계열 ──→│  Chronos (T5)  ──→ 예측 기반 스코어      │
                   │  MOIRAI         ──→ 이상탐지 스코어       │──→ 앙상블 ──→ failure_probability
  45개 피처 벡터 ──→│  XGBoost        ──→ 분류 확률            │    (가중합)
  CE 72h 시계열 ──→│  Anomaly Trans. ──→ 구조적 이상 스코어    │
                   └─────────────────────────────────────────┘
```

### 9.1 Chronos — 시계열 예측 모델

| 항목 | 내용 |
|---|---|
| **원본 모델** | Amazon Chronos-T5 (2024) |
| **모델 크기** | chronos-t5-small (46M 파라미터) |
| **기반 구조** | Google T5 (Encoder-Decoder Transformer) |
| **원본 학습 데이터** | 대규모 공개 시계열 데이터셋 (에너지, 교통, 금융 등) |
| **우리 시스템 사용 방식** | **Zero-shot (파인튜닝 없이 그대로 사용)** |
| **입력** | CE 72시간 시계열 (분 단위, 4320 포인트) |
| **출력** | 향후 24시간 CE 예측값 (1440 포인트) |
| **이상 판단** | 예측 피크가 현재 평균 대비 크게 높으면 이상 |

### 9.2 MOIRAI — 범용 시계열 이상탐지

| 항목 | 내용 |
|---|---|
| **원본 모델** | Salesforce MOIRAI (2024) |
| **모델 크기** | moirai-1.0-R-small |
| **기반 구조** | Patch-based Transformer |
| **원본 학습 데이터** | LOTSA — 27B 관측값 |
| **우리 시스템 사용 방식** | **Zero-shot (파인튜닝 없이 그대로 사용)** |
| **입력** | CE 72시간 시계열 |
| **출력** | 예측 불확실성 (표준편차) = 이상 스코어 |

### 9.3 XGBoost — 테이블 피처 기반 분류

| 항목 | 내용 |
|---|---|
| **원본 모델** | XGBoost (Gradient Boosted Decision Trees) |
| **사전학습 데이터** | Alibaba PAKDD 2021 (데이터센터 DRAM CE/UE 로그 300만 건) |
| **파인튜닝** | TTA 자체 데이터 (sample_weight=3.0) |
| **HPO** | Optuna 30 trials |
| **입력** | 45개 피처 벡터 (CE 20 + HW 10 + 워크로드 10 + ESXi 5) |
| **출력** | 장애 확률 (0~1) |
| **재학습** | 매일 새벽 2시 (cuda:1) |

### 9.4 Anomaly Transformer — 비지도 시계열 이상탐지

| 항목 | 내용 |
|---|---|
| **원본 모델** | Anomaly Transformer (Tsinghua, ICLR 2022) |
| **학습 방식** | 비지도 학습 (레이블 불필요) |
| **입력** | CE 시계열 윈도우 (100분 단위) |
| **출력** | Association Discrepancy 기반 이상 스코어 |
| **재학습** | 월 1회 (cuda:1) |

### 앙상블 가중치

```
failure_probability = Chronos × 0.25
                    + MOIRAI × 0.15
                    + XGBoost × 0.35    ← 가장 높음 (45개 피처 종합)
                    + Anomaly Transformer × 0.25
```

---

## 10. 데이터 흐름

```
[60초 주기 수집]

  ESXi 4대                   node3 (AI 서버)              node2 (모니터링)
  ┌──────────┐              ┌──────────────┐            ┌──────────────┐
  │vmgnode18 │──pyVmomi──→ │              │            │              │
  │vmgnode23 │──SSH─────→  │ FastAPI      │──scrape──→│VictoriaMetrics│
  │vmgnode26 │              │ /metrics     │  (60초)    │  (8428)      │
  │vmgnode30 │              │              │            │              │
  └──────────┘              │ 앙상블 추론   │            │ Telegraf     │
                            │ (cuda:0)     │            │ PostgreSQL   │
  node3 로컬                │              │            │ Grafana      │
  ┌──────────┐              │ Chronos      │            └──────┬───────┘
  │ EDAC     │──→           │ MOIRAI       │                   │
  │ IPMI     │──→           │ XGBoost      │                   ↓
  └──────────┘              │ AnomalyT     │            ┌──────────────┐
                            └──────┬───────┘            │ 대시보드      │
                                   │                    │ :3000        │
                                   ↓                    └──────────────┘
                            ┌──────────────┐
                            │ ESXi 자동대응 │            node1 (저장)
                            │ + Slack 알림  │            ┌──────────────┐
                            │ + audit_log  │            │ VM 장기보존   │
                            └──────────────┘            │ MinIO        │
                                                        └──────────────┘

수집 경로:
  promscrape (VictoriaMetrics) ──→ node3 FastAPI /metrics (60초마다)
    → failure_probability, model_score(chronos/moirai/anomaly_transformer)
    → xgb_score, esxi_cpu_usage, esxi_mem_usage, esxi_vmkernel_error_cnt
  Telegraf (node2) ──→ VictoriaMetrics
    → cpu, mem, disk, net, system 메트릭
```

---

## 11. 평가지표

**Accuracy는 사용하지 않습니다** (클래스 불균형으로 무의미).

| 지표 | 설명 | 현재 목표 | 6개월 목표 |
|---|---|---|---|
| **F1 Score** | 정밀도와 재현율의 조화 평균 | > 0.65 | > 0.82 |
| **AUC-PR** | Precision-Recall 곡선 아래 면적 | > 0.60 | > 0.85 |
| **Recall** | 실제 장애 중 탐지한 비율 | > 0.75 | > 0.88 |

Recall을 높이는 것이 우선 — 장애를 놓치는 것(False Negative)이 오탐(False Positive)보다 훨씬 위험합니다.

---

## 12. 모델별 탐지 가능/불가능 영역

| 에러 유형 | Chronos | MOIRAI | XGBoost | AnomalyT |
|---|---|---|---|---|
| CE 급증 | O | O | O | O |
| CE 점진적 증가 | △ | △ | O | △ |
| UE 발생 | O | O | O | O |
| 온도 상승 | X | X | **O** | X |
| 팬 이상 | X | X | **O** | X |
| SCSI 에러 | X | X | △ | X |
| 전압 불안정 | X | X | **O** | X |
| 워크로드 과부하 | X | X | **O** | X |

**XGBoost 가중치가 0.35로 가장 높은 이유:** 환경 요인(온도/팬/전압)을 유일하게 탐지할 수 있는 모델
