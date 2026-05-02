"""자동 레이블 생성기.

rasdaemon UE 이벤트를 감지하고, UE 발생 전
6/12/24/48/72시간 시점을 Positive 샘플로 자동 생성한다.

레이블 전략:
  - Positive: UE 발생 전 각 시간대의 피처 스냅샷
  - Negative: UE 없는 정상 구간에서 랜덤 샘플링
  - 비율: Positive:Negative = 1:10
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from src.labeling.models import AuditLog, FailureEvent, TrainingLabel

logger = logging.getLogger(__name__)

# UE 발생 전 Positive 구간 (시간)
POSITIVE_WINDOWS_HOURS = [6, 12, 24, 48, 72]

# Positive:Negative 비율
POS_NEG_RATIO = 10


class LabelGenerator:
    """자동 레이블 생성기.

    Args:
        session: SQLAlchemy 세션.
    """

    def __init__(self, session: Session):
        self._session = session

    def record_failure_event(
        self,
        server_id: str,
        dimm_slot: str,
        mc: int = 0,
        channel: int = 0,
        csrow: int = 0,
        error_count: int = 1,
        address: Optional[str] = None,
    ) -> FailureEvent:
        """UE 장애 이벤트를 기록한다.

        Args:
            server_id: 서버 ID.
            dimm_slot: DIMM 슬롯 명.
            mc: 메모리 컨트롤러 번호.
            channel: 채널 번호.
            csrow: csrow 번호.
            error_count: 에러 횟수.
            address: 메모리 주소.

        Returns:
            생성된 FailureEvent 객체.
        """
        event = FailureEvent(
            server_id=server_id,
            dimm_slot=dimm_slot,
            mc=mc,
            channel=channel,
            csrow=csrow,
            error_type="uncorrected",
            error_count=error_count,
            address=address,
            detected_at=datetime.utcnow(),
        )
        self._session.add(event)
        self._session.commit()

        logger.info(
            "UE 이벤트 기록: server=%s, dimm=%s, count=%d",
            server_id, dimm_slot, error_count,
        )

        # Positive 레이블 자동 생성
        self._generate_positive_labels(event)

        return event

    def _generate_positive_labels(self, event: FailureEvent) -> list[TrainingLabel]:
        """UE 발생 전 각 시간대에 대한 Positive 레이블을 생성한다.

        Args:
            event: UE 장애 이벤트.

        Returns:
            생성된 TrainingLabel 리스트.
        """
        labels = []

        for hours in POSITIVE_WINDOWS_HOURS:
            feature_ts = event.detected_at - timedelta(hours=hours)

            label = TrainingLabel(
                server_id=event.server_id,
                dimm_slot=event.dimm_slot,
                feature_timestamp=feature_ts,
                label=1,
                hours_before_failure=float(hours),
                failure_event_id=event.id,
            )
            labels.append(label)

        self._session.add_all(labels)
        self._session.commit()

        logger.info(
            "Positive 레이블 %d개 생성: server=%s, windows=%s",
            len(labels), event.server_id, POSITIVE_WINDOWS_HOURS,
        )

        return labels

    def generate_negative_labels(
        self,
        server_id: str,
        dimm_slot: str,
        start_time: datetime,
        end_time: datetime,
        count: Optional[int] = None,
    ) -> list[TrainingLabel]:
        """정상 구간에서 Negative 레이블을 생성한다.

        Args:
            server_id: 서버 ID.
            dimm_slot: DIMM 슬롯 명.
            start_time: 시작 시간.
            end_time: 종료 시간.
            count: 생성할 Negative 수 (None이면 Positive 수 × POS_NEG_RATIO).

        Returns:
            생성된 TrainingLabel 리스트.
        """
        if count is None:
            pos_count = (
                self._session.query(TrainingLabel)
                .filter_by(server_id=server_id, label=1)
                .count()
            )
            count = pos_count * POS_NEG_RATIO

        if count == 0:
            logger.info("Positive 레이블 없음, Negative 생성 건너뜀")
            return []

        # 장애 이벤트 시간 조회 (Negative에서 제외할 구간)
        failure_times = [
            e.detected_at
            for e in self._session.query(FailureEvent)
            .filter_by(server_id=server_id)
            .all()
        ]

        # 정상 구간에서 균등 간격으로 샘플링
        import numpy as np

        total_seconds = (end_time - start_time).total_seconds()
        if total_seconds <= 0:
            return []

        labels = []
        offsets = np.linspace(0, total_seconds, count + 2)[1:-1]

        for offset in offsets:
            ts = start_time + timedelta(seconds=float(offset))

            # 장애 전 72시간 이내인지 확인 → 제외
            too_close = any(
                timedelta(0) < (ft - ts) < timedelta(hours=72)
                for ft in failure_times
            )
            if too_close:
                continue

            label = TrainingLabel(
                server_id=server_id,
                dimm_slot=dimm_slot,
                feature_timestamp=ts,
                label=0,
                hours_before_failure=None,
                failure_event_id=None,
            )
            labels.append(label)

        self._session.add_all(labels)
        self._session.commit()

        logger.info(
            "Negative 레이블 %d개 생성: server=%s", len(labels), server_id
        )

        return labels

    def get_training_data(self) -> list[TrainingLabel]:
        """전체 학습용 레이블을 조회한다.

        Returns:
            TrainingLabel 리스트.
        """
        return self._session.query(TrainingLabel).all()

    def get_label_stats(self) -> dict[str, int]:
        """레이블 통계를 반환한다.

        Returns:
            {positive: 수, negative: 수, total: 수}.
        """
        pos = self._session.query(TrainingLabel).filter_by(label=1).count()
        neg = self._session.query(TrainingLabel).filter_by(label=0).count()
        return {"positive": pos, "negative": neg, "total": pos + neg}

    def record_audit(
        self,
        server_id: str,
        host_id: str,
        action: str,
        risk_level: str,
        failure_probability: float,
        model_scores: dict,
        details: str = "",
        success: bool = True,
    ) -> AuditLog:
        """ESXi 대응 이력을 기록한다.

        Args:
            server_id: 서버 ID.
            host_id: ESXi 호스트 ID.
            action: 수행한 액션.
            risk_level: 리스크 레벨.
            failure_probability: 앙상블 장애 확률.
            model_scores: 모델별 스코어 딕셔너리.
            details: 상세 내용.
            success: 성공 여부.

        Returns:
            생성된 AuditLog 객체.
        """
        log = AuditLog(
            server_id=server_id,
            host_id=host_id,
            action=action,
            risk_level=risk_level,
            failure_probability=failure_probability,
            model_scores=model_scores,
            details=details,
            success=success,
            executed_at=datetime.utcnow(),
        )
        self._session.add(log)
        self._session.commit()

        logger.info(
            "감사 로그 기록: %s → %s (%s, prob=%.4f)",
            server_id, action, risk_level, failure_probability,
        )

        return log
