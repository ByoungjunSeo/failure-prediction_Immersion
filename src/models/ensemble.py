"""앙상블 예측 모듈.

다수 모델의 예측 결과를 가중 합산하여
최종 장애 확률과 리스크 레벨을 산출한다.

가중치 전략:
  초기 (Phase 3): Chronos 0.5 + MOIRAI 0.5
  파인튜닝 후 (Phase 4): Chronos 0.25 + MOIRAI 0.15 + XGBoost 0.35 + AnomalyT 0.25
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.models.anomaly_transformer import AnomalyTransformerPredictor
from src.models.chronos_predictor import ChronosPredictor
from src.models.moirai_predictor import MoiraiPredictor
from src.models.xgboost_predictor import XGBoostPredictor

logger = logging.getLogger(__name__)

# 리스크 레벨 기준 (CLAUDE.md 참조)
CRITICAL_THRESHOLD = 0.85
WARNING_THRESHOLD = 0.65
RECOVERY_THRESHOLD = 0.30


@dataclass
class EnsembleResult:
    """앙상블 예측 결과."""

    failure_probability: float = 0.0
    risk_level: str = "NORMAL"
    model_scores: dict[str, float] = field(default_factory=dict)
    model_details: dict[str, dict] = field(default_factory=dict)


class EnsemblePredictor:
    """앙상블 예측기.

    Args:
        weights: {모델명: 가중치} 딕셔너리.
        device: CUDA 디바이스 (추론용).
    """

    # 기본 가중치 (Phase 3: Zero-shot만)
    DEFAULT_WEIGHTS = {
        "chronos": 0.5,
        "moirai": 0.5,
    }

    # Phase 4 가중치 (파인튜닝 후)
    FINETUNED_WEIGHTS = {
        "chronos": 0.25,
        "moirai": 0.15,
        "xgboost": 0.35,
        "anomaly_transformer": 0.25,
    }

    def __init__(
        self,
        weights: Optional[dict[str, float]] = None,
        device: str = "cuda:0",
    ):
        self._weights = weights or self.DEFAULT_WEIGHTS.copy()
        self._device = device
        self._chronos: Optional[ChronosPredictor] = None
        self._moirai: Optional[MoiraiPredictor] = None
        self._xgboost: Optional[XGBoostPredictor] = None
        self._anomaly_transformer: Optional[AnomalyTransformerPredictor] = None

    def load_models(self) -> None:
        """모든 모델을 GPU에 로드한다."""
        if "chronos" in self._weights:
            self._chronos = ChronosPredictor(device=self._device)
            self._chronos.load()

        if "moirai" in self._weights:
            self._moirai = MoiraiPredictor(device=self._device)
            self._moirai.load()

        if "xgboost" in self._weights:
            self._xgboost = XGBoostPredictor(device=self._device)
            try:
                self._xgboost.load()
            except FileNotFoundError:
                logger.warning("XGBoost 모델 파일 없음, 학습 후 사용 가능")
                self._xgboost = None

        if "anomaly_transformer" in self._weights:
            self._anomaly_transformer = AnomalyTransformerPredictor(infer_device=self._device)
            try:
                self._anomaly_transformer.load()
            except FileNotFoundError:
                logger.warning("Anomaly Transformer 모델 파일 없음, 학습 후 사용 가능")
                self._anomaly_transformer = None

        logger.info("앙상블 모델 로드 완료: %s", list(self._weights.keys()))

    def predict(
        self,
        ce_series: pd.Series,
        features: Optional[dict[str, float]] = None,
    ) -> EnsembleResult:
        """앙상블 예측을 수행한다.

        Args:
            ce_series: 분 단위 CE 카운트 시계열.
            features: 45개 피처 벡터 (XGBoost용, Phase 4).

        Returns:
            EnsembleResult 객체.
        """
        result = EnsembleResult()
        weighted_sum = 0.0
        weight_total = 0.0

        # Chronos
        if self._chronos and "chronos" in self._weights:
            try:
                detail = self._chronos.predict_ce_anomaly(ce_series)
                score = detail["anomaly_score"]
                result.model_scores["chronos"] = score
                result.model_details["chronos"] = detail
                weighted_sum += self._weights["chronos"] * score
                weight_total += self._weights["chronos"]
            except Exception:
                logger.warning("Chronos 예측 실패, 앙상블에서 제외")

        # MOIRAI
        if self._moirai and "moirai" in self._weights:
            try:
                detail = self._moirai.detect_anomaly(ce_series)
                score = detail["anomaly_score"]
                result.model_scores["moirai"] = score
                result.model_details["moirai"] = detail
                weighted_sum += self._weights["moirai"] * score
                weight_total += self._weights["moirai"]
            except Exception:
                logger.warning("MOIRAI 예측 실패, 앙상블에서 제외")

        # XGBoost (Phase 4)
        if self._xgboost and "xgboost" in self._weights and features:
            try:
                score = self._predict_xgboost(features)
                result.model_scores["xgboost"] = score
                result.model_details["xgboost"] = {"anomaly_score": score, "model": "xgboost"}
                weighted_sum += self._weights["xgboost"] * score
                weight_total += self._weights["xgboost"]
            except Exception:
                logger.warning("XGBoost 예측 실패, 앙상블에서 제외")

        # Anomaly Transformer (Phase 4)
        if self._anomaly_transformer and "anomaly_transformer" in self._weights:
            try:
                score = self._predict_anomaly_transformer(ce_series)
                result.model_scores["anomaly_transformer"] = score
                result.model_details["anomaly_transformer"] = {
                    "anomaly_score": score, "model": "anomaly_transformer"
                }
                weighted_sum += self._weights["anomaly_transformer"] * score
                weight_total += self._weights["anomaly_transformer"]
            except Exception:
                logger.warning("Anomaly Transformer 예측 실패, 앙상블에서 제외")

        # 최종 확률 계산
        if weight_total > 0:
            result.failure_probability = weighted_sum / weight_total
        else:
            result.failure_probability = 0.0
            logger.error("모든 모델 예측 실패")

        result.risk_level = self._classify_risk(result.failure_probability)

        logger.info(
            "앙상블 예측: prob=%.4f, risk=%s, scores=%s",
            result.failure_probability,
            result.risk_level,
            result.model_scores,
        )

        return result

    def update_weights(self, new_weights: dict[str, float]) -> None:
        """앙상블 가중치를 업데이트한다.

        Args:
            new_weights: {모델명: 가중치} 딕셔너리.
        """
        total = sum(new_weights.values())
        if abs(total - 1.0) > 0.01:
            logger.warning("가중치 합이 1이 아님 (%.2f), 정규화 적용", total)
            new_weights = {k: v / total for k, v in new_weights.items()}

        self._weights = new_weights
        logger.info("앙상블 가중치 업데이트: %s", self._weights)

    def _predict_xgboost(self, features: dict[str, float]) -> float:
        """XGBoost 예측.

        Args:
            features: 45개 피처 벡터.

        Returns:
            장애 확률 (0~1).
        """
        return self._xgboost.predict(features)

    def _predict_anomaly_transformer(self, ce_series: pd.Series) -> float:
        """Anomaly Transformer 예측.

        Args:
            ce_series: CE 시계열.

        Returns:
            이상 스코어 (0~1).
        """
        result = self._anomaly_transformer.detect_anomaly(ce_series)
        return result["anomaly_score"]

    @staticmethod
    def _classify_risk(probability: float) -> str:
        """장애 확률을 리스크 레벨로 분류한다.

        Args:
            probability: 장애 확률 (0~1).

        Returns:
            NORMAL / WARNING / CRITICAL / RECOVERY.
        """
        if probability >= CRITICAL_THRESHOLD:
            return "CRITICAL"
        elif probability >= WARNING_THRESHOLD:
            return "WARNING"
        elif probability <= RECOVERY_THRESHOLD:
            return "RECOVERY"
        return "NORMAL"
