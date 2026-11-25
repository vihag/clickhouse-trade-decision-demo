"""TEST-ONLY: run the real app with ClickHouse + MCP faked (ClickHouse service stopped).
Usage: POSTGRES_MODE=local python tests/run_fake_app.py [port] [--ch-fails] [--fake-cdc]"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")
from tests import fakes
fakes.install(ch_fails="--ch-fails" in sys.argv)
import uvicorn
from app.main import app
if "--fake-cdc" in sys.argv:  # simulate a ClickPipes mirror with ~8 s lag (screenshots of the audit trail)
    from app import config
    fakes.STATE["mirror_tables"] = {"trade_demo_decisions", "trade_demo_audit_events"}
    fakes.STATE["fake_cdc_lag_s"] = 8
    config.POSTGRES_MODE = "managed"  # status logic only; the data still comes from local Postgres
port = int(next((a for a in sys.argv[1:] if a.isdigit()), "8010"))
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
