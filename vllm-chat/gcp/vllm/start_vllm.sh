#!/usr/bin/env bash
# Entry point of the vLLM container.
#   /start_vllm.sh            serve the model (what the Cloud Run service runs)
#   /start_vllm.sh prefetch   download the model into the bucket once (what the Cloud Run job runs)
#
# The bucket is mounted at /models. Weights are downloaded once and then read from there on
# every cold start, so starting a new instance does not depend on Hugging Face.
set -euo pipefail

MODEL_ID="${MODEL_ID:-Qwen/Qwen2.5-7B-Instruct}"
MODEL_DIR="${MODEL_DIR:-/models/${MODEL_ID##*/}}"
DONE_MARK="$MODEL_DIR/.download-complete"

if [ "${1:-}" = "prefetch" ]; then
  if [ -f "$DONE_MARK" ]; then echo "Model already in the bucket: $MODEL_DIR"; exit 0; fi
  echo "Downloading $MODEL_ID into $MODEL_DIR (this takes a few minutes)"
  mkdir -p "$MODEL_DIR"
  MODEL_ID="$MODEL_ID" MODEL_DIR="$MODEL_DIR" python3 - <<'PY'
import os
from huggingface_hub import snapshot_download
snapshot_download(repo_id=os.environ["MODEL_ID"], local_dir=os.environ["MODEL_DIR"],
                  allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model", "*.tiktoken"])
PY
  touch "$DONE_MARK"
  echo "Done."
  exit 0
fi

if [ ! -f "$DONE_MARK" ]; then
  echo "ERROR: $MODEL_DIR has no model. Run deploy_gcp.sh (it downloads the model first)." >&2
  exit 1
fi
: "${VLLM_API_KEY:?VLLM_API_KEY must be set}"

# --served-model-name keeps the name the chat app expects (the Hugging Face id).
# $EXTRA_ARGS is for experiments, e.g. EXTRA_ARGS="--enforce-eager".
# shellcheck disable=SC2086
exec vllm serve "$MODEL_DIR" \
  --served-model-name "$MODEL_ID" \
  --host 0.0.0.0 --port "${PORT:-8000}" \
  --api-key "$VLLM_API_KEY" \
  --max-model-len "${MAX_MODEL_LEN:-8192}" \
  --gpu-memory-utilization 0.90 \
  ${EXTRA_ARGS:-}
