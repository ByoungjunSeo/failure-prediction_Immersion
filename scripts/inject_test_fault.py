"""가상 CE 에러 주입 및 모델별 탐지 검증 스크립트.

정상 CE 시계열에 다양한 패턴의 이상을 주입하고
4개 모델이 각각 탐지하는지 비교 리포트를 생성한다.

사용법:
    python scripts/inject_test_fault.py

테스트 시나리오:
  1. CE 급증 (burst) — 마지막 2시간에 50x 증가
  2. CE 점진적 증가 (ramp) — 24시간에 걸쳐 선형 증가
  3. CE 주기적 패턴 (periodic) — 30분 주기 반복 스파이크
  4. CE 단일 스파이크 (spike) — 1분 동안 극단적 폭증
"""

import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/opt/failure_prediction")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def generate_normal_ce(length: int = 4320) -> np.ndarray:
    """정상 CE 시계열 생성 (72시간, 분 단위).

    Args:
        length: 시계열 길이.

    Returns:
        정상 CE 시계열.
    """
    np.random.seed(42)
    return np.random.poisson(2, length).astype(np.float32)


def inject_burst(base: np.ndarray, duration_min: int = 120, multiplier: int = 50) -> np.ndarray:
    """CE 급증 주입.

    Args:
        base: 기본 시계열.
        duration_min: 급증 지속 시간 (분).
        multiplier: CE 증가 배수.

    Returns:
        이상 주입된 시계열.
    """
    data = base.copy()
    data[-duration_min:] = np.random.poisson(multiplier, duration_min).astype(np.float32)
    return data


def inject_ramp(base: np.ndarray, ramp_hours: int = 24) -> np.ndarray:
    """점진적 CE 증가 주입.

    Args:
        base: 기본 시계열.
        ramp_hours: 증가 구간 (시간).

    Returns:
        이상 주입된 시계열.
    """
    data = base.copy()
    ramp_len = ramp_hours * 60
    ramp = np.linspace(0, 80, ramp_len).astype(np.float32)
    data[-ramp_len:] += ramp
    return data


def inject_periodic(base: np.ndarray, period_min: int = 30, spike_val: float = 100.0) -> np.ndarray:
    """주기적 CE 스파이크 주입.

    Args:
        base: 기본 시계열.
        period_min: 스파이크 주기 (분).
        spike_val: 스파이크 값.

    Returns:
        이상 주입된 시계열.
    """
    data = base.copy()
    # 마지막 6시간에 주기적 스파이크
    for i in range(360):
        if i % period_min == 0:
            data[-(360 - i)] = spike_val
    return data


def inject_spike(base: np.ndarray, spike_val: float = 500.0) -> np.ndarray:
    """단일 극단 스파이크 주입.

    Args:
        base: 기본 시계열.
        spike_val: 스파이크 값.

    Returns:
        이상 주입된 시계열.
    """
    data = base.copy()
    data[-1] = spike_val
    return data


def test_with_chronos(ce_series: pd.Series) -> dict:
    """Chronos로 탐지 테스트.

    Args:
        ce_series: CE 시계열.

    Returns:
        탐지 결과.
    """
    from src.models.chronos_predictor import ChronosPredictor

    predictor = ChronosPredictor(device="cuda:0")
    predictor.load()

    start = time.time()
    result = predictor.predict_ce_anomaly(ce_series)
    elapsed = time.time() - start

    return {**result, "elapsed_ms": elapsed * 1000}


def test_with_moirai(ce_series: pd.Series) -> dict:
    """MOIRAI로 탐지 테스트.

    Args:
        ce_series: CE 시계열.

    Returns:
        탐지 결과.
    """
    from src.models.moirai_predictor import MoiraiPredictor

    predictor = MoiraiPredictor(device="cuda:0", prediction_length=64, context_length=512)
    predictor.load()

    start = time.time()
    result = predictor.detect_anomaly(ce_series)
    elapsed = time.time() - start

    return {**result, "elapsed_ms": elapsed * 1000}


def test_with_xgboost(features: dict) -> dict:
    """XGBoost로 탐지 테스트.

    Args:
        features: 45개 피처.

    Returns:
        탐지 결과.
    """
    from src.models.xgboost_predictor import XGBoostPredictor

    predictor = XGBoostPredictor(device="cpu")
    try:
        predictor.load()
    except FileNotFoundError:
        return {"anomaly_score": 0.0, "risk_level": "N/A", "model": "xgboost", "elapsed_ms": 0}

    start = time.time()
    try:
        prob = predictor.predict(features)
    except ValueError:
        # 피처명 불일치 (샘플 데이터로 학습된 모델)
        logger.warning("XGBoost 피처명 불일치 — 실제 데이터로 재학습 필요")
        return {"anomaly_score": 0.0, "risk_level": "N/A (피처 불일치)", "model": "xgboost", "elapsed_ms": 0}
    elapsed = time.time() - start

    risk = "CRITICAL" if prob >= 0.85 else "WARNING" if prob >= 0.65 else "NORMAL"
    return {"anomaly_score": prob, "risk_level": risk, "model": "xgboost", "elapsed_ms": elapsed * 1000}


def test_with_anomaly_transformer(ce_series: pd.Series) -> dict:
    """Anomaly Transformer로 탐지 테스트.

    Args:
        ce_series: CE 시계열.

    Returns:
        탐지 결과.
    """
    from src.models.anomaly_transformer import AnomalyTransformerPredictor

    predictor = AnomalyTransformerPredictor(infer_device="cuda:0")
    try:
        predictor.load()
    except FileNotFoundError:
        return {"anomaly_score": 0.0, "risk_level": "N/A", "model": "anomaly_transformer", "elapsed_ms": 0}

    start = time.time()
    result = predictor.detect_anomaly(ce_series)
    elapsed = time.time() - start

    return {**result, "elapsed_ms": elapsed * 1000}


def compute_features_from_series(ce_series: pd.Series) -> dict:
    """CE 시계열에서 피처를 계산한다.

    Args:
        ce_series: CE 시계열.

    Returns:
        45개 피처 딕셔너리.
    """
    from src.features.feature_pipeline import FeaturePipeline

    pipeline = FeaturePipeline(vm_base_url="http://mock:8428")
    features = pipeline.compute_ce_features(ce_series)
    features.update(pipeline.compute_hw_features.__wrapped__(pipeline, "mock") if hasattr(pipeline.compute_hw_features, '__wrapped__') else {f"hw_{i}": 0.0 for i in range(10)})

    # 나머지 피처 기본값
    while len(features) < 45:
        features[f"pad_{len(features)}"] = 0.0

    return features


def run_scenario(name: str, ce_data: np.ndarray) -> dict:
    """단일 시나리오를 실행한다.

    Args:
        name: 시나리오 이름.
        ce_data: 이상 주입된 CE 시계열.

    Returns:
        {model: result} 딕셔너리.
    """
    ce_series = pd.Series(ce_data, dtype=np.float64)
    logger.info("=== 시나리오: %s (max=%.0f, last_1h_mean=%.1f) ===", name, ce_data.max(), ce_data[-60:].mean())

    results = {}

    # Chronos
    logger.info("  Chronos 테스트...")
    results["chronos"] = test_with_chronos(ce_series)

    # MOIRAI
    logger.info("  MOIRAI 테스트...")
    results["moirai"] = test_with_moirai(ce_series)

    # XGBoost
    logger.info("  XGBoost 테스트...")
    from src.features.feature_pipeline import FeaturePipeline
    pipeline = FeaturePipeline(vm_base_url="http://mock:8428")
    features = pipeline.compute_ce_features(ce_series)
    hw = {k: 0.0 for k in [
        "cpu_temp_mean_1h", "cpu_temp_slope_6h", "cpu_temp_throttle_cnt",
        "psu_voltage_stddev_1h", "fan_rpm_anomaly", "system_uptime_days",
        "smart_reallocated_delta_7d", "smart_wear_leveling", "power_consumption", "ipmi_sel_error_cnt"
    ]}
    wl = {k: 0.0 for k in [
        "cpu_usage_mean_1h", "cpu_usage_max_1h", "memory_used_pct",
        "memory_bandwidth_util", "cache_miss_rate", "numa_local_ratio",
        "page_fault_rate", "oom_kill_count_24h", "kernel_panic_cnt_7d", "swap_usage_pct"
    ]}
    esxi = {k: 0.0 for k in [
        "esxi_vm_count", "esxi_mem_balloon_sum", "esxi_cpu_ready_sum",
        "esxi_mem_swapped_sum", "esxi_mem_overcommit_ratio"
    ]}
    all_features = {**features, **hw, **wl, **esxi}
    results["xgboost"] = test_with_xgboost(all_features)

    # Anomaly Transformer
    logger.info("  Anomaly Transformer 테스트...")
    results["anomaly_transformer"] = test_with_anomaly_transformer(ce_series)

    return results


def print_report(all_results: dict) -> None:
    """탐지 결과 비교 리포트를 출력한다.

    Args:
        all_results: {scenario: {model: result}} 딕셔너리.
    """
    print("\n" + "=" * 80)
    print("장애 주입 탐지 비교 리포트")
    print("=" * 80)

    for scenario, results in all_results.items():
        print(f"\n{'─' * 60}")
        print(f"시나리오: {scenario}")
        print(f"{'─' * 60}")
        print(f"{'모델':<25} {'스코어':>8} {'리스크':>10} {'시간(ms)':>10}")
        print(f"{'─' * 55}")

        detected_count = 0
        for model, r in results.items():
            score = r.get("anomaly_score", 0.0)
            risk = r.get("risk_level", "N/A")
            elapsed = r.get("elapsed_ms", 0.0)
            detected = risk in ("WARNING", "CRITICAL")
            if detected:
                detected_count += 1
            marker = " *" if detected else ""
            print(f"  {model:<23} {score:>8.4f} {risk:>10} {elapsed:>9.0f}{marker}")

        print(f"\n  탐지 모델: {detected_count}/4", end="")
        if detected_count >= 3:
            print(" [PASS]")
        else:
            print(" [주의: 3개 미만 탐지]")

    print(f"\n{'=' * 80}")
    print("* = 이상 탐지됨 (WARNING 또는 CRITICAL)")
    print("=" * 80)


if __name__ == "__main__":
    base = generate_normal_ce()

    scenarios = {
        "1_burst_2h": inject_burst(base, duration_min=120, multiplier=50),
        "2_ramp_24h": inject_ramp(base, ramp_hours=24),
        "3_periodic_30m": inject_periodic(base, period_min=30),
        "4_single_spike": inject_spike(base, spike_val=500.0),
    }

    all_results = {}
    for name, data in scenarios.items():
        all_results[name] = run_scenario(name, data)

    print_report(all_results)
