# 03. AI 모델 상세

> 5개 모델 앙상블로 노드별 장애 확률(0.0 ~ 1.0)을 산출.

---

## 앙상블 구성

```
failure_probability = 0.25 x Chronos
                    + 0.15 x MOIRAI
                    + 0.35 x XGBoost
                    + 0.25 x Anomaly Transformer
                    (+ NPU Embedding: 피처 벡터로 XGBoost에 공급)
```

| 모델 | 가중치 | 분석 관점 | GPU/NPU | 강점 |
|---|---|---|---|---|
| **Chronos** (Amazon) | 0.25 | 향후 1시간 CE 예측, 피크/평균 비율 | GPU 0.25 x 3 replicas | 급증 전조 감지 |
| **MOIRAI** (Salesforce) | 0.15 | Zero-shot 시계열 이상탐지, 예측 분산 | GPU 0.25 x 3 replicas | 패턴 안정성 |
| **XGBoost** | 0.35 | CE 통계 피처(1h/24h/72h) 기반 분류 | CPU | 가장 신뢰도 높은 신호 |
| **Anomaly Transformer** (ICLR 2022) | 0.25 | 최근 100 CE 샘플 재구성 오차 | CPU | 정상 패턴과의 편차 |
| **NPU LLM Embedding** | (간접) | Qwen3-Embedding-4B 벡터로 XGBoost 피처 보강 | Furiosa RNGD | 텍스트 기반 이상 컨텍스트 |

---

## 모델별 상세

### 1. Chronos (Amazon Chronos-T5-Small)

- **원리**: T5 기반 시계열 예측 모델. CE 72시간(4320포인트) 시계열을 입력받아 향후 64포인트를 예측.
- **점수 산출**: `min(1.0, forecast_peak / (recent_60min_mean + ε) / 20.0)`
  - 예측 피크가 최근 평균의 20배 이상이면 1.0 (이상)
- **학습 불필요**: Zero-shot 사전학습 모델, 즉시 적용
- **GPU 사용**: replica당 0.25 GPU, 3 replica (node2-4에 각 1개)
- **패키지**: `chronos-forecasting`

### 2. MOIRAI (Salesforce MOIRAI-1.0-R-Small)

- **원리**: 범용 시계열 모델. 512포인트 컨텍스트로 64포인트 예측, 20 샘플의 분산으로 이상도 측정.
- **점수 산출**: 예측 분산(uncertainty)이 높으면 이상
- **학습 불필요**: Zero-shot
- **GPU 사용**: replica당 0.25 GPU, 3 replica
- **패키지**: `uni2ts`

### 3. XGBoost

- **원리**: 18개 CE 통계 피처로 이진 분류 (정상/이상)
- **피처**: CE 1h/24h/72h 합계, 기울기, 가속도, 분산, 버스트 횟수 등
- **학습**: 매일 02:00 CronJob으로 자동 재학습 (`retrain-xgboost.yaml`)
  - VictoriaMetrics에서 CE 시계열 슬라이싱
  - 자가 라벨링: `ce_count_24h` 분위수 기준
- **모델 저장**: ConfigMap `xgboost-model` (JSON 형식)
- **CPU 전용**: GPU 불필요

### 4. Anomaly Transformer (ICLR 2022)

- **원리**: 시계열 이상탐지 SOTA. 최근 100개 CE 샘플의 재구성 오차가 threshold를 넘으면 이상.
- **점수 산출**: `min(1.0, reconstruction_error / threshold)`
- **threshold**: 환경변수 `AT_THRESHOLD` (현재 2500)
- **PUE 겸용**: PUE GPU Load Controller가 Anomaly Transformer의 continual training을 부하로 사용
- **체크포인트**: `models/checkpoints/anomaly_transformer.pt`
- **CPU 전용**: 추론 시 CPU, 학습 시 GPU (PUE 부하 컨트롤러를 통해)

### 5. NPU LLM Embedding (Qwen3-Embedding-4B)

- **원리**: Furiosa RNGD NPU에서 Qwen3-Embedding-4B 모델로 텍스트 임베딩 생성
- **역할**: 노드 메트릭의 텍스트 기술을 2560차원 벡터로 변환, XGBoost 피처 보강
- **API**: OpenAI 호환 `/v1/embeddings` (port 8000)
- **아티팩트**: `/artifacts/qwen3-embed-4b` (Longhorn PVC 20GB)

---

## Ray Serve 배포 구조

```
RayCluster "failure-pred"
├── Head (node1)
│   ├── Ray GCS
│   ├── Serve Controller
│   └── FastAPI Router (:8000)
│
├── GPU Workers x 3 (node2, node3, node4)
│   ├── ChronosPredictor (0.25 GPU, max 1/node)
│   ├── MOIRAIPredictor (0.25 GPU, max 1/node)
│   └── AnomalyTransformerPredictor (CPU)
│
└── CPU Workers x 1 (유동)
    └── XGBoostPredictor (CPU)
```

### API 엔드포인트

| 경로 | 메서드 | 설명 |
|---|---|---|
| `/predict/node/all` | GET | 5노드 전체 예측 (self-monitoring) |
| `/predict/node/{node_id}` | GET | 개별 노드 예측 |
| `/predict/esxi/all` | GET | ESXi 4대 예측 (레거시, 현재 미사용) |
| `/health` | GET | 서비스 상태 확인 |

### 응답 예시

```json
{
  "predictions": [
    {
      "server_id": "node1",
      "failure_probability": 0.127,
      "risk_level": "RECOVERY",
      "model_scores": {
        "chronos": 0.042,
        "moirai": 0.089,
        "xgboost": 0.213,
        "anomaly_transformer": 0.051
      }
    }
  ]
}
```

---

## 위험 단계

| 앙상블 점수 | 레벨 | 색상 | 대응 |
|---|---|---|---|
| 0.00 ~ 0.30 | RECOVERY | 초록 | 정상 |
| 0.30 ~ 0.65 | NORMAL | 흰색 | 추이 관찰 |
| 0.65 ~ 0.85 | WARNING | 노랑 | Slack 알림, 원인 분석 |
| 0.85 ~ 1.00 | CRITICAL | 빨강 | 긴급 Slack 알림, 즉시 점검 |

---

## 모델 재학습

### XGBoost 자동 재학습 (매일 02:00)

```bash
# CronJob: k8s/cronjobs/retrain-xgboost.yaml
# 수동 실행:
kubectl create job --from=cronjob/retrain-xgboost \
  retrain-manual-$(date +%s) -n failure-prediction
```

### Anomaly Transformer Continual Training

PUE GPU Load Controller가 GPU 부하 생성을 위해 AT를 계속 학습시킵니다. 이 과정에서 체크포인트가 업데이트되며, 운영 CE 패턴에 자연스럽게 적응합니다.

---

## 컨테이너 이미지

| 이미지 | 용도 | 위치 |
|---|---|---|
| `10.100.230.130:5000/failure-pred:gpu-latest` | Ray head/worker, PUE controller, CronJobs | 로컬 레지스트리 |
| `10.100.230.130:5000/failure-pred:npu-latest` | NPU embed 서비스 | 로컬 레지스트리 |
