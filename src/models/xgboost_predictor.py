"""XGBoost 기반 장애 예측기.

Alibaba PAKDD 2021 데이터로 사전학습하고,
TTA 자체 데이터로 파인튜닝한다.

학습: cuda:1 (파인튜닝 전담 GPU)
추론: cuda:0 (FastAPI 서빙)

Optuna HPO 30 trials로 최적 하이퍼파라미터 탐색.
평가: F1, AUC-PR, Recall (Accuracy 금지).
"""

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import average_precision_score, f1_score, recall_score
from sklearn.model_selection import StratifiedKFold

logger = logging.getLogger(__name__)

DEFAULT_MODEL_PATH = Path("/opt/failure_prediction/models/checkpoints/xgboost_model.json")


class XGBoostPredictor:
    """XGBoost 장애 예측기.

    Args:
        model_path: 모델 저장/로드 경로.
        device: 학습 디바이스 (cuda:0 또는 cuda:1).
    """

    def __init__(
        self,
        model_path: Path = DEFAULT_MODEL_PATH,
        device: str = "cuda:1",
    ):
        self._model_path = model_path
        self._device = device
        self._model: Optional[xgb.XGBClassifier] = None
        self._best_params: dict = {}

    def train(
        self,
        x_train: pd.DataFrame,
        y_train: pd.Series,
        x_val: Optional[pd.DataFrame] = None,
        y_val: Optional[pd.Series] = None,
    ) -> dict[str, float]:
        """모델을 학습한다.

        Args:
            x_train: 학습 피처.
            y_train: 학습 레이블.
            x_val: 검증 피처 (없으면 학습 데이터에서 분리).
            y_val: 검증 레이블.

        Returns:
            {f1, auc_pr, recall} 평가지표 딕셔너리.
        """
        scale_pos = float((y_train == 0).sum()) / max(float((y_train == 1).sum()), 1)

        params = self._best_params or {
            "max_depth": 6,
            "learning_rate": 0.01,
            "n_estimators": 1000,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
        }

        self._model = xgb.XGBClassifier(
            device=self._device,
            tree_method="hist",
            scale_pos_weight=scale_pos,
            eval_metric="aucpr",
            early_stopping_rounds=50,
            **params,
        )

        if x_val is not None and y_val is not None:
            self._model.fit(
                x_train, y_train,
                eval_set=[(x_val, y_val)],
                verbose=False,
            )
        else:
            self._model.fit(x_train, y_train, verbose=False)

        # 평가
        eval_x = x_val if x_val is not None else x_train
        eval_y = y_val if y_val is not None else y_train

        metrics = self._evaluate(eval_x, eval_y)
        logger.info(
            "XGBoost 학습 완료: F1=%.4f, AUC-PR=%.4f, Recall=%.4f",
            metrics["f1"], metrics["auc_pr"], metrics["recall"],
        )

        return metrics

    def finetune(
        self,
        public_x: pd.DataFrame,
        public_y: pd.Series,
        tta_x: pd.DataFrame,
        tta_y: pd.Series,
        tta_weight: float = 3.0,
    ) -> dict[str, float]:
        """Alibaba 데이터 + TTA 데이터로 파인튜닝한다.

        Args:
            public_x: Alibaba 학습 피처.
            public_y: Alibaba 학습 레이블.
            tta_x: TTA 자체 피처.
            tta_y: TTA 자체 레이블.
            tta_weight: TTA 데이터 가중치 (기본 3.0).

        Returns:
            평가지표 딕셔너리.
        """
        x = pd.concat([public_x, tta_x], ignore_index=True)
        y = pd.concat([public_y, tta_y], ignore_index=True)

        weights = np.concatenate([
            np.ones(len(public_x)),
            np.ones(len(tta_x)) * tta_weight,
        ])

        scale_pos = float((y == 0).sum()) / max(float((y == 1).sum()), 1)

        params = self._best_params or {
            "max_depth": 6,
            "learning_rate": 0.01,
            "n_estimators": 1000,
            "subsample": 0.8,
            "colsample_bytree": 0.8,
        }

        self._model = xgb.XGBClassifier(
            device=self._device,
            tree_method="hist",
            scale_pos_weight=scale_pos,
            eval_metric="aucpr",
            early_stopping_rounds=50,
            **params,
        )

        self._model.fit(
            x, y,
            sample_weight=weights,
            eval_set=[(tta_x, tta_y)],
            verbose=False,
        )

        metrics = self._evaluate(tta_x, tta_y)
        logger.info(
            "XGBoost 파인튜닝 완료: F1=%.4f, AUC-PR=%.4f, Recall=%.4f",
            metrics["f1"], metrics["auc_pr"], metrics["recall"],
        )

        return metrics

    def optimize_hyperparams(
        self,
        x: pd.DataFrame,
        y: pd.Series,
        n_trials: int = 30,
    ) -> dict:
        """Optuna로 하이퍼파라미터를 최적화한다.

        Args:
            x: 피처 데이터.
            y: 레이블 데이터.
            n_trials: HPO 시도 횟수.

        Returns:
            최적 하이퍼파라미터 딕셔너리.
        """
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)

        def objective(trial: optuna.Trial) -> float:
            params = {
                "max_depth": trial.suggest_int("max_depth", 3, 10),
                "learning_rate": trial.suggest_float("learning_rate", 0.005, 0.3, log=True),
                "n_estimators": trial.suggest_int("n_estimators", 100, 2000),
                "subsample": trial.suggest_float("subsample", 0.5, 1.0),
                "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 1.0),
                "min_child_weight": trial.suggest_int("min_child_weight", 1, 10),
                "gamma": trial.suggest_float("gamma", 0.0, 5.0),
                "reg_alpha": trial.suggest_float("reg_alpha", 1e-8, 10.0, log=True),
                "reg_lambda": trial.suggest_float("reg_lambda", 1e-8, 10.0, log=True),
            }

            scale_pos = float((y == 0).sum()) / max(float((y == 1).sum()), 1)
            skf = StratifiedKFold(n_splits=3, shuffle=True, random_state=42)
            f1_scores = []

            for train_idx, val_idx in skf.split(x, y):
                model = xgb.XGBClassifier(
                    device=self._device,
                    tree_method="hist",
                    scale_pos_weight=scale_pos,
                    eval_metric="aucpr",
                    early_stopping_rounds=30,
                    **params,
                )
                model.fit(
                    x.iloc[train_idx], y.iloc[train_idx],
                    eval_set=[(x.iloc[val_idx], y.iloc[val_idx])],
                    verbose=False,
                )
                preds = model.predict(x.iloc[val_idx])
                f1_scores.append(f1_score(y.iloc[val_idx], preds))

            return float(np.mean(f1_scores))

        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=n_trials)

        self._best_params = study.best_params
        logger.info("HPO 완료: best F1=%.4f, params=%s", study.best_value, study.best_params)

        return study.best_params

    def predict(self, features: dict[str, float]) -> float:
        """단일 피처 벡터로 장애 확률을 예측한다.

        Args:
            features: 45개 피처 딕셔너리.

        Returns:
            장애 확률 (0~1).

        Raises:
            RuntimeError: 모델 미학습 상태.
        """
        if self._model is None:
            raise RuntimeError("XGBoost 모델이 학습되지 않음")

        x = pd.DataFrame([features])
        proba = self._model.predict_proba(x)[0][1]
        return float(proba)

    def predict_batch(self, x: pd.DataFrame) -> np.ndarray:
        """배치 예측을 수행한다.

        Args:
            x: 피처 DataFrame.

        Returns:
            장애 확률 배열.

        Raises:
            RuntimeError: 모델 미학습 상태.
        """
        if self._model is None:
            raise RuntimeError("XGBoost 모델이 학습되지 않음")

        return self._model.predict_proba(x)[:, 1]

    def save(self, path: Optional[Path] = None) -> None:
        """모델을 저장한다.

        Args:
            path: 저장 경로 (기본: self._model_path).
        """
        save_path = path or self._model_path
        save_path.parent.mkdir(parents=True, exist_ok=True)

        if self._model is not None:
            self._model.save_model(str(save_path))
            logger.info("XGBoost 모델 저장: %s", save_path)

    def load(self, path: Optional[Path] = None) -> None:
        """모델을 로드한다.

        Args:
            path: 로드 경로 (기본: self._model_path).
        """
        load_path = path or self._model_path
        if not load_path.exists():
            raise FileNotFoundError(f"모델 파일 없음: {load_path}")

        self._model = xgb.XGBClassifier()
        self._model.load_model(str(load_path))
        logger.info("XGBoost 모델 로드: %s", load_path)

    def _evaluate(self, x: pd.DataFrame, y: pd.Series) -> dict[str, float]:
        """모델을 평가한다.

        Args:
            x: 피처 데이터.
            y: 실제 레이블.

        Returns:
            {f1, auc_pr, recall} 딕셔너리.
        """
        preds = self._model.predict(x)
        proba = self._model.predict_proba(x)[:, 1]

        return {
            "f1": float(f1_score(y, preds)),
            "auc_pr": float(average_precision_score(y, proba)),
            "recall": float(recall_score(y, preds)),
        }
