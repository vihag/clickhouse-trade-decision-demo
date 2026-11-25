"""End-to-end scripted run against the REAL services (ClickHouse Cloud, Postgres, MCP stdio, Langfuse).
Order: reset -> A -> burst -> B (review) -> reject -> duplicate reject fails -> C -> Langfuse verification."""
import json, os, sys, time
from pathlib import Path
import httpx, pytest
if os.getenv("DEMO_LIVE_TESTS") != "1":
    pytest.skip("live end-to-end test: run via `make test` (needs ClickHouse, Postgres, Langfuse up)", allow_module_level=True)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fastapi.testclient import TestClient
from app.main import app

c = TestClient(app)
STATE = {}
EVID = Path(__file__).resolve().parent.parent / "evidence"


def test_reset():
    r = c.post("/admin/reset-demo").json()
    assert r["baselines"]["1042"]["recent_notional_cents"] == 12_000_000
    assert r["baselines"]["1055"]["recent_notional_cents"] == 1_400_000
    STATE["run_id"] = r["run_id"]; STATE["reset_s"] = r["elapsed_s"]


def test_case_a_auto_approves():
    d = c.post("/api/cases/A/run").json()
    ctx = d["recommendation"]["analytics_context"]
    assert ctx["transport"] == "MCP stdio" and ctx["clickhouse_user"] == "demo_ro"
    assert ctx["recent_notional_cents"] == 1_400_000
    assert d["recommendation"]["recommendation"] == "AUTO_APPROVE"
    assert d["decision"]["final_disposition"] == "AUTO_APPROVED" and d["decision"]["reviewer"] is None
    assert [e["event_type"] for e in d["audit"]] == ["REQUESTED", "RECOMMENDED", "AUTO_APPROVED"]
    again = c.post("/api/cases/A/run").json()  # idempotent
    assert again["idempotent_replay"] and len(again["audit"]) == 3
    STATE["A"] = d


def test_burst_then_case_b_review_and_reject():
    b = c.post("/admin/inject-burst").json()
    assert b["before_cents"] == 12_000_000 and b["recent_notional_cents"] == 19_600_000
    STATE["burst"] = b
    d = c.post("/api/cases/B/run").json()
    r = d["recommendation"]
    assert r["prior_notional_cents"] == 19_600_000 and r["prospective_cents"] == 26_600_000
    assert r["recommendation"] == "REVIEW" and r["reason_code"] == "ACTIVITY_THRESHOLD_EXCEEDED"
    assert d["decision"]["final_disposition"] == "PENDING_REVIEW"
    tid = d["request"]["trade_id"]
    rej = c.post(f"/api/trades/{tid}/review", json={"decision": "REJECT", "comment": "Burst pushes acct past limit"})
    assert rej.status_code == 200
    rj = rej.json()
    assert rj["decision"]["final_disposition"] == "HUMAN_REJECTED" and rj["decision"]["version"] == 2
    assert rj["langfuse_enriched"]
    assert [e["event_type"] for e in rj["audit"]] == ["REQUESTED", "RECOMMENDED", "REVIEW_REQUESTED", "HUMAN_REJECTED"]
    dup = c.post(f"/api/trades/{tid}/review", json={"decision": "APPROVE", "comment": "double click"})
    assert dup.status_code == 409
    assert len(c.get(f"/api/trades/{tid}").json()["audit"]) == 4
    STATE["B"] = rj


def test_case_c_restricted_reviews():
    d = c.post("/api/cases/C/run").json()
    assert d["recommendation"]["reason_code"] == "ACCOUNT_RESTRICTED"
    assert d["decision"]["final_disposition"] == "PENDING_REVIEW"
    STATE["C"] = d


def test_langfuse_traces_and_score():
    from app import config
    if not config.LANGFUSE_ENABLED:
        pytest.skip("Langfuse not configured in .env")
    host = os.environ["LANGFUSE_HOST"].rstrip("/")
    auth = (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    found = {}
    deadline = time.time() + 120
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    win = {"fromStartTime": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "toStartTime": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    for k in ("A", "B", "C"):
        tid = STATE[k]["decision"]["trace_id"]
        while True:  # Observations API v2 (v3 /traces endpoints are removed on Langfuse Cloud)
            obs = httpx.get(f"{host}/api/public/v2/observations", auth=auth, timeout=20,
                            params={"traceId": tid, "fields": "core,basic", **win}).json().get("data", [])
            names = {o["name"] for o in obs}
            need = {"evaluate-trade", "get-recent-activity", "load-account-policy", "apply-decision-policy", "commit-decision"}
            if k == "B":
                need.add("record-human-review")
            scores = httpx.get(f"{host}/api/public/v3/scores", auth=auth, timeout=20,
                               params={"traceId": tid, "fields": "details,subject"}).json().get("data", [])
            scores = [s for s in scores if s["name"] == "human_approved"]
            if need <= names and (k != "B" or scores):
                found[k] = {"trace_id": tid, "observations": sorted(f"{o['name']}:{o['type']}" for o in obs),
                            "scores": [{"name": s["name"], "value": s["value"], "dataType": s.get("dataType"),
                                        "comment": s.get("comment")} for s in scores]}
                break
            assert time.time() < deadline, f"trace {k} {tid} incomplete in Langfuse: {names}, scores={len(scores)}"
            time.sleep(5)
    assert found["B"]["scores"][0]["value"] in (0, False)
    STATE["langfuse"] = found
    EVID.mkdir(exist_ok=True)
    (EVID / "e2e_result.json").write_text(json.dumps({
        "run_id": STATE["run_id"], "reset_seconds": STATE["reset_s"],
        "burst": {k: STATE["burst"][k] for k in ("before_cents", "recent_notional_cents", "insert_ack_ms", "visible_after_ms")},
        "cases": {k: {"trade_id": str(STATE[k]["request"]["trade_id"]), "final": STATE[k]["decision"]["final_disposition"],
                      "recommendation": STATE[k]["recommendation"]["recommendation"], "reason": STATE[k]["recommendation"]["reason_code"],
                      "prior_cents": STATE[k]["recommendation"]["prior_notional_cents"], "audit": [e["event_type"] for e in STATE[k]["audit"]]}
                  for k in ("A", "B", "C")},
        "langfuse": found}, indent=2, default=str))


def test_audit_append_only_outside_reset():
    import psycopg
    from app import postgres_store as pg
    with pg.conn() as cn:
        assert cn.execute("SELECT count(*) AS n FROM trade_demo.audit_events").fetchone()["n"] > 0
        with pytest.raises(psycopg.errors.RaiseException):
            cn.execute("DELETE FROM trade_demo.audit_events")
