"""
server.py - chat backend for self-hosted vLLM models, built for external users.

    Browser  --(model id + messages only)-->  this server  --(full request)-->  vLLM

The browser can choose WHICH configured model to talk to and send the conversation.
Everything else is decided here and never reaches the client:

  * generation parameters (temperature, top_p, max_tokens, ...)   -> models.json "params"
  * the system prompt                                             -> prompts/system.md (or models.json)
  * vLLM addresses, served model names, API key                   -> models.json / VLLM_API_KEY
  * rate limit, message-size caps, concurrency cap                -> models.json "limits"

Any extra field a client adds to a request (temperature, max_tokens, system messages,
tools, ...) is ignored, so a user cannot tune the model or inflate cost by editing requests.

Environment:
    MODELS_FILE         path to the model config            (default: ./models.json)
    VLLM_API_KEY        bearer token sent to vLLM            (optional; use with `vllm serve --api-key`)
    TRUST_PROXY         "1" if behind ONE reverse proxy that sets X-Forwarded-For
    AVAILABILITY_TTL    seconds to cache model health checks (default: 10)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chat")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MODELS_FILE", BASE_DIR / "models.json"))
VLLM_API_KEY = os.environ.get("VLLM_API_KEY", "")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "") == "1"
AVAILABILITY_TTL = float(os.environ.get("AVAILABILITY_TTL", "10"))
MAX_BODY_BYTES = 256 * 1024

DEFAULT_APP = {
    "title": "Assistant",
    "greeting": "How can I help you today?",
    "suggestions": [],
    "default_model": None,
    "system_prompt": "You are a helpful assistant. Today's date is {date}.",
}
DEFAULT_LIMITS = {
    "max_messages": 30,
    "max_message_chars": 8000,
    "max_total_chars": 24000,
    "requests_per_minute": 20,
    "max_concurrent": 16,
}
RESERVED_PARAMS = {"model", "messages", "stream", "stream_options"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_prompt_cache: dict[Path, tuple[float, str]] = {}


def read_prompt_file(path: Path) -> str:
    """Read a prompt file, re-reading it only when it changes on disk. If it becomes
    unreadable later, keep serving the last good text instead of failing chats."""
    try:
        mtime = path.stat().st_mtime
        hit = _prompt_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        text = path.read_text(encoding="utf-8")
        _prompt_cache[path] = (mtime, text)
        return text
    except OSError as err:
        if path in _prompt_cache:
            log.warning("Could not read %s (%s); using the last version", path, err)
            return _prompt_cache[path][1]
        raise RuntimeError(f"Cannot read system prompt file {path}: {err}") from err


def system_prompt_for(model: dict[str, Any]) -> str:
    """Order of precedence: this model's file, this model's inline text, the app-wide file,
    the app-wide inline text."""
    for holder in (model, APP_CFG):
        if holder.get("system_prompt_file"):
            text = read_prompt_file(holder["system_prompt_file"]).strip()
            if text:
                return text
        if holder.get("system_prompt"):
            return holder["system_prompt"]
    return DEFAULT_APP["system_prompt"]


def load_config(path: Path) -> dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    models = cfg.get("models") or []
    if not models:
        raise RuntimeError(f"{path}: 'models' must list at least one vLLM model")

    seen: set[str] = set()
    for m in models:
        for key in ("id", "label", "base_url", "vllm_model"):
            if not m.get(key):
                raise RuntimeError(f"{path}: model entry is missing '{key}': {m}")
        if m["id"] in seen:
            raise RuntimeError(f"{path}: duplicate model id '{m['id']}'")
        seen.add(m["id"])
        m["base_url"] = m["base_url"].rstrip("/")
        m.setdefault("description", "")
        m.setdefault("params", {})
        m.setdefault("system_prompt", None)
        m.setdefault("system_prompt_file", None)
        # Serverless backends (e.g. Modal) scale to zero. Probing them would wake the GPU and keep
        # it billed, so for those set "always_available": true to skip the health check.
        m.setdefault("always_available", False)
        # How long to wait for a reply. Raise it for backends with slow cold starts.
        m.setdefault("timeout_seconds", 300)

    app = {**DEFAULT_APP, **cfg.get("app", {})}
    if app["default_model"] not in seen:
        app["default_model"] = models[0]["id"]

    # System prompts can live in their own text files (paths are relative to the config file),
    # so they can be edited without touching JSON. Fail early if a named file is missing.
    for holder in (app, *models):
        name = holder.get("system_prompt_file")
        if name:
            holder["system_prompt_file"] = file = (path.parent / name).resolve()
            if not read_prompt_file(file).strip():
                raise RuntimeError(f"{path}: system prompt file {file} is empty")
        else:
            holder["system_prompt_file"] = None
    return {"app": app, "limits": {**DEFAULT_LIMITS, **cfg.get("limits", {})}, "models": models}


CFG = load_config(CONFIG_PATH)
APP_CFG, LIMITS = CFG["app"], CFG["limits"]
MODELS: dict[str, dict[str, Any]] = {m["id"]: m for m in CFG["models"]}

http: httpx.AsyncClient
active_streams = 0


@asynccontextmanager
async def lifespan(_: FastAPI):
    global http
    http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=5.0))
    log.info("Loaded %d model(s) from %s", len(MODELS), CONFIG_PATH)
    if VLLM_API_KEY:
        # A short fingerprint (never the key itself) so a key mismatch is easy to spot.
        fp = hashlib.sha256(VLLM_API_KEY.encode()).hexdigest()[:6]
        log.info("VLLM_API_KEY is set (%d characters, fingerprint %s)", len(VLLM_API_KEY), fp)
    else:
        log.warning("VLLM_API_KEY is NOT set; requests to a vLLM server that needs a key will get 401")
    yield
    await http.aclose()


app = FastAPI(title="vLLM Chat", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class SecurityHeaders:
    """Pure ASGI middleware (does not buffer or interfere with streaming)."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                message["headers"] = headers
            await send(message)

        await self.inner(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {VLLM_API_KEY}"} if VLLM_API_KEY else {}


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        # With one trusted proxy, the LAST entry is the address it saw. Earlier entries
        # are client-supplied and can be spoofed, so never use them for rate limiting.
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


_hits: dict[str, deque[float]] = defaultdict(deque)


def rate_limited(ip: str) -> bool:
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= LIMITS["requests_per_minute"]:
        return True
    q.append(now)
    if len(_hits) > 5000:  # keep memory bounded
        for key in [k for k, v in _hits.items() if not v or now - v[-1] > 60]:
            _hits.pop(key, None)
    return False


_availability: dict[str, tuple[float, bool]] = {}


async def is_available(model: dict[str, Any]) -> bool:
    if model["always_available"]:
        return True  # never probe: a probe could wake a scale-to-zero GPU and keep it running
    now = time.monotonic()
    hit = _availability.get(model["id"])
    if hit and now - hit[0] < AVAILABILITY_TTL:
        return hit[1]
    ok = False
    try:
        r = await http.get(f"{model['base_url']}/models", headers=auth_headers(), timeout=3)
        ok = r.status_code == 200 and any(x.get("id") == model["vllm_model"] for x in r.json().get("data", []))
    except Exception:
        ok = False
    _availability[model["id"]] = (now, ok)
    return ok


def clean_messages(raw: Any) -> list[dict[str, str]]:
    """Accept only user/assistant text, enforce size caps, and give vLLM a clean
    alternating conversation (many chat templates reject anything else)."""
    if not isinstance(raw, list):
        raise ValueError("Please send a message.")

    msgs: list[dict[str, str]] = []
    for m in raw[-LIMITS["max_messages"]:]:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue  # silently drops client-supplied "system"/"tool" messages
        if len(content) > LIMITS["max_message_chars"]:
            raise ValueError("That message is too long. Please shorten it and try again.")
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n\n" + content
        else:
            msgs.append({"role": role, "content": content})

    while msgs and msgs[0]["role"] == "assistant":
        msgs.pop(0)
    if not msgs or msgs[-1]["role"] != "user":
        raise ValueError("Please send a message.")

    total = sum(len(m["content"]) for m in msgs)
    while len(msgs) > 1 and total > LIMITS["max_total_chars"]:  # drop oldest turns first
        total -= len(msgs.pop(0)["content"])
        while msgs and msgs[0]["role"] == "assistant":
            total -= len(msgs.pop(0)["content"])
    if total > LIMITS["max_total_chars"]:
        raise ValueError("That message is too long. Please shorten it and try again.")
    return msgs


DEBUG_ERRORS = os.environ.get("DEBUG_ERRORS") == "1"   # shows the upstream status in the chat error
WARMUP_RETRY_SECONDS = 3.0   # pause between retries while a serverless model wakes up


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


def fail(message: str, status: int, **headers: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
async def stream_reply(model: dict[str, Any], messages: list[dict[str, str]]) -> AsyncIterator[str]:
    global active_streams
    active_streams += 1
    try:
        system = system_prompt_for(model).replace("{date}", dt.date.today().isoformat())
        payload = {k: v for k, v in model["params"].items() if k not in RESERVED_PARAMS}
        payload.update(
            model=model["vllm_model"],
            messages=[{"role": "system", "content": system}, *messages],
            stream=True,
        )

        try:
            # Serverless GPUs (Modal) answer 503 straight away while no container is ready, and
            # the request itself starts one. So on a 503 we keep retrying until the model's
            # timeout instead of showing an error. The comment lines keep the browser
            # connection and any proxy in front of us from timing out while we wait.
            deadline = time.monotonic() + float(model["timeout_seconds"])
            while True:
                warming = False
                async with http.stream(
                    "POST", f"{model['base_url']}/chat/completions", json=payload, headers=auth_headers(),
                    timeout=httpx.Timeout(float(model["timeout_seconds"]), connect=10.0),
                ) as resp:
                    if resp.status_code == 503 and time.monotonic() + WARMUP_RETRY_SECONDS < deadline:
                        await resp.aread()
                        warming = True
                    elif resp.status_code != 200:
                        detail = (await resp.aread())[:400].decode(errors="replace")
                        log.warning("vLLM %s returned %s: %s", model["id"], resp.status_code, detail)
                        msg = "The model couldn't answer that. Please try again."
                        if DEBUG_ERRORS:  # local troubleshooting only; never enable for public users
                            msg += f" [debug: upstream HTTP {resp.status_code}: {detail[:200]}]"
                        yield sse({"type": "error", "message": msg})
                        return
                    else:
                        finish = None
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                choice = (json.loads(data).get("choices") or [{}])[0]
                            except ValueError:
                                continue
                            text = (choice.get("delta") or {}).get("content")
                            if text:
                                yield sse({"type": "token", "text": text})
                            finish = choice.get("finish_reason") or finish

                        if finish == "length":
                            yield sse({"type": "truncated"})
                        yield sse({"type": "done"})
                if not warming:
                    break
                yield ": waiting for the model to start\n\n"
                await asyncio.sleep(WARMUP_RETRY_SECONDS)

        except (httpx.ConnectError, httpx.ConnectTimeout):
            _availability.pop(model["id"], None)
            yield sse({"type": "error", "message": "This model is unavailable right now. Please pick another or try again shortly."})
        except httpx.TimeoutException:
            yield sse({"type": "error", "message": "The model took too long to respond. Please try again."})
        except Exception:
            log.exception("Unexpected error while streaming from %s", model["id"])
            yield sse({"type": "error", "message": "Something went wrong. Please try again."})
    finally:
        active_streams -= 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/config")
async def public_config() -> dict[str, Any]:
    """What the UI is allowed to know: model ids, labels and whether each is reachable.
    No URLs, served model names, parameters or prompts."""
    models = list(MODELS.values())
    up = await asyncio.gather(*(is_available(m) for m in models))
    return {
        "title": APP_CFG["title"],
        "greeting": APP_CFG["greeting"],
        "suggestions": APP_CFG["suggestions"],
        "default_model": APP_CFG["default_model"],
        "max_message_chars": LIMITS["max_message_chars"],
        "models": [
            {"id": m["id"], "label": m["label"], "description": m["description"], "available": ok}
            for m, ok in zip(models, up)
        ],
    }


@app.post("/api/chat")
async def chat(request: Request):
    ip = client_ip(request)
    if rate_limited(ip):
        return fail("You're sending messages too quickly. Please wait a few seconds and try again.", 429, **{"Retry-After": "10"})

    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        return fail("That request is too large.", 413)
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return fail("That request is too large.", 413)
    try:
        body = json.loads(raw)
    except ValueError:
        return fail("Invalid request.", 400)
    if not isinstance(body, dict):
        return fail("Invalid request.", 400)

    model = MODELS.get(body.get("model")) if isinstance(body.get("model"), str) else None
    if model is None:
        return fail("Please choose one of the available models.", 400)

    try:
        messages = clean_messages(body.get("messages"))
    except ValueError as err:
        return fail(str(err), 400)

    if active_streams >= LIMITS["max_concurrent"]:
        return fail("We're busy right now. Please try again in a moment.", 503, **{"Retry-After": "5"})

    log.info("chat model=%s ip=%s turns=%d", model["id"], ip, len(messages))
    return StreamingResponse(
        stream_reply(model, messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )