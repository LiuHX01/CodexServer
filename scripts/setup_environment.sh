#!/usr/bin/env bash
# Prepare the uv environments and local .env files for both services.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
EXECUTION_DIR="$ROOT_DIR/agent-execution"
MANAGEMENT_DIR="$ROOT_DIR/agent-management"
ROOT_ENV="$ROOT_DIR/.env"
DRY_RUN=0
SKIP_SYNC=0

usage() {
  cat <<'USAGE'
Usage: ./scripts/setup_environment.sh [options]

Prepare both CodexServer services for local development.
The shared configuration is stored in the repository root .env.

Options:
  --dry-run     Print actions without changing files or installing dependencies.
  --skip-sync   Keep existing uv environments and only prepare .env files.
  -h, --help    Show this help.
USAGE
}

run() {
  if [[ "$DRY_RUN" == 1 ]]; then
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'error: command not found: %s\n' "$1" >&2
    exit 1
  fi
}

prepare_root_env() {
  if [[ -f "$ROOT_ENV" ]]; then
    echo "kept existing root .env"
    return
  fi
  if [[ "$DRY_RUN" == 1 ]]; then
    echo "would create root .env from .env.example and migrate existing service settings"
    return
  fi
  ROOT_ENV_PATH="$ROOT_ENV" EXECUTION_ENV_PATH="$EXECUTION_DIR/.env" MANAGEMENT_ENV_PATH="$MANAGEMENT_DIR/.env" uv run python - <<'PY'
from pathlib import Path
import os
import re
import secrets

root = Path(os.environ["ROOT_ENV_PATH"])
values = {}
for source in (Path(os.environ["EXECUTION_ENV_PATH"]), Path(os.environ["MANAGEMENT_ENV_PATH"])):
    if not source.exists():
        continue
    for line in source.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            values[key] = value

values.setdefault("API_TOKEN", secrets.token_urlsafe(32))
values.setdefault("RELAY_ADMIN_TOKEN", secrets.token_urlsafe(32))
values.setdefault("RELAY_SECRET_KEY", secrets.token_urlsafe(48))

text = root.with_name(".env.example").read_text()
for key, value in values.items():
    text = re.sub(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
root.write_text(text)
root.chmod(0o600)
PY
  echo "created root .env and migrated existing service settings"
}

prepare_execution_env() {
  if [[ -f "$ROOT_ENV" ]]; then
    echo "using root .env for agent-execution"
    return
  fi
  cd "$EXECUTION_DIR"
  if [[ ! -f .env ]]; then
    if [[ "$DRY_RUN" == 1 ]]; then
      echo "would create agent-execution/.env from .env.example and generate API_TOKEN"
      return
    fi
    cp .env.example .env
    API_TOKEN_PATH="$EXECUTION_DIR/.env" uv run python - <<'PY'
from pathlib import Path
import os
import secrets

path = Path(os.environ["API_TOKEN_PATH"])
text = path.read_text()
placeholder = "API_TOKEN=replace-this-with-a-random-secret-at-least-24-characters"
text = text.replace(f"{placeholder}\n", f"API_TOKEN={secrets.token_urlsafe(32)}\n", 1)
path.write_text(text)
path.chmod(0o600)
PY
    echo "created agent-execution/.env with a generated API_TOKEN"
  else
    echo "kept existing agent-execution/.env"
  fi
}

prepare_management_env() {
  if [[ -f "$ROOT_ENV" ]]; then
    echo "using root .env for agent-management"
    return
  fi
  cd "$MANAGEMENT_DIR"
  if [[ ! -f .env ]]; then
    if [[ "$DRY_RUN" == 1 ]]; then
      echo "would create agent-management/.env with scripts.bootstrap"
      return
    fi
    uv run python -m scripts.bootstrap
    chmod 600 .env
  else
    echo "kept existing agent-management/.env"
  fi
}

while (($# > 0)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --skip-sync)
      SKIP_SYNC=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown option: %s\n\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

require_command uv

if [[ ! -d "$EXECUTION_DIR" || ! -f "$EXECUTION_DIR/pyproject.toml" ]]; then
  echo "error: agent-execution is missing or invalid" >&2
  exit 1
fi
if [[ ! -d "$MANAGEMENT_DIR" || ! -f "$MANAGEMENT_DIR/pyproject.toml" ]]; then
  echo "error: agent-management is missing or invalid" >&2
  exit 1
fi
if [[ ! -f "$ROOT_DIR/.env.example" ]]; then
  echo "error: root .env.example is missing" >&2
  exit 1
fi

if [[ "$SKIP_SYNC" == 0 ]]; then
  echo "syncing agent-execution dependencies"
  run uv sync --locked --directory "$EXECUTION_DIR"
  echo "syncing agent-management dependencies"
  run uv sync --locked --directory "$MANAGEMENT_DIR"
else
  echo "skipping dependency synchronization"
fi

prepare_root_env
prepare_execution_env
prepare_management_env

cat <<'MESSAGE'

Environment setup finished. Shared configuration: .env

Before the first start, log the execution service into the Codex account if needed:
  cd agent-execution && uv run python -m app.codex_cli login --device-auth

Start both services with:
  ./scripts/start_all.sh
MESSAGE
