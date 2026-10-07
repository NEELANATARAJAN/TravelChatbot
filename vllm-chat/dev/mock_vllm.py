"""
mock_vllm.py - a stand-in for `vllm serve` so you can run and test the chat app
on a machine with no GPU (for example a MacBook). It speaks the same OpenAI-compatible
API: GET /v1/models and streaming POST /v1/chat/completions.

    python dev/mock_vllm.py --port 8000 --model Qwen/Qwen2.5-7B-Instruct
    python dev/mock_vllm.py --port 8001 --model meta-llama/Llama-3.1-8B-Instruct

The ports and model names above match the default models.json. For testing, GET /_last
returns the most recent request the mock received (so you can check which parameters
the chat server actually sent). Not for production.
"""

import argparse
import asyncio
import json

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def reply_for(model: str, user_text: str) -> tuple[str, str]:
    t = user_text.lower()
    if "longtest" in t:
        return "This reply is deliberately cut off by the mock to test the length-limit notice, so it ends mid", "length"
    if "code" in t:
        return ("Here's a small example:\n\n```python\ndef greet(name: str) -> str:\n"
                "    return f\"Hello, {name}!\"\n```\n\nCall it with `greet(\"world\")`."), "stop"
    if "table" in t:
        return ("Here is a comparison:\n\n| Model | Size | Good for |\n|---|---|---|\n"
                "| Small | 3B | Speed |\n| Medium | 7B | Everyday use |\n| Large | 70B | Hard problems |\n"), "stop"
    return (f"**{model}** (mock) here. You said: *{user_text[:80]}*\n\n"
            "- First point\n- Second point\n\n1. Step one\n2. Step two\n\n"
            "This is a development stub, not a real model."), "stop"


def build(model: str, first_token_delay: float = 0.0) -> FastAPI:
    app = FastAPI()
    last: dict = {}
    counters = {"models_hits": 0}

    @app.get("/v1/models")
    async def models():
        counters["models_hits"] += 1
        return {"object": "list", "data": [{"id": model, "object": "model"}]}

    @app.get("/_models_hits")
    async def models_hits():
        return counters

    @app.get("/_last")
    async def get_last():
        return last

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        last.clear()
        last.update(body)
        last["_auth"] = request.headers.get("authorization", "")
        if body.get("model") != model:
            return JSONResponse({"error": f"model '{body.get('model')}' not found"}, status_code=404)

        user_text = next((m["content"] for m in reversed(body["messages"]) if m["role"] == "user"), "")
        text, finish = reply_for(model, user_text)

        async def gen():
            if first_token_delay:
                await asyncio.sleep(first_token_delay)   # simulate a serverless cold start
            words = text.split(" ")
            for i, w in enumerate(words):
                piece = w if i == len(words) - 1 else w + " "
                chunk = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
                await asyncio.sleep(0.015)
            yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--model", default="Qwen/Qwen2.5-7B-Instruct")
    ap.add_argument("--first-token-delay", type=float, default=0.0,
                    help="seconds to wait before the first token, to simulate a cold start")
    args = ap.parse_args()
    uvicorn.run(build(args.model, args.first_token_delay), host="127.0.0.1", port=args.port, log_level="warning")
