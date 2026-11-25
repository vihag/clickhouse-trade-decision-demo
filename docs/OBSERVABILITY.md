# App observability: ClickHouse Observability (managed ClickStack)

The demo app sends its **own** traces, logs and metrics over OpenTelemetry (OTLP/HTTP) to **ClickHouse Observability**,
the fully managed observability service on ClickHouse Cloud (public beta since 19 Nov 2026, built on open-source
ClickStack, explored in the ClickStack/HyperDX UI). It's one more proof point: the same ClickHouse that makes the
trade decisions also observes the app that makes them.

It is **optional and off by default**. Without `CLICKSTACK_OTLP_ENDPOINT` in `.env` the instrumentation is a no-op,
the footer chip says "Observability · off", and the "Open in observability" link is hidden. Nothing else changes.

## What is sent (service.name `trade-decision-desk`)
| Signal | Content |
|---|---|
| Traces | one SERVER span per API call (`POST /api/cases/{case}/run`, review, audit trail, insight SQL, Start demo…) with `http.route`, status, `demo.run_id`, `trade.id`, `cdc.status`. The page's every-few-seconds polls (`GET /api/trades`, `/api/audit`, `/health`…) are metrics only, so traces stay readable. |
| Logs | one access line per traced call, plus business events: `start demo: READY`, `decision AUTO_APPROVED (WITHIN_ACTIVITY_POLICY)`, `human review REJECT -> HUMAN_REJECTED`, all correlated to the span (TraceId) and tagged `demo.run_id` / `trade.id`. |
| Metrics (every 15 s) | `trade_demo.http.server.duration` (ms, by route/status) · `trade_demo.decisions` (by disposition, reason_code, case) · `trade_demo.reviews` · `trade_demo.clickhouse.query.duration` (ms: the agent's MCP query, the insight/raw queries) · `trade_demo.start_demo` (ready true/false). |

Langfuse stays the AI/decision trace (one trace per trade). The app uses **private** OTel providers, so app spans never
go to Langfuse and Langfuse spans never go to ClickHouse Observability. No new Python packages are needed (the OTel SDK
and OTLP/HTTP exporter already come with the Langfuse SDK).

**Cost:** billed per volume. Public beta pricing: $0.10/GB for logs+traces, $0.05 per million metric points.
A full demo day is roughly 1–3 MB of logs/traces and under 50k metric points: well under $0.01.

## One-time setup (about 5 minutes, in the ClickHouse Cloud console)
1. Open the ClickHouse Cloud console → your service (the one in `CH_CONSOLE_SERVICE_ID`) → **ClickStack / Observability** in the left
   menu. If the console asks, create/turn on the fully managed **ClickHouse Observability** service (public beta).
   - If your org/trial doesn't offer the fully managed option, use **Managed ClickStack (self-managed)** instead: it
     needs your own OTel collector, so it's more work; skip the feature unless you need it.
2. On the Getting Started page choose **View connection details** (also at **Team Settings → Data → Managed Telemetry**).
   Copy the **OTLP HTTP endpoint** (full `https://…:4318`, no `/v1/...`) and the **API key**.
3. Click **Send a test log** there and confirm it shows in Search (proves the endpoint works).
4. Add to the repo's `.env` (never commit; never paste the key into chat):
   ```
   CLICKSTACK_OTLP_ENDPOINT=https://<endpoint-from-console>:4318
   CLICKSTACK_API_KEY=<api key>
   # optional
   CLICKSTACK_SERVICE_NAME=trade-decision-desk
   CLICKSTACK_UI_URL=https://hyperdx.clickhouse.cloud     # base of the observability UI (part of the URL before /search)
   CLICKSTACK_CHC_SERVICE_ID=<service id>                  # default: CH_CONSOLE_SERVICE_ID (5e4a4885-…)
   ```
   Check `CLICKSTACK_UI_URL` and `CLICKSTACK_CHC_SERVICE_ID` against the address bar while you're in the observability UI:
   if the UI lives under a different host, or the observability service has its own ID, set them so the deep links land
   on the right service.
5. `make stop && make serve-bg`, click **Start demo**, run Scenario 1. Within ~15 s:
   footer chip **Observability** turns green, and **Open in observability** (step 6) shows `trade-decision-desk`.

## Where it shows in the demo
- **Step 6 · Back to ClickHouse → Open in observability**: the service's HTTP dashboard (request rate, latency,
  errors) for this demo run's time window (`/services?service=trade-decision-desk`).
- **Footer chip "Observability ↗"**: same link; green once exports succeed, red if the endpoint/key is wrong.
- **Audit-trail pop-up → This trade in observability**: Logs search filtered to `trade.id:"<id>"`.
- Deep links use the documented URL parameters (`/search?source=Logs|Traces&filters=…&where=…&from=…&to=…`,
  `/services?service=…`) plus `chcServiceId`.

Talk track (10 s): *"And the app itself is observed in ClickHouse too: its traces, logs and latency metrics go over
OpenTelemetry into ClickHouse Observability. Same engine, pennies per GB."*

## Verify offline
`make test-offline` includes tests that run the real OTLP/HTTP exporters against a local sink (`/v1/traces`, `/v1/logs`,
`/v1/metrics`, `authorization: Bearer …`) and check the spans, metrics and log/trace correlation in memory, plus that
the API key never appears in any API response.
