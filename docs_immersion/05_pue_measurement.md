# 05. PUE 전력효율 측정

> GPU/NPU 부하를 정밀 제어하여 다양한 부하 수준에서 데이터센터 PUE를 측정.

---

## PUE란?

**PUE (Power Usage Effectiveness)** = 데이터센터 총 전력 / IT 장비 전력

- PUE 1.0 = 냉각 전력 0 (이론적 완벽)
- PUE 1.1 = IT 전력 대비 10% 추가 (냉각 등)
- 액침냉각은 공랭 대비 PUE를 크게 낮출 수 있음

**목표**: 부하 30% / 50% / 90% / 99%에서의 PUE를 측정하여 액침냉각 효율 검증.

---

## GPU 부하 제어 (PI 피드백 컨트롤러)

### 동작 원리

Anomaly Transformer의 continual training을 GPU 부하로 사용합니다. batch size를 PI 컨트롤러로 동적 조정하여 목표 GPU 사용률을 달성합니다.

```
┌─────────────────────────────────────────────────┐
│            PUE GPU Load Controller              │
│                                                 │
│  Target GPU Util ────┐                          │
│                      ▼                          │
│              PI Controller ───► batch_size      │
│                      ▲                          │
│                      │                          │
│  Actual GPU Util ────┘ (DCGM Prometheus)        │
│                                                 │
│  Safety:                                        │
│    - batch clamp: [4, MAX_BATCH]                │
│    - VRAM guard: > 85% → block increase         │
│    - temp guard: > 83°C → force reduce          │
│    - Xid error: auto-shutdown                   │
│    - target cap: max 85%                        │
└─────────────────────────────────────────────────┘
```

### 안전장치 (post-incident hardening)

| 안전장치 | 조건 | 동작 |
|---|---|---|
| **batch 상한** | batch_size > MAX_BATCH (72) | 72로 클램프 |
| **VRAM 가드** | VRAM 사용률 > 85% | batch 증가 차단 |
| **온도 가드** | GPU 온도 > 83°C | 강제 batch 감소 |
| **Xid 에러 감지** | DCGM_FI_DEV_XID_ERRORS > 0 | 즉시 중단 |
| **타겟 캡** | 입력 target > 85% | 85%로 클램프 |
| **CUDA OOM** | 메모리 부족 오류 | batch 반감 + cache clear |
| **추론 watchdog** | 응답 > 5초 3회 연속 | GPU 부하 자동 정지 + Slack |
| **node1 제외** | 항상 | GPU PCIe 사망 이력으로 영구 제외 |

### 구성 (ConfigMap)

```yaml
# ConfigMap: pue-gpu-load-config
TARGET_GPU_UTIL: "80"      # 목표 GPU 사용률 (%)
MAX_BATCH: "72"            # batch 상한
INITIAL_BATCH: "32"        # 초기 batch (수렴 속도 최적화)
CONTROL_INTERVAL: "90"     # PI 제어 주기 (초)
GPU_TEMP_LIMIT: "83"       # 온도 제한 (°C)
WARMUP_SECONDS: "120"      # 워밍업 기간 (초)
```

### 85% GPU 클램프 (의도적)

PI 컨트롤러가 target을 85%로 내부 클램프합니다. GPU PCIe 사망 사고 2회 후 추가된 보호 장치로, `start_90`과 `start_99`는 GPU 측면에서 사실상 동일 부하(~88%)입니다.

99% 부하가 정말 필요하면:
```bash
kubectl -n failure-prediction patch cm pue-gpu-load-config \
  --type merge -p '{"data":{"MAX_GPU_UTIL":"99"}}'
```

---

## NPU 부하 제어

NPU 부하는 Qwen3-Embedding-4B 임베딩 요청의 전송 간격으로 제어합니다.

### 전력 기반 매핑 (실측 2026-06-18)

| 목표 | INTERVAL_SEC | 실측 전력 | 설명 |
|---|---|---|---|
| OFF | - | 45W | idle |
| 30% | 5.0 | 84W | 저부하 |
| 50% | 3.0 | 102W | 중부하 |
| 90% | 0.5 | 155W | 고부하 |
| 99% | 0.2 | 156W | 최대 (90%와 거의 동일) |

**NPU TDP**: ~160W. 90%와 99%의 실측 전력 차이가 거의 없음 (155W vs 156W).
실질적 NPU 구분은 3단계: 30%(84W) / 50%(102W) / 풀가동(155W+).

---

## PUE 스크립트 (scripts/pue/)

한 줄로 GPU/NPU 부하 단계를 변경하는 운영 자동화 스크립트.

### 사용법

```bash
./scripts/pue/start_30.sh   # 30% 부하
./scripts/pue/start_50.sh   # 50% 부하
./scripts/pue/start_90.sh   # 90% 부하
./scripts/pue/start_99.sh   # 99% 부하 (주의)
./scripts/pue/stop_all.sh   # 전체 정지
./scripts/pue/reset_all.sh  # 재부팅/사고 후 전체 복구
./scripts/pue/_status.sh    # 현재 상태 확인
```

### 부하 단계별 실측

| 스크립트 | GPU 의도 | GPU 실측 | NPU 실측 | 추론 응답 | 비고 |
|---|---|---|---|---|---|
| start_30 | 30% | ~30% (7분 수렴) | 73~84W (~30%) | 3.1초 | 저부하 |
| start_50 | 50% | ~50% | 95~102W (~50%) | 3.3초 | 중부하 |
| start_90 | 90% | ~88% (85% 클램프) | 155~161W (풀가동) | 3.1초 | 고부하 |
| start_99 | 99% | ~88% (85% 클램프) | 154~156W (풀가동) | 3.3초 | 최대 |
| stop_all | OFF | 0% | 45W (idle) | 3.3초 | 정지 |

### INITIAL_BATCH 최적화

부하 목표에 따라 초기 batch size를 다르게 설정하여 PI 컨트롤러 수렴 속도를 개선합니다.

| 목표 | INITIAL_BATCH | 이유 |
|---|---|---|
| 30% | 8 | 낮은 초기값으로 오버슈트 방지 |
| 50% | 16 | 중간 시작점 |
| 90% | 32 | 높은 초기값으로 빠른 수렴 |
| 99% | 32 | 동일 (85% 클램프) |

---

## Web UI

브라우저에서 GPU 부하를 제어할 수 있는 웹 인터페이스.

| 항목 | 값 |
|---|---|
| URL | http://10.100.230.130:31600 |
| 기능 | 타겟 슬라이더 (0-85%), 노드별 GPU 상태, Emergency Stop |
| 갱신 | 10초마다 자동 |
| 인증 | 없음 (사내망 전용) |

### 화면 구성
- **Status Badge**: Running / Stopped / Starting
- **GPU Node Cards**: node2/3/4별 사용률, 온도, 전력, VRAM
- **Target Slider**: 0-85% 범위 조절 (모든 변경에 확인 모달)
- **Emergency Stop**: 즉시 replicas=0

---

## Inference Watchdog

추론 응답시간을 모니터링하여 PUE 부하가 추론에 악영향을 주면 자동 정지합니다.

| 항목 | 값 |
|---|---|
| 체크 주기 | 30초 |
| 임계값 | 5초 |
| 트리거 | 3회 연속 초과 |
| 동작 | `pue-gpu-load` replicas=0 + Slack 알림 |
| 쿨다운 | 5분 후 재측정 (자동 재시작 안 함) |

---

## 운영 명령어 (수동)

```bash
# GPU 부하 ON/OFF
kubectl -n failure-prediction scale deploy pue-gpu-load --replicas=1   # ON
kubectl -n failure-prediction scale deploy pue-gpu-load --replicas=0   # OFF

# NPU 부하 ON/OFF
kubectl -n failure-prediction scale deploy npu-load-generator --replicas=1  # ON
kubectl -n failure-prediction scale deploy npu-load-generator --replicas=0  # OFF

# GPU 타겟 변경 (수동)
kubectl -n failure-prediction patch cm pue-gpu-load-config \
  --type merge -p '{"data":{"TARGET_GPU_UTIL":"60"}}'

# NPU 간격 변경 (수동)
kubectl -n failure-prediction set env deploy/npu-load-generator INTERVAL_SEC=3.0

# 현재 상태 확인
./scripts/pue/_status.sh
```
