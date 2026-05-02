"""Alibaba PAKDD 2021 데이터셋 다운로드 스크립트.

실제 데이터센터 DRAM CE/UE 로그 300만 건.
XGBoost 사전학습 데이터로 활용한다.

사용법:
    python scripts/download_public_data.py
"""

import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path("/opt/failure_prediction/data/alibaba_pakdd2021")

# Alibaba Tianchi 데이터셋 URL
# 참고: https://tianchi.aliyun.com/dataset/132973
# 실제 다운로드는 Tianchi 계정 로그인이 필요할 수 있음
DATASET_INFO = """
Alibaba PAKDD 2021 Memory Failure Prediction Dataset
=====================================================

URL: https://tianchi.aliyun.com/dataset/132973

내용:
  - kernel_errors.csv   : 커널 메모리 에러 로그 (~300만 건)
  - mcelog.csv           : MCE (Machine Check Exception) 로그
  - address_log.csv      : 물리 메모리 주소별 에러 로그
  - failure_labels.csv   : DIMM 장애 레이블 (Positive/Negative)

활용 계획:
  1. failure_labels.csv + kernel_errors.csv → XGBoost 사전학습
  2. TTA 자체 CE 데이터로 파인튜닝 (sample_weight=3.0)
  3. Phase 4에서 Optuna HPO 30 trials

수동 다운로드 방법:
  1. https://tianchi.aliyun.com/dataset/132973 접속
  2. Tianchi 계정으로 로그인
  3. 데이터셋 다운로드
  4. data/alibaba_pakdd2021/ 에 압축 해제
"""


def setup_data_directory() -> None:
    """데이터 디렉토리를 생성하고 안내 파일을 작성한다."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    readme_path = DATA_DIR / "README.md"
    readme_path.write_text(DATASET_INFO)
    logger.info("데이터 디렉토리 생성: %s", DATA_DIR)
    logger.info("README.md 작성 완료")


def check_existing_data() -> bool:
    """이미 다운로드된 데이터가 있는지 확인한다.

    Returns:
        데이터 존재 여부.
    """
    expected_files = [
        "kernel_errors.csv",
        "mcelog.csv",
        "failure_labels.csv",
    ]

    found = []
    for fname in expected_files:
        fpath = DATA_DIR / fname
        if fpath.exists():
            found.append(fname)
            logger.info("발견: %s (%.1f MB)", fname, fpath.stat().st_size / 1e6)

    if found:
        logger.info("기존 데이터 %d/%d 파일 발견", len(found), len(expected_files))
        return len(found) == len(expected_files)

    return False


def try_download() -> bool:
    """데이터셋 다운로드를 시도한다.

    Returns:
        다운로드 성공 여부.
    """
    try:
        import requests

        # Tianchi API는 로그인이 필요하므로 직접 URL 다운로드 시도
        # 실패 시 수동 다운로드 안내
        logger.info("Tianchi 데이터셋 접근 시도...")
        resp = requests.head(
            "https://tianchi.aliyun.com/dataset/132973",
            timeout=10,
            allow_redirects=True,
        )
        logger.info("Tianchi 응답: %d", resp.status_code)

        if resp.status_code == 200:
            logger.info(
                "Tianchi 데이터셋 페이지 접근 가능. "
                "수동 다운로드 후 %s 에 배치하세요.", DATA_DIR
            )
        return False

    except Exception:
        logger.warning("Tianchi 접속 실패. 수동 다운로드가 필요합니다.")
        return False


def create_sample_data() -> None:
    """테스트용 샘플 데이터를 생성한다 (실제 데이터 없을 때)."""
    import numpy as np
    import pandas as pd

    np.random.seed(42)
    n_samples = 10000

    # 샘플 커널 에러 데이터
    kernel_errors = pd.DataFrame({
        "timestamp": pd.date_range("2020-01-01", periods=n_samples, freq="5min"),
        "server_id": np.random.randint(1, 100, n_samples),
        "dimm_id": np.random.randint(0, 16, n_samples),
        "ce_count": np.random.poisson(2, n_samples),
        "ue_count": np.random.binomial(1, 0.001, n_samples),
        "mc": np.random.randint(0, 2, n_samples),
        "channel": np.random.randint(0, 4, n_samples),
        "csrow": np.random.randint(0, 8, n_samples),
    })

    # 장애 레이블
    failure_rate = 0.05
    labels = pd.DataFrame({
        "server_id": range(1, 101),
        "dimm_id": np.random.randint(0, 16, 100),
        "failure": np.random.binomial(1, failure_rate, 100),
        "failure_time": pd.NaT,
    })

    kernel_errors.to_csv(DATA_DIR / "kernel_errors_sample.csv", index=False)
    labels.to_csv(DATA_DIR / "failure_labels_sample.csv", index=False)
    logger.info("샘플 데이터 생성 완료: kernel_errors_sample.csv, failure_labels_sample.csv")


if __name__ == "__main__":
    setup_data_directory()

    if check_existing_data():
        logger.info("데이터셋이 이미 존재합니다.")
        sys.exit(0)

    downloaded = try_download()

    if not downloaded:
        logger.info("수동 다운로드가 필요합니다. 안내는 %s/README.md 참조.", DATA_DIR)
        logger.info("테스트용 샘플 데이터를 생성합니다...")
        create_sample_data()

    sys.exit(0)
