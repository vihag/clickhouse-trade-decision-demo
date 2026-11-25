# ClickHouse demo query outputs

Captured 2026-11-21T12:53:12+00:00 from ClickHouse Cloud (26.6.1.2455). Real output, not edited.

## 1_total_rows

```sql
-- Logical total of synthetic activity rows (count() is not read_rows).
SELECT count() AS total_events, uniq(account_id) AS accounts, min(event_ts) AS first_event, max(event_ts) AS last_event
FROM trade_demo.activity_events
```

```
total_events	accounts	first_event	last_event
5000009	4002	2026-11-14 12:12:12.066000	2026-11-21 12:50:23.174000
```

stats: read_rows=5000009 read_bytes=60000108 server_elapsed_ms=60.8 client_roundtrip_ms=73

## 2_explain_pruning_1042

```sql
-- Sparse primary index (account_id, event_ts): how many granules survive for one account.
EXPLAIN indexes = 1
SELECT count(), sum(notional_cents)
FROM trade_demo.activity_events
WHERE account_id = 1042 AND event_ts >= now() - INTERVAL 1 DAY
```

```
explain
Expression ((Project names + Projection))
  Aggregating
    Expression (Before GROUP BY)
      Expression ((WHERE + Change column names to column identifiers))
        ReadFromMergeTree (trade_demo.activity_events)
        Indexes:
          Statistics
            Keys:
              account_id
              event_ts
            Parts: 1/2
            Granules: 1/612
          PrimaryKey
            Keys:
              account_id
              event_ts
            Condition: and((event_ts in [1795179192, +Inf)), (account_id in [1042, 1042]))
            Parts: 1/1
            Granules: 1/1
            Search Algorithm: binary search
          Ranges: 1
```

stats: read_rows=21 read_bytes=808 server_elapsed_ms=22.7 client_roundtrip_ms=39

## 3_filtered_query_1042

```sql
-- Run the filtered query itself, then compare read_rows in the stats with the table total.
SELECT count() AS events, sum(notional_cents) / 100 AS usd
FROM trade_demo.activity_events
WHERE account_id = 1042 AND event_ts >= now() - INTERVAL 1 DAY
```

```
events	usd
5	120000.0
```

stats: read_rows=9 read_bytes=180 server_elapsed_ms=4.5 client_roundtrip_ms=14

## 4_mv_context_last_15m

```sql
-- Agent context: same query the MCP tool get_recent_activity runs (sumMerge/countMerge on the MV target).
SELECT account_id,
       countMerge(trade_count)          AS recent_trade_count,
       sumMerge(notional_cents) / 100   AS recent_usd,
       max(minute_bucket)               AS latest_bucket
FROM trade_demo.account_activity_1m
WHERE account_id IN (1042, 1055, 1060)
  AND minute_bucket >= toStartOfMinute(now()) - INTERVAL 14 MINUTE
GROUP BY account_id
ORDER BY account_id
```

```
account_id	recent_trade_count	recent_usd	latest_bucket
1042	5	120000.0	2026-11-21 12:50:00
1055	4	14000.0	2026-11-21 12:50:00
```

stats: read_rows=8 read_bytes=320 server_elapsed_ms=5.3 client_roundtrip_ms=20

## 5_raw_vs_mv_reconciliation

```sql
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
ORDER BY account_id
```

```
account_id	raw_count	mv_count	raw_cents	mv_cents	reconciled
1042	5	5	12000000	12000000	1
1055	4	4	1400000	1400000	1
```

stats: read_rows=17 read_bytes=500 server_elapsed_ms=31.2 client_roundtrip_ms=51

## 6_whole_table_reconciliation

```sql
-- Whole-table check: MV totals equal raw totals (lightweight-deleted rows are excluded from both).
SELECT
    (SELECT count() FROM trade_demo.activity_events) AS raw_rows,
    (SELECT countMerge(trade_count) FROM trade_demo.account_activity_1m) AS mv_rows,
    (SELECT sum(notional_cents) FROM trade_demo.activity_events) AS raw_cents,
    (SELECT sumMerge(notional_cents) FROM trade_demo.account_activity_1m) AS mv_cents
```

```
raw_rows	mv_rows	raw_cents	mv_cents
5000009	5000009	81349534672800	81349534672800
```

stats: read_rows=14403165 read_bytes=190450553 server_elapsed_ms=71.8 client_roundtrip_ms=92

## 7_cdc_reconciliation

```sql
-- ClickPipes Postgres CDC mirror (trade_demo_cdc, ReplacingMergeTree). Current state = FINAL and not deleted.
-- Compare with Postgres: SELECT count(*) FROM trade_demo.decisions / trade_demo.audit_events. Mirror lags ~60 s.
SELECT
    (SELECT count() FROM trade_demo_cdc.trade_demo_decisions    FINAL WHERE _peerdb_is_deleted = 0) AS ch_decisions,
    (SELECT count() FROM trade_demo_cdc.trade_demo_audit_events FINAL WHERE _peerdb_is_deleted = 0) AS ch_audit_events,
    (SELECT countIf(final_disposition = 'AUTO_APPROVED') FROM trade_demo_cdc.trade_demo_decisions FINAL WHERE _peerdb_is_deleted = 0) AS auto_approved,
    (SELECT countIf(final_disposition = 'HUMAN_REJECTED') FROM trade_demo_cdc.trade_demo_decisions FINAL WHERE _peerdb_is_deleted = 0) AS human_rejected,
    (SELECT max(_peerdb_synced_at) FROM trade_demo_cdc.trade_demo_audit_events) AS last_synced_at
```

```
ch_decisions	ch_audit_events	auto_approved	human_rejected	last_synced_at
0	0	0	0	2026-11-21 12:51:51.211000
```

stats: read_rows=234 read_bytes=4099 server_elapsed_ms=16.5 client_roundtrip_ms=32
