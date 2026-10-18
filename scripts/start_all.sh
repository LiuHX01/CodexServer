#!/usr/bin/env bash
# Compatibility helper: start the two independent CodexServer services together.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
MANAGEMENT_ARGS=()
EXECUTION_ARGS=()

usage() {
  cat <<'USAGE'
Usage: ./scripts/start_all.sh [options]

Compatibility helper for starting both services. For separate logs, use
./scripts/start_management.sh and ./scripts/start_execution.sh directly.

Options:
  --execution-host HOST    Execution bind host.
  --execution-port PORT    Execution port.
  --management-host HOST   Management bind host.
  --management-port PORT   Management port.
  --management-url URL     Container-cloud callback URL.
  --register-codex         Register the execution service after migrations.
  --skip-postgres          Reuse the configured database.
  -h, --help               Show this help.
USAGE
}

while (($# > 0)); do
  case "$1" in
    --execution-host)
      [[ $# -ge 2 ]] || { echo "error: --execution-host needs a value" >&2; exit 2; }
      EXECUTION_ARGS+=(--host "$2")
      shift
      ;;
    --execution-port)
      [[ $# -ge 2 ]] || { echo "error: --execution-port needs a value" >&2; exit 2; }
      EXECUTION_ARGS+=(--port "$2")
      shift
      ;;
    --management-host)
      [[ $# -ge 2 ]] || { echo "error: --management-host needs a value" >&2; exit 2; }
      MANAGEMENT_ARGS+=(--host "$2")
      shift
      ;;
    --management-port)
      [[ $# -ge 2 ]] || { echo "error: --management-port needs a value" >&2; exit 2; }
      MANAGEMENT_ARGS+=(--port "$2")
      shift
      ;;
    --management-url)
      [[ $# -ge 2 ]] || { echo "error: --management-url needs a value" >&2; exit 2; }
      MANAGEMENT_ARGS+=(--management-url "$2")
      shift
      ;;
    --register-codex)
      MANAGEMENT_ARGS+=(--register-codex)
      ;;
    --skip-postgres)
      MANAGEMENT_ARGS+=(--skip-postgres)
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

management_pid=""
execution_pid=""
status_dir="$(mktemp -d "${TMPDIR:-/tmp}/codexserver-start.XXXXXX")"
management_status_file="$status_dir/management.status"
execution_status_file="$status_dir/execution.status"
mkdir -p "$RUNTIME_DIR"
cleanup() {
  trap - EXIT INT TERM
  [[ -n "$management_pid" ]] && kill "$management_pid" 2>/dev/null || true
  [[ -n "$execution_pid" ]] && kill "$execution_pid" 2>/dev/null || true
  wait "$management_pid" "$execution_pid" 2>/dev/null || true
  rm -rf "$status_dir"
  rm -f "$RUNTIME_DIR/agent-management.pid" "$RUNTIME_DIR/agent-execution.pid"
}
trap cleanup EXIT INT TERM

(cd "$ROOT_DIR" && set +e; ./scripts/start_management.sh "${MANAGEMENT_ARGS[@]}"; printf '%s\n' "$?" > "$management_status_file") &
management_pid=$!
printf '%s\n' "$management_pid" > "$RUNTIME_DIR/agent-management.pid"
(cd "$ROOT_DIR" && set +e; ./scripts/start_execution.sh "${EXECUTION_ARGS[@]}"; printf '%s\n' "$?" > "$execution_status_file") &
execution_pid=$!
printf '%s\n' "$execution_pid" > "$RUNTIME_DIR/agent-execution.pid"

echo "agent-management and agent-execution started. Press Ctrl+C to stop both."
while [[ ! -f "$management_status_file" && ! -f "$execution_status_file" ]]; do
  sleep 1
done

status=0
if [[ -f "$management_status_file" ]]; then
  status="$(<"$management_status_file")"
elif [[ -f "$execution_status_file" ]]; then
  status="$(<"$execution_status_file")"
fi
if [[ "$status" -ne 0 ]]; then
  echo "CodexServer service exited with status $status" >&2
fi
exit "$status"
