"""
modal_vllm.py - run a vLLM model on a Modal serverless NVIDIA GPU.

    modal deploy deploy/modal_vllm.py

Modal prints the endpoint URL. Put it (plus "/v1") in models.modal.json as base_url.

How it behaves, and why:
  * Scale to zero: with no traffic the GPU shuts down and you stop paying. The next request
    starts a container, which takes a while (see "cold starts" below).
  * One GPU at a time (max_containers=1): this caps spend. Concurrent users share that GPU;
    vLLM batches requests together, so a few users at once is fine.
  * The URL is public, so vLLM itself checks a bearer token (the VLLM_API_KEY stored in the
    Modal secret "vllm-chat-key"). Without the right key, requests are rejected.
  * Weights are cached in a Modal Volume, so only the very first start downloads the model.

Cold starts: loading a 7B model typically takes on the order of a minute or two. The first
start ever is longer because it downloads ~15 GB. FAST_BOOT=True skips some compilation to
shorten that, at a small cost in generation speed. Set it to False if the app stays busy.

Cost: Modal bills per second of GPU time while a container is up, INCLUDING the idle
scaledown_window after the last request. An L4 is about $0.80/hour at list price, so
the $30 monthly free credit is roughly 37 GPU-hours.
"""

import os
import subprocess

import modal

# ----- what to run (change these) ------------------------------------------------------
MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct"   # not gated; no Hugging Face token needed
GPU = "L4"                                 # 24 GB. 7-8B models in 16-bit fit with room for context
MAX_MODEL_LEN = 8192                       # context window; keep >= chat limits in models.json
FAST_BOOT = True                           # True = faster cold start, slightly slower tokens
SCALEDOWN_MINUTES = 5                      # idle time before the GPU shuts down (and billing stops)
# ----------------------------------------------------------------------------------------

MINUTES = 60
VLLM_PORT = 8000

vllm_image = (
    modal.Image.from_registry("nvidia/cuda:12.9.0-devel-ubuntu22.04", add_python="3.12")
    .entrypoint([])
    .uv_pip_install("vllm==0.21.0")
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})
)

hf_cache_vol = modal.Volume.from_name("huggingface-cache", create_if_missing=True)
vllm_cache_vol = modal.Volume.from_name("vllm-cache", create_if_missing=True)

app = modal.App("vllm-chat-backend")


@app.server(
    image=vllm_image,
    gpu=GPU,
    port=VLLM_PORT,
    scaledown_window=SCALEDOWN_MINUTES * MINUTES,
    startup_timeout=15 * MINUTES,
    max_containers=1,
    target_concurrency=16,
    volumes={
        "/root/.cache/huggingface": hf_cache_vol,
        "/root/.cache/vllm": vllm_cache_vol,
    },
    secrets=[modal.Secret.from_name("vllm-chat-key")],   # provides VLLM_API_KEY
    unauthenticated=True,   # Modal's own auth is off; vLLM enforces the API key instead
)
class Server:
    @modal.enter()
    def start(self):
        cmd = [
            "vllm", "serve", MODEL_NAME,
            "--host", "0.0.0.0",
            "--port", str(VLLM_PORT),
            "--api-key", os.environ["VLLM_API_KEY"],
            "--max-model-len", str(MAX_MODEL_LEN),
            "--gpu-memory-utilization", "0.90",
            "--uvicorn-log-level", "info",
        ]
        if FAST_BOOT:
            cmd.append("--enforce-eager")
        # Skip FlashInfer's sampler: it can spend minutes compiling kernels on a cold start.
        env = {**os.environ, "VLLM_USE_FLASHINFER_SAMPLER": "0"}
        self.process = subprocess.Popen(cmd, env=env)

    @modal.exit()
    def stop(self):
        self.process.terminate()