# AGENTS.md: instructions for AI coding agents

You are setting up (or changing) the ClickHouse Trade Decision Demo. Read this file fully before acting.

## Goal and done-criteria
Get the demo running for the user and prove it. **Done** means `./install.sh` printed `INSTALL OK (<mode>)` and the
app answers on `http://127.0.0.1:$APP_PORT` (`APP_PORT` in `.env`, default 8000). Then tell the user to open it,
click **Start demo**, wait for **READY**, and follow `docs/RUN_OF_SHOW.md`.

## Setup contract
1. Choose the mode with the user:
   - **local** (default, no accounts): needs Docker running with Compose v2, Python 3.11+, `make`, `curl`.
   - **cloud**: the user creates a ClickHouse Cloud API key (console → Organization → API keys, role **Admin**) and
     puts `CLICKHOUSE_CLOUD_API_KEY` / `CLICKHOUSE_CLOUD_API_SECRET` into `.env` **themselves**. Optional:
     `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`, `CLICKHOUSE_REGION`.
     Before creating anything, run `make cloud-check` and show the user the plan (it changes nothing), and get a yes:
     the cloud path creates billable resources in their organization.
2. Run `./install.sh` (auto-picks cloud when the API key is set; `--local` / `--cloud` force it).
   On Linux where Docker needs root: `DOCKER="sudo docker" ./install.sh`.
3. If a step fails, read the message (each failure names the fix), fix the cause, and **rerun `./install.sh`**. Every
   step is idempotent: it reuses existing containers, data, cloud resources and ports. Don't hand-run the steps out
   of order unless debugging.
4. Verify: `make preflight` → `PREFLIGHT: GREEN`; `curl -s http://127.0.0.1:$APP_PORT/health`; the last trial's
   evidence is in `evidence/previous_run/LATEST`.
5. Cleanup only when the user asks: `make teardown` (local: containers + data; cloud: ClickPipe + Managed Postgres),
   `make cloud-destroy` (cloud: also the ClickHouse service and its data).

## Rules
- **Secrets:** never print, log, paste or commit `.env`, `.pg_conn`, passwords, API keys, connection strings or tokens.
  Check presence by key name (`grep -c '^CLICKHOUSE_PASSWORD=.' .env`), never by value. Don't ask the user to paste
  keys into chat; ask them to edit `.env`. `scripts/envfile.py` edits `.env` without echoing values.
- Never ask for or use the user's console password; the API key is the only cloud credential.
- Destructive or billable actions (`cloud-up`, `cloud-pipe`, `cloud-down`, `cloud-destroy`, `teardown`, `clean`)
  need the user's go-ahead. `./install.sh` in cloud mode runs `cloud-up` and `cloud-pipe`.
- Don't commit generated files: `.env`, `.pg_conn`, `.compose.env`, `logs/`, `.run/`, `evidence/previous_run/`.
- Keep the data fictitious. Don't turn the rule-based policy into anything that looks like real trading advice.

## Layout
| Path | What |
|---|---|
| `install.sh` | the one command: prereqs → venv → infra → bootstrap → (cloud: ClickPipe) → app → tests → trial |
| `Makefile` | every step as a target; `make help` |
| `docker-compose.yml` | local ClickHouse 25.8 + Postgres 17 (ports from `.compose.env`, written by `scripts/configure.py`) |
| `scripts/cloud.py` | ClickHouse Cloud OpenAPI provisioner: `check`, `up`, `pipe`, `down [--all]`, `--dry-run` |
| `scripts/configure.py` | points `.env` at the local stack and picks free ports |
| `scripts/bootstrap.py` | schemas, seed (~5M rows, skipped if ≥1M), `demo_ro`, reset, preflight |
| `scripts/trial.py` | scripted A / burst / B-reject / C through the running app → `evidence/previous_run/` |
| `app/main.py` | FastAPI app and API; `app/static/index.html` is the whole UI |
| `app/mcp_server.py`, `app/mcp_client.py` | the stdio MCP tool `get_recent_activity` (read-only user) |
| `app/agent.py` | deterministic policy (no LLM) |
| `app/postgres_store.py` | Postgres system of record (`POSTGRES_MODE=managed` uses `.pg_conn`, `local` uses `DATABASE_URL_LOCAL`) |
| `app/cdc.py` | ClickPipes mirror status and reconciliation |
| `app/links.py` | console deep links (Cloud console, or the local Play UI when `CLICKHOUSE_SECURE=false`) |
| `app/config.py` | all configuration, from `.env` (see `.env.example` for every key) |
| `sql/` | ClickHouse and Postgres schemas, demo queries |
| `tests/` | `make test-offline` (fast, faked ClickHouse), `make test` (live) |

## Changing code
- Run `make test-offline` after any change (needs `make local-up` for Postgres); run `./install.sh --local` for the
  full check. CI runs `./install.sh --local` twice (fresh + idempotent rerun).
- Keep `./install.sh` idempotent and non-interactive. New config goes in `.env.example` with a comment, read through
  `app/config.py` (`_env()` treats blank as unset).
- The UI must keep working in both modes: guard Cloud-only links and features (see `app/links.py`).

## Known limits
- ClickPipes CDC is ClickHouse Cloud only; on the local path step 6 and the audit trail show "not configured".
- Managed Postgres is in beta; if creation fails in a region, set `CLICKHOUSE_REGION` or check the org has it.
- A new cloud service allows only the installer's public IP (`CLICKHOUSE_IP_ALLOW` overrides; VPNs change exit IPs).
- The 15-minute window: after ~10 min the page shows WINDOW EXPIRED; Start demo again.
