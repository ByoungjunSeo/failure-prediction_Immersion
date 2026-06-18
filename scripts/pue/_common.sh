#!/bin/bash
##
## PUE Load Control — Common Functions
##
## Sourced by all pue/*.sh scripts.
## Do NOT execute directly.
##

set -euo pipefail

NS="failure-prediction"
MASTER_HOST="${PUE_MASTER_HOST:-newcluster-master}"

# ── Logging ──────────────────────────────────────────────────
_ts() { date "+%H:%M:%S"; }
info()  { echo "$(_ts) [INFO]  $*"; }
warn()  { echo "$(_ts) [WARN]  $*" >&2; }
error() { echo "$(_ts) [ERROR] $*" >&2; }

# ── Remote execution ─────────────────────────────────────────
ssh_master() {
  ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$MASTER_HOST" "$@"
}

# ── Cluster health check ────────────────────────────────────
check_cluster() {
  info "클러스터 노드 확인..."
  local NOT_READY
  NOT_READY=$(ssh_master "kubectl get nodes --no-headers | grep -v ' Ready ' | wc -l")
  if [ "$NOT_READY" -gt 0 ]; then
    error "Ready 아닌 노드 $NOT_READY 개 존재"
    ssh_master "kubectl get nodes" >&2
    return 1
  fi
  info "전 노드 Ready"
  return 0
}

# ── Inference health check ───────────────────────────────────
check_inference() {
  local HEAD
  HEAD=$(ssh_master "kubectl -n $NS get pod -l ray.io/node-type=head --no-headers 2>/dev/null | awk '{print \$1}' | head -1")
  if [ -z "$HEAD" ]; then
    error "Ray head pod 없음"
    return 1
  fi
  local RESP
  RESP=$(ssh_master "kubectl -n $NS exec $HEAD -- curl -s --max-time 30 http://localhost:8000/predict/node/all 2>/dev/null | wc -c")
  if [ "$RESP" -lt 10 ]; then
    error "추론 응답 비정상 (size=$RESP)"
    return 1
  fi
  info "추론 정상 (response size=$RESP bytes)"
  return 0
}

# ── NPU INTERVAL_SEC mapping ────────────────────────────────
# Maps target load level → INTERVAL_SEC for npu-load-generator.
# Based on empirical power measurements (2026-06-18):
#   OFF=45W, 5.0s=84W, 3.0s=102W, 1.0s=138W, 0.5s=155W, 0.2s=156W
# NPU TDP ~160W. Mapping by power fraction:
#   30% → ~80W  → 5.0s
#   50% → ~100W → 3.0s
#   90% → ~150W → 0.5s
#   99% → ~158W → 0.2s
npu_interval_for_target() {
  local TARGET=$1
  case $TARGET in
    30)  echo "5.0"  ;;
    50)  echo "3.0"  ;;
    90)  echo "0.5"  ;;
    99)  echo "0.2"  ;;
    *)
      # Fallback: linear interpolation between 5.0s (30%) and 0.2s (99%)
      echo "scale=1; 5.0 - ($TARGET * 48 / 1000)" | bc
      ;;
  esac
}
