#!/usr/bin/env bash
# Stop paying for the Google Cloud deployment.
#
#   ./deploy/teardown_gcp.sh          # delete the two Cloud Run services and the download job
#   ./deploy/teardown_gcp.sh --all    # ... and also the model bucket and the image registry
#
# Deleting the services removes the public URLs. The GPU only costs money while an instance runs,
# but the bucket (about 15 GB) and the images cost a little every month until deleted.
set -euo pipefail
cd "$(dirname "$0")/.."
GCLOUD="${GCLOUD_CMD:-gcloud}"
REGION="${REGION:-us-central1}"
PROJECT_ID="${PROJECT_ID:-$($GCLOUD config get-value project 2>/dev/null || true)}"
[ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "(unset)" ] || { echo "No project selected."; exit 1; }
BUCKET="${BUCKET:-${PROJECT_ID}-vllm-models}"

for svc in vllm-chat-ui vllm-backend; do
  $GCLOUD run services delete "$svc" --project "$PROJECT_ID" --region "$REGION" --quiet || echo "  ($svc was not deployed)"
done
$GCLOUD run jobs delete vllm-prefetch --project "$PROJECT_ID" --region "$REGION" --quiet || echo "  (vllm-prefetch was not deployed)"

if [ "${1:-}" = "--all" ]; then
  read -r -p "Also delete gs://$BUCKET and the 'vllm-chat' registry? The model must be downloaded again later. Type yes: " ans
  if [ "$ans" = "yes" ]; then
    $GCLOUD storage rm --recursive "gs://$BUCKET" || echo "  (bucket not found)"
    $GCLOUD artifacts repositories delete vllm-chat --project "$PROJECT_ID" --location "$REGION" --quiet || echo "  (registry not found)"
  else
    echo "Kept the bucket and registry."
  fi
fi
echo "Done."
