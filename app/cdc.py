"""ClickPipes Postgres CDC mirror checks (trade_demo_cdc, ReplacingMergeTree + _peerdb_* columns).
Current state = FINAL + _peerdb_is_deleted = 0. Replication is ~60 s behind.
Tolerant by design: a missing, half-created or freshly recreated mirror yields a status, never an exception."""
import os
from datetime import timezone
from app import config, postgres_store as pg

DB = "trade_demo_cdc"
TABLES = {"decisions": "trade_demo_decisions", "audit_events": "trade_demo_audit_events"}
NEWEST_SQL = """SELECT toString(trade_id), final_disposition, version, updated_at, _peerdb_synced_at,
       dateDiff('millisecond', updated_at, _peerdb_synced_at) / 1000 AS lag_s
FROM trade_demo_cdc.trade_demo_decisions FINAL
WHERE _peerdb_is_deleted = 0
ORDER BY updated_at DESC LIMIT 1"""
NOTE = "ClickPipes syncs about every 60 s, so the mirror may lag Postgres (catch-up is normal)."


def _iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds") if dt and dt.year > 1970 else None


def mirror_tables(ch) -> set:
    rows = ch.query("SELECT name FROM system.tables WHERE database = %(db)s", parameters={"db": DB}).result_rows
    return {r[0] for r in rows}


def mirror_exists(ch) -> bool:
    try:
        return bool(mirror_tables(ch))
    except Exception:
        return False


def reconcile(ch) -> dict:
    try:
        tables = mirror_tables(ch)
    except Exception as e:
        return {"status": "unavailable", "note": f"ClickHouse not reachable ({type(e).__name__})"}
    if not tables:
        if (os.getenv("CLICKPIPE_ID") or "").strip():
            return {"status": "not configured",
                    "note": "ClickPipe created; waiting for its initial load into trade_demo_cdc (about 1-2 min)."}
        return {"status": "not configured", "note": "No trade_demo_cdc mirror yet (ClickPipe not created)."}
    ch_counts, last = {}, None
    for key, t in TABLES.items():
        if t not in tables:
            ch_counts[key] = None
            continue
        try:
            ch_counts[key] = int(ch.query(f"SELECT count() FROM {DB}.{t} FINAL WHERE _peerdb_is_deleted = 0").result_rows[0][0])
            if key == "audit_events":
                last = ch.query(f"SELECT max(_peerdb_synced_at) FROM {DB}.{t}").result_rows[0][0]
        except Exception:
            ch_counts[key] = None  # table mid-creation / initial snapshot
    try:
        with pg.conn() as c:
            p = c.execute("""SELECT (SELECT count(*) FROM trade_demo.decisions) AS d,
                                    (SELECT count(*) FROM trade_demo.audit_events) AS a""").fetchone()
        pg_counts = {"decisions": p["d"], "audit_events": p["a"]}
    except Exception as e:
        pg_counts = None
    out = {"mirror_database": DB, "pg": pg_counts, "ch_mirror": ch_counts,
           "last_synced_at_utc": _iso(last), "postgres_mode": config.POSTGRES_MODE, "note": NOTE}
    if config.POSTGRES_MODE != "managed":
        out.update(status="running", in_sync=None,
                   note="Mirror replicates Managed Postgres; this app is on the local fallback, so counts are not comparable.")
    elif pg_counts is None:
        out.update(status="running", in_sync=None, note="Postgres not reachable; cannot compare.")
    else:
        in_sync = pg_counts == ch_counts
        out.update(status="in sync" if in_sync else "catching up", in_sync=in_sync)
    try:
        if TABLES["decisions"] in tables:
            newest = ch.query(NEWEST_SQL).result_rows
            if newest:
                tid, disp, ver, upd, synced, lag = newest[0]
                out["newest_replicated_decision"] = {"trade_id": tid, "final_disposition": disp, "version": ver,
                                                     "pg_updated_at_utc": _iso(upd), "synced_at_utc": _iso(synced),
                                                     "observed_lag_s": round(float(lag), 1)}
    except Exception:
        pass
    return out


def trade_replication(ch, trade_id, pg_version) -> dict:
    """Has this decision version reached the mirror? Only claim 'replicated' when a query sees it."""
    try:
        if TABLES["decisions"] not in mirror_tables(ch):
            return {"status": "not configured"}
        if config.POSTGRES_MODE != "managed":
            return {"status": "n/a (local Postgres fallback is not replicated)"}
        rows = ch.query("""SELECT final_disposition, version, _peerdb_synced_at FROM trade_demo_cdc.trade_demo_decisions FINAL
                           WHERE trade_id = %(t)s AND _peerdb_is_deleted = 0""", parameters={"t": str(trade_id)}).result_rows
    except Exception as e:
        return {"status": "unavailable", "error": type(e).__name__}
    if rows and rows[0][1] >= pg_version:
        return {"status": "replicated", "final_disposition": rows[0][0], "version": rows[0][1], "synced_at_utc": _iso(rows[0][2])}
    return {"status": "pending replication", "mirror_version": rows[0][1] if rows else None}
