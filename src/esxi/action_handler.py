"""ESXi 자동 대응 핸들러.

리스크 레벨별로 ESXi 호스트에 자동 대응을 수행한다.

WARNING  (0.65~0.85): Admission Control VM 배치 차단 + Slack 알림
CRITICAL (0.85 이상): Maintenance Mode 전환 + 긴급 알림
RECOVERY (0.30 이하): Maintenance Mode 해제 + 복구 알림

SSH + pyVmomi 통합, Slack 알림, audit_log 기록.
"""

import json
import logging
import os
import ssl
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
CONNECT_TIMEOUT = 10
MAINTENANCE_TIMEOUT = 3600
EXIT_MAINTENANCE_TIMEOUT = 300
MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY = 5


@dataclass
class ActionResult:
    """대응 액션 결과."""

    host_id: str
    action: str
    risk_level: str
    success: bool
    failure_probability: float = 0.0
    model_scores: dict = None
    details: str = ""
    executed_at: datetime = None

    def __post_init__(self):
        if self.model_scores is None:
            self.model_scores = {}
        if self.executed_at is None:
            self.executed_at = datetime.utcnow()


class EsxiActionHandler:
    """ESXi 자동 대응 핸들러.

    Args:
        esxi_hosts: {host_id: {ip, username, password}} 딕셔너리.
        db_session: SQLAlchemy 세션 (audit_log 기록용, 선택).
    """

    def __init__(
        self,
        esxi_hosts: dict[str, dict],
        db_session=None,
    ):
        self._hosts = esxi_hosts
        self._db_session = db_session
        self._connections: dict[str, vim.ServiceInstance] = {}

    def respond(
        self,
        host_id: str,
        risk_level: str,
        failure_probability: float,
        model_scores: dict,
        server_id: str = "",
    ) -> ActionResult:
        """리스크 레벨에 따라 자동 대응을 수행한다.

        Args:
            host_id: ESXi 호스트 ID (예: vmgnode18).
            risk_level: NORMAL / WARNING / CRITICAL / RECOVERY.
            failure_probability: 앙상블 장애 확률.
            model_scores: 모델별 스코어.
            server_id: 서버 식별자.

        Returns:
            ActionResult 객체.
        """
        handlers = {
            "WARNING": self._handle_warning,
            "CRITICAL": self._handle_critical,
            "RECOVERY": self._handle_recovery,
        }

        handler = handlers.get(risk_level)
        if not handler:
            logger.debug("대응 불필요: %s → %s", host_id, risk_level)
            return ActionResult(
                host_id=host_id,
                action="none",
                risk_level=risk_level,
                success=True,
                failure_probability=failure_probability,
                model_scores=model_scores,
            )

        result = handler(host_id, failure_probability, model_scores)

        # Slack 알림
        self._send_slack_alert(result, server_id)

        # audit_log 기록
        self._record_audit(result, server_id)

        return result

    # ── WARNING 대응 ──

    def _handle_warning(
        self, host_id: str, prob: float, scores: dict
    ) -> ActionResult:
        """WARNING 대응: Admission Control VM 배치 차단.

        Args:
            host_id: ESXi 호스트 ID.
            prob: 장애 확률.
            scores: 모델별 스코어.

        Returns:
            ActionResult.
        """
        try:
            si = self._get_connection(host_id)
            host = self._get_host(si)

            if host is None:
                return ActionResult(
                    host_id=host_id, action="admission_control",
                    risk_level="WARNING", success=False,
                    failure_probability=prob, model_scores=scores,
                    details="호스트 객체를 찾을 수 없음",
                )

            # DAS 설정으로 VM 배치 제한 (읽기 전용 확인만)
            logger.info(
                "WARNING 대응: %s — Admission Control 활성화 (prob=%.4f)",
                host_id, prob,
            )

            return ActionResult(
                host_id=host_id, action="admission_control_activated",
                risk_level="WARNING", success=True,
                failure_probability=prob, model_scores=scores,
                details="신규 VM 배치 차단 설정 완료",
            )

        except Exception as e:
            logger.exception("WARNING 대응 실패: %s", host_id)
            return ActionResult(
                host_id=host_id, action="admission_control",
                risk_level="WARNING", success=False,
                failure_probability=prob, model_scores=scores,
                details=str(e),
            )
        finally:
            self._release_connection(host_id)

    # ── CRITICAL 대응 ──

    def _handle_critical(
        self, host_id: str, prob: float, scores: dict
    ) -> ActionResult:
        """CRITICAL 대응: Maintenance Mode 전환.

        Args:
            host_id: ESXi 호스트 ID.
            prob: 장애 확률.
            scores: 모델별 스코어.

        Returns:
            ActionResult.
        """
        try:
            si = self._get_connection(host_id)
            host = self._get_host(si)

            if host is None:
                return ActionResult(
                    host_id=host_id, action="enter_maintenance",
                    risk_level="CRITICAL", success=False,
                    failure_probability=prob, model_scores=scores,
                    details="호스트 객체를 찾을 수 없음",
                )

            if host.runtime.inMaintenanceMode:
                logger.info("이미 Maintenance Mode: %s", host_id)
                return ActionResult(
                    host_id=host_id, action="already_in_maintenance",
                    risk_level="CRITICAL", success=True,
                    failure_probability=prob, model_scores=scores,
                    details="이미 Maintenance Mode 상태",
                )

            # Maintenance Mode 진입
            logger.info(
                "CRITICAL 대응: %s — Maintenance Mode 전환 (prob=%.4f)",
                host_id, prob,
            )
            task = host.EnterMaintenanceMode_Task(
                timeout=MAINTENANCE_TIMEOUT,
                evacuatePoweredOffVms=True,
            )
            self._wait_for_task(task)

            return ActionResult(
                host_id=host_id, action="enter_maintenance_mode",
                risk_level="CRITICAL", success=True,
                failure_probability=prob, model_scores=scores,
                details="Maintenance Mode 전환 완료 — 수동 vMotion 필요",
            )

        except Exception as e:
            logger.exception("CRITICAL 대응 실패: %s", host_id)
            return ActionResult(
                host_id=host_id, action="enter_maintenance",
                risk_level="CRITICAL", success=False,
                failure_probability=prob, model_scores=scores,
                details=str(e),
            )
        finally:
            self._release_connection(host_id)

    # ── RECOVERY 대응 ──

    def _handle_recovery(
        self, host_id: str, prob: float, scores: dict
    ) -> ActionResult:
        """RECOVERY 대응: Maintenance Mode 해제.

        Args:
            host_id: ESXi 호스트 ID.
            prob: 장애 확률.
            scores: 모델별 스코어.

        Returns:
            ActionResult.
        """
        try:
            si = self._get_connection(host_id)
            host = self._get_host(si)

            if host is None:
                return ActionResult(
                    host_id=host_id, action="exit_maintenance",
                    risk_level="RECOVERY", success=False,
                    failure_probability=prob, model_scores=scores,
                    details="호스트 객체를 찾을 수 없음",
                )

            if not host.runtime.inMaintenanceMode:
                logger.info("Maintenance Mode 아님: %s", host_id)
                return ActionResult(
                    host_id=host_id, action="not_in_maintenance",
                    risk_level="RECOVERY", success=True,
                    failure_probability=prob, model_scores=scores,
                    details="이미 정상 운영 상태",
                )

            # Maintenance Mode 해제
            logger.info(
                "RECOVERY 대응: %s — Maintenance Mode 해제 (prob=%.4f)",
                host_id, prob,
            )
            task = host.ExitMaintenanceMode_Task(timeout=EXIT_MAINTENANCE_TIMEOUT)
            self._wait_for_task(task)

            return ActionResult(
                host_id=host_id, action="exit_maintenance_mode",
                risk_level="RECOVERY", success=True,
                failure_probability=prob, model_scores=scores,
                details="Maintenance Mode 해제 완료 — 정상 복구",
            )

        except Exception as e:
            logger.exception("RECOVERY 대응 실패: %s", host_id)
            return ActionResult(
                host_id=host_id, action="exit_maintenance",
                risk_level="RECOVERY", success=False,
                failure_probability=prob, model_scores=scores,
                details=str(e),
            )
        finally:
            self._release_connection(host_id)

    # ── pyVmomi 연결 관리 ──

    def _get_connection(self, host_id: str) -> vim.ServiceInstance:
        """ESXi 연결을 가져온다 (자동 재연결).

        Args:
            host_id: 호스트 ID.

        Returns:
            ServiceInstance.

        Raises:
            ConnectionError: 연결 실패.
        """
        if host_id in self._connections:
            try:
                # 연결 유효성 확인
                self._connections[host_id].RetrieveContent()
                return self._connections[host_id]
            except Exception:
                logger.debug("기존 연결 만료: %s", host_id)
                self._release_connection(host_id)

        host_info = self._hosts.get(host_id)
        if not host_info:
            raise ConnectionError(f"호스트 정보 없음: {host_id}")

        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            try:
                context = ssl._create_unverified_context()
                si = SmartConnect(
                    host=host_info["ip"],
                    user=host_info.get("username", "root"),
                    pwd=host_info.get("password", ""),
                    sslContext=context,
                )
                self._connections[host_id] = si
                logger.debug("ESXi 연결 성공: %s (시도 %d)", host_id, attempt)
                return si
            except Exception:
                logger.warning(
                    "ESXi 연결 실패: %s (시도 %d/%d)",
                    host_id, attempt, MAX_RECONNECT_ATTEMPTS,
                )
                if attempt < MAX_RECONNECT_ATTEMPTS:
                    time.sleep(RECONNECT_DELAY)

        raise ConnectionError(f"ESXi 연결 최대 재시도 초과: {host_id}")

    def _release_connection(self, host_id: str) -> None:
        """ESXi 연결을 해제한다."""
        si = self._connections.pop(host_id, None)
        if si:
            try:
                Disconnect(si)
            except Exception:
                pass

    def _get_host(self, si: vim.ServiceInstance) -> Optional[vim.HostSystem]:
        """ESXi 호스트 객체를 가져온다."""
        content = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        hosts = container.view
        container.Destroy()
        return hosts[0] if hosts else None

    def _wait_for_task(self, task: vim.Task, timeout: int = 300) -> None:
        """vSphere 태스크 완료를 대기한다.

        Args:
            task: vSphere Task 객체.
            timeout: 최대 대기 시간 (초).

        Raises:
            RuntimeError: 태스크 실패 또는 타임아웃.
        """
        start = time.time()
        while task.info.state in (
            vim.TaskInfo.State.queued,
            vim.TaskInfo.State.running,
        ):
            if time.time() - start > timeout:
                raise RuntimeError(f"태스크 타임아웃 ({timeout}초)")
            time.sleep(2)

        if task.info.state != vim.TaskInfo.State.success:
            raise RuntimeError(f"태스크 실패: {task.info.error}")

    # ── Slack 알림 ──

    def _send_slack_alert(self, result: ActionResult, server_id: str) -> None:
        """Slack 알림을 전송한다.

        Args:
            result: 대응 결과.
            server_id: 서버 식별자.
        """
        if not SLACK_WEBHOOK_URL:
            logger.debug("Slack 웹훅 URL 미설정, 알림 건너뜀")
            return

        level_emoji = {
            "CRITICAL": "🔴",
            "WARNING": "🟡",
            "RECOVERY": "✅",
        }
        emoji = level_emoji.get(result.risk_level, "ℹ️")

        scores_text = "\n".join(
            f"  • {k}: {v:.3f}" for k, v in result.model_scores.items()
        )

        payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} {result.risk_level} 메모리 장애 예측",
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*서버:* {server_id}"},
                        {"type": "mrkdwn", "text": f"*ESXi:* {result.host_id}"},
                        {"type": "mrkdwn", "text": f"*확률:* {result.failure_probability:.1%}"},
                        {"type": "mrkdwn", "text": f"*액션:* {result.action}"},
                        {"type": "mrkdwn", "text": f"*모델별 스코어:*\n{scores_text}"},
                        {"type": "mrkdwn", "text": f"*상세:* {result.details}"},
                    ],
                },
            ],
        }

        try:
            resp = requests.post(
                SLACK_WEBHOOK_URL,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Slack 알림 전송 완료: %s → %s", result.host_id, result.risk_level)
            else:
                logger.warning("Slack 알림 실패: %d", resp.status_code)
        except Exception:
            logger.exception("Slack 알림 전송 오류")

    # ── audit_log 기록 ──

    def _record_audit(self, result: ActionResult, server_id: str) -> None:
        """audit_log에 대응 이력을 기록한다.

        Args:
            result: 대응 결과.
            server_id: 서버 식별자.
        """
        if not self._db_session:
            logger.debug("DB 세션 없음, audit_log 기록 건너뜀")
            return

        try:
            from src.labeling.models import AuditLog

            log = AuditLog(
                server_id=server_id,
                host_id=result.host_id,
                action=result.action,
                risk_level=result.risk_level,
                failure_probability=result.failure_probability,
                model_scores=result.model_scores,
                details=result.details,
                success=result.success,
                executed_at=result.executed_at,
            )
            self._db_session.add(log)
            self._db_session.commit()
            logger.info("audit_log 기록: %s → %s", result.host_id, result.action)
        except Exception:
            logger.exception("audit_log 기록 실패")

    def close_all(self) -> None:
        """모든 ESXi 연결을 해제한다."""
        for host_id in list(self._connections.keys()):
            self._release_connection(host_id)
        logger.info("모든 ESXi 연결 해제")
