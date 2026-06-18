#!/bin/bash
##
## PUE Load Stop — Stop all GPU and NPU load
##
## Usage: ./scripts/pue/stop_all.sh
##

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_common.sh"

info "═══════════════════════════════════════════"
info "PUE 부하 전체 정지"
info "═══════════════════════════════════════════"

# ── Scale down ───────────────────────────────────────────────
ssh_master "
  kubectl -n $NS scale deployment pue-gpu-load --replicas=0 2>&1 || true
  kubectl -n $NS scale deployment npu-load-generator --replicas=0 2>&1 || true
"

info "30초 대기 (부하 해제)..."
sleep 30

# ── Verify ───────────────────────────────────────────────────
info "정지 확인:"
ssh_master "
  echo '  GPU 부하:'
  kubectl -n $NS get deploy pue-gpu-load --no-headers 2>/dev/null \
    | awk '{printf \"    replicas: %s\n\", \$2}'
  echo '  NPU 부하:'
  kubectl -n $NS get deploy npu-load-generator --no-headers 2>/dev/null \
    | awk '{printf \"    replicas: %s\n\", \$2}'
  echo ''
  echo '  남은 부하 pods:'
  kubectl -n $NS get pods --no-headers 2>/dev/null | grep -iE 'pue-gpu-load-[a-z]|npu-load-gen' || echo '    (없음)'
"

# ── Quick metrics check ──────────────────────────────────────
echo ""
info "GPU/NPU 사용률 (0% 근처 예상):"
ssh_master '
  PROM=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus -o name | head -1)
  echo "  GPU:"
  kubectl -n monitoring exec $PROM -c prometheus -- wget -qO- \
    "http://localhost:9090/api/v1/query?query=avg(DCGM_FI_DEV_GPU_UTIL)" 2>/dev/null \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data[\"data\"][\"result\"]:
    v = float(r[\"value\"][1])
    print(f\"    avg: {v:.1f}%\")
" || echo "    (메트릭 없음)"
  echo "  NPU:"
  kubectl -n monitoring exec $PROM -c prometheus -- wget -qO- \
    "http://localhost:9090/api/v1/query?query=avg(furiosa_npu_core_utilization)" 2>/dev/null \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data[\"data\"][\"result\"]:
    v = float(r[\"value\"][1])
    print(f\"    avg: {v:.1f}%\")
" || echo "    (메트릭 없음)"
'

info "═══════════════════════════════════════════"
info "PUE 부하 전체 정지 완료"
info "═══════════════════════════════════════════"
