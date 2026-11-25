"""Green only if: B baseline == $120k, A baseline == $14k, seed present, no pending decisions,
CH / PG / Langfuse reachable, MCP tool answers via the read-only user. CDC line is informational."""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.preflight import run_checks, print_report

r = asyncio.run(run_checks())
print_report(r)
sys.exit(0 if r["ready"] else 1)
