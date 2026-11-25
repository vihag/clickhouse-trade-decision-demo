"""TEST-ONLY in-memory stand-ins for ClickHouse and the MCP call, used when the ClickHouse service is
stopped. Never imported by the app. Postgres, FastAPI, the policy and the UI run for real."""
import json, re, time
from datetime import datetime, timedelta, timezone
from app import config, analytics, mcp_client

FX = json.loads(config.FIXTURES.read_text())
STATE = {"totals": {}, "rows": 5_000_000, "mirror": False, "mirror_tables": set(), "fake_cdc_lag_s": None}
_utc = lambda t: t.astimezone(timezone.utc).replace(tzinfo=None)  # CH DateTime64 values are naive UTC
_TID = re.compile(r"trade_id = '([0-9a-f-]{36})'")


def _fake_cdc(s, parameters):
    """Simulated ClickPipes mirror for the audit-trail point lookups: serves the local Postgres rows once
    they are older than STATE['fake_cdc_lag_s'] (before that, the previous decision version), like CDC lag."""
    from app import postgres_store as pg
    m = _TID.search(s)
    tid = (parameters or {}).get("t") or (m.group(1) if m else None)
    lag = STATE["fake_cdc_lag_s"]
    now = datetime.now(timezone.utc)
    with pg.conn() as c:
        if "trade_demo_audit_events" in s:
            rows = c.execute("SELECT event_id, event_type, actor, created_at FROM trade_demo.audit_events "
                             "WHERE trade_id=%s ORDER BY event_id", (tid,)).fetchall()
            vis = [r for r in rows if (now - r["created_at"]).total_seconds() >= lag]
            return _Res([(r["event_id"], r["event_type"], r["actor"], _utc(r["created_at"]),
                          _utc(r["created_at"] + timedelta(seconds=lag)), float(lag)) for r in vis],
                        ["event_id", "event_type", "actor", "pg_created_at", "synced_to_clickhouse_at", "replication_lag_s"])
        d = c.execute("SELECT * FROM trade_demo.decisions WHERE trade_id=%s", (tid,)).fetchone()
    if not d:
        return _Res([])
    disp, ver, rev = d["final_disposition"], d["version"], d["reviewer"]
    if (now - d["updated_at"]).total_seconds() < lag:
        if ver <= 1:
            return _Res([])
        disp, ver, rev = "PENDING_REVIEW", ver - 1, None
    synced = _utc(d["updated_at"] + timedelta(seconds=lag))
    if parameters:  # cdc.trade_replication: (final_disposition, version, _peerdb_synced_at)
        return _Res([(disp, ver, synced)])
    return _Res([(tid, disp, ver, rev, _utc(d["updated_at"]), synced, float(lag))],
                ["trade_id", "final_disposition", "version", "reviewer", "pg_updated_at", "synced_to_clickhouse_at",
                 "replication_lag_s"])


class _Res:
    def __init__(self, rows, cols=None):
        self.result_rows, self.column_names, self.summary = rows, cols or [], {"read_rows": 3}


class FakeCH:
    def query(self, sql, parameters=None, **_):
        s = " ".join(sql.split())
        if "version()" in s:
            return _Res([("fake-clickhouse",)])
        if "count() FROM trade_demo.activity_events" in s:
            return _Res([(STATE["rows"],)])
        if "system.tables" in s:
            return _Res([(t,) for t in sorted(STATE["mirror_tables"])])
        if STATE["fake_cdc_lag_s"] is not None and "FINAL WHERE trade_id" in s and "trade_demo_cdc.trade_demo_" in s:
            return _fake_cdc(s, parameters)
        if "trade_demo_cdc" in s:
            if STATE["fake_cdc_lag_s"] is not None and ("GROUP BY reason" in s or "approval_rate_pct" in s):
                return _Res([])  # product analytics over the fake mirror: not simulated
            if "max(_peerdb_synced_at)" in s:
                return _Res([(datetime(1970, 1, 1),)])
            if "ORDER BY updated_at DESC" in s:
                return _Res([])
            return _Res([(0,)])
        raise AssertionError(f"FakeCH: unexpected query {s[:80]}")

    def command(self, *a, **k):
        return None


def _ctx(acct):
    total = STATE["totals"].get(acct, 0)
    return {"account_id": acct, "window": "recent 15-minute buckets (minute-bucketed)",
            "recent_trade_count": 1 if total else 0, "recent_notional_cents": total,
            "latest_activity_minute_utc": datetime.now(timezone.utc).isoformat() if total else None,
            "query_observed_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "query_ms": 1.0, "read_rows": 3, "source": "FAKE ClickHouse (test)", "status": "ok" if total else "no_recent_activity"}


def install(ch_fails=False):
    def ch_client(readonly=False):
        if ch_fails:
            raise ConnectionError("fake: ClickHouse service stopped")
        return FakeCH()
    config.ch_client = ch_client
    analytics.recent_activity = lambda client, acct, settings=None: _ctx(int(acct))

    def reset_demo_accounts(client):
        time.sleep(0.2)
        STATE["totals"] = {b["account_id"]: b["total_cents"] for b in FX["baselines"]}
        out = {b["account_id"]: {**_ctx(b["account_id"]), "visible_after_ms": 1.0} for b in FX["baselines"]}
        out["baseline_valid_until_utc"] = (datetime.now(timezone.utc) + timedelta(minutes=11)).replace(second=0, microsecond=0).isoformat()
        return out
    analytics.reset_demo_accounts = reset_demo_accounts

    def inject_burst(client):
        b = FX["burst"]; before = STATE["totals"].get(b["account_id"], 0)
        STATE["totals"][b["account_id"]] = before + b["total_cents"]
        return {**_ctx(b["account_id"]), "before_cents": before, "burst_cents": b["total_cents"],
                "insert_ack_ms": 1.0, "visible_after_ms": 1.0, "inserted_at_utc": datetime.now(timezone.utc).isoformat()}
    analytics.inject_burst = inject_burst

    async def call_get_recent_activity(acct):
        if ch_fails:
            return {"account_id": acct, "status": "error", "error": "fake: ClickHouse stopped"}
        return {**_ctx(int(acct)), "clickhouse_user": config.CH_RO_USER, "transport": "MCP stdio (FAKE in tests)",
                "mcp_server": "fake"}
    mcp_client.call_get_recent_activity = call_get_recent_activity
