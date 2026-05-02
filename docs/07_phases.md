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
