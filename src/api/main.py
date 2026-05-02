"""FastAPI 추론 API 서버.

모든 모델을 cuda:0에 로드하고
단일/일괄 서버 장애 확률 예측 엔드포인트를 제공한다.

포트: 8000
GPU:  cuda:0 (추론 전담)
문서: http://10.100.230.71:8000/docs

실행: uvicorn src.api.main:app --host 0.0.0.0 --port 8000
"""

import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, "/opt/failure_prediction")

from src.features.feature_pipeline import FeaturePipeline
from src.models.ensemble import EnsemblePredictor

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# ── 글로벌 객체 ──
ensemble: Optional[EnsemblePredictor] = None
feature_pipeline: Optional[FeaturePipeline] = None
scheduler = None
_metrics_cache: dict[str, "PredictionResult"] = {}  # 캐시: server_id → 최신 결과
_esxi_cache: dict[str, dict] = {}  # 캐시: host_id → {cpu, mem, vmkernel, vm_count}
_ce_simulation: dict[str, float] = {}  # CE 시뮬레이션 값: server_id → ce_count
_ce_sim_minute: int = 0  # CE 시뮬레이션 진행 분

# 서버 목록 (ESXi 4대)
ESXI_SERVERS = ["vmgnode18", "vmgnode23", "vmgnode26", "vmgnode30"]
NODE_SERVERS = ["18AFD199"]  # node3


# ── Pydantic 모델 ──

class PredictionResult(BaseModel):
    """단일 서버 예측 결과."""

    server_id: str
    timestamp: str
    failure_probability: float
    risk_level: str
    model_scores: dict[str, float]
    model_details: dict[str, dict] = {}
    top_causes: list[dict] = []
    recommended_action: str = ""


class AllPredictionResult(BaseModel):
    """전체 서버 일괄 예측 결과."""

    predictions: list[PredictionResult]
    total_servers: int
    warning_count: int
    critical_count: int


class ModelScores(BaseModel):
    """모델별 개별 스코어."""

    server_id: str
    timestamp: str
    scores: dict[str, dict]


class HealthResponse(BaseModel):
    """서버 상태."""

    status: str
    models_loaded: list[str]
    uptime_seconds: float
    gpu_device: str


class ModelInfo(BaseModel):
    """모델 정보."""

    weights: dict[str, float]
    models: list[str]
    version: str


class LabelRequest(BaseModel):
    """수동 레이블 추가 요청."""

    server_id: str
    dimm_slot: str
    error_type: str = "uncorrected"
    error_count: int = 1


# ── 앱 시작 시간 ──
_start_time = time.time()


# ── Lifespan ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작 시 모델 로드, 종료 시 정리."""
    global ensemble, feature_pipeline, scheduler

    logger.info("모델 로드 시작 (cuda:0)...")

    # Phase 3: Zero-shot만 로드 (XGBoost/AT는 파일 있을 때만)
    weights = {"chronos": 0.5, "moirai": 0.5}

    xgb_path = Path("/opt/failure_prediction/models/checkpoints/xgboost_model.json")
    at_path = Path("/opt/failure_prediction/models/checkpoints/anomaly_transformer.pt")

    if xgb_path.exists() and at_path.exists():
        weights = {
            "chronos": 0.25,
            "moirai": 0.15,
            "xgboost": 0.35,
            "anomaly_transformer": 0.25,
        }

    ensemble = EnsemblePredictor(weights=weights, device="cuda:0")
    ensemble.load_models()

    feature_pipeline = FeaturePipeline()

    # APScheduler
    _setup_scheduler()

    logger.info("API 서버 준비 완료")
    yield

    # 종료
    if scheduler:
        scheduler.shutdown(wait=False)
    logger.info("API 서버 종료")


def _setup_scheduler():
    """APScheduler 스케줄러를 설정한다."""
    global scheduler

    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler()

        # 1분마다: 전체 서버 추론
        scheduler.add_job(_scheduled_inference, "interval", minutes=1, id="inference")

        # 매일 새벽 2시: XGBoost 재학습
        scheduler.add_job(_scheduled_retrain, "cron", hour=2, id="retrain")

        # 일요일 새벽 3시: 앙상블 가중치 최적화
        scheduler.add_job(_scheduled_optimize, "cron", day_of_week="sun", hour=3, id="optimize")

        scheduler.start()
        logger.info("APScheduler 시작: inference(1m), retrain(2am), optimize(Sun 3am)")
    except Exception:
        logger.exception("스케줄러 시작 실패")


def _scheduled_inference():
    """정기 추론 실행 + ESXi 수집 + CE 시뮬레이션 + 캐시 갱신."""
    # ESXi 메트릭 수집
    _collect_esxi_metrics()

    # CE 시뮬레이션 값 갱신
    _update_ce_simulation()

    if not ensemble or not feature_pipeline:
        return
    for server_id in ESXI_SERVERS + NODE_SERVERS:
        try:
            result = _run_prediction(server_id)
            _metrics_cache[server_id] = result
        except Exception:
            logger.warning("정기 추론 실패: %s", server_id)


def _update_ce_simulation():
    """CE 에러 시뮬레이션 값을 갱신한다.

    4단계 패턴 순환 (각 60분):
      0~59분:   정상 (CE 0~2)
      60~119분: 점진 증가 (CE 0→50)
      120~179분: 급증 (CE 50~150)
      180~239분: 복구 (CE 50→0)
      240분~:   다시 처음부터
    """
    global _ce_sim_minute
    _ce_sim_minute += 1
    cycle_min = _ce_sim_minute % 240
    phase = cycle_min // 60

    for server_id in ESXI_SERVERS + NODE_SERVERS:
        if phase == 0:
            ce = float(np.random.poisson(1))
        elif phase == 1:
            progress = (cycle_min - 60) / 60.0
            ce = float(np.random.poisson(2 + int(progress * 50)))
        elif phase == 2:
            ce = float(np.random.poisson(100))
        else:
            progress = (cycle_min - 180) / 60.0
            ce = float(np.random.poisson(max(1, int(50 * (1 - progress)))))

        _ce_simulation[server_id] = ce

    if _ce_sim_minute % 10 == 0:
        phase_names = ["정상", "점진증가", "급증", "복구"]
        logger.info("CE 시뮬레이션 [%d분, %s]: CE=%.0f",
                    _ce_sim_minute, phase_names[phase], ce)


def _collect_esxi_metrics():
    """ESXi 메트릭을 수집하여 캐시에 저장한다."""
    import os as _os
    from src.collectors.esxi_collector import EsxiCollector
    from src.collectors.esxi_ssh_collector import EsxiSshCollector

    esxi_hosts = [
        ("vmgnode18", "10.148.148.118"),
        ("vmgnode23", "10.148.148.123"),
        ("vmgnode26", "10.148.148.126"),
        ("vmgnode30", "10.148.148.130"),
    ]
    esxi_pw = _os.getenv("ESXI_PASSWORD", "VMware!0")

    for host_id, host_ip in esxi_hosts:
        cache = {"cpu": 0.0, "mem": 0.0, "vmkernel": 0, "vm_count": 0}
        try:
            col = EsxiCollector(host_id=host_id, host_ip=host_ip, username="root", password=esxi_pw)
            data = col.collect()
            cache["cpu"] = data.host_metrics.cpu_usage_percent
            cache["mem"] = data.host_metrics.mem_usage_percent
            cache["vm_count"] = data.vm_aggregate.vm_count
        except Exception:
            logger.debug("ESXi pyVmomi 수집 실패: %s", host_id)

        try:
            ssh_col = EsxiSshCollector(host_id=host_id, host_ip=host_ip, username="root", password=esxi_pw)
            ssh_data = ssh_col.collect()
            cache["vmkernel"] = ssh_data.error_count
        except Exception:
            logger.debug("ESXi SSH 수집 실패: %s", host_id)

        _esxi_cache[host_id] = cache


def _scheduled_retrain():
    """XGBoost 일일 재학습."""
    logger.info("XGBoost 재학습 시작 (스케줄)")
    # TODO: 신규 데이터 수집 → 파인튜닝 → 모델 교체


def _scheduled_optimize():
    """앙상블 가중치 주간 최적화."""
    logger.info("앙상블 가중치 최적화 시작 (스케줄)")
    # TODO: Optuna로 최적 가중치 탐색


# ── FastAPI 앱 ──

app = FastAPI(
    title="HPC 메모리 장애 예측 API",
    description="AI 기반 DRAM Fault 예측 — Chronos, MOIRAI, XGBoost, Anomaly Transformer 앙상블",
    version="1.0.0",
    lifespan=lifespan,
)


# ── 엔드포인트 ──

@app.get("/predict/{server_id}", response_model=PredictionResult)
async def predict_server(server_id: str):
    """단일 서버 앙상블 장애 확률 예측.

    Args:
        server_id: 서버 식별자 (예: vmgnode18, 18AFD199).

    Returns:
        PredictionResult 객체.
    """
    result = _run_prediction(server_id)
    return result


@app.get("/predict/all", response_model=AllPredictionResult)
async def predict_all():
    """전체 서버 일괄 추론."""
    predictions = []
    for server_id in ESXI_SERVERS + NODE_SERVERS:
        try:
            result = _run_prediction(server_id)
            predictions.append(result)
        except Exception:
            logger.warning("서버 예측 실패: %s", server_id)

    warning_count = sum(1 for p in predictions if p.risk_level == "WARNING")
    critical_count = sum(1 for p in predictions if p.risk_level == "CRITICAL")

    return AllPredictionResult(
        predictions=predictions,
        total_servers=len(predictions),
        warning_count=warning_count,
        critical_count=critical_count,
    )


@app.get("/models/scores/{server_id}", response_model=ModelScores)
async def model_scores(server_id: str):
    """모델별 개별 스코어 조회.

    Args:
        server_id: 서버 식별자.

    Returns:
        ModelScores 객체 (4개 모델 각각).
    """
    result = _run_prediction(server_id)
    return ModelScores(
        server_id=server_id,
        timestamp=result.timestamp,
        scores=result.model_details,
    )


@app.get("/health", response_model=HealthResponse)
async def health():
    """서버 상태 확인."""
    loaded = []
    if ensemble:
        loaded = list(ensemble._weights.keys())

    return HealthResponse(
        status="healthy" if ensemble else "loading",
        models_loaded=loaded,
        uptime_seconds=time.time() - _start_time,
        gpu_device="cuda:0",
    )


@app.get("/models/info", response_model=ModelInfo)
async def model_info():
    """현재 모델 버전 및 가중치 정보."""
    weights = ensemble._weights if ensemble else {}
    return ModelInfo(
        weights=weights,
        models=list(weights.keys()),
        version="1.0.0",
    )


@app.post("/labels/add")
async def add_label(req: LabelRequest):
    """수동 장애 레이블 추가."""
    logger.info("수동 레이블 추가: %s, %s", req.server_id, req.dimm_slot)
    return {
        "status": "recorded",
        "server_id": req.server_id,
        "dimm_slot": req.dimm_slot,
        "error_type": req.error_type,
    }


@app.get("/metrics")
async def metrics():
    """Prometheus 형식 메트릭 (캐시 기반 — 즉시 응답)."""
    lines = []
    lines.append("# HELP failure_prediction_up API 서버 상태")
    lines.append("# TYPE failure_prediction_up gauge")
    lines.append("failure_prediction_up 1")

    lines.append("# HELP failure_prediction_uptime_seconds 서버 업타임")
    lines.append("# TYPE failure_prediction_uptime_seconds gauge")
    lines.append(f"failure_prediction_uptime_seconds {time.time() - _start_time:.1f}")

    lines.append("# HELP failure_probability 서버별 장애 확률")
    lines.append("# TYPE failure_probability gauge")
    lines.append("# HELP model_score 모델별 개별 스코어")
    lines.append("# TYPE model_score gauge")

    for server_id, result in _metrics_cache.items():
        lines.append(
            f'failure_probability{{server="{server_id}"}} {result.failure_probability:.4f}'
        )
        for model_name, score in result.model_scores.items():
            lines.append(
                f'model_score{{server="{server_id}",model="{model_name}"}} {score:.4f}'
            )
            # XGBoost는 별도 메트릭으로도 출력 (Grafana 호환성)
            if model_name == "xgboost":
                lines.append(
                    f'xgb_score{{server="{server_id}"}} {score:.4f}'
                )

    # ESXi 호스트 메트릭 (캐시에서 읽기 — 즉시 응답)
    lines.append("# HELP esxi_cpu_usage ESXi CPU 사용률")
    lines.append("# TYPE esxi_cpu_usage gauge")
    lines.append("# HELP esxi_mem_usage ESXi 메모리 사용률")
    lines.append("# TYPE esxi_mem_usage gauge")
    lines.append("# HELP esxi_vmkernel_error_cnt ESXi vmkernel 메모리 에러 수")
    lines.append("# TYPE esxi_vmkernel_error_cnt gauge")
    lines.append("# HELP esxi_vm_count ESXi VM 수")
    lines.append("# TYPE esxi_vm_count gauge")

    for host_id, cache in _esxi_cache.items():
        lines.append(f'esxi_cpu_usage{{host="{host_id}"}} {cache["cpu"]:.2f}')
        lines.append(f'esxi_mem_usage{{host="{host_id}"}} {cache["mem"]:.2f}')
        lines.append(f'esxi_vmkernel_error_cnt{{host="{host_id}"}} {cache["vmkernel"]}')
        lines.append(f'esxi_vm_count{{host="{host_id}"}} {cache["vm_count"]}')

    # CE 시뮬레이션 메트릭
    lines.append("# HELP memory_errors DIMM CE 에러 수 (시뮬레이션)")
    lines.append("# TYPE memory_errors gauge")
    for server_id, ce_val in _ce_simulation.items():
        lines.append(f'memory_errors{{server="{server_id}",mc="0",csrow="0",channel="0"}} {ce_val:.0f}')

    from starlette.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")


# ── 내부 함수 ──

def _run_prediction(server_id: str) -> PredictionResult:
    """단일 서버 예측을 실행한다.

    Args:
        server_id: 서버 ID.

    Returns:
        PredictionResult 객체.

    Raises:
        HTTPException: 모델 미로드 시.
    """
    if not ensemble or not feature_pipeline:
        raise HTTPException(status_code=503, detail="모델 로드 중")

    start = time.time()

    # CE 시계열 조회
    ce_series = feature_pipeline.fetch_ce_series(server_id, hours=72)

    # 피처 벡터 (XGBoost용)
    features = None
    try:
        features = feature_pipeline.build_feature_vector(server_id)
    except Exception:
        logger.debug("피처 벡터 계산 실패: %s", server_id)

    # 앙상블 예측
    ens_result = ensemble.predict(ce_series, features)

    elapsed_ms = (time.time() - start) * 1000
    logger.info(
        "예측: %s → prob=%.4f, risk=%s (%.0fms)",
        server_id, ens_result.failure_probability, ens_result.risk_level, elapsed_ms,
    )

    # 추천 액션
    action = _get_recommended_action(ens_result.risk_level)

    return PredictionResult(
        server_id=server_id,
        timestamp=datetime.now().isoformat(),
        failure_probability=ens_result.failure_probability,
        risk_level=ens_result.risk_level,
        model_scores=ens_result.model_scores,
        model_details=ens_result.model_details,
        recommended_action=action,
    )


def _get_recommended_action(risk_level: str) -> str:
    """리스크 레벨별 추천 액션을 반환한다.

    Args:
        risk_level: NORMAL / WARNING / CRITICAL / RECOVERY.

    Returns:
        추천 액션 문자열.
    """
    actions = {
        "CRITICAL": "즉시 VM 마이그레이션 + Maintenance Mode 전환 + 긴급 알림",
        "WARNING": "ESXi Admission Control VM 배치 차단 + Slack 알림",
        "RECOVERY": "Maintenance Mode 해제 + 복구 알림",
        "NORMAL": "정상 — 추가 조치 불필요",
    }
    return actions.get(risk_level, "상태 모니터링 지속")
