#!/bin/bash
# NPU pod entrypoint:
#   1) artifact가 PVC에 없으면 build_npu_artifact.py 실행 (initContainer 대체 가능)
#   2) furiosa-llm serve로 OpenAI 호환 HTTP API 시작 (포트 8000)

set -euo pipefail

ARTIFACT_DIR="${ARTIFACT_DIR:-/artifacts/qwen3-embed-0.6b}"
MODEL_ID="${MODEL_ID:-Qwen/Qwen3-Embedding-0.6B}"
SERVE_HOST="${SERVE_HOST:-0.0.0.0}"
SERVE_PORT="${SERVE_PORT:-8000}"

if [ ! -d "$ARTIFACT_DIR" ] || [ -z "$(ls -A "$ARTIFACT_DIR" 2>/dev/null)" ]; then
  echo "[entrypoint] artifact missing at $ARTIFACT_DIR — building first time"
  python /app/build_npu_artifact.py --model "$MODEL_ID" --save-dir "$ARTIFACT_DIR"
fi

echo "[entrypoint] starting furiosa-llm serve on ${SERVE_HOST}:${SERVE_PORT}"
exec furiosa-llm serve "$ARTIFACT_DIR" --host "$SERVE_HOST" --port "$SERVE_PORT"
