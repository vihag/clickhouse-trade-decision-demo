# Trade Decision Demo. Start here: ./install.sh (or `make install`). `make help` lists every target.
PY      := .venv/bin/python
PYTHON  ?= python3
DOCKER  ?= docker
# Compose reads only .compose.env (host ports, written by `make env-local`), never the app's .env (secrets).
COMPOSE ?= $(DOCKER) compose --env-file $$([ -f .compose.env ] && echo .compose.env || echo /dev/null)
APP_PORT ?= $(or $(shell sed -n 's/^APP_PORT=//p' .env 2>/dev/null),8000)

.DEFAULT_GOAL := help
.PHONY: help install setup local-up local-down env-local schema seed reset preflight demo serve-bg stop trial \
        test test-offline queries bootstrap cloud-check cloud-up cloud-pipe cloud-down cloud-destroy \
        tunnel-bg tunnel-stop teardown clean

help:             ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

install:          ## ONE COMMAND: clone -> running demo (local Docker, or ClickHouse Cloud if the API key is in .env)
	./install.sh

VENV_OK := .venv/.installed
setup: $(VENV_OK) ## Python venv + dependencies (idempotent)
$(VENV_OK): requirements.txt
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11+ required (found " + sys.version.split()[0] + "); run ./install.sh or set PYTHON=python3.12")'
	@echo "creating .venv and installing requirements.txt (first run: about 1 minute)"
	@if command -v uv >/dev/null 2>&1; then uv venv -q --allow-existing --python $(PYTHON) .venv && uv pip install -q --python $(PY) -r requirements.txt; \
	else $(PYTHON) -m venv .venv && $(PY) -m pip install -q --upgrade pip && $(PY) -m pip install -q --prefer-binary -r requirements.txt; fi
	@touch $(VENV_OK)   # a stamp file: .venv/bin/python is a symlink to a possibly read-only interpreter

# ---------------------------------------------------------------- local (Docker) stack
local-up: env-local ## start local ClickHouse + Postgres (docker compose) and wait until healthy
	$(COMPOSE) up -d --wait
env-local: setup  ## point .env at the local containers (keeps any other settings)
	$(PY) scripts/configure.py local
local-down:       ## stop the local containers and delete their data
	$(COMPOSE) down -v

# ---------------------------------------------------------------- ClickHouse Cloud
cloud-check: setup ## validate the Cloud API key and show what `cloud-up` would create (no changes)
	$(PY) scripts/cloud.py check
cloud-up: setup   ## create/reuse ClickHouse service + Managed Postgres (writes .env, .pg_conn)
	$(PY) scripts/cloud.py up
cloud-pipe: setup ## create/reuse the ClickPipe Postgres CDC (after bootstrap)
	$(PY) scripts/cloud.py pipe
cloud-down: setup ## delete the ClickPipe + Managed Postgres (keeps the ClickHouse service and its data)
	$(PY) scripts/cloud.py down
cloud-destroy: setup ## delete the ClickPipe, Managed Postgres AND the ClickHouse service
	$(PY) scripts/cloud.py down --all

# ---------------------------------------------------------------- demo data + app
bootstrap: setup  ## idempotent: PG schema+fixtures, CH schema, seed if <1M rows, demo_ro, reset, preflight -> GREEN/RED
	$(PY) scripts/bootstrap.py
schema: setup     ## (re)apply CH + PG schema and the read-only CH user
	$(PY) scripts/apply_schema.py
seed: setup       ## ~5M synthetic rows (skips if present)
	$(PY) scripts/seed_history.py
reset: setup      ## same reset as the Start demo button
	$(PY) scripts/reset_demo.py
preflight: setup  ## same checks as the Start demo button
	$(PY) scripts/preflight.py
demo: setup       ## app in the foreground on http://127.0.0.1:$(APP_PORT) (Ctrl-C to stop)
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(APP_PORT)
serve-bg: setup   ## app in the background (logs/app.log, logs/app.pid)
	@mkdir -p logs
	@if [ -f logs/app.pid ] && kill -0 $$(cat logs/app.pid) 2>/dev/null; then echo "already running (pid $$(cat logs/app.pid)) on http://127.0.0.1:$(APP_PORT)"; \
	else nohup $(PY) -m uvicorn app.main:app --host 127.0.0.1 --port $(APP_PORT) >> logs/app.log 2>&1 & echo $$! > logs/app.pid; \
	for i in $$(seq 1 60); do curl -s -o /dev/null http://127.0.0.1:$(APP_PORT)/ && break; sleep 0.5; done; \
	curl -s -o /dev/null http://127.0.0.1:$(APP_PORT)/ && echo "app running (pid $$(cat logs/app.pid)) -> http://127.0.0.1:$(APP_PORT)" || (echo "app failed to start; see logs/app.log"; exit 1); fi
stop: tunnel-stop ## stop the background app (and the tunnel)
	@if [ -f logs/app.pid ] && kill -0 $$(cat logs/app.pid) 2>/dev/null; then P=$$(cat logs/app.pid); kill $$P; \
	for i in $$(seq 1 20); do kill -0 $$P 2>/dev/null || break; sleep 0.5; done; \
	kill -0 $$P 2>/dev/null && kill -9 $$P; echo "stopped pid $$P"; else echo "app not running"; fi; rm -f logs/app.pid
trial: setup      ## scripted A / burst / B-reject / C via the running app -> evidence/previous_run/<ts>, leaves it READY
	$(PY) scripts/trial.py --base http://127.0.0.1:$(APP_PORT)
queries: setup    ## run sql/demo_queries.sql, save outputs to evidence/
	$(PY) scripts/run_demo_queries.py

# ---------------------------------------------------------------- tests
test-offline: setup ## fast tests: local Postgres (make local-up) + faked ClickHouse/MCP; no cloud needed
	@$(PY) -c 'import sys, psycopg; psycopg.connect(sys.argv[1] or "postgresql://demo:demo@127.0.0.1:5433/trade_demo", connect_timeout=5).close()' "$$(sed -n 's/^DATABASE_URL_LOCAL=//p' .env 2>/dev/null)" 2>/dev/null \
	  || { echo "test-offline needs the local Postgres container: run 'make local-up' (or ./install.sh --local) first"; exit 1; }
	CLICKHOUSE_SECURE=true POSTGRES_MODE=local DATABASE_URL_LOCAL=$$(grep -E '^DATABASE_URL_LOCAL=' .env 2>/dev/null | cut -d= -f2-)  LANGFUSE_TRACING_ENABLED=false CLICKHOUSE_HOST=unreachable.invalid $(PY) -m pytest -q tests/test_offline.py tests/test_features.py
test: setup       ## LIVE end-to-end against whatever .env points at (resets the demo first)
	DEMO_LIVE_TESTS=1 $(PY) -m pytest -q -s tests

# ---------------------------------------------------------------- optional public URL
tunnel-bg:        ## public URL via ngrok (NGROK_DOMAIN in .env, else a random ngrok URL)
	@mkdir -p .run
	@if [ -f .run/tunnel.pid ] && kill -0 $$(cat .run/tunnel.pid) 2>/dev/null; then echo "tunnel already running (pid $$(cat .run/tunnel.pid))"; exit 0; fi; \
	command -v ngrok >/dev/null 2>&1 || { echo "ngrok not installed (https://ngrok.com/download), then: ngrok config add-authtoken <token>"; exit 1; }; \
	D=$$(grep -E '^NGROK_DOMAIN=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' ); \
	nohup ngrok http $(APP_PORT) $${D:+--url=$$D} --log=stdout >> .run/tunnel.log 2>&1 & echo $$! > .run/tunnel.pid; \
	for i in $$(seq 1 40); do U=$$(curl -sf http://127.0.0.1:4040/api/tunnels 2>/dev/null | python3 -c 'import sys,json; t=json.load(sys.stdin)["tunnels"]; print(t[0]["public_url"] if t else "")' 2>/dev/null); [ -n "$$U" ] && break; \
	kill -0 $$(cat .run/tunnel.pid) 2>/dev/null || break; sleep 0.25; done; \
	if [ -n "$$U" ]; then echo "tunnel running (pid $$(cat .run/tunnel.pid)) -> $$U  (anyone with the link can click Start demo)"; \
	else echo "tunnel failed to start; see .run/tunnel.log"; rm -f .run/tunnel.pid; exit 1; fi
tunnel-stop:      ## stop the ngrok tunnel only
	@if [ -f .run/tunnel.pid ] && kill -0 $$(cat .run/tunnel.pid) 2>/dev/null; then P=$$(cat .run/tunnel.pid); kill $$P; echo "stopped tunnel pid $$P"; fi; rm -f .run/tunnel.pid

# ---------------------------------------------------------------- teardown
teardown: stop    ## stop the app; local: remove containers; cloud: delete ClickPipe + Managed Postgres
	@if grep -qE '^POSTGRES_MODE=managed' .env 2>/dev/null; then $(PY) scripts/cloud.py down; else $(COMPOSE) down -v; fi
clean: stop       ## remove the venv, logs and run evidence (keeps .env)
	rm -rf .venv logs .run evidence/previous_run .pytest_cache
