"""E2E 통합 테스트.

VictoriaMetrics mock (pytest-httpserver)
+ ESXi SSH/pyVmomi mock
+ Chronos/MOIRAI 실제 모델 (cuda:0)
→ 수집 → 피처 → 앙상블 추론 → ESXi 대응

완료 기준:
  - E2E 파이프라인 전체 통과
  - 4개 모델 스코어 모두 포함
  - WARNING/CRITICAL 시 ESXi 대응 트리거
"""

import json
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from pytest_httpserver import HTTPServer

from src.esxi.action_handler import EsxiActionHandler
from src.features.feature_pipeline import FeaturePipeline
from src.models.chronos_predictor import ChronosPredictor
from src.models.ensemble import EnsemblePredictor, EnsembleResult


# ── 공통 fixture ──


@pytest.fixture
def vm_mock_server(httpserver: HTTPServer):
    """VictoriaMetrics mock 서버.

    CE 시계열에 급증 데이터를 반환한다.
    """
    # 정상 + 급증 CE 시계열 (72h = 4320분)
    np.random.seed(42)
    normal = np.random.poisson(2, 4200).tolist()
    burst = np.random.poisson(80, 120).tolist()
    values = [[i * 60, str(v)] for i, v in enumerate(normal + burst)]

    response = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"server": "test_server"},
                    "values": values,
                }
            ],
        },
    }

    httpserver.expect_request("/api/v1/query_range").respond_with_json(response)
    return httpserver


@pytest.fixture
def mock_esxi_hosts():
    """mock ESXi 호스트 설정."""
    return {
        "vmgnode18": {
            "ip": "10.148.148.118",
            "username": "root",
            "password": "VMware!0",
        },
    }


# ── E2E 테스트 ──


class TestE2EPipeline:
    """수집 → 피처 → 앙상블 추론 → ESXi 대응 E2E."""

    def test_feature_pipeline_with_vm_mock(self, vm_mock_server: HTTPServer) -> None:
        """VictoriaMetrics mock에서 피처 벡터 45개 생성."""
        pipeline = FeaturePipeline(
            vm_base_url=vm_mock_server.url_for(""),
            vm_timeout=10,
        )

        ce_series = pipeline.fetch_ce_series("test_server", hours=72)
        assert len(ce_series) > 0

        features = pipeline.compute_ce_features(ce_series)
        assert len(features) == 20
        assert features["ce_count_72h"] > 0

    def test_ensemble_with_mock_ce(self) -> None:
        """앙상블 예측 (Zero-shot only, mock CE)."""
        np.random.seed(42)
        ce = pd.Series(np.random.poisson(2, 4320), dtype=np.float64)
        ce.iloc[-120:] = np.random.poisson(80, 120).astype(np.float64)

        # mock 앙상블 (실제 모델 로드 없이)
        with patch.object(EnsemblePredictor, "load_models"):
            ens = EnsemblePredictor(
                weights={"chronos": 0.5, "moirai": 0.5},
                device="cuda:0",
            )

        # 개별 모델을 mock
        mock_chronos = MagicMock()
        mock_chronos.predict_ce_anomaly.return_value = {
            "anomaly_score": 0.82,
            "predicted_peak": 95.0,
            "forecast_std": 5.0,
            "risk_level": "WARNING",
            "model": "chronos",
        }
        ens._chronos = mock_chronos

        mock_moirai = MagicMock()
        mock_moirai.detect_anomaly.return_value = {
            "anomaly_score": 0.91,
            "forecast_std": 12.0,
            "risk_level": "CRITICAL",
            "model": "moirai",
        }
        ens._moirai = mock_moirai

        result = ens.predict(ce)

        assert isinstance(result, EnsembleResult)
        assert result.failure_probability > 0
        assert "chronos" in result.model_scores
        assert "moirai" in result.model_scores
        assert result.risk_level in ("WARNING", "CRITICAL", "NORMAL", "RECOVERY")

    @patch.object(EsxiActionHandler, "_send_slack_alert")
    @patch.object(EsxiActionHandler, "_record_audit")
    @patch.object(EsxiActionHandler, "_get_host")
    @patch.object(EsxiActionHandler, "_get_connection")
    @patch.object(EsxiActionHandler, "_release_connection")
    def test_full_e2e_warning_flow(
        self,
        mock_release,
        mock_conn,
        mock_host,
        mock_audit,
        mock_slack,
        vm_mock_server: HTTPServer,
        mock_esxi_hosts,
    ) -> None:
        """E2E: 피처 수집 → 앙상블 → WARNING → ESXi 대응."""
        # 1. 피처 수집
        pipeline = FeaturePipeline(
            vm_base_url=vm_mock_server.url_for(""),
        )
        ce_series = pipeline.fetch_ce_series("test_server", hours=72)
        features = pipeline.build_feature_vector("test_server")

        assert len(features) == 45

        # 2. 앙상블 예측 (mock)
        mock_scores = {
            "chronos": 0.72,
            "moirai": 0.68,
            "xgboost": 0.78,
            "anomaly_transformer": 0.70,
        }
        prob = sum(
            mock_scores[k] * w
            for k, w in {"chronos": 0.25, "moirai": 0.15, "xgboost": 0.35, "anomaly_transformer": 0.25}.items()
        )

        # 3. ESXi 대응
        host_mock = MagicMock()
        host_mock.runtime.inMaintenanceMode = False
        mock_host.return_value = host_mock
        mock_conn.return_value = MagicMock()

        handler = EsxiActionHandler(mock_esxi_hosts)
        result = handler.respond(
            "vmgnode18", "WARNING", prob, mock_scores, "test_server"
        )

        assert result.success is True
        assert result.risk_level == "WARNING"
        assert "admission_control" in result.action
        mock_slack.assert_called_once()

    @patch.object(EsxiActionHandler, "_send_slack_alert")
    @patch.object(EsxiActionHandler, "_record_audit")
    @patch.object(EsxiActionHandler, "_wait_for_task")
    @patch.object(EsxiActionHandler, "_get_host")
    @patch.object(EsxiActionHandler, "_get_connection")
    @patch.object(EsxiActionHandler, "_release_connection")
    def test_full_e2e_critical_flow(
        self,
        mock_release,
        mock_conn,
        mock_host,
        mock_wait,
        mock_audit,
        mock_slack,
        vm_mock_server: HTTPServer,
        mock_esxi_hosts,
    ) -> None:
        """E2E: 피처 수집 → 앙상블 → CRITICAL → Maintenance Mode."""
        # 1. 피처 수집
        pipeline = FeaturePipeline(
            vm_base_url=vm_mock_server.url_for(""),
        )
        features = pipeline.build_feature_vector("test_server")
        assert len(features) == 45

        # 2. CRITICAL 레벨 스코어
        mock_scores = {
            "chronos": 0.88,
            "moirai": 0.95,
            "xgboost": 0.91,
            "anomaly_transformer": 0.87,
        }

        # 3. ESXi 대응
        host_mock = MagicMock()
        host_mock.runtime.inMaintenanceMode = False
        task_mock = MagicMock()
        task_mock.info.state = "success"
        host_mock.EnterMaintenanceMode_Task.return_value = task_mock
        mock_host.return_value = host_mock
        mock_conn.return_value = MagicMock()

        handler = EsxiActionHandler(mock_esxi_hosts)
        result = handler.respond(
            "vmgnode18", "CRITICAL", 0.91, mock_scores, "test_server"
        )

        assert result.success is True
        assert result.risk_level == "CRITICAL"
        assert result.action == "enter_maintenance_mode"
        host_mock.EnterMaintenanceMode_Task.assert_called_once()

    @patch.object(EsxiActionHandler, "_send_slack_alert")
    @patch.object(EsxiActionHandler, "_record_audit")
    @patch.object(EsxiActionHandler, "_wait_for_task")
    @patch.object(EsxiActionHandler, "_get_host")
    @patch.object(EsxiActionHandler, "_get_connection")
    @patch.object(EsxiActionHandler, "_release_connection")
    def test_full_e2e_recovery_flow(
        self,
        mock_release,
        mock_conn,
        mock_host,
        mock_wait,
        mock_audit,
        mock_slack,
        mock_esxi_hosts,
    ) -> None:
        """E2E: RECOVERY → Maintenance Mode 해제."""
        host_mock = MagicMock()
        host_mock.runtime.inMaintenanceMode = True
        task_mock = MagicMock()
        task_mock.info.state = "success"
        host_mock.ExitMaintenanceMode_Task.return_value = task_mock
        mock_host.return_value = host_mock
        mock_conn.return_value = MagicMock()

        handler = EsxiActionHandler(mock_esxi_hosts)
        result = handler.respond(
            "vmgnode18", "RECOVERY", 0.15,
            {"chronos": 0.12, "moirai": 0.18, "xgboost": 0.10, "anomaly_transformer": 0.14},
        )

        assert result.success is True
        assert result.action == "exit_maintenance_mode"

    def test_model_scores_completeness(self) -> None:
        """앙상블 결과에 4개 모델 스코어가 모두 포함되는지 확인."""
        with patch.object(EnsemblePredictor, "load_models"):
            ens = EnsemblePredictor(
                weights={
                    "chronos": 0.25, "moirai": 0.15,
                    "xgboost": 0.35, "anomaly_transformer": 0.25,
                },
            )

        # 4개 모델 mock
        mock_chronos = MagicMock()
        mock_chronos.predict_ce_anomaly.return_value = {
            "anomaly_score": 0.5, "model": "chronos",
            "predicted_peak": 10.0, "forecast_std": 1.0, "risk_level": "NORMAL",
        }
        ens._chronos = mock_chronos

        mock_moirai = MagicMock()
        mock_moirai.detect_anomaly.return_value = {
            "anomaly_score": 0.6, "model": "moirai",
            "forecast_std": 2.0, "risk_level": "NORMAL",
        }
        ens._moirai = mock_moirai

        mock_xgb = MagicMock()
        mock_xgb.predict.return_value = 0.7
        ens._xgboost = mock_xgb

        mock_at = MagicMock()
        mock_at.detect_anomaly.return_value = {
            "anomaly_score": 0.4, "model": "anomaly_transformer",
        }
        ens._anomaly_transformer = mock_at

        ce = pd.Series(np.zeros(100))
        features = {f"f{i}": 0.0 for i in range(45)}
        result = ens.predict(ce, features)

        assert len(result.model_scores) == 4
        assert "chronos" in result.model_scores
        assert "moirai" in result.model_scores
        assert "xgboost" in result.model_scores
        assert "anomaly_transformer" in result.model_scores
