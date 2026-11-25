-- Postgres (OLTP / source of truth). Idempotent: safe to run repeatedly.
CREATE SCHEMA IF NOT EXISTS trade_demo;

CREATE TABLE IF NOT EXISTS trade_demo.demo_runs (
    run_id      text PRIMARY KEY,
    started_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade_demo.account_policies (
    account_id              integer PRIMARY KEY,
    account_status          text    NOT NULL CHECK (account_status IN ('ACTIVE','RESTRICTED')),
    max_single_trade_cents  bigint  NOT NULL CHECK (max_single_trade_cents > 0),
    activity_limit_cents    bigint  NOT NULL CHECK (activity_limit_cents > 0),
    policy_version          text    NOT NULL,
    updated_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade_demo.trade_requests (
    trade_id        uuid PRIMARY KEY,
    run_id          text   NOT NULL REFERENCES trade_demo.demo_runs(run_id),
    case_label      text   NOT NULL,
    account_id      integer NOT NULL REFERENCES trade_demo.account_policies(account_id),
    symbol          text   NOT NULL,
    notional_cents  bigint NOT NULL CHECK (notional_cents > 0),
    synthetic       boolean NOT NULL DEFAULT true,
    requested_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, case_label)
);

CREATE TABLE IF NOT EXISTS trade_demo.recommendations (
    trade_id               uuid PRIMARY KEY REFERENCES trade_demo.trade_requests(trade_id),
    recommendation         text   NOT NULL CHECK (recommendation IN ('AUTO_APPROVE','REVIEW')),
    reason_code            text   NOT NULL,
    prior_notional_cents   bigint,
    candidate_cents        bigint NOT NULL,
    prospective_cents      bigint,
    activity_limit_cents   bigint NOT NULL,
    policy_version         text   NOT NULL,
    explanation            text   NOT NULL,
    explanation_mode       text   NOT NULL DEFAULT 'rule-based',
    analytics_context      jsonb  NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade_demo.decisions (
    trade_id           uuid PRIMARY KEY REFERENCES trade_demo.trade_requests(trade_id),
    final_disposition  text NOT NULL CHECK (final_disposition IN ('AUTO_APPROVED','PENDING_REVIEW','HUMAN_APPROVED','HUMAN_REJECTED')),
    trace_id           text NOT NULL,
    reviewer           text,
    review_comment     text,
    version            integer NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade_demo.audit_events (
    event_id    bigserial PRIMARY KEY,
    trade_id    uuid NOT NULL REFERENCES trade_demo.trade_requests(trade_id),
    event_type  text NOT NULL CHECK (event_type IN ('REQUESTED','RECOMMENDED','AUTO_APPROVED','REVIEW_REQUESTED','HUMAN_APPROVED','HUMAN_REJECTED')),
    actor       text NOT NULL,
    payload     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS audit_events_trade_idx ON trade_demo.audit_events(trade_id, event_id);

-- Append-only guard: rows may be inserted, never updated or deleted. The only exception is the
-- operator's demo reset, which sets trade_demo.allow_reset=on for its own transaction (SET LOCAL)
-- and uses DELETE (not TRUNCATE) so that ClickPipes CDC replicates the removal.
CREATE OR REPLACE FUNCTION trade_demo.audit_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('trade_demo.allow_reset', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_events is append-only (% blocked)', TG_OP;
END $$;
DROP TRIGGER IF EXISTS audit_events_no_mutation ON trade_demo.audit_events;
CREATE TRIGGER audit_events_no_mutation BEFORE UPDATE OR DELETE ON trade_demo.audit_events
    FOR EACH ROW EXECUTE FUNCTION trade_demo.audit_events_append_only();

-- CDC-friendly (for the P1 ClickPipes "Sync to ClickHouse"): every table has a primary key and
-- REPLICA IDENTITY DEFAULT (= primary key), so UPDATEs on decisions replicate as keyed versions.
ALTER TABLE trade_demo.account_policies REPLICA IDENTITY DEFAULT;
ALTER TABLE trade_demo.trade_requests   REPLICA IDENTITY DEFAULT;
ALTER TABLE trade_demo.recommendations  REPLICA IDENTITY DEFAULT;
ALTER TABLE trade_demo.decisions        REPLICA IDENTITY DEFAULT;
ALTER TABLE trade_demo.audit_events     REPLICA IDENTITY DEFAULT;
ALTER TABLE trade_demo.demo_runs        REPLICA IDENTITY DEFAULT;
