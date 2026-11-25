"""Targeted reset: PG decision tables truncated + new run_id; only demo accounts' CH rows deleted;
baselines (1042=$120k, 1055=$14k) reinserted with fresh timestamps and verified via the MV."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, postgres_store as pg, analytics

t0 = time.perf_counter()
pg.migrate(); pg.upsert_policies()
rid = pg.reset_and_new_run()
ctx = analytics.reset_demo_accounts(config.ch_client())
print(f"run_id={rid}")
for a in (1042, 1055):
    print(f"  acct {a}: ${ctx[a]['recent_notional_cents']/100:,.0f} visible in MV after {ctx[a]['visible_after_ms']} ms")
print(f"  baseline valid until {ctx['baseline_valid_until_utc']} (15-min window) — run the demo before then")
print(f"reset done in {time.perf_counter()-t0:.1f}s")
