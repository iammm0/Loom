#!/usr/bin/env sh

CURRENT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$CURRENT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export UV_PROJECT_ENVIRONMENT="$CURRENT_DIR/.venv-video-loom"
VENV_PY="$CURRENT_DIR/.venv-video-loom/bin/python"

MPT_WEBUI_HOST="${MPT_WEBUI_HOST:-127.0.0.1}"
MPT_API_PORT="${MPT_API_PORT:-8080}"

if [ -f "$CURRENT_DIR/webui/package.json" ]; then
  if [ ! -d "$CURRENT_DIR/webui/node_modules" ]; then
    (cd "$CURRENT_DIR/webui" && npm install)
  fi
  if [ ! -f "$CURRENT_DIR/webui/dist/index.html" ]; then
    (cd "$CURRENT_DIR/webui" && npm run build)
  fi
fi

echo "***** WebUI address: http://$MPT_WEBUI_HOST:$MPT_API_PORT *****"

if [ -x "$VENV_PY" ]; then
  "$VENV_PY" "$CURRENT_DIR/main.py"
elif [ -x "$CURRENT_DIR/.venv/bin/python" ]; then
  "$CURRENT_DIR/.venv/bin/python" "$CURRENT_DIR/main.py"
elif command -v uv >/dev/null 2>&1; then
  uv run python "$CURRENT_DIR/main.py"
else
  python3 "$CURRENT_DIR/main.py"
fi
