"""피처 파이프라인 단위 테스트.

VictoriaMetrics mock 기반으로 45개 피처 계산을 검증한다.
"""

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.features.feature_pipeline import FeaturePipeline


# === CE 피처 계산 테스트 ===


class TestComputeCeFeatures:
    """Category A — CE 시계열 피처 20개 테스트."""

    def _make_pipeline(self) -> FeaturePipeline:
        return FeaturePipeline(vm_base_url="http://mock:8428")

    def test_feature_count(self) -> None:
        """CE 피처가 정확히 20개인지 확인."""
        pipeline = self._make_pipeline()
        ce = pd.Series(np.random.poisson(2, 4320), dtype=np.float64)
        features = pipeline.compute_ce_features(ce)
        assert len(features) == 20

    def test_zero_series(self) -> None:
        """0 시계열에서 모든 피처가 0 또는 기본값인지 확인."""
        pipeline = self._make_pipeline()
        ce = pd.Series(np.zeros(4320), dtype=np.float64)
        features = pipeline.compute_ce_features(ce)

        assert features["ce_count_1h"] == 0.0
        assert features["ce_count_24h"] == 0.0
        assert features["ce_count_72h"] == 0.0
        assert features["ce_slope_1h"] == 0.0
        assert features["ce_burst_ratio"] == 0.0
        assert features["ce_interval_mean"] == 99999.0

    def test_burst_detection(self) -> None:
        """CE 급증이 감지되는지 확인."""
        pipeline = self._make_pipeline()
        # 72시간 중 마지막 1시간에만 높은 CE
        ce = pd.Series(np.zeros(4320), dtype=np.float64)
        ce.iloc[-60:] = 100.0  # 마지막 1시간 급증
        ce.iloc[:4260] = 0.1   # 이전에는 낮은 값

        features = pipeline.compute_ce_features(ce)
        assert features["ce_burst_ratio"] > 10.0
        assert features["ce_burst_flag"] == 1.0

    def test_slope_positive_for_increasing(self) -> None:
        """CE가 증가하는 시계열의 기울기가 양수인지 확인."""
        pipeline = self._make_pipeline()
        ce = pd.Series(np.arange(4320, dtype=np.float64))
        features = pipeline.compute_ce_features(ce)

        assert features["ce_slope_1h"] > 0
        assert features["ce_slope_24h"] > 0

    def test_interval_decreasing(self) -> None:
        """CE 이벤트 간격이 줄어들면 interval_slope이 음수인지 확인."""
        pipeline = self._make_pipeline()
        ce = pd.Series(np.zeros(4320), dtype=np.float64)
        # 간격이 점점 좁아지는 이벤트
        positions = [100, 200, 280, 340, 380, 400, 410, 415, 418, 420]
        for p in positions:
            ce.iloc[p] = 5.0

        features = pipeline.compute_ce_features(ce)
        assert features["ce_interval_slope"] < 0  # 간격 단축 = 위험

    def test_short_series(self) -> None:
        """짧은 시계열에서도 에러 없이 계산되는지 확인."""
        pipeline = self._make_pipeline()
        ce = pd.Series([1.0, 2.0, 3.0], dtype=np.float64)
        features = pipeline.compute_ce_features(ce)
        assert len(features) == 20
        assert features["ce_count_72h"] == 6.0


# === HW 피처 테스트 ===


class TestComputeHwFeatures:
    """Category B — 하드웨어 환경 피처 10개 테스트."""

    @patch.object(FeaturePipeline, "_fetch_metric", return_value=pd.Series(dtype=np.float64))
    def test_feature_count_default(self, mock_fetch: MagicMock) -> None:
        """VictoriaMetrics 응답 없을 때 기본값 10개 반환."""
        pipeline = FeaturePipeline(vm_base_url="http://mock:8428")
        features = pipeline.compute_hw_features("test_server")
        assert len(features) == 10

    @patch.object(FeaturePipeline, "_fetch_metric")
    def test_temp_features(self, mock_fetch: MagicMock) -> None:
        """온도 시계열에서 평균/기울기 계산."""
        temp_data = pd.Series(np.linspace(60, 80, 360), dtype=np.float64)
        mock_fetch.return_value = temp_data

        pipeline = FeaturePipeline(vm_base_url="http://mock:8428")
        features = pipeline.compute_hw_features("test_server")

        assert features["cpu_temp_mean_1h"] > 0
        assert features["cpu_temp_slope_6h"] > 0  # 온도 상승


# === 워크로드 피처 테스트 ===


class TestComputeWorkloadFeatures:
    """Category C — 워크로드 피처 10개 테스트."""

    @patch.object(FeaturePipeline, "_fetch_metric", return_value=pd.Series(dtype=np.float64))
    def test_feature_count_default(self, mock_fetch: MagicMock) -> None:
        """기본값 10개 반환."""
        pipeline = FeaturePipeline(vm_base_url="http://mock:8428")
        features = pipeline.compute_workload_features("test_server")
        assert len(features) == 10


# === ESXi 피처 테스트 ===


class TestComputeEsxiFeatures:
    """Category D — ESXi 호스트 피처 5개 테스트."""

    @patch.object(FeaturePipeline, "_fetch_metric", return_value=pd.Series(dtype=np.float64))
    def test_feature_count_default(self, mock_fetch: MagicMock) -> None:
        """기본값 5개 반환."""
        pipeline = FeaturePipeline(vm_base_url="http://mock:8428")
        features = pipeline.compute_esxi_features("vmgnode18")
        assert len(features) == 5


# === 전체 파이프라인 테스트 ===


class TestBuildFeatureVector:
    """build_feature_vector 통합 테스트."""

    @patch.object(FeaturePipeline, "_fetch_metric", return_value=pd.Series(dtype=np.float64))
    @patch.object(FeaturePipeline, "fetch_ce_series")
    def test_45_features(self, mock_ce: MagicMock, mock_metric: MagicMock) -> None:
        """총 45개 피처가 생성되는지 확인."""
        mock_ce.return_value = pd.Series(np.random.poisson(1, 4320), dtype=np.float64)

        pipeline = FeaturePipeline(vm_base_url="http://mock:8428")
        features = pipeline.build_feature_vector("test_server")

        assert len(features) == 45

    @patch.object(FeaturePipeline, "_fetch_metric", return_value=pd.Series(dtype=np.float64))
    @patch.object(FeaturePipeline, "fetch_ce_series")
    def test_no_missing_values(self, mock_ce: MagicMock, mock_metric: MagicMock) -> None:
        """결측값이 없는지 확인."""
        mock_ce.return_value = pd.Series(np.random.poisson(1, 4320), dtype=np.float64)

        pipeline = FeaturePipeline(vm_base_url="http://mock:8428")
        features = pipeline.build_feature_vector("test_server")

        for name, val in features.items():
            assert val is not None, f"결측값: {name}"
            assert not np.isnan(val), f"NaN: {name}"

    @patch.object(FeaturePipeline, "_fetch_metric", return_value=pd.Series(dtype=np.float64))
    @patch.object(FeaturePipeline, "fetch_ce_series")
    def test_computation_under_5s(self, mock_ce: MagicMock, mock_metric: MagicMock) -> None:
        """45개 피처 계산이 5초 이내에 완료되는지 확인."""
        mock_ce.return_value = pd.Series(np.random.poisson(1, 4320), dtype=np.float64)

        pipeline = FeaturePipeline(vm_base_url="http://mock:8428")

        start = time.time()
        pipeline.build_feature_vector("test_server")
        elapsed = time.time() - start

        assert elapsed < 5.0, f"피처 계산 {elapsed:.1f}초 초과"
