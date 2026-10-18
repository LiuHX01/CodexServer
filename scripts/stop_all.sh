#!/usr/bin/env bash
# Stop CodexServer services started by scripts/start_all.sh.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="$ROOT_DIR/.runtime"
USE_LEGACY_FALLBACK=1
TERM_WAIT_SECONDS=5

usage() {
  cat <<'USAGE'
Usage: ./scripts/stop_all.sh [options]

Stop CodexServer services recorded by start_all.sh, including their child processes.

Options:
  --no-legacy-fallback  Do not search for Worker processes from older starts.
  --runtime-dir DIR     Read PID files from DIR instead of .runtime.
  -h, --help            Show this help.
USAGE
}

while (($# > 0)); do
  case "$1" in
    --no-legacy-fallback)
      USE_LEGACY_FALLBACK=0
      ;;
    --runtime-dir)
      [[ $# -ge 2 ]] || { echo "error: --runtime-dir needs a value" >&2; exit 2; }
      RUNTIME_DIR="$2"
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

pid_is_alive() {
  kill -0 "$1" 2>/dev/null
}

child_pids() {
  pgrep -P "$1" 2>/dev/null || true
}

signal_tree() {
  local pid="$1"
  local signal="$2"
  local child
  for child in $(child_pids "$pid"); do
    signal_tree "$child" "$signal"
  done
  kill "-$signal" "$pid" 2>/dev/null || true
}

stop_pid() {
  local pid="$1"
  local label="$2"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  if ! pid_is_alive "$pid"; then
    return 0
  fi

  printf 'stopping %s (pid %s)\n' "$label" "$pid"
  signal_tree "$pid" TERM

  local deadline=$((SECONDS + TERM_WAIT_SECONDS))
  while pid_is_alive "$pid" && ((SECONDS < deadline)); do
    sleep 0.2
  done
  if pid_is_alive "$pid"; then
    printf 'force-stopping %s (pid %s)\n' "$label" "$pid"
    signal_tree "$pid" KILL
  fi
}

stop_pid_file() {
  local file="$1"
  local label="$2"
  [[ -f "$file" ]] || return 0
  local pid
  pid="$(sed -n '1p' "$file")"
  stop_pid "$pid" "$label"
  rm -f "$file"
}

stop_pid_file "$RUNTIME_DIR/agent-execution.pid" "agent-execution"
stop_pid_file "$RUNTIME_DIR/agent-management.pid" "agent-management"
stop_pid_file "$RUNTIME_DIR/container-cloud-worker.pid" "container-cloud-worker"

# Older start_all.sh versions did not write PID files. Restrict the fallback
# to the exact Worker module so unrelated Python services are not affected.
if [[ "$USE_LEGACY_FALLBACK" == 1 ]]; then
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    stop_pid "$pid" "legacy container-cloud-worker"
  done < <(pgrep -f '[s]cripts.container_cloud_worker' 2>/dev/null || true)
fi

rm -f \
  "$RUNTIME_DIR/agent-execution.pid" \
  "$RUNTIME_DIR/agent-management.pid" \
  "$RUNTIME_DIR/container-cloud-worker.pid"
printf 'CodexServer services stopped.\n'
