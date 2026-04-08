#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ALT_HOME="${CODEX_HOME:-$HOME/.codex-openspace}"
AUTH_FILE="${OPENSPACE_AUTH_FILE:-$ALT_HOME/auth.json}"

api_key="${OPENSPACE_LLM_API_KEY:-}"
if [[ -z "$api_key" ]]; then
  api_key="$(
    python3 - "$AUTH_FILE" <<'PY'
import json
import sys
from pathlib import Path

auth_path = Path(sys.argv[1])
data = json.loads(auth_path.read_text(encoding="utf-8"))
print(data.get("OPENAI_API_KEY", ""), end="")
PY
  )"
fi

if [[ -z "$api_key" ]]; then
  echo "OPENSPACE_LLM_API_KEY is not set and $AUTH_FILE does not contain OPENAI_API_KEY" >&2
  exit 1
fi

export OPENSPACE_MODEL="${OPENSPACE_MODEL:-gpt-5.4}"
export OPENSPACE_LLM_API_KEY="$api_key"
export OPENSPACE_LLM_API_BASE="${OPENSPACE_LLM_API_BASE:-https://codexapi.space/v1}"
export OPENSPACE_LLM_OPENAI_STREAM_COMPAT="${OPENSPACE_LLM_OPENAI_STREAM_COMPAT:-true}"
export OPENSPACE_HOST_SKILL_DIRS="${OPENSPACE_HOST_SKILL_DIRS:-$ALT_HOME/skills}"
export OPENSPACE_WORKSPACE="${OPENSPACE_WORKSPACE:-$REPO_ROOT}"

exec "$REPO_ROOT/.venv/bin/openspace" "$@"
