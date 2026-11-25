"""Offline tests (ClickHouse stopped): real Postgres (local fallback) + real FastAPI/policy, faked CH + MCP.
Run: POSTGRES_MODE=local LANGFUSE_TRACING_ENABLED=false pytest tests/test_offline.py"""
import importlib, os, subprocess, sys, time
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")
# Placeholder console IDs (not a real organization/service) so the deep-link assertions have something to build on.
os.environ.setdefault("CH_CONSOLE_ORG_ID", "00000000-0000-4000-8000-00000000a0a0")
os.environ.setdefault("CH_CONSOLE_SERVICE_ID", "00000000-0000-4000-8000-00000000c1c1")
os.environ["CLICKHOUSE_SECURE"] = "true"  # assertions below expect Cloud-console links


def test_import_without_clickhouse():
    env = {**os.environ, "CLICKHOUSE_HOST": "unreachable.invalid", "POSTGRES_MODE": "local"}
    r = subprocess.run([sys.executable, "-c", "import app.main, app.preflight, app.cdc, app.health; print('ok')"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


@pytest.fixture()
def client_factory():
    from fastapi.testclient import TestClient
    from tests import fakes

    def make(ch_fails=False, mirror_tables=()):
        fakes.install(ch_fails=ch_fails)
        fakes.STATE["mirror_tables"] = set(mirror_tables)
        fakes.STATE["fake_cdc_lag_s"] = None
        import app.main as m
        m._ch = None
        return TestClient(m.app)
    return make


def test_start_demo_ready_then_full_flow_and_restart(client_factory):
    c = client_factory()
    assert c.get("/admin/start-demo").json()["state"] in ("idle", "ready", "not_ready")
    t0 = time.time()
    st = c.post("/admin/start-demo").json()
    assert st["state"] == "ready", st["result"]["first_failure"]
    assert time.time() - t0 < 15
    assert c.get("/admin/start-demo").json()["state"] == "ready"
    a = c.post("/api/cases/A/run").json(); assert a["decision"]["final_disposition"] == "AUTO_APPROVED"
    c.post("/admin/inject-burst")
    b = c.post("/api/cases/B/run").json(); assert b["recommendation"]["prospective_cents"] == 26_600_000
    tid = b["request"]["trade_id"]
    assert c.post(f"/api/trades/{tid}/review", json={"decision": "REJECT", "comment": "x"}).json()["decision"]["final_disposition"] == "HUMAN_REJECTED"
    cc = c.post("/api/cases/C/run").json(); assert cc["recommendation"]["reason_code"] == "ACCOUNT_RESTRICTED"
    # second click = full restart: clean run, no pending decisions
    st2 = c.post("/admin/start-demo").json()
    assert st2["state"] == "ready" and st2["result"]["run_id"] != st["result"]["run_id"]
    assert c.get("/api/trades").json()["trades"] == []


def test_start_demo_not_ready_when_clickhouse_down(client_factory):
    c = client_factory(ch_fails=True)
    st = c.post("/admin/start-demo").json()
    assert st["state"] == "not_ready"
    ff = st["result"]["first_failure"]
    assert "ClickHouse" in ff["hint"] and ff["hint"]
    assert c.get("/health").status_code == 200
    assert c.get("/api/cdc").json()["status"] == "unavailable"


@pytest.mark.parametrize("tables,expected", [((), "not configured"),
                                             (("trade_demo_decisions",), "running"),
                                             (("trade_demo_decisions", "trade_demo_audit_events"), "running")])
def test_cdc_tolerates_missing_or_partial_mirror(client_factory, tables, expected):
    c = client_factory(mirror_tables=tables)
    c.post("/admin/start-demo")
    r = c.get("/api/cdc").json()
    assert r["status"] == expected  # local mode: 'running' + not comparable; never an exception
    if tables:
        assert "ch_mirror" in r


def test_cdc_catching_up_in_managed_mode(client_factory, monkeypatch):
    from app import config, cdc
    c = client_factory(mirror_tables=("trade_demo_decisions",))
    c.post("/admin/start-demo"); c.post("/api/cases/A/run")
    monkeypatch.setattr(config, "POSTGRES_MODE", "managed")
    r = cdc.reconcile(config.ch_client())
    assert r["status"] == "catching up" and r["ch_mirror"]["audit_events"] is None


def test_ui_and_readonly_detail_endpoints(client_factory):
    c = client_factory()
    html = c.get("/").text
    assert "Start demo" in html and "Scripted controls" not in html and "Audit trail" in html
    # Top tabs start as "Scenario N" and reveal the real title only after click;
    # Start demo clears the revealed set. ClickPipes link matches the Postgres wording.
    assert "revealed=new Set()" in html and "revealed.add(id)" in html
    assert "'Scenario '+s.n" in html
    assert "Show the row in Postgres (SQL + result)" in html
    assert "Show the row in ClickHouse (SQL + result)" in html
    assert "Mirror query + analytics SQL" not in html
    ln = c.get("/api/links").json()
    assert ln["service_id"] and ln["new_query"].endswith("/console/newQuery") and ln["tables"]["rollup"]
    c.post("/admin/start-demo")
    a = c.post("/api/cases/A/run").json()
    tid = a["request"]["trade_id"]
    rb = c.get(f"/api/trades/{tid}/pg-readback").json()
    assert rb["rows"][0]["final_disposition"] == "AUTO_APPROVED" and "trade_demo.decisions" in rb["sql"]
    assert c.get(f"/api/trades/{tid}/cdc-readback").json()["status"] == "not configured"
    ins = c.get("/api/insight-sql/1055").json()
    assert "account_activity_1m" in ins["mv_sql"] and "1055" in ins["raw_sql"]


def test_clickpipe_link_deep_or_list(monkeypatch):
    from app import links
    monkeypatch.setenv("CLICKPIPE_ID", "11111111-2222-4333-8444-555555555555")
    ln = links.console_links()
    assert ln["clickpipes"].endswith("/imports/11111111-2222-4333-8444-555555555555/details?tabName=tab_metrics")
    monkeypatch.delenv("CLICKPIPE_ID")
    assert links.console_links()["clickpipes"].endswith("/imports")


def test_console_links_utf8_safe_and_pg_uuid(client_factory, monkeypatch):
    """Console deep-links must prefill /console/newQuery with UTF-8-safe base64,
    including ClickPipe point-row lookups (database=trade_demo_cdc) and SQL that
    contains non-ASCII comment characters. PG_CONSOLE_ID skips the pg… redirect."""
    import base64, json, re, subprocess
    from urllib.parse import quote, unquote
    html = client_factory().get("/").text
    assert "revealed=new Set()" in html and "revealed.add(id)" in html
    assert "'Scenario '+s.n" in html
    assert "Show the row in ClickHouse (SQL + result)" in html
    assert "Show the row in Postgres (SQL + result)" in html
    assert "function b64(sql)" in html and "new TextEncoder()" in html
    assert "btoa(sql)" not in html
    assert "encodeURIComponent(b64(sql))" in html
    assert "nq(c.trade_sql,'trade_demo_cdc')" in html
    assert "nq(c.decision_sql,'trade_demo_cdc')" in html
    assert "Open in ClickHouse console" in html and "Open in Postgres console" in html
    assert "Copy SQL" in html and "openAndCopy(" in html and "function consoleLinks(" in html

    m = re.search(r"function b64\(sql\)\{[^}]+\}", html)
    assert m, "b64 helper not found in index.html"
    sample = "SELECT trade_id FROM trade_demo_cdc.trade_demo_decisions FINAL\nWHERE trade_id = 'abc' -- lag — ms"
    js = m.group(0) + """
const samples = JSON.parse(process.argv[1]);
const out = samples.map((sql) => {
  const query = encodeURIComponent(b64(sql));
  const url = 'https://console.clickhouse.cloud/services/sid/console/newQuery?query=' + query + '&database=' + encodeURIComponent('trade_demo_cdc');
  return {query, url, u: Buffer.from(sql, 'utf8').toString('base64')};
});
process.stdout.write(JSON.stringify(out));
"""
    samples = [
        "SELECT 1",
        sample,
        "SELECT d.trade_id, d.final_disposition FROM trade_demo.decisions d\nWHERE d.trade_id = 'cd8f984a-3292-4241-adcc-29d8863f650d';",
    ]
    import shutil
    if not shutil.which("node"):
        pytest.skip("Node.js not installed: the browser base64 helper check needs `node` (optional dev dependency)")
    r = subprocess.run(["node", "-e", js, json.dumps(samples)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    for sql, row in zip(samples, got):
        expected = base64.b64encode(sql.encode("utf-8")).decode("ascii")
        assert row["u"] == expected
        assert row["query"] == quote(expected, safe="")
        decoded = base64.b64decode(unquote(row["query"])).decode("utf-8")
        assert decoded == sql
        assert "/console/newQuery?query=" in row["url"] and "database=trade_demo_cdc" in row["url"]
    assert "—" in samples[1] and base64.b64decode(unquote(got[1]["query"])).decode("utf-8") == samples[1]

    from app import links
    monkeypatch.setenv("PG_CONSOLE_ID", "aaaaaaaa-bbbb-8ccc-8ddd-eeeeeeeeeeee")
    ln = links.console_links()
    assert ln["new_query"].endswith("/console/newQuery")
    assert ln["postgres"]["new_query"] == (
        "https://console.clickhouse.cloud/postgres/aaaaaaaa-bbbb-8ccc-8ddd-eeeeeeeeeeee/console/newQuery"
    )
    assert "pgjexns" not in ln["postgres"]["new_query"]
    tid = "cd8f984a-3292-4241-adcc-29d8863f650d"
    point = links.CDC_TRADE_SQL.format(tid=tid)
    assert "FROM trade_demo_cdc.trade_demo_decisions FINAL" in point and f"trade_id = '{tid}'" in point


def test_migrate_skips_when_current_and_retries_deadlock(monkeypatch):
    import psycopg
    from app import postgres_store as pg
    pg.migrate()                      # applies (or already current) and records the marker
    assert pg.migrate() is False      # second call: no DDL, no ACCESS EXCLUSIVE locks
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise psycopg.errors.DeadlockDetected("deadlock detected")
        return "ok"
    assert pg.with_retry(flaky, backoff=0.01) == "ok" and calls["n"] == 3


def test_deck_route_and_public_url(client_factory, tmp_path, monkeypatch):
    """ /deck is 404 when the file is missing, 200 HTML when present; PUBLIC_URL is in /api/links.
    The UI has a hidden Deck link (no hard-coded localhost) that /api/links.deck unhides. """
    from app import config
    monkeypatch.setattr(config, "DECK_PATH", tmp_path / "missing.html")
    monkeypatch.setattr(config, "PUBLIC_URL", "https://demo.example.ngrok-free.app")
    c = client_factory()
    html = c.get("/").text
    assert "Start demo" in html and 'id="decklink"' in html and 'href="/deck"' in html
    assert "127.0.0.1" not in html and "localhost" not in html
    assert c.get("/deck").status_code == 404
    ln = c.get("/api/links").json()
    assert ln["public_url"] == "https://demo.example.ngrok-free.app"
    assert ln["deck"] is None

    deck = tmp_path / "deck.html"
    deck.write_text("<!doctype html><title>deck fixture</title><h1>sample deck</h1>", encoding="utf-8")
    monkeypatch.setattr(config, "DECK_PATH", deck)
    r = c.get("/deck")
    assert r.status_code == 200 and "sample deck" in r.text
    assert "text/html" in r.headers.get("content-type", "")
    assert c.get("/api/links").json()["deck"] == "/deck"
