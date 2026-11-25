"""Central config. Loads .env before anything else imports Langfuse."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(key: str, default: str = "") -> str:
    """Env value, with blank (KEY= in .env) treated as unset."""
    v = (os.getenv(key) or "").strip()
    return v or default


if os.getenv("LANGFUSE_BASE_URL") and not os.getenv("LANGFUSE_HOST"):
    os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]
# Langfuse is optional. With no keys, tracing is switched off before the SDK is imported and the
# preflight reports it as "not configured" instead of failing.
LANGFUSE_ENABLED = bool(_env("LANGFUSE_PUBLIC_KEY") and _env("LANGFUSE_SECRET_KEY"))
if not LANGFUSE_ENABLED:
    os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
# Default Langfuse export timeout is 5 s; a cold TLS handshake to a far-away Langfuse region can exceed
# that and the OTel batch is then dropped. Give exports more headroom.
os.environ.setdefault("LANGFUSE_TIMEOUT", "30")

# Nothing here may raise or touch the network at import time: the app must start even when
# ClickHouse or Postgres is unreachable, and report that through Start demo / preflight instead.
CH_HOST = _env("CLICKHOUSE_HOST")
CH_PORT = int(_env("CLICKHOUSE_PORT", "443"))
# ClickHouse Cloud is HTTPS (443/8443). The local docker-compose ClickHouse is plain HTTP on 8123.
CH_SECURE = _env("CLICKHOUSE_SECURE", "true").strip().lower() not in ("0", "false", "no", "off")
CH_LABEL = "ClickHouse Cloud" if CH_SECURE else "ClickHouse (local)"
CH_USER = _env("CLICKHOUSE_USER", "default")
CH_PASSWORD = _env("CLICKHOUSE_PASSWORD")
CH_RO_USER = _env("CLICKHOUSE_RO_USER", "demo_ro")
CH_RO_PASSWORD = _env("CLICKHOUSE_RO_PASSWORD")
# Primary: ClickHouse Managed Postgres. Local Postgres is an explicit fallback switch (POSTGRES_MODE=local).
POSTGRES_MODE = _env("POSTGRES_MODE", "managed").lower()
if POSTGRES_MODE not in ("managed", "local"):
    raise SystemExit("POSTGRES_MODE must be 'managed' or 'local'")
PG_CONN_FILE = ROOT / ".pg_conn"
# Matches docker-compose.yml (the local fallback stack). Not a secret: a throwaway local container.
LOCAL_DEFAULT_URL = "postgresql://demo:demo@127.0.0.1:5433/trade_demo"


def _managed_url() -> tuple[str | None, str]:
    """Managed Postgres URL: .pg_conn wins (a rebuilt service drops its new URL there), else .env."""
    if PG_CONN_FILE.exists() and PG_CONN_FILE.read_text().strip():
        return PG_CONN_FILE.read_text().strip(), ".pg_conn"
    if _env("DATABASE_URL_MANAGED"):
        return _env("DATABASE_URL_MANAGED"), ".env DATABASE_URL_MANAGED"
    return None, "missing"


if POSTGRES_MODE == "managed":
    DATABASE_URL, DATABASE_URL_SOURCE = _managed_url()
else:
    DATABASE_URL, DATABASE_URL_SOURCE = (_env("DATABASE_URL_LOCAL") or _env("DATABASE_URL") or LOCAL_DEFAULT_URL,
                                         ".env DATABASE_URL_LOCAL")
POSTGRES_LABEL = "Postgres: ClickHouse Managed Postgres" if POSTGRES_MODE == "managed" else "Postgres: local fallback"
FIXTURES = ROOT / "fixtures" / "scripted_scenarios.json"
# Public URL when the app is reached through the ngrok tunnel (optional; surfaced in /api/links).
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").rstrip("/")
# Optional slide deck served read-only at /deck. Unset (the default) hides the Deck link and /deck returns 404.
DECK_PATH = Path(_env("DECK_PATH")) if _env("DECK_PATH") else None


def deck_file() -> Path | None:
    """Return the deck path if it is a readable file; otherwise None (hide the UI link, 404 /deck)."""
    if not DECK_PATH:
        return None
    p = Path(DECK_PATH).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p if p.is_file() else None



def ch_client(readonly: bool = False):
    if not CH_HOST:
        raise RuntimeError("CLICKHOUSE_HOST missing in .env (run `make setup`)")
    import clickhouse_connect
    user, pw = (CH_RO_USER, CH_RO_PASSWORD) if readonly and CH_RO_PASSWORD else (CH_USER, CH_PASSWORD)
    settings = {} if readonly and CH_RO_PASSWORD else ({"readonly": 1} if readonly else {})
    return clickhouse_connect.get_client(
        host=CH_HOST, port=CH_PORT, username=user, password=pw, secure=CH_SECURE,
        connect_timeout=int(_env("CLICKHOUSE_CONNECT_TIMEOUT", "10")), send_receive_timeout=120, settings=settings, autogenerate_session_id=False,
    )
