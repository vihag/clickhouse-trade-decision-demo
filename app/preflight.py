"""One shared preflight used by `make preflight`, `make bootstrap` and the Start demo button.
Every check is isolated: a dead dependency yields a failed line with a one-line hint, never an exception."""
import asyncio, os, time
import httpx
from app import config, postgres_store as pg
from app import mcp_client

HINTS = {
    "ClickHouse reachable": "ClickHouse unreachable: local: `make local-up`; Cloud: start the service and check its IP access list includes this machine. Then click Start demo again.",
    "Seed present (>= 1M rows)": "Synthetic history missing: run `make bootstrap` (seeds ~5M rows).",
    "Case B baseline acct 1042 == $120,000": "Baseline not at the expected value: click Start demo again.",
    "Case A baseline acct 1055 == $14,000": "Baseline not at the expected value: click Start demo again.",
    "Postgres reachable": "Postgres unreachable: check Managed Postgres is running and .pg_conn / DATABASE_URL_MANAGED (local mode: `make local-up`).",
    "No pending decisions": "Leftover pending decisions: click Start demo again.",
    "Demo run exists": "No demo run yet: click Start demo.",
    "Langfuse reachable": "Langfuse unreachable: check network and LANGFUSE_* keys in .env.",
    "MCP tool via read-only user": "MCP tool failed: run `make bootstrap` (creates read-only user demo_ro) and check ClickHouse is up.",
}


def _line(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": detail, "hint": None if ok else HINTS.get(name, "")}


def _err(e):
    return f"({type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''})"


def _ch_checks(ch_factory):
    from app.analytics import recent_activity
    out = []
    try:
        ch = ch_factory()
        rows = ch.query("SELECT count() FROM trade_demo.activity_events").result_rows[0][0]
        out.append(_line("ClickHouse reachable", True, f"({rows:,} rows)"))
        out.append(_line("Seed present (>= 1M rows)", rows >= 1_000_000, f"({rows:,} rows)"))
        b = recent_activity(ch, 1042)["recent_notional_cents"]
        out.append(_line("Case B baseline acct 1042 == $120,000", b == 12_000_000, f"(saw ${b/100:,.0f})"))
        a = recent_activity(ch, 1055)["recent_notional_cents"]
        out.append(_line("Case A baseline acct 1055 == $14,000", a == 1_400_000, f"(saw ${a/100:,.0f})"))
    except Exception as e:
        out.append(_line("ClickHouse reachable", False, _err(e)))
    return out


def _pg_checks():
    try:
        with pg.conn() as c:
            v = c.execute("SHOW server_version").fetchone()["server_version"].split()[0]
        out = [_line("Postgres reachable", True, f"({config.POSTGRES_LABEL} {v})")]
        n = pg.pending_count()
        out.append(_line("No pending decisions", n == 0, f"({n} pending)"))
        rid = pg.current_run_id()
        out.append(_line("Demo run exists", rid is not None, f"({rid})"))
        return out
    except Exception as e:
        return [_line("Postgres reachable", False, f"({config.POSTGRES_LABEL}) {_err(e)}")]


def _langfuse_check():
    if not config.LANGFUSE_ENABLED:
        return [_line("Langfuse reachable", True, "(not configured: tracing off)")]
    try:
        r = httpx.get(os.environ["LANGFUSE_HOST"].rstrip("/") + "/api/public/projects",
                      auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]), timeout=10)
        return [_line("Langfuse reachable", r.status_code == 200, f"(HTTP {r.status_code})")]
    except Exception as e:
        return [_line("Langfuse reachable", False, _err(e))]


async def _mcp_check():
    try:
        m = await asyncio.wait_for(mcp_client.call_get_recent_activity(1042), timeout=30)
        ok = m.get("status") == "ok" and m.get("clickhouse_user") == config.CH_RO_USER
        return [_line("MCP tool via read-only user", ok, f"(user={m.get('clickhouse_user')}, status={m.get('status')})")]
    except Exception as e:
        return [_line("MCP tool via read-only user", False, _err(e))]


def _cdc_info(ch_factory):
    try:
        from app.cdc import reconcile
        r = reconcile(ch_factory())
        if r.get("pg"):
            n = r.get("newest_replicated_decision") or {}
            return (f"CDC ({r['status']}): PG decisions={r['pg']['decisions']} audit_events={r['pg']['audit_events']} | "
                    f"CH mirror decisions={r['ch_mirror']['decisions']} audit_events={r['ch_mirror']['audit_events']} | "
                    f"newest replicated lag={n.get('observed_lag_s')}s")
        return f"CDC: {r['status']}"
    except Exception as e:
        return f"CDC: unavailable (ClickHouse unreachable: {type(e).__name__})"


async def run_checks(ch_factory=None) -> dict:
    ch_factory = ch_factory or config.ch_client
    t0 = time.perf_counter()
    ch_l, pg_l, lf_l, mcp_l = await asyncio.gather(
        asyncio.to_thread(_ch_checks, ch_factory), asyncio.to_thread(_pg_checks),
        asyncio.to_thread(_langfuse_check), _mcp_check())
    checks = ch_l + pg_l + lf_l + mcp_l
    from app import observability as obs
    info = [await asyncio.to_thread(_cdc_info, ch_factory), "LLM: off (rule-based explanation)", obs.preflight_info()]
    failed = [c for c in checks if not c["ok"]]
    return {"ready": not failed, "checks": checks, "info": info, "first_failure": failed[0] if failed else None,
            "elapsed_s": round(time.perf_counter() - t0, 1)}


def print_report(r: dict) -> None:
    for c in r["checks"]:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['name']} {c['detail']}")
    for i in r["info"]:
        print(f"[INFO] {i}")
    if r["ready"]:
        print("\nPREFLIGHT: GREEN ✅")
    else:
        print(f"\nPREFLIGHT: RED ❌  {r['first_failure']['name']} → {r['first_failure']['hint']}")
