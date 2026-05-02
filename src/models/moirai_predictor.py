"""MOIRAI 기반 Zero-shot 이상탐지 예측기.

Salesforce MOIRAI를 사용하여 CE 시계열의
이상 구간을 탐지한다. 예측 불확실성(표준편차)을
이상 스코어로 사용한다.

Zero-shot — 레이블 불필요, 즉시 가동 가능.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd
import torch

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "Salesforce/moirai-1.0-R-small"
PREDICTION_LENGTH = 1440  # 24시간
CONTEXT_LENGTH = 4320  # 72시간
NUM_SAMPLES = 20


class MoiraiPredictor:
    """MOIRAI Zero-shot 이상탐지 예측기.

    Args:
        model_name: HuggingFace 모델 이름.
        device: CUDA 디바이스.
        prediction_length: 예측 길이 (분).
        context_length: 컨텍스트 길이 (분).
        num_samples: 예측 샘플 수.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        device: str = "cuda:0",
        prediction_length: int = PREDICTION_LENGTH,
        context_length: int = CONTEXT_LENGTH,
        num_samples: int = NUM_SAMPLES,
    ):
        self._model_name = model_name
        self._device = device
        self._prediction_length = prediction_length
        self._context_length = context_length
        self._num_samples = num_samples
        self._model = None

    def load(self) -> None:
        """모델을 GPU에 로드한다.

        Raises:
            RuntimeError: 모델 로드 실패.
        """
        try:
            from uni2ts.model.moirai import MoiraiForecast, MoiraiModule

            module = MoiraiModule.from_pretrained(self._model_name)

            self._model = MoiraiForecast(
                prediction_length=self._prediction_length,
                context_length=self._context_length,
                patch_size="auto",
                num_samples=self._num_samples,
                target_dim=1,
                feat_dynamic_real_dim=0,
                past_feat_dynamic_real_dim=0,
                module=module,
            ).to(self._device)

            logger.info("MOIRAI 로드 완료: %s → %s", self._model_name, self._device)
        except Exception:
            logger.exception("MOIRAI 로드 실패: %s", self._model_name)
            raise

    def detect_anomaly(self, ce_series: pd.Series) -> dict:
        """CE 시계열에서 이상을 탐지한다.

        Args:
            ce_series: 분 단위 CE 카운트 시계열.

        Returns:
            {
                "anomaly_score": float,
                "forecast_std": float,
                "risk_level": str,
                "model": "moirai",
            }

        Raises:
            RuntimeError: 모델 미로드 상태.
        """
        if self._model is None:
            raise RuntimeError("MOIRAI 모델이 로드되지 않음. load()를 먼저 호출하세요.")

        try:
            score, std = self._run_inference(ce_series)
        except Exception:
            logger.exception("MOIRAI 추론 실패")
            return self._default_result()

        risk_level = self._classify_risk(score)

        logger.debug(
            "MOIRAI 탐지: score=%.4f, std=%.4f, risk=%s",
            score, std, risk_level,
        )

        return {
            "anomaly_score": score,
            "forecast_std": std,
            "risk_level": risk_level,
            "model": "moirai",
        }

    def _run_inference(self, ce_series: pd.Series) -> tuple[float, float]:
        """MOIRAI 추론을 실행한다.

        Args:
            ce_series: CE 시계열.

        Returns:
            (anomaly_score, forecast_std) 튜플.
        """
        from gluonts.dataset.pandas import PandasDataset

        # 시계열 데이터 준비
        context = ce_series[-self._context_length:].copy()
        context.index = pd.date_range(
            end=pd.Timestamp.now(), periods=len(context), freq="min"
        )
        context.name = "target"

        ds = PandasDataset({"target": context})
        predictor = self._model.create_predictor(batch_size=1)
        forecasts = list(predictor.predict(ds))

        if not forecasts:
            return 0.0, 0.0

        # 예측 불확실성을 이상 스코어로 사용
        samples = forecasts[0].samples  # shape: (num_samples, prediction_length)
        forecast_std = float(samples.std(axis=0).mean())
        forecast_mean = float(np.abs(samples.mean(axis=0)).mean())

        # 현재 수준 대비 예측 편차
        current_mean = float(ce_series[-60:].mean()) if len(ce_series) >= 60 else float(ce_series.mean())
        deviation = forecast_mean / (current_mean + 1e-9)

        # 복합 스코어: 불확실성 + 편차
        score = min(1.0, (forecast_std / 10.0) + (deviation / 20.0))

        return score, forecast_std

    @staticmethod
    def _classify_risk(score: float) -> str:
        """스코어를 리스크 레벨로 분류한다.

        Args:
            score: 0~1 범위의 이상 스코어.

        Returns:
            NORMAL / WARNING / CRITICAL.
        """
        if score >= 0.85:
            return "CRITICAL"
        elif score >= 0.65:
            return "WARNING"
        return "NORMAL"

    @staticmethod
    def _default_result() -> dict:
        """추론 실패 시 기본 결과를 반환한다."""
        return {
            "anomaly_score": 0.0,
            "forecast_std": 0.0,
            "risk_level": "NORMAL",
            "model": "moirai",
        }
