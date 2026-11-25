"""Read-only MCP server (stdio). One tool, one fixed parameterized SELECT.
Connects to ClickHouse as the read-only user `demo_ro` (readonly=1, SELECT on trade_demo.* only)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from app.config import ch_client
from app.analytics import recent_activity

server = MCPServer("trade-demo-analytics")
_client = None


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_recent_activity(account_id: int) -> dict:
    """Recent 15-minute minute-bucketed trade activity for a fictitious account from ClickHouse."""
    global _client
    if not (0 < account_id < 10_000_000):
        return {"account_id": account_id, "status": "invalid_account"}
    try:
        _client = _client or ch_client(readonly=True)
        out = recent_activity(_client, account_id)
        out["clickhouse_user"] = _client.query("SELECT currentUser()").result_rows[0][0]
        return out
    except Exception as e:  # fail closed: caller treats non-ok as CONTEXT_UNAVAILABLE
        return {"account_id": account_id, "status": "error", "error": str(e)[:300]}


if __name__ == "__main__":
    server.run("stdio")
