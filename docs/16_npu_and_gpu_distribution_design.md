# 16. NPU 통합 + GPU 균등 분산 설계 (신규 클러스터)

> 작성: 2026-05-13. 신규 5노드 클러스터의 GPU 4대 + NPU 1대를 모두 활용. NPU는 별도 OpenAI-compatible HTTP 서비스로 분리.

---

## 1. 자원 현황 (확정)

| 노드 | IP | 자원 | 비고 |
|---|---|---|---|
| node1 | 10.100.230.130 | RTX 5060 Ti 16GB | control-plane (taint 없음), `/home/registry` 호스트 |
| node2 | 10.100.230.131 | RTX 5060 Ti 16GB | GPU worker |
| node3 | 10.100.230.132 | RTX 5060 Ti 16GB | GPU worker |
| node4 | 10.100.230.133 | RTX 5080 16GB | GPU worker |
| node5 | 10.100.230.134 | Furiosa RNGD NPU, 251GB RAM | Ubuntu 22.04, **NPU 추론 전용** |

**전 GPU 균등 분산** — premium/standard 구분 없음. Ray가 free GPU에 deployment를 알아서 배치. node1도 control-plane 겸 GPU worker로 운용.

---

## 2. 핵심 결정: NPU는 별도 OpenAI-compatible HTTP 서비스

### 왜 별도 서비스인가
- Furiosa SDK 2026.2.x는 **furiosa-llm**만 지원 (Decoder-only LLM + Pooling). T5/AnomalyTransformer/MOIRAI 등 우리 4 기존 모델은 NPU에서 못 돈다.
- `pip install furiosa-llm`이 **Ray 2.55.1 + torch 2.10**을 의존성으로 강제 설치. 기존 GPU 이미지(Ray 2.40 + torch 2.7)와 단일 RayCluster로 묶으면 버전 충돌.
- 따라서 NPU pod = 독립 Deployment, `furiosa-llm serve`로 OpenAI 호환 HTTP API(/v1/embeddings) 띄움. Ray Serve의 NPU predictor는 HTTP 클라이언트로 호출.

### 통신 흐름
```
Ray Serve AnomalyEnsemble
    └─ LLMEmbeddingAnomalyPredictor (Ray 액터, CPU 1)
            │  HTTP POST /v1/embeddings
            ▼
       npu-embed-svc:8000 (ClusterIP)
            │
            ▼
  Deployment: npu-embed (node5, replicas=1)
       container: furiosa-llm serve /artifacts/qwen3-embed-0.6b
       requests: furiosa.ai/rngd=1
       PVC mount: /artifacts (Longhorn, qwen3 모델 캐시)
```

---

## 3. RayCluster 변경 (`k8s/rayserve/raycluster.yaml`)

### 워커 그룹 단일화 + Replica 4
- 기존: `cpu-workers` (replicas 3) — 워커 이미지가 gpu-latest로 라이브 수정된 상태
- 변경: **`gpu-workers`** (replicas **4**), 각 노드(node1~4) 1개씩, 각 1 GPU 보유
- head: GPU 없음, CPU coordinator만 (현재는 head에 nvidia.com/gpu=1 잡혀 있어서 node1~4 중 하나 점유 중)

### 핵심 변경
```yaml
spec:
  headGroupSpec:
    rayStartParams:
      num-cpus: "4"
      num-gpus: "0"        # head는 GPU 없음
    template.spec:
      nodeSelector: {}     # control-plane이든 어디든
      containers:
      - resources:
          limits: {cpu: "8", memory: "16Gi"}
          requests: {cpu: "2", memory: "8Gi"}
  workerGroupSpecs:
  - groupName: gpu-workers
    replicas: 4
    minReplicas: 4
    maxReplicas: 4
    rayStartParams:
      num-cpus: "8"
      num-gpus: "1"
    template.spec:
      nodeSelector:
        nvidia.com/gpu.present: "true"
      affinity:
        podAntiAffinity:                # 한 노드에 워커 2개 못 들어가게
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector: {matchLabels: {ray.io/group: gpu-workers}}
            topologyKey: kubernetes.io/hostname
      containers:
      - resources:
          limits: {cpu: "16", memory: "32Gi", nvidia.com/gpu: "1"}
          requests: {cpu: "4", memory: "16Gi", nvidia.com/gpu: "1"}
```

**결과:** GPU 4장 전부 점유 (node1~4 각 1개), head는 별도 노드 또는 GPU 노드 중 한 곳에 CPU만 점유.

---

## 4. Ray Serve 예측기 배치 (`k8s/rayserve/ensemble_app.py`)

### 변경 요약
| Deployment | replicas | ray_actor_options | 어디 돌까 |
|---|---|---|---|
| ChronosPredictor | 1 | `num_gpus: 0.5, num_cpus: 2` | GPU worker 한 군데 |
| MOIRAIPredictor | 1 | `num_gpus: 0.5, num_cpus: 2` | (Chronos와 같은 워커 GPU 공유 가능, 또는 다른 워커) |
| AnomalyTransformerPredictor | **4** | `num_gpus: 0.25, num_cpus: 1` | 4 워커에 1개씩 (작은 모델, 처리량) |
| XGBoostPredictor | 4 | `num_cpus: 1` | 어디든 (CPU만) |
| **LLMEmbeddingAnomalyPredictor (신규)** | 1 | `num_cpus: 0.5` | head 또는 worker (HTTP 호출만, NPU 직접 안 잡음) |
| AnomalyEnsemble | 1 | `num_cpus: 1` | head |

**GPU 점유 합계:** Chronos 0.5 + MOIRAI 0.5 + AT × 4 × 0.25 = **2.0 GPU 단위** 분배. Ray scheduler가 4 워커(각 1.0 GPU)에 분산. fractional 활용으로 4 GPU 전부 부분이라도 사용.

### 신규 predictor 클래스 스케치
```python
@serve.deployment(num_replicas=1, ray_actor_options={"num_cpus": 0.5},
                  health_check_period_s=30)
class LLMEmbeddingAnomalyPredictor:
    """CE 시계열 → 텍스트 → Qwen3-Embedding(NPU) → baseline 거리 → score."""
    NPU_URL = os.getenv("NPU_EMBED_URL", "http://npu-embed-svc.failure-prediction:8000/v1/embeddings")

    def __init__(self):
        # baseline embedding: 시작 시 평온한 패턴 임베딩을 계산해두고 캐시
        self._baseline = None
        self._client = httpx.AsyncClient(timeout=10.0)

    def _series_to_text(self, ce_values):
        """CE 시계열 → 자연어 요약."""
        recent = np.asarray(ce_values[-60:], dtype=np.float32)
        return (f"CE counts last 60 minutes: mean={recent.mean():.2f}, "
                f"max={recent.max():.0f}, std={recent.std():.2f}, "
                f"slope={(recent[-1]-recent[0]):.2f}, "
                f"nonzero_frac={(recent>0).mean():.2f}")

    async def _embed(self, text):
        r = await self._client.post(self.NPU_URL,
            json={"model": "qwen3-embed", "input": text})
        return np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)

    async def predict(self, ce_values):
        if not ce_values:
            return {"anomaly_score": 0.5, "model": "llm_embedding"}
        try:
            if self._baseline is None:
                self._baseline = await self._embed(
                    "CE counts last 60 minutes: mean=0.00, max=0, std=0.00, slope=0.00, nonzero_frac=0.00")
            emb = await self._embed(self._series_to_text(ce_values))
            # cosine distance → [0,1]
            cos = float(np.dot(emb, self._baseline) /
                       (np.linalg.norm(emb)*np.linalg.norm(self._baseline)+1e-9))
            score = max(0.0, min(1.0, (1 - cos)))
        except Exception as e:
            logger.warning("LLM-embed fail: %s", e); score = 0.5
        return {"anomaly_score": score, "model": "llm_embedding"}
```

### Ensemble 가중치 통합 — 보수적 단계
- Phase A: ensemble은 LLM 점수를 **로깅만**, weight=0. /predict/all 응답엔 `model_scores.llm_embedding` 추가됨.
- Phase B: 1주일치 점수 분석 후 의미있는 신호면 weight 0.1~0.2 부여, 4모델 weight 재조정.

---

## 5. NPU Deployment 구성

### Dockerfile.npu (신규, `/home/failure_prediction_build/Dockerfile.npu`)
```dockerfile
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.10 python3.10-venv python3-pip curl ca-certificates && \
    pip install --upgrade pip && \
    rm -rf /var/lib/apt/lists/*
# Furiosa SDK (NPU 추론용)
RUN pip install --no-cache-dir furiosa-llm==2026.2.1
WORKDIR /app
COPY scripts/build_npu_artifact.py /app/build_npu_artifact.py
ENTRYPOINT ["bash","-lc"]
CMD ["furiosa-llm serve /artifacts/qwen3-embed-0.6b --host 0.0.0.0 --port 8000"]
```
이미지 태그: `10.100.230.130:5000/failure-pred:npu-latest`. 추정 크기 ~3GB (Furiosa SDK + torch + transformers).

### Artifact 생성 — 일회성 Job
```yaml
# k8s/jobs/build-npu-artifact.yaml
apiVersion: batch/v1
kind: Job
metadata: {name: build-npu-artifact, namespace: failure-prediction}
spec:
  template:
    spec:
      nodeSelector: {furiosa.ai/npu.family: rngd}
      restartPolicy: OnFailure
      containers:
      - name: builder
        image: 10.100.230.130:5000/failure-pred:npu-latest
        command: ["python","/app/build_npu_artifact.py"]
        env:
        - {name: HF_HOME, value: /artifacts/.hf}
        - {name: ARTIFACT_DIR, value: /artifacts/qwen3-embed-0.6b}
        volumeMounts:
        - {name: artifacts, mountPath: /artifacts}
        resources:
          limits: {cpu: "16", memory: "32Gi", furiosa.ai/rngd: "1"}
      volumes:
      - name: artifacts
        persistentVolumeClaim: {claimName: pvc-npu-artifacts}
```
PVC `pvc-npu-artifacts` — Longhorn 10GB. 한 번 빌드해두면 Deployment에서 ReadOnly로 마운트.

### NPU Deployment + Service
```yaml
# k8s/infra/npu-embed.yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: npu-embed, namespace: failure-prediction}
spec:
  replicas: 1
  strategy: {type: Recreate}        # NPU 1대뿐이라 동시 2 pod 불가
  selector: {matchLabels: {app: npu-embed}}
  template:
    metadata: {labels: {app: npu-embed}}
    spec:
      nodeSelector: {furiosa.ai/npu.family: rngd}
      containers:
      - name: serve
        image: 10.100.230.130:5000/failure-pred:npu-latest
        ports: [{containerPort: 8000, name: openai-api}]
        resources:
          limits: {cpu: "16", memory: "32Gi", furiosa.ai/rngd: "1"}
          requests: {cpu: "4", memory: "16Gi", furiosa.ai/rngd: "1"}
        readinessProbe:
          httpGet: {path: /health, port: 8000}
          initialDelaySeconds: 60
          periodSeconds: 10
        volumeMounts:
        - {name: artifacts, mountPath: /artifacts, readOnly: true}
      volumes:
      - name: artifacts
        persistentVolumeClaim: {claimName: pvc-npu-artifacts}
---
apiVersion: v1
kind: Service
metadata: {name: npu-embed-svc, namespace: failure-prediction}
spec:
  selector: {app: npu-embed}
  ports: [{port: 8000, targetPort: 8000, name: openai-api}]
```

---

## 6. 구현 순서

| Phase | 산출물 | 검증 |
|---|---|---|
| 0 | (DONE) registry 복구 | curl `10.100.230.130:5000/v2/` → 200 |
| 1 | RayCluster gpu-workers replicas 4 + ensemble_app.py GPU 분산 | `ray status`에서 GPU 4장 인식, AT 4 replica RUNNING |
| 2 | Dockerfile.npu 빌드 + push, build-npu-artifact Job 실행 | PVC에 artifact 파일 생성, log "done" |
| 3 | npu-embed Deployment + Service apply | `kubectl exec` → curl `/v1/embeddings` → 200 + embedding 배열 |
| 4 | ensemble_app.py에 LLMEmbeddingAnomalyPredictor 추가 (weight 0) | `/predict/all` 응답에 `llm_embedding` 점수 포함 |
| 5 | (옵션) registry Deployment + Longhorn PVC 전환 | 정전 회복력 |
| 6 | 1주 모니터링 후 weight 조정 | model_scores 분포 분석 |

---

## 7. 미해결/위험

- **HF 다운로드 의존:** artifact build 시 Qwen3-Embedding-0.6B(~1.2GB)을 HF에서 받음. node5 인터넷 차단되면 사전 다운로드 후 PVC에 미리 적재 필요.
- **Furiosa SDK 토치 의존 충돌 가능:** furiosa-torch 2026.2 ↔ 시스템 PyTorch 비교 — 격리된 venv/컨테이너 안에서 동작하므로 RayCluster 워커엔 영향 없음.
- **CE → 텍스트 표현이 임베딩 신호로 적합한지 미검증** — Phase B에서 baseline vs 실측 차이가 충분히 분리되는지 확인 후 가중치 부여.
- **Qwen3-Embedding-0.6B 정확도 — 시계열 anomaly와 무관한 LLM이라 의미 있는 차이를 줄지 불확실.** 부족 시 0.6B → 4B 승격 또는 reranker로 전환.

---

## 8. 산출물 트리

```
k8s/
  rayserve/
    raycluster.yaml          ← gpu-workers replicas 4 + head no-GPU
    ensemble_app.py          ← Predictor placement + LLMEmbeddingAnomalyPredictor 추가
  infra/
    npu-embed.yaml           ← Deployment + Service (신규)
    pvc-npu-artifacts.yaml   ← Longhorn 10GB PVC (신규)
  jobs/
    build-npu-artifact.yaml  ← 일회성 Job (신규)
docker/ (또는 /home/failure_prediction_build/)
  Dockerfile.npu             ← 신규
scripts/
  build_npu_artifact.py      ← ArtifactBuilder 호출 스크립트 (신규)
docs/
  16_npu_and_gpu_distribution_design.md   ← 이 문서
```
