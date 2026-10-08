#!/usr/bin/env bash
# Host the chat web app on Cloud Run (CPU only, scales to zero, no GPU cost).
# Run ./deploy/deploy_gcp.sh first: this uses the URL it wrote into models.gcp.json and the key in .env.
#
#   ./deploy/deploy_gcp_chat.sh
#
# Settings: PROJECT_ID, REGION (same meaning as in deploy_gcp.sh).
# To change the prompts, edit prompts/*.md and run this script again (the prompt files are baked into the image).

set -euo pipefail
cd "$(dirname "$0")/.."

GCLOUD="${GCLOUD_CMD:-gcloud}"
REGION="${REGION:-us-central1}"
SERVICE="vllm-chat-ui"; REPO="vllm-chat"
say() { printf '\n==> %s\n' "$*"; }

PROJECT_ID="${PROJECT_ID:-$($GCLOUD config get-value project 2>/dev/null || true)}"
[ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "(unset)" ] || { echo "No project selected. Run: gcloud config set project YOUR_PROJECT_ID"; exit 1; }
grep -q '^VLLM_API_KEY=.' .env 2>/dev/null || { echo "No VLLM_API_KEY in .env. Run ./deploy/deploy_gcp.sh first."; exit 1; }
grep -q 'PASTE-YOUR' models.gcp.json && { echo "models.gcp.json has no backend URL yet. Run ./deploy/deploy_gcp.sh first."; exit 1; }
VLLM_API_KEY="$(grep '^VLLM_API_KEY=' .env | tail -1 | cut -d= -f2-)"

say "Packaging the chat app"
stage="$(mktemp -d)"; trap 'rm -rf "$stage"' EXIT
cp server.py requirements.txt "$stage/"
cp -r static prompts "$stage/"
cp models.gcp.json "$stage/models.json"
cp deploy/gcp/chat/Dockerfile "$stage/Dockerfile"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/chat:$(date +%Y%m%d-%H%M%S)"

$GCLOUD services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project "$PROJECT_ID"
$GCLOUD artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || $GCLOUD artifacts repositories create "$REPO" --repository-format=docker \
       --location "$REGION" --project "$PROJECT_ID" --description "vLLM chat images"

say "Building $IMAGE"
$GCLOUD builds submit "$stage" --tag "$IMAGE" --project "$PROJECT_ID" --region "$REGION"

say "Deploying $SERVICE"
# max-instances 1: the rate limiter keeps its counters in memory, so keep a single copy.
# timeout 900: longer than the model's cold start, because the chat app waits while the GPU wakes.
$GCLOUD run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
  --port 8080 --cpu 1 --memory 512Mi --cpu-boost \
  --min-instances 0 --max-instances 1 --concurrency 40 --timeout 900 \
  --set-env-vars "VLLM_API_KEY=$VLLM_API_KEY,TRUST_PROXY=1" \
  --allow-unauthenticated

url="$($GCLOUD run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
say "Chat app is live:"
echo "    $url"
echo "The first message after a quiet period waits for the GPU to start (minutes, not seconds)."
