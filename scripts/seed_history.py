"""Synthetic historical activity generator -> real HTTP inserts into ClickHouse Cloud.

~5M rows over 7 days across ~4,000 non-demo accounts (2000-5999). Demo accounts
(1000-1099, incl. 1042/1055/1060) are never generated, so the big seed cannot
contaminate scripted cases. Skips if the seed already exists (rerunnable).
"""
import argparse, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import ch_client

SYMBOLS = ["ACME", "GLBX", "NOVA", "ORBT", "QNTM", "ZEPH", "HELX", "TRDE"]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=5_000_000)
    ap.add_argument("--chunk", type=int, default=1_000_000)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    c = ch_client()
    existing = c.query("SELECT count() FROM trade_demo.activity_events WHERE source='seed_history'").result_rows[0][0]
    if existing >= a.rows * 0.95 and not a.force:
        print(f"seed already present ({existing:,} rows); skipping"); return
    rng = np.random.default_rng(42)
    now = pd.Timestamp.now("UTC").floor("s")
    start = now - pd.Timedelta(days=7)
    span_ms = int((now - pd.Timedelta(minutes=30) - start).total_seconds() * 1000)
    done, t0 = 0, time.time()
    while done < a.rows:
        n = min(a.chunk, a.rows - done)
        df = pd.DataFrame({
            "event_ts": start + pd.to_timedelta(rng.integers(0, span_ms, n), unit="ms"),
            "account_id": rng.integers(2000, 6000, n).astype("uint32"),
            "symbol": rng.choice(SYMBOLS, n),
            "notional_cents": (rng.lognormal(11.5, 1.0, n)).astype("uint64") * 100,
            "source": "seed_history",
        })
        c.insert_df("trade_demo.activity_events", df)
        done += n
        print(f"inserted {done:,}/{a.rows:,} ({time.time()-t0:.0f}s)", flush=True)

if __name__ == "__main__":
    main()
