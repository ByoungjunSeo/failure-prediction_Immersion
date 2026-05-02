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
