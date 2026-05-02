# HPC 서버 장애 예측 시스템 — Kubernetes 전면 이전
# Claude Code 개발 지시

> 기존 서버 3대의 모든 서비스를 Kubernetes 클러스터로 완전 이전
> Ray Serve 기반 Pod 형태로 장애 예측 서비스 운영
> 기존 서버(node3/18AFD199 등)는 이전 완료 후 역할 종료

---

## 🖥️ Kubernetes 클러스터 구성

| 역할 | Hostname | IP | BMC | 주요 HW |
|---|---|---|---|---|
| **Master** | node1 | 10.100.230.6 | 10.100.230.106 | Xeon Platinum 8558 (192코어), Tesla T4 16GB×4, NVMe 1.92TB×2 |
| Slave | node2 | 10.100.230.41 | 10.100.231.41 | Xeon Gold 6140 (36코어), RAM 64GB |
| Slave | node3 | 10.100.230.42 | 10.100.231.42 | Xeon Gold 6140 (36코어), RAM 64GB |
| Slave | node4 | 10.100.230.43 | 10.100.231.43 | Xeon Gold 6140 (36코어), RAM 64GB |
| Slave | node5 | 10.100.230.44 | 10.100.231.44 | Xeon Gold 6140 (36코어), RAM 64GB |
| Slave | node6 | 10.100.230.45 | 10.100.231.45 | Xeon Gold 6140 (36코어), RAM 64GB |

> GPU: node1에만 Tesla T4 16GB × 4장
> 공통 계정: root / qwe123, Rocky Linux 9.7

---

## 📡 Claude Code 실행 위치

```
Claude Code는 기존 서버(node3/18AFD199, 10.100.230.71)에서 실행
kubectl로 k8s Master(10.100.230.6)를 원격 제어
이전 완료 후 Claude Code도 k8s Pod(Jupyter/shell)로 이동 가능
```

---

## 🏗️ 전체 아키텍처 — 모든 서비스 k8s 이전

```
Namespace: failure-prediction
│
├── [인프라 레이어]
│   ├── VictoriaMetrics StatefulSet  (메트릭 저장, 포트 8428)
│   ├── PostgreSQL StatefulSet       (레이블 DB, 포트 5432)
│   ├── MinIO StatefulSet            (모델 아티팩트, 포트 9000)
│   ├── MLflow Deployment            (실험 추적, 포트 5000)
│   └── Grafana Deployment           (대시보드, 포트 3000)
│
├── [수집 레이어]
│   ├── ESXi Collector CronJob       (1분마다 ESXi 메트릭 수집)
│   └── EDAC/IPMI Collector CronJob  (기존 서버 모니터링 계속)
│
├── [AI 레이어 — Ray Serve on KubeRay]
│   ├── RayCluster Head Pod (node1, GPU)
│   │   ├── ChronosPredictor     (T4 GPU, num_gpus=0.5)
│   │   ├── MOIRAIPredictor      (T4 GPU, num_gpus=0.5)
│   │   ├── AnomalyTransformer   (T4 GPU, num_gpus=0.5)
│   │   └── AnomalyEnsemble API  (FastAPI ingress)
│   └── RayCluster Worker Pods (node2~6, CPU)
│       └── XGBoostPredictor     (num_replicas=2)
│
├── [대응 레이어]
│   ├── ESXi Response CronJob    (1분마다 /predict/all → ESXi 대응)
│   └── Retrain CronJob          (새벽 2시 XGBoost 재학습)
│
└── [스토리지]
    └── PersistentVolume (node1 NVMe 1.92TB × 2 활용)
```

---

## 🧠 모델 구성

```
Chronos (Amazon)       → T4 GPU  : CE 시계열 Zero-shot 예측
MOIRAI (Salesforce)    → T4 GPU  : Zero-shot 이상탐지
XGBoost (파인튜닝)     → CPU     : Alibaba PAKDD 사전학습 + 자체 데이터
Anomaly Transformer    → T4 GPU  : 비지도 이상탐지
앙상블: (Chronos+MOIRAI)×0.4 + XGBoost×0.35 + AnomalyT×0.25
```

---

## 🚦 리스크 대응

| 확률 | 레벨 | 대응 |
|---|---|---|
| 0.65~0.85 | WARNING | ESXi VM 배치 차단 + Slack |
| 0.85+ | CRITICAL | Maintenance Mode + 긴급 알림 |
| 0.30- | RECOVERY | Maintenance Mode 해제 |

---

## 📏 코드 규칙

- Ray Serve: `@serve.deployment` + `@serve.ingress(app)` 구조
- Secret: k8s Secret으로만 관리, 코드 하드코딩 금지
- ConfigMap: 서버 목록/설정 관리
- logging 모듈 사용, print() 금지
- 평가지표: Accuracy 금지 → F1, AUC-PR, Recall
- GPU 배치: `nodeSelector: kubernetes.io/hostname: node1`
- Namespace: `failure-prediction`

---

## 🔄 현재 개발 단계

```
현재 Phase : K-P0 — k8s 환경 셋업
완료 Phase  : 없음
업데이트    : 2025-04-08
```

---

## 📁 상세 문서

| 문서 | 경로 | 내용 |
|---|---|---|
| 환경 구성 | `docs/k8s_01_environment.md` | kubectl, namespace, KubeRay, StorageClass |
| 인프라 서비스 | `docs/k8s_02_infra.md` | VictoriaMetrics/PG/MinIO/MLflow/Grafana k8s 배포 |
| 컨테이너 이미지 | `docs/k8s_03_images.md` | Docker 이미지 빌드 전략 |
| Ray Serve | `docs/k8s_04_rayserve.md` | RayCluster/RayService YAML + 앱 코드 |
| 데이터 수집 | `docs/k8s_05_data_collection.md` | ESXi 수집 CronJob |
| ESXi 연동 | `docs/k8s_06_esxi.md` | ESXi 대응 CronJob |
| 이전 절차 | `docs/k8s_07_migration.md` | 기존 데이터 이전 방법 |
| Phase 지시 | `docs/k8s_08_phases.md` | Phase별 Claude Code 입력 지시문 |
| 제약/검증 | `docs/k8s_09_constraints.md` | 금지 사항, 검증 체크리스트 |
