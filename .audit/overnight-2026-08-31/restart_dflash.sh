#!/usr/bin/env bash
# Restart the DFlash server under the hardware lease.
# Usage: ./restart_dflash.sh [extra dflash_server.py args...]
# Pass "default" for the exact original argument set.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SERVER_DIR="$(cd "$DIR/../../server" && pwd)"
LAUNCH="$SERVER_DIR/launch.sh"
TARGET="/Volumes/BBALL_GM_DATA/models/Qwen3.8-27B-Uncensored-MLX/4-bit"
PORT=8083
LEASE="/var/tmp/local-model-hardware-lease"
LOG="$(mktemp -t dflash-restart)"
ROLLBACK_LOG="$(mktemp -t dflash-restore)"
rollback_armed=0
restoring=0
newpid=""

extra=()
if [ "${1:-default}" != "default" ]; then
  extra=("$@")
fi

if ! mkdir "$LEASE" 2>/dev/null; then
  echo "lease held: $(cat "$LEASE/lease.json" 2>/dev/null)" >&2
  exit 2
fi
cleanup() {
  rm -f "$LEASE/lease.json"
  rmdir "$LEASE" 2>/dev/null || true
}
trap cleanup EXIT
LEASE_ARGV="$0 $*" python3 - "$LEASE/lease.json" <<'PY'
import json, os, sys
from datetime import datetime, timezone
with open(sys.argv[1], "w") as f:
    json.dump({"pid": os.getppid(), "argv": os.environ["LEASE_ARGV"], "acquired_utc": datetime.now(timezone.utc).isoformat()}, f)
PY
start_via_launch() {
  local log="$1"
  shift
  cd "$SERVER_DIR"
  nohup env TARGET_PATH="$TARGET" "$LAUNCH" --host 127.0.0.1 --port "$PORT" "$@" > "$log" 2>&1 &
  newpid=$!
}

wait_ready() {
  local wait_pid="$1"
  local log="$2"
  local ready=0
  for _ in $(seq 1 300); do
    if curl -fsS "http://127.0.0.1:$PORT/health" 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ready") is True or d.get("status") == "ready" else 1)' 2>/dev/null; then
      ready=1
      break
    fi
    if ! kill -0 "$wait_pid" 2>/dev/null; then
      echo "server exited during load:" >&2
      tail -20 "$log" >&2
      return 1
    fi
    sleep 2
  done
  if [ "$ready" -ne 1 ]; then
    echo "server not ready after 600s:" >&2
    tail -20 "$log" >&2
    return 1
  fi
  return 0
}

stop_candidate() {
  local cpid="${1:-}"
  [ -n "$cpid" ] || return 0
  kill -TERM "$cpid" 2>/dev/null || true
  local i
  for i in $(seq 1 60); do
    if ! kill -0 "$cpid" 2>/dev/null && [ -z "$(lsof -ti tcp:$PORT -sTCP:LISTEN || true)" ]; then
      return 0
    fi
    if [ "$i" -eq 30 ] && kill -0 "$cpid" 2>/dev/null; then
      kill -KILL "$cpid" 2>/dev/null || true
    fi
    sleep 1
  done
  if kill -0 "$cpid" 2>/dev/null || [ -n "$(lsof -ti tcp:$PORT -sTCP:LISTEN || true)" ]; then
    return 1
  fi
  return 0
}

restore_default() {
  [ "$rollback_armed" -eq 1 ] || return 1
  [ "$restoring" -eq 0 ] || return 1
  trap - INT TERM HUP
  restoring=1
  rollback_armed=0
  echo "candidate failed; restoring exact default via launch.sh" >&2
  if ! stop_candidate "$newpid"; then
    echo "candidate still running or port $PORT still occupied; not launching default" >&2
    return 1
  fi
  start_via_launch "$ROLLBACK_LOG"
  if ! wait_ready "$newpid" "$ROLLBACK_LOG"; then
    echo "default restoration failed" >&2
    return 1
  fi
  echo "restored pid: $newpid"
  curl -fsS "http://127.0.0.1:$PORT/v1/dflash/stats" || true
  echo
  echo "log: $ROLLBACK_LOG"
  return 0
}

fail_after_exit() {
  if restore_default; then
    exit 2
  fi
  exit 2
}

on_interrupt() {
  trap - INT TERM HUP
  if [ "$rollback_armed" -eq 1 ] && [ "$restoring" -eq 0 ]; then
    restore_default || true
  fi
  exit 2
}
trap on_interrupt INT TERM HUP

pid="$(lsof -ti tcp:$PORT -sTCP:LISTEN || true)"
[ -n "$pid" ] || { echo "no server listening on $PORT" >&2; exit 2; }
req1="$(curl -fsS "http://127.0.0.1:$PORT/v1/dflash/stats" | python3 -c 'import json,sys; print(json.load(sys.stdin)["requests"])')"
sleep 3
req2="$(curl -fsS "http://127.0.0.1:$PORT/v1/dflash/stats" | python3 -c 'import json,sys; print(json.load(sys.stdin)["requests"])')"
[ "$req1" = "$req2" ] || { echo "server busy (requests $req1 -> $req2), refusing restart" >&2; exit 2; }

echo "stopping pid $pid"
newpid="$pid"
rollback_armed=1
if ! kill -TERM "$pid" 2>/dev/null; then
  echo "TERM failed; leaving known default" >&2
  rollback_armed=0
  exit 2
fi
for _ in $(seq 1 60); do
  kill -0 "$pid" 2>/dev/null || break
  sleep 1
done
if kill -0 "$pid" 2>/dev/null; then
  echo "server did not exit on TERM" >&2
  rollback_armed=0
  exit 2
fi
sleep 5


echo "starting with extra args: ${extra[*]:-none}"
if ((${#extra[@]})); then
  start_via_launch "$LOG" "${extra[@]}"
else
  start_via_launch "$LOG"
fi
if ! wait_ready "$newpid" "$LOG"; then
  fail_after_exit
fi

echo "new pid: $newpid"
if ! curl -fsS "http://127.0.0.1:$PORT/v1/dflash/stats"; then
  echo "post-launch stats acquisition failed" >&2
  fail_after_exit
fi
echo
echo "log: $LOG"
