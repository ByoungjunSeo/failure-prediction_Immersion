"""피처 엔지니어링 파이프라인.

VictoriaMetrics에서 원시 데이터를 조회하고
45개 피처 벡터를 계산한다.

Categories:
  A — 메모리 CE 시계열 (20개) ★ 핵심
  B — 하드웨어 환경 (10개)
  C — 워크로드 (10개)
  D — ESXi 호스트 (5개)
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import requests
from scipy import stats

logger = logging.getLogger(__name__)

VM_BASE_URL = "http://10.100.230.72:8428"
VM_TIMEOUT = 30


class FeaturePipeline:
    """45개 피처 벡터 계산 파이프라인.

    Args:
        vm_base_url: VictoriaMetrics HTTP API URL.
        vm_timeout: HTTP 요청 타임아웃(초).
    """

    def __init__(
        self,
        vm_base_url: str = VM_BASE_URL,
        vm_timeout: int = VM_TIMEOUT,
    ):
        self._vm_base_url = vm_base_url.rstrip("/")
        self._vm_timeout = vm_timeout

    # ──────────────────────────────────────────────
    #  공개 API
    # ──────────────────────────────────────────────

    def build_feature_vector(self, server_id: str) -> dict[str, float]:
        """단일 서버의 45개 피처 벡터를 계산한다.

        Args:
            server_id: 서버 식별자 (예: 18AFD199, vmgnode18).

        Returns:
            {피처명: 값} 딕셔너리 (45개).
        """
        features: dict[str, float] = {}

        ce_series = self.fetch_ce_series(server_id, hours=72)
        features.update(self.compute_ce_features(ce_series))
        features.update(self.compute_hw_features(server_id))
        features.update(self.compute_workload_features(server_id))
        features.update(self.compute_esxi_features(server_id))

        logger.info("피처 벡터 계산 완료: %s (%d개)", server_id, len(features))
        return features

    def build_training_dataset(
        self, server_ids: list[str], days: int = 90
    ) -> tuple[pd.DataFrame, pd.Series]:
        """학습용 데이터셋을 구성한다.

        Args:
            server_ids: 대상 서버 ID 리스트.
            days: 수집 기간 (일).

        Returns:
            (X, y) — 피처 DataFrame, 레이블 Series.
        """
        rows = []
        labels = []

        for sid in server_ids:
            try:
                features = self.build_feature_vector(sid)
                rows.append(features)
                label = self._fetch_failure_label(sid)
                labels.append(label)
            except Exception:
                logger.warning("학습 데이터 구성 실패: %s", sid)

        x_df = pd.DataFrame(rows).fillna(0.0)
        y_series = pd.Series(labels, name="failure_label")
        return x_df, y_series

    # ──────────────────────────────────────────────
    #  데이터 조회
    # ──────────────────────────────────────────────

    def fetch_ce_series(self, server_id: str, hours: int = 72) -> pd.Series:
        """VictoriaMetrics에서 CE 시계열을 조회한다.

        Args:
            server_id: 서버 ID.
            hours: 조회 기간 (시간).

        Returns:
            분 단위 CE 카운트 시계열 (길이 = hours * 60).
        """
        query = f'memory_errors{{server="{server_id}"}}'
        end = datetime.now()
        start = end - timedelta(hours=hours)

        try:
            data = self._query_range(query, start, end, step="1m")
            if data:
                values = [float(v[1]) for v in data[0].get("values", [])]
                series = pd.Series(values, dtype=np.float64)
                return series
        except Exception:
            logger.warning("CE 시계열 조회 실패: %s", server_id)

        # 조회 실패 시 0으로 채운 시계열 반환
        return pd.Series(np.zeros(hours * 60), dtype=np.float64)

    def _fetch_metric(
        self, query: str, hours: int = 1, step: str = "1m"
    ) -> pd.Series:
        """VictoriaMetrics에서 메트릭 시계열을 조회한다.

        Args:
            query: PromQL 쿼리.
            hours: 조회 기간.
            step: 스텝 간격.

        Returns:
            메트릭 시계열.
        """
        end = datetime.now()
        start = end - timedelta(hours=hours)

        try:
            data = self._query_range(query, start, end, step=step)
            if data:
                values = [float(v[1]) for v in data[0].get("values", [])]
                return pd.Series(values, dtype=np.float64)
        except Exception:
            logger.debug("메트릭 조회 실패: %s", query)

        return pd.Series(dtype=np.float64)

    def _query_range(
        self, query: str, start: datetime, end: datetime, step: str = "1m"
    ) -> list[dict]:
        """VictoriaMetrics range query API를 호출한다.

        Args:
            query: PromQL 쿼리.
            start: 시작 시간.
            end: 종료 시간.
            step: 스텝 간격.

        Returns:
            result 리스트.

        Raises:
            requests.RequestException: HTTP 요청 실패.
        """
        url = f"{self._vm_base_url}/api/v1/query_range"
        params = {
            "query": query,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "step": step,
        }

        try:
            resp = requests.get(url, params=params, timeout=self._vm_timeout)
            resp.raise_for_status()
            body = resp.json()
            return body.get("data", {}).get("result", [])
        except requests.Timeout:
            logger.error("VictoriaMetrics 타임아웃: %s", query)
            raise
        except requests.RequestException:
            logger.error("VictoriaMetrics 요청 실패: %s", query)
            raise

    # ──────────────────────────────────────────────
    #  Category A — CE 시계열 피처 (20개)
    # ──────────────────────────────────────────────

    def compute_ce_features(self, ce_series: pd.Series) -> dict[str, float]:
        """CE 시계열에서 20개 피처를 계산한다.

        Args:
            ce_series: 분 단위 CE 카운트 시계열 (최대 72시간 = 4320분).

        Returns:
            {피처명: 값} 딕셔너리 (20개).
        """
        f: dict[str, float] = {}

        # 집계
        f["ce_count_1h"] = float(ce_series[-60:].sum()) if len(ce_series) >= 60 else float(ce_series.sum())
        f["ce_count_24h"] = float(ce_series[-1440:].sum()) if len(ce_series) >= 1440 else float(ce_series.sum())
        f["ce_count_72h"] = float(ce_series.sum())

        # 기울기 (1h, 6h, 24h) + R²는 24h만
        for window, name in [(60, "1h"), (360, "6h"), (1440, "24h")]:
            s = ce_series[-window:] if len(ce_series) >= window else ce_series
            if len(s) > 1 and s.sum() > 0:
                x = np.arange(len(s))
                slope, _, r, _, _ = stats.linregress(x, s.values)
                f[f"ce_slope_{name}"] = float(slope)
                if name == "24h":
                    f["ce_r2_24h"] = float(r ** 2)
            else:
                f[f"ce_slope_{name}"] = 0.0
                if name == "24h":
                    f["ce_r2_24h"] = 0.0

        # Burst ratio
        recent = float(ce_series[-60:].mean()) if len(ce_series) >= 60 else 0.0
        baseline = float(ce_series[:-60].mean()) if len(ce_series) > 60 else 0.0
        f["ce_burst_ratio"] = recent / (baseline + 1e-9)
        f["ce_burst_flag"] = 1.0 if f["ce_burst_ratio"] > 10 else 0.0

        # 발생 간격
        event_idx = ce_series[ce_series > 0].index.tolist()
        if len(event_idx) > 2:
            intervals = np.diff(event_idx).astype(float)
            f["ce_interval_mean"] = float(intervals.mean())
            if len(intervals) > 1:
                slope_val, *_ = stats.linregress(range(len(intervals)), intervals)
                f["ce_interval_slope"] = float(slope_val)
            else:
                f["ce_interval_slope"] = 0.0
        else:
            f["ce_interval_mean"] = 99999.0
            f["ce_interval_slope"] = 0.0

        # 가속도
        if len(ce_series) > 2:
            f["ce_acceleration"] = float(np.diff(ce_series.values, n=2).mean())
        else:
            f["ce_acceleration"] = 0.0

        # 7일 누적
        f["ce_cumulative_7d"] = float(ce_series.sum())  # 72h 내에서 가능한 최대

        # MCE / UE 관련 (rasdaemon 기반, 별도 조회)
        f["mce_page_count_24h"] = 0.0
        f["mce_uncorrected_flag"] = 0.0

        # 공간적 패턴 (DIMM 구조 기반, 기본값)
        f["same_rank_ce_ratio"] = 0.0
        f["same_row_ce_ratio"] = 0.0

        # DIMM/채널/소켓 ID (기본값, 실제 수집 시 설정)
        f["dimm_slot_id"] = 0.0
        f["channel_id"] = 0.0
        f["socket_id"] = 0.0

        # r2_24h는 위에서 이미 계산됨 — 총 20개 확인
        return f

    # ──────────────────────────────────────────────
    #  Category B — 하드웨어 환경 피처 (10개)
    # ──────────────────────────────────────────────

    def compute_hw_features(self, server_id: str) -> dict[str, float]:
        """하드웨어 환경 10개 피처를 계산한다.

        Args:
            server_id: 서버 ID.

        Returns:
            {피처명: 값} 딕셔너리 (10개).
        """
        f: dict[str, float] = {}

        # CPU 온도
        temp_series = self._fetch_metric(
            f'ipmi_temperature{{server="{server_id}", name=~"CPU.*Temp"}}', hours=6
        )
        if len(temp_series) > 0:
            f["cpu_temp_mean_1h"] = float(temp_series[-60:].mean()) if len(temp_series) >= 60 else float(temp_series.mean())
            if len(temp_series) > 1:
                x = np.arange(len(temp_series))
                slope, *_ = stats.linregress(x, temp_series.values)
                f["cpu_temp_slope_6h"] = float(slope)
            else:
                f["cpu_temp_slope_6h"] = 0.0
        else:
            f["cpu_temp_mean_1h"] = 0.0
            f["cpu_temp_slope_6h"] = 0.0

        f["cpu_temp_throttle_cnt"] = 0.0  # SEL 이벤트 기반

        # 전압 안정성
        volt_series = self._fetch_metric(
            f'ipmi_voltage{{server="{server_id}"}}', hours=1
        )
        f["psu_voltage_stddev_1h"] = float(volt_series.std()) if len(volt_series) > 1 else 0.0

        # 팬 이상
        fan_series = self._fetch_metric(
            f'ipmi_fan{{server="{server_id}"}}', hours=1
        )
        f["fan_rpm_anomaly"] = 0.0
        if len(fan_series) > 0:
            f["fan_rpm_anomaly"] = 1.0 if fan_series.min() < 500 else 0.0

        # 업타임
        uptime_series = self._fetch_metric(
            f'system_uptime{{server="{server_id}"}}', hours=1
        )
        f["system_uptime_days"] = float(uptime_series.iloc[-1] / 86400) if len(uptime_series) > 0 else 0.0

        # SMART
        f["smart_reallocated_delta_7d"] = 0.0  # smartctl delta 기반
        f["smart_wear_leveling"] = 100.0  # 기본값

        # 전력
        power_series = self._fetch_metric(
            f'ipmi_power{{server="{server_id}"}}', hours=1
        )
        f["power_consumption"] = float(power_series.mean()) if len(power_series) > 0 else 0.0

        # SEL 에러
        f["ipmi_sel_error_cnt"] = 0.0

        return f

    # ──────────────────────────────────────────────
    #  Category C — 워크로드 피처 (10개)
    # ──────────────────────────────────────────────

    def compute_workload_features(self, server_id: str) -> dict[str, float]:
        """워크로드 10개 피처를 계산한다.

        Args:
            server_id: 서버 ID.

        Returns:
            {피처명: 값} 딕셔너리 (10개).
        """
        f: dict[str, float] = {}

        # CPU 사용률
        cpu_series = self._fetch_metric(
            f'cpu_usage_idle{{server="{server_id}", cpu="cpu-total"}}', hours=1
        )
        if len(cpu_series) > 0:
            cpu_used = 100.0 - cpu_series  # idle → used
            f["cpu_usage_mean_1h"] = float(cpu_used.mean())
            f["cpu_usage_max_1h"] = float(cpu_used.max())
        else:
            f["cpu_usage_mean_1h"] = 0.0
            f["cpu_usage_max_1h"] = 0.0

        # 메모리 사용률
        mem_series = self._fetch_metric(
            f'mem_used_percent{{server="{server_id}"}}', hours=1
        )
        f["memory_used_pct"] = float(mem_series.mean()) if len(mem_series) > 0 else 0.0

        # 메모리 대역폭/캐시 (perf 기반, 기본값)
        f["memory_bandwidth_util"] = 0.0
        f["cache_miss_rate"] = 0.0

        # NUMA
        f["numa_local_ratio"] = 1.0  # 기본 100% 로컬

        # 페이지 폴트
        f["page_fault_rate"] = 0.0

        # OOM / 커널 패닉 (syslog 기반)
        f["oom_kill_count_24h"] = 0.0
        f["kernel_panic_cnt_7d"] = 0.0

        # 스왑 사용률
        swap_series = self._fetch_metric(
            f'swap_used_percent{{server="{server_id}"}}', hours=1
        )
        f["swap_usage_pct"] = float(swap_series.mean()) if len(swap_series) > 0 else 0.0

        return f

    # ──────────────────────────────────────────────
    #  Category D — ESXi 호스트 피처 (5개)
    # ──────────────────────────────────────────────

    def compute_esxi_features(self, host_id: str) -> dict[str, float]:
        """ESXi 호스트 5개 피처를 계산한다.

        Args:
            host_id: ESXi 호스트 ID (예: vmgnode18).

        Returns:
            {피처명: 값} 딕셔너리 (5개).
        """
        f: dict[str, float] = {}

        # VM 수
        vm_cnt = self._fetch_metric(
            f'esxi_vm_count{{host="{host_id}"}}', hours=1
        )
        f["esxi_vm_count"] = float(vm_cnt.iloc[-1]) if len(vm_cnt) > 0 else 0.0

        # Balloon 메모리
        balloon = self._fetch_metric(
            f'esxi_vm_balloon{{host="{host_id}"}}', hours=1
        )
        f["esxi_mem_balloon_sum"] = float(balloon.iloc[-1]) if len(balloon) > 0 else 0.0

        # CPU Ready
        ready = self._fetch_metric(
            f'esxi_cpu_ready{{host="{host_id}"}}', hours=1
        )
        f["esxi_cpu_ready_sum"] = float(ready.iloc[-1]) if len(ready) > 0 else 0.0

        # 스왑 메모리
        swapped = self._fetch_metric(
            f'esxi_mem_swapped{{host="{host_id}"}}', hours=1
        )
        f["esxi_mem_swapped_sum"] = float(swapped.iloc[-1]) if len(swapped) > 0 else 0.0

        # 오버커밋 비율
        overcommit = self._fetch_metric(
            f'esxi_mem_overcommit_ratio{{host="{host_id}"}}', hours=1
        )
        f["esxi_mem_overcommit_ratio"] = float(overcommit.iloc[-1]) if len(overcommit) > 0 else 0.0

        return f

    # ──────────────────────────────────────────────
    #  내부 헬퍼
    # ──────────────────────────────────────────────

    def _fetch_failure_label(self, server_id: str) -> int:
        """서버의 장애 레이블을 조회한다.

        Args:
            server_id: 서버 ID.

        Returns:
            0 (정상) 또는 1 (장애).
        """
        # TODO: PostgreSQL failure_events 테이블에서 조회
        return 0
