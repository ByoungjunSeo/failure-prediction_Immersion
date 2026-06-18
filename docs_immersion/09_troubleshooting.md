# 09. 트러블슈팅

> 주요 장애 시나리오와 해결 방법.

---

## GPU 장애

### GPU PCIe 사망 (Xid 79)

**증상**: `nvidia-smi`에서 "No devices found", DCGM 메트릭 중단, `rev ff`

**원인**: PCIe 버스 장애. node1에서 2회 발생 이력.

**해결**:
```bash
# 1. GPU 상태 확인
ssh newcluster-master "ssh node1 nvidia-smi 2>&1"

# 2. PCIe revision 확인 (ff = dead)
ssh newcluster-master "ssh node1 'lspci -vvv -s \$(lspci | grep -i nvidia | awk \"{print \\\$1}\") | grep Rev'"

# 3. IPMI cold cycle (PCIe 커패시터 방전)
export IPMI_HOST=10.100.231.130
export IPMI_USER=admin
export IPMI_PASS=<password>
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS chassis power off
sleep 30   # 필수: PCIe 커패시터 방전
ipmitool -I lanplus -H $IPMI_HOST -U $IPMI_USER -P $IPMI_PASS chassis power on
# 3분 부팅 대기

# 4. 복구 확인
ssh newcluster-master "ssh node1 nvidia-smi"
```

**자동 복구**: `./scripts/pue/reset_all.sh` Step 3에서 자동 진단 및 cold cycle 옵션 제공.

### GPU 온도 과열

**증상**: GPU 온도 85°C+ 지속, PUE 부하 컨트롤러가 batch 감소

**해결**:
1. PUE 부하 정지: `./scripts/pue/stop_all.sh`
2. 냉각 시스템 확인 (액침액 순환, 펌프 상태)
3. 온도 정상화 후 부하 재시작

### DCGM 메트릭 stale (0% 고정)

**증상**: Grafana에서 GPU 사용률이 0%로 고정, 실제로는 동작 중

**해결**:
```bash
# DCGM exporter pod 재시작
ssh newcluster-master "kubectl -n gpu-operator delete pod -l app=nvidia-dcgm-exporter \
  --field-selector spec.nodeName=<node>"
```

---

## Ray Serve 장애

### Ray Pod 반복 재시작

**증상**: head 또는 worker pod이 CrashLoopBackOff

**확인**:
```bash
ssh newcluster-master "kubectl -n failure-prediction logs <pod-name> --previous --tail=50"
```

**원인/해결**:
| 원인 | 해결 |
|---|---|
| KubeRay probe가 `wget` 호출 (이미지에 없음) | raycluster.yaml에 `curl` 기반 probe 명시 (이미 적용) |
| CUDA OOM | VRAM 부족. replica 수 조정 또는 GPU 부하 정지 |
| ConfigMap 오류 | `ensemble_app.py` 문법 에러. ConfigMap 갱신 후 pod 재시작 |

### Worker가 head에 join 안 함

**증상**: worker pod Running이지만 `ray status`에 미등장

**해결**:
```bash
# head 강제 삭제 후 worker도 재시작 필요
ssh newcluster-master "kubectl -n failure-prediction delete pod -l ray.io/cluster=failure-pred"
# 3-5분 대기
```

### 추론 응답 느림 (5초+)

**증상**: `/predict/node/all` 응답 5초 초과, watchdog 알림

**원인**:
1. GPU 부하가 추론과 경합 → PUE 부하 정지
2. 모델 콜드 스타트 → 첫 요청 후 정상화
3. CPU worker 부족 → XGBoost replica 확인

**해결**:
```bash
# PUE 부하 정지
./scripts/pue/stop_all.sh

# 응답시간 확인
ssh newcluster-master "kubectl -n failure-prediction exec \
  \$(kubectl -n failure-prediction get pod -l ray.io/node-type=head -o name | head -1) \
  -- curl -s -w '%{time_total}' http://localhost:8000/predict/node/all -o /dev/null"
```

---

## Container Registry 장애

### Registry Pod 미기동 (정전 후)

**증상**: ImagePullBackOff 에러, Registry pod 없음

**원인**: Registry는 bare Pod로, 정전/리부트 시 자동 복구 안 됨.

**해결**:
```bash
# reset_all.sh Step 4에서 자동 제공
./scripts/pue/reset_all.sh

# 또는 수동 재생성
ssh newcluster-master "kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: registry
  namespace: failure-prediction
  labels: {app: registry}
spec:
  nodeSelector:
    node-role.kubernetes.io/control-plane: \"\"
  tolerations:
  - key: node-role.kubernetes.io/control-plane
    operator: Exists
    effect: NoSchedule
  containers:
  - name: registry
    image: registry:2
    ports:
    - containerPort: 5000
      hostPort: 5000
    volumeMounts:
    - name: registry-data
      mountPath: /var/lib/registry
    resources:
      requests: {cpu: \"100m\", memory: \"128Mi\"}
  volumes:
  - name: registry-data
    hostPath: {path: /home/registry, type: DirectoryOrCreate}
  restartPolicy: Always
EOF"
```

### ImagePullBackOff

**증상**: Pod 생성 시 이미지 pull 실패

**확인**:
```bash
# Registry 접근 테스트
ssh newcluster-master "curl -s http://10.100.230.130:5000/v2/_catalog"

# 이미지 목록
ssh newcluster-master "curl -s http://10.100.230.130:5000/v2/failure-pred/tags/list"
```

**해결**:
1. Registry pod가 Running인지 확인
2. Registry pod 재시작
3. 이미지 재빌드 + push

---

## NPU 장애

### NPU embed 서비스 장애

**증상**: npu-embed pod CrashLoopBackOff 또는 Ready 안 됨

**확인**:
```bash
ssh newcluster-master "kubectl -n failure-prediction logs deploy/npu-embed --tail=50"
ssh newcluster-master "ssh node5 furiosa-smi"
```

**원인/해결**:
| 원인 | 해결 |
|---|---|
| NPU 디바이스 미감지 | `furiosa-smi` 확인. `--devices npu:0:*` 옵션 확인 |
| 아티팩트 손상 | PVC 삭제 후 `build-npu-artifact` job 재실행 |
| OOM | `resources.limits.memory` 증가 |

---

## 모니터링 장애

### Grafana 접속 불가

**확인**:
```bash
ssh newcluster-master "kubectl -n monitoring get pod -l app.kubernetes.io/name=grafana"
ssh newcluster-master "kubectl -n monitoring get svc monitoring-grafana"
```

**해결**:
```bash
ssh newcluster-master "kubectl -n monitoring rollout restart deployment monitoring-grafana"
```

### Prometheus 데이터 없음

**확인**:
```bash
ssh newcluster-master "kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus"
```

### Slack 알림 미수신

**확인**:
```bash
# Alertmanager 로그
ssh newcluster-master "kubectl -n monitoring logs \
  \$(kubectl -n monitoring get pod -l app.kubernetes.io/name=alertmanager -o name | head -1) \
  --tail=20"

# Slack webhook 시크릿 확인
ssh newcluster-master "kubectl -n monitoring get secret slack-secret -o jsonpath='{.data.webhook-url}' | base64 -d"
```

---

## 디스크 장애

### disk-pressure taint

**증상**: Pod Pending, `disk-pressure` taint

**확인**:
```bash
ssh newcluster-master "kubectl describe node <node> | grep -A5 Conditions"
ssh newcluster-master "ssh <node> df -h / /home"
```

**해결**:
1. Registry GC 실행: `kubectl create job --from=cronjob/registry-gc gc-$(date +%s) -n failure-prediction`
2. 불필요한 이미지 삭제: `ssh <node> nerdctl image prune -a`
3. 로그 정리

### Longhorn 볼륨 장애

```bash
ssh newcluster-master "kubectl -n longhorn-system get volumes"
# Longhorn UI: http://10.100.230.130:<longhorn-port>
```

---

## 전체 복구 절차

모든 장애 시나리오를 포괄하는 복구 스크립트:

```bash
./scripts/pue/reset_all.sh
```

이 스크립트는 인터랙티브로 10단계를 순차 실행하며, 위험 작업(IPMI cold cycle, Registry 재생성, Ray pod 삭제)은 `yes` 확인이 필요합니다.
