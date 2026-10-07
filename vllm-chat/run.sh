#!/usr/bin/env bash
# Start the chat server (not vLLM itself).
#   ./run.sh                       # http://127.0.0.1:8080
#   HOST=0.0.0.0 PORT=80 ./run.sh  # listen on all interfaces (put HTTPS in front of this)
#   MODELS_FILE=models.modal.json ./run.sh
#
# With uv installed (recommended) this runs `uv sync` against pyproject.toml/uv.lock, which
# creates ./.venv and, if needed, downloads a suitable Python by itself. Without uv it falls
# back to python3 + pip + requirements.txt. Either way nothing is installed globally.
#
# Settings in ./.env (for example VLLM_API_KEY=...) are loaded automatically, but anything
# you have already exported in your shell wins over the file.
#
# Runs ONE worker on purpose: the rate limiter keeps its counters in memory. If you need
# several workers or several machines, move the rate limit to your reverse proxy or Redis.

set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue ;; esac
    if [ -n "${!key:-}" ]; then
      [ "${!key}" = "$value" ] || echo "Note: $key is already set in your shell, so the different value in .env is ignored." >&2
    else
      export "$key=$value"
    fi
  done < .env
fi

# Defaults come after .env is loaded, so HOST and PORT can be set there too.
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"

if command -v uv >/dev/null 2>&1; then
  # PYTHON_VERSION (optional) pins the interpreter, for example PYTHON_VERSION=3.12
  uv sync --quiet ${PYTHON_VERSION:+--python "$PYTHON_VERSION"}
  echo "Chat UI: http://$HOST:$PORT"
  exec uv run --no-sync python -m uvicorn server:app --host "$HOST" --port "$PORT"
fi

PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10+ is required (found: $("$PY" --version 2>&1)). Install uv (brew install uv) or set PYTHON=python3.12."; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q -r requirements.txt
echo "Chat UI: http://$HOST:$PORT"
exec python -m uvicorn server:app --host "$HOST" --port "$PORT"