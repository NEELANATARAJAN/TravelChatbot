#!/usr/bin/env bash
# One-command deployment of the vLLM backend to Google Cloud Run (NVIDIA L4 GPU).
#
#   ./deploy/deploy_gcp.sh            # set everything up, deploy, and point models.gcp.json at it
#   ./deploy/deploy_gcp.sh <URL>      # skip deploying; just point models.gcp.json at an existing URL
#
# Settings (all optional, pass as environment variables):
#   PROJECT_ID   Google Cloud project             (default: your gcloud default project)
#   REGION       Cloud Run region with L4 GPUs    (default: us-central1)
#   MODEL_ID     Hugging Face model               (default: Qwen/Qwen2.5-7B-Instruct)
#   VLLM_TAG     vllm/vllm-openai image version   (default: v0.21.0, see "If the GPU container fails")
#   BUCKET       Cloud Storage bucket for weights (default: <project>-vllm-models)
#   HF_TOKEN     only needed for gated models such as Llama
#   FORCE_BUILD=1   rebuild the image even if it exists
#
# What it does, in order:
#   1. Checks gcloud login, project and billing; enables the Google Cloud APIs it needs.
#   2. Reuses (or creates) VLLM_API_KEY in ./.env, the same key the Modal setup uses.
#   3. Creates the storage bucket and an Artifact Registry repository.
#   4. Builds the vLLM container image with Cloud Build.
#   5. Downloads the model into the bucket once, using a Cloud Run job.
#   6. Deploys the vLLM service on an L4 GPU that scales to zero.
#   7. Writes the service URL (plus /v1) into models.gcp.json and calls it once.
#
# Afterwards:  MODELS_FILE=models.gcp.json ./run.sh
#              ./deploy/deploy_gcp_chat.sh        (host the chat web app on Cloud Run as well)

set -euo pipefail
cd "$(dirname "$0")/.."

GCLOUD="${GCLOUD_CMD:-gcloud}"
REGION="${REGION:-us-central1}"
MODEL_ID="${MODEL_ID:-Qwen/Qwen2.5-7B-Instruct}"
VLLM_TAG="${VLLM_TAG:-v0.21.0}"
SERVICE="vllm-backend"; JOB="vllm-prefetch"; REPO="vllm-chat"
CONFIG="models.gcp.json"
say() { printf '\n==> %s\n' "$*"; }

# ---- 1. gcloud, project, billing, APIs -------------------------------------------------
say "Checking gcloud"
command -v "${GCLOUD%% *}" >/dev/null 2>&1 || { echo "gcloud is not installed. See https://cloud.google.com/sdk/docs/install"; exit 1; }
if [ -z "$($GCLOUD auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)" ]; then
  echo "Not logged in yet. A browser window will open."
  $GCLOUD auth login
fi
PROJECT_ID="${PROJECT_ID:-$($GCLOUD config get-value project 2>/dev/null || true)}"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "No project selected. Run:  gcloud config set project YOUR_PROJECT_ID   (or pass PROJECT_ID=...)"
  exit 1
fi
echo "Project: $PROJECT_ID   Region: $REGION"
billing="$($GCLOUD billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)"
if [ "$billing" = "False" ]; then
  echo "Billing is not enabled for $PROJECT_ID. Link a billing account in the Cloud Console first."
  exit 1
fi
say "Enabling Google Cloud APIs (the first time this takes a minute)"
$GCLOUD services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com --project "$PROJECT_ID"

# ---- 2. API key ------------------------------------------------------------------------
say "API key"
touch .env && chmod 600 .env
if ! grep -q '^VLLM_API_KEY=.' .env; then
  if command -v openssl >/dev/null 2>&1; then key="$(openssl rand -hex 24)"
  else key="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"; fi
  printf 'VLLM_API_KEY=%s\n' "$key" >> .env
  echo "Generated a new key and saved it in .env"
else
  echo "Reusing the key already in .env"
fi
VLLM_API_KEY="$(grep '^VLLM_API_KEY=' .env | tail -1 | cut -d= -f2-)"

url="${1:-}"
if [ -z "$url" ]; then
  BUCKET="${BUCKET:-${PROJECT_ID}-vllm-models}"
  IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/vllm:$VLLM_TAG"
  MODEL_DIR_NAME="${MODEL_ID##*/}"

  # ---- 3. bucket and registry ----------------------------------------------------------
  say "Storage bucket gs://$BUCKET and container registry '$REPO'"
  $GCLOUD storage buckets describe "gs://$BUCKET" --project "$PROJECT_ID" >/dev/null 2>&1 \
    || $GCLOUD storage buckets create "gs://$BUCKET" --project "$PROJECT_ID" \
         --location "$REGION" --uniform-bucket-level-access
  $GCLOUD artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 \
    || $GCLOUD artifacts repositories create "$REPO" --repository-format=docker \
         --location "$REGION" --project "$PROJECT_ID" --description "vLLM chat images"

  # ---- 4. image ------------------------------------------------------------------------
  if [ -n "${FORCE_BUILD:-}" ] || ! $GCLOUD artifacts docker images describe "$IMAGE" --project "$PROJECT_ID" >/dev/null 2>&1; then
    say "Building $IMAGE with Cloud Build (about 10 minutes the first time; the base image is large)"
    stage="$(mktemp -d)"; trap 'rm -rf "$stage"' EXIT
    cp deploy/gcp/vllm/start_vllm.sh "$stage/"
    sed "s|VLLM_TAG_PLACEHOLDER|$VLLM_TAG|" deploy/gcp/vllm/Dockerfile > "$stage/Dockerfile"
    $GCLOUD builds submit "$stage" --tag "$IMAGE" --project "$PROJECT_ID" --region "$REGION" --timeout=45m
  else
    say "Image $IMAGE already exists (set FORCE_BUILD=1 to rebuild)"
  fi

  # ---- 5. model weights into the bucket ------------------------------------------------
  if $GCLOUD storage ls "gs://$BUCKET/$MODEL_DIR_NAME/.download-complete" >/dev/null 2>&1; then
    say "Model $MODEL_ID is already in the bucket"
  else
    say "Downloading $MODEL_ID into the bucket with a Cloud Run job (about 5 to 10 minutes)"
    $GCLOUD run jobs deploy "$JOB" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
      --cpu 4 --memory 16Gi --task-timeout 60m --max-retries 1 \
      --command /start_vllm.sh --args prefetch \
      --set-env-vars "MODEL_ID=$MODEL_ID${HF_TOKEN:+,HF_TOKEN=$HF_TOKEN}" \
      --add-volume "name=models,type=cloud-storage,bucket=$BUCKET" \
      --add-volume-mount "volume=models,mount-path=/models" \
      --wait
  fi

  # ---- 6. the GPU service --------------------------------------------------------------
  say "Deploying $SERVICE on an NVIDIA L4 (scales to zero when idle)"
  if ! $GCLOUD run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
      --port 8000 --cpu 4 --memory 16Gi \
      --gpu 1 --gpu-type nvidia-l4 --no-gpu-zonal-redundancy \
      --no-cpu-throttling --min-instances 0 --max-instances 1 --concurrency 16 --timeout 900 \
      --set-env-vars "MODEL_ID=$MODEL_ID,VLLM_API_KEY=$VLLM_API_KEY,MAX_MODEL_LEN=8192" \
      --add-volume "name=models,type=cloud-storage,bucket=$BUCKET,readonly=true" \
      --add-volume-mount "volume=models,mount-path=/models" \
      --startup-probe "httpGet.path=/health,httpGet.port=8000,periodSeconds=10,timeoutSeconds=5,failureThreshold=60" \
      --allow-unauthenticated; then
    cat <<MSG

The GPU service could not be deployed. The usual causes:
  * No GPU quota. New projects often start with 0 L4 GPUs. In the Cloud Console open
    IAM & Admin > Quotas, search for "Nvidia L4 GPU allocation without zonal redundancy",
    pick region $REGION, and request 1.
  * A free-trial account that cannot use GPUs yet. Upgrade the account (the free credits stay).
  * $REGION does not offer L4 GPUs on Cloud Run. Try REGION=us-central1 or europe-west1.
  * Your organisation blocks public services (--allow-unauthenticated). Ask your admin, or use a
    personal Google account.
Fix that, then run this script again; finished steps are skipped.
MSG
    exit 1
  fi
  url="$($GCLOUD run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
  echo "Service URL: $url"
fi

# ---- 7. configure the chat app and test ------------------------------------------------
base="${url%/}"
case "$base" in */v1) ;; *) base="$base/v1" ;; esac
say "Writing $base into $CONFIG"
python3 - "$CONFIG" "$base" <<'PY'
import json, sys
path, base = sys.argv[1], sys.argv[2]
cfg = json.load(open(path))
for model in cfg["models"]:
    model["base_url"] = base
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY

say "Calling the endpoint once (this starts the GPU; a cold start can take a few minutes)"
ok=""
for attempt in $(seq 1 "${CHECK_ATTEMPTS:-45}"); do
  if out="$(curl -fsS --max-time 60 "$base/models" -H "Authorization: Bearer $VLLM_API_KEY" 2>&1)"; then
    ok=1; echo "$out"; break
  fi
  echo "  attempt $attempt: not ready yet (${out##*curl: })"; sleep "${CHECK_WAIT:-10}"
done

if [ -n "$ok" ]; then
  say "All set. Start the chat app with:"
  echo "    MODELS_FILE=models.gcp.json ./run.sh"
  echo "To host the chat web app on Cloud Run too:  ./deploy/deploy_gcp_chat.sh"
else
  say "The endpoint did not answer yet."
  echo "Look at the service logs:  $GCLOUD run services logs read $SERVICE --region $REGION --project $PROJECT_ID --limit 80"
  echo "If it is just slow, re-run:  ./deploy/deploy_gcp.sh $url"
  echo "If the logs show a CUDA or driver error, try another image version:  VLLM_TAG=<tag> FORCE_BUILD=1 ./deploy/deploy_gcp.sh"
  exit 1
fi
