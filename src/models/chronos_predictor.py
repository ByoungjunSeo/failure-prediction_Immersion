"""Chronos 기반 CE 이상탐지 예측기.

Amazon Chronos (T5 기반 시계열 예측 모델)을 사용하여
CE 72시간 시계열로 향후 24시간을 예측하고,
예측값 급증 여부로 장애 위험도를 판단한다.

Zero-shot — 레이블 불필요, 즉시 가동 가능.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd
import torch

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "amazon/chronos-t5-small"
PREDICTION_LENGTH = 1440  # 24시간 (분 단위)
NUM_SAMPLES = 20
CONTEXT_LENGTH = 4320  # 72시간 (분 단위)


class ChronosPredictor:
    """Chronos Zero-shot CE 이상탐지 예측기.

    Args:
        model_name: HuggingFace 모델 이름.
        device: CUDA 디바이스 (기본: cuda:0).
        prediction_length: 예측 길이 (분).
        num_samples: 예측 샘플 수.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        device: str = "cuda:0",
        prediction_length: int = PREDICTION_LENGTH,
        num_samples: int = NUM_SAMPLES,
    ):
        self._model_name = model_name
        self._device = device
        self._prediction_length = prediction_length
        self._num_samples = num_samples
        self._pipeline = None

    def load(self) -> None:
        """모델을 GPU에 로드한다.

        Raises:
            RuntimeError: 모델 로드 실패.
        """
        from chronos import ChronosPipeline

        try:
            self._pipeline = ChronosPipeline.from_pretrained(
                self._model_name,
                device_map=self._device,
                dtype=torch.float32,
            )
            logger.info(
                "Chronos 로드 완료: %s → %s", self._model_name, self._device
            )
        except Exception:
            logger.exception("Chronos 로드 실패: %s", self._model_name)
            raise

    def predict_ce_anomaly(self, ce_series: pd.Series) -> dict:
        """CE 시계열로 장애 위험도를 예측한다.

        Args:
            ce_series: 분 단위 CE 카운트 시계열 (최대 72시간).

        Returns:
            {
                "anomaly_score": float,    # 이상 스코어 (높을수록 위험)
                "predicted_peak": float,   # 예측된 CE 최대값
                "risk_level": str,         # NORMAL / WARNING / CRITICAL
                "model": "chronos",
            }

        Raises:
            RuntimeError: 모델 미로드 상태.
        """
        if self._pipeline is None:
            raise RuntimeError("Chronos 모델이 로드되지 않음. load()를 먼저 호출하세요.")

        # 컨텍스트 길이 제한
        context = ce_series[-CONTEXT_LENGTH:].values.astype(np.float32)
        context_tensor = torch.tensor(context).unsqueeze(0)

        try:
            forecast = self._pipeline.predict(
                inputs=context_tensor,
                prediction_length=self._prediction_length,
                num_samples=self._num_samples,
                limit_prediction_length=False,
            )
        except Exception:
            logger.exception("Chronos 추론 실패")
            return self._default_result()

        # forecast shape: (1, num_samples, prediction_length)
        samples = forecast[0]  # (num_samples, prediction_length)
        median_forecast = samples.median(dim=0)[0].cpu().numpy()
        predicted_peak = float(median_forecast.max())

        # 최근 1시간 평균 대비 예측 피크
        recent_mean = float(ce_series[-60:].mean()) if len(ce_series) >= 60 else float(ce_series.mean())
        anomaly_score = predicted_peak / (recent_mean + 1e-9)

        # 예측 불확실성 (표준편차)
        forecast_std = float(samples.std(dim=0).mean().item())

        # 복합 스코어: 피크 비율 + 불확실성
        combined_score = min(1.0, (anomaly_score / 20.0) + (forecast_std / 10.0))

        risk_level = self._classify_risk(combined_score)

        logger.debug(
            "Chronos 예측: peak=%.2f, score=%.4f, risk=%s",
            predicted_peak, combined_score, risk_level,
        )

        return {
            "anomaly_score": combined_score,
            "predicted_peak": predicted_peak,
            "forecast_std": forecast_std,
            "risk_level": risk_level,
            "model": "chronos",
        }

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
            "predicted_peak": 0.0,
            "forecast_std": 0.0,
            "risk_level": "NORMAL",
            "model": "chronos",
        }
