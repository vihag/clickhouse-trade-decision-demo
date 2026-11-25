"""Offline tests for the Approve/Reject audit-trail pop-up and app observability (OpenTelemetry -> ClickStack).
Same harness as test_offline.py: real local Postgres + FastAPI + policy, faked ClickHouse/MCP (tests/fakes.py)."""
import os, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")
# Placeholder console IDs (not a real organization/service) so the deep-link assertions have something to build on.
os.environ.setdefault("CH_CONSOLE_ORG_ID", "00000000-0000-4000-8000-00000000a0a0")
os.environ.setdefault("CH_CONSOLE_SERVICE_ID", "00000000-0000-4000-8000-00000000c1c1")
os.environ["CLICKHOUSE_SECURE"] = "true"  # assertions below expect Cloud-console links


@pytest.fixture()
def client_factory(monkeypatch):
    from fastapi.testclient import TestClient
    from tests import fakes

    def make(ch_fails=False, mirror_tables=(), cdc_lag=None):
        fakes.install(ch_fails=ch_fails)
        fakes.STATE["mirror_tables"] = set(mirror_tables)
        fakes.STATE["fake_cdc_lag_s"] = cdc_lag
        import app.main as m
        m._ch = None
        return TestClient(m.app)
    yield make
    from tests import fakes
    fakes.STATE["fake_cdc_lag_s"] = None


def _reviewed_b(c, decision="APPROVE"):
    assert c.post("/admin/start-demo").json()["state"] == "ready"
    c.post("/admin/inject-burst")
    tid = c.post("/api/cases/B/run").json()["request"]["trade_id"]
    r = c.post(f"/api/trades/{tid}/review", json={"decision": decision, "comment": "ok"}).json()
    assert r["decision"]["final_disposition"] == ("HUMAN_APPROVED" if decision == "APPROVE" else "HUMAN_REJECTED")
    return tid


# ---------------- Feature B: audit trail ----------------
def test_audit_trail_postgres_part_and_mirror_not_configured(client_factory):
    c = client_factory()
    tid = _reviewed_b(c, "APPROVE")
    x = c.get(f"/api/trades/{tid}/audit-trail").json()
    p, ch = x["postgres"], x["clickhouse"]
    assert p["rows"][0]["final_disposition"] == "HUMAN_APPROVED" and p["rows"][0]["version"] == 2
    assert [e["event_type"] for e in p["audit_rows"]] == ["REQUESTED", "RECOMMENDED", "REVIEW_REQUESTED", "HUMAN_APPROVED"]
    assert tid in p["sql"] and "trade_demo.decisions" in p["sql"] and tid in p["audit_sql"]
    assert ch["status"] == "not configured" and ch["want_version"] == 2 and ch["want_audit_events"] == 4
    for q in (ch["decision_sql"], ch["audit_sql"]):  # the exact, read-only ClickHouse queries shown in the pop-up
        assert "FINAL" in q and "trade_demo_cdc." in q and tid in q and "_peerdb_is_deleted = 0" in q
        assert q.lstrip().upper().startswith("SELECT")
    assert x["langfuse"]["score"] == "human_approved = 1"


def test_audit_trail_syncing_then_replicated(client_factory, monkeypatch):
    from app import config
    c = client_factory(mirror_tables=("trade_demo_decisions", "trade_demo_audit_events"), cdc_lag=1.5)
    tid = _reviewed_b(c, "REJECT")
    monkeypatch.setattr(config, "POSTGRES_MODE", "managed")  # status logic only (data is local PG + fake mirror)
    first = c.get(f"/api/trades/{tid}/audit-trail").json()["clickhouse"]
    assert first["status"] == "syncing" and first["mirror_version"] in (None, 1)
    time.sleep(1.7)
    x = c.get(f"/api/trades/{tid}/audit-trail").json()
    ch = x["clickhouse"]
    assert ch["status"] == "replicated", ch
    assert ch["decision_rows"][0]["final_disposition"] == "HUMAN_REJECTED" and ch["decision_rows"][0]["version"] == 2
    assert len(ch["audit_rows"]) == 4 and ch["replication_lag_s"] == 1.5 and ch["synced_at"].endswith("Z")
    assert x["langfuse"]["score"] == "human_approved = 0"


def test_audit_trail_local_fallback_and_clickhouse_down(client_factory):
    c = client_factory(mirror_tables=("trade_demo_decisions", "trade_demo_audit_events"), cdc_lag=0)
    tid = _reviewed_b(c)
    assert c.get(f"/api/trades/{tid}/audit-trail").json()["clickhouse"]["status"] == "local fallback"
    c2 = client_factory(ch_fails=True)
    x = c2.get(f"/api/trades/{tid}/audit-trail").json()
    assert x["clickhouse"]["status"] == "unavailable" and x["postgres"]["rows"]  # Postgres part still shown


def test_audit_trail_unknown_or_bad_trade(client_factory):
    c = client_factory()
    c.post("/admin/start-demo")
    assert c.get("/api/trades/00000000-0000-0000-0000-000000000000/audit-trail").status_code == 404
    assert c.get("/api/trades/not-a-uuid/audit-trail").status_code == 422


def test_ui_has_audit_trail_and_observability_hooks(client_factory):
    html = client_factory().get("/").text
    for s in ("function auditTrail(", "auditTrail(dcs)", "/audit-trail", "Open in observability", "observability:'Observability'"):
        assert s in html, s


# ---------------- Feature A: observability ----------------
def test_observability_is_noop_when_unconfigured(client_factory, monkeypatch):
    from app import observability as obs
    monkeypatch.delenv("CLICKSTACK_OTLP_ENDPOINT", raising=False)
    assert not obs.configured() and not obs.enabled() and obs.setup() is False
    c = client_factory()
    assert c.get("/health").json()["observability"]["status"] == "off"
    o = c.get("/api/links").json()["observability"]
    assert o["configured"] is False and "hyperdx" in o["ui"]
    assert c.post("/admin/start-demo").json()["state"] == "ready"


def test_observability_spans_metrics_logs_in_memory(client_factory):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    try:
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter as InMemoryLogExporter
    except ImportError:
        from opentelemetry.sdk._logs.export import InMemoryLogExporter
    from app import observability as obs
    spans, reader, logs = InMemorySpanExporter(), InMemoryMetricReader(), InMemoryLogExporter()
    assert obs.setup(span_exporter=spans, metric_reader=reader, log_exporter=logs)
    try:
        c = client_factory()
        st = c.post("/admin/start-demo").json()
        rid = st["result"]["run_id"]
        tid = c.post("/api/cases/A/run").json()["request"]["trade_id"]
        c.get(f"/api/trades/{tid}/audit-trail")
        names = [s.name for s in spans.get_finished_spans()]
        assert "POST /admin/start-demo" in names and "POST /api/cases/{case}/run" in names
        assert "GET /api/trades/{trade_id}/audit-trail" in names
        run_span = next(s for s in spans.get_finished_spans() if s.name == "POST /api/cases/{case}/run")
        assert run_span.attributes["demo.run_id"] == rid and run_span.attributes["trade.id"] == tid
        assert run_span.attributes["http.response.status_code"] == 200
        assert run_span.resource.attributes["service.name"] == obs.SERVICE_NAME
        metrics = {m.name: m for rm in reader.get_metrics_data().resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
        assert {"trade_demo.http.server.duration", "trade_demo.decisions", "trade_demo.clickhouse.query.duration",
                "trade_demo.start_demo"} <= set(metrics)
        dp = metrics["trade_demo.decisions"].data.data_points
        assert any(p.attributes.get("disposition") == "AUTO_APPROVED" and p.value == 1 for p in dp)
        bodies = [l.log_record.body for l in logs.get_finished_logs()]
        assert any("decision AUTO_APPROVED" in str(b) for b in bodies) and any("start demo: READY" in str(b) for b in bodies)
        dlog = next(l.log_record for l in logs.get_finished_logs() if "decision AUTO_APPROVED" in str(l.log_record.body))
        assert dlog.attributes["trade.id"] == tid and dlog.attributes["demo.run_id"] == rid
        assert format(dlog.trace_id, "032x") == format(run_span.context.trace_id, "032x")  # log <-> trace correlation
        lk = obs.links(rid)
        assert "demo.run_id" in lk["logs_run"] and rid in lk["logs_run"] and "services" in lk["service"]
    finally:
        obs.shutdown()
    assert not obs.enabled()


class _Sink(BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        self.rfile.read(n)
        _Sink.got.append((self.path, self.headers.get("authorization"), self.headers.get("content-type"), n))
        self.send_response(200); self.send_header("content-type", "application/x-protobuf"); self.end_headers()

    def log_message(self, *a):
        pass


def test_observability_real_otlp_http_export_to_local_sink(client_factory, monkeypatch):
    """The real OTLP/HTTP exporters (as used against ClickHouse Observability) post protobuf to /v1/{traces,logs,metrics}
    with 'authorization: Bearer <key>'. The key never appears in API responses."""
    from app import observability as obs
    srv = HTTPServer(("127.0.0.1", 0), _Sink)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Sink.got = []
    monkeypatch.setenv("CLICKSTACK_OTLP_ENDPOINT", f"http://127.0.0.1:{srv.server_port}/")
    monkeypatch.setenv("CLICKSTACK_API_KEY", "test-ingest-key-123")
    try:
        assert obs.setup()
        c = client_factory()
        c.post("/admin/start-demo"); c.post("/api/cases/A/run")
        obs.flush()
        paths = {g[0] for g in _Sink.got}
        assert {"/v1/traces", "/v1/logs", "/v1/metrics"} <= paths, _Sink.got
        assert all(g[1] == "Bearer test-ingest-key-123" and g[2] == "application/x-protobuf" for g in _Sink.got)
        h, ln = c.get("/health").text, c.get("/api/links").text
        assert "test-ingest-key-123" not in h and "test-ingest-key-123" not in ln
        assert c.get("/health").json()["observability"]["status"] == "running"
        assert c.get("/api/links").json()["observability"]["configured"] is True
    finally:
        obs.shutdown(); srv.shutdown()
