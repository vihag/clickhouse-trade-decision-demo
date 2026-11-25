#!/usr/bin/env bash
# One command from a fresh clone to a running, tested demo.
#   ./install.sh            local Docker stack, or ClickHouse Cloud when CLICKHOUSE_CLOUD_API_KEY is set in .env
#   ./install.sh --local    force the local Docker stack
#   ./install.sh --cloud    force ClickHouse Cloud (needs CLICKHOUSE_CLOUD_API_KEY + _SECRET in .env)
# Re-runnable: every step reuses what already exists.
set -euo pipefail
cd "$(dirname "$0")"
MODE=auto
for a in "$@"; do case "$a" in --local) MODE=local;; --cloud) MODE=cloud;; -h|--help) sed -n '2,6p' "$0"; exit 0;; *) echo "unknown option $a"; exit 2;; esac; done

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[31mINSTALL FAILED:\033[0m %s\n' "$*"; trap - EXIT; exit 1; }
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    printf "\n\033[31mINSTALL FAILED\033[0m (exit %s) in the step above. Fix the cause it names, then rerun ./install.sh (every step is idempotent). Help: README.md -> Troubleshooting.\n" "$rc"
  fi
}
trap on_exit EXIT
envval() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"' || true; }

[ -f .env ] || { cp .env.example .env; chmod 600 .env; echo "created .env from .env.example"; }
if [ "$MODE" = auto ]; then
  if [ -n "$(envval CLICKHOUSE_CLOUD_API_KEY)" ]; then MODE=cloud; else MODE=local; fi
fi
echo "install mode: $MODE"

step "Prerequisites"
pick_python() {  # first Python >= 3.11 on PATH; else let uv (if installed) fetch one
  for c in ${PYTHON:-} python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$c"; return
    fi
  done
  if command -v uv >/dev/null 2>&1 && uv python install -q 3.12 >/dev/null 2>&1; then uv python find 3.12; fi
}
PYTHON="$(pick_python)"
[ -n "$PYTHON" ] || fail "Python 3.11+ not found (python3 is $(python3 -V 2>&1 || echo missing)). macOS: brew install python@3.12; Ubuntu/Debian: apt install python3.12 python3.12-venv; or install uv (https://docs.astral.sh/uv/) and rerun."
export PYTHON
echo "python: $PYTHON ($("$PYTHON" -V 2>&1))"
command -v make >/dev/null || fail "make not found (macOS: xcode-select --install; Debian/Ubuntu: apt install make)"
command -v curl >/dev/null || fail "curl not found"
DOCKER="${DOCKER:-docker}"
if [ "$MODE" = local ]; then
  command -v "${DOCKER##* }" >/dev/null || fail "Docker not found. Install Docker Desktop (https://docs.docker.com/get-docker/) and start it, or use the cloud path (see README)."
  $DOCKER info >/dev/null 2>&1 || fail "Docker is installed but not responding. Start Docker Desktop and rerun. (Linux, permission denied on docker.sock: rerun as DOCKER=\"sudo docker\" ./install.sh)"
  $DOCKER compose version >/dev/null 2>&1 || fail "Docker Compose v2 not found ('docker compose'). Update Docker Desktop / install the compose plugin."
fi
echo "ok"

step "Python environment (.venv)"
make -s setup

if [ "$MODE" = local ]; then
  step "Local ClickHouse + Postgres (docker compose)"
  make -s local-up DOCKER="$DOCKER"
else
  [ -n "$(envval CLICKHOUSE_CLOUD_API_SECRET)" ] || fail "CLICKHOUSE_CLOUD_API_SECRET is empty in .env"
  step "ClickHouse Cloud: service + Managed Postgres (first run: about 5-10 min)"
  make -s cloud-up
fi

step "Bootstrap: schemas, ~5M synthetic rows (first run: 1-3 min), read-only MCP user, reset, preflight"
make -s bootstrap

if [ "$MODE" = cloud ]; then
  step "ClickHouse Cloud: ClickPipe Postgres CDC -> trade_demo_cdc"
  make -s cloud-pipe
fi

step "App (background)"
make -s stop >/dev/null 2>&1 || true
.venv/bin/python scripts/configure.py app-port
make -s serve-bg

if [ "$MODE" = local ]; then
  step "Offline test suite"
  make -s test-offline
fi

step "Scripted trial through the running app (A auto-approve, burst, B reject, C restricted)"
make -s trial

PORT="$(envval APP_PORT)"; PORT="${PORT:-8000}"
printf '\n\033[32mINSTALL OK (%s)\033[0m  Demo running at http://127.0.0.1:%s  -> click "Start demo", wait for READY.\n' "$MODE" "$PORT"
echo "Stop the app: make stop    Tear everything down: make teardown    All targets: make help"
