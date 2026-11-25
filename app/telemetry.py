"""Langfuse (OpenTelemetry-based SDK). Imported after app.config has loaded .env."""
import app.config  # noqa: F401  (loads env first)
from langfuse import get_client, propagate_attributes  # noqa: F401

langfuse = get_client()


def trace_id_for(trade_id: str) -> str:
    """Deterministic 32-hex trace id derived from the trade id (one trace per trade)."""
    return langfuse.create_trace_id(seed=str(trade_id))


def trace_url(trace_id: str) -> str | None:
    try:
        return langfuse.get_trace_url(trace_id=trace_id)
    except Exception:
        return None


def flush_trace() -> None:
    """Block until queued spans and scores are exported, before the API response returns."""
    langfuse.flush()
