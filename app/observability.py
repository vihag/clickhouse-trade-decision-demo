"""Application observability: OpenTelemetry traces, logs and metrics for this app, exported over OTLP/HTTP
to ClickHouse Observability (managed ClickStack / HyperDX UI on ClickHouse Cloud).

Configured from .env; a strict no-op when CLICKSTACK_OTLP_ENDPOINT is unset, so nothing breaks offline:
  CLICKSTACK_OTLP_ENDPOINT   base OTLP HTTP endpoint from Team Settings -> Data -> Managed Telemetry
                             (e.g. https://<id>.<region>.clickhouse.cloud:4318; no /v1/... suffix)
  CLICKSTACK_API_KEY         its API key (sent as 'authorization: Bearer <key>'; never logged or returned)
  CLICKSTACK_SERVICE_NAME    service.name (default trade-decision-desk)
  CLICKSTACK_UI_URL          observability UI base for deep links (default https://hyperdx.clickhouse.cloud)
  CLICKSTACK_CHC_SERVICE_ID  ?chcServiceId= for the deep links (default: CH_CONSOLE_SERVICE_ID)

Isolation from Langfuse: Langfuse (SDK v5) owns the *global* OTel TracerProvider. This module uses its own private
providers, so app spans never go to Langfuse and Langfuse spans never go to the observability endpoint.
Cost: one span + one log line per API call and a handful of metric series every 15 s: kilobytes per demo."""
import json, logging, os, time
from urllib.parse import quote, urlencode

SERVICE_NAME = os.getenv("CLICKSTACK_SERVICE_NAME", "trade-decision-desk")
log = logging.getLogger("trade_demo")
log.setLevel(logging.INFO)

_state = {"enabled": False, "tracer": None, "providers": [], "exports_ok": 0, "exports_failed": 0,
          "last_error": None, "last_export_at": None}
_m = {}  # metric instruments


def configured() -> bool:
    return bool(os.getenv("CLICKSTACK_OTLP_ENDPOINT", "").strip())


def enabled() -> bool:
    return _state["enabled"]


def _wrap(exporter):
    """Count export results for the status chip; never raises into the app."""
    orig = exporter.export

    def export(batch, *a, **k):
        try:
            r = orig(batch, *a, **k)
        except Exception as e:  # pragma: no cover - exporter normally returns a failure code instead
            _state.update(exports_failed=_state["exports_failed"] + 1, last_error=type(e).__name__)
            raise
        ok = getattr(r, "name", str(r)) == "SUCCESS"
        _state["exports_ok" if ok else "exports_failed"] += 1
        _state["last_export_at"] = time.time()
        if not ok:
            _state["last_error"] = "export rejected (check endpoint / API key)"
        return r
    exporter.export = export
    return exporter


def setup(span_exporter=None, metric_reader=None, log_exporter=None) -> bool:
    """Idempotent. Real OTLP exporters when configured; tests may inject in-memory ones.
    The RequestTelemetry middleware is always installed by app.main and is a pass-through until this succeeds."""
    if _state["enabled"]:
        return True
    if not (configured() or span_exporter):
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
        res = Resource.create({"service.name": SERVICE_NAME, "service.namespace": "clickhouse-demo",
                               "deployment.environment": "demo", "service.version": "trade-demo"})
        ep = os.getenv("CLICKSTACK_OTLP_ENDPOINT", "").strip().rstrip("/")
        key = os.getenv("CLICKSTACK_API_KEY", "").strip()
        if key.lower().startswith("bearer "):
            key = key[7:]
        headers = {"authorization": f"Bearer {key}"} if key else {}
        test = span_exporter is not None
        if not test:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
            span_exporter = OTLPSpanExporter(endpoint=f"{ep}/v1/traces", headers=headers, timeout=10)
            log_exporter = OTLPLogExporter(endpoint=f"{ep}/v1/logs", headers=headers, timeout=10)
            metric_reader = PeriodicExportingMetricReader(
                _wrap(OTLPMetricExporter(endpoint=f"{ep}/v1/metrics", headers=headers, timeout=10)),
                export_interval_millis=15000)
        tp = TracerProvider(resource=res)
        tp.add_span_processor(SimpleSpanProcessor(span_exporter) if test else
                              BatchSpanProcessor(_wrap(span_exporter), schedule_delay_millis=2000))
        mp = MeterProvider(resource=res, metric_readers=[metric_reader] if metric_reader else [])
        lp = LoggerProvider(resource=res)
        if log_exporter is not None:
            lp.add_log_record_processor(SimpleLogRecordProcessor(log_exporter) if test else
                                        BatchLogRecordProcessor(_wrap(log_exporter), schedule_delay_millis=2000))
        import warnings
        with warnings.catch_warnings():  # SDK LoggingHandler is marked deprecated but works; avoids a new dependency
            warnings.simplefilter("ignore", DeprecationWarning)
            handler = LoggingHandler(level=logging.INFO, logger_provider=lp)
        log.addHandler(handler)
        meter = mp.get_meter("trade_demo")
        _m.update(
            http=meter.create_histogram("trade_demo.http.server.duration", unit="ms",
                                        description="API request latency"),
            decisions=meter.create_counter("trade_demo.decisions", description="Trade decisions by outcome"),
            reviews=meter.create_counter("trade_demo.reviews", description="Human reviews by outcome"),
            ch_ms=meter.create_histogram("trade_demo.clickhouse.query.duration", unit="ms",
                                         description="ClickHouse query latency seen by the agent / UI"),
            starts=meter.create_counter("trade_demo.start_demo", description="Start demo results"),
        )
        _state.update(enabled=True, tracer=tp.get_tracer("trade_demo"), providers=[tp, mp, lp], handler=handler)
        return True
    except Exception as e:  # observability must never stop the demo
        _state.update(enabled=False, last_error=f"setup failed: {type(e).__name__}")
        return False


def shutdown():
    for p in _state.get("providers", []):
        try:
            (p.shutdown if hasattr(p, "shutdown") else (lambda: None))()
        except Exception:
            pass
    if _state.get("handler"):
        log.removeHandler(_state["handler"])
    _state.update(enabled=False, tracer=None, providers=[], handler=None)
    _m.clear()


def flush():
    for p in _state.get("providers", []):
        try:
            p.force_flush()
        except Exception:
            pass


# ---------- helpers used by the app (all no-ops when disabled) ----------
def _clean(attrs: dict) -> dict:
    return {k: (v if isinstance(v, (str, bool, int, float)) else str(v)) for k, v in attrs.items() if v is not None}


def set_run(run_id):
    """Remember the current demo run so every span and access log carries demo.run_id."""
    if run_id:
        _state["run_id"] = run_id


def annotate(**attrs):
    """Add attributes (e.g. trade.id, demo.run_id) to the current request span."""
    if not _state["enabled"]:
        return
    from opentelemetry import trace
    sp = trace.get_current_span()
    if sp is not None and sp.is_recording():
        sp.set_attributes(_clean(attrs))


def event(msg: str, level=logging.INFO, **attrs):
    """Structured log line (exported as an OTel log record, correlated with the current trace)."""
    annotate(**{k: v for k, v in attrs.items() if k.startswith(("trade.", "demo."))})
    if _state["enabled"]:
        log.log(level, msg, extra=_clean(attrs))


def record_decision(disposition, reason_code, case, run_id=None, trade_id=None, ch_query_ms=None):
    set_run(run_id)
    if not _state["enabled"]:
        return
    _m["decisions"].add(1, _clean({"disposition": disposition, "reason_code": reason_code, "case": case}))
    if ch_query_ms is not None:
        _m["ch_ms"].record(float(ch_query_ms), {"query": "get_recent_activity (MCP)"})
    event(f"decision {disposition} ({reason_code})", **{"trade.id": trade_id, "demo.run_id": run_id,
          "trade.case": case, "trade.disposition": disposition, "trade.reason_code": reason_code,
          "clickhouse.query_ms": ch_query_ms})


def record_review(decision, disposition, run_id=None, trade_id=None):
    if not _state["enabled"]:
        return
    _m["reviews"].add(1, {"decision": decision})
    event(f"human review {decision} -> {disposition}", **{"trade.id": trade_id, "demo.run_id": run_id,
          "trade.disposition": disposition, "review.decision": decision})


def record_ch_ms(ms, query: str):
    if _state["enabled"] and ms is not None:
        _m["ch_ms"].record(float(ms), {"query": query})


def record_start(ready: bool, run_id=None, elapsed_s=None, first_failure=None):
    set_run(run_id)
    if not _state["enabled"]:
        return
    _m["starts"].add(1, {"ready": str(bool(ready)).lower()})
    event(f"start demo: {'READY' if ready else 'NOT READY'}", level=logging.INFO if ready else logging.WARNING,
          **{"demo.run_id": run_id, "demo.ready": bool(ready), "demo.elapsed_s": elapsed_s,
             "demo.first_failure": first_failure})


_POLLS = {("GET", "/api/trades"), ("GET", "/api/audit"), ("GET", "/admin/start-demo"), ("GET", "/api/cdc"),
          ("GET", "/health"), ("GET", "/api/links")}


class RequestTelemetry:
    """Pure ASGI middleware: one server span + latency histogram + access log per /api and /admin call."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or not _state["enabled"] or not path.startswith(("/api", "/admin", "/health")):
            return await self.app(scope, receive, send)
        from opentelemetry.trace import SpanKind, Status, StatusCode
        method, t0, status = scope.get("method", "GET"), time.perf_counter(), {"code": 500}

        async def send_wrapper(msg):
            if msg.get("type") == "http.response.start":
                status["code"] = msg.get("status", 500)
            await send(msg)
        if (method, path) in _POLLS:  # the page polls these every few seconds: latency metric only, no span/log noise
            try:
                return await self.app(scope, receive, send_wrapper)
            finally:
                _m["http"].record((time.perf_counter() - t0) * 1000, {"http.route": path, "http.request.method": method,
                                                                      "http.response.status_code": status["code"]})
        with _state["tracer"].start_as_current_span(f"{method} {path}", kind=SpanKind.SERVER) as sp:
            sp.set_attributes({"http.request.method": method, "url.path": path})
            if _state.get("run_id"):
                sp.set_attribute("demo.run_id", _state["run_id"])
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as e:
                sp.record_exception(e)
                raise
            finally:
                route = getattr(scope.get("route"), "path", None) or path
                ms = (time.perf_counter() - t0) * 1000
                code = status["code"]
                sp.update_name(f"{method} {route}")
                sp.set_attributes({"http.route": route, "http.response.status_code": code})
                if code >= 500:
                    sp.set_status(Status(StatusCode.ERROR))
                _m["http"].record(ms, {"http.route": route, "http.request.method": method,
                                       "http.response.status_code": code})
                log.log(logging.WARNING if code >= 400 else logging.INFO, f"{method} {route} {code} {ms:.0f}ms",
                        extra=_clean({"http.route": route, "http.response.status_code": code,
                                       "duration_ms": round(ms, 1), "demo.run_id": _state.get("run_id")}))


# ---------- deep links into the observability UI ----------
def links(run_id: str | None = None, run_started_ms: int | None = None) -> dict:
    """Deep links (documented /search and /services URL parameters). No secrets."""
    base = os.getenv("CLICKSTACK_UI_URL", "https://hyperdx.clickhouse.cloud").rstrip("/")
    sid = os.getenv("CLICKSTACK_CHC_SERVICE_ID") or os.getenv("CH_CONSOLE_SERVICE_ID") or ""
    now = int(time.time() * 1000)
    if run_id and not run_started_ms:  # run ids encode their start: demo-20261125T060102345Z
        try:
            from datetime import datetime, timezone
            run_started_ms = int(datetime.strptime(run_id[5:20], "%Y%m%dT%H%M%S")
                                 .replace(tzinfo=timezone.utc).timestamp() * 1000)
        except Exception:
            run_started_ms = None
    frm = (run_started_ms - 60_000) if run_started_ms else now - 30 * 60_000
    to = max(now, frm) + 3_600_000
    svc_filter = json.dumps([{"type": "sql", "condition": f"ServiceName IN ('{SERVICE_NAME}')"}])

    def url(path, **p):
        p = {k: v for k, v in p.items() if v is not None}
        p["chcServiceId"] = sid
        return f"{base}{path}?{urlencode(p, quote_via=quote)}"
    run_where = f'demo.run_id:"{run_id}"' if run_id else None
    return {
        "configured": configured(), "enabled": enabled(), "service_name": SERVICE_NAME, "ui": url("/search"),
        # the service's HTTP dashboard (request rate, latency, errors) for this demo run's time window
        "service": url("/services", service=SERVICE_NAME, tab="http", **{"from": frm}, to=to),
        "traces": url("/search", source="Traces", filters=svc_filter, **{"from": frm}, to=to),
        "logs_run": url("/search", source="Logs", filters=svc_filter, where=run_where,
                        whereLanguage="lucene" if run_where else None, **{"from": frm}, to=to),
        "trade_template": url("/search", source="Logs", filters=svc_filter, where='trade.id:"__TID__"',
                              whereLanguage="lucene", **{"from": frm}, to=to),
    }


def status() -> dict:
    if not configured():
        return {"status": "off", "label": "Observability: not configured (set CLICKSTACK_OTLP_ENDPOINT in .env)"}
    if not _state["enabled"]:
        return {"status": "error", "label": f"Observability: {_state['last_error'] or 'not started'}"}
    ok, bad = _state["exports_ok"], _state["exports_failed"]
    if bad and not ok:
        return {"status": "error", "label": f"Observability: exports failing ({_state['last_error']})"}
    return {"status": "running" if ok else "configured",
            "label": f"ClickHouse Observability (OTLP): service {SERVICE_NAME} · {ok} exports ok"
                     + (f", {bad} failed" if bad else "")}


def preflight_info() -> str:
    """One INFO line for Start demo / make preflight (never a blocking check: the demo runs without it)."""
    if not configured():
        return "Observability: off (optional; set CLICKSTACK_OTLP_ENDPOINT + CLICKSTACK_API_KEY in .env)"
    st = status() if enabled() else {"label": f"OTLP export configured for service {SERVICE_NAME}"}
    return "Observability: " + st["label"].removeprefix("Observability: ")
