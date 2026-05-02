# HPC 서버 장애 예측 시스템 — Claude Code 개발 지시

> AI 기반 메모리 Fault 예측 플랫폼 | TTA
> 상세 문서: `docs/` 디렉토리 참고

---

## 🖥️ 서버 구성

| 역할 | 호스트명 | IP | 접속 |
|---|---|---|---|
| **AI 학습 (현재 서버)** | node3 (18AFD199) | 10.100.230.71 | 로컬 실행 |
| 모니터링 | node2 (18AFD226) | 10.100.230.72 | `ssh hpcdev@10.100.230.72` |
| 데이터 저장 | node1 (18AFD201) | 10.100.230.70 | `ssh hpcdev@10.100.230.70` |
| ESXi vmgnode18 | - | 10.148.148.118 | SSH(읽기) + pyVmomi |
| ESXi vmgnode23 | - | 10.148.148.123 | SSH(읽기) + pyVmomi |
| ESXi vmgnode26 | - | 10.148.148.126 | SSH(읽기) + pyVmomi |
| ESXi vmgnode30 | - | 10.148.148.130 | SSH(읽기) + pyVmomi |

> ⚠️ vmgnode17 (10.148.148.117): 접속 장애로 제외
> ⚠️ vCenter 미사용 — ESXi 직접 접근 (SSH 읽기 허용 + pyVmomi API)
> 🚫 ESXi SSH는 읽기/모니터링 목적만 허용. 패키지 설치·설정 변경 절대 금지

---

## 👤 계정

- **개발 계정**: `hpcdev` (sudo 권한)
- root 계정으로 개발 작업 금지
- node1/node2: SSH 키 인증 (`ssh hpcdev@10.100.230.7x`)
- ESXi: `root / VMware!0` (SSH 읽기 + pyVmomi API)

---

## 🤖 GPU 규칙 (node3 A100 80GB × 2)

- `cuda:0` → FastAPI 실시간 추론 + Chronos/MOIRAI 추론 전담
- `cuda:1` → 모델 파인튜닝 / 재학습 전담
- 대규모 재학습 시 DDP로 2장 투입 가능 (월 1회)

---

## 🧠 모델 전략 — 오픈소스 모델 활용 (핵심)

```
처음부터 모델을 만들지 않습니다.
검증된 오픈소스 모델을 활용해 개발 기간을 단축합니다.
레이블 없이도 3주차부터 이상탐지 가동 가능.

━━ 1단계: Zero-shot 즉시 가동 ━━━━━━━━━━━━━━━━━━━━━
  Chronos (Amazon, 2024)
    - pip install chronos-forecasting
    - T5 기반 시계열 예측 모델
    - CE 72시간 시계열 → 향후 24시간 급증 예측
    - 레이블 불필요, 즉시 적용

  MOIRAI (Salesforce, 2024)
    - pip install uni2ts
    - Zero-shot 시계열 이상탐지
    - CE 패턴 이상 구간 탐지

━━ 2단계: 공개 데이터로 파인튜닝 ━━━━━━━━━━━━━━━━━━
  Alibaba PAKDD 2021 데이터셋
    - DRAM CE/UE 로그 300만 건 (실제 데이터센터)
    - https://tianchi.aliyun.com/dataset/132973
    - XGBoost 사전학습 → TTA 데이터로 파인튜닝

  Anomaly Transformer (ICLR 2022)
    - GitHub: thuml/Anomaly-Transformer
    - 시계열 이상탐지 SOTA 구조
    - CE 시계열에 맞게 파인튜닝

━━ 3단계: 앙상블 (6개월 후, 자체 데이터 충분 시) ━━
  Chronos/MOIRAI × 0.4
  + XGBoost (파인튜닝) × 0.35
  + Anomaly Transformer × 0.25
```

---

## 🚦 리스크 대응

| 확률 | 레벨 | 자동 대응 |
|---|---|---|
| 0.65 ~ 0.85 | WARNING | ESXi Admission Control VM 배치 차단 + Slack 알림 |
| 0.85 이상 | CRITICAL | ESXi Maintenance Mode 전환 + 긴급 알림 |
| 0.30 이하 | RECOVERY | Maintenance Mode 해제 + 복구 알림 |

---

## 📏 코드 규칙

- docstring 필수 (Args / Returns / Raises)
- 외부 API 호출: try/except + timeout 필수
- 로깅: `logging` 모듈, `print()` 금지
- 설정값: `configs/` YAML 분리, 하드코딩 금지
- 비밀번호/키: `.env` 파일, 코드 직접 기재 금지
- 평가지표: Accuracy 금지 → F1, AUC-PR, Recall
- 신규 함수: `tests/` 단위 테스트 동시 작성

---

## 🔄 현재 개발 단계

```
현재 Phase : P0 — 개발 환경 셋업
완료 Phase  : 없음
업데이트    : 2025-04-04
```

> Phase 완료 시 이 섹션을 업데이트하세요.

---

## 📁 상세 문서

| 문서 | 경로 | 내용 |
|---|---|---|
| 환경 구성 | `docs/01_environment.md` | 서버 스펙, 계정 설정, ESXi SSH 접근 |
| 데이터 수집 | `docs/02_data_collection.md` | EDAC, IPMI, SMART, ESXi SSH 수집 |
| 피처 엔지니어링 | `docs/03_features.md` | 45개 피처 정의 |
| **모델 전략** | `docs/04_model.md` | **오픈소스 모델 활용 전략 (핵심)** |
| 추론 API | `docs/05_api.md` | FastAPI 엔드포인트, 스케줄러 |
| ESXi 연동 | `docs/06_esxi.md` | SSH + pyVmomi 연동, 대응 로직 |
| Phase 지시 | `docs/07_phases.md` | Phase별 Claude Code 입력 지시문 |
| 제약 사항 | `docs/08_constraints.md` | 금지 사항, 검증 체크리스트 |
-e 

---

# 01. 환경 구성

## 개발 서버 3대 상세 스펙

### node3 — AI 학습 서버 ★ Claude Code 실행 (18AFD199)

| 항목 | 내용 |
|---|---|
| Hostname | 18AFD199 |
| IP / BMC | 10.100.230.71 / 10.100.231.71 |
| 접속 | `ssh hpcdev@10.100.230.71` |
| OS | Rocky Linux 9.7 |
| CPU | Intel Xeon Gold 6140 × 2소켓 (36코어 72스레드) |
| GPU | NVIDIA A100 **80GB** HBM2 × 2장 (PCIe) |
| RAM | 64GB DDR4 × 8 = **512GB** |
| Disk | TOSHIBA 600GB × 2 + Micron NVMe 3.84TB × 1 |
| NIC | Intel X722 1GbE + 1GbE 2포트 |
| 서비스 | MLflow, FastAPI, PyTorch, XGBoost, Chronos, MOIRAI, Jupyter, Claude Code |

### node2 — 데이터 수집 및 모니터링 (18AFD226)

| 항목 | 내용 |
|---|---|
| Hostname | 18AFD226 |
| IP / BMC | 10.100.230.72 / 10.100.231.72 |
| 접속 | `ssh hpcdev@10.100.230.72` |
| OS | Rocky Linux 9.7 |
| CPU | Intel Xeon Gold 6140 × 2소켓 |
| RAM | 512GB (64GB × 8) |
| Disk | TOSHIBA 600GB × 2 + Micron NVMe 3.84TB × 1 |
| RAID | Broadcom / LSI MegaRAID SAS-3 3108 |
| 서비스 | Telegraf, VictoriaMetrics, Grafana, Alertmanager, PostgreSQL |

### node1 — 데이터 저장 및 백업 (18AFD201)

| 항목 | 내용 |
|---|---|
| Hostname | 18AFD201 |
| IP / BMC | 10.100.230.70 / 10.100.231.70 |
| 접속 | `ssh hpcdev@10.100.230.70` |
| OS | Rocky Linux 9.7 |
| CPU | Intel Xeon Gold 6140 × 2소켓 |
| RAM | 512GB (64GB × 8) |
| Disk | TOSHIBA 600GB × 2 + Micron NVMe 3.84TB × 1 |
| NIC | Intel X710 10GbE SFP+ (다른 노드 대비 추가) |
| 서비스 | VictoriaMetrics 장기보존, MinIO, PostgreSQL Replica |

---

## ESXi 호스트 (4대 운영)

> ✅ SSH 접속 허용 (읽기/모니터링 목적)
> 🚫 패키지 설치, 설정 파일 수정, 서비스 변경 절대 금지
> ❌ vmgnode17 (10.148.148.117): 접속 장애로 제외

| 호스트 | IP | BMC IP | ESXi 계정 | BMC 계정 |
|---|---|---|---|---|
| vmgnode18 | 10.148.148.118 | 172.31.201.118 | root / VMware!0 | admin / admin |
| vmgnode23 | 10.148.148.123 | 172.31.201.123 | root / VMware!0 | admin / admin |
| vmgnode26 | 10.148.148.126 | 172.31.201.126 | root / VMware!0 | admin / admin |
| vmgnode30 | 10.148.148.130 | 172.31.201.130 | root / VMware!0 | admin / admin |

### ESXi SSH로 가능한 추가 수집

```bash
# SSH로 가져올 수 있는 정보 (pyVmomi 대비 추가)
ssh root@10.148.148.118 'esxcli hardware memory get'
ssh root@10.148.148.118 'esxcli system stats memory get'
ssh root@10.148.148.118 'esxcli hardware ipmi fru list'   # DIMM 물리 정보
ssh root@10.148.148.118 'cat /var/log/vmkernel.log | grep -i "memory\|error" | tail -200'
ssh root@10.148.148.118 'vim-cmd hostsvc/hostsummary'
ssh root@10.148.148.118 'esxcli system syslog config get'
```

---

## 계정 초기 설정 (최초 1회, root로 실행)

```bash
# ── 3대 서버 모두 동일 실행 (node1, node2, node3) ──
useradd -m -s /bin/bash hpcdev
passwd hpcdev
usermod -aG wheel hpcdev
echo 'hpcdev ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers.d/hpcdev
chmod 440 /etc/sudoers.d/hpcdev
mkdir -p /opt/failure_prediction
chown -R hpcdev:hpcdev /opt/failure_prediction
```

```bash
# ── node3에서만: SSH 키 배포 ──
su - hpcdev
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''
ssh-copy-id hpcdev@10.100.230.70   # node1
ssh-copy-id hpcdev@10.100.230.72   # node2

# 연결 확인
ssh hpcdev@10.100.230.70 'echo node1 ok'
ssh hpcdev@10.100.230.72 'echo node2 ok'
```

---

## node3 ML 스택 설치

```bash
conda create -n failure_pred python=3.11 -y
conda activate failure_pred

# PyTorch (CUDA 12.1)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

# 오픈소스 시계열 모델 (핵심)
pip install chronos-forecasting          # Amazon Chronos
pip install uni2ts                       # Salesforce MOIRAI
pip install git+https://github.com/thuml/Anomaly-Transformer  # Anomaly Transformer

# ML 패키지
pip install xgboost lightgbm scikit-learn imbalanced-learn shap optuna
pip install mlflow fastapi uvicorn
pip install pandas numpy scipy
pip install apscheduler prometheus-client
pip install pyVmomi paramiko pyyaml python-dotenv
pip install pytest pytest-httpserver

# GPU 확인
python -c "import torch; print(torch.cuda.device_count(), torch.cuda.get_device_name(0))"
# 기대: 2  NVIDIA A100 80GB PCIe
```

---

## configs/esxi.yaml

```yaml
# vCenter 미사용 — ESXi 직접 접근
# vmgnode17: 접속 장애로 제외
esxi_hosts:
  - id: vmgnode18
    ip: 10.148.148.118
    bmc_ip: 172.31.201.118
    username: root
    password: ${ESXI_PASSWORD}
  - id: vmgnode23
    ip: 10.148.148.123
    bmc_ip: 172.31.201.123
    username: root
    password: ${ESXI_PASSWORD}
  - id: vmgnode26
    ip: 10.148.148.126
    bmc_ip: 172.31.201.126
    username: root
    password: ${ESXI_PASSWORD}
  - id: vmgnode30
    ip: 10.148.148.130
    bmc_ip: 172.31.201.130
    username: root
    password: ${ESXI_PASSWORD}

collection:
  interval_seconds: 60
  timeout_seconds: 10
  retry_count: 3
  ssh_key_path: ~/.ssh/id_ed25519   # ESXi SSH 키 (선택)
```

## .env 파일

```bash
ESXI_PASSWORD=VMware!0
DB_PASSWORD=your_pg_password
SLACK_WEBHOOK_URL=https://hooks.slack.com/...
MLFLOW_TRACKING_URI=http://localhost:5000
MINIO_ENDPOINT=http://10.100.230.70:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
```
-e 

---

# 02. 데이터 수집

## 수집 대상 및 도구

| 소스 | 도구 | 주기 | 비고 |
|---|---|---|---|
| DRAM CE/UE 에러 | rasdaemon + edac-util | 60초 | node3 (18AFD199) 로컬 |
| CPU 온도/전압/팬/전력 | ipmitool (BMC) | 60초 | node3 (18AFD199) BMC |
| SSD/HDD SMART | smartctl | 600초 | node3 (18AFD199) 로컬 |
| ESXi 호스트 메트릭 | pyVmomi (API) | 60초 | 4대 직접 연결 |
| ESXi 커널 로그/DIMM | **SSH 읽기** | 300초 | vmkernel.log |
| OS 시스템 메트릭 | Telegraf (node2 / 18AFD226) | 60초 | VictoriaMetrics 저장 |

---

## rasdaemon + edac-util 설치 (node3 / 18AFD199)

```bash
sudo dnf install -y rasdaemon edac-utils
sudo systemctl enable --now rasdaemon

# DIMM 구조 확인
edac-util -v
# 예: mc0/csrow0/ch0: 0 Uncorrected Errors, 2 Corrected Errors

# rasdaemon DB 확인
ls /var/lib/rasdaemon/ras-mc_event.db
```

---

## src/collectors/edac_collector.py 명세

```python
"""
수집 항목:
  - edac-util: mc/csrow/channel별 CE/UE 카운트
  - rasdaemon SQLite: 신규 MCE 이벤트 (last_id 관리)
  - dmidecode --type 17: DIMM 슬롯 물리 위치 매핑

DataClass:
  DimmLocation(mc, csrow, channel, slot_label, socket_id)
  MemoryErrorEvent(timestamp, dimm_loc, ce_count, ue_count, address, error_type)
"""
```

---

## src/collectors/esxi_ssh_collector.py 명세 (신규)

```python
"""
ESXi SSH 읽기로 추가 수집 (pyVmomi 대비 더 많은 정보)

수집 항목:
  1. vmkernel 로그 (메모리 에러 키워드 필터)
     ssh root@{ip} 'grep -i "memory\|DRAM\|ECC\|correctable" /var/log/vmkernel.log | tail -500'

  2. DIMM 물리 정보
     ssh root@{ip} 'esxcli hardware ipmi fru list'

  3. 메모리 상세 통계
     ssh root@{ip} 'esxcli hardware memory get'
     ssh root@{ip} 'esxcli system stats memory get'

  4. 호스트 요약
     ssh root@{ip} 'vim-cmd hostsvc/hostsummary'

주의:
  - 읽기 전용 명령만 허용
  - 설치/변경 명령 절대 사용 금지
  - paramiko SSH 클라이언트 사용
  - timeout=10초 설정 필수
"""
```

---

## src/collectors/esxi_collector.py 명세 (pyVmomi)

```python
"""
pyVmomi 직접 연결 (vCenter 미사용)

수집 메트릭:
  호스트: cpu.usage, mem.usage, mem.consumed, mem.swapinRate,
          net.errorsRx, net.errorsTx, power.power, sys.uptime
  VM 집계: vm_count, mem_balloon_sum, cpu_ready_sum, mem_swapped_sum

연결:
  SmartConnect(host=esxi_ip, user='root', pwd='VMware!0',
               sslContext=no_verify_context)
"""
```

---

## Telegraf 설정 (node2 / 18AFD226 배포)

```toml
[global_tags]
  datacenter = "TTA-HPC"

[agent]
  interval = "60s"

[[outputs.influxdb_v2]]
  urls = ["http://10.100.230.72:8428"]
  bucket = "hpc_metrics"

[[inputs.ipmi_sensor]]
  servers = ["root:qwe123@localhost"]
  metric_version = 2

[[inputs.smart]]
  path = "/usr/bin/smartctl"
  interval = "600s"

[[inputs.cpu]]
  percpu = true
[[inputs.mem]]
[[inputs.disk]]
  ignore_fs = ["tmpfs", "devtmpfs"]

[[inputs.exec]]
  commands = ["/opt/failure_prediction/scripts/collect_edac.sh"]
  timeout = "10s"
  data_format = "influx"
  interval = "60s"
```

---

## VictoriaMetrics 메트릭 네이밍

```
memory_errors{server="18AFD199", mc="0", channel="0"}  ce=5, ue=0
ipmi_temperature{server="18AFD199", name="CPU1 Temp"}  value=72.0
esxi_cpu_usage{host="vmgnode18"}                    value=45.2
esxi_vm_balloon{host="vmgnode18"}                   value=2048
esxi_vmkernel_error_cnt{host="vmgnode18"}           value=3    ← SSH 수집 신규
```
-e 

---

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
-e 

---

# 04. 모델 전략 — 오픈소스 모델 활용

## 핵심 원칙

> 처음부터 모델을 만들지 않습니다.
> 검증된 오픈소스 모델을 활용해 개발 기간을 단축하고,
> 레이블 없이도 3주차부터 이상탐지를 가동합니다.

```
개발 기간: 9주 → 6~7주로 단축
레이블:    없어도 즉시 시작 가능 (Zero-shot)
공개 데이터: Alibaba PAKDD 2021 (실제 DRAM 로그 300만 건)
```

---

## 3단계 모델 전략

### 1단계 — Zero-shot 즉시 가동 (Week 3부터)

레이블 없이 바로 CE 시계열 이상탐지를 시작합니다.

#### Chronos (Amazon, 2024)

```python
pip install chronos-forecasting

from chronos import ChronosPipeline
import torch, pandas as pd

# 모델 로드 (cuda:0 추론 전담 GPU)
pipeline = ChronosPipeline.from_pretrained(
    "amazon/chronos-t5-small",   # 46M 파라미터, 빠른 추론
    device_map="cuda:0",
    torch_dtype=torch.float16,
)
# 필요 시 더 큰 모델:
# "amazon/chronos-t5-base"   (200M)
# "amazon/chronos-t5-large"  (710M)

def predict_ce_anomaly(ce_series: pd.Series) -> dict:
    """
    CE 72시간 시계열 → 향후 24시간 예측
    예측값이 현재 평균보다 크게 높으면 장애 예측
    """
    context = torch.tensor(ce_series.values, dtype=torch.float32)

    forecast = pipeline.predict(
        context=context.unsqueeze(0),
        prediction_length=24 * 60,   # 24시간 (분 단위)
        num_samples=20,
    )
    # forecast shape: (1, num_samples, prediction_length)

    median_forecast = forecast[0].median(dim=0).values
    current_mean    = ce_series[-60:].mean()   # 최근 1시간 평균
    predicted_peak  = median_forecast.max().item()

    anomaly_score = predicted_peak / (current_mean + 1e-9)

    return {
        "anomaly_score":   anomaly_score,
        "predicted_peak":  predicted_peak,
        "risk_level":      "CRITICAL" if anomaly_score > 10
                           else "WARNING" if anomaly_score > 3
                           else "NORMAL",
    }
```

#### MOIRAI (Salesforce, 2024)

```python
pip install uni2ts

from uni2ts.model.moirai import MoiraiForecast, MoiraiConfig
from gluonts.dataset.pandas import PandasDataset
import torch

# 모델 로드
model = MoiraiForecast.from_pretrained(
    "Salesforce/moirai-1.0-R-small",   # 소형 빠른 모델
    prediction_length=1440,             # 24시간
    context_length=4320,                # 72시간
    patch_size=32,
    num_samples=20,
    target_dim=1,
    feat_dynamic_real_dim=0,
    past_feat_dynamic_real_dim=0,
).to("cuda:0")

def detect_anomaly_moirai(ce_series: pd.Series) -> float:
    """
    Zero-shot 이상탐지 스코어 반환
    높을수록 이상
    """
    ds = PandasDataset(dict(target=ce_series))
    predictor = model.create_predictor(batch_size=32)
    forecasts = list(predictor.predict(ds))

    # 예측 불확실성 = 이상 스코어
    forecast_std = forecasts[0].samples.std(axis=0).mean()
    return float(forecast_std)
```

---

### 2단계 — 공개 데이터 파인튜닝 (Week 4~5)

#### Alibaba PAKDD 2021 데이터셋

```python
"""
데이터셋 정보:
  URL: https://tianchi.aliyun.com/dataset/132973
  내용: 실제 데이터센터 DRAM CE/UE 로그 300만 건
  포함: kernel log, mcelog, address log, 장애 레이블

다운로드 후 활용:
  1. 데이터 전처리 → TTA 피처 형식으로 변환
  2. XGBoost 사전학습
  3. TTA 자체 데이터로 파인튜닝 (Transfer Learning)
"""

import pandas as pd
import xgboost as xgb
from sklearn.model_selection import StratifiedKFold

def train_xgboost_with_public_data(
    public_X, public_y,   # Alibaba 데이터
    tta_X=None, tta_y=None  # TTA 자체 데이터 (쌓이면 추가)
):
    # 클래스 불균형
    scale_pos = (public_y == 0).sum() / (public_y == 1).sum()

    params = {
        "device":           "cuda",   # cuda:1 (학습 전담)
        "tree_method":      "hist",
        "max_depth":        6,
        "learning_rate":    0.01,
        "n_estimators":     1000,
        "scale_pos_weight": scale_pos,
        "eval_metric":      "aucpr",
        "subsample":        0.8,
        "colsample_bytree": 0.8,
        "early_stopping_rounds": 50,
    }

    model = xgb.XGBClassifier(**params)

    if tta_X is not None:
        # TTA 데이터 가중치 높여서 파인튜닝
        import numpy as np
        X = pd.concat([public_X, tta_X])
        y = pd.concat([public_y, tta_y])
        w = np.concatenate([
            np.ones(len(public_X)),
            np.ones(len(tta_X)) * 3.0   # TTA 데이터 3배 가중치
        ])
        model.fit(X, y, sample_weight=w,
                  eval_set=[(tta_X, tta_y)])
    else:
        model.fit(public_X, public_y)

    return model
```

#### Anomaly Transformer (ICLR 2022)

```python
"""
GitHub: thuml/Anomaly-Transformer
설치: pip install git+https://github.com/thuml/Anomaly-Transformer

특징:
  - Association Discrepancy 기반 이상탐지
  - 시계열의 이상 구간을 Association score로 탐지
  - CE 72시간 시계열에 직접 적용 가능
  - 레이블 없이 비지도 학습 가능

활용:
  - Chronos + MOIRAI + Anomaly Transformer 결과를 앙상블
  - 각자 다른 관점에서 이상 탐지 → 더 안정적
"""

# configs/model_config.yaml에 설정
anomaly_transformer:
  seq_len: 4320        # 72시간 (분 단위)
  d_model: 512
  n_heads: 8
  e_layers: 3
  d_ff: 512
  dropout: 0.0
  activation: gelu
  output_attention: true
  k: 3                 # Association Discrepancy top-k
```

---

### 3단계 — 앙상블 (6개월 후, 자체 데이터 충분 시)

```python
def ensemble_predict(ce_series: pd.Series, features: dict) -> dict:
    """
    3개 모델 앙상블
    - 데이터 초기: Chronos/MOIRAI 위주
    - 데이터 쌓이면: XGBoost 비중 증가
    """
    # 각 모델 스코어 (0~1)
    score_chronos  = chronos_score(ce_series)        # Zero-shot
    score_moirai   = moirai_score(ce_series)          # Zero-shot
    score_xgb      = xgb_model.predict_proba(        # 파인튜닝
                         features.values.reshape(1,-1))[0][1]
    score_anomaly_t= anomaly_transformer_score(ce_series)

    # 가중치 (데이터 양에 따라 동적 조정)
    w_zero_shot = max(0.4, 1.0 - tta_data_weight)  # 초기엔 높음
    w_xgb       = min(0.4, tta_data_weight)          # 데이터 쌓이면 높아짐
    w_anomaly_t  = 0.2

    final_score = (
        w_zero_shot * 0.5 * (score_chronos + score_moirai) +
        w_xgb       * score_xgb +
        w_anomaly_t * score_anomaly_t
    )

    return {
        "failure_probability": final_score,
        "scores": {
            "chronos":           score_chronos,
            "moirai":            score_moirai,
            "xgboost":           score_xgb,
            "anomaly_transformer": score_anomaly_t,
        }
    }
```

---

## MLflow 실험 관리

```python
import mlflow

mlflow.set_tracking_uri("http://localhost:5000")
mlflow.set_experiment("memory_failure_pred")

# 각 모델별 실험 추적
with mlflow.start_run(run_name="chronos_zero_shot"):
    mlflow.log_param("model", "amazon/chronos-t5-small")
    mlflow.log_param("prediction_length", 1440)
    mlflow.log_metrics({
        "auc_pr": 0.82,
        "recall": 0.88,
        "f1":     0.79,
    })

with mlflow.start_run(run_name="xgboost_finetuned"):
    mlflow.log_param("pretrain_data", "alibaba_pakdd2021")
    mlflow.log_param("finetune_weight", 3.0)
    mlflow.xgboost.log_model(model, "xgboost",
        registered_model_name="xgb_finetuned")
```

---

## 자동 재학습 스케줄

```
매일 새벽 2시:
  Chronos / MOIRAI: 재학습 불필요 (Zero-shot)
  XGBoost: TTA 신규 데이터로 파인튜닝 재실행 (cuda:1)
  성능 비교 → 개선 시 Production 교체

월 1회:
  Anomaly Transformer 전체 재학습 (cuda:1)
  앙상블 가중치 재최적화 (Optuna)
```

---

## 평가지표

```
✅ 사용:  F1, AUC-PR, Recall
❌ 금지:  Accuracy

목표:
  1단계 Zero-shot:  F1 > 0.65, Recall > 0.75 (레이블 없어도 가능)
  2단계 파인튜닝:   F1 > 0.75, AUC-PR > 0.80
  3단계 앙상블:     F1 > 0.82, AUC-PR > 0.85, Recall > 0.88
```
-e 

---

# 05. 추론 API

## FastAPI 서버 구성

```
포트: 8000
GPU:  cuda:0 (Chronos/MOIRAI/XGBoost 추론 전담)
URL:  http://10.100.230.71:8000
문서: http://10.100.230.71:8000/docs
```

---

## 엔드포인트

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/predict/{server_id}` | 단일 서버 앙상블 장애 확률 |
| GET | `/predict/all` | 전체 서버 일괄 추론 |
| GET | `/models/scores/{server_id}` | 모델별 개별 스코어 |
| GET | `/health` | 서버 상태 |
| GET | `/models/info` | 현재 모델 버전 및 가중치 |
| POST | `/labels/add` | 수동 장애 레이블 추가 |
| GET | `/metrics` | Prometheus 형식 메트릭 |

---

## 응답 형식

```json
{
  "server_id": "node3 (18AFD199)",
  "timestamp": "2025-04-04T02:30:00",
  "failure_probability": 0.823,
  "risk_level": "CRITICAL",
  "suspect_dimm": "mc0/csrow2/ch0 (슬롯 A2)",
  "model_scores": {
    "chronos":             0.81,
    "moirai":              0.79,
    "xgboost_finetuned":   0.87,
    "anomaly_transformer": 0.78
  },
  "top_causes": [
    {"feature": "CE 24h 증가 기울기", "impact": 0.342},
    {"feature": "CE 급증 비율",       "impact": 0.218},
    {"feature": "Chronos 예측 피크",  "impact": 0.156}
  ],
  "recommended_action": "즉시 VM 마이그레이션 + Maintenance Mode",
  "lead_time_estimate": "12~24시간 내 장애 예상"
}
```

---

## 모델 로드 전략 (시작 시)

```python
# cuda:0에 추론용 모델 모두 로드
chronos_pipeline = ChronosPipeline.from_pretrained(
    "amazon/chronos-t5-small", device_map="cuda:0", torch_dtype=torch.float16
)
moirai_model = MoiraiForecast.from_pretrained(
    "Salesforce/moirai-1.0-R-small"
).to("cuda:0")
xgb_model = mlflow.xgboost.load_model("models:/xgb_finetuned/Production")
anomaly_transformer = load_anomaly_transformer("models:/anomaly_t/Production")
```

---

## 자동 스케줄러 (APScheduler)

```python
# 1분마다: 전체 서버 앙상블 추론
@scheduler.scheduled_job('interval', minutes=1)
async def run_inference():
    for server_id in get_active_servers():
        result = await predict(server_id)
        if result.risk_level != "NORMAL":
            await esxi_handler.respond(result)

# 매일 새벽 2시: XGBoost 파인튜닝 재실행
@scheduler.scheduled_job('cron', hour=2)
async def retrain_xgboost():
    await finetune_xgboost_with_new_data()

# 매주 일요일 새벽 3시: 데이터 품질 + 앙상블 가중치 재최적화
@scheduler.scheduled_job('cron', day_of_week='sun', hour=3)
async def weekly_tune():
    await optimize_ensemble_weights()
    await check_feature_drift()
```

---

## 성능 목표

```
GET /predict/{server_id}  : < 300ms (Chronos 추론 포함)
GET /predict/all (4대)    : < 2초
GPU 메모리 (cuda:0)       : < 20GB (모델 모두 로드 후)
```
-e 

---

# 06. ESXi 연동

## 접근 방식

```
vCenter 미사용 이유: 네트워크 FQDN 제약
ESXi 접근 방법: SSH 읽기 + pyVmomi API 직접 연결

✅ SSH 허용: 읽기/모니터링 목적 (vmkernel 로그, DIMM 정보 등)
🚫 SSH 금지: 패키지 설치, 설정 변경, 파일 수정
```

---

## ESXi SSH 수집 (신규 추가)

```python
import paramiko
from typing import Optional

class ESXiSSHCollector:
    """ESXi SSH 읽기 전용 수집기"""

    def __init__(self, host_ip: str, username: str = "root",
                 password: str = "VMware!0", timeout: int = 10):
        self.host_ip  = host_ip
        self.username = username
        self.password = password
        self.timeout  = timeout

    def _exec(self, command: str) -> Optional[str]:
        """SSH 명령 실행 (읽기 전용만 허용)"""
        # 위험 명령어 차단
        forbidden = ["install", "rm ", "mv ", "chmod", "chown",
                     "esxcli software", "vim-cmd vmsvc/power",
                     ">", ">>", "|tee", "sed -i"]
        for kw in forbidden:
            if kw in command:
                raise ValueError(f"금지된 명령어 포함: {kw}")

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(self.host_ip, username=self.username,
                        password=self.password, timeout=self.timeout)
            _, stdout, stderr = ssh.exec_command(command, timeout=self.timeout)
            result = stdout.read().decode('utf-8', errors='ignore')
            ssh.close()
            return result
        except Exception as e:
            logging.error(f"ESXi SSH 오류 {self.host_ip}: {e}")
            return None

    def get_vmkernel_memory_errors(self, lines: int = 500) -> list:
        """vmkernel 로그에서 메모리 에러 추출"""
        cmd = (f"grep -i 'memory\\|DRAM\\|ECC\\|correctable\\|uncorrectable' "
               f"/var/log/vmkernel.log | tail -{lines}")
        output = self._exec(cmd)
        if not output:
            return []
        return [line.strip() for line in output.splitlines() if line.strip()]

    def get_dimm_info(self) -> str:
        """DIMM 물리 정보 (FRU)"""
        return self._exec("esxcli hardware ipmi fru list") or ""

    def get_memory_stats(self) -> dict:
        """메모리 상세 통계"""
        output = self._exec("esxcli system stats memory get") or ""
        # 파싱 후 dict 반환
        stats = {}
        for line in output.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                stats[k.strip()] = v.strip()
        return stats

    def get_host_summary(self) -> str:
        """호스트 요약 정보"""
        return self._exec("vim-cmd hostsvc/hostsummary") or ""
```

---

## ESXi pyVmomi 연결

```python
import ssl
from pyVmomi import vim
from pyVim.connect import SmartConnect, Disconnect

def connect_esxi(host_ip: str, username: str = "root",
                 password: str = "VMware!0"):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode    = ssl.CERT_NONE
    return SmartConnect(host=host_ip, user=username,
                        pwd=password, sslContext=context)
```

---

## 리스크 레벨별 자동 대응

### WARNING (0.65 ~ 0.85)

```python
async def warning_response(host_ip: str, result):
    si   = connect_esxi(host_ip)
    host = get_host_object(si)

    # Admission Control: 신규 VM 배치 차단
    # (설정 변경 아닌 API 호출)
    spec = vim.host.ConfigSpec()
    host.ReconfigureHost_Task(spec)

    await send_slack_alert("🟡 WARNING", result)
    await log_action("WARNING", host_ip, result)
    Disconnect(si)
```

### CRITICAL (0.85 이상)

```python
async def critical_response(host_ip: str, result):
    si   = connect_esxi(host_ip)
    host = get_host_object(si)

    task = host.EnterMaintenanceMode(
        timeout=3600,
        evacuatePoweredOffVms=True
    )
    await wait_for_task(task)

    await send_slack_alert("🔴 CRITICAL — 수동 vMotion 필요", result)
    await create_maintenance_ticket(host_ip, result)
    await log_action("CRITICAL", host_ip, result)
    Disconnect(si)
```

### RECOVERY (0.30 이하)

```python
async def recovery_response(host_ip: str, result):
    si   = connect_esxi(host_ip)
    host = get_host_object(si)

    task = host.ExitMaintenanceMode(timeout=300)
    await wait_for_task(task)

    await send_slack_alert("✅ RECOVERY", result)
    await log_action("RECOVERY", host_ip, result)
    Disconnect(si)
```

---

## Slack 알림 포맷

```python
async def send_slack_alert(level: str, result):
    model_scores = "\n".join([
        f"  • {k}: {v:.3f}"
        for k, v in result.model_scores.items()
    ])
    top3 = "\n".join([
        f"  • {c['feature']}: {c['impact']:.3f}"
        for c in result.top_causes[:3]
    ])
    message = {
        "blocks": [
            {"type": "header",
             "text": {"type": "plain_text",
                      "text": f"{level} 메모리 장애 예측"}},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*서버:* {result.server_id}"},
                {"type": "mrkdwn", "text": f"*확률:* {result.failure_probability:.1%}"},
                {"type": "mrkdwn", "text": f"*의심 DIMM:* {result.suspect_dimm}"},
                {"type": "mrkdwn", "text": f"*모델별 스코어:*\n{model_scores}"},
                {"type": "mrkdwn", "text": f"*주요 원인:*\n{top3}"},
            ]}
        ]
    }
    await post_to_slack(SLACK_WEBHOOK_URL, message)
```

---

## audit_log 테이블

```sql
CREATE TABLE audit_log (
    id           SERIAL PRIMARY KEY,
    action_time  TIMESTAMP DEFAULT NOW(),
    host_ip      VARCHAR(20),
    server_id    VARCHAR(50),
    action_type  VARCHAR(20),
    probability  FLOAT,
    model_scores JSONB,
    suspect_dimm VARCHAR(50),
    operator     VARCHAR(50) DEFAULT 'auto',
    notes        TEXT
);
```
-e 

---

# 07. Phase별 Claude Code 지시문

> 아래 내용을 Claude Code 대화창에 그대로 복사해서 입력합니다.
> Phase 완료 후 CLAUDE.md의 "현재 개발 단계"를 업데이트하세요.

---

## Phase 0 — 개발 환경 셋업 (Week 1)

```
다음을 순서대로 실행해줘:

1. conda create -n failure_pred python=3.11 -y
2. conda activate failure_pred
3. nvidia-smi 실행해서 A100 80GB × 2장 인식 확인
4. pip 패키지 설치:
   - torch torchvision (CUDA 12.1)
   - chronos-forecasting                     # Amazon Chronos
   - uni2ts                                  # Salesforce MOIRAI
   - git+https://github.com/thuml/Anomaly-Transformer
   - xgboost lightgbm scikit-learn imbalanced-learn shap optuna
   - mlflow fastapi uvicorn
   - pandas numpy scipy apscheduler prometheus-client
   - pyVmomi paramiko pyyaml python-dotenv
   - pytest pytest-httpserver
5. Git 초기화, .gitignore 생성 (data/, models/, .env 포함)
6. SSH 연결 확인:
   ssh hpcdev@10.100.230.70 'echo node1 ok'   # 18AFD201
   ssh hpcdev@10.100.230.72 'echo node2 ok'   # 18AFD226 (모니터링)
7. ESXi 연결 확인 (SSH + pyVmomi):
   ssh root@10.148.148.118 'echo vmgnode18 ok'   # VMware!0
   pyVmomi SmartConnect(host='10.148.148.118', user='root', pwd='VMware!0')
8. Chronos 동작 확인:
   from chronos import ChronosPipeline
   pipeline = ChronosPipeline.from_pretrained("amazon/chronos-t5-small", device_map="cuda:0")
9. configs/servers.yaml, configs/esxi.yaml 생성 (vmgnode17 제외)
10. 전체 디렉토리 구조 생성 (docs/01_environment.md 참고)

완료 기준:
- A100 80GB × 2장 nvidia-smi 확인
- SSH node1, node2, ESXi 4대 연결 성공
- Chronos import 및 cuda:0 로드 성공
```

---

## Phase 1 — 데이터 수집 개발 (Week 2~3)

```
docs/02_data_collection.md를 참고해서 다음 파일들을 작성해줘:

1. src/collectors/edac_collector.py
   - edac-util -s 0 파싱 → DIMM별 CE/UE 추출
   - rasdaemon SQLite 폴링 (last_id 관리)
   - dmidecode --type 17 파싱 → 슬롯 위치 매핑
   - DimmLocation, MemoryErrorEvent dataclass

2. src/collectors/ipmi_collector.py
   - ipmitool 온도/전압/팬/전력 수집
   - BMC: 10.100.231.71, root/qwe123   # node3 (18AFD199)

3. src/collectors/smart_collector.py
   - smartctl SMART 속성 파싱

4. src/collectors/esxi_ssh_collector.py  ← 신규
   - paramiko SSH로 ESXi 4대 읽기 전용 수집
   - vmkernel.log 메모리 에러 필터링
   - esxcli hardware ipmi fru list (DIMM 물리 정보)
   - 위험 명령어 차단 로직 포함 (install, rm 등)

5. src/collectors/esxi_collector.py
   - pyVmomi로 ESXi 직접 연결 (vCenter 미사용)
   - 호스트 메트릭 + VM 집계

6. scripts/setup_telegraf.py
   - node2 18AFD226 (10.100.230.72)에 SSH로 telegraf.conf 배포

7. tests/test_edac_collector.py
   - mock 기반 단위 테스트

완료 기준:
- 단위 테스트 통과
- ESXi SSH 읽기 성공 (vmgnode18)
- Telegraf node2 정상 수집 확인
```

---

## Phase 2 — 피처 엔지니어링 (Week 3~4)

```
docs/03_features.md를 참고해서 다음을 작성해줘:

src/features/feature_pipeline.py

구현:
1. fetch_raw_data(server_id, hours=72)
   - VictoriaMetrics (http://10.100.230.72:8428) ← node2 18AFD226 HTTP API
2. compute_ce_features(ce_series)     → Category A 20개
3. compute_hw_features(server_id)     → Category B 10개
4. compute_workload_features()        → Category C 10개
5. compute_esxi_features(host_ip)     → Category D 5개
6. build_feature_vector(server_id)    → 45개 피처 벡터
7. build_training_dataset(days=90)    → (X, y)

tests/test_feature_pipeline.py (VictoriaMetrics mock)

완료 기준:
- 45개 피처 계산 < 5초
- 결측값 0개
```

---

## Phase 3 — Zero-shot 모델 즉시 가동 (Week 3~4, Phase 2와 병행)

```
docs/04_model.md의 1단계를 참고해서 다음을 작성해줘:

1. src/models/chronos_predictor.py
   - ChronosPipeline.from_pretrained("amazon/chronos-t5-small")
   - device_map="cuda:0"
   - predict_ce_anomaly(ce_series) → anomaly_score, risk_level
   - CE 72시간 시계열 → 향후 24시간 예측

2. src/models/moirai_predictor.py
   - MoiraiForecast.from_pretrained("Salesforce/moirai-1.0-R-small")
   - detect_anomaly_moirai(ce_series) → anomaly_score

3. src/models/ensemble.py
   - 초기 가중치: Chronos 0.5 + MOIRAI 0.5
   - XGBoost 파인튜닝 완료 후 가중치 조정

4. scripts/download_public_data.py
   - Alibaba PAKDD 2021 데이터셋 다운로드
   - URL: https://tianchi.aliyun.com/dataset/132973
   - data/alibaba_pakdd2021/ 에 저장

완료 기준:
- Chronos로 CE 시계열 이상 스코어 출력 확인
- MOIRAI 이상탐지 동작 확인
- 레이블 없이 3주차부터 이상탐지 가동 ← 핵심
```

---

## Phase 4 — 레이블링 + XGBoost 파인튜닝 (Week 4~5)

```
1. 레이블링 시스템:
   - PostgreSQL 스키마 (Alembic): failure_events, training_labels, audit_log
   - rasdaemon UE 자동 감지 → failure_events 삽입
   - UE 전 6/12/24/48/72h Positive 샘플 자동 생성
   - Positive:Negative = 1:10

2. src/models/xgboost_predictor.py
   - Alibaba PAKDD 데이터로 사전학습 (device='cuda:1')
   - TTA 자체 데이터로 파인튜닝 (sample_weight=3.0)
   - Optuna HPO 30 trials

3. src/models/anomaly_transformer.py
   - thuml/Anomaly-Transformer 구조 활용
   - CE 72시간 시계열 학습 (cuda:1)
   - 비지도 학습 (레이블 불필요)

4. 앙상블 가중치 업데이트:
   - Chronos 0.25 + MOIRAI 0.15 + XGBoost 0.35 + AnomalyT 0.25

완료 기준:
- XGBoost 파인튜닝 F1 > 0.75 (Alibaba 검증셋)
- Anomaly Transformer 학습 완료
- MLflow UI 실험 기록 확인
```

---

## Phase 5 — 추론 API 개발 (Week 5~6)

```
docs/05_api.md를 참고해서 다음을 작성해줘:

src/api/main.py (FastAPI)

1. 시작 시 모델 로드 (cuda:0):
   - Chronos, MOIRAI, XGBoost, Anomaly Transformer

2. 엔드포인트:
   GET /predict/{server_id}     → 앙상블 결과 + model_scores
   GET /predict/all             → 전체 4대 ESXi 기준 일괄
   GET /models/scores/{server_id} → 모델별 개별 스코어
   GET /metrics                 → Prometheus 형식

3. 응답에 model_scores 포함 (4개 모델 각각)

4. APScheduler:
   - 1분: 전체 서버 추론 + ESXi 대응
   - 새벽 2시: XGBoost 파인튜닝 재실행
   - 일요일 새벽 3시: 앙상블 가중치 재최적화

완료 기준:
- /predict/{server_id} < 300ms
- model_scores 4개 모두 포함된 응답 확인
```

---

## Phase 6 — ESXi 연동 (Week 6~7)

```
docs/06_esxi.md를 참고해서 다음을 작성해줘:

src/esxi/action_handler.py

1. ESXiSSHCollector + pyVmomi 통합
2. 리스크 레벨별 대응 (WARNING/CRITICAL/RECOVERY)
3. Slack 알림 (model_scores 포함)
4. audit_log 기록 (model_scores JSONB 컬럼)
5. 연결 실패 시 자동 재연결

tests/test_esxi_handler.py (mock 기반)

완료 기준:
- mock 테스트 통과
- Slack 알림에 model_scores 4개 표시
- audit_log 기록 확인
```

---

## Phase 7 — 통합 테스트 & 완료 (Week 7~8)

```
다음을 작성해줘:

1. tests/integration/test_full_pipeline.py
   - VictoriaMetrics mock (pytest-httpserver)
   - ESXi SSH + pyVmomi mock
   - Chronos/MOIRAI는 실제 모델 사용 (cuda:0)
   - E2E: 수집 → 피처 → 앙상블 추론 → ESXi 대응

2. scripts/inject_test_fault.py
   - 가상 CE 에러 주입 → 모델들이 탐지하는지 검증
   - 각 모델별 탐지 결과 비교 리포트

3. node2 Grafana 대시보드:
   - Panel 1: 서버별 앙상블 확률 게이지
   - Panel 2: DIMM별 CE 에러 시계열
   - Panel 3: 모델별 스코어 비교 (4개 모델)
   - Panel 4: ESXi 대응 이벤트 로그
   - Panel 5: 모델 정확도 추이 (데이터 쌓이면서)

완료 기준:
- E2E 테스트 전체 통과
- 장애 주입 후 3개 이상 모델에서 탐지 성공
- Grafana node2:3000 대시보드 정상 표시
```
-e 

---

# 08. 제약 사항 및 검증 체크리스트

## ESXi 접근 규칙

| 구분 | 내용 |
|---|---|
| ✅ SSH 허용 | 읽기/모니터링 목적 (vmkernel 로그, DIMM 정보, 메모리 통계) |
| ✅ pyVmomi | API 읽기, EnterMaintenanceMode, ExitMaintenanceMode |
| 🚫 SSH 금지 | 패키지 설치, 설정 파일 수정, 서비스 변경, 파일 삭제 |
| 🚫 금지 명령어 | install, rm, mv, chmod, chown, esxcli software, vim-cmd vmsvc/power |
| ❌ 제외 호스트 | vmgnode17 (10.148.148.117) 접속 장애 |

---

## 절대 금지 사항

| 구분 | 금지 내용 |
|---|---|
| 🚫 계정 | root 계정으로 Claude Code 실행 |
| 🚫 코드 | 비밀번호/API 키 코드 직접 기재 (→ .env 분리) |
| 🚫 지표 | Accuracy 사용 (→ F1, AUC-PR, Recall) |
| 🚫 로깅 | print() 사용 (→ logging 모듈) |
| 🚫 설정 | 코드 내 IP/PW 하드코딩 (→ configs/ YAML) |

---

## 코드 품질 체크리스트

```
신규 파일 작성 시:
  [ ] docstring 있는가? (Args, Returns, Raises)
  [ ] try/except + timeout 있는가? (외부 API 호출)
  [ ] logging 사용하는가? (print 없는가?)
  [ ] 설정값이 YAML/.env로 분리되었는가?
  [ ] 단위 테스트가 tests/에 있는가?
  [ ] ESXi SSH 명령이 읽기 전용인가? (금지 키워드 체크)
  [ ] cuda:0 / cuda:1 역할이 명시되었는가?
```

---

## Phase별 완료 검증 기준

| Phase | 검증 기준 |
|---|---|
| P0 환경 셋업 | A100 80GB × 2장 / SSH 5대 성공 / Chronos cuda:0 로드 성공 |
| P1 데이터 수집 | edac_collector 테스트 통과 / ESXi SSH vmkernel 로그 수집 확인 |
| P2 피처 계산 | 45개 피처 < 5초 / 결측값 0개 |
| P3 Zero-shot | Chronos/MOIRAI 이상 스코어 출력 / **레이블 없이 이상탐지 가동** |
| P4 파인튜닝 | XGBoost F1 > 0.75 / MLflow 실험 기록 / 앙상블 가중치 설정 |
| P5 추론 API | /predict < 300ms / model_scores 4개 응답 확인 |
| P6 ESXi 연동 | mock 테스트 통과 / Slack model_scores 포함 알림 수신 |
| P7 통합 테스트 | E2E 통과 / 장애 주입 탐지 성공 / Grafana 대시보드 표시 |

---

## 개발 일정 요약

```
기존 계획 (처음부터):     오픈소스 모델 활용:
  Week 1: 환경 셋업        Week 1: 환경 셋업 (+ Chronos/MOIRAI 설치)
  Week 2-3: 수집            Week 2-3: 수집
  Week 3-4: 피처            Week 3-4: 피처 + Zero-shot 즉시 가동 ★
  Week 4-5: 레이블           Week 4-5: 레이블 + XGBoost 파인튜닝
  Week 5-6: 모델 학습        Week 5-6: API 개발
  Week 6-7: API              Week 6-7: ESXi 연동
  Week 7-8: ESXi 연동        Week 7-8: 통합 테스트
  Week 8-9: 테스트           (1~2주 단축)

핵심 차이:
  - Week 3부터 이상탐지 가동 가능 (레이블 없이)
  - 공개 데이터(Alibaba PAKDD)로 빠른 파인튜닝
  - 자체 데이터 부족 문제 해결
```

---

## 네트워크 접근 정리

```
개발 서버:
  node3 (18AFD199) → node2 (18AFD226): ssh hpcdev@10.100.230.72  ✅
  node3 (18AFD199) → node1 (18AFD201): ssh hpcdev@10.100.230.70  ✅

ESXi (4대):
  node3 (18AFD199) → vmgnode17: ❌ 접속 장애 제외
  node3 (18AFD199) → vmgnode18: SSH 읽기 + pyVmomi ✅
  node3 (18AFD199) → vmgnode23: SSH 읽기 + pyVmomi ✅
  node3 (18AFD199) → vmgnode26: SSH 읽기 + pyVmomi ✅
  node3 (18AFD199) → vmgnode30: SSH 읽기 + pyVmomi ✅

BMC:
  node3 (18AFD199) BMC: ipmitool -H 10.100.231.71 -U root -P qwe123
  ESXi BMC:  admin/admin (읽기 전용)
```

---

## 장애 대응 운영 절차

```
1. Slack CRITICAL 알림 수신
   → 모델별 스코어 확인 (4개 모두 높으면 확실한 장애)
2. Grafana 대시보드(node2:3000)에서 CE 패턴 시각 확인
3. ESXi Maintenance Mode 진입 확인 (자동)
4. ESXi 관리 콘솔에서 VM 수동 vMotion 실행
5. DIMM 슬롯 물리 교체 (현장 작업)
6. memtest 통과 후 Maintenance Mode 해제
```
