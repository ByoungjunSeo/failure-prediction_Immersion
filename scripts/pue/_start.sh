#!/bin/bash
##
## PUE Load Start — Internal helper, called by start_XX.sh
##
## Usage (internal): _start.sh <GPU_TARGET> <NPU_TARGET>
##
## Do NOT call directly. Use start_30.sh, start_50.sh, etc.
##

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/_common.sh"

GPU_TARGET="${1:?Usage: _start.sh <GPU_TARGET> [NPU_TARGET]}"
NPU_TARGET="${2:-$GPU_TARGET}"

info "═══════════════════════════════════════════"
info "PUE 부하 시작: GPU=${GPU_TARGET}%, NPU=${NPU_TARGET}%"
info "═══════════════════════════════════════════"

# ── 1. 사전 체크 ─────────────────────────────────────────────
info "[1/4] 클러스터 상태 확인"
check_cluster || exit 1

info "추론 서비스 확인"
check_inference || { error "추론 비정상 — 부하 시작 중단"; exit 1; }

# ── 2. GPU 부하 ──────────────────────────────────────────────
GPU_INITIAL=$(initial_batch_for_target "$GPU_TARGET")
info "[2/4] GPU 부하: target=${GPU_TARGET}%, initial_batch=${GPU_INITIAL}"
ssh_master "
  kubectl -n $NS patch configmap pue-gpu-load-config --type merge \
    -p '{\"data\":{\"TARGET_GPU_UTIL\":\"${GPU_TARGET}\",\"INITIAL_BATCH\":\"${GPU_INITIAL}\"}}'
  kubectl -n $NS scale deployment pue-gpu-load --replicas=1
  kubectl -n $NS rollout restart deployment pue-gpu-load
  kubectl -n $NS rollout status deployment pue-gpu-load --timeout=120s
"
info "GPU 부하 컨트롤러 ON (target=${GPU_TARGET}%, batch=${GPU_INITIAL})"

# ── 3. NPU 부하 ─────────────────────────────────────────────
NPU_INTERVAL=$(npu_interval_for_target "$NPU_TARGET")
info "[3/4] NPU 부하: target=${NPU_TARGET}% → INTERVAL_SEC=${NPU_INTERVAL}"
ssh_master "
  kubectl -n $NS set env deploy/npu-load-generator INTERVAL_SEC=${NPU_INTERVAL}
  kubectl -n $NS scale deployment npu-load-generator --replicas=1
"
info "NPU 부하 생성기 ON (interval=${NPU_INTERVAL}s)"

# ── 4. 안정화 대기 + 검증 ────────────────────────────────────
info "[4/4] 안정화 대기 (3분)..."
sleep 180

info "상태 확인:"
"$SCRIPT_DIR/_status.sh"

info "═══════════════════════════════════════════"
info "PUE 부하 시작 완료: GPU=${GPU_TARGET}%, NPU=${NPU_TARGET}%"
info "═══════════════════════════════════════════"
