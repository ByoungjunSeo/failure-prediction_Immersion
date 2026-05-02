# 03. 피처 엔지니어링 (총 45개)

## Category A — 메모리 CE 시계열 (20개) ★ 핵심

| 피처명 | 설명 | 계산 방법 |
|---|---|---|
| `ce_count_1h` | 1시간 CE 발생 수 | 합계 |
| `ce_count_24h` | 24시간 CE 누적 | 합계 |
| `ce_count_72h` | 72시간 CE 누적 | 합계 |
| `ce_slope_1h` | 1시간 기울기 | scipy linregress |
| `ce_slope_6h` | 6시간 기울기 | scipy linregress |
| `ce_slope_24h` | 24시간 기울기 | **핵심 피처** |
| `ce_burst_ratio` | CE 급증 비율 | 최근1h / 이전평균 |
| `ce_burst_flag` | 급증 여부 | ratio > 10 → 1 |
| `ce_interval_mean` | 발생 평균 간격 | 이벤트 간 시간 |
| `ce_interval_slope` | 간격 단축 추세 | 음수 = 위험 |
| `ce_acceleration` | 증가 가속도 | 2차 미분 |
| `ce_cumulative_7d` | 7일 누적 | 합계 |
| `mce_page_count_24h` | MCE 페이지 에러 | rasdaemon |
| `mce_uncorrected_flag` | UE 발생 여부 | 0/1 |
| `same_rank_ce_ratio` | 같은 Rank CE 비율 | 공간적 패턴 |
| `same_row_ce_ratio` | 같은 Row CE 비율 | 공간적 패턴 |
| `dimm_slot_id` | DIMM 슬롯 번호 | 원-핫 인코딩 |
| `channel_id` | 메모리 채널 번호 | 원-핫 인코딩 |
| `socket_id` | CPU 소켓 번호 | 원-핫 인코딩 |
| `ce_r2_24h` | 24h 기울기 결정계수 | R² |

## Category B — 하드웨어 환경 (10개)

| 피처명 | 설명 | 소스 |
|---|---|---|
| `cpu_temp_mean_1h` | CPU 온도 1시간 평균 | IPMI |
| `cpu_temp_slope_6h` | CPU 온도 추세 | IPMI |
| `cpu_temp_throttle_cnt` | 쓰로틀링 횟수/h | IPMI SEL |
| `psu_voltage_stddev_1h` | 전압 불안정성 | IPMI |
| `fan_rpm_anomaly` | 팬 이상 여부 | IPMI (0/1) |
| `system_uptime_days` | 서버 가동 일수 | IPMI |
| `smart_reallocated_delta_7d` | SSD 재할당 7일 증가 | smartctl |
| `smart_wear_leveling` | SSD 마모도 | smartctl |
| `power_consumption` | 시스템 전력(W) | IPMI dcmi |
| `ipmi_sel_error_cnt` | SEL 에러 횟수/h | IPMI SEL |

## Category C — 워크로드 (10개)

| 피처명 | 설명 | 소스 |
|---|---|---|
| `cpu_usage_mean_1h` | CPU 사용률 평균 | Telegraf |
| `cpu_usage_max_1h` | CPU 사용률 최대 | Telegraf |
| `memory_used_pct` | 메모리 사용률 | Telegraf |
| `memory_bandwidth_util` | 메모리 대역폭 | perf |
| `cache_miss_rate` | 캐시 미스율 | perf |
| `numa_local_ratio` | NUMA 로컬 접근 비율 | numastat |
| `page_fault_rate` | 페이지 폴트율 | /proc/vmstat |
| `oom_kill_count_24h` | OOM Kill 횟수 | syslog |
| `kernel_panic_cnt_7d` | 커널 패닉 이력 | syslog |
| `swap_usage_pct` | 스왑 사용률 | /proc/meminfo |

## Category D — ESXi 호스트 (5개)

| 피처명 | 설명 | 소스 |
|---|---|---|
| `esxi_vm_count` | VM 수 | pyVmomi |
| `esxi_mem_balloon_sum` | Balloon 메모리 합계 | pyVmomi |
| `esxi_cpu_ready_sum` | CPU 준비 대기 합계 | pyVmomi |
| `esxi_mem_swapped_sum` | 스왑 메모리 합계 | pyVmomi |
| `esxi_mem_overcommit_ratio` | 오버커밋 비율 | pyVmomi |

---

## CE 피처 계산 코드

```python
import numpy as np
import pandas as pd
from scipy import stats

def compute_ce_features(ce_series: pd.Series) -> dict:
    features = {}

    # 집계
    features['ce_count_1h']  = ce_series[-60:].sum()
    features['ce_count_24h'] = ce_series[-1440:].sum()
    features['ce_count_72h'] = ce_series.sum()

    # 기울기
    for window, name in [(60,'1h'), (360,'6h'), (1440,'24h')]:
        s = ce_series[-window:]
        if len(s) > 1 and s.sum() > 0:
            x = np.arange(len(s))
            slope, _, r, _, _ = stats.linregress(x, s.values)
            features[f'ce_slope_{name}'] = slope
            features[f'ce_r2_{name}']    = r ** 2
        else:
            features[f'ce_slope_{name}'] = 0.0
            features[f'ce_r2_{name}']    = 0.0

    # Burst ratio
    recent   = ce_series[-60:].mean()
    baseline = ce_series[:-60].mean() if len(ce_series) > 60 else 0
    features['ce_burst_ratio'] = recent / (baseline + 1e-9)
    features['ce_burst_flag']  = int(features['ce_burst_ratio'] > 10)

    # 발생 간격
    event_idx = ce_series[ce_series > 0].index
    if len(event_idx) > 2:
        intervals = pd.Series(event_idx).diff().dropna()
        features['ce_interval_mean']  = intervals.mean()
        slope, *_ = stats.linregress(range(len(intervals)), intervals)
        features['ce_interval_slope'] = slope
    else:
        features['ce_interval_mean']  = 99999
        features['ce_interval_slope'] = 0.0

    # 가속도
    features['ce_acceleration'] = np.diff(ce_series.values, n=2).mean() \
                                   if len(ce_series) > 2 else 0.0
    return features
```
