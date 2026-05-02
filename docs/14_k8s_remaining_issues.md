# 14. K8s 이관 — 남은 이슈 및 해결 방안

> 작성일: 2026-04-11
> 작업 서버: node3/18AFD199 (10.100.230.71)
> 작업 경로: /opt/failure_prediction

---

## 1. 현재 상태 요약

### 완료된 것

| 항목 | 상태 |
|---|---|
| K8s 클러스터 6노드 | Ready |
| 인프라 Pod 5개 | PostgreSQL, MinIO, VictoriaMetrics, MLflow, Grafana Running |
| Docker 이미지 2종 | cpu-latest, gpu-latest (Python 3.11 + protobuf 3.20) 레지스트리 push |
| GPU T4 x 4 | nvidia-smi 인식, nvidia.com/gpu: 4 K8s 등록, NVIDIA Container Toolkit |
| containerd /home 이전 | 디스크 문제 해결 (1.7TB 여유) |
| ESXi Collector CronJob | 1분마다 Prometheus import → VictoriaMetrics 정상 수집 |
| ESXi Response CronJob | 예측 결과 → VictoriaMetrics push 구현 |
| Grafana 대시보드 | 22패널 import, 데이터소스 2개 연결 |
| ensemble_app.py | /predict/all 라우트 수정, GPU/CPU 자동 감지 |

### 동작 확인된 것 (수동 Serve 배포 후)

| 항목 | 결과 |
|---|---|
| /health | healthy, 4모델 |
| /predict/all | 4대 서버 × 4모델 스코어 반환 |
| GPU 추론 (GPU head) | Chronos 1.0, AnomalyT 1.0 → WARNING 감지 |
| CPU dummy (CPU head) | 4모델 0.5 → NORMAL |
| ESXi 수집 → 대시보드 | esxi_cpu_usage, esxi_vmkernel_error_cnt 표시 |

---

## 2. 핵심 미해결 이슈

### 2.1 Ray head Pod 반복 재시작 (가장 중요)

**현상:**
- head Pod이 수 시간 후 CrashLoopBackOff (96회+ 재시작)
- 재시작될 때마다 Serve 앱이 사라짐
- CronJob이 API 접근 실패 → 예측 데이터 수집 중단

**원인:**
- KubeRay Operator가 head Pod의 readiness probe를 체크
- Ray Serve가 수동 배포되어 있어서, Pod 재시작 시 Serve 앱이 없음
- Serve 앱 없이 API 8000 포트가 응답 안 함 → readiness 실패 → 재시작 반복

**해결 방안:**

```yaml
# 방법 A: head Pod에 init script로 Serve 자동 배포
# RayCluster YAML의 head container에 lifecycle hook 추가
lifecycle:
  postStart:
    exec:
      command:
      - /bin/bash
      - -c
      - |
        sleep 30  # Ray 시작 대기
        cd /app/rayserve
        python -c "
        import ray, sys
        sys.path.insert(0, '/app')
        sys.path.insert(0, '/app/rayserve')
        ray.init(address='auto')
        import importlib
        spec = importlib.util.spec_from_file_location('ensemble_app', '/app/rayserve/ensemble_app.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from ray import serve
        serve.run(mod.ensemble, name='ensemble', route_prefix='/', host='0.0.0.0')
        "
```

```yaml
# 방법 B: readiness probe 비활성화
# RayCluster head template에서
containers:
- name: ray-head
  # readinessProbe를 명시적으로 제거하거나 느슨하게 설정
  readinessProbe:
    httpGet:
      path: /api/v1/healthcheck
      port: 52365
    initialDelaySeconds: 120
    periodSeconds: 30
    failureThreshold: 10
```

```yaml
# 방법 C: 별도 Deployment로 Serve 앱 배포 Job
# RayCluster와 별도로 Serve 배포 Job을 실행
apiVersion: batch/v1
kind: Job
metadata:
  name: deploy-serve
spec:
  template:
    spec:
      containers:
      - name: deployer
        image: 10.100.230.6:5000/failure-pred:cpu-latest
        command: ["python", "-c"]
        args:
        - |
          import ray, sys, time
          # Ray head에 연결
          ray.init(address="ray://failure-pred-head-svc.failure-prediction:10001")
          # Serve 앱 배포
          ...
      restartPolicy: OnFailure
```

### 2.2 GPU head Pod 크래시 (AnomalyTransformer)

**현상:**
- GPU 이미지로 head Pod을 실행하면 AnomalyTransformer 초기화 중 크래시
- `Fatal Python error: Aborted` → head Pod 재시작

**원인:**
- AnomalyTransformer가 worker Pod (CPU 노드)에 스케줄링될 때 GPU 없어서 크래시
- 또는 GPU 메모리 부족 (T4 15GB에 Chronos + MOIRAI + AT 동시 로드)

**해결 방안:**

```python
# ensemble_app.py에서 GPU 모델의 ray_actor_options에 GPU 리소스 명시
@serve.deployment(
    num_replicas=1,
    ray_actor_options={
        "num_cpus": 2,
        "num_gpus": 0.3,  # T4 1장을 3개 모델이 공유
    }
)
class ChronosPredictor:
    ...
```

또는 GPU 모델을 head Pod에서만 실행하도록 placement 제약:

```python
@serve.deployment(
    num_replicas=1,
    ray_actor_options={
        "num_cpus": 2,
        "resources": {"node:head": 0.001},  # head 노드에만
    }
)
```

### 2.3 VictoriaMetrics Prometheus import 지연

**현상:**
- `/api/v1/import/prometheus`로 push한 데이터가 즉시 쿼리에 안 나옴
- VictoriaMetrics 재시작 후에야 데이터 반영

**해결 방안:**
- CronJob에서 InfluxDB line protocol (`/write`)로 push → 즉시 반영
- 또는 promscrape가 Ray Serve `/metrics`를 수집하도록 설정 (현재 구현됨)

현재 Response CronJob이 Prometheus import를 사용하므로, `/write`로 변경:

```python
# CronJob에서
# 변경 전
requests.post(f"{VM_URL}/api/v1/import/prometheus", data=prom_data)

# 변경 후 (InfluxDB line protocol)
influx_lines = []
for srv, prob in predictions.items():
    influx_lines.append(f"failure_probability,server={srv} value={prob}")
requests.post(f"{VM_URL}/write", data="\n".join(influx_lines))
```

단, InfluxDB protocol은 메트릭명에 `_value` suffix가 붙으므로 대시보드 쿼리 조정 필요.

---

## 3. 안정화를 위한 작업 순서

```
1. head Pod readiness probe 조정 또는 Serve 자동 배포 구현
   → Pod 재시작 후에도 Serve 앱이 자동 복구
   
2. GPU 모델 리소스 할당 최적화
   → num_gpus 설정으로 GPU 모델이 head 노드 GPU에서만 실행
   
3. Response CronJob push 방식 변경
   → Prometheus import → InfluxDB write 또는 promscrape 수집
   
4. CronJob historyLimit 설정
   → successfulJobsHistoryLimit: 3, failedJobsHistoryLimit: 1
   
5. 전체 검증 후 기존 서비스 중지
```

---

## 4. 접속 정보

```
Grafana:         http://10.100.230.6:31618
VictoriaMetrics: http://10.100.230.6:30171/vmui
Ray Serve API:   http://10.100.230.6:31494 (Serve 배포 시에만 동작)

Claude Code 작업 경로: /opt/failure_prediction (node3, 10.100.230.71)
K8s config: ~/.kube/config → https://10.100.230.6:6443
```

---

## 5. Claude에 줄 지시문 (다음 세션)

```
K8s 이관 남은 이슈를 해결해줘:

1. Ray head Pod이 반복 재시작(96회+)되는 문제 해결
   - Serve 앱 자동 배포 (postStart lifecycle hook 또는 init container)
   - readiness probe 조정
   
2. GPU 모델 안정적 실행
   - ensemble_app.py에서 num_gpus 설정
   - AnomalyTransformer 크래시 방지
   
3. 예측 데이터가 VictoriaMetrics에 안정적으로 수집되도록
   - Response CronJob push 방식 최적화
   
문서: docs/14_k8s_remaining_issues.md 참고
작업 경로: /opt/failure_prediction
```
