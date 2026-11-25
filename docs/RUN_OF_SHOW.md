# Run of show

## Before the session
```bash
./install.sh        # idempotent: infra (local Docker or ClickHouse Cloud), bootstrap, app, tests, scripted trial → INSTALL OK
make tunnel-bg      # optional public URL via ngrok (NGROK_DOMAIN in .env for a fixed domain); .run/tunnel.log
```
Leave the app running. `make stop` stops the app and the tunnel; `make tunnel-stop` stops only the tunnel.
If the infra is already up and you only want a fresh scripted run: `make bootstrap && make serve-bg && make trial`.

Public URL notes: a first browser visit to a free ngrok URL shows ngrok's **"You are about to visit"** page; click
through once (`curl` can send `ngrok-skip-browser-warning: 1`). Anyone with the link can press **Start demo**, so share
it only with your audience. A free ngrok account runs one session per domain at a time.

Cloud path: `CLICKPIPE_ID` is written to `.env` by the installer. If you recreate the ClickPipe by hand, update it
(from the pipe's console URL `…/imports/<CLICKPIPE_ID>/details`) and restart the app (`make stop && make serve-bg`);
if it's unset or stale, the ClickPipe link falls back to the service's Data sources list.

**Optional, app observability:** see `docs/OBSERVABILITY.md`. After Start demo + Scenario 1 the footer chip
**Observability** turns green. If it doesn't, leave it: the demo runs without it (the link just hides).

## During the demo
1. Open the app (`http://127.0.0.1:8000`, or the `APP_PORT` in `.env`, or the tunnel URL). This one page is the whole
   demo; the technical detail sits behind links. If `DECK_PATH` is set, the header shows a **Deck** link (`/deck`).
2. Click **▶ Start demo** (top right). It resets the demo, so the 15-minute baseline window starts now, then runs every
   preflight check (target under 15 s).
3. Wait for **READY** (green, next to the button). The four scenario tabs unlock only when READY.
   - **NOT READY** (red) shows the failing check with a one-line hint (click the badge for all checks). Fix it, or click Start demo again.
   - **WINDOW EXPIRED** (amber) means the 15-minute window has passed. Click Start demo again.
4. For each scenario: click its tab, read the **brief** (situation · real-time analytics · insight · decision), then
   click **▶ Run scenario**. The storyline reveals six steps:
   **1 Incoming trade → 2 Real-time queries → 3 Insight → 4 Decision → 5 Action → 6 Back to ClickHouse**.
   - **1 · Routine trade** (acct 1055, $8k) → Approved automatically.
   - **2 · Activity spike** (acct 1042, +$76k) → $120k → $196k, visible in the rollup within milliseconds.
   - **3 · Large trade after spike** (acct 1042, $70k) → $266k > $250k → Sent for human review. In step 5, click **Reject**.
   - **4 · Restricted account** (acct 1060, $12k) → Held for compliance review. Leave it pending.
5. The "wow" links, in story order:
   - Step 2: **SQL + EXPLAIN** drawer (exact query, latency, rows read, granules skipped on the 5M-row raw table) ·
     **Open in ClickHouse console** (prefilled SQL; the local Play UI on the local path) · rollup / MV / raw table.
   - Step 4: the agent's reasoning (MCP tool call → policy → rule → commit) · **Langfuse trace** (when configured).
   - Step 5: **Approve / Reject** opens the **Audit trail**: ① persisted to Postgres (*Show Postgres SQL + result*) →
     ② ClickPipes CDC (syncing… → lag, about 60 s) → ③ the same row in ClickHouse with `FINAL` → ④ Langfuse
     `human_approved` score. Leave it open while it syncs; that's the integration proof. On the local path ② and ③ show
     "not configured" (ClickPipes is a ClickHouse Cloud feature).
   - Step 6: replication status with lag · straight-through and approval rates over the mirror · **ClickPipe** ·
     **Mirror query + analytics SQL** · **Langfuse scores / session** · **Open in observability** (when configured).
6. The audit trail strip (above the footer) shows each event in plain words; click one for the raw record. Status chips are in the footer.

**Restart at any point:** click **Start demo** again. It's a full restart with a clean run and fresh baselines.

## Fallback if something breaks live
Say so, then open `evidence/previous_run/<latest>/` (the name is in `evidence/previous_run/LATEST`): `README.md`,
`monitor_case_{A,B,C}.png` (storyline view of each case), `trial.json` and the trace IDs from the last scripted trial.
Present it as **PREVIOUS RUN**, never as live.
If the app itself is down: `make stop; make serve-bg` (and `make tunnel-bg`), then reload and click Start demo.
