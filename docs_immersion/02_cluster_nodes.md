# 02. 클러스터 노드 구성

> 액침냉각 탱크 내 5노드 Kubernetes 클러스터 상세 사양.

---

## 노드 일람

| 노드 | IP | 역할 | OS | 가속기 | VRAM/NPU 메모리 |
|---|---|---|---|---|---|
| **node1** | 10.100.230.130 | control-plane + Ray head | Rocky 9.7 | RTX 5060 Ti 16GB | 16GB (추론 전용) |
| **node2** | 10.100.230.131 | GPU worker | Rocky 9.7 | RTX 5060 Ti 16GB | 16GB |
| **node3** | 10.100.230.132 | GPU worker | Rocky 9.7 | RTX 5060 Ti 16GB | 16GB |
| **node4** | 10.100.230.133 | GPU worker | Rocky 9.7 | RTX 5080 16GB | 16GB |
| **node5** | 10.100.230.134 | NPU worker | Ubuntu 22.04 | Furiosa RNGD | 48GB HBM |

모든 노드에 2.7TB SATA SSD (`/dev/sdb`)가 Longhorn 분산 스토리지용으로 할당되어 있습니다.

---

## 노드별 역할 상세

### node1 (control-plane)

| 항목 | 내용 |
|---|---|
| K8s 역할 | control-plane (etcd, api-server, scheduler, controller-manager) |
| Ray 역할 | head node (Ray GCS, Serve controller) |
| GPU 사용 | 추론만 (num-gpus: 0 → PUE 부하 제외) |
| 주요 Pod | Ray head, Container Registry, NPU Load Generator |

**GPU 상태 이력**:
- PCIe 사망 2회 (Xid 79 에러). IPMI cold cycle로 복구.
- 현재 정상 동작 중이나, PUE GPU 부하 대상에서 **영구 제외** (안전 조치).
- 추론 actor (Chronos/MOIRAI/AT)가 GPU VRAM 약 1GB 사용 중.

### node2 (GPU worker)

| 항목 | 내용 |
|---|---|
| K8s 역할 | worker node |
| Ray 역할 | GPU worker (Chronos/MOIRAI/AT replica 1) |
| GPU 사용 | 추론 + PUE 부하 (PI 제어 대상) |
| 주요 Pod | GPU worker, PUE GPU Load Controller, Inference Watchdog, MLflow, vmalert |

PUE GPU Load Controller 관제 Pod이 이 노드에서 실행됩니다.

### node3 (GPU worker)

| 항목 | 내용 |
|---|---|
| K8s 역할 | worker node |
| Ray 역할 | GPU worker (Chronos/MOIRAI/AT replica 2) |
| GPU 사용 | 추론 + PUE 부하 (PI 제어 대상) |
| 주요 Pod | GPU worker, Prometheus, Alertmanager, VictoriaMetrics, MinIO, Loki |

모니터링 스택 핵심 Pod들이 이 노드에 집중되어 있습니다.

### node4 (GPU worker)

| 항목 | 내용 |
|---|---|
| K8s 역할 | worker node |
| Ray 역할 | GPU worker (Chronos/MOIRAI/AT replica 3) |
| GPU 사용 | 추론 + PUE 부하 (PI 제어 대상) |
| GPU 모델 | **RTX 5080** (TDP 360W, 다른 노드의 2배) |
| 주요 Pod | GPU worker |

RTX 5080은 RTX 5060 Ti (TDP 180W) 대비 TDP가 높으므로, PUE Web UI에서 power utilization 계산 시 TDP 차이가 반영됩니다.

### node5 (NPU worker)

| 항목 | 내용 |
|---|---|
| K8s 역할 | worker node |
| NPU 역할 | Furiosa RNGD — Qwen3-Embedding-4B 임베딩 서비스 |
| NPU 사양 | HBM 48GB, TDP ~160W |
| OS | Ubuntu 22.04 (Furiosa SDK 요구사항) |
| 주요 Pod | npu-embed, Grafana (failure-prediction NS), Longhorn UI |

NPU는 furiosa-llm 프레임워크를 통해 OpenAI 호환 `/v1/embeddings` API로 서비스합니다.

---

## 접속 방법

### 관리 워크스테이션에서 접속

```bash
# 워크스테이션: 10.100.250.103, 계정: mlcommons
# 프로젝트 디렉토리: /mlcommons_cm/failure_prediction

# K8s master 접속
ssh newcluster-master   # → root@10.100.230.130

# kubectl 명령 실행
ssh newcluster-master "kubectl get nodes"
ssh newcluster-master "kubectl -n failure-prediction get pods"
```

### SSH 설정 (`~/.ssh/config`)

```
Host newcluster-master
  HostName 10.100.230.130
  User root
  IdentityFile ~/.ssh/id_ed25519_newcluster
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
```

### 노드 간 접속 (master에서)

```bash
ssh node1   # → 10.100.230.130 (자기 자신)
ssh node2   # → 10.100.230.131
ssh node3   # → 10.100.230.132
ssh node4   # → 10.100.230.133
ssh node5   # → 10.100.230.134
```

### IPMI (BMC) 접속

| 노드 | BMC IP | 용도 |
|---|---|---|
| node1 | 10.100.231.130 | GPU PCIe 장애 시 cold cycle |

```bash
# IPMI cold cycle (GPU PCIe 장애 복구)
export IPMI_HOST=10.100.231.130
export IPMI_USER=admin
export IPMI_PASS=<password>
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS chassis power off
sleep 30   # PCIe 커패시터 방전
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS chassis power on
```

---

## K8s 네임스페이스 구조

| 네임스페이스 | 용도 | 주요 리소스 |
|---|---|---|
| `failure-prediction` | **핵심 서비스** | Ray cluster, VictoriaMetrics, PostgreSQL, MLflow, MinIO, Grafana, PUE controllers |
| `monitoring` | 모니터링 스택 | Prometheus, Alertmanager, Grafana (kube-prometheus-stack), node-exporter |
| `gpu-operator` | GPU 관리 | DCGM exporter, device plugin, GPU feature discovery |
| `furiosa-system` | NPU 관리 | Furiosa device plugin, metrics exporter, feature discovery |
| `smartctl-system` | 디스크 모니터링 | smartctl-exporter (DaemonSet) |
| `longhorn-system` | 분산 스토리지 | Longhorn manager, CSI driver |
| `logging` | 로그 수집 | Loki, Promtail |
| `ray-system` | KubeRay 오퍼레이터 | kuberay-operator |
| `kube-system` | K8s 기본 | calico, coredns, kube-proxy, apiserver 등 |
