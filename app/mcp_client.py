"""Genuine MCP client: spawns app/mcp_server.py over stdio and calls the tool."""
import json, sys
from app.config import ROOT
from mcp import Client
from mcp.client.stdio import StdioServerParameters

SERVER = StdioServerParameters(command=sys.executable, args=[str(ROOT / "app" / "mcp_server.py")], cwd=str(ROOT))
TOOL = "get_recent_activity"


async def call_get_recent_activity(account_id: int) -> dict:
    async with Client(SERVER, cache=None) as client:
        res = await client.call_tool(TOOL, {"account_id": int(account_id)})
        if getattr(res, "is_error", False) or getattr(res, "isError", False):
            return {"account_id": account_id, "status": "error", "error": "MCP tool error"}
        sc = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        if sc is None:
            sc = json.loads(res.content[0].text)
        if isinstance(sc, dict) and set(sc) == {"result"}:
            sc = sc["result"]
        sc["transport"] = "MCP stdio"
        sc["mcp_server"] = "trade-demo-analytics"
        return sc
