"""Anomaly Transformer 기반 이상탐지 예측기.

thuml/Anomaly-Transformer (ICLR 2022) 구조를 활용하여
CE 72시간 시계열에서 비지도 이상탐지를 수행한다.

Association Discrepancy 기반으로 이상 구간을 탐지한다.
레이블 불필요 — 비지도 학습.

학습: cuda:1 (파인튜닝 전담 GPU)
추론: cuda:0
"""

import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

logger = logging.getLogger(__name__)

# vendor 경로 추가
sys.path.insert(0, "/opt/failure_prediction/vendor/Anomaly-Transformer")

DEFAULT_MODEL_PATH = Path("/opt/failure_prediction/models/checkpoints/anomaly_transformer.pt")

# 기본 설정 (docs/04_model.md 참조)
DEFAULT_CONFIG = {
    "win_size": 100,       # 윈도우 크기 (추론 속도와 균형)
    "enc_in": 1,           # CE 단일 채널
    "c_out": 1,
    "d_model": 512,
    "n_heads": 8,
    "e_layers": 3,
    "d_ff": 512,
    "dropout": 0.0,
    "activation": "gelu",
    "k": 3,                # Association Discrepancy top-k
    "lr": 1e-4,
    "epochs": 10,
    "batch_size": 32,
}


class CETimeSeriesDataset(Dataset):
    """CE 시계열 슬라이딩 윈도우 데이터셋.

    Args:
        data: CE 시계열 numpy 배열.
        win_size: 윈도우 크기.
    """

    def __init__(self, data: np.ndarray, win_size: int = 100):
        self._data = data.astype(np.float32)
        self._win_size = win_size

    def __len__(self) -> int:
        return max(0, len(self._data) - self._win_size + 1)

    def __getitem__(self, idx: int) -> torch.Tensor:
        window = self._data[idx: idx + self._win_size]
        return torch.tensor(window).unsqueeze(-1)  # (win_size, 1)


class AnomalyTransformerPredictor:
    """Anomaly Transformer 이상탐지 예측기.

    Args:
        config: 모델 설정 딕셔너리.
        model_path: 모델 저장/로드 경로.
        train_device: 학습 디바이스.
        infer_device: 추론 디바이스.
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        model_path: Path = DEFAULT_MODEL_PATH,
        train_device: str = "cuda:1",
        infer_device: str = "cuda:0",
    ):
        self._config = config or DEFAULT_CONFIG.copy()
        self._model_path = model_path
        self._train_device = train_device
        self._infer_device = infer_device
        self._model: Optional[nn.Module] = None
        self._threshold: float = 0.5

    def _build_model(self) -> nn.Module:
        """Anomaly Transformer 모델을 생성한다.

        Returns:
            AnomalyTransformer 모듈.
        """
        from model.AnomalyTransformer import AnomalyTransformer

        return AnomalyTransformer(
            win_size=self._config["win_size"],
            enc_in=self._config["enc_in"],
            c_out=self._config["c_out"],
            d_model=self._config["d_model"],
            n_heads=self._config["n_heads"],
            e_layers=self._config["e_layers"],
            d_ff=self._config["d_ff"],
            dropout=self._config["dropout"],
            activation=self._config["activation"],
            output_attention=True,
        )

    def train(self, ce_data: np.ndarray) -> dict[str, float]:
        """비지도 학습을 수행한다.

        Args:
            ce_data: CE 시계열 numpy 배열 (전체 학습 데이터).

        Returns:
            {train_loss, threshold} 딕셔너리.
        """
        self._model = self._build_model().to(self._train_device)

        dataset = CETimeSeriesDataset(ce_data, self._config["win_size"])
        dataloader = DataLoader(
            dataset,
            batch_size=self._config["batch_size"],
            shuffle=True,
            drop_last=True,
        )

        optimizer = torch.optim.Adam(
            self._model.parameters(), lr=self._config["lr"]
        )
        criterion = nn.MSELoss()

        self._model.train()
        total_loss = 0.0
        k = self._config["k"]
        win_size = self._config["win_size"]

        for epoch in range(self._config["epochs"]):
            epoch_loss = 0.0
            for batch in dataloader:
                batch = batch.to(self._train_device)
                optimizer.zero_grad()

                output, series, prior, _ = self._model(batch)

                # Association discrepancy (원본 solver.py 방식)
                series_loss = 0.0
                prior_loss = 0.0
                for u in range(len(prior)):
                    p_norm = prior[u] / (torch.sum(prior[u], dim=-1, keepdim=True) + 1e-9)
                    series_loss += (
                        torch.mean(self._my_kl_loss(series[u], p_norm.detach()))
                        + torch.mean(self._my_kl_loss(p_norm.detach(), series[u]))
                    )
                    prior_loss += (
                        torch.mean(self._my_kl_loss(p_norm, series[u].detach()))
                        + torch.mean(self._my_kl_loss(series[u].detach(), p_norm))
                    )
                series_loss /= len(prior)
                prior_loss /= len(prior)

                rec_loss = criterion(output, batch)

                # Minimax strategy
                loss1 = rec_loss - k * series_loss
                loss2 = rec_loss + k * prior_loss

                loss1.backward(retain_graph=True)
                loss2.backward()
                optimizer.step()

                epoch_loss += loss1.item()

            avg_loss = epoch_loss / max(len(dataloader), 1)
            total_loss = avg_loss
            logger.info("Epoch %d/%d, Loss: %.6f", epoch + 1, self._config["epochs"], avg_loss)

        # 학습 데이터에서 이상 스코어 임계값 설정
        self._threshold = self._compute_threshold(ce_data)

        logger.info(
            "Anomaly Transformer 학습 완료: loss=%.6f, threshold=%.6f",
            total_loss, self._threshold,
        )

        return {"train_loss": total_loss, "threshold": self._threshold}

    def detect_anomaly(self, ce_series: pd.Series) -> dict:
        """CE 시계열에서 이상을 탐지한다.

        Args:
            ce_series: CE 시계열.

        Returns:
            {anomaly_score, risk_level, model} 딕셔너리.

        Raises:
            RuntimeError: 모델 미학습 상태.
        """
        if self._model is None:
            raise RuntimeError("Anomaly Transformer 모델이 학습되지 않음")

        try:
            scores = self._compute_anomaly_scores(ce_series.values)
            avg_score = float(np.mean(scores)) if len(scores) > 0 else 0.0
            max_score = float(np.max(scores)) if len(scores) > 0 else 0.0

            # 정규화 (0~1)
            normalized_score = min(1.0, avg_score / (self._threshold + 1e-9))

            risk_level = self._classify_risk(normalized_score)

            logger.debug(
                "Anomaly Transformer: avg=%.4f, max=%.4f, norm=%.4f, risk=%s",
                avg_score, max_score, normalized_score, risk_level,
            )

            return {
                "anomaly_score": normalized_score,
                "raw_score_avg": avg_score,
                "raw_score_max": max_score,
                "risk_level": risk_level,
                "model": "anomaly_transformer",
            }
        except Exception:
            logger.exception("Anomaly Transformer 추론 실패")
            return {
                "anomaly_score": 0.0,
                "raw_score_avg": 0.0,
                "raw_score_max": 0.0,
                "risk_level": "NORMAL",
                "model": "anomaly_transformer",
            }

    @staticmethod
    def _my_kl_loss(p: torch.Tensor, q: torch.Tensor) -> torch.Tensor:
        """KL divergence (원본 solver.py 방식).

        Args:
            p: 확률 분포 텐서.
            q: 확률 분포 텐서.

        Returns:
            KL divergence (batch별).
        """
        res = p * (torch.log(p + 0.0001) - torch.log(q + 0.0001))
        return torch.mean(torch.sum(res, dim=-1), dim=1)

    def _compute_anomaly_scores(self, data: np.ndarray) -> np.ndarray:
        """각 윈도우의 이상 스코어를 계산한다.

        Args:
            data: CE 시계열 numpy 배열.

        Returns:
            이상 스코어 배열.
        """
        self._model.eval()
        self._model.to(self._infer_device)

        dataset = CETimeSeriesDataset(data, self._config["win_size"])
        if len(dataset) == 0:
            return np.array([0.0])

        dataloader = DataLoader(dataset, batch_size=self._config["batch_size"], shuffle=False)
        scores = []

        with torch.no_grad():
            for batch in dataloader:
                batch = batch.to(self._infer_device)
                output, series, prior, _ = self._model(batch)

                # Reconstruction error (per sample)
                rec_error = torch.mean((output - batch) ** 2, dim=(1, 2))

                # Association discrepancy (per sample)
                assoc_disc = torch.zeros(batch.size(0), device=self._infer_device)
                for u in range(len(prior)):
                    p_norm = prior[u] / (torch.sum(prior[u], dim=-1, keepdim=True) + 1e-9)
                    # KL(series || prior_norm) — per sample
                    kl = self._my_kl_loss(series[u], p_norm)
                    assoc_disc += kl.mean(dim=-1)  # average over heads
                assoc_disc /= len(prior)

                # 결합 스코어
                combined = rec_error * (1 + assoc_disc)
                scores.append(combined.cpu().numpy())

        return np.concatenate(scores)

    def _compute_threshold(self, data: np.ndarray) -> float:
        """학습 데이터에서 이상 스코어 임계값을 설정한다.

        Args:
            data: 학습 CE 시계열.

        Returns:
            임계값 (상위 5% 백분위수).
        """
        scores = self._compute_anomaly_scores(data)
        if len(scores) == 0:
            return 1.0
        return float(np.percentile(scores, 95))

    def save(self, path: Optional[Path] = None) -> None:
        """모델을 저장한다.

        Args:
            path: 저장 경로.
        """
        save_path = path or self._model_path
        save_path.parent.mkdir(parents=True, exist_ok=True)

        if self._model is not None:
            torch.save(
                {
                    "model_state_dict": self._model.state_dict(),
                    "config": self._config,
                    "threshold": self._threshold,
                },
                save_path,
            )
            logger.info("Anomaly Transformer 저장: %s", save_path)

    def load(self, path: Optional[Path] = None) -> None:
        """모델을 로드한다.

        Args:
            path: 로드 경로.
        """
        load_path = path or self._model_path
        if not load_path.exists():
            raise FileNotFoundError(f"모델 파일 없음: {load_path}")

        checkpoint = torch.load(load_path, map_location=self._infer_device, weights_only=False)
        self._config = checkpoint["config"]
        self._threshold = checkpoint["threshold"]
        self._model = self._build_model()
        self._model.load_state_dict(checkpoint["model_state_dict"])
        self._model.to(self._infer_device)
        self._model.eval()

        logger.info("Anomaly Transformer 로드: %s (threshold=%.6f)", load_path, self._threshold)

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
