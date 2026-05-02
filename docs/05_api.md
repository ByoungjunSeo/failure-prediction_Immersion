# 05. 추론 API

## FastAPI 서버 구성

```
포트: 8000
GPU:  cuda:0 (Chronos/MOIRAI/XGBoost 추론 전담)
URL:  http://10.100.230.71:8000
문서: http://10.100.230.71:8000/docs
```

---

## 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/predict/{server_id}` | 단일 서버 앙상블 장애 확률 |
| GET | `/predict/all` | 전체 서버 일괄 추론 |
| GET | `/models/scores/{server_id}` | 모델별 개별 스코어 |
| GET | `/health` | 서버 상태 |
| GET | `/models/info` | 현재 모델 버전 및 가중치 |
| POST | `/labels/add` | 수동 장애 레이블 추가 |
| GET | `/metrics` | Prometheus 형식 메트릭 |

---

## 응답 형식

```json
{
  "server_id": "node3 (18AFD199)",
  "timestamp": "2025-04-04T02:30:00",
  "failure_probability": 0.823,
  "risk_level": "CRITICAL",
  "suspect_dimm": "mc0/csrow2/ch0 (슬롯 A2)",
  "model_scores": {
    "chronos":             0.81,
    "moirai":              0.79,
    "xgboost_finetuned":   0.87,
    "anomaly_transformer": 0.78
  },
  "top_causes": [
    {"feature": "CE 24h 증가 기울기", "impact": 0.342},
    {"feature": "CE 급증 비율",       "impact": 0.218},
    {"feature": "Chronos 예측 피크",  "impact": 0.156}
  ],
  "recommended_action": "즉시 VM 마이그레이션 + Maintenance Mode",
  "lead_time_estimate": "12~24시간 내 장애 예상"
}
```

---

## 모델 로드 전략 (시작 시)

```python
# cuda:0에 추론용 모델 모두 로드
chronos_pipeline = ChronosPipeline.from_pretrained(
    "amazon/chronos-t5-small", device_map="cuda:0", torch_dtype=torch.float16
)
moirai_model = MoiraiForecast.from_pretrained(
    "Salesforce/moirai-1.0-R-small"
).to("cuda:0")
xgb_model = mlflow.xgboost.load_model("models:/xgb_finetuned/Production")
anomaly_transformer = load_anomaly_transformer("models:/anomaly_t/Production")
```

---

## 자동 스케줄러 (APScheduler)

```python
# 1분마다: 전체 서버 앙상블 추론
@scheduler.scheduled_job('interval', minutes=1)
async def run_inference():
    for server_id in get_active_servers():
        result = await predict(server_id)
        if result.risk_level != "NORMAL":
            await esxi_handler.respond(result)

# 매일 새벽 2시: XGBoost 파인튜닝 재실행
@scheduler.scheduled_job('cron', hour=2)
async def retrain_xgboost():
    await finetune_xgboost_with_new_data()

# 매주 일요일 새벽 3시: 데이터 품질 + 앙상블 가중치 재최적화
@scheduler.scheduled_job('cron', day_of_week='sun', hour=3)
async def weekly_tune():
    await optimize_ensemble_weights()
    await check_feature_drift()
```

---

## 성능 목표

```
GET /predict/{server_id}  : < 300ms (Chronos 추론 포함)
GET /predict/all (4대)    : < 2초
GPU 메모리 (cuda:0)       : < 20GB (모델 모두 로드 후)
```
