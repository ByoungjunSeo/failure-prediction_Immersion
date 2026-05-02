"""XGBoost 장애예측기 재학습 스크립트 (시뮬레이터 자가라벨 방식).

운영 환경(K8s VictoriaMetrics)에 흐르는 CE 시계열을 직접 슬라이싱하여
18 피처를 계산하고, ``ce_count_24h`` 분위수 임계로 양성/음성을 자가라벨링하여
XGBoost를 학습한다. 결과 모델은 운영 분포에 정합된다.

평가지표: F1, AUC-PR, Recall (Accuracy 금지).
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import xgboost as xgb
from sklearn.metrics import average_precision_score, f1_score, recall_score
from sklearn.model_selection import train_test_split

logger = logging.getLogger("retrain_xgboost")

FEATURE_COLS = [
    "ce_count_1h", "ce_count_24h", "ce_count_72h", "ce_slope_24h",
    "ce_burst_ratio", "ce_burst_flag", "ce_max", "ce_std", "ce_acceleration",
    "cpu_temp_mean_1h", "cpu_temp_slope_6h", "psu_voltage_stddev_1h",
    "fan_rpm_anomaly", "system_uptime_days", "power_consumption",
    "cpu_usage_mean_1h", "memory_used_pct", "swap_usage_pct",
]
LABEL_COL = "label"


def compute_features_from_series(arr: np.ndarray) -> dict[str, float]:
    """CE per-minute 시계열에서 18개 피처를 계산.

    하드웨어/워크로드 9개는 NaN. ensemble_app._compute_xgb_features와 동일 로직.
    """
    a = np.asarray(arr, dtype=np.float64)
    n = len(a)
    f: dict[str, float] = {}
    f["ce_count_1h"] = float(a[-60:].sum()) if n else 0.0
    f["ce_count_24h"] = float(a[-1440:].sum()) if n else 0.0
    f["ce_count_72h"] = float(a[-4320:].sum()) if n else 0.0
    win24 = a[-1440:]
    if len(win24) > 1 and win24.sum() > 0:
        x = np.arange(len(win24), dtype=np.float64)
        f["ce_slope_24h"] = float(np.polyfit(x, win24, 1)[0])
    else:
        f["ce_slope_24h"] = 0.0
    rec = float(a[-60:].mean()) if n >= 60 else (float(a.mean()) if n else 0.0)
    base = float(a[:-60].mean()) if n > 60 else 0.0
    br = rec / (base + 1e-9)
    f["ce_burst_ratio"] = br
    f["ce_burst_flag"] = 1 if br > 10 else 0
    f["ce_max"] = float(a.max()) if n else 0.0
    f["ce_std"] = float(a.std()) if n > 1 else 0.0
    f["ce_acceleration"] = float(np.diff(a, n=2).mean()) if n > 2 else 0.0
    for k in (
        "cpu_temp_mean_1h", "cpu_temp_slope_6h", "psu_voltage_stddev_1h",
        "fan_rpm_anomaly", "system_uptime_days", "power_consumption",
        "cpu_usage_mean_1h", "memory_used_pct", "swap_usage_pct",
    ):
        f[k] = float("nan")
    return f


def fetch_simulator_samples(
    vm_url: str,
    servers: list[str],
    history_days: int,
    sample_step_min: int,
    pos_quantile: float,
) -> pd.DataFrame:
    """시뮬레이터 CE 시계열을 슬라이싱하여 자가라벨된 학습 샘플 DataFrame을 만든다."""
    end = int(time.time())
    start = end - history_days * 86400
    rows: list[dict[str, float]] = []
    for srv in servers:
        r = requests.get(
            f"{vm_url}/api/v1/query_range",
            params={
                "query": f'memory_errors_correctable{{server="{srv}"}}',
                "start": start - 4320 * 60,  # 첫 슬라이스 72h 백워드 여유
                "end": end,
                "step": "60s",
            },
            timeout=30,
        )
        result = r.json().get("data", {}).get("result", [])
        if not result or "values" not in result[0]:
            logger.warning("시뮬레이터 데이터 없음: %s", srv)
            continue
        ts_vals = result[0]["values"]
        full = np.array([float(v[1]) for v in ts_vals], dtype=np.float64)
        first_t = int(ts_vals[0][0])
        last_t = int(ts_vals[-1][0])
        t_cur = max(start, first_t + 4320 * 60)
        while t_cur <= last_t:
            idx_end = (t_cur - first_t) // 60
            idx_start = max(0, idx_end - 4320)
            slice_arr = full[idx_start:idx_end]
            if len(slice_arr) >= 60:
                f = compute_features_from_series(slice_arr)
                f["server_id"] = srv
                f["sample_t"] = t_cur
                rows.append(f)
            t_cur += sample_step_min * 60
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    threshold = float(df["ce_count_24h"].quantile(pos_quantile))
    df[LABEL_COL] = (df["ce_count_24h"] > threshold).astype(int)
    logger.info(
        "시뮬레이터 샘플 %d개 (서버 %d × 시점 %d), 양성 임계 ce_count_24h>%.1f, 양성 비율=%.3f",
        len(df), df["server_id"].nunique(), len(df) // max(df["server_id"].nunique(), 1),
        threshold, float(df[LABEL_COL].mean()),
    )
    return df


def train(df: pd.DataFrame, seed: int = 42) -> tuple[xgb.XGBClassifier, dict[str, float]]:
    if df[LABEL_COL].nunique() < 2:
        raise ValueError("자가라벨 결과 단일 클래스 — pos_quantile 또는 history_days를 조정하세요.")

    x = df[FEATURE_COLS].astype(np.float64)
    y = df[LABEL_COL].astype(int)

    x_train, x_val, y_train, y_val = train_test_split(
        x, y, test_size=0.2, stratify=y, random_state=seed,
    )

    pos = max(int((y_train == 1).sum()), 1)
    neg = int((y_train == 0).sum())
    scale_pos = neg / pos

    model = xgb.XGBClassifier(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_weight=3,
        reg_lambda=1.0,
        scale_pos_weight=scale_pos,
        tree_method="hist",
        eval_metric="aucpr",
        early_stopping_rounds=40,
        random_state=seed,
    )
    model.fit(x_train, y_train, eval_set=[(x_val, y_val)], verbose=False)

    proba_val = model.predict_proba(x_val)[:, 1]
    pred_val = (proba_val >= 0.5).astype(int)
    metrics = {
        "f1": float(f1_score(y_val, pred_val)),
        "auc_pr": float(average_precision_score(y_val, proba_val)),
        "recall": float(recall_score(y_val, pred_val)),
        "n_train": int(len(x_train)),
        "n_val": int(len(x_val)),
        "pos_rate_train": float((y_train == 1).mean()),
        "best_iteration": int(getattr(model, "best_iteration", model.n_estimators) or 0),
    }
    return model, metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--vm-url",
        default="http://victoria-metrics-svc.failure-prediction:8428",
    )
    parser.add_argument(
        "--servers", nargs="+",
        default=["vmgnode18", "vmgnode23", "vmgnode26", "vmgnode30"],
    )
    parser.add_argument("--history-days", type=int, default=5)
    parser.add_argument("--sample-step-min", type=int, default=60)
    parser.add_argument("--pos-quantile", type=float, default=0.85)
    parser.add_argument(
        "--output", type=Path,
        default=Path("/opt/failure_prediction/models/checkpoints/xgboost_model.json"),
    )
    parser.add_argument("--metrics-out", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    logger.info("시뮬레이터 데이터 수집: VM=%s servers=%s history=%dd step=%dm",
                args.vm_url, args.servers, args.history_days, args.sample_step_min)
    df = fetch_simulator_samples(
        vm_url=args.vm_url,
        servers=list(args.servers),
        history_days=args.history_days,
        sample_step_min=args.sample_step_min,
        pos_quantile=args.pos_quantile,
    )
    if df.empty:
        raise RuntimeError("시뮬레이터에서 샘플을 가져오지 못했습니다.")

    logger.info("학습 시작 (n=%d, 양성률=%.4f)", len(df), float(df[LABEL_COL].mean()))
    model, metrics = train(df, seed=args.seed)
    logger.info(
        "결과: F1=%.4f AUC-PR=%.4f Recall=%.4f (best_iter=%d)",
        metrics["f1"], metrics["auc_pr"], metrics["recall"], metrics["best_iteration"],
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    model.save_model(str(args.output))
    logger.info("모델 저장: %s", args.output)

    if args.metrics_out:
        args.metrics_out.parent.mkdir(parents=True, exist_ok=True)
        args.metrics_out.write_text(json.dumps(metrics, indent=2))
        logger.info("지표 저장: %s", args.metrics_out)


if __name__ == "__main__":
    main()
