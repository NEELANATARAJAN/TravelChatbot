#!/usr/bin/env bash
# One-command Modal deployment for the chat app's vLLM backend.
#
#   ./deploy/deploy_modal.sh              # log in if needed, create key + secret, deploy, configure
#   ./deploy/deploy_modal.sh <URL>        # skip deploying; just point models.modal.json at an existing endpoint
#
# What it does, in order:
#   1. Makes sure you are logged in to Modal (opens a browser the first time).
#   2. Creates a random API key and saves it to ./.env (reused on later runs; .env is git-ignored).
#   3. Stores that key in Modal as the secret "vllm-chat-key" (what deploy/modal_vllm.py reads).
#   4. Deploys deploy/modal_vllm.py and finds the endpoint URL it prints.
#   5. Writes the URL (plus /v1) into models.modal.json.
#   6. Calls the endpoint once to confirm it works. This starts the GPU and can take a few minutes.
#
# Afterwards:  MODELS_FILE=models.modal.json ./run.sh

set -euo pipefail
cd "$(dirname "$0")/.."

# How to invoke the Modal CLI. Default: via uv, with the optional "deploy" dependency group.
MODAL="${MODAL_CMD:-uv run --group deploy modal}"
SECRET_NAME="vllm-chat-key"
CONFIG="models.modal.json"
say() { printf '\n==> %s\n' "$*"; }

# ---- 1. login --------------------------------------------------------------------------
say "Checking your Modal login"
# `modal profile current` prints "default" even when no token exists, so test with a real
# authenticated call instead.
profile="$($MODAL profile current 2>/dev/null || true)"
if ! $MODAL secret list >/dev/null 2>&1; then
  echo "Not logged in yet. A browser window will open; sign up or log in there."
  $MODAL setup
  $MODAL secret list >/dev/null 2>&1 || { echo "Modal login did not complete. Run: $MODAL token new"; exit 1; }
fi
echo "Logged in (profile: ${profile:-unknown})"

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

# ---- 3. Modal secret -------------------------------------------------------------------
say "Saving the key to Modal as secret '$SECRET_NAME'"
$MODAL secret create "$SECRET_NAME" "VLLM_API_KEY=$VLLM_API_KEY" --force >/dev/null
echo "Done"

# ---- 4. deploy -------------------------------------------------------------------------
url="${1:-}"
if [ -z "$url" ]; then
  say "Deploying deploy/modal_vllm.py (the first deploy builds the image and takes a few minutes)"
  log="$(mktemp)"
  $MODAL deploy deploy/modal_vllm.py 2>&1 | tee "$log"
  # Pick the endpoint URL out of Modal's output, ignoring the dashboard link.
  url="$(sed 's/\x1b\[[0-9;]*m//g' "$log" \
        | grep -Eo "https://[A-Za-z0-9._~:/?#@!\$&'()*+,;=%-]+" \
        | grep -v 'modal\.com' | head -1 || true)"
  if [ -n "$url" ]; then
    echo; echo "Detected endpoint: $url"
    echo "(If that looks wrong, re-run with the right one:  ./deploy/deploy_modal.sh <URL>)"
  else
    read -r -p "Could not find the endpoint URL in Modal's output. Paste it here: " url
  fi
fi

# ---- 5. configure the chat app ---------------------------------------------------------
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

# ---- 6. check it works -----------------------------------------------------------------
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
  echo "    MODELS_FILE=models.modal.json ./run.sh"
else
  say "The endpoint did not answer yet."
  echo "That is normal if the model is still downloading on first start. Watch it in the Modal"
  echo "dashboard (Apps > vllm-chat-backend > Logs), then re-run:  ./deploy/deploy_modal.sh $url"
  exit 1
fi