"""Deep links for the UI's 'View details' hyperlinks (presentation only; no secrets).
Console route patterns come from the ClickHouse Cloud console's own router:
  /services/{sid}/console/newQuery?query=<base64>&database=<db>   SQL console, prefilled query
  /services/{sid}/console/database/{db}/table/{table}             table view
  /services/{sid}/imports[/{pipeId}/details?tabName=tab_metrics|tab_tables]  ClickPipes (Data sources)
  /services/{sid}/queryInsights                                   query insights
  /postgres/{pgId}[/console/database/{db}/schema/{s}/table/{t}]   Managed Postgres
IDs can be overridden in .env (CH_CONSOLE_ORG_ID, CH_CONSOLE_SERVICE_ID, CLICKPIPE_ID, PG_CONSOLE_ID)."""
import os, re
from urllib.parse import urlparse
from app import config

CONSOLE = "https://console.clickhouse.cloud"
# Your own ClickHouse Cloud organization and service (console URL: /services/<SERVICE_ID>/...).
# Unset: the 'Open in console' links fall back to the console home page.
ORG_ID = os.getenv("CH_CONSOLE_ORG_ID", "").strip()
SERVICE_ID = os.getenv("CH_CONSOLE_SERVICE_ID", "").strip()
_PG_ID = re.compile(r"^pg[0-9a-hj-km-np-tv-z]{24}$")


def _pg_id() -> str | None:
    """Managed Postgres console ID: the 'pg…' label in the host (trade-demo-pg-x.<pgId>.c0.<region>…)."""
    if os.getenv("PG_CONSOLE_ID"):
        return os.environ["PG_CONSOLE_ID"]
    if config.POSTGRES_MODE != "managed" or not config.DATABASE_URL:
        return None
    try:
        host = urlparse(config.DATABASE_URL).hostname or ""
    except Exception:
        return None
    return next((p for p in host.split(".") if _PG_ID.match(p)), None)


def _local_links() -> dict:
    """Local docker-compose ClickHouse: every 'console' link opens its built-in Play UI (the SQL is also
    copied to the clipboard on click, so it can be pasted there). ClickPipes / Managed Postgres don't exist locally."""
    play = f"http://{config.CH_HOST}:{config.CH_PORT}/play"
    t = {k: play for k in ("raw", "rollup", "mv", "cdc_decisions", "cdc_audit")}
    return {"org_id": None, "service_id": None, "console": "local", "service": play, "sql_console": play,
            "new_query": play, "query_insights": play, "clickpipe_id": None, "clickpipes": play,
            "clickpipe_tables": play, "clickpipes_list": play, "tables": t, "postgres": None,
            "postgres_mode": config.POSTGRES_MODE}


def console_links() -> dict:
    if not config.CH_SECURE:
        return _local_links()
    svc = f"{CONSOLE}/services/{SERVICE_ID}" if SERVICE_ID else CONSOLE
    pipe = (os.getenv("CLICKPIPE_ID") or "").strip()
    pg_id = _pg_id()
    pg_db = (urlparse(config.DATABASE_URL).path.lstrip("/") or "postgres") if config.DATABASE_URL else "postgres"
    tbl = lambda db, t: f"{svc}/console/database/{db}/table/{t}"
    return {
        "org_id": ORG_ID or None, "service_id": SERVICE_ID or None, "console": "cloud",
        "service": svc,
        "sql_console": f"{svc}/console",
        "new_query": f"{svc}/console/newQuery",  # + ?query=<base64 SQL>&database=trade_demo
        "query_insights": f"{svc}/queryInsights",
        # Set CLICKPIPE_ID after (re)creating the pipe; the ID changes on every rebuild. Unset -> Data sources list.
        "clickpipe_id": pipe or None,
        "clickpipes": f"{svc}/imports/{pipe}/details?tabName=tab_metrics" if pipe else f"{svc}/imports",
        "clickpipe_tables": f"{svc}/imports/{pipe}/details?tabName=tab_tables" if pipe else f"{svc}/imports",
        "clickpipes_list": f"{svc}/imports",
        "tables": {
            "raw": tbl("trade_demo", "activity_events"),
            "rollup": tbl("trade_demo", "account_activity_1m"),
            "mv": tbl("trade_demo", "account_activity_1m_mv"),
            "cdc_decisions": tbl("trade_demo_cdc", "trade_demo_decisions"),
            "cdc_audit": tbl("trade_demo_cdc", "trade_demo_audit_events"),
        },
        "postgres": None if not pg_id else {
            "home": f"{CONSOLE}/postgres/{pg_id}",
            "new_query": f"{CONSOLE}/postgres/{pg_id}/console/newQuery",  # + ?query=<base64>&database=<db>
            "database": pg_db,
            "decisions": f"{CONSOLE}/postgres/{pg_id}/console/database/{pg_db}/schema/trade_demo/table/decisions",
            "trade_requests": f"{CONSOLE}/postgres/{pg_id}/console/database/{pg_db}/schema/trade_demo/table/trade_requests",
            "account_policies": f"{CONSOLE}/postgres/{pg_id}/console/database/{pg_db}/schema/trade_demo/table/account_policies",
            "audit_events": f"{CONSOLE}/postgres/{pg_id}/console/database/{pg_db}/schema/trade_demo/table/audit_events",
        },
        "postgres_mode": config.POSTGRES_MODE,
    }


RAW_SQL = """SELECT count() AS recent_trade_count, sum(notional_cents) AS recent_notional_cents
FROM trade_demo.activity_events
WHERE account_id = {acct}
  AND event_ts >= toStartOfMinute(now()) - INTERVAL 14 MINUTE"""


def insight_sql(ch, account_id: int) -> dict:
    """The exact insight query (MV target), its raw-table equivalent, and EXPLAIN indexes=1 for both,
    with read_rows from running each once. Read-only; tolerant of failures."""
    from app.analytics import CONTEXT_SQL
    acct = int(account_id)
    mv_sql = CONTEXT_SQL.replace("{account_id:UInt32}", str(acct))
    raw_sql = RAW_SQL.format(acct=acct)
    out = {"account_id": acct, "mv_sql": mv_sql, "raw_sql": raw_sql}
    for key, sql in (("mv", mv_sql), ("raw", raw_sql)):
        try:
            out[f"{key}_explain"] = "\n".join(r[0] for r in ch.query("EXPLAIN indexes = 1\n" + sql).result_rows)
        except Exception as e:
            out[f"{key}_explain"] = f"(EXPLAIN unavailable: {type(e).__name__})"
        out[f"{key}_stats"] = _granules(out[f"{key}_explain"])
        try:
            res, ms, rr = _timed(ch, sql)
            out[f"{key}_read_rows"], out[f"{key}_ms"], out[f"{key}_result"] = rr, ms, _rows(res)
        except Exception:
            out[f"{key}_read_rows"], out[f"{key}_ms"] = None, None
    try:
        out["raw_total_rows"] = int(ch.query("SELECT count() FROM trade_demo.activity_events").result_rows[0][0])
        out["mv_total_rows"] = int(ch.query("SELECT count() FROM trade_demo.account_activity_1m").result_rows[0][0])
    except Exception:
        out["raw_total_rows"] = out.get("raw_total_rows")
    return out


def _granules(explain: str) -> dict:
    """From EXPLAIN indexes=1: total granules (first index stage) and granules finally selected (last stage)."""
    g = re.findall(r"Granules:\s*(\d+)/(\d+)", explain or "")
    p = re.findall(r"Parts:\s*(\d+)/(\d+)", explain or "")
    return {"granules_selected": int(g[-1][0]) if g else None, "granules_total": int(g[0][1]) if g else None,
            "parts_selected": int(p[-1][0]) if p else None, "parts_total": int(p[0][1]) if p else None}


def _timed(ch, sql):
    import time
    t0 = time.perf_counter()
    res = ch.query(sql)
    s = res.summary or {}
    return res, round((time.perf_counter() - t0) * 1000, 1), int(s.get("read_rows", 0) or 0)


def _jsonable(v):
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None  # NaN/inf (e.g. 0/0) -> null
    return v if isinstance(v, (int, str, type(None))) else str(v)


def _rows(res):
    return [dict(zip(res.column_names, [_jsonable(v) for v in r])) for r in res.result_rows]


PG_READBACK_SQL = """SELECT d.trade_id, d.final_disposition, d.version, d.reviewer, d.review_comment, d.updated_at
FROM trade_demo.decisions d
WHERE d.trade_id = '{tid}';"""
PG_AUDIT_SQL = """SELECT event_id, event_type, actor, created_at
FROM trade_demo.audit_events
WHERE trade_id = '{tid}'
ORDER BY event_id;"""


def pg_readback(trade_id) -> dict:
    """Pre-scripted read-only Postgres queries: the decision row and its audit events (OLTP source of truth)."""
    import time
    from app import postgres_store as pg
    tid = str(trade_id)
    sql, asql = PG_READBACK_SQL.format(tid=tid), PG_AUDIT_SQL.format(tid=tid)
    t0 = time.perf_counter()
    with pg.conn() as c:
        rows = c.execute("""SELECT d.trade_id, d.final_disposition, d.version, d.reviewer, d.review_comment, d.updated_at
                            FROM trade_demo.decisions d WHERE d.trade_id = %s""", (tid,)).fetchall()
        audit = c.execute("""SELECT event_id, event_type, actor, created_at FROM trade_demo.audit_events
                             WHERE trade_id = %s ORDER BY event_id""", (tid,)).fetchall()
    ms = round((time.perf_counter() - t0) * 1000, 1)
    fix = lambda rs: [{k: (v if isinstance(v, (int, float, str, type(None))) else str(v)) for k, v in r.items()} for r in rs]
    return {"sql": sql, "rows": fix(rows), "audit_sql": asql, "audit_rows": fix(audit), "ms": ms,
            "store": config.POSTGRES_LABEL}


CDC_TRADE_SQL = """SELECT trade_id, final_disposition, version, reviewer, updated_at AS pg_updated_at,
       _peerdb_synced_at AS synced_to_clickhouse_at,
       dateDiff('millisecond', updated_at, _peerdb_synced_at) / 1000 AS replication_lag_s
FROM trade_demo_cdc.trade_demo_decisions FINAL
WHERE trade_id = '{tid}' AND _peerdb_is_deleted = 0"""

CDC_ANALYTICS_SQL = """-- Product analytics over the ClickPipes CDC mirror (current state = FINAL, not deleted)
SELECT r.reason_code                                   AS reason,
       d.final_disposition                             AS outcome,
       count()                                         AS decisions,
       round(avg(dateDiff('millisecond', q.requested_at, d.updated_at)) / 1000, 2) AS avg_time_to_final_s,
       round(avg(dateDiff('millisecond', d.updated_at, d._peerdb_synced_at)) / 1000, 1) AS avg_cdc_lag_s
FROM trade_demo_cdc.trade_demo_decisions AS d FINAL
JOIN trade_demo_cdc.trade_demo_recommendations AS r FINAL ON r.trade_id = d.trade_id
JOIN trade_demo_cdc.trade_demo_trade_requests  AS q FINAL ON q.trade_id = d.trade_id
WHERE d._peerdb_is_deleted = 0 AND r._peerdb_is_deleted = 0 AND q._peerdb_is_deleted = 0
GROUP BY reason, outcome
ORDER BY decisions DESC"""

CDC_RATES_SQL = """SELECT count() AS decisions,
       countIf(final_disposition = 'AUTO_APPROVED') AS auto_approved,
       countIf(final_disposition IN ('HUMAN_APPROVED', 'HUMAN_REJECTED')) AS human_decided,
       countIf(final_disposition = 'PENDING_REVIEW') AS pending,
       round(100 * countIf(final_disposition IN ('AUTO_APPROVED', 'HUMAN_APPROVED')) / count(), 1) AS approval_rate_pct,
       round(100 * countIf(final_disposition = 'AUTO_APPROVED') / count(), 1) AS straight_through_pct
FROM trade_demo_cdc.trade_demo_decisions FINAL
WHERE _peerdb_is_deleted = 0"""


def cdc_readback(ch, trade_id) -> dict:
    """The same decision read back from the ClickHouse CDC mirror, plus product analytics over the mirror."""
    tid = str(trade_id)
    out = {"trade_sql": CDC_TRADE_SQL.format(tid=tid), "analytics_sql": CDC_ANALYTICS_SQL, "rates_sql": CDC_RATES_SQL}
    try:
        from app.cdc import mirror_tables, TABLES
        if TABLES["decisions"] not in mirror_tables(ch):
            return {**out, "status": "not configured"}
    except Exception as e:
        return {**out, "status": "unavailable", "error": type(e).__name__}
    for key, sql in (("trade", out["trade_sql"]), ("analytics", CDC_ANALYTICS_SQL), ("rates", CDC_RATES_SQL)):
        try:
            res, ms, rr = _timed(ch, sql)
            out[f"{key}_rows"], out[f"{key}_ms"], out[f"{key}_read_rows"] = _rows(res), ms, rr
        except Exception as e:
            out[f"{key}_rows"], out[f"{key}_error"] = [], f"{type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}"
    out["status"] = "replicated" if out.get("trade_rows") else "pending replication"
    return out


CDC_AUDIT_TRAIL_SQL = """SELECT event_id, event_type, actor, created_at AS pg_created_at,
       _peerdb_synced_at AS synced_to_clickhouse_at,
       dateDiff('millisecond', created_at, _peerdb_synced_at) / 1000 AS replication_lag_s
FROM trade_demo_cdc.trade_demo_audit_events FINAL
WHERE trade_id = '{tid}' AND _peerdb_is_deleted = 0
ORDER BY event_id"""


def audit_trail(ch_factory, trade_id) -> dict:
    """Approve/Reject audit pop-up, read-only. 1) the decision + audit events as persisted in Postgres,
    2) ClickPipes CDC status for exactly this version, 3) the same rows queried in ClickHouse (FINAL on the
    trade_demo_cdc mirror). Cheap enough to poll every few seconds (2 PG selects + 2 point lookups in CH).
    Status never raises: 'replicated' | 'syncing' | 'not configured' | 'unavailable' | 'local fallback'."""
    import uuid
    tid = str(uuid.UUID(str(trade_id)))  # canonical form; also guarantees the literal in the shown SQL is safe
    pg_part = pg_readback(tid)
    want_v = (pg_part["rows"][0]["version"] if pg_part["rows"] else None)
    want_n = len(pg_part["audit_rows"])
    ch = {"decision_sql": CDC_TRADE_SQL.format(tid=tid), "audit_sql": CDC_AUDIT_TRAIL_SQL.format(tid=tid),
          "want_version": want_v, "want_audit_events": want_n, "decision_rows": [], "audit_rows": []}
    out = {"trade_id": tid, "postgres": pg_part, "clickhouse": ch, "postgres_mode": config.POSTGRES_MODE}
    try:
        client = ch_factory()
        from app.cdc import mirror_tables, TABLES
        tables = mirror_tables(client)
    except Exception as e:
        ch.update(status="unavailable", note=f"ClickHouse not reachable ({type(e).__name__})")
        return out
    if TABLES["decisions"] not in tables:
        ch.update(status="not configured", note="No trade_demo_cdc mirror yet (ClickPipe not created).")
        return out
    for key, sql, tbl in (("decision", ch["decision_sql"], TABLES["decisions"]),
                          ("audit", ch["audit_sql"], TABLES["audit_events"])):
        if tbl not in tables:
            ch[f"{key}_error"] = "table not in mirror yet (initial snapshot)"
            continue
        try:
            res, ms, rr = _timed(client, sql)
            ch[f"{key}_rows"], ch[f"{key}_ms"], ch[f"{key}_read_rows"] = _rows(res), ms, rr
        except Exception as e:
            ch[f"{key}_error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}"
    d = ch["decision_rows"][0] if ch["decision_rows"] else None
    dec_ok = bool(d) and want_v is not None and int(d.get("version") or 0) >= int(want_v)
    aud_ok = len(ch["audit_rows"]) >= want_n
    if config.POSTGRES_MODE != "managed":
        ch.update(status="local fallback",
                  note="This app is on the local Postgres fallback; the ClickPipe replicates Managed Postgres only.")
    elif dec_ok and aud_ok:
        sa = d.get("synced_to_clickhouse_at")  # DateTime64 in UTC, rendered without a zone
        if isinstance(sa, str) and sa and not sa.endswith("Z") and "+" not in sa:
            sa = sa.replace(" ", "T") + "Z"
        ch.update(status="replicated", replication_lag_s=d.get("replication_lag_s"), synced_at=sa)
    else:
        ch.update(status="syncing", mirror_version=(d or {}).get("version"),
                  note="ClickPipes syncs about every 60 s; the mirror catches up shortly.")
    return out
