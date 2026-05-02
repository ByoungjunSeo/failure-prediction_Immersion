# 10. 로그 분석 가이드 — 실제 로그 예시와 모델별 판단

> 이 문서는 실제 운영 환경에서 수집된 로그를 기반으로
> 각 모델이 어떻게 이상을 판단하는지 설명합니다.

---

## 1. 로그 수집 소스 및 현재 상태

### 현재 수집 중인 로그

| 소스 | 서버 | 수집 방법 | 현재 상태 |
|---|---|---|---|
| vmkernel.log | ESXi 4대 | SSH (paramiko) | 수집 중 |
| EDAC CE/UE | node3 | /sys/devices/system/edac/ | CE=0, UE=0 (정상) |
| IPMI SEL | ESXi BMC | esxcli hardware ipmi sel | 수집 중 |
| SMART | node3 | smartctl | 수집 중 |
| 시스템 메트릭 | node2 | Telegraf → VictoriaMetrics | 수집 중 |

---

## 2. 실제 수집된 로그 분석

### 2.1 ESXi vmkernel — SCSI 디바이스 에러

현재 ESXi 호스트에서 가장 많이 검출되는 에러입니다.

**실제 로그 (vmgnode26에서 수집):**
```
2026-04-06T13:52:11.478Z cpu0:2098084)ScsiDeviceIO: 3469: 
  Cmd(0x459a8e3f4680) 0x1a, CmdSN 0x76a018 from world 0 
  to dev "naa.600605b00ebcdff024d2da38046660c8" 
  failed H:0x0 D:0x2 P:0x0 Valid sense data: 0x5 0x24 0x0.
```

**로그 해석:**
| 필드 | 값 | 의미 |
|---|---|---|
| `ScsiDeviceIO: 3469` | SCSI I/O 에러 | 스토리지 디바이스 통신 실패 |
| `Cmd 0x1a` | MODE SENSE | 디바이스 설정 조회 명령 |
| `Cmd 0x85` | ATA PASS-THROUGH | 디스크 직접 명령 |
| `H:0x0 D:0x2 P:0x0` | Host=OK, Device=Check, Plugin=OK | 디바이스 측 오류 |
| `Sense 0x5 0x24 0x0` | Illegal Request / Invalid Field in CDB | 명령 파라미터 오류 |
| `Sense 0x5 0x20 0x0` | Illegal Request / Invalid Command | 지원하지 않는 명령 |

**모델별 판단:**
| 모델 | 판단 | 이유 |
|---|---|---|
| **Chronos** | NORMAL (0.70) | CE 시계열에 직접 영향 없음 — SCSI 에러는 메모리가 아닌 스토리지 |
| **MOIRAI** | NORMAL~WARNING | 에러 빈도가 패턴에서 벗어나면 감지 |
| **XGBoost** | 피처 반영됨 | `esxi_vmkernel_error_cnt`가 45개 피처 중 하나로 간접 반영 |
| **Anomaly Transformer** | 직접 영향 없음 | CE 시계열만 분석하므로 SCSI 에러 자체는 미반영 |

**운영 판단:**
- 이 에러는 **스토리지(RAID/디스크) 관련**이며 메모리 장애와는 직접 관련 없음
- 단, 에러 빈도가 급증하면 시스템 전반 불안정 → 간접적으로 메모리 부하 증가 가능
- vmgnode26에서 하루 19건 발생 → 주의 관찰 필요

---

### 2.2 ESXi vmkernel — 호스트별 에러 빈도 비교

**실제 수집 데이터 (2026-04-07 기준):**

| 호스트 | vmkernel 에러 수 | 에러 유형 | 기간 |
|---|---|---|---|
| **vmgnode18** | 2건 | SCSI Sense 0x5 0x24 | 2026-02~03 (2개월간 2건) |
| **vmgnode23** | 10건 | SCSI Sense 0x5 0x24, 0x20 | 2026-03~04 (1개월간 10건) |
| **vmgnode26** | **19건** | SCSI Sense 0x5 0x24, 0x20 | 2026-01~04 (3개월간 19건) |
| **vmgnode30** | 0건 | - | 에러 없음 |

**모델별 판단:**
```
vmgnode26 (19건, 최근 가속):
  Chronos:              0.70 (NORMAL)    — CE 시계열은 정상
  MOIRAI:               1.00 (CRITICAL)  — vmkernel 에러 패턴 이상 감지
  Anomaly Transformer:  1.00 (CRITICAL)  — 에러 빈도 구조 변화 감지
  앙상블:               0.88 (CRITICAL)  — 종합 판단: 주의 필요

vmgnode30 (0건):
  모든 모델:            NORMAL           — 에러 없음, 정상
```

**운영 조치:**
- vmgnode26: SCSI 에러가 지속 증가 중 → RAID 컨트롤러/디스크 점검 권장
- vmgnode23: 최근 1개월간 에러 집중 → 추세 관찰 필요
- vmgnode18/30: 정상 범위

---

### 2.3 EDAC CE/UE — DRAM 메모리 에러 (node3)

**실제 수집 데이터:**
```
=== node3 (18AFD199) EDAC 현재 상태 ===
mc0: CE=0, UE=0   (CPU1_DIMM_A1, 64GB)
mc1: CE=0, UE=0   (CPU1_DIMM_B1, 64GB)
mc2: CE=0, UE=0   (CPU2_DIMM_A1, 64GB)
mc3: CE=0, UE=0   (CPU2_DIMM_B1, 64GB)

설치된 DIMM: 8장 × 64GB = 512GB
  CPU1: DIMM_A1, DIMM_B1, DIMM_D1, DIMM_E1
  CPU2: DIMM_A1, DIMM_B1, DIMM_D1, DIMM_E1
```

**현재 상태: 완전 정상 (CE=0, UE=0)**

이 상태에서 모델은 다음과 같이 판단합니다:
| 모델 | 판단 | 이유 |
|---|---|---|
| **Chronos** | NORMAL | CE가 0이므로 예측할 이상 패턴 없음 |
| **MOIRAI** | NORMAL | CE 시계열이 평탄 — 이상 없음 |
| **XGBoost** | NORMAL | CE 관련 피처 20개 모두 0 |
| **Anomaly Transformer** | NORMAL | 정상 패턴 (변화 없음) |

---

### 2.4 IPMI SEL — 하드웨어 이벤트 로그

**실제 수집 데이터 (vmgnode26):**
```
Record:19
  When: 2026-04-03T15:15:23
  Event Type: 1 (Other)
  SEL Type: 2 (System Event)
  Message: Deassert + Fan Lower Non-critical going low
  Sensor Number: 168
```

**로그 해석:**
| 필드 | 의미 |
|---|---|
| `Deassert` | 이벤트가 해소됨 (이전에 발생했다가 복구) |
| `Fan Lower Non-critical going low` | 팬 RPM이 하한 경고 수준 이하로 떨어졌다가 복구 |
| `Sensor 168` | 특정 팬 센서 |

**모델별 판단:**
| 모델 | 반영 방식 |
|---|---|
| **XGBoost** | `fan_rpm_anomaly` 피처에 반영 (팬 이상=1) |
| **XGBoost** | `ipmi_sel_error_cnt` 피처에 카운트 반영 |
| 다른 모델 | IPMI 데이터를 직접 사용하지 않음 (CE 시계열 기반) |

**운영 판단:**
- 팬 RPM 저하 → 냉각 성능 감소 → CPU/DIMM 온도 상승 가능
- 온도 상승이 지속되면 DRAM CE 에러 발생 확률 증가
- XGBoost가 `cpu_temp_slope_6h`와 `fan_rpm_anomaly`를 종합하여 간접 위험 판단

---

## 3. 장애 시나리오별 로그 패턴과 모델 반응

### 시나리오 A: DIMM CE 에러 급증 (가장 흔한 장애 전조)

**예상 로그:**
```
# EDAC (node3)
mc0/csrow0/ch0: 0 Uncorrected Errors, 127 Corrected Errors  ← 1시간에 127건
mc0/csrow0/ch0: 0 Uncorrected Errors, 342 Corrected Errors  ← 2시간 후 342건

# rasdaemon
timestamp=2026-04-07 10:00:00, mc=0, csrow=0, channel=0, 
  err_type=Corrected, err_count=15, address=0x7f8a3000
```

**모델별 반응:**
| 모델 | 스코어 | 판단 근거 |
|---|---|---|
| **Chronos** | 0.85+ (CRITICAL) | CE 시계열 급증 → 향후 24h 피크 예측 높음 |
| **MOIRAI** | 0.90+ (CRITICAL) | 예측 불확실성 급증 — 패턴 이탈 |
| **XGBoost** | 0.80+ (WARNING~CRITICAL) | `ce_count_1h=127`, `ce_slope_1h` 급경사, `ce_burst_flag=1` |
| **AnomalyT** | 0.85+ (CRITICAL) | 학습된 정상 패턴 대비 Association Discrepancy 급증 |

**앙상블 결과:** `failure_probability ≈ 0.85~0.95 (CRITICAL)`

**자동 대응:**
1. ESXi Maintenance Mode 전환
2. Slack 긴급 알림 (4개 모델 스코어 포함)
3. audit_log 기록

**운영자 조치:**
1. 해당 서버의 VM을 다른 호스트로 vMotion
2. DIMM 교체 요청 (mc0/csrow0/ch0 → CPU1_DIMM_A1)
3. 교체 후 CE 카운트가 0으로 리셋되면 RECOVERY

---

### 시나리오 B: CE 점진적 증가 (DIMM 열화)

**예상 로그:**
```
# EDAC — 일주일간 서서히 증가
Day 1: mc1/csrow0/ch0: CE=3
Day 2: mc1/csrow0/ch0: CE=8
Day 3: mc1/csrow0/ch0: CE=15
Day 4: mc1/csrow0/ch0: CE=28
Day 5: mc1/csrow0/ch0: CE=47
Day 6: mc1/csrow0/ch0: CE=82
Day 7: mc1/csrow0/ch0: CE=156
```

**모델별 반응:**
| 모델 | 스코어 | 판단 근거 |
|---|---|---|
| **Chronos** | 0.65~0.75 (WARNING) | 24h 예측에서 지속 증가 예측, 하지만 급증은 아님 |
| **MOIRAI** | 0.50~0.70 (NORMAL~WARNING) | 패턴은 변하고 있지만 극적이지 않음 |
| **XGBoost** | 0.75~0.85 (WARNING~CRITICAL) | `ce_slope_24h` 양수, `ce_cumulative_7d` 높음, `ce_acceleration` 양수 |
| **AnomalyT** | 0.60~0.80 (NORMAL~WARNING) | 구조적 변화 감지, 서서히 상승 |

**앙상블 결과:** `failure_probability ≈ 0.65~0.78 (WARNING)`

**자동 대응:** VM 신규 배치 차단 + Slack 알림

**운영자 조치:**
1. DIMM 교체 계획 수립 (1~2주 내)
2. 매일 CE 추세 확인
3. CRITICAL로 진행 시 즉시 교체

---

### 시나리오 C: UE (Uncorrectable Error) 발생 — 긴급 상황

**예상 로그:**
```
# EDAC
mc0/csrow2/ch0: 1 Uncorrected Error, 89 Corrected Errors

# rasdaemon
timestamp=2026-04-07 14:30:00, mc=0, csrow=2, channel=0,
  err_type=Uncorrected, err_count=1, address=0x3f2a1000

# vmkernel (ESXi)
MCE: Machine Check Exception: CPU 0 Bank 8
MCE: ADDR 0x3f2a1000: MC Error: Uncorrected Memory Error
```

**모델별 반응:**
| 모델 | 스코어 | 판단 근거 |
|---|---|---|
| **Chronos** | 0.90+ (CRITICAL) | UE 직전 CE 급증 패턴이 이미 감지됨 |
| **MOIRAI** | 1.00 (CRITICAL) | 극단적 이상 |
| **XGBoost** | 0.95+ (CRITICAL) | `mce_uncorrected_flag=1`, CE 피처 전부 높음 |
| **AnomalyT** | 1.00 (CRITICAL) | 학습 데이터에 없던 극단 패턴 |

**앙상블 결과:** `failure_probability ≈ 0.95~1.00 (CRITICAL)`

**자동 대응:**
1. 즉시 Maintenance Mode 전환
2. Slack 긴급 알림 (UE 발생 명시)
3. 자동 레이블링 → training_labels에 Positive 샘플 5개 생성 (6/12/24/48/72h 전)

**운영자 조치:**
1. **즉시** VM 마이그레이션 (데이터 손실 위험)
2. 서버 전원 차단 및 DIMM 긴급 교체
3. 교체 후 `memtest86+` 실행하여 메모리 검증

---

### 시나리오 D: 환경 요인 — 온도 상승으로 인한 간헐적 CE

**예상 로그:**
```
# IPMI
CPU1 Temp: 72°C → 78°C → 83°C (3시간 동안 11도 상승)
System Fan 3: 8500 RPM → 4200 RPM (팬 성능 저하)

# IPMI SEL
Assert + CPU1 Upper Non-critical going high
Assert + Fan Lower Non-critical going low

# EDAC — 온도 상승과 함께 간헐적 CE
14:00 mc0/csrow0/ch0: CE=0
15:00 mc0/csrow0/ch0: CE=3
16:00 mc0/csrow0/ch0: CE=0
17:00 mc0/csrow0/ch0: CE=7
18:00 mc0/csrow0/ch0: CE=2
```

**모델별 반응:**
| 모델 | 스코어 | 판단 근거 |
|---|---|---|
| **Chronos** | 0.40~0.55 (NORMAL) | CE가 간헐적이라 예측 불확실 |
| **MOIRAI** | 0.60~0.75 (NORMAL~WARNING) | 불규칙 패턴에 반응 |
| **XGBoost** | 0.70~0.85 (WARNING~CRITICAL) | `cpu_temp_slope_6h` 높음 + `fan_rpm_anomaly=1` + CE 발생 → 복합 판단 |
| **AnomalyT** | 0.55~0.70 (NORMAL~WARNING) | 간헐적 스파이크 감지 |

**앙상블 결과:** `failure_probability ≈ 0.60~0.75 (WARNING)`

**핵심:** XGBoost만이 온도+팬+CE를 종합 분석하여 **환경 요인**임을 판별.
다른 모델은 CE 시계열만 보므로 보수적으로 판단.

**운영자 조치:**
1. 서버실 온도/냉각 시스템 점검
2. 팬 교체 또는 청소
3. 온도가 정상으로 돌아오면 CE도 자연 감소 → RECOVERY

---

## 4. 로그 분석 빠른 참조표

### vmkernel Sense Code 해석

| Sense Key | ASC/ASCQ | 의미 | 심각도 |
|---|---|---|---|
| 0x0 | - | No Sense (정상) | 무시 |
| 0x1 | - | Recovered Error | 낮음 — 자동 복구됨 |
| 0x3 0x11 0x0 | Medium Error / Unrecovered Read | **높음** — 디스크 읽기 실패 |
| 0x3 0x0C 0x0 | Write Error | **높음** — 디스크 쓰기 실패 |
| 0x4 | - | Hardware Error | **높음** — 하드웨어 장애 |
| 0x5 0x20 0x0 | Invalid Command | 낮음 — 명령 미지원 |
| 0x5 0x24 0x0 | Invalid Field in CDB | 낮음 — 파라미터 오류 |
| 0xB | - | Aborted Command | 보통 — 재시도 필요 |

### EDAC 에러 수준별 판단

| CE 빈도 | 판단 | 조치 |
|---|---|---|
| 0건/시간 | 정상 | 모니터링 지속 |
| 1~10건/시간 | 주의 | 추세 관찰, 24시간 후 재확인 |
| 10~100건/시간 | **경고** | DIMM 교체 계획 수립 (1주 내) |
| 100건+/시간 | **긴급** | 즉시 교체 준비, VM 마이그레이션 |
| UE 1건이라도 | **최긴급** | 즉시 서버 분리 및 DIMM 교체 |

### IPMI SEL 주요 이벤트

| 이벤트 | 의미 | 모델 반영 |
|---|---|---|
| `Memory Correctable ECC` | DIMM CE 발생 | XGBoost `mce_page_count_24h` |
| `Memory Uncorrectable ECC` | **DIMM UE 발생 — 긴급** | XGBoost `mce_uncorrected_flag` |
| `CPU Upper Critical going high` | CPU 과열 | XGBoost `cpu_temp_mean_1h` |
| `Fan Lower Non-critical going low` | 팬 RPM 저하 | XGBoost `fan_rpm_anomaly` |
| `Power Supply Failure` | PSU 장애 | XGBoost `psu_voltage_stddev_1h` |

---

## 5. 현재 환경의 실제 진단

### 2026-04-07 기준 실제 상태

**node3 (18AFD199) — AI 학습 서버:**
```
DIMM: 8장 × 64GB = 512GB DDR4
EDAC: CE=0, UE=0 (전 채널 정상)
진단: 메모리 완전 정상, 장애 징후 없음
```

**vmgnode18 — ESXi 호스트:**
```
메모리: 384GB (Physical: 410,875,527,168 Bytes)
vmkernel 에러: 2건 (SCSI Sense 0x5 — 스토리지 파라미터 오류)
에러 간격: 7일 (2026-02-21 → 03-01)
진단: 스토리지 경미한 이슈, 메모리 정상
```

**vmgnode23 — ESXi 호스트:**
```
메모리: 384GB
vmkernel 에러: 10건 (SCSI Sense 0x5)
에러 간격: 평균 2~3일
최근 에러: 2026-04-06 22:22
진단: 스토리지 에러 빈도 증가 추세, RAID 컨트롤러 점검 권장
```

**vmgnode26 — ESXi 호스트 (가장 주의 필요):**
```
메모리: 384GB (Physical: 410,875,531,264 Bytes)
vmkernel 에러: 19건 (SCSI Sense 0x5 0x24/0x20)
에러 빈도: 2026-04-06 하루에만 19건 집중 발생!
IPMI SEL: 팬 RPM 저하 이벤트 (2026-04-03)
진단: ⚠️ 스토리지 에러 급증 + 팬 이상
      → RAID 컨트롤러 및 냉각 시스템 점검 필요
      → 현재는 메모리 에러는 아니지만, 환경 악화 시 메모리 영향 가능
```

**vmgnode30 — ESXi 호스트:**
```
메모리: 384GB
vmkernel 에러: 0건
VM: 1대 운영 중
진단: 완전 정상
```

---

## 6. 모델별 탐지 가능/불가능 영역 정리

| 에러 유형 | Chronos | MOIRAI | XGBoost | AnomalyT | 설명 |
|---|---|---|---|---|---|
| CE 급증 | ✅ | ✅ | ✅ | ✅ | **4모델 모두 탐지** |
| CE 점진적 증가 | ⚠️ | ⚠️ | ✅ | ⚠️ | XGBoost가 slope 피처로 가장 민감 |
| UE 발생 | ✅ | ✅ | ✅ | ✅ | 4모델 모두 극단값에 반응 |
| 온도 상승 | ❌ | ❌ | ✅ | ❌ | **XGBoost만** IPMI 피처 활용 |
| 팬 이상 | ❌ | ❌ | ✅ | ❌ | **XGBoost만** fan_rpm_anomaly 피처 |
| SCSI 에러 | ❌ | ❌ | ⚠️ | ❌ | vmkernel_error_cnt로 간접 반영 |
| 전압 불안정 | ❌ | ❌ | ✅ | ❌ | **XGBoost만** voltage_stddev 피처 |
| 워크로드 과부하 | ❌ | ❌ | ✅ | ❌ | **XGBoost만** cpu/mem 피처 |

**결론:**
- **CE/UE 에러**: 4개 모델 모두 효과적
- **환경 요인 (온도/팬/전압)**: XGBoost만 탐지 가능 → XGBoost 가중치가 0.35로 가장 높은 이유
- **복합 요인**: XGBoost의 45개 피처 종합 분석이 가장 강력
