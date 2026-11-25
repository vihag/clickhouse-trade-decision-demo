"""Create (idempotently) a read-only ClickHouse user for the MCP tool; stores its password in .env."""
import secrets, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import ch_client, ROOT, CH_RO_USER, CH_RO_PASSWORD

pw = CH_RO_PASSWORD or ("Ro" + secrets.token_urlsafe(18) + "9!")
c = ch_client()
c.command(f"CREATE USER IF NOT EXISTS {CH_RO_USER} IDENTIFIED WITH sha256_password BY %(pw)s SETTINGS readonly = 1",
          parameters={"pw": pw})
c.command(f"ALTER USER {CH_RO_USER} IDENTIFIED WITH sha256_password BY %(pw)s", parameters={"pw": pw})
c.command(f"GRANT SELECT ON trade_demo.* TO {CH_RO_USER}")
if not CH_RO_PASSWORD:
    sys.path.insert(0, str(ROOT / "scripts"))
    import envfile
    envfile.set_many({"CLICKHOUSE_RO_USER": CH_RO_USER, "CLICKHOUSE_RO_PASSWORD": pw})  # stored, never printed
print("read-only user ready:", CH_RO_USER)
