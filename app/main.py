"""Trade Decision Monitor — FastAPI backend (bind 127.0.0.1, single presenter)."""
import asyncio, json, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from pydantic import BaseModel, Field
from app import config, postgres_store as pg, analytics, agent, cdc, links, observability as obs
from app.health import check_all
from app.preflight import run_checks
from app.telemetry import trace_url

@asynccontextmanager
async def _lifespan(_app):
    yield
    obs.shutdown()  # flush queued telemetry on exit (no-op when observability is off)


app = FastAPI(title="Trade Decision Monitor (synthetic demo)", lifespan=_lifespan)
# Outermost: honour X-Forwarded-Proto/Host from the ngrok tunnel so absolute URLs stay https.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.add_middleware(obs.RequestTelemetry)  # pass-through unless observability is configured
obs.setup()                                # no-op without CLICKSTACK_OTLP_ENDPOINT in .env
app.mount("/static", StaticFiles(directory=config.ROOT / "app" / "static"), name="static")
FX = json.loads(config.FIXTURES.read_text())
CASES = {c["case"]: c for c in FX["cases"]}
_ch = None


def ch():
    """Lazily created; never at import time, so the app starts even when ClickHouse is down."""
    global _ch
    _ch = _ch or config.ch_client()
    return _ch


def fresh_ch():
    """Drop any cached client (e.g. after the service was stopped/restarted) and reconnect."""
    global _ch
    _ch = None
    return ch()


START = {"state": "idle", "message": "Click Start demo"}
_start_lock = asyncio.Lock()


def run_id():
    rid = pg.current_run_id()
    if not rid:
        raise HTTPException(409, "no demo run; click Start demo")
    return rid


@app.get("/")
def index():
    return FileResponse(config.ROOT / "app" / "static" / "index.html")


@app.get("/deck")
def deck():
    """Optional read-only slide deck: served only when DECK_PATH points at an existing HTML file."""
    path = config.deck_file()
    if not path:
        raise HTTPException(404, "deck file not found")
    return FileResponse(path, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {**check_all(ch), "observability": obs.status()}


def _do_reset() -> dict:
    t0 = time.perf_counter()
    pg.migrate(); pg.upsert_policies()
    rid = pg.reset_and_new_run()
    ctx = analytics.reset_demo_accounts(fresh_ch())
    return {"run_id": rid, "baselines": ctx, "elapsed_s": round(time.perf_counter() - t0, 1)}


@app.post("/admin/start-demo")
async def start_demo():
    """One click: reset (fresh baselines, so the 15-minute window starts now) + full preflight.
    Clicking again is a full restart. Never raises: the result is READY or NOT READY with a hint."""
    if _start_lock.locked():
        raise HTTPException(409, "Start demo already running")
    async with _start_lock:
        t0 = time.perf_counter()
        START.update(state="starting", message="Resetting demo and running checks…",
                     started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        reset, reset_error = None, None
        try:
            reset = await run_in_threadpool(_do_reset)
        except Exception as e:
            reset_error = f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"
        checks = await run_checks(ch_factory=fresh_ch if reset_error else ch)
        if reset_error:  # reset failed: surface it first, naming every unreachable dependency
            down = [c for c in checks["checks"] if not c["ok"] and c["name"] in ("Postgres reachable", "ClickHouse reachable")]
            if len(down) == 2:
                hint = ("ClickHouse AND Postgres unreachable: start your ClickHouse service and Postgres "
                        "(Cloud console, or `make local-up`), then click Start demo again.")
            elif down:
                hint = down[0]["hint"]
            else:
                hint = (checks["first_failure"] or {}).get("hint") or "Reset failed: click Start demo again."
            checks["checks"].insert(0, {"name": "Reset demo fixtures", "ok": False, "detail": f"({reset_error})",
                                        "hint": hint})
            checks["first_failure"] = checks["checks"][0]
            checks["ready"] = False
        result = {**checks, "reset": reset, "elapsed_s": round(time.perf_counter() - t0, 1),
                  "baseline_valid_until_utc": (reset or {}).get("baselines", {}).get("baseline_valid_until_utc"),
                  "run_id": (reset or {}).get("run_id"),
                  "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        obs.record_start(result["ready"], run_id=result["run_id"], elapsed_s=result["elapsed_s"],
                         first_failure=(result.get("first_failure") or {}).get("name"))
        START.clear()
        START.update(state="ready" if result["ready"] else "not_ready",
                     message="READY" if result["ready"] else "NOT READY", result=result)
        return START


@app.get("/admin/start-demo")
def start_status():
    return START


@app.post("/admin/reset-demo")
def reset_demo():
    """Reset only (no checks). The UI uses Start demo instead."""
    return _do_reset()


@app.post("/admin/inject-burst")
def inject_burst():
    return analytics.inject_burst(ch())


@app.get("/api/context/{account_id}")
def context(account_id: int):
    """Same query as the MCP tool, for the UI's freshness panel (direct read, labelled as such)."""
    return analytics.recent_activity(ch(), account_id)


@app.post("/api/cases/{case}/run")
async def run_case(case: str):
    c = CASES.get(case.upper())
    if not c:
        raise HTTPException(404, "unknown case")
    req = pg.create_request(run_id(), c["case"], c["account_id"], c["symbol"], c["candidate_cents"])
    return _observe_decision(await agent.evaluate_trade(req["trade_id"]))


def _observe_decision(d: dict) -> dict:
    """Decision count + the agent's ClickHouse latency as metrics, plus a correlated log line (no-op if off)."""
    try:
        if not d.get("idempotent_replay") and d.get("decision"):
            rec = d.get("recommendation") or {}
            obs.record_decision(d["decision"]["final_disposition"], rec.get("reason_code"), d["request"]["case_label"],
                                run_id=d["request"]["run_id"], trade_id=str(d["request"]["trade_id"]),
                                ch_query_ms=(rec.get("analytics_context") or {}).get("query_ms"))
    except Exception:
        pass
    return d


@app.get("/api/trades")
def trades():
    rid = run_id()
    return {"run_id": rid, "trades": pg.list_trades(rid), "metrics": pg.metrics(rid)}


@app.post("/api/trades/{trade_id}/evaluate")
async def evaluate(trade_id: UUID):
    try:
        return _observe_decision(await agent.evaluate_trade(trade_id))
    except KeyError:
        raise HTTPException(404, "unknown trade")


class Review(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")
    comment: str = Field(default="", max_length=500)


@app.post("/api/trades/{trade_id}/review")
def review(trade_id: UUID, body: Review):
    try:
        d = agent.review_trade(trade_id, body.decision, body.comment)
    except pg.ReviewConflict as e:
        obs.event("review conflict", level=30, **{"trade.id": str(trade_id)})
        raise HTTPException(409, str(e))
    obs.record_review(body.decision, d["decision"]["final_disposition"], run_id=d["request"]["run_id"],
                      trade_id=str(trade_id))
    return d


@app.get("/api/trades/{trade_id}")
def trade(trade_id: UUID):
    d = pg.trade_detail(trade_id)
    if not d:
        raise HTTPException(404, "unknown trade")
    if d["decision"]:
        d["trace_url"] = trace_url(d["decision"]["trace_id"])
        try:
            d["replication"] = cdc.trade_replication(ch(), trade_id, d["decision"]["version"])
        except Exception as e:
            d["replication"] = {"status": "unavailable", "error": type(e).__name__}
    return d


@app.get("/api/cdc")
def cdc_reconcile():
    try:
        return cdc.reconcile(ch())
    except Exception as e:
        return {"status": "unavailable", "note": f"ClickHouse not reachable ({type(e).__name__})"}


@app.get("/api/audit")
def audit():
    return pg.audit_history(run_id())


@app.get("/api/links")
def console_links():
    """Console/Postgres/observability deep-link bases for the UI's 'View details' links (IDs only, no secrets)."""
    try:
        rid = pg.current_run_id()
    except Exception:
        rid = None
    return {**links.console_links(), "observability": obs.links(rid),
            "public_url": config.PUBLIC_URL or None,
            "deck": "/deck" if config.deck_file() else None}


@app.get("/api/insight-sql/{account_id}")
def insight_sql(account_id: int):
    """The insight query + raw-table equivalent with EXPLAIN indexes=1, for the SQL drawer."""
    try:
        x = links.insight_sql(ch(), account_id)
        obs.record_ch_ms(x.get("mv_ms"), "insight (rollup)"); obs.record_ch_ms(x.get("raw_ms"), "insight (raw table)")
        return x
    except Exception as e:
        raise HTTPException(503, f"ClickHouse not reachable ({type(e).__name__})")


@app.get("/api/trades/{trade_id}/pg-readback")
def pg_readback(trade_id: UUID):
    """Read-only: the decision row and audit events as stored in Postgres (OLTP)."""
    return links.pg_readback(trade_id)


@app.get("/api/trades/{trade_id}/cdc-readback")
def cdc_readback(trade_id: UUID):
    """Read-only: the same decision in the ClickPipes CDC mirror + product analytics over the mirror."""
    try:
        return links.cdc_readback(ch(), trade_id)
    except Exception as e:
        return {"status": "unavailable", "error": type(e).__name__}


@app.get("/api/trades/{trade_id}/audit-trail")
def audit_trail(trade_id: UUID):
    """Read-only audit pop-up after Approve/Reject: Postgres rows -> ClickPipes CDC status -> same rows in ClickHouse."""
    if not pg.get_request(trade_id):
        raise HTTPException(404, "unknown trade")
    out = links.audit_trail(ch, trade_id)
    d = pg.trade_detail(trade_id)
    tr = d and d["decision"] and d["decision"].get("trace_id")
    out["langfuse"] = {"trace_url": trace_url(tr) if tr else None,
                       "score": ("human_approved = " + ("1" if d["decision"]["final_disposition"] == "HUMAN_APPROVED" else "0"))
                       if d and d["decision"] and d["decision"].get("reviewer") else None}
    obs.annotate(**{"trade.id": str(trade_id), "cdc.status": out["clickhouse"].get("status")})
    return out
