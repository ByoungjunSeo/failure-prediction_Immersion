"""레이블링 시스템 SQLAlchemy 모델.

PostgreSQL 스키마:
  - failure_events: UE 발생 이벤트 기록
  - training_labels: 학습용 Positive/Negative 샘플
  - audit_log: ESXi 대응 이력 (model_scores JSONB 포함)
"""

import logging
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy 선언적 베이스."""
    pass


class FailureEvent(Base):
    """UE (Uncorrectable Error) 장애 이벤트.

    rasdaemon에서 UE 감지 시 자동 삽입된다.
    """

    __tablename__ = "failure_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(String(50), nullable=False, index=True)
    dimm_slot = Column(String(50), nullable=False)
    mc = Column(Integer, default=0)
    channel = Column(Integer, default=0)
    csrow = Column(Integer, default=0)
    error_type = Column(String(20), default="uncorrected")
    error_count = Column(Integer, default=1)
    address = Column(String(50))
    detected_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TrainingLabel(Base):
    """학습용 Positive/Negative 레이블.

    UE 발생 전 6/12/24/48/72시간 구간을 Positive로 설정.
    Positive:Negative = 1:10 비율 유지.
    """

    __tablename__ = "training_labels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(String(50), nullable=False, index=True)
    dimm_slot = Column(String(50), nullable=False)
    feature_timestamp = Column(DateTime, nullable=False, index=True)
    label = Column(Integer, nullable=False)  # 0=Negative, 1=Positive
    hours_before_failure = Column(Float)  # UE 발생 전 시간 (Positive만)
    failure_event_id = Column(Integer)  # 관련 FailureEvent ID
    features_json = Column(JSONB)  # 해당 시점 45개 피처 스냅샷
    created_at = Column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    """ESXi 자동 대응 이력.

    리스크 레벨별 대응 액션과 모델별 스코어를 기록한다.
    """

    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    server_id = Column(String(50), nullable=False, index=True)
    host_id = Column(String(50))  # ESXi 호스트 ID
    action = Column(String(100), nullable=False)
    risk_level = Column(String(20), nullable=False)
    failure_probability = Column(Float)
    model_scores = Column(JSONB)  # 4개 모델 각각의 스코어
    details = Column(Text)
    success = Column(Boolean, default=True)
    executed_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


def get_engine(db_url: str = "postgresql://hpcdev:password@10.100.230.72:5432/failure_pred"):
    """SQLAlchemy 엔진을 생성한다.

    Args:
        db_url: PostgreSQL 연결 URL.

    Returns:
        Engine 객체.
    """
    return create_engine(db_url, pool_size=5, max_overflow=10, echo=False)


def create_tables(db_url: str = "postgresql://hpcdev:password@10.100.230.72:5432/failure_pred") -> None:
    """모든 테이블을 생성한다.

    Args:
        db_url: PostgreSQL 연결 URL.
    """
    engine = get_engine(db_url)
    Base.metadata.create_all(engine)
    logger.info("DB 테이블 생성 완료")


def get_session(db_url: str = "postgresql://hpcdev:password@10.100.230.72:5432/failure_pred") -> Session:
    """DB 세션을 생성한다.

    Args:
        db_url: PostgreSQL 연결 URL.

    Returns:
        Session 객체.
    """
    engine = get_engine(db_url)
    session_factory = sessionmaker(bind=engine)
    return session_factory()
