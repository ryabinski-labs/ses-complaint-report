#!/usr/bin/env bash
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

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
  [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

start_bg() {
  if is_running; then
    echo "Already running (pid $(cat "$PID_FILE")) → $URL"
    exit 0
  fi
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
  if is_running; then
    kill "$(cat "$PID_FILE")" && echo "Stopped (pid $(cat "$PID_FILE"))."
    rm -f "$PID_FILE"
  else
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
