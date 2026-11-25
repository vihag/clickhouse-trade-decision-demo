-- ClickHouse Cloud: analytics side (OLAP). Idempotent.
CREATE DATABASE IF NOT EXISTS trade_demo;

-- Append-only raw synthetic activity. Sorted for account/time pruning.
CREATE TABLE IF NOT EXISTS trade_demo.activity_events
(
    event_id        UUID DEFAULT generateUUIDv4(),
    event_ts        DateTime64(3, 'UTC'),
    account_id      UInt32,
    symbol          LowCardinality(String),
    notional_cents  UInt64,
    source          LowCardinality(String),
    ingested_at     DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = MergeTree
ORDER BY (account_id, event_ts);

-- Minute-bucketed aggregate states per account.
CREATE TABLE IF NOT EXISTS trade_demo.account_activity_1m
(
    account_id      UInt32,
    minute_bucket   DateTime('UTC'),
    trade_count     AggregateFunction(count),
    notional_cents  AggregateFunction(sum, UInt64)
)
ENGINE = AggregatingMergeTree
ORDER BY (account_id, minute_bucket);

-- Incremental MV: fires on every insert block into activity_events.
CREATE MATERIALIZED VIEW IF NOT EXISTS trade_demo.account_activity_1m_mv
TO trade_demo.account_activity_1m AS
SELECT
    account_id,
    toStartOfMinute(event_ts) AS minute_bucket,
    countState()               AS trade_count,
    sumState(notional_cents)   AS notional_cents
FROM trade_demo.activity_events
GROUP BY account_id, minute_bucket;
