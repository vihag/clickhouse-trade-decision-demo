"""ClickHouse access. CONTEXT_SQL is the single analytical truth used by the MCP tool
and saved in sql/demo_queries.sql for the SQL Editor."""
import json, time, uuid
from datetime import datetime, timedelta, timezone
from app.config import ch_client, FIXTURES

CONTEXT_SQL = """SELECT
    countMerge(trade_count)    AS recent_trade_count,
    sumMerge(notional_cents)   AS recent_notional_cents,
    max(minute_bucket)         AS latest_activity_minute_utc
FROM trade_demo.account_activity_1m
WHERE account_id = {account_id:UInt32}
  AND minute_bucket >= toStartOfMinute(now()) - INTERVAL 14 MINUTE"""


# Reset-path reads only: on ClickHouse Cloud (SharedMergeTree, several replicas behind one endpoint) a read can land
# on a replica that has not yet caught up with the DELETE just issued, and see the previous run's rows (seen once:
# "expected 12000000, saw 19600000" on Start demo). select_sequential_consistency makes those reads current.
CONSISTENT = {"select_sequential_consistency": 1}


def recent_activity(client, account_id: int, settings: dict | None = None) -> dict:
    t0 = time.perf_counter()
    res = client.query(CONTEXT_SQL, parameters={"account_id": int(account_id)}, settings=settings)
    cnt, total, latest = res.result_rows[0]
    s = res.summary or {}
    return {
        "account_id": int(account_id),
        "window": "recent 15-minute buckets (minute-bucketed)",
        "recent_trade_count": int(cnt),
        "recent_notional_cents": int(total),
        "latest_activity_minute_utc": latest.replace(tzinfo=timezone.utc).isoformat() if cnt else None,
        "query_observed_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "query_ms": round((time.perf_counter() - t0) * 1000, 1),
        "read_rows": int(s.get("read_rows", 0) or 0),
        "source": "ClickHouse trade_demo.account_activity_1m (AggregatingMergeTree via incremental MV)",
        "status": "ok" if cnt else "no_recent_activity",
    }


def _fixture():
    return json.loads(FIXTURES.read_text())


def _insert_events(client, account_id: int, cents: list[int], ts: list[datetime], source: str, symbol="DEMO"):
    rows = [[uuid.uuid4(), t, account_id, symbol, c, source] for c, t in zip(cents, ts)]
    client.insert("trade_demo.activity_events", rows,
                  column_names=["event_id", "event_ts", "account_id", "symbol", "notional_cents", "source"],
                  settings={"async_insert": 0})


def wait_for_total(client, account_id: int, expected_cents: int, timeout=20.0) -> dict:
    """Poll the MV target (not a sleep-and-hope) until the expected total is queryable."""
    t0 = time.perf_counter()
    while True:
        ctx = recent_activity(client, account_id, settings=CONSISTENT)
        if ctx["recent_notional_cents"] == expected_cents:
            ctx["visible_after_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            return ctx
        if time.perf_counter() - t0 > timeout:
            raise TimeoutError(f"account {account_id}: expected {expected_cents}, saw {ctx['recent_notional_cents']}")
        time.sleep(0.3)


def reset_demo_accounts(client) -> dict:
    """Delete the demo accounts' rows, insert fresh baselines, wait until the MV target shows them.
    Retried once from scratch if a stale read/delete race leaves the totals off."""
    try:
        return _reset_demo_accounts(client)
    except TimeoutError:
        return _reset_demo_accounts(client)


def _wait_deleted(client, accts: str, timeout=10.0):
    """Poll (consistent reads) until no rows remain for the demo accounts in raw table and MV target."""
    t0 = time.perf_counter()
    while True:
        raw, agg = client.query(f"""SELECT (SELECT count() FROM trade_demo.activity_events WHERE account_id IN ({accts})),
                                          (SELECT count() FROM trade_demo.account_activity_1m WHERE account_id IN ({accts}))""",
                                settings=CONSISTENT).result_rows[0]
        if raw == 0 and agg == 0:
            return
        if time.perf_counter() - t0 > timeout:
            raise TimeoutError(f"demo-account rows still visible after DELETE (raw={raw}, mv={agg})")
        time.sleep(0.3)


def _reset_demo_accounts(client) -> dict:
    fx = _fixture()
    accts = ",".join(str(a) for a in fx["demo_accounts"])
    s = {"mutations_sync": 2}
    client.command(f"DELETE FROM trade_demo.activity_events WHERE account_id IN ({accts})", settings={"lightweight_deletes_sync": 2})
    client.command(f"DELETE FROM trade_demo.account_activity_1m WHERE account_id IN ({accts})", settings={"lightweight_deletes_sync": 2})
    _wait_deleted(client, accts)
    now = datetime.now(timezone.utc)
    out = {}
    for b in fx["baselines"]:
        n = len(b["events_cents"])
        # spread between ~3 min and ~30 s ago, so the 15-bucket window holds them for ~11 min
        ts = [now - timedelta(seconds=180 - i * (150 // max(n - 1, 1))) for i in range(n)]
        _insert_events(client, b["account_id"], b["events_cents"], ts, "demo_fixture_baseline")
        out[b["account_id"]] = wait_for_total(client, b["account_id"], b["total_cents"])
    out["baseline_valid_until_utc"] = (now - timedelta(seconds=180) + timedelta(minutes=14)).replace(second=0, microsecond=0).isoformat()
    return out


def inject_burst(client) -> dict:
    fx = _fixture()
    b = fx["burst"]
    before = recent_activity(client, b["account_id"])["recent_notional_cents"]
    now = datetime.now(timezone.utc)
    ts = [now - timedelta(milliseconds=200 * i) for i in range(len(b["events_cents"]))]
    t0 = time.perf_counter()
    _insert_events(client, b["account_id"], b["events_cents"], ts, "demo_fixture_burst")
    insert_ack_ms = round((time.perf_counter() - t0) * 1000, 1)
    ctx = wait_for_total(client, b["account_id"], before + b["total_cents"])
    ctx.update({"before_cents": before, "burst_cents": b["total_cents"], "insert_ack_ms": insert_ack_ms,
                "inserted_at_utc": now.isoformat(timespec="milliseconds")})
    return ctx
