#!/usr/bin/env bash
# Start the Codex execution service only.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
EXECUTION_DIR="$ROOT_DIR/agent-execution"
HOST="${EXECUTION_HOST:-127.0.0.1}"
PORT="${EXECUTION_PORT:-8000}"

usage() {
  cat <<'USAGE'
Usage: ./scripts/start_execution.sh [options]

Start agent-execution. This process owns the Codex SDK connection and execution logs.

Options:
  --host HOST          Bind host (default: 127.0.0.1).
  --port PORT          Bind port (default: 8000).
  -h, --help           Show this help.
USAGE
}

while (($# > 0)); do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || { echo "error: --host needs a value" >&2; exit 2; }
      HOST="$2"
      shift
      ;;
    --port)
      [[ $# -ge 2 ]] || { echo "error: --port needs a value" >&2; exit 2; }
      PORT="$2"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

cd "$EXECUTION_DIR"
exec uv run uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers 1 \
  --no-access-log
