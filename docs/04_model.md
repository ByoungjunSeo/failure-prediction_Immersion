# 04. 모델 전략 — 오픈소스 모델 활용

## 핵심 원칙

> 처음부터 모델을 만들지 않습니다.
> 검증된 오픈소스 모델을 활용해 개발 기간을 단축하고,
> 레이블 없이도 3주차부터 이상탐지를 가동합니다.

```
개발 기간: 9주 → 6~7주로 단축
레이블:    없어도 즉시 시작 가능 (Zero-shot)
공개 데이터: Alibaba PAKDD 2021 (실제 DRAM 로그 300만 건)
```

---

## 3단계 모델 전략

### 1단계 — Zero-shot 즉시 가동 (Week 3부터)

레이블 없이 바로 CE 시계열 이상탐지를 시작합니다.

#### Chronos (Amazon, 2024)

```python
pip install chronos-forecasting

from chronos import ChronosPipeline
import torch, pandas as pd

# 모델 로드 (cuda:0 추론 전담 GPU)
pipeline = ChronosPipeline.from_pretrained(
    "amazon/chronos-t5-small",   # 46M 파라미터, 빠른 추론
    device_map="cuda:0",
    torch_dtype=torch.float16,
)
# 필요 시 더 큰 모델:
# "amazon/chronos-t5-base"   (200M)
# "amazon/chronos-t5-large"  (710M)

def predict_ce_anomaly(ce_series: pd.Series) -> dict:
    """
    CE 72시간 시계열 → 향후 24시간 예측
    예측값이 현재 평균보다 크게 높으면 장애 예측
    """
    context = torch.tensor(ce_series.values, dtype=torch.float32)

    forecast = pipeline.predict(
        context=context.unsqueeze(0),
        prediction_length=24 * 60,   # 24시간 (분 단위)
        num_samples=20,
    )
    # forecast shape: (1, num_samples, prediction_length)

    median_forecast = forecast[0].median(dim=0).values
    current_mean    = ce_series[-60:].mean()   # 최근 1시간 평균
    predicted_peak  = median_forecast.max().item()

    anomaly_score = predicted_peak / (current_mean + 1e-9)

    return {
        "anomaly_score":   anomaly_score,
        "predicted_peak":  predicted_peak,
        "risk_level":      "CRITICAL" if anomaly_score > 10
                           else "WARNING" if anomaly_score > 3
                           else "NORMAL",
    }
```

#### MOIRAI (Salesforce, 2024)

```python
pip install uni2ts

from uni2ts.model.moirai import MoiraiForecast, MoiraiConfig
from gluonts.dataset.pandas import PandasDataset
import torch

# 모델 로드
model = MoiraiForecast.from_pretrained(
    "Salesforce/moirai-1.0-R-small",   # 소형 빠른 모델
    prediction_length=1440,             # 24시간
    context_length=4320,                # 72시간
    patch_size=32,
    num_samples=20,
    target_dim=1,
    feat_dynamic_real_dim=0,
    past_feat_dynamic_real_dim=0,
).to("cuda:0")

def detect_anomaly_moirai(ce_series: pd.Series) -> float:
    """
    Zero-shot 이상탐지 스코어 반환
    높을수록 이상
    """
    ds = PandasDataset(dict(target=ce_series))
    predictor = model.create_predictor(batch_size=32)
    forecasts = list(predictor.predict(ds))

    # 예측 불확실성 = 이상 스코어
    forecast_std = forecasts[0].samples.std(axis=0).mean()
    return float(forecast_std)
```

---

### 2단계 — 공개 데이터 파인튜닝 (Week 4~5)

#### Alibaba PAKDD 2021 데이터셋

```python
"""
데이터셋 정보:
  URL: https://tianchi.aliyun.com/dataset/132973
  내용: 실제 데이터센터 DRAM CE/UE 로그 300만 건
  포함: kernel log, mcelog, address log, 장애 레이블

다운로드 후 활용:
  1. 데이터 전처리 → TTA 피처 형식으로 변환
  2. XGBoost 사전학습
  3. TTA 자체 데이터로 파인튜닝 (Transfer Learning)
"""

import pandas as pd
import xgboost as xgb
from sklearn.model_selection import StratifiedKFold

def train_xgboost_with_public_data(
    public_X, public_y,   # Alibaba 데이터
    tta_X=None, tta_y=None  # TTA 자체 데이터 (쌓이면 추가)
):
    # 클래스 불균형
    scale_pos = (public_y == 0).sum() / (public_y == 1).sum()

    params = {
        "device":           "cuda",   # cuda:1 (학습 전담)
        "tree_method":      "hist",
        "max_depth":        6,
        "learning_rate":    0.01,
        "n_estimators":     1000,
        "scale_pos_weight": scale_pos,
        "eval_metric":      "aucpr",
        "subsample":        0.8,
        "colsample_bytree": 0.8,
        "early_stopping_rounds": 50,
    }

    model = xgb.XGBClassifier(**params)

    if tta_X is not None:
        # TTA 데이터 가중치 높여서 파인튜닝
        import numpy as np
        X = pd.concat([public_X, tta_X])
        y = pd.concat([public_y, tta_y])
        w = np.concatenate([
            np.ones(len(public_X)),
            np.ones(len(tta_X)) * 3.0   # TTA 데이터 3배 가중치
        ])
        model.fit(X, y, sample_weight=w,
                  eval_set=[(tta_X, tta_y)])
    else:
        model.fit(public_X, public_y)

    return model
```

#### Anomaly Transformer (ICLR 2022)

```python
"""
GitHub: thuml/Anomaly-Transformer
설치: pip install git+https://github.com/thuml/Anomaly-Transformer

특징:
  - Association Discrepancy 기반 이상탐지
  - 시계열의 이상 구간을 Association score로 탐지
  - CE 72시간 시계열에 직접 적용 가능
  - 레이블 없이 비지도 학습 가능

활용:
  - Chronos + MOIRAI + Anomaly Transformer 결과를 앙상블
  - 각자 다른 관점에서 이상 탐지 → 더 안정적
"""

# configs/model_config.yaml에 설정
anomaly_transformer:
  seq_len: 4320        # 72시간 (분 단위)
  d_model: 512
  n_heads: 8
  e_layers: 3
  d_ff: 512
  dropout: 0.0
  activation: gelu
  output_attention: true
  k: 3                 # Association Discrepancy top-k
```

---

### 3단계 — 앙상블 (6개월 후, 자체 데이터 충분 시)

```python
def ensemble_predict(ce_series: pd.Series, features: dict) -> dict:
    """
    3개 모델 앙상블
    - 데이터 초기: Chronos/MOIRAI 위주
    - 데이터 쌓이면: XGBoost 비중 증가
    """
    # 각 모델 스코어 (0~1)
    score_chronos  = chronos_score(ce_series)        # Zero-shot
    score_moirai   = moirai_score(ce_series)          # Zero-shot
    score_xgb      = xgb_model.predict_proba(        # 파인튜닝
                         features.values.reshape(1,-1))[0][1]
    score_anomaly_t= anomaly_transformer_score(ce_series)

    # 가중치 (데이터 양에 따라 동적 조정)
    w_zero_shot = max(0.4, 1.0 - tta_data_weight)  # 초기엔 높음
    w_xgb       = min(0.4, tta_data_weight)          # 데이터 쌓이면 높아짐
    w_anomaly_t  = 0.2

    final_score = (
        w_zero_shot * 0.5 * (score_chronos + score_moirai) +
        w_xgb       * score_xgb +
        w_anomaly_t * score_anomaly_t
    )

    return {
        "failure_probability": final_score,
        "scores": {
            "chronos":           score_chronos,
            "moirai":            score_moirai,
            "xgboost":           score_xgb,
            "anomaly_transformer": score_anomaly_t,
        }
    }
```

---

## MLflow 실험 관리

```python
import mlflow

mlflow.set_tracking_uri("http://localhost:5000")
mlflow.set_experiment("memory_failure_pred")

# 각 모델별 실험 추적
with mlflow.start_run(run_name="chronos_zero_shot"):
    mlflow.log_param("model", "amazon/chronos-t5-small")
    mlflow.log_param("prediction_length", 1440)
    mlflow.log_metrics({
        "auc_pr": 0.82,
        "recall": 0.88,
        "f1":     0.79,
    })

with mlflow.start_run(run_name="xgboost_finetuned"):
    mlflow.log_param("pretrain_data", "alibaba_pakdd2021")
    mlflow.log_param("finetune_weight", 3.0)
    mlflow.xgboost.log_model(model, "xgboost",
        registered_model_name="xgb_finetuned")
```

---

## 자동 재학습 스케줄

```
매일 새벽 2시:
  Chronos / MOIRAI: 재학습 불필요 (Zero-shot)
  XGBoost: TTA 신규 데이터로 파인튜닝 재실행 (cuda:1)
  성능 비교 → 개선 시 Production 교체

월 1회:
  Anomaly Transformer 전체 재학습 (cuda:1)
  앙상블 가중치 재최적화 (Optuna)
```

---

## 평가지표

```
✅ 사용:  F1, AUC-PR, Recall
❌ 금지:  Accuracy

목표:
  1단계 Zero-shot:  F1 > 0.65, Recall > 0.75 (레이블 없어도 가능)
  2단계 파인튜닝:   F1 > 0.75, AUC-PR > 0.80
  3단계 앙상블:     F1 > 0.82, AUC-PR > 0.85, Recall > 0.88
```
