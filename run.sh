#!/bin/bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PATH="$PROJECT_ROOT/.venv"
cd "$PROJECT_ROOT"

if [[ ! -d "$VENV_PATH" ]]; then
  if command -v python3.12 >/dev/null 2>&1; then
    python3.12 -m venv "$VENV_PATH"
  else
    python3 -m venv "$VENV_PATH"
  fi
fi

# shellcheck disable=SC1091
source "$VENV_PATH/bin/activate"

if ! python -c "import dotenv, httpx, playwright" >/dev/null 2>&1; then
  pip install -r "$PROJECT_ROOT/requirements.txt"
fi

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

check_only=0
for arg in "$@"; do
  if [[ "$arg" == "--check" ]]; then
    check_only=1
  fi
done

if [[ "$check_only" -eq 0 ]]; then
  required_vars=("TELEGRAM_BOT_TOKEN" "TELEGRAM_CHAT_ID")
  for var_name in "${required_vars[@]}"; do
    if [[ -z "${!var_name:-}" ]]; then
      echo "Missing required environment variable: $var_name"
      exit 1
    fi
  done
fi

python3 - <<'PY'
import json
from pathlib import Path

config = json.loads(Path("config/targets.json").read_text(encoding="utf-8"))
targets = [target for target in config["targets"] if target.get("enabled", True)]
print("==============================================")
print(" Goethe Sentry")
print("==============================================")
print(f" Active targets: {len(targets)}")
for target in targets:
    print(f"  - {target['label']}: {target['url']}")
print("==============================================")
PY

exec python "$PROJECT_ROOT/src/monitor.py" "$@"
