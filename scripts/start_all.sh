#!/usr/bin/env bash
# Start agent-execution and agent-management together.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
EXECUTION_DIR="$ROOT_DIR/agent-execution"
MANAGEMENT_DIR="$ROOT_DIR/agent-management"
EXECUTION_HOST="${EXECUTION_HOST:-127.0.0.1}"
EXECUTION_PORT="${EXECUTION_PORT:-8000}"
MANAGEMENT_HOST="${MANAGEMENT_HOST:-127.0.0.1}"
MANAGEMENT_PORT="${MANAGEMENT_PORT:-8080}"
REGISTER_CODEX=1
SKIP_POSTGRES=0
START_WORKER=0
WORKER_ENDPOINT="${WORKER_ENDPOINT:-}"
WORKER_TOKEN="${WORKER_TOKEN:-}"
WORKER_MANAGEMENT_TOKEN="${WORKER_MANAGEMENT_TOKEN:-}"
WORKER_PRESET_DIR="${WORKER_PRESET_DIR:-}"
WORKER_LOCAL=0

env_file_value() {
  local key="$1"
  local file="$ROOT_DIR/.env"
  [[ -f "$file" ]] || return 0
  awk -v key="$key" '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      if (line !~ ("^" key "[[:space:]]*=")) next
      sub(/^[^=]*=[[:space:]]*/, "", line)
      sub(/[[:space:]]+#.*$/, "", line)
      if (line ~ /^".*"$/ || line ~ /^'"'"'.*'"'"'$/) line = substr(line, 2, length(line) - 2)
      print line
      exit
    }
  ' "$file"
}

database_is_reachable() {
  python3 - "$1" <<'PY'
import socket
import sys
from urllib.parse import urlparse

url = urlparse(sys.argv[1])
try:
    host = url.hostname or "127.0.0.1"
    port = url.port or 5432
    with socket.create_connection((host, port), timeout=0.5):
        pass
except (OSError, ValueError):
    raise SystemExit(1)
PY
}

usage() {
  cat <<'USAGE'
Usage: ./scripts/start_all.sh [options]

Start the agent-execution service and the agent-management service in parallel.
The management service also starts its local PostgreSQL cluster and applies migrations.
Optionally start the container-cloud Worker in the same command.

Options:
  --execution-host HOST    Execution service bind host (default: 127.0.0.1).
  --execution-port PORT    Execution service port (default: 8000).
  --management-host HOST   Management service bind host (default: 127.0.0.1).
  --management-port PORT   Management service port (default: 8080).
  --no-register-codex      Do not register the local execution service in management.
  --skip-postgres          Force reuse of the database configured in .env (auto by default).
  --worker                 Force-start the container-cloud Worker using .env (auto when configured).
  --worker-endpoint URL    Start Worker and override its WebSocket endpoint.
  --worker-token TOKEN     Override the Worker pairing token.
  --worker-management-token TOKEN
                           Override the Worker management API token.
  --worker-local           Allow ws:// Worker endpoint for local testing.
  --worker-preset-dir DIR  Project directory used by test-* probes.
  -h, --help               Show this help.

Environment variables with the same names can also provide the defaults.
Press Ctrl+C to stop all started services.
USAGE
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'error: command not found: %s\n' "$1" >&2
    exit 1
  fi
}

while (($# > 0)); do
  case "$1" in
    --execution-host)
      [[ $# -ge 2 ]] || { echo "error: --execution-host needs a value" >&2; exit 2; }
      EXECUTION_HOST="$2"
      shift
      ;;
    --execution-port)
      [[ $# -ge 2 ]] || { echo "error: --execution-port needs a value" >&2; exit 2; }
      EXECUTION_PORT="$2"
      shift
      ;;
    --management-host)
      [[ $# -ge 2 ]] || { echo "error: --management-host needs a value" >&2; exit 2; }
      MANAGEMENT_HOST="$2"
      shift
      ;;
    --management-port)
      [[ $# -ge 2 ]] || { echo "error: --management-port needs a value" >&2; exit 2; }
      MANAGEMENT_PORT="$2"
      shift
      ;;
    --no-register-codex)
      REGISTER_CODEX=0
      ;;
    --skip-postgres)
      SKIP_POSTGRES=1
      ;;
    --worker)
      START_WORKER=1
      ;;
    --worker-endpoint)
      [[ $# -ge 2 ]] || { echo "error: --worker-endpoint needs a value" >&2; exit 2; }
      START_WORKER=1
      WORKER_ENDPOINT="$2"
      shift
      ;;
    --worker-token)
      [[ $# -ge 2 ]] || { echo "error: --worker-token needs a value" >&2; exit 2; }
      START_WORKER=1
      WORKER_TOKEN="$2"
      shift
      ;;
    --worker-management-token)
      [[ $# -ge 2 ]] || { echo "error: --worker-management-token needs a value" >&2; exit 2; }
      START_WORKER=1
      WORKER_MANAGEMENT_TOKEN="$2"
      shift
      ;;
    --worker-local)
      START_WORKER=1
      WORKER_LOCAL=1
      ;;
    --worker-preset-dir)
      [[ $# -ge 2 ]] || { echo "error: --worker-preset-dir needs a value" >&2; exit 2; }
      START_WORKER=1
      WORKER_PRESET_DIR="$2"
      shift
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

if [[ ! -f "$EXECUTION_DIR/pyproject.toml" ]]; then
  echo "error: agent-execution/pyproject.toml is missing" >&2
  exit 1
fi
if [[ ! -f "$MANAGEMENT_DIR/pyproject.toml" ]]; then
  echo "error: agent-management/pyproject.toml is missing" >&2
  exit 1
fi
if [[ ! -f "$ROOT_DIR/.env" && ! -f "$EXECUTION_DIR/.env" ]]; then
  echo "error: .env is missing; run ./scripts/setup_environment.sh first" >&2
  exit 1
fi
if [[ ! -f "$ROOT_DIR/.env" && ! -f "$MANAGEMENT_DIR/.env" ]]; then
  echo "error: .env is missing; run ./scripts/setup_environment.sh first" >&2
  exit 1
fi

# The shared .env is the normal source of these values. Explicit environment
# variables and CLI flags still take precedence over the automatic defaults.
if [[ -z "$WORKER_ENDPOINT" ]]; then
  WORKER_ENDPOINT="${RELAY_CONTAINER_CLOUD_ENDPOINT:-$(env_file_value RELAY_CONTAINER_CLOUD_ENDPOINT)}"
fi
if [[ -z "$WORKER_TOKEN" ]]; then
  WORKER_TOKEN="${RELAY_CONTAINER_CLOUD_TOKEN:-$(env_file_value RELAY_CONTAINER_CLOUD_TOKEN)}"
fi
if [[ -z "$WORKER_MANAGEMENT_TOKEN" ]]; then
  WORKER_MANAGEMENT_TOKEN="${RELAY_CONTAINER_CLOUD_MANAGEMENT_TOKEN:-$(env_file_value RELAY_CONTAINER_CLOUD_MANAGEMENT_TOKEN)}"
fi
if [[ -z "$WORKER_PRESET_DIR" ]]; then
  WORKER_PRESET_DIR="${RELAY_CONTAINER_CLOUD_PRESET_DIR:-$(env_file_value RELAY_CONTAINER_CLOUD_PRESET_DIR)}"
fi
if [[ "$WORKER_LOCAL" == 0 ]]; then
  worker_local_config="${RELAY_CONTAINER_CLOUD_LOCAL_MODE:-$(env_file_value RELAY_CONTAINER_CLOUD_LOCAL_MODE)}"
  if [[ "${worker_local_config,,}" == "true" || "$worker_local_config" == "1" ]]; then
    WORKER_LOCAL=1
  fi
fi

if [[ "$START_WORKER" == 0 && -n "$WORKER_ENDPOINT" && -n "$WORKER_TOKEN" ]]; then
  START_WORKER=1
  echo "container-cloud-worker: auto-enabled from .env"
fi

if [[ "$SKIP_POSTGRES" == 0 ]]; then
  database_url="${RELAY_DATABASE_URL:-$(env_file_value RELAY_DATABASE_URL)}"
  database_url="${database_url:-postgresql+asyncpg://relay@127.0.0.1:55432/agentrelay}"
  database_host="$(python3 - "$database_url" <<'PY'
import sys
from urllib.parse import urlparse
print(urlparse(sys.argv[1]).hostname or "127.0.0.1")
PY
)"
  database_port="$(python3 - "$database_url" <<'PY'
import sys
from urllib.parse import urlparse
print(urlparse(sys.argv[1]).port or 5432)
PY
)"
  if [[ "$database_host" == "127.0.0.1" || "$database_host" == "localhost" ]] && [[ "$database_port" == "55432" ]]; then
    if database_is_reachable "$database_url"; then
      SKIP_POSTGRES=1
      echo "postgres: reusing reachable database at ${database_host}:${database_port}"
    else
      echo "postgres: starting project-local database at ${database_host}:${database_port}"
    fi
  else
    # The project-local PostgreSQL can only own the default local endpoint.
    # Never start it when the configured URL points elsewhere.
    SKIP_POSTGRES=1
    echo "postgres: using configured external database at ${database_host}:${database_port}"
  fi
fi

execution_pid=""
management_pid=""
worker_pid=""
cleaned_up=0
status_dir="$(mktemp -d "${TMPDIR:-/tmp}/codexserver-start.XXXXXX")"
execution_status_file="$status_dir/execution.status"
management_status_file="$status_dir/management.status"
worker_status_file="$status_dir/worker.status"

cleanup() {
  local exit_code=$?
  if [[ "$cleaned_up" == 1 ]]; then
    exit "$exit_code"
  fi
  cleaned_up=1
  trap - EXIT INT TERM

  echo
  echo "stopping CodexServer services"
  for pid in "$execution_pid" "$management_pid" "$worker_pid"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$execution_pid" "$management_pid" "$worker_pid"; do
    if [[ -n "$pid" ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
  rm -rf "$status_dir"
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

management_command=(
  uv run python -m scripts.dev
)
if [[ "$REGISTER_CODEX" == 1 ]]; then
  management_command+=(--register-codex)
fi
if [[ "$SKIP_POSTGRES" == 1 ]]; then
  management_command+=(--skip-postgres)
fi
management_command+=(
  --host "$MANAGEMENT_HOST"
  --port "$MANAGEMENT_PORT"
)

worker_command=(uv run python -m scripts.container_cloud_worker)
if [[ -n "$WORKER_ENDPOINT" ]]; then
  worker_command+=(--endpoint "$WORKER_ENDPOINT")
fi
if [[ -n "$WORKER_TOKEN" ]]; then
  worker_command+=(--token "$WORKER_TOKEN")
fi
if [[ -n "$WORKER_MANAGEMENT_TOKEN" ]]; then
  worker_command+=(--management-token "$WORKER_MANAGEMENT_TOKEN")
fi
if [[ "$WORKER_LOCAL" == 1 ]]; then
  worker_command+=(--local)
fi
if [[ -n "$WORKER_PRESET_DIR" ]]; then
  worker_command+=(--preset-dir "$WORKER_PRESET_DIR")
fi

(
  set +e
  cd "$EXECUTION_DIR"
  uv run uvicorn app.main:app \
    --host "$EXECUTION_HOST" \
    --port "$EXECUTION_PORT" \
    --workers 1
  status=$?
  printf '%s\n' "$status" > "$execution_status_file"
  exit "$status"
) &
execution_pid=$!

(
  set +e
  cd "$MANAGEMENT_DIR"
  "${management_command[@]}"
  status=$?
  printf '%s\n' "$status" > "$management_status_file"
  exit "$status"
) &
management_pid=$!

if [[ "$START_WORKER" == 1 ]]; then
  (
    set +e
    cd "$MANAGEMENT_DIR"
    "${worker_command[@]}"
    status=$?
    printf '%s\n' "$status" > "$worker_status_file"
    exit "$status"
  ) &
  worker_pid=$!
fi

echo "agent-execution: http://${EXECUTION_HOST}:${EXECUTION_PORT} (pid ${execution_pid})"
echo "agent-management: http://${MANAGEMENT_HOST}:${MANAGEMENT_PORT} (pid ${management_pid})"
if [[ "$START_WORKER" == 1 ]]; then
  echo "container-cloud-worker: started (pid ${worker_pid})"
fi
echo "Press Ctrl+C to stop all started services."

while [[ ! -f "$execution_status_file" && ! -f "$management_status_file" && ! -f "$worker_status_file" ]]; do
  sleep 1
done

if [[ -f "$execution_status_file" ]]; then
  execution_status="$(cat "$execution_status_file")"
else
  execution_status=0
fi
if [[ -f "$management_status_file" ]]; then
  management_status="$(cat "$management_status_file")"
else
  management_status=0
fi

if [[ "$execution_status" -ne 0 ]]; then
  echo "agent-execution exited with status $execution_status" >&2
  exit "$execution_status"
fi
if [[ "$management_status" -ne 0 ]]; then
  echo "agent-management exited with status $management_status" >&2
  exit "$management_status"
fi
if [[ -f "$worker_status_file" ]]; then
  worker_status="$(cat "$worker_status_file")"
  if [[ "$worker_status" -ne 0 ]]; then
    echo "container-cloud-worker exited with status $worker_status" >&2
    exit "$worker_status"
  fi
fi
exit 0
