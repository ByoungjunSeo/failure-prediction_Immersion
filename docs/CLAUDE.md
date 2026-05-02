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
현재 Phase : 완료
완료 Phase  : P0~P7 전체 완료
업데이트    : 2026-04-07
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
