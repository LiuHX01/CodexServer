#!/usr/bin/env bash
# Start the management service and its embedded workers.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MANAGEMENT_DIR="$ROOT_DIR/agent-management"
HOST="${MANAGEMENT_HOST:-127.0.0.1}"
PORT="${MANAGEMENT_PORT:-8080}"
REGISTER_CODEX=0
SKIP_POSTGRES=0

usage() {
  cat <<'USAGE'
Usage: ./scripts/start_management.sh [options]

Start agent-management. The process includes the database-backed dispatcher,
event observer, and the container-cloud WebSocket listener when configured in .env.

Options:
  --host HOST          Bind host (default: 127.0.0.1).
  --port PORT          Bind port (default: 8080).
  --management-url URL Container-cloud callback URL (defaults to the bind URL).
  --register-codex     Register the local execution service after migrations.
  --skip-postgres      Reuse the configured database without starting local PostgreSQL.
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
    --management-url)
      [[ $# -ge 2 ]] || { echo "error: --management-url needs a value" >&2; exit 2; }
      MANAGEMENT_URL="$2"
      shift
      ;;
    --register-codex)
      REGISTER_CODEX=1
      ;;
    --skip-postgres)
      SKIP_POSTGRES=1
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

command=(uv run python -m scripts.dev --host "$HOST" --port "$PORT")
if [[ -n "${MANAGEMENT_URL:-}" ]]; then
  command+=(--management-url "$MANAGEMENT_URL")
fi
if [[ "$REGISTER_CODEX" == 1 ]]; then
  command+=(--register-codex)
fi
if [[ "$SKIP_POSTGRES" == 1 ]]; then
  command+=(--skip-postgres)
fi

cd "$MANAGEMENT_DIR"
exec "${command[@]}"
