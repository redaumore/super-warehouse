#!/usr/bin/env bash
# Bring up the full local dev environment after a reboot.
#
# Starts, in order:
#   1. Docker Desktop (macOS) if the daemon is down
#   2. Postgres + pgvector (docker compose)
#   3. Alembic migrations
#   4. Backoffice UI (Gradio, 127.0.0.1:7860)
#   5. Telegram loop (scripts/telegram_loop.py): ngrok tunnel + setWebhook +
#      the intake API (src.api.webhook:app) on 127.0.0.1:8000
#
# The Telegram loop serves the same FastAPI app as `make run`, so the plain
# API is not started separately (both own port 8000).
#
# Usage:
#   scripts/dev.sh up [--seed]   # start everything (default subcommand: up)
#   scripts/dev.sh down          # stop app processes + Postgres (data kept)
#   scripts/dev.sh status        # show what is running
#
# Logs and PID files live in ./logs/ (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=".venv/bin/python"
LOG_DIR="logs"
TELEGRAM_PORT="${TELEGRAM_PORT:-8000}"
BACKOFFICE_PORT="${BACKOFFICE_PORT:-7860}"
DB_CONTAINER="super-warehouse-db"
DOCKER_TIMEOUT=120
DB_TIMEOUT=90
HTTP_TIMEOUT=90

info() { printf '\033[1;34m[dev]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[dev]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[dev]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[dev]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
}

is_alive() { kill -0 "$1" 2>/dev/null; }

port_pid() { lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true; }

wait_for_docker() {
  if docker info >/dev/null 2>&1; then
    info "Docker daemon is up"
    return 0
  fi
  info "Docker daemon is down — starting Docker Desktop"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    open -a Docker || true
  else
    warn "Not macOS — make sure Docker is started manually"
  fi
  local deadline=$((SECONDS + DOCKER_TIMEOUT))
  while ((SECONDS < deadline)); do
    if docker info >/dev/null 2>&1; then
      ok "Docker daemon is up"
      return 0
    fi
    sleep 3
  done
  die "Docker daemon did not start within ${DOCKER_TIMEOUT}s. Start it manually and retry."
}

ensure_venv() {
  if [[ -x "$PY" ]] && "$PY" -m pip show super-warehouse >/dev/null 2>&1; then
    info "Python venv OK ($("$PY" --version 2>&1))"
    return 0
  fi
  info "Creating .venv and installing dependencies (one-time)"
  python3 -m venv .venv
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -e ".[dev]"
  ok "Python venv ready"
}

ensure_env_file() {
  [[ -f .env ]] || die ".env is missing — copy .env.example to .env and set the required vars."
  grep -q '^POSTGRES_PASSWORD=.\+' .env \
    || die "POSTGRES_PASSWORD is not set in .env (docker compose requires it)."
}

wait_for_db() {
  info "Waiting for Postgres to become healthy"
  local deadline=$((SECONDS + DB_TIMEOUT))
  local status
  while ((SECONDS < deadline)); do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$DB_CONTAINER" 2>/dev/null || true)"
    if [[ "$status" == "healthy" ]]; then
      ok "Postgres is healthy"
      return 0
    fi
    if [[ "$status" == "unhealthy" ]]; then
      die "Postgres is unhealthy — check logs: docker compose logs db"
    fi
    sleep 2
  done
  die "Postgres did not become healthy within ${DB_TIMEOUT}s — check logs: docker compose logs db"
}

start_service() {
  # $1: name; rest: command. Logs to logs/<name>.log, PID to logs/<name>.pid.
  local name="$1"; shift
  local pid_file="$LOG_DIR/$name.pid"
  local log_file="$LOG_DIR/$name.log"
  if [[ -f "$pid_file" ]] && is_alive "$(cat "$pid_file")"; then
    warn "$name is already running (pid $(cat "$pid_file"))"
    return 0
  fi
  rm -f "$pid_file"
  info "Starting $name"
  nohup "$@" >>"$log_file" 2>&1 &
  echo "$!" >"$pid_file"
}

wait_for_http() {
  # $1: url; $2: service name; $3: optional timeout (seconds).
  local url="$1" name="$2" timeout="${3:-$HTTP_TIMEOUT}"
  local deadline=$((SECONDS + timeout))
  info "Waiting for $name at $url"
  while ((SECONDS < deadline)); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
      ok "$name is up"
      return 0
    fi
    if [[ -f "$LOG_DIR/$name.pid" ]] && ! is_alive "$(cat "$LOG_DIR/$name.pid")"; then
      die "$name exited during startup — see $LOG_DIR/$name.log"
    fi
    sleep 2
  done
  die "$name did not respond within ${timeout}s — see $LOG_DIR/$name.log"
}

do_up() {
  local seed=0
  [[ "${1:-}" == "--seed" ]] && seed=1
  mkdir -p "$LOG_DIR"

  wait_for_docker
  ensure_venv
  ensure_env_file

  info "Starting Postgres (pgvector)"
  docker compose up -d db
  wait_for_db

  info "Running Alembic migrations"
  "$PY" -m alembic upgrade head
  "$PY" -m alembic current

  info "Running Alembic migrations on the test database"
  TEST_DB_URL="$("$PY" -c 'from src.config import get_settings; print(get_settings().sqlalchemy_test_database_url)')"
  ALEMBIC_DATABASE_URL="$TEST_DB_URL" "$PY" -m alembic upgrade head

  if ((seed)); then
    info "Seeding inventory (idempotent)"
    "$PY" scripts/seed_inventory.py
  fi

  if [[ -n "$(port_pid "$BACKOFFICE_PORT")" ]]; then
    warn "Port $BACKOFFICE_PORT is already in use — skipping backoffice"
  else
    start_service backoffice "$PY" -m src.backoffice.app
    wait_for_http "http://127.0.0.1:$BACKOFFICE_PORT/" backoffice 60
  fi

  if [[ -n "$(port_pid "$TELEGRAM_PORT")" ]]; then
    warn "Port $TELEGRAM_PORT is already in use — skipping telegram loop"
  else
    grep -q '^TELEGRAM_BOT_TOKEN=.\+' .env \
      || die "TELEGRAM_BOT_TOKEN is not set in .env — the telegram loop needs it."
    command -v ngrok >/dev/null 2>&1 \
      || die "ngrok is not installed or not on PATH (brew install ngrok && ngrok config add-authtoken <token>)."
    start_service telegram "$PY" scripts/telegram_loop.py --port "$TELEGRAM_PORT"
    wait_for_http "http://127.0.0.1:$TELEGRAM_PORT/healthz" telegram 90
  fi

  echo
  ok "Environment is up:"
  echo "  Postgres       : localhost:5432 (container $DB_CONTAINER)"
  if curl -fsS -o /dev/null "http://127.0.0.1:$BACKOFFICE_PORT/" 2>/dev/null; then
    echo "  Backoffice     : http://127.0.0.1:$BACKOFFICE_PORT"
  else
    warn "  Backoffice     : NOT responding (see $LOG_DIR/backoffice.log)"
  fi
  if curl -fsS -o /dev/null "http://127.0.0.1:$TELEGRAM_PORT/healthz" 2>/dev/null; then
    echo "  Telegram loop  : http://127.0.0.1:$TELEGRAM_PORT (ngrok webhook active)"
  else
    warn "  Telegram loop  : NOT responding (see $LOG_DIR/telegram.log)"
  fi
  echo "  Logs           : $LOG_DIR/*.log"
  echo "  Stop           : scripts/dev.sh down"
}

do_down() {
  local pid_file name pid
  for pid_file in "$LOG_DIR"/*.pid; do
    [[ -f "$pid_file" ]] || continue
    name="$(basename "$pid_file" .pid)"
    pid="$(cat "$pid_file")"
    if is_alive "$pid"; then
      info "Stopping $name (pid $pid)"
      kill -TERM "$pid"
      local deadline=$((SECONDS + 15))
      while ((SECONDS < deadline)) && is_alive "$pid"; do
        sleep 1
      done
      if is_alive "$pid"; then
        warn "$name ignored SIGTERM — sending SIGKILL"
        kill -KILL "$pid"
      fi
      # The telegram loop cleans up (deleteWebhook + ngrok) on SIGTERM.
    else
      info "$name is not running"
    fi
    rm -f "$pid_file"
  done
  info "Stopping Postgres (data volume preserved)"
  docker compose stop db 2>/dev/null || warn "Docker daemon not running — skipping db stop"
  ok "Environment is down. Run 'scripts/dev.sh up' to start it again."
}

do_status() {
  echo "Environment status:"
  if docker info >/dev/null 2>&1; then
    echo "  Docker   : up"
    echo "  Postgres : $(docker compose ps --format '{{.Service}} {{.Status}}' 2>/dev/null | grep '^db' || echo 'down')"
  else
    echo "  Docker   : down (run scripts/dev.sh up to start Docker Desktop)"
    echo "  Postgres : unknown (Docker daemon down)"
  fi
  local pid_file name pid found=0
  for pid_file in "$LOG_DIR"/*.pid; do
    [[ -f "$pid_file" ]] || continue
    found=1
    name="$(basename "$pid_file" .pid)"
    pid="$(cat "$pid_file")"
    if is_alive "$pid"; then
      echo "  $name : running (pid $pid)"
    else
      echo "  $name : stopped (stale pid $pid)"
    fi
  done
  ((found)) || echo "  (no dev.sh-managed processes)"
  if curl -fsS -o /dev/null "http://127.0.0.1:$TELEGRAM_PORT/healthz" 2>/dev/null; then
    echo "  API healthz ($TELEGRAM_PORT) : ok"
  else
    echo "  API healthz ($TELEGRAM_PORT) : not responding"
  fi
  if curl -fsS -o /dev/null "http://127.0.0.1:$BACKOFFICE_PORT/" 2>/dev/null; then
    echo "  Backoffice ($BACKOFFICE_PORT) : ok"
  else
    echo "  Backoffice ($BACKOFFICE_PORT) : not responding"
  fi
}

case "${1:-up}" in
  up) shift; do_up "$@" ;;
  down) do_down ;;
  status) do_status ;;
  -h | --help) usage ;;
  *) usage >&2; exit 1 ;;
esac
