"""Actual verified status of each dependency. Never invents a 'running' badge."""
import os
import time
import httpx
from app import config, postgres_store as pg


def check_all(ch_factory=None) -> dict:
    out = {}
    t = time.perf_counter()
    ch = None
    try:
        ch = (ch_factory or config.ch_client)()
        ver = ch.query("SELECT version()").result_rows[0][0]
        rows = ch.query("SELECT count() FROM trade_demo.activity_events").result_rows[0][0]
        out["clickhouse"] = {"status": "running", "label": f"{config.CH_LABEL} {ver}", "rows": rows,
                             "ms": round((time.perf_counter() - t) * 1000)}
    except Exception as e:
        out["clickhouse"] = {"status": "error", "label": config.CH_LABEL, "error": str(e)[:200]}
    try:
        with pg.conn() as c:
            v = c.execute("SHOW server_version").fetchone()["server_version"]
        managed = config.POSTGRES_MODE == "managed"
        out["postgres"] = {"status": "running" if managed else "fallback", "label": f"{config.POSTGRES_LABEL} ({v.split()[0]})"}
    except Exception as e:
        out["postgres"] = {"status": "error", "label": f"{config.POSTGRES_LABEL}: unreachable", "error": str(e)[:200]}
    if not config.LANGFUSE_ENABLED:
        out["langfuse"] = {"status": "off", "label": "Langfuse: not configured (tracing off)"}
    else:
      try:
        r = httpx.get(os.environ["LANGFUSE_HOST"].rstrip("/") + "/api/public/projects",
                      auth=(os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"]), timeout=10)
        out["langfuse"] = {"status": "running" if r.status_code == 200 else "error",
                           "label": "Langfuse", "http": r.status_code}
      except Exception as e:
        out["langfuse"] = {"status": "error", "label": "Langfuse", "error": str(e)[:200]}
    out["mcp"] = {"status": "configured" if config.CH_RO_PASSWORD else "pending",  # verified by Start demo
                  "label": "MCP stdio tool (read-only user demo_ro)" if config.CH_RO_PASSWORD else "MCP (readonly=1 setting)"}
    try:
        from app.cdc import mirror_exists
        if ch is None or out["clickhouse"]["status"] != "running":
            raise RuntimeError("ClickHouse unreachable")
        live = mirror_exists(ch)
        last = None
        if live:
            try:
                v = ch.query("SELECT max(_peerdb_synced_at) FROM trade_demo_cdc.trade_demo_audit_events").result_rows[0][0]
                last = v.strftime("%H:%M:%S UTC") if v and v.year > 1970 else None
            except Exception:
                pass
        # Mirror presence alone doesn't prove the pipe is alive, so show when it last synced.
        out["clickpipes"] = {"status": "running" if live else "not configured",
                             "label": (f"ClickPipes CDC: mirror trade_demo_cdc (last sync {last or 'none yet'})"
                                       if live else ("ClickPipes CDC: initial load in progress"
                                                     if (os.getenv("CLICKPIPE_ID") or "").strip() else "ClickPipes CDC: not configured"))}
    except Exception as e:
        out["clickpipes"] = {"status": "error", "label": "ClickPipes CDC: unknown (ClickHouse unreachable)", "error": str(e)[:200]}
    out["llm"] = {"status": "off", "label": "Rule-based explanation (no LLM)"}
    return out
