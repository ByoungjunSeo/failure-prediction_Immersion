# 07. 운영 가이드

> 일상적인 운영, 점검, 장애 대응 절차.

---

## 일상 점검

### 1. 시스템 상태 확인

```bash
# 전체 Pod 상태
ssh newcluster-master "kubectl -n failure-prediction get pods"

# PUE 상태 (GPU/NPU 사용률, 온도, 전력, 추론 응답)
./scripts/pue/_status.sh

# Ray Serve 상태
ssh newcluster-master "kubectl -n failure-prediction exec \
  \$(kubectl -n failure-prediction get pod -l ray.io/node-type=head -o name | head -1) \
  -- serve status"
```

### 2. 추론 동작 확인

```bash
# 5노드 전체 예측
ssh newcluster-master "kubectl -n failure-prediction exec \
  \$(kubectl -n failure-prediction get pod -l ray.io/node-type=head -o name | head -1) \
  -- curl -s http://localhost:8000/predict/node/all"

# 외부에서 직접 호출
curl -s http://10.100.230.130:31494/predict/node/all | python3 -m json.tool
```

### 3. GPU 상태 확인

```bash
# 노드별 GPU 상태
ssh newcluster-master "ssh node2 nvidia-smi"
ssh newcluster-master "ssh node3 nvidia-smi"
ssh newcluster-master "ssh node4 nvidia-smi"
```

### 4. NPU 상태 확인

```bash
ssh newcluster-master "ssh node5 furiosa-smi"
```

---

## PUE 부하 운영

### 부하 시작/정지

```bash
./scripts/pue/start_30.sh   # 30% 부하
./scripts/pue/start_50.sh   # 50% 부하
./scripts/pue/start_90.sh   # 90% 부하
./scripts/pue/stop_all.sh   # 전체 정지
```

### 상태 확인

```bash
./scripts/pue/_status.sh
```

출력 내용:
- 부하 컨트롤러 replicas/ready
- 노드별 GPU 사용률 / 온도 / 전력
- NPU 사용률 / 전력
- 추론 응답 시간

### 사고 후 전체 복구

```bash
./scripts/pue/reset_all.sh
```

인터랙티브 10단계 복구:
1. 클러스터 노드 확인
2. control-plane 확인
3. node1 GPU 진단 (IPMI cold cycle 옵션)
4. Registry 확인/재생성
5. Ray 클러스터 확인
6. Predictor 배포 확인
7. NPU embedding 확인
8. CronJob 확인
9. 모니터링 스택 확인
10. PUE 컨트롤러 OFF + 추론 검증

---

## CronJob 관리

### 상태 확인

```bash
ssh newcluster-master "kubectl -n failure-prediction get cronjob"
```

### 수동 실행

```bash
# XGBoost 수동 재학습
ssh newcluster-master "kubectl create job --from=cronjob/retrain-xgboost \
  retrain-manual-\$(date +%s) -n failure-prediction"

# Registry GC 수동
ssh newcluster-master "kubectl create job --from=cronjob/registry-gc \
  gc-manual-\$(date +%s) -n failure-prediction"
```

### 일시 중지/재개

```bash
# CE 시뮬레이터 일시 중지
ssh newcluster-master "kubectl -n failure-prediction patch cronjob ce-simulator \
  -p '{\"spec\":{\"suspend\":true}}'"

# 재개
ssh newcluster-master "kubectl -n failure-prediction patch cronjob ce-simulator \
  -p '{\"spec\":{\"suspend\":false}}'"
```

---

## 추론 코드 업데이트

### ensemble_app.py 수정 후 배포

```bash
# 1. ConfigMap 갱신
ssh newcluster-master "kubectl create configmap ensemble-app \
  --from-file=ensemble_app.py=/path/to/ensemble_app.py \
  -n failure-prediction --dry-run=client -o yaml | kubectl apply -f -"

# 2. Ray pod 재시작 (ConfigMap은 volume mount라 pod 재시작 필요)
ssh newcluster-master "kubectl -n failure-prediction delete pod \
  -l ray.io/cluster=failure-pred"

# 3. 배포 확인 (2-3분 소요)
ssh newcluster-master "kubectl -n failure-prediction get pods -l ray.io/cluster=failure-pred -w"
```

### 컨테이너 이미지 재빌드

```bash
# node1에서 이미지 빌드 (nerdctl + buildkitd)
ssh newcluster-master "cd /path/to/build && \
  nerdctl build -t 10.100.230.130:5000/failure-pred:gpu-latest . && \
  nerdctl push 10.100.230.130:5000/failure-pred:gpu-latest"

# Pod 재시작
ssh newcluster-master "kubectl -n failure-prediction rollout restart deployment <name>"
```

---

## 로그 확인

```bash
# Ray head 로그
ssh newcluster-master "kubectl -n failure-prediction logs \
  \$(kubectl -n failure-prediction get pod -l ray.io/node-type=head -o name | head -1) \
  --tail=100"

# PUE GPU Load Controller 로그
ssh newcluster-master "kubectl -n failure-prediction logs deploy/pue-gpu-load --tail=100"

# Inference Watchdog 로그
ssh newcluster-master "kubectl -n failure-prediction logs deploy/inference-watchdog --tail=50"

# NPU embed 로그
ssh newcluster-master "kubectl -n failure-prediction logs deploy/npu-embed --tail=50"

# CronJob 최근 실행 로그
ssh newcluster-master "kubectl -n failure-prediction logs \
  \$(kubectl -n failure-prediction get pod -l job-name --sort-by=.metadata.creationTimestamp -o name | tail -1) \
  --tail=50"
```

---

## Container Registry 관리

Registry는 node1의 hostPath(`/home/registry`)에 저장되는 standalone Pod입니다.

```bash
# Registry 상태
ssh newcluster-master "kubectl -n failure-prediction get pod registry"

# 저장소 크기
ssh newcluster-master "du -sh /home/registry"

# 수동 GC
ssh newcluster-master "kubectl create job --from=cronjob/registry-gc \
  gc-manual-\$(date +%s) -n failure-prediction"
```

**주의**: Registry는 Deployment가 아니라 bare Pod로 운영됩니다. 정전/재부팅 시 수동 재생성이 필요합니다. `reset_all.sh` Step 4에서 자동으로 안내합니다.

---

## 디스크 관리

```bash
# 각 노드 디스크 사용량
ssh newcluster-master "for n in node1 node2 node3 node4 node5; do \
  echo \"=== \$n ===\"; ssh \$n 'df -h / /home 2>/dev/null'; done"

# Longhorn 상태
ssh newcluster-master "kubectl -n longhorn-system get volumes"

# Registry 디스크 (node1)
ssh newcluster-master "du -sh /home/registry"
```

---

## 환경변수

### PUE 스크립트

| 변수 | 기본값 | 설명 |
|---|---|---|
| `PUE_MASTER_HOST` | `newcluster-master` | SSH 접속할 마스터 호스트 |
| `IPMI_HOST` | - | node1 BMC IP (reset_all.sh) |
| `IPMI_USER` | - | IPMI 사용자명 |
| `IPMI_PASS` | - | IPMI 비밀번호 |

### K8s ConfigMap/환경변수

| 변수 | 위치 | 설명 |
|---|---|---|
| `TARGET_GPU_UTIL` | pue-gpu-load-config CM | GPU 목표 사용률 |
| `INTERVAL_SEC` | npu-load-generator env | NPU 요청 간격 |
| `AT_THRESHOLD` | raycluster.yaml env | AT 재구성 오차 threshold |
| `VICTORIA_METRICS_URL` | raycluster.yaml env | VM 주소 |
