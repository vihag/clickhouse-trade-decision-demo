"""Scripted full trial through the RUNNING app's HTTP API (A, burst, B reject with comment, C).
Saves screenshots (if Chrome is available) or JSON, plus trace IDs, to evidence/previous_run/<timestamp>/,
then clicks Start demo again so the app is left clean and READY.
Usage: python scripts/trial.py [--base http://127.0.0.1:8000] [--no-langfuse-check]"""
import argparse, json, os, shutil, subprocess, sys, time
from datetime import datetime
from pathlib import Path
import httpx
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://127.0.0.1:8000")
ap.add_argument("--no-langfuse-check", action="store_true")
a = ap.parse_args()
H = httpx.Client(base_url=a.base, timeout=120)
stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
OUT = ROOT / "evidence" / "previous_run" / stamp
OUT.mkdir(parents=True, exist_ok=True)


def post(p, **kw):
    r = H.post(p, **kw); r.raise_for_status(); return r.json()


def find_chrome():
    for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", shutil.which("google-chrome"),
              shutil.which("chromium"), shutil.which("chromium-browser")):
        if c and Path(c).exists():
            return c


SHOTS = {"ok": True}


def shot(name, url):
    chrome = find_chrome()
    if not chrome or not SHOTS["ok"] or os.getenv("TRIAL_SCREENSHOTS", "1") == "0":
        return None
    path = OUT / f"{name}.png"
    # Isolated profile: headless Chrome on macOS hangs if it tries to reuse the logged-in default profile.
    profile = Path("/tmp") / f"ch-trade-demo-chrome-{name}"
    profile.mkdir(exist_ok=True)
    cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
           "--window-size=1440,1000", "--virtual-time-budget=8000",
           f"--user-data-dir={profile}", f"--screenshot={path}", url]
    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        SHOTS["ok"] = False  # headless Chrome hangs on this machine: don't wait for the next ones too
        print(f"  note: headless Chrome screenshot timed out; skipping screenshots (JSON evidence is saved)", flush=True)
    return path.name if path.exists() and path.stat().st_size > 0 else None


try:
    H.get("/health").raise_for_status()
except Exception as e:
    sys.exit(f"app not reachable at {a.base} ({e}); run `make serve-bg` first")

st = post("/admin/start-demo")
print(f"Start demo: {st['state'].upper()} ({st['result']['elapsed_s']}s)")
if st["state"] != "ready":
    ff = st["result"]["first_failure"]
    sys.exit(f"NOT READY: {ff['name']} {ff['detail']} → {ff['hint']}")
res = {"captured_at_local": stamp, "label": "PREVIOUS RUN (scripted trial, not live)", "base": a.base,
       "start_demo": {k: st["result"][k] for k in ("run_id", "elapsed_s", "baseline_valid_until_utc")}, "cases": {}}
A = post("/api/cases/A/run")
burst = post("/admin/inject-burst")
B = post("/api/cases/B/run")
B = post(f"/api/trades/{B['request']['trade_id']}/review",
         json={"decision": "REJECT", "comment": "Burst pushes account 1042 past its activity limit; reject."})
C = post("/api/cases/C/run")
res["burst"] = {k: burst.get(k) for k in ("before_cents", "recent_notional_cents", "insert_ack_ms", "visible_after_ms")}
for k, d in (("A", A), ("B", B), ("C", C)):
    rec = d["recommendation"]
    res["cases"][k] = {"trade_id": str(d["request"]["trade_id"]), "final": d["decision"]["final_disposition"],
                       "recommendation": rec["recommendation"], "reason": rec["reason_code"],
                       "prior_cents": rec["prior_notional_cents"], "prospective_cents": rec["prospective_cents"],
                       "audit": [e["event_type"] for e in d["audit"]], "trace_id": d["decision"]["trace_id"],
                       "trace_url": d.get("trace_url"), "explanation": rec["explanation"]}
    (OUT / f"case_{k}.json").write_text(json.dumps(d, indent=2, default=str))
ok = (res["cases"]["A"]["final"] == "AUTO_APPROVED" and res["cases"]["B"]["final"] == "HUMAN_REJECTED"
      and res["cases"]["C"]["final"] == "PENDING_REVIEW")
res["scripted_outcomes_ok"] = ok

shots = {k: shot(f"monitor_case_{k}", f"{a.base}/?trade={res['cases'][k]['trade_id']}") for k in ("A", "B", "C")}
res["screenshots"] = shots
for name, path in (("cdc", "/api/cdc"), ("health", "/health")):
    try:
        (OUT / f"{name}.json").write_text(json.dumps(H.get(path).json(), indent=2, default=str))
    except Exception:
        pass

from app import config  # loads .env
if os.getenv("CLICKPIPE_ID") and os.getenv("POSTGRES_MODE") == "managed" and os.getenv("TRIAL_CDC_CHECK", "1") != "0":
    # cloud: prove the decisions made above reached the ClickHouse mirror through ClickPipes (syncs every ~60 s)
    want, deadline = {c["trade_id"]: c["final"] for c in res["cases"].values()}, time.time() + 240

    def mirrored(t):
        rows = H.get(f"/api/trades/{t}/cdc-readback").json().get("trade_rows") or []
        return bool(rows) and rows[0].get("final_disposition") == want[t]

    while time.time() < deadline:
        seen = {t for t in want if mirrored(t)}
        if seen == set(want):
            break
        time.sleep(10)
    cdc = H.get("/api/cdc").json()
    res["cdc_verified"] = {"replicated": sorted(seen), "all": seen == set(want),
                           "observed_lag_s": (cdc.get("newest_replicated_decision") or {}).get("observed_lag_s")}
    print(f"CDC (ClickPipes -> trade_demo_cdc): {len(seen)}/{len(want)} decisions replicated, "
          f"lag {res['cdc_verified']['observed_lag_s']}s", flush=True)
    ok = ok and seen == set(want)
if not a.no_langfuse_check and config.LANGFUSE_ENABLED:
    from datetime import timedelta, timezone
    host, auth = os.environ["LANGFUSE_HOST"].rstrip("/"), (os.environ["LANGFUSE_PUBLIC_KEY"], os.environ["LANGFUSE_SECRET_KEY"])
    now = datetime.now(timezone.utc)
    win = {"fromStartTime": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "toStartTime": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    seen, deadline = {}, time.time() + 90
    while time.time() < deadline and len(seen) < 3:
        for k, c in res["cases"].items():
            if k in seen:
                continue
            try:
                obs = httpx.get(f"{host}/api/public/v2/observations", auth=auth, timeout=20,
                                params={"traceId": c["trace_id"], "fields": "core", **win}).json().get("data", [])
                if len(obs) >= 5:
                    seen[k] = len(obs)
            except Exception:
                pass
        if len(seen) < 3:
            time.sleep(5)
    res["langfuse_verified"] = {k: k in seen for k in res["cases"]}

(OUT / "trial.json").write_text(json.dumps(res, indent=2, default=str))
lines = [f"# PREVIOUS RUN — scripted trial {stamp} (not live)", "",
         f"Run {res['start_demo']['run_id']} · scripted outcomes {'OK' if ok else 'MISMATCH'}", ""]
for k, c in res["cases"].items():
    lines.append(f"- **Case {k}**: {c['final']} ({c['reason']}) · trace `{c['trace_id']}`"
                 + (f" · {c['trace_url']}" if c.get("trace_url") else "")
                 + (f" · ![{k}]({shots[k]})" if shots.get(k) else ""))
lines.append(f"\nBurst: {res['burst']}\n\nLangfuse verified: {res.get('langfuse_verified', 'skipped')}")
(OUT / "README.md").write_text("\n".join(lines) + "\n")
(ROOT / "evidence" / "previous_run" / "LATEST").write_text(stamp + "\n")
print(f"Saved {OUT.relative_to(ROOT)}: screenshots={[v for v in shots.values() if v] or 'none (JSON only)'}")
for k, c in res["cases"].items():
    print(f"  {k}: {c['final']:15s} trace {c['trace_id']}")
print(f"  Langfuse verified: {res.get('langfuse_verified', 'skipped')}")

st = post("/admin/start-demo")  # leave the app clean and READY for the presenter
print(f"Start demo again: {st['state'].upper()} ({st['result']['elapsed_s']}s) — app left clean")
sys.exit(0 if ok and st["state"] == "ready" else 1)
