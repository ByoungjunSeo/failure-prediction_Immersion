#!/bin/bash
##
## PUE Status — Show current GPU/NPU utilization, power, inference health
##
## Usage: ./scripts/pue/_status.sh
##

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_common.sh"

NS="failure-prediction"

info "========== PUE 시스템 상태 =========="

# ── Deployments ───────────────────────────────────────────────
echo ""
info "[부하 컨트롤러]"
ssh_master '
  NS=failure-prediction
  echo "  GPU 부하:"
  kubectl -n $NS get deploy pue-gpu-load --no-headers 2>/dev/null \
    | awk "{printf \"    replicas: %s  ready: %s\n\", \$2, \$4}" \
    || echo "    (없음)"
  echo "  NPU 부하:"
  kubectl -n $NS get deploy npu-load-generator --no-headers 2>/dev/null \
    | awk "{printf \"    replicas: %s  ready: %s\n\", \$2, \$4}" \
    || echo "    (없음)"
  echo "  Watchdog:"
  kubectl -n $NS get deploy inference-watchdog --no-headers 2>/dev/null \
    | awk "{printf \"    replicas: %s  ready: %s\n\", \$2, \$4}" \
    || echo "    (없음)"
'

# ── GPU utilization per node ──────────────────────────────────
echo ""
info "[GPU 사용률 (노드별)]"
ssh_master '
  PROM=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus -o name | head -1)
  kubectl -n monitoring exec $PROM -c prometheus -- wget -qO- \
    "http://localhost:9090/api/v1/query?query=DCGM_FI_DEV_GPU_UTIL" 2>/dev/null \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in sorted(data[\"data\"][\"result\"], key=lambda x: x[\"metric\"].get(\"Hostname\",\"\")):
    node = r[\"metric\"].get(\"Hostname\", \"?\")
    val  = r[\"value\"][1]
    print(f\"    {node}: {val}%\")
"
'

# ── GPU temperature per node ──────────────────────────────────
echo ""
info "[GPU 온도 (노드별)]"
ssh_master '
  PROM=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus -o name | head -1)
  kubectl -n monitoring exec $PROM -c prometheus -- wget -qO- \
    "http://localhost:9090/api/v1/query?query=DCGM_FI_DEV_GPU_TEMP" 2>/dev/null \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in sorted(data[\"data\"][\"result\"], key=lambda x: x[\"metric\"].get(\"Hostname\",\"\")):
    node = r[\"metric\"].get(\"Hostname\", \"?\")
    val  = r[\"value\"][1]
    print(f\"    {node}: {val} C\")
"
'

# ── GPU power per node ────────────────────────────────────────
echo ""
info "[GPU 전력 (노드별)]"
ssh_master '
  PROM=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus -o name | head -1)
  kubectl -n monitoring exec $PROM -c prometheus -- wget -qO- \
    "http://localhost:9090/api/v1/query?query=DCGM_FI_DEV_POWER_USAGE" 2>/dev/null \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in sorted(data[\"data\"][\"result\"], key=lambda x: x[\"metric\"].get(\"Hostname\",\"\")):
    node = r[\"metric\"].get(\"Hostname\", \"?\")
    val  = float(r[\"value\"][1])
    print(f\"    {node}: {val:.0f}W\")
"
'

# ── NPU utilization ──────────────────────────────────────────
echo ""
info "[NPU 사용률]"
ssh_master '
  PROM=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus -o name | head -1)
  kubectl -n monitoring exec $PROM -c prometheus -- wget -qO- \
    "http://localhost:9090/api/v1/query?query=avg(furiosa_npu_core_utilization)" 2>/dev/null \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data[\"data\"][\"result\"]:
    val = float(r[\"value\"][1])
    print(f\"    avg: {val:.1f}%\")
" || echo "    (메트릭 없음)"
'

# ── NPU power ────────────────────────────────────────────────
echo ""
info "[NPU 전력]"
ssh_master '
  PROM=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=prometheus -o name | head -1)
  kubectl -n monitoring exec $PROM -c prometheus -- wget -qO- \
    "http://localhost:9090/api/v1/query?query=avg(furiosa_npu_hw_power)" 2>/dev/null \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
for r in data[\"data\"][\"result\"]:
    val = float(r[\"value\"][1])
    print(f\"    avg: {val:.0f}W\")
" || echo "    (메트릭 없음)"
'

# ── Inference response time ───────────────────────────────────
echo ""
info "[추론 응답 시간]"
ssh_master '
  NS=failure-prediction
  HEAD=$(kubectl -n $NS get pod -l ray.io/node-type=head --no-headers | awk "{print \$1}" | head -1)
  if [ -z "$HEAD" ]; then
    echo "    Ray head pod 없음"
  else
    for i in 1 2 3; do
      T=$(date +%s%N)
      kubectl -n $NS exec $HEAD -- curl -s --max-time 30 http://localhost:8000/predict/node/all >/dev/null 2>&1
      T2=$(date +%s%N)
      echo "    요청 $i: $(( (T2-T) / 1000000 ))ms"
    done
  fi
'

echo ""
info "========== 상태 확인 완료 =========="
