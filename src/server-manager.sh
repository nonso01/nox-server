#!/usr/bin/env bash

SERVER_NAME="nox"
SERVER_DIR="$(dirname "$(realpath "$0")")"

# $PREFIX is a Termux-specific env var (points to Termux's install root,
# e.g. /data/data/com.termux/files/usr). On a regular Linux/macOS system
# it's unset, so fall back to /tmp there. This makes the script portable
# across "every system capable of running linux" instead of assuming
# Termux like the original did unconditionally.
TMP_DIR="${PREFIX:-}/tmp"
mkdir -p "$TMP_DIR" 2>/dev/null

PID_FILE="$TMP_DIR/$SERVER_NAME.pid"
LOG_FILE="$TMP_DIR/$SERVER_NAME.log"

# Entry point for the Python port (was: target/release/$SERVER_NAME binary)
SCRIPT_PATH="$SERVER_DIR/main.py"

# Resolve a Python interpreter. Respect $PYTHON_BIN if the caller set one
# (e.g. to point at a venv), otherwise try python3 then python, in that
# order, since "python" is ambiguous (Python 2 on some older/embedded
# systems) while "python3" is the unambiguous modern standard.
resolve_python() {
  if [ -n "$PYTHON_BIN" ]; then
    echo "$PYTHON_BIN"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    echo "python"
    return 0
  fi
  return 1
}

start_server() {
  # Load environment variables safely
  if [ -f "$SERVER_DIR/.env" ]; then
    set -o allexport
    source "$SERVER_DIR/.env"
    set +o allexport
  fi

  if [ -f "$PID_FILE" ]; then
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "Server is already running (PID: $(cat "$PID_FILE"))"
      return 1
    else
      rm "$PID_FILE"
    fi
  fi

  PYTHON_CMD="$(resolve_python)"
  if [ -z "$PYTHON_CMD" ]; then
    echo "No Python interpreter found (looked for \$PYTHON_BIN, python3, python)."
    echo "Install Python 3, or set PYTHON_BIN=/path/to/python before running this script."
    return 1
  fi

  if [ ! -f "$SCRIPT_PATH" ]; then
    echo "Server script not found at: $SCRIPT_PATH"
    return 1
  fi

  echo "Starting server with $PYTHON_CMD..."
  cd "$SERVER_DIR" || return 1

  # No build step for Python (unlike `cargo build --release`) — the
  # interpreter runs main.py directly, so we go straight to launching it.
  nohup "$PYTHON_CMD" "$SCRIPT_PATH" >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"

  # Briefly confirm the process didn't die immediately (e.g. import error,
  # port already in use) before declaring success.
  sleep 0.5
  if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Server failed to start. Check the log for details:"
    echo "  $LOG_FILE"
    rm -f "$PID_FILE"
    return 1
  fi

  echo "Server started with PID: $(cat "$PID_FILE")"
  echo "Logs: $LOG_FILE"
}

stop_server() {
  if [ ! -f "$PID_FILE" ]; then
    echo "Server is not running"
    return 1
  fi

  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "Stopping server (PID: $PID)..."
    kill "$PID"
    rm "$PID_FILE"
    rm -f "$LOG_FILE"
    echo "Server stopped"
  else
    echo "Server process not found, removing stale PID file"
    rm "$PID_FILE"
  fi
}

restart_server() {
  stop_server
  sleep 2
  start_server
}

status() {
  if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
      echo "Server is running (PID: $PID)"
      # cat "$LOG_FILE"
    else
      echo "Server is not running (stale PID file)"
      rm "$PID_FILE"
    fi
  else
    echo "Server is not running"
  fi
}

logs() {
  if [ -f "$LOG_FILE" ]; then
    tail -f "$LOG_FILE"
  else
    echo "Log file not found"
  fi
}

case "$1" in
start) start_server ;;
stop) stop_server ;;
restart) restart_server ;;
status) status ;;
logs) logs ;;
*) echo "Usage: $0 {start|stop|restart|status|logs}" ;;
esac