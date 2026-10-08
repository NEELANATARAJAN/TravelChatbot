# vLLM Chat

A Claude-style chat app for **self-hosted vLLM models**, built for external users.

- Users pick a model from a dropdown. That's the only setting they get.
- **Temperature, token limits, top-p, system prompt and every other generation setting are controlled on the server** and never appear in the UI or in what the browser sends.
- Streaming replies, chat history in a sidebar, Markdown with code blocks and tables, copy / regenerate / stop.

```
Browser  ──{ model id, messages }──▶  server.py  ──{ full request + params + API key }──▶  vLLM (one per model)
         ◀──────── streamed text ────          ◀─────────────── streamed tokens ─────────
```

## Setup with uv

This is a [uv](https://docs.astral.sh/uv/) project: `pyproject.toml` lists the dependencies and `uv.lock` pins exact versions. Nothing is installed globally. Everything goes into `./.venv`.

```bash
brew install uv                  # macOS. Other platforms: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync                          # creates .venv with Python 3.10+ (downloads one if needed) and installs the app
uv run python -m uvicorn server:app --port 8080     # run the server; or just use ./run.sh, which does both
```

| Command | What it does |
|---|---|
| `uv sync` | Install the app's dependencies (`fastapi`, `uvicorn`, `httpx`) into `.venv` |
| `uv sync --group deploy` | Also install the Modal CLI (only needed to deploy to Modal) |
| `uv run <cmd>` | Run a command inside the project environment, for example `uv run python dev/smoke_test.py` |
| `uv run --group deploy modal <args>` | Run the Modal CLI without installing it globally |
| `uv add <package>` | Add a dependency (updates `pyproject.toml` and `uv.lock`) |
| `uv lock --upgrade` | Refresh `uv.lock` to the latest compatible versions |

Always use `uv run` (or `source .venv/bin/activate`) for the dev scripts. Plain `python3 dev/mock_vllm.py` uses whatever Python is on your PATH, which usually doesn't have FastAPI installed.

`requirements.txt` is kept only as a fallback for machines without uv; `pyproject.toml` is the source of truth, so if you add a dependency, add it there.

## Quick start

### Try the UI with no GPU (works on a Mac)

```bash
uv run python dev/mock_vllm.py --port 8000 --model Qwen/Qwen2.5-7B-Instruct &
uv run python dev/mock_vllm.py --port 8001 --model meta-llama/Llama-3.1-8B-Instruct &
./run.sh                      # then open http://127.0.0.1:8080
```

The mock speaks vLLM's API with canned replies, so you can exercise the whole app. Ask it for "some code" or "a table" to see those render.

### Against real vLLM

vLLM needs **Linux with an NVIDIA GPU**. It will not run on an Apple Silicon Mac. Run it on a GPU server, then run this app anywhere that can reach it.

```bash
# on the GPU host
export VLLM_API_KEY=$(openssl rand -hex 24)
export HUGGING_FACE_HUB_TOKEN=hf_xxx          # for gated models like Llama
docker compose up -d                           # starts the two models in docker-compose.yml

# on the machine running the chat app
export VLLM_API_KEY=<same value>
./run.sh
```

Each `vllm serve` process serves **one** model on **one** port, so every model in the picker is its own vLLM instance. Add a service in `docker-compose.yml` and a matching entry in `models.json`.

> The compose file splits one GPU between two 7-8B models at ~45% each, which needs a large GPU (roughly 80 GB). On a 24 GB card, run one model, or use 4-bit (AWQ) versions.

### On Modal (serverless GPU, free monthly credit)

Modal gives new accounts $30 of free credit each month with no card required. It runs vLLM on a GPU only while someone is chatting, so you pay nothing while idle.

**One command:**

```bash
./deploy/deploy_modal.sh
MODELS_FILE=models.modal.json ./run.sh
```

The script logs you in to Modal (browser, first time only), creates a random API key in `./.env`, stores it in Modal as the secret `vllm-chat-key`, deploys `deploy/modal_vllm.py`, writes the endpoint URL into `models.modal.json`, and calls the endpoint once to confirm it works. `run.sh` picks the key up from `.env` automatically. Re-running the script is safe: it reuses the key. To only re-point the config at an existing endpoint, pass the URL: `./deploy/deploy_modal.sh https://...`.

**The same thing by hand:**

```bash
uv run --group deploy modal setup                         # one-time browser login
export VLLM_API_KEY=$(openssl rand -hex 24)               # keep this; the chat app needs the same value
uv run --group deploy modal secret create vllm-chat-key VLLM_API_KEY=$VLLM_API_KEY --force
uv run --group deploy modal deploy deploy/modal_vllm.py   # prints the endpoint URL
# put that URL + /v1 into models.modal.json as base_url, then:
MODELS_FILE=models.modal.json ./run.sh
curl https://YOUR-MODAL-URL/v1/models -H "Authorization: Bearer $VLLM_API_KEY"    # optional direct check
```

What to expect and how to stay inside the free credit:

- **The first message after a quiet period is slow** (the GPU has to start and load the model, measured on an L4: about 3 to 4 minutes, including about 2 minutes of vLLM engine start-up). The UI says so after a few seconds. The very first start is longer because it downloads about 15 GB once.
- **Billing is per second while the GPU is up**, including the idle `SCALEDOWN_MINUTES` (default 5) after the last request. An L4 is about $0.80/hour at list price, so $30 is roughly 37 GPU-hours.
- **`max_containers=1`** in the script caps spend at one GPU. `models.modal.json` also uses a lower `max_tokens` and rate limit than the default config.
- **`always_available: true`** in `models.modal.json` is deliberate. Without it the chat server's health check would wake the GPU and keep it running all day. The trade-off is that the picker can't show this model as "unavailable".
- Watch usage on the Modal dashboard, and stop the app with `modal app stop vllm-chat-backend`. Set a spending limit in your workspace billing settings if one is offered.

### On Google Cloud Run (L4 GPU that scales to zero)

Same idea as Modal, on Google Cloud. Both can exist side by side: each has its own config file, so you pick one when you start the chat server and can switch at any time.

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID       # billing must be enabled (the $300 trial credit counts)
./deploy/deploy_gcp.sh                           # build image, download model, deploy GPU service
MODELS_FILE=models.gcp.json ./run.sh             # chat server on your laptop, model on Cloud Run
./deploy/deploy_gcp_chat.sh                      # optional: host the chat web app on Cloud Run too
./deploy/teardown_gcp.sh                         # delete the services; add --all to delete bucket + images
```

- **What gets created:** a Cloud Storage bucket holding the model weights (downloaded once by a Cloud Run job), an Artifact Registry repository with the vLLM image (built by Cloud Build from `deploy/gcp/vllm/`), the `vllm-backend` GPU service, and (optionally) the `vllm-chat-ui` service. The same `VLLM_API_KEY` in `.env` protects the model endpoint, as on Modal.
- **Settings** (environment variables for `deploy_gcp.sh`): `PROJECT_ID`, `REGION` (default `us-central1`), `MODEL_ID`, `VLLM_TAG` (the `vllm/vllm-openai` image version), `BUCKET`, `HF_TOKEN` (gated models only), `FORCE_BUILD=1`.
- **Cost:** the L4 on Cloud Run is billed per second while an instance runs, idle time included, and not at all when it has scaled to zero. The bucket (about 15 GB) and the images cost a little every month; `teardown_gcp.sh --all` removes them. Google Cloud has no hard spending cap, so set a budget alert in Billing, and run the teardown script when you stop using it.
- **Cold starts** include loading the weights through the bucket mount, which can be slower than a local disk. The chat app waits for the GPU (up to the model's `timeout_seconds`), exactly as with Modal.
- **If the GPU service fails to deploy:** the script prints the usual causes, mostly missing L4 GPU quota (request 1 under IAM & Admin > Quotas) or an account that cannot use GPUs yet.
- **If the container starts but crashes with a CUDA or driver error,** try another image version: `VLLM_TAG=<tag> FORCE_BUILD=1 ./deploy/deploy_gcp.sh`. The default tag was chosen to match the vLLM version used on Modal, but it was not tested on Cloud Run's GPU driver.
- **Switching providers:** `MODELS_FILE=models.modal.json ./run.sh` or `MODELS_FILE=models.gcp.json ./run.sh`. Stop the one you are not using (`modal app stop vllm-chat-backend`, or `./deploy/teardown_gcp.sh`), because each bills while it runs.

## Configuring models and parameters: `models.json`

This file is the single place where you control the experience. Users cannot see or change any of it.

```jsonc
{
  "app":    { "title": "...", "greeting": "...", "suggestions": [...], "default_model": "qwen2.5-7b",
              "system_prompt_file": "prompts/system.md" },   // the assistant's instructions live in this file
  "limits": { "max_messages": 30, "max_message_chars": 8000, "max_total_chars": 24000,
              "requests_per_minute": 20, "max_concurrent": 16 },
  "models": [{
      "id": "qwen2.5-7b",                                   // what the browser sends
      "label": "Qwen 2.5 7B",                               // what users see
      "description": "Fast, well-rounded everyday model",   // shown beside the picker
      "base_url": "http://localhost:8000/v1",               // where this model's vLLM listens
      "vllm_model": "Qwen/Qwen2.5-7B-Instruct",             // must match `vllm serve --model`
      "params": { "temperature": 0.6, "top_p": 0.9, "max_tokens": 1024, "repetition_penalty": 1.05 },
      "system_prompt_file": null,                           // optional: a different prompt file for this model
      "always_available": false,                            // true = never health-check (serverless backends)
      "timeout_seconds": 300                                // how long to wait for a reply (cold starts need more)
  }]
}
```

- **The opening message** is the plain-text (Markdown) file `prompts/opening.md`, set with `"opening_message_file"` in `app`. The chat shows it as the assistant's first message before the user types anything, so it costs nothing and does not wake a sleeping GPU. The model is told what it said, so the user's answers make sense to it. The shipped one asks about destination and timing, number of days, focus areas (Eat, Stay, Buy, See & do), travellers and budget, and whether to include typical weather. Leave the setting out to show only the greeting and suggestion buttons.
- **The system prompt** is the plain-text file `prompts/system.md`. Edit it freely: the server re-reads it whenever it changes, so you do not need to restart. Write `{date}` anywhere to insert today's date. Paths are relative to the config file. To give one model its own prompt, add `"system_prompt_file": "prompts/other.md"` to that model. Precedence: the model's file, then the model's inline `"system_prompt"` text, then the shared file, then the shared inline text (inline text still works if you prefer it). A missing or empty prompt file stops the server at startup with a clear message. On Modal the prompt is applied by the chat server, not by the GPU app, so changing it never needs a redeploy.
- **`params`** are sent to vLLM exactly as written, so anything vLLM's chat endpoint accepts works (`top_k`, `min_p`, `presence_penalty`, `seed`, ...). `model`, `messages` and `stream` are reserved and ignored here.
- **`max_tokens`** is your cost and latency cap. Set it per model.
- Changes need a server restart.
- `limits` are in **characters**, not tokens, because that is cheap to enforce without loading a tokenizer. Keep `max_total_chars` comfortably under the model's `--max-model-len`. Roughly 4 characters per token for English, fewer for code and other languages. The oldest turns are dropped first when a conversation gets too long.

## What users can and can't do

| Can | Cannot |
|---|---|
| Choose among models you list | Change temperature, top-p, max tokens, stop words, etc. |
| Send and read messages, copy, regenerate, stop | Add a system prompt or tools (client `system`/`tool` messages are dropped) |
| Keep history in their own browser | See vLLM URLs, served model names, parameters or your API key |
| | Reach models not in `models.json` |

The browser's `/api/config` response contains only each model's `id`, `label`, `description` and whether it's currently reachable. Models that are down show as "(unavailable)" and can't be selected; the list refreshes every 30 seconds.

## Protections built in

- Strict Content-Security-Policy (no inline scripts or styles), `nosniff`, no referrer, no framing.
- All model output is HTML-escaped before display, and links are limited to `http(s)` and `mailto`.
- Request size cap, per-message and per-conversation caps, per-IP rate limit (429 + `Retry-After`), and a cap on simultaneous generations (503 when busy).
- Errors shown to users are friendly and generic. Internals (URLs, vLLM responses) go to the server log only. Prompts and replies are never logged.
- Client disconnects (closing the tab, Stop) cancel the upstream request so vLLM stops generating.

## Before you put it on the internet

1. **HTTPS and a reverse proxy** (nginx, Caddy, a cloud load balancer) in front of this app. Set `TRUST_PROXY=1` only when exactly one proxy sits in front and sets `X-Forwarded-For`; otherwise rate limiting would see every user as the proxy's IP.
2. **Don't expose vLLM.** Keep it on a private network or localhost (the compose file binds to `127.0.0.1`) and use `VLLM_API_KEY`.
3. **Authentication is not included.** If these are named users or you need accountability, put your SSO / auth proxy in front. Without it, anyone with the URL can use your GPUs, bounded only by the per-IP rate limit.
4. **One worker.** Rate-limit counters live in process memory, so `run.sh` starts a single worker. For several workers or machines, enforce limits at the proxy or add Redis.
5. **Chat history lives in each user's browser** (localStorage), not on your server. Clearing site data erases it, and it doesn't follow users between devices. If you need server-side history, that's a database plus auth, and a natural next step.

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI backend: model allowlist, server-side params, limits, streaming proxy to vLLM |
| `models.json` | Models, parameters, system prompt, limits, UI text |
| `static/` | The chat UI (`index.html`, `style.css`, `app.js`; no build step, no CDN) |
| `pyproject.toml`, `uv.lock` | Dependencies (uv project). `requirements.txt` is a pip fallback only |
| `run.sh` | `uv sync` then starts the server; loads `./.env` (`HOST`, `PORT`, `MODELS_FILE`, `VLLM_API_KEY`) |
| `deploy/deploy_modal.sh` | One-command Modal login, secret, deploy and config |
| `docker-compose.yml` | Two vLLM servers on one NVIDIA host |
| `deploy/modal_vllm.py` | Deploys a vLLM model to a Modal serverless GPU (scale to zero, API-key protected) |
| `models.modal.json` | Chat-app config for the Modal deployment (set `base_url` after deploying) |
| `deploy/deploy_gcp.sh`, `deploy/deploy_gcp_chat.sh`, `deploy/teardown_gcp.sh` | Google Cloud Run: backend (GPU), chat web app, and cleanup |
| `deploy/gcp/vllm/`, `deploy/gcp/chat/` | Dockerfiles and the container start script used by the GCP scripts |
| `models.gcp.json` | Chat-app config for the Google Cloud deployment (the script sets `base_url`) |
| `prompts/system.md`, `prompts/opening.md` | The assistant's instructions and its first message (editable text files) |
| `dev/mock_vllm.py` | Fake vLLM for development without a GPU |
| `dev/smoke_test.py` | End-to-end checks, including that injected parameters are ignored (`python dev/smoke_test.py`) |

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MODELS_FILE` | `./models.json` | Config path |
| `VLLM_API_KEY` | none | Bearer token sent to vLLM (pair with `vllm serve --api-key`) |
| `TRUST_PROXY` | off | `1` to read the client IP from `X-Forwarded-For` (single trusted proxy) |
| `AVAILABILITY_TTL` | `10` | Seconds to cache model health checks |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | Used by `run.sh` |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Server exits on start with a `models.json` error | Missing `id`, `label`, `base_url` or `vllm_model`, or a duplicate `id` |
| First reply on Modal takes minutes, or times out | Cold start. Raise `timeout_seconds` in `models.modal.json`; the very first start also downloads the model |
| Modal credit disappears quickly | The GPU is staying awake. Make sure `always_available` is `true`, nothing else is polling the endpoint, and lower `SCALEDOWN_MINUTES` |
| 401 from Modal / "couldn't answer that" | `VLLM_API_KEY` for the chat app doesn't match the value stored in the `vllm-chat-key` secret |
| Every model shows "(unavailable)" | vLLM isn't running, wrong `base_url`, wrong `VLLM_API_KEY`, or `vllm_model` doesn't exactly match the `--model` it was started with |
| "The model couldn't answer that" | vLLM rejected the request. Check the chat server log for vLLM's response (often context length, or a chat template that needs alternating roles) |
| Replies end abruptly with a "length limit" note | `max_tokens` in `models.json` is too low for the task |
| Rate limiting hits everyone at once | Behind a proxy without `TRUST_PROXY=1`, so all users share one IP |
