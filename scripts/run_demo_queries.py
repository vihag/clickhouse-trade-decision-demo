"""Execute sql/demo_queries.sql against ClickHouse Cloud; save real outputs + query stats to evidence/."""
import sys, time
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.config import ch_client

c = ch_client()
blocks = [b.strip() for b in (ROOT / "sql" / "demo_queries.sql").read_text().split(";") if b.strip()]
out = [f"# ClickHouse demo query outputs\n\nCaptured {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
       f"from ClickHouse Cloud ({c.query('SELECT version()').result_rows[0][0]}). Real output, not edited.\n"]
for b in blocks:
    name = b.splitlines()[0].replace("-- name:", "").strip()
    sql = "\n".join(l for l in b.splitlines() if not l.startswith("-- name:"))
    t0 = time.perf_counter()
    exec_sql = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    r = c.query(exec_sql)
    ms = (time.perf_counter() - t0) * 1000
    s = r.summary or {}
    lines = ["\t".join(r.column_names)] + ["\t".join(str(v) for v in row) for row in r.result_rows]
    out.append(f"## {name}\n\n```sql\n{sql.strip()}\n```\n\n```\n" + "\n".join(lines) + "\n```\n\n"
               f"stats: read_rows={s.get('read_rows')} read_bytes={s.get('read_bytes')} "
               f"server_elapsed_ms={round(int(s.get('elapsed_ns', 0))/1e6, 1)} client_roundtrip_ms={ms:.0f}\n")
(ROOT / "evidence").mkdir(exist_ok=True)
(ROOT / "evidence" / "demo_queries_output.md").write_text("\n".join(out))
print("\n".join(out))
