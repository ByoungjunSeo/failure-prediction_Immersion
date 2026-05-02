# 11. 학습 데이터 확보 시나리오

> 모델이 정확히 동작하려면 "정상 상태"와 "이상 상태"를 모두 경험해야 합니다.
> 현재는 ESXi에 VM도 없는 유휴 상태이므로, 단계별로 워크로드와 스트레스를 생성하여
> 다양한 패턴의 학습 데이터를 확보합니다.

---

## 현재 문제

```
지금 모델이 보는 데이터:
  - CE 에러: 0건 (node3 메모리 정상)
  - ESXi CPU: 0.1~0.2% (유휴)
  - ESXi MEM: 1.1% (유휴)
  - VM 수: 0~1대
  - vmkernel 에러: SCSI 에러만 (메모리 에러 아님)

→ 모델이 "유휴 상태 = 정상"만 학습
→ 실제 장애 발생 시 구분 불가
→ 현재 높은 스코어(0.88)는 "데이터 부족으로 판단 불가" 상태
```

---

## 시나리오 1: 정상 워크로드 생성 (1~2주)

**목적:** "정상적인 서버 운영 상태"의 기준선(baseline) 확보

### ESXi에 VM 배포

```
vmgnode18: VM 2~3대 (웹서버, DB 등 일반 워크로드)
vmgnode23: VM 2~3대
vmgnode26: VM 1~2대
vmgnode30: VM 2~3대

각 VM 사양 예시:
  - vCPU: 4개
  - RAM: 16~32GB
  - Disk: 100GB
```

### node3에서 AI 워크로드 실행

```bash
# GPU 워크로드 — 일반적인 학습 작업
# cuda:0은 추론 전담이므로 cuda:1에서 실행
CUDA_VISIBLE_DEVICES=1 python -c "
import torch
# 간단한 반복 연산으로 GPU + 메모리 부하
x = torch.randn(10000, 10000, device='cuda')
for i in range(1000):
    y = torch.mm(x, x)
"

# CPU + 메모리 워크로드
stress-ng --cpu 16 --vm 4 --vm-bytes 32G --timeout 3600
```

### 이 단계에서 수집되는 데이터

| 지표 | 기대값 | 의미 |
|---|---|---|
| ESXi CPU | 20~60% | 정상 운영 범위 |
| ESXi MEM | 30~70% | VM 메모리 사용 |
| CE 에러 | 0~낮은 수 | 정상 상태 기준선 |
| 온도 | 50~70°C | 정상 운영 온도 |

### 모델이 학습하는 것

```
"이 수준이 정상이다"를 학습
→ 나중에 이 범위를 벗어나면 이상으로 탐지
```

---

## 시나리오 2: 메모리 스트레스 테스트 (3~5일)

**목적:** 메모리에 부하를 줘서 CE 에러 발생 가능성 높이기

### 방법 A: 메모리 집중 워크로드

```bash
# node3에서 실행 — 대규모 메모리 할당/해제 반복
stress-ng --vm 8 --vm-bytes 48G --vm-method all --timeout 7200

# 또는 memtester (특정 메모리 영역 반복 테스트)
sudo memtester 32G 10
```

### 방법 B: ESXi VM 메모리 오버커밋

```
ESXi 호스트의 물리 메모리: 384GB

VM 메모리 합계를 물리 메모리의 120~150%로 설정:
  - VM 1: 128GB
  - VM 2: 128GB  
  - VM 3: 128GB
  - VM 4: 128GB
  합계: 512GB > 384GB (오버커밋)

→ ESXi가 Balloon/Swap을 사용하기 시작
→ 메모리 압박 상태에서 CE 발생 가능성 증가
→ esxi_mem_balloon_sum, esxi_mem_swapped_sum 데이터 수집
```

### 방법 C: NUMA 불균형 워크로드

```bash
# 특정 NUMA 노드에만 메모리 집중 — 불균형으로 에러 유발 가능
numactl --cpunodebind=0 --membind=0 stress-ng --vm 4 --vm-bytes 64G --timeout 3600
```

### 이 단계에서 수집되는 데이터

| 지표 | 기대값 | 의미 |
|---|---|---|
| CE 에러 | 0~수십 건 | 메모리 부하 시 CE 발생 여부 |
| 온도 | 65~85°C | 부하에 따른 온도 상승 |
| mem_balloon | > 0 | ESXi 메모리 회수 시작 |
| mem_swapped | > 0 | ESXi 스왑 사용 |
| cpu_usage | 70~95% | 고부하 상태 |

---

## 시나리오 3: 환경 변동 시뮬레이션 (1주)

**목적:** 온도/전압 변동과 CE 에러의 상관관계 데이터 확보

### 방법 A: 시간대별 워크로드 변동

```
새벽 2~6시:  최소 워크로드 (Idle)    → 온도 낮음, CE 0
오전 9~12시: 중간 워크로드 (50% CPU)  → 온도 중간
오후 1~6시:  최대 워크로드 (90% CPU)  → 온도 높음, CE 발생 가능
저녁 7~새벽: 점진적 감소              → 온도 하강

→ cron으로 자동 스케줄링
```

```bash
# /opt/failure_prediction/scripts/workload_schedule.sh
#!/bin/bash
HOUR=$(date +%H)

if [ $HOUR -ge 9 ] && [ $HOUR -lt 12 ]; then
    # 오전: 중간 부하
    stress-ng --cpu 8 --vm 2 --vm-bytes 16G --timeout 3600 &
elif [ $HOUR -ge 13 ] && [ $HOUR -lt 18 ]; then
    # 오후: 고부하
    stress-ng --cpu 24 --vm 6 --vm-bytes 48G --timeout 3600 &
else
    # 나머지: 경부하
    killall stress-ng 2>/dev/null
fi
```

### 방법 B: VM 라이브 마이그레이션 (vMotion)

```
VM을 호스트 간 이동시켜 메모리 전송 부하 발생:
  vmgnode18 → vmgnode23 (vMotion)
  vmgnode23 → vmgnode26 (vMotion)
  
→ 대량 메모리 복사 과정에서 CE 발생 가능
→ esxi_cpu_ready, esxi_mem_swapped 데이터 수집
```

---

## 시나리오 4: 인위적 CE 에러 주입 (주의! 테스트 환경에서만)

**목적:** 실제 CE 에러 데이터 확보 — 가장 중요한 학습 데이터

### 방법 A: einj (Error Injection Framework)

```bash
# Linux 커널의 APEI Error Injection
# ⚠️ 주의: 테스트 서버에서만 실행

# 모듈 로드
sudo modprobe einj

# CE (Correctable Error) 주입
echo 0x8 > /sys/kernel/debug/apei/einj/error_type    # Memory Correctable
echo 0x0 > /sys/kernel/debug/apei/einj/param1         # Physical address (0=자동)
echo 0x1 > /sys/kernel/debug/apei/einj/notrigger       
echo 1 > /sys/kernel/debug/apei/einj/error_inject

# rasdaemon이 감지하는지 확인
sudo ras-mc-ctl --errors
```

**einj 사용 시 장점:**
- 실제 하드웨어 에러 경로를 통과 → EDAC, rasdaemon이 정상 감지
- CE만 주입하면 서버에 영향 없음 (UE는 절대 주입하지 말 것!)
- 다양한 빈도/패턴으로 주입 가능

### 방법 B: 소프트웨어 시뮬레이션 (안전)

```bash
# EDAC sysfs에 직접 쓰기 (실제 에러는 아니지만 카운트 증가)
# 일부 커널에서만 가능

# 또는 rasdaemon DB에 직접 이벤트 삽입
sqlite3 /var/lib/rasdaemon/ras-mc_event.db "
INSERT INTO mc_event (timestamp, err_count, err_type, mc, top_layer, middle_layer)
VALUES (datetime('now'), 5, 'Corrected', 0, 0, 0);
"
```

### 방법 C: 가상 CE 시계열 생성 (가장 안전)

```bash
# scripts/inject_test_fault.py를 활용
# 다양한 패턴의 CE 시계열을 VictoriaMetrics에 직접 push

# 시나리오별:
# 1. 정상 → 급증 (DIMM 결함)
# 2. 서서히 증가 (DIMM 열화)  
# 3. 주기적 스파이크 (환경 요인)
# 4. 단일 극단값 (순간 결함)
```

---

## 시나리오 5: Alibaba 공개 데이터로 오프라인 학습 (병행)

**목적:** 실제 데이터 수집과 병행하여 XGBoost 사전학습 품질 향상

### Alibaba PAKDD 2021 데이터셋 활용

```
1. https://tianchi.aliyun.com/dataset/132973 다운로드
2. data/alibaba_pakdd2021/ 에 배치
3. XGBoost 사전학습 실행:

   python -c "
   from src.models.xgboost_predictor import XGBoostPredictor
   import pandas as pd
   
   # Alibaba 데이터 로드
   X = pd.read_csv('data/alibaba_pakdd2021/kernel_errors.csv')
   y = pd.read_csv('data/alibaba_pakdd2021/failure_labels.csv')
   
   # 학습
   xgb = XGBoostPredictor(device='cuda:1')
   xgb.optimize_hyperparams(X, y, n_trials=30)
   xgb.train(X, y)
   xgb.save()
   "
```

---

## 권장 실행 순서

```
주차별 계획:

Week 1~2: 시나리오 1 (정상 워크로드)
  → ESXi에 VM 배포, node3에 워크로드 실행
  → "정상 상태" 기준선 확보
  → 이 기간 동안 Alibaba 데이터로 XGBoost 오프라인 학습 (시나리오 5 병행)

Week 3~4: 시나리오 2 (메모리 스트레스)
  → 메모리 오버커밋, NUMA 불균형
  → CE 에러 발생 여부 관찰
  → Anomaly Transformer 재학습 (정상 2주 데이터로)

Week 5: 시나리오 3 (환경 변동)
  → 시간대별 워크로드 변동
  → 온도-CE 상관관계 데이터

Week 6: 시나리오 4 (CE 주입)
  → einj로 실제 CE 주입 (테스트 서버에서)
  → 다양한 패턴 (급증, 점진, 주기적)
  → 이 데이터로 전체 모델 재학습

Week 7~8: 모델 재학습 + 앙상블 최적화
  → 6주간 축적된 데이터로 전체 재학습
  → Optuna로 앙상블 가중치 최적화
  → F1 > 0.75 목표 달성 확인
```

---

## 각 시나리오에서 모델이 학습하는 것

| 시나리오 | Chronos | MOIRAI | XGBoost | AnomalyT |
|---|---|---|---|---|
| 1. 정상 워크로드 | 정상 CE 패턴 | 정상 시계열 기준 | 정상 피처 범위 | 정상 구조 학습 |
| 2. 메모리 스트레스 | CE 변동 패턴 | 스트레스 시 패턴 | 고부하 피처 | 스트레스 구조 |
| 3. 환경 변동 | 시간대별 CE | 주기적 패턴 | 온도-CE 상관 | 변동 구조 |
| 4. CE 주입 | **CE 급증 예측** | **이상 패턴 탐지** | **장애 분류** | **이상 구조 탐지** |
| 5. Alibaba 데이터 | - | - | **다양한 장애 패턴** | - |

---

## 최소한의 즉시 실행 가능 항목

시간이 부족하다면 최소한 아래만이라도 실행하세요:

### 1. ESXi에 VM 띄우기 (30분)
```
각 호스트에 VM 2대씩 배포 → 실제 운영과 유사한 환경 조성
```

### 2. node3에 stress-ng 설치 및 실행 (10분)
```bash
sudo dnf install -y stress-ng
# 4시간 동안 중간 부하
stress-ng --cpu 16 --vm 4 --vm-bytes 32G --timeout 14400 &
```

### 3. Alibaba 데이터 다운로드 (20분)
```bash
# Tianchi 계정으로 다운로드 후
python scripts/download_public_data.py
```

이 3가지만 해도 **1주일 후에는 현재보다 훨씬 나은 학습 데이터**를 확보할 수 있습니다.
