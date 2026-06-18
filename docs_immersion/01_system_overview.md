# 01. 시스템 개요

> 액침냉각(Immersion Cooling) 탱크 내 5노드 K8s 클러스터의 하드웨어 장애를 AI 앙상블로 예측하고, PUE 전력효율을 측정하는 통합 플랫폼.

---

## 배경

데이터센터 전력효율(PUE) 측정과 서버 신뢰성 검증을 위해, 액침냉각 탱크 안에 5대의 서버를 설치하고 Kubernetes 클러스터로 구성했습니다. 이 시스템은 두 가지 목표를 동시에 달성합니다:

1. **장애 예측**: 메모리 CE(Correctable Error) 패턴을 5개 AI 모델 앙상블로 분석하여 하드웨어 장애를 사전 예측
2. **PUE 측정**: GPU/NPU 부하를 정밀 제어하여 다양한 부하 수준에서의 전력효율(PUE) 측정

---

## 시스템 진화

| 항목 | 기존 (ESXi) | 현재 (액침) |
|---|---|---|
| 대상 서버 | ESXi 가상화 호스트 4대 | 액침탱크 내 K8s 노드 5대 |
| 모니터링 방식 | 외부 → ESXi SSH/pyVmomi 접근 | **자기 클러스터 자체 모니터링** |
| GPU | A100 80GB x 2 (학습 전용) | RTX 5060 Ti x 3 + RTX 5080 x 1 (추론 + PUE 부하) |
| NPU | 없음 | Furiosa RNGD (node5) |
| AI 모델 | 4개 (Chronos, MOIRAI, XGBoost, AT) | **5개** (+NPU LLM Embedding) |
| 자동 대응 | ESXi vMotion / Maintenance Mode | Slack 알림 + 대시보드 |
| 추가 기능 | 없음 | **PUE GPU/NPU 부하 제어** |
| 워크로드 | 추론만 | 추론 + Continual AT Training (PUE 부하 겸용) |

---

## 전체 아키텍처

```
                        ┌─────────────────────────────────────────────┐
                        │         액침냉각 탱크 (Immersion Tank)        │
                        │                                             │
  ┌──────────┐         │  ┌────────┐ ┌────────┐ ┌────────┐          │
  │ 운영자   │         │  │ node1  │ │ node2  │ │ node3  │          │
  │ (관리PC) │◄────────┤  │ master │ │GPU wkr │ │GPU wkr │          │
  │          │         │  │5060 Ti │ │5060 Ti │ │5060 Ti │          │
  └──────────┘         │  └────┬───┘ └────┬───┘ └────┬───┘          │
       │               │       │          │          │               │
       │               │  ┌────┴───┐ ┌────┴──────────┘               │
  Grafana              │  │ node4  │ │                               │
  Slack 알림            │  │GPU wkr │ │  ┌────────┐                  │
                        │  │ 5080   │ │  │ node5  │                  │
                        │  └────────┘ │  │NPU wkr │                  │
                        │             │  │Furiosa │                  │
                        │             │  │ RNGD   │                  │
                        │             │  └────────┘                  │
                        └─────────────────────────────────────────────┘
```

---

## 주요 구성요소

### 1. AI 앙상블 추론 (Ray Serve)
- **5개 모델**: Chronos, MOIRAI, XGBoost, Anomaly Transformer, NPU LLM Embedding
- GPU 노드 3대에 분산 (node2-4, 각 0.25 GPU per replica)
- 1분마다 5노드 전체 예측, VictoriaMetrics에 결과 저장

### 2. 데이터 수집 파이프라인
- **CE Simulator**: 합성 CE 데이터 생성 (학습용, 1분마다)
- **Self-Pred Push**: K8s 노드 메트릭 수집 + 예측 결과 push (1분마다)
- **Node Exporter**: CPU/메모리/디스크/네트워크 (Prometheus 기본)
- **DCGM Exporter**: GPU 메트릭 (온도/전력/사용률/VRAM)
- **Furiosa Metrics**: NPU 메트릭 (코어 사용률/전력/온도)
- **smartctl Exporter**: 디스크 S.M.A.R.T. 건강 상태

### 3. PUE 부하 제어
- **GPU Load Controller**: PI 피드백 제어로 GPU 사용률 목표 달성 (Anomaly Transformer continual training)
- **NPU Load Generator**: Furiosa RNGD에 임베딩 요청을 연속 전송
- **Inference Watchdog**: 추론 응답시간 5초 초과 시 GPU 부하 자동 정지
- **Web UI**: 브라우저에서 GPU 부하 목표값 조정 (NodePort 31600)

### 4. 모니터링 & 알림
- **Grafana**: 6개 대시보드 (AI 예측 점수, Node Overview, PUE GPU Load, DCGM, NPU, Node Exporter Full)
- **Prometheus**: kube-prometheus-stack (메트릭 수집/저장)
- **VictoriaMetrics**: AI 예측 점수 전용 시계열 DB
- **Alertmanager**: Slack 알림 (#액침서버_장애예측_알람)
- **vmalert**: AI 점수 기반 알림 규칙 (WARNING 0.65+, CRITICAL 0.85+)

### 5. 인프라
- **Container Registry**: 로컬 이미지 저장 (node1 hostPath)
- **Longhorn**: 분산 스토리지 (3-replica, 각 노드 2.7TB SSD)
- **PostgreSQL**: 메타데이터 DB
- **MLflow + MinIO**: 모델 실험 트래킹 및 아티팩트 저장

---

## 네트워크 구성

| 대역 | 용도 |
|---|---|
| `10.100.230.130-134` | K8s 노드 (node1-5) |
| `10.100.230.130:31618` | Grafana 대시보드 |
| `10.100.230.130:31494` | Ray Serve API |
| `10.100.230.130:30171` | VictoriaMetrics |
| `10.100.230.130:31600` | PUE Web UI |
| `10.100.230.130:5000` | Container Registry |
| `10.100.250.103` | 관리 워크스테이션 (SSH 점프호스트) |

---

## 기술 스택

| 구성요소 | 기술 |
|---|---|
| 오케스트레이션 | Kubernetes v1.35.4, containerd 2.2.3 |
| 추론 프레임워크 | Ray 2.9.0, Ray Serve (KubeRay v1.6.0) |
| AI 모델 | PyTorch 2.1, XGBoost 2.0, Chronos, MOIRAI, Anomaly Transformer |
| NPU 추론 | Furiosa SDK (furiosa-llm), Qwen3-Embedding-4B |
| 시계열 DB | VictoriaMetrics |
| 메트릭 수집 | Prometheus (kube-prometheus-stack) |
| GPU 모니터링 | NVIDIA DCGM Exporter |
| NPU 모니터링 | Furiosa Metrics Exporter |
| 디스크 모니터링 | smartctl-exporter |
| 대시보드 | Grafana |
| 알림 | Alertmanager → Slack |
| 분산 스토리지 | Longhorn |
| 모델 트래킹 | MLflow + MinIO |
| 메타데이터 | PostgreSQL + SQLAlchemy |
| 컨테이너 빌드 | nerdctl + buildkitd |
| OS | Rocky Linux 9.7 (node1-4), Ubuntu 22.04 (node5) |
