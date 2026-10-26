#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Yoni Ryabinski
# Start the live SES Complaint Report service.
#
#   ./start.sh          start in the background, print + open the URL
#   ./start.sh fg       run in the foreground (Ctrl-C to stop)
#   ./start.sh stop     stop a background instance
#   ./start.sh status   show whether it is running
#   ./start.sh logs     tail the background log
#
# Config comes from .env (AWS profile/region, port, refresh interval).
set -euo pipefail

# Physical path: process command lines resolve symlinks, so orphan detection
# must compare against the real path, not the path used to invoke the script.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$DIR"

PID_FILE="$DIR/.server.pid"
LOG_DIR="$DIR/logs"
LOG_FILE="$LOG_DIR/service.log"

# load .env (without exporting blindly) for HOST/PORT defaults
if [[ -f "$DIR/.env" ]]; then
  set -a; # shellcheck disable=SC1091
  source "$DIR/.env"; set +a
fi
HOST="${SESREPORT_HOST:-127.0.0.1}"
PORT="${SESREPORT_PORT:-8787}"
URL="http://${HOST}:${PORT}/"

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  kill -0 "$pid" 2>/dev/null || return 1
  # A recycled PID must not make a dead instance look alive.
  ps -p "$pid" -o command= 2>/dev/null | grep -q sesreport
}

# Every sesreport server started from this checkout. The PID file alone is not
# enough: a later start overwrites it, so an older instance (for example a
# demo-mode one on another port) can keep serving untracked.
project_pids() {
  pgrep -f "$DIR/.venv/bin/sesreport" 2>/dev/null || true
}

stop_project() {
  local pids
  pids="$(project_pids)"
  [[ -z "$pids" ]] && return 1
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 20); do
    sleep 0.25
    [[ -z "$(project_pids)" ]] && break
  done
  pids="$(project_pids)"
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
  return 0
}

start_bg() {
  if is_running; then
    echo "Already running (pid $(cat "$PID_FILE")) → $URL"
    exit 0
  fi
  # A previous instance may still be up even though the PID file is gone or
  # points at a dead process. Clear it before starting, so a stale server (for
  # example one serving demo data on another port) cannot answer the browser.
  if stop_project; then
    echo "Stopped a leftover instance from this checkout."
  fi
  rm -f "$PID_FILE"
  mkdir -p "$LOG_DIR"
  echo "Starting SES Complaint Report…"
  nohup uv run sesreport >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  # wait for the port to answer
  for _ in $(seq 1 40); do
    if curl -fsS -o /dev/null "${URL}api/status" 2>/dev/null; then break; fi
    sleep 0.5
  done
  if is_running; then
    echo "Up: $URL  (pid $(cat "$PID_FILE"))"
    echo "Logs: $LOG_FILE"
    command -v open >/dev/null && open "$URL" || true
  else
    echo "Failed to start — see $LOG_FILE" >&2
    tail -n 30 "$LOG_FILE" >&2 || true
    exit 1
  fi
}

stop_bg() {
  if stop_project; then
    rm -f "$PID_FILE"
    echo "Stopped (including any leftover instances from this checkout)."
  else
    rm -f "$PID_FILE"
    echo "Not running."
  fi
}

case "${1:-start}" in
  start) start_bg ;;
  fg)    echo "Running in foreground → $URL  (Ctrl-C to stop)"; exec uv run sesreport ;;
  stop)  stop_bg ;;
  status)
    if is_running; then
      echo "Running (pid $(cat "$PID_FILE")) → $URL"
      curl -fsS "${URL}api/status" && echo
    else
      echo "Not running."
    fi ;;
  logs)  mkdir -p "$LOG_DIR"; tail -f "$LOG_FILE" ;;
  *)     echo "usage: $0 [start|fg|stop|status|logs]" >&2; exit 2 ;;
esac
