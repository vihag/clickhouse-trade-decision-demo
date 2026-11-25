"""Idempotent setup for a FRESH (or existing) Managed Postgres + ClickHouse service, ending GREEN/RED.

1. Postgres schema + account_policies fixtures (URL from .pg_conn, else .env DATABASE_URL_MANAGED)
2. ClickHouse schema (CREATE ... IF NOT EXISTS; existing data kept)
3. Seed ~5M synthetic rows only if activity_events has < 1M rows
4. Read-only ClickHouse user demo_ro for the MCP tool
5. Reset (fresh baselines) + full preflight
"""
import asyncio, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlparse
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config, postgres_store as pg, analytics
from app.preflight import run_checks, print_report

t0 = time.perf_counter()


HINTS = {"Postgres schema + fixtures": "check Postgres is running and .pg_conn / DATABASE_URL_MANAGED is current (local mode: `make local-up`)",
         "ClickHouse schema": "local: `make local-up`. Cloud: the service must be running and its IP access list must include this machine's public IP (set CLICKHOUSE_IP_ALLOW or edit it in the console). Then rerun ./install.sh"}


def step(name, fn):
    print(f"==> {name}", flush=True)
    try:
        r = fn()
        if r:
            print(f"    {r}", flush=True)
        return True
    except Exception as e:
        print(f"    FAILED: {type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}", flush=True)
        print(f"    hint: {HINTS.get(name, 'fix the error above and rerun make bootstrap (it is idempotent)')}", flush=True)
        print("\nBOOTSTRAP: RED ❌", flush=True)
        sys.exit(1)


def pg_step():
    host = urlparse(config.DATABASE_URL).hostname if config.DATABASE_URL else None
    pg.migrate(); pg.upsert_policies()
    return f"{config.POSTGRES_LABEL} at {host} (URL from {config.DATABASE_URL_SOURCE}); schema + policies applied"


def ch_schema():
    c = config.ch_client()
    for stmt in (ROOT / "sql" / "clickhouse_schema.sql").read_text().split(";"):
        s = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if s:
            c.command(s)
    return "trade_demo tables + MV present"


def seed():
    n = config.ch_client().query("SELECT count() FROM trade_demo.activity_events").result_rows[0][0]
    if n >= 1_000_000:
        return f"seed present ({n:,} rows) — kept"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "seed_history.py")], check=True)
    return "seeded"


def ro_user():
    subprocess.run([sys.executable, str(ROOT / "scripts" / "create_ro_user.py")], check=True)
    importlib_reload()
    return None


def importlib_reload():
    # create_ro_user may append CLICKHOUSE_RO_PASSWORD to .env on first run; pick it up
    from dotenv import dotenv_values
    v = dotenv_values(ROOT / ".env").get("CLICKHOUSE_RO_PASSWORD")
    if v:
        import os
        os.environ["CLICKHOUSE_RO_PASSWORD"] = v
        config.CH_RO_PASSWORD = v


def reset():
    rid = pg.reset_and_new_run()
    ctx = analytics.reset_demo_accounts(config.ch_client())
    return f"run {rid}; baselines 1042=${ctx[1042]['recent_notional_cents']/100:,.0f}, 1055=${ctx[1055]['recent_notional_cents']/100:,.0f}; valid until {ctx['baseline_valid_until_utc']}"


step("Postgres schema + fixtures", pg_step)
step("ClickHouse schema", ch_schema)
step("Synthetic seed (only if < 1M rows)", seed)
step("Read-only ClickHouse user demo_ro", ro_user)
step("Reset demo fixtures", reset)
print("==> Preflight", flush=True)
r = asyncio.run(run_checks())
print_report(r)
print(f"\nBOOTSTRAP: {'GREEN ✅' if r['ready'] else 'RED ❌'}  ({time.perf_counter() - t0:.0f}s)")
sys.exit(0 if r["ready"] else 1)
