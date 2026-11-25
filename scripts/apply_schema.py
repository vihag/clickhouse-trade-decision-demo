import subprocess, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config, postgres_store as pg

c = config.ch_client()
for stmt in (ROOT / "sql" / "clickhouse_schema.sql").read_text().split(";"):
    s = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
    if s:
        c.command(s)
pg.migrate(force=True); pg.upsert_policies()
subprocess.run([sys.executable, str(ROOT / "scripts" / "create_ro_user.py")], check=True)
print("schema applied (ClickHouse trade_demo + Postgres trade_demo)")
