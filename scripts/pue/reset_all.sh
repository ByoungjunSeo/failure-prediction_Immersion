#!/bin/bash
##
## PUE Cluster Reset — Full recovery after reboot/incident
##
## Interactive: asks confirmation at each dangerous step.
## Usage: ./scripts/pue/reset_all.sh
##

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_common.sh"

info "═══════════════════════════════════════════════════"
info "  PUE 클러스터 전체 복구 시작"
info "  각 단계마다 확인. 위험 작업은 yes 입력 필요."
info "═══════════════════════════════════════════════════"

# ── Step 1. Cluster nodes ────────────────────────────────────
echo ""
info "[1/10] 클러스터 노드 확인"
ssh_master "kubectl get nodes"
NOT_READY=$(ssh_master "kubectl get nodes --no-headers | grep -v ' Ready ' | wc -l")
if [ "$NOT_READY" -gt 0 ]; then
  warn "Ready 아닌 노드 ${NOT_READY}개 있음"
else
  info "전체 Ready"
fi

# ── Step 2. Control-plane ────────────────────────────────────
echo ""
info "[2/10] control-plane 확인"
ssh_master "kubectl get pods -n kube-system --no-headers | grep -iE 'etcd|api-server|controller|scheduler' | head -5"

# ── Step 3. Node1 GPU diagnosis ──────────────────────────────
echo ""
info "[3/10] node1 GPU 상태 진단"
GPU_STATUS=$(ssh_master "ssh node1 'nvidia-smi --query-gpu=gpu_name --format=csv,noheader 2>&1'")
if echo "$GPU_STATUS" | grep -qi "no devices\|error\|failed"; then
  warn "node1 GPU 죽어있음: $GPU_STATUS"
  echo ""
  echo -n "  IPMI cold cycle 실행하시겠습니까? (yes/no): "
  read -r CONFIRM || CONFIRM=""
  if [ "$CONFIRM" = "yes" ]; then
    if [ -z "${IPMI_HOST:-}" ] || [ -z "${IPMI_USER:-}" ] || [ -z "${IPMI_PASS:-}" ]; then
      error "IPMI 환경변수 필요:"
      error "  export IPMI_HOST=10.100.231.130"
      error "  export IPMI_USER=admin"
      error "  export IPMI_PASS=<password>"
      exit 1
    fi
    info "IPMI Immediate Shutdown..."
    ipmitool -I lanplus -H "$IPMI_HOST" -U "$IPMI_USER" -P "$IPMI_PASS" chassis power off
    info "30초 대기 (PCIe 커패시터 방전)..."
    sleep 30
    info "Power ON..."
    ipmitool -I lanplus -H "$IPMI_HOST" -U "$IPMI_USER" -P "$IPMI_PASS" chassis power on
    info "부팅 대기 (3분)..."
    sleep 180

    # Verify
    GPU_CHECK=$(ssh_master "ssh node1 'nvidia-smi --query-gpu=gpu_name --format=csv,noheader 2>&1'" || true)
    if echo "$GPU_CHECK" | grep -qi "RTX"; then
      info "GPU 복구 성공: $GPU_CHECK"
    else
      warn "GPU 여전히 비정상: $GPU_CHECK"
      warn "물리적 점검 필요. CPU 워크로드만 가능."
    fi
  else
    info "cold cycle 건너뜀. node1 GPU 죽은 상태로 진행."
  fi
else
  info "node1 GPU 정상: $GPU_STATUS"
fi

# ── Step 4. Registry ─────────────────────────────────────────
echo ""
info "[4/10] 로컬 레지스트리 확인"
REG_STATUS=$(ssh_master "kubectl -n $NS get pod registry --no-headers 2>/dev/null | awk '{print \$3}'" || echo "NotFound")
if [ "$REG_STATUS" != "Running" ]; then
  warn "Registry 비정상 ($REG_STATUS)"
  echo -n "  Registry pod 재생성하시겠습니까? (yes/no): "
  read -r CONFIRM || CONFIRM=""
  if [ "$CONFIRM" = "yes" ]; then
    ssh_master "kubectl -n $NS delete pod registry --ignore-not-found=true" || true
    sleep 5
    ssh_master "cat <<'REGEOF' | kubectl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: registry
  namespace: failure-prediction
  labels:
    app: registry
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
      requests:
        cpu: \"100m\"
        memory: \"128Mi\"
  volumes:
  - name: registry-data
    hostPath:
      path: /home/registry
      type: DirectoryOrCreate
  restartPolicy: Always
REGEOF"
    sleep 10
    info "Registry 재생성 완료"
    ssh_master "kubectl -n $NS get pod registry"
  fi
else
  info "Registry Running"
fi

# ── Step 5. Ray cluster ──────────────────────────────────────
echo ""
info "[5/10] Ray 클러스터 확인"
ssh_master "kubectl -n $NS get raycluster 2>/dev/null || echo '  RayCluster 없음'"
ssh_master "kubectl -n $NS get pods -l ray.io/node-type --no-headers"

UNHEALTHY=$(ssh_master "kubectl -n $NS get pods -l ray.io/node-type --no-headers | grep -cvE 'Running|Completed' || echo 0")
if [ "$UNHEALTHY" -gt 0 ]; then
  warn "비정상 Ray pod ${UNHEALTHY}개"
  echo -n "  비정상 Ray pod 재시작하시겠습니까? (yes/no): "
  read -r CONFIRM || CONFIRM=""
  if [ "$CONFIRM" = "yes" ]; then
    ssh_master "kubectl -n $NS delete pod -l ray.io/node-type --field-selector 'status.phase!=Running' --ignore-not-found=true" || true
    info "60초 대기..."
    sleep 60
    ssh_master "kubectl -n $NS get pods -l ray.io/node-type --no-headers"
  fi
fi

# ── Step 6. Predictor distribution ───────────────────────────
echo ""
info "[6/10] predictor 노드 분포 확인"
ssh_master "kubectl -n $NS get pods -o wide --no-headers | grep -E 'head|worker' | awk '{printf \"  %-50s %s  %s\n\", \$1, \$3, \$7}'"

HEAD=$(ssh_master "kubectl -n $NS get pod -l ray.io/node-type=head --no-headers 2>/dev/null | awk '{print \$1}' | head -1")
if [ -n "$HEAD" ]; then
  ssh_master "kubectl -n $NS exec $HEAD -- serve status 2>&1 | head -20" || true
fi

# ── Step 7. NPU embedding ───────────────────────────────────
echo ""
info "[7/10] NPU 임베딩 서비스 확인"
ssh_master "kubectl -n $NS get deployment npu-embed 2>/dev/null || echo '  npu-embed 없음'"

# ── Step 8. CronJobs ─────────────────────────────────────────
echo ""
info "[8/10] CronJob 확인"
ssh_master "kubectl -n $NS get cronjob --no-headers 2>/dev/null || echo '  없음'"

# ── Step 9. Monitoring stack ─────────────────────────────────
echo ""
info "[9/10] 모니터링 스택 확인"
ssh_master "kubectl get pods -A --no-headers | grep -iE 'grafana|prometheus|victoria' | awk '{printf \"  %-25s %-50s %s\n\", \$1, \$2, \$4}' | head -10"

# ── Step 10. PUE controllers OFF + inference ─────────────────
echo ""
info "[10/10] PUE 컨트롤러 OFF 상태로 + 추론 확인"
ssh_master "
  kubectl -n $NS scale deployment pue-gpu-load --replicas=0 2>&1 || true
  kubectl -n $NS scale deployment npu-load-generator --replicas=0 2>&1 || true
"
sleep 5
ssh_master "kubectl -n $NS get deployment pue-gpu-load npu-load-generator --no-headers 2>/dev/null"

echo ""
info "추론 응답 시간 확인:"
if [ -n "$HEAD" ]; then
  ssh_master "
    for i in 1 2 3; do
      T=\$(date +%s%N)
      kubectl -n $NS exec $HEAD -- curl -s --max-time 30 http://localhost:8000/predict/node/all >/dev/null 2>&1
      T2=\$(date +%s%N)
      echo \"  요청 \$i: \$(( (T2-T) / 1000000 ))ms\"
    done
  "
else
  warn "Ray head 없음 — 추론 확인 불가"
fi

echo ""
info "═══════════════════════════════════════════════════"
info "  복구 완료"
info "  부하 시작: ./start_30.sh / start_50.sh / start_90.sh / start_99.sh"
info "═══════════════════════════════════════════════════"
