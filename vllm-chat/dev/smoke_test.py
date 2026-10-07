"""
smoke_test.py - end-to-end checks against two mock vLLM servers and the real chat server.

    python dev/smoke_test.py

Verifies the properties that matter for external users:
  * the public config leaks no URLs, served model names or generation parameters
  * extra fields a client sends (temperature, max_tokens, system messages, ...) are ignored
    and the values from models.json are what vLLM actually receives
  * bad input is rejected, oversize input is rejected, rate limiting kicks in
  * an offline model is reported as unavailable and fails with a friendly message
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
APP_URL = "http://127.0.0.1:8099"
QWEN, LLAMA = "http://127.0.0.1:8000", "http://127.0.0.1:8001"
procs: list[subprocess.Popen] = []


def start(*cmd: str, env: dict | None = None) -> subprocess.Popen:
    p = subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, **(env or {})},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    procs.append(p)
    return p


def wait_for(url: str, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up")


def chat(body: dict) -> tuple[int, list[dict], dict]:
    """POST /api/chat; returns (status, parsed SSE events, json error body)."""
    with httpx.stream("POST", f"{APP_URL}/api/chat", json=body, timeout=30) as r:
        raw = r.read().decode()
        if r.status_code != 200:
            return r.status_code, [], json.loads(raw)
        events = [json.loads(l[5:]) for l in raw.splitlines() if l.startswith("data:")]
        return 200, events, {}


def text_of(events: list[dict]) -> str:
    return "".join(e["text"] for e in events if e["type"] == "token")


passed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        raise SystemExit(f"FAILED: {name} {detail}")
    passed += 1


def main() -> None:
    cfg = json.loads((ROOT / "models.json").read_text())
    cfg["limits"]["requests_per_minute"] = 30            # rejected requests count too, so leave room for the earlier checks
    cfg["models"].append({                               # a scale-to-zero style backend: must never be probed
        "id": "serverless", "label": "Serverless 3B", "description": "scales to zero",
        "base_url": "http://127.0.0.1:8002/v1", "vllm_model": "Qwen/Qwen2.5-3B-Instruct",
        "always_available": True, "timeout_seconds": 60,
        "params": {"temperature": 0.3, "max_tokens": 256},
    })
    tmp = Path(tempfile.mkdtemp()) / "models.json"
    tmp.write_text(json.dumps(cfg))
    py = sys.executable

    try:
        start(py, "dev/mock_vllm.py", "--port", "8000", "--model", "Qwen/Qwen2.5-7B-Instruct")
        llama = start(py, "dev/mock_vllm.py", "--port", "8001", "--model", "meta-llama/Llama-3.1-8B-Instruct")
        start(py, "dev/mock_vllm.py", "--port", "8002", "--model", "Qwen/Qwen2.5-3B-Instruct")
        start(py, "-m", "uvicorn", "server:app", "--port", "8099", "--log-level", "warning",
              env={"MODELS_FILE": str(tmp), "AVAILABILITY_TTL": "1", "VLLM_API_KEY": "test-key-123"})
        wait_for(f"{QWEN}/v1/models"); wait_for(f"{LLAMA}/v1/models"); wait_for(f"{APP_URL}/api/health")
        wait_for("http://127.0.0.1:8002/_models_hits")
        SERVERLESS = "http://127.0.0.1:8002"
        hits_before = httpx.get(f"{SERVERLESS}/_models_hits").json()["models_hits"]

        print("Public surface")
        r = httpx.get(f"{APP_URL}/api/config")
        conf, raw = r.json(), r.text
        check("config lists all three models, all available", [m["available"] for m in conf["models"]] == [True, True, True])
        time.sleep(1.3); httpx.get(f"{APP_URL}/api/config"); time.sleep(1.3); httpx.get(f"{APP_URL}/api/config")
        check("always_available model is never health-probed (would wake a serverless GPU)",
              httpx.get(f"{SERVERLESS}/_models_hits").json()["models_hits"] == hits_before)
        check("normal models are still probed", httpx.get(f"{QWEN}/_models_hits").json()["models_hits"] > 1)
        check("each model exposes only id/label/description/available",
              all(set(m) == {"id", "label", "description", "available"} for m in conf["models"]))
        leaks = [s for s in ("http", "localhost", "127.0.0.1", "/v1", "Qwen/", "meta-llama", "temperature", "top_p", "max_tokens", "base_url", "vllm_model", "system_prompt", "test-key") if s in raw]
        check("config leaks no URLs, served names, params, prompt or key", not leaks, str(leaks))
        page = httpx.get(f"{APP_URL}/")
        check("index served with strict CSP", page.status_code == 200 and "script-src 'self'" in page.headers.get("content-security-policy", ""))
        ui = " ".join(httpx.get(f"{APP_URL}/static/{f}").text.lower() for f in ("index.html", "app.js"))
        check("UI has no temperature/token controls", "temperature" not in ui and "max_tokens" not in ui and 'type="range"' not in ui)

        print("Chat and server-side parameters")
        status, ev, _ = chat({"model": "qwen2.5-7b", "messages": [{"role": "user", "content": "hello there"}]})
        check("streams a reply", status == 200 and "mock" in text_of(ev) and ev[-1]["type"] == "done", text_of(ev))
        sent = httpx.get(f"{QWEN}/_last").json()
        check("vLLM got temperature from models.json", sent["temperature"] == 0.6, str(sent.get("temperature")))
        check("vLLM got max_tokens from models.json", sent["max_tokens"] == 1024)
        check("vLLM got the API key", sent["_auth"] == "Bearer test-key-123")
        check("system prompt injected with today's date", sent["messages"][0]["role"] == "system" and "{date}" not in sent["messages"][0]["content"])

        status, ev, _ = chat({
            "model": "qwen2.5-7b", "temperature": 99, "max_tokens": 999999, "top_p": 5, "stream": False,
            "tools": [{"x": 1}], "stop": ["a"], "system": "EVIL-TOP",
            "messages": [{"role": "system", "content": "EVIL-SYSTEM"}, {"role": "user", "content": "inject test"}],
        })
        sent = httpx.get(f"{QWEN}/_last").json()
        check("injected params ignored", status == 200 and sent["temperature"] == 0.6 and sent["max_tokens"] == 1024 and sent["top_p"] == 0.9 and sent["stream"] is True)
        check("injected tools/stop not forwarded", "tools" not in sent and "stop" not in sent)
        check("client system message dropped", "EVIL" not in json.dumps(sent["messages"]))

        chat({"model": "qwen2.5-7b", "messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]})
        roles = [m["role"] for m in httpx.get(f"{QWEN}/_last").json()["messages"]]
        check("consecutive user turns merged for chat-template safety", roles == ["system", "user"], str(roles))

        chat({"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "to llama"}]})
        sent = httpx.get(f"{LLAMA}/_last").json()
        check("per-model params respected (llama temp 0.5)", sent["temperature"] == 0.5 and sent["model"] == "meta-llama/Llama-3.1-8B-Instruct")

        status, ev, _ = chat({"model": "serverless", "messages": [{"role": "user", "content": "hi serverless"}]})
        sent = httpx.get(f"{SERVERLESS}/_last").json()
        check("always_available model serves chats with its own params", status == 200 and sent["temperature"] == 0.3 and sent["max_tokens"] == 256)

        status, ev, _ = chat({"model": "qwen2.5-7b", "messages": [{"role": "user", "content": "LONGTEST"}]})
        check("length cut-off surfaced as 'truncated' event", any(e["type"] == "truncated" for e in ev))

        print("Validation")
        for name, body in {
            "unknown model rejected": {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]},
            "missing model rejected": {"messages": [{"role": "user", "content": "hi"}]},
            "empty messages rejected": {"model": "qwen2.5-7b", "messages": []},
            "assistant-last rejected": {"model": "qwen2.5-7b", "messages": [{"role": "assistant", "content": "hi"}]},
            "oversize message rejected": {"model": "qwen2.5-7b", "messages": [{"role": "user", "content": "x" * 9000}]},
        }.items():
            status, _, err = chat(body)
            check(name, status == 400 and "error" in err, f"{status} {err}")
        r = httpx.post(f"{APP_URL}/api/chat", content=b"not json", headers={"Content-Type": "application/json"})
        check("invalid JSON rejected", r.status_code == 400)
        r = httpx.post(f"{APP_URL}/api/chat", content=b"x" * 300_000, headers={"Content-Type": "application/json"})
        check("huge body rejected", r.status_code == 413)

        print("Model goes offline")
        os.killpg(llama.pid, signal.SIGTERM); llama.wait(5)
        time.sleep(1.5)
        conf = httpx.get(f"{APP_URL}/api/config").json()
        check("config marks llama unavailable", {m["id"]: m["available"] for m in conf["models"]} == {"qwen2.5-7b": True, "llama-3.1-8b": False, "serverless": True})
        status, ev, _ = chat({"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "hi"}]})
        err = next((e["message"] for e in ev if e["type"] == "error"), "")
        check("offline model gives a friendly error, no internals", "unavailable" in err and "127.0.0.1" not in err and "8001" not in err, err)

        print("Rate limiting")
        codes = []
        for _ in range(60):
            codes.append(chat({"model": "qwen2.5-7b", "messages": [{"role": "user", "content": "spam"}]})[0])
            if codes[-1] == 429:
                break
        check("429 after exceeding requests_per_minute", codes[-1] == 429 and codes[0] == 200, str(codes))
        r = httpx.post(f"{APP_URL}/api/chat", json={"model": "qwen2.5-7b", "messages": [{"role": "user", "content": "x"}]})
        check("429 carries Retry-After and a friendly message", r.status_code == 429 and "Retry-After" in r.headers and "too quickly" in r.json()["error"])

        print(f"\nAll {passed} checks passed.")
    finally:
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                pass


if __name__ == "__main__":
    main()
