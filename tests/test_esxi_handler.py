"""ESXi Action Handler 단위 테스트.

mock 기반으로 WARNING/CRITICAL/RECOVERY 대응 및 Slack 알림을 검증한다.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.esxi.action_handler import ActionResult, EsxiActionHandler


MOCK_HOSTS = {
    "vmgnode18": {
        "ip": "10.148.148.118",
        "username": "root",
        "password": "VMware!0",
    },
}

MOCK_SCORES = {
    "chronos": 0.81,
    "moirai": 0.79,
    "xgboost": 0.87,
    "anomaly_transformer": 0.78,
}


# === mock 헬퍼 ===


def _make_mock_host(in_maintenance: bool = False) -> MagicMock:
    """mock ESXi 호스트 객체."""
    host = MagicMock()
    host.runtime.inMaintenanceMode = in_maintenance

    # EnterMaintenanceMode_Task
    task = MagicMock()
    task.info.state = "success"
    host.EnterMaintenanceMode_Task.return_value = task

    # ExitMaintenanceMode_Task
    exit_task = MagicMock()
    exit_task.info.state = "success"
    host.ExitMaintenanceMode_Task.return_value = exit_task

    return host


# === WARNING 대응 테스트 ===


class TestWarningResponse:
    """WARNING 레벨 대응 테스트."""

    @patch.object(EsxiActionHandler, "_send_slack_alert")
    @patch.object(EsxiActionHandler, "_record_audit")
    @patch.object(EsxiActionHandler, "_get_host")
    @patch.object(EsxiActionHandler, "_get_connection")
    @patch.object(EsxiActionHandler, "_release_connection")
    def test_warning_success(
        self, mock_release, mock_conn, mock_host, mock_audit, mock_slack
    ) -> None:
        """WARNING 대응이 성공하는지 확인."""
        mock_host.return_value = _make_mock_host()
        mock_conn.return_value = MagicMock()

        handler = EsxiActionHandler(MOCK_HOSTS)
        result = handler.respond(
            "vmgnode18", "WARNING", 0.75, MOCK_SCORES, "18AFD199"
        )

        assert result.success is True
        assert result.risk_level == "WARNING"
        assert "admission_control" in result.action
        mock_slack.assert_called_once()
        mock_audit.assert_called_once()

    @patch.object(EsxiActionHandler, "_send_slack_alert")
    @patch.object(EsxiActionHandler, "_record_audit")
    @patch.object(EsxiActionHandler, "_get_host", return_value=None)
    @patch.object(EsxiActionHandler, "_get_connection")
    @patch.object(EsxiActionHandler, "_release_connection")
    def test_warning_host_not_found(
        self, mock_release, mock_conn, mock_host, mock_audit, mock_slack
    ) -> None:
        """호스트를 찾을 수 없을 때 실패 반환."""
        mock_conn.return_value = MagicMock()

        handler = EsxiActionHandler(MOCK_HOSTS)
        result = handler.respond(
            "vmgnode18", "WARNING", 0.75, MOCK_SCORES
        )

        assert result.success is False
        assert "찾을 수 없음" in result.details


# === CRITICAL 대응 테스트 ===


class TestCriticalResponse:
    """CRITICAL 레벨 대응 테스트."""

    @patch.object(EsxiActionHandler, "_send_slack_alert")
    @patch.object(EsxiActionHandler, "_record_audit")
    @patch.object(EsxiActionHandler, "_wait_for_task")
    @patch.object(EsxiActionHandler, "_get_host")
    @patch.object(EsxiActionHandler, "_get_connection")
    @patch.object(EsxiActionHandler, "_release_connection")
    def test_critical_enter_maintenance(
        self, mock_release, mock_conn, mock_host, mock_wait, mock_audit, mock_slack
    ) -> None:
        """CRITICAL 시 Maintenance Mode 진입."""
        host = _make_mock_host(in_maintenance=False)
        mock_host.return_value = host
        mock_conn.return_value = MagicMock()

        handler = EsxiActionHandler(MOCK_HOSTS)
        result = handler.respond(
            "vmgnode18", "CRITICAL", 0.92, MOCK_SCORES
        )

        assert result.success is True
        assert result.action == "enter_maintenance_mode"
        host.EnterMaintenanceMode_Task.assert_called_once()
        mock_slack.assert_called_once()

    @patch.object(EsxiActionHandler, "_send_slack_alert")
    @patch.object(EsxiActionHandler, "_record_audit")
    @patch.object(EsxiActionHandler, "_get_host")
    @patch.object(EsxiActionHandler, "_get_connection")
    @patch.object(EsxiActionHandler, "_release_connection")
    def test_critical_already_in_maintenance(
        self, mock_release, mock_conn, mock_host, mock_audit, mock_slack
    ) -> None:
        """이미 Maintenance Mode일 때 중복 진입하지 않음."""
        mock_host.return_value = _make_mock_host(in_maintenance=True)
        mock_conn.return_value = MagicMock()

        handler = EsxiActionHandler(MOCK_HOSTS)
        result = handler.respond(
            "vmgnode18", "CRITICAL", 0.95, MOCK_SCORES
        )

        assert result.success is True
        assert result.action == "already_in_maintenance"


# === RECOVERY 대응 테스트 ===


class TestRecoveryResponse:
    """RECOVERY 레벨 대응 테스트."""

    @patch.object(EsxiActionHandler, "_send_slack_alert")
    @patch.object(EsxiActionHandler, "_record_audit")
    @patch.object(EsxiActionHandler, "_wait_for_task")
    @patch.object(EsxiActionHandler, "_get_host")
    @patch.object(EsxiActionHandler, "_get_connection")
    @patch.object(EsxiActionHandler, "_release_connection")
    def test_recovery_exit_maintenance(
        self, mock_release, mock_conn, mock_host, mock_wait, mock_audit, mock_slack
    ) -> None:
        """RECOVERY 시 Maintenance Mode 해제."""
        host = _make_mock_host(in_maintenance=True)
        mock_host.return_value = host
        mock_conn.return_value = MagicMock()

        handler = EsxiActionHandler(MOCK_HOSTS)
        result = handler.respond(
            "vmgnode18", "RECOVERY", 0.15, MOCK_SCORES
        )

        assert result.success is True
        assert result.action == "exit_maintenance_mode"
        host.ExitMaintenanceMode_Task.assert_called_once()

    @patch.object(EsxiActionHandler, "_send_slack_alert")
    @patch.object(EsxiActionHandler, "_record_audit")
    @patch.object(EsxiActionHandler, "_get_host")
    @patch.object(EsxiActionHandler, "_get_connection")
    @patch.object(EsxiActionHandler, "_release_connection")
    def test_recovery_not_in_maintenance(
        self, mock_release, mock_conn, mock_host, mock_audit, mock_slack
    ) -> None:
        """Maintenance Mode 아닐 때 아무 동작하지 않음."""
        mock_host.return_value = _make_mock_host(in_maintenance=False)
        mock_conn.return_value = MagicMock()

        handler = EsxiActionHandler(MOCK_HOSTS)
        result = handler.respond(
            "vmgnode18", "RECOVERY", 0.10, MOCK_SCORES
        )

        assert result.success is True
        assert result.action == "not_in_maintenance"


# === NORMAL — 대응 불필요 ===


class TestNormalResponse:
    """NORMAL 레벨 테스트."""

    def test_normal_no_action(self) -> None:
        """NORMAL일 때 대응 안 함."""
        handler = EsxiActionHandler(MOCK_HOSTS)
        result = handler.respond(
            "vmgnode18", "NORMAL", 0.30, MOCK_SCORES
        )

        assert result.success is True
        assert result.action == "none"


# === Slack 알림 테스트 ===


class TestSlackAlert:
    """Slack 알림 검증."""

    @patch("src.esxi.action_handler.requests.post")
    @patch("src.esxi.action_handler.SLACK_WEBHOOK_URL", "https://hooks.slack.com/test")
    def test_slack_payload_contains_model_scores(self, mock_post) -> None:
        """Slack 알림에 model_scores 4개가 포함되는지 확인."""
        mock_post.return_value = MagicMock(status_code=200)

        handler = EsxiActionHandler(MOCK_HOSTS)
        result = ActionResult(
            host_id="vmgnode18",
            action="enter_maintenance_mode",
            risk_level="CRITICAL",
            success=True,
            failure_probability=0.92,
            model_scores=MOCK_SCORES,
            details="test",
        )

        handler._send_slack_alert(result, "18AFD199")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

        # 페이로드 검증
        assert payload is not None
        payload_str = str(payload)
        assert "chronos" in payload_str
        assert "moirai" in payload_str
        assert "xgboost" in payload_str
        assert "anomaly_transformer" in payload_str
        assert "CRITICAL" in payload_str

    @patch("src.esxi.action_handler.SLACK_WEBHOOK_URL", "")
    def test_slack_skip_when_no_url(self) -> None:
        """웹훅 URL이 없으면 Slack 알림을 건너뜀."""
        handler = EsxiActionHandler(MOCK_HOSTS)
        result = ActionResult(
            host_id="vmgnode18", action="test",
            risk_level="WARNING", success=True,
            model_scores=MOCK_SCORES,
        )
        # 에러 없이 완료되어야 함
        handler._send_slack_alert(result, "test")


# === audit_log 기록 테스트 ===


class TestAuditLog:
    """audit_log 기록 검증."""

    def test_audit_log_with_session(self) -> None:
        """DB 세션이 있으면 audit_log에 기록."""
        mock_session = MagicMock()

        handler = EsxiActionHandler(MOCK_HOSTS, db_session=mock_session)
        result = ActionResult(
            host_id="vmgnode18", action="enter_maintenance_mode",
            risk_level="CRITICAL", success=True,
            failure_probability=0.92, model_scores=MOCK_SCORES,
        )

        handler._record_audit(result, "18AFD199")

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    def test_audit_log_skip_without_session(self) -> None:
        """DB 세션 없으면 audit_log 기록 건너뜀."""
        handler = EsxiActionHandler(MOCK_HOSTS, db_session=None)
        result = ActionResult(
            host_id="vmgnode18", action="test",
            risk_level="WARNING", success=True,
            model_scores=MOCK_SCORES,
        )
        # 에러 없이 완료되어야 함
        handler._record_audit(result, "test")
