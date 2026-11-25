-- name: 1_total_rows
-- Logical total of synthetic activity rows (count() is not read_rows).
SELECT count() AS total_events, uniq(account_id) AS accounts, min(event_ts) AS first_event, max(event_ts) AS last_event
FROM trade_demo.activity_events;

-- name: 2_explain_pruning_1042
-- Sparse primary index (account_id, event_ts): how many granules survive for one account.
EXPLAIN indexes = 1
SELECT count(), sum(notional_cents)
FROM trade_demo.activity_events
WHERE account_id = 1042 AND event_ts >= now() - INTERVAL 1 DAY;

-- name: 3_filtered_query_1042
-- Run the filtered query itself, then compare read_rows in the stats with the table total.
SELECT count() AS events, sum(notional_cents) / 100 AS usd
FROM trade_demo.activity_events
WHERE account_id = 1042 AND event_ts >= now() - INTERVAL 1 DAY;

-- name: 4_mv_context_last_15m
-- Agent context: same query the MCP tool get_recent_activity runs (sumMerge/countMerge on the MV target).
SELECT account_id,
       countMerge(trade_count)          AS recent_trade_count,
       sumMerge(notional_cents) / 100   AS recent_usd,
       max(minute_bucket)               AS latest_bucket
FROM trade_demo.account_activity_1m
WHERE account_id IN (1042, 1055, 1060)
  AND minute_bucket >= toStartOfMinute(now()) - INTERVAL 14 MINUTE
GROUP BY account_id
ORDER BY account_id;

-- name: 5_raw_vs_mv_reconciliation
-- Raw table vs MV target over the same minute window, per demo account.
SELECT r.account_id, r.raw_count, m.mv_count, r.raw_cents, m.mv_cents,
       (r.raw_count = m.mv_count AND r.raw_cents = m.mv_cents) AS reconciled
FROM
(
    SELECT account_id, count() AS raw_count, sum(notional_cents) AS raw_cents
    FROM trade_demo.activity_events
    WHERE account_id IN (1042, 1055) AND event_ts >= toStartOfMinute(now()) - INTERVAL 14 MINUTE
    GROUP BY account_id
) AS r
INNER JOIN
(
    SELECT account_id, countMerge(trade_count) AS mv_count, sumMerge(notional_cents) AS mv_cents
    FROM trade_demo.account_activity_1m
    WHERE account_id IN (1042, 1055) AND minute_bucket >= toStartOfMinute(now()) - INTERVAL 14 MINUTE
    GROUP BY account_id
) AS m USING (account_id)
ORDER BY account_id;

-- name: 6_whole_table_reconciliation
-- Whole-table check: MV totals equal raw totals (lightweight-deleted rows are excluded from both).
SELECT
    (SELECT count() FROM trade_demo.activity_events) AS raw_rows,
    (SELECT countMerge(trade_count) FROM trade_demo.account_activity_1m) AS mv_rows,
    (SELECT sum(notional_cents) FROM trade_demo.activity_events) AS raw_cents,
    (SELECT sumMerge(notional_cents) FROM trade_demo.account_activity_1m) AS mv_cents;

-- name: 7_cdc_reconciliation
-- ClickPipes Postgres CDC mirror (trade_demo_cdc, ReplacingMergeTree). Current state = FINAL and not deleted.
-- Compare with Postgres: SELECT count(*) FROM trade_demo.decisions / trade_demo.audit_events. Mirror lags ~60 s.
SELECT
    (SELECT count() FROM trade_demo_cdc.trade_demo_decisions    FINAL WHERE _peerdb_is_deleted = 0) AS ch_decisions,
    (SELECT count() FROM trade_demo_cdc.trade_demo_audit_events FINAL WHERE _peerdb_is_deleted = 0) AS ch_audit_events,
    (SELECT countIf(final_disposition = 'AUTO_APPROVED') FROM trade_demo_cdc.trade_demo_decisions FINAL WHERE _peerdb_is_deleted = 0) AS auto_approved,
    (SELECT countIf(final_disposition = 'HUMAN_REJECTED') FROM trade_demo_cdc.trade_demo_decisions FINAL WHERE _peerdb_is_deleted = 0) AS human_rejected,
    (SELECT max(_peerdb_synced_at) FROM trade_demo_cdc.trade_demo_audit_events) AS last_synced_at;
