"""Authoritative OLTP state: requests, recommendations, decisions, append-only audit."""
import hashlib, json, time, uuid
from datetime import datetime, timezone
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from app.config import DATABASE_URL, FIXTURES, ROOT


def conn():
    if not DATABASE_URL:
        raise RuntimeError("no Postgres URL: set .pg_conn / DATABASE_URL_MANAGED (or POSTGRES_MODE=local)")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10, application_name='trade-demo')


RETRYABLE = ("40P01", "40001", "55P03")  # deadlock_detected, serialization_failure, lock_not_available


def with_retry(fn, *a, attempts=4, backoff=0.4, **kw):
    """Run one Postgres transaction function, retrying deadlocks / lock timeouts with short backoff.
    Each attempt is a fresh transaction, so a retry never sees a half-applied change."""
    for i in range(attempts):
        try:
            return fn(*a, **kw)
        except psycopg.Error as e:
            if getattr(e, "sqlstate", None) not in RETRYABLE or i == attempts - 1:
                raise
            time.sleep(backoff * (2 ** i))


def _schema_sql() -> tuple[str, str]:
    sql = (ROOT / "sql" / "postgres_schema.sql").read_text()
    return sql, "trade-demo-schema sha256:" + hashlib.sha256(sql.encode()).hexdigest()[:16]


def migrate(force: bool = False):
    """Apply sql/postgres_schema.sql only when it changed (marker in the schema comment).
    The file has DDL that takes ACCESS EXCLUSIVE locks on every table (DROP/CREATE TRIGGER, ALTER TABLE
    REPLICA IDENTITY). Re-running it on every Start demo deadlocked with concurrent lock holders
    (e.g. the ClickPipes CDC reader catching up after an idle period), so it is now skipped when current,
    and when it does run it uses a lock_timeout and is retried."""
    sql, marker = _schema_sql()
    if not force:
        with conn() as c:
            r = c.execute("SELECT obj_description(oid, 'pg_namespace') AS d FROM pg_namespace WHERE nspname = 'trade_demo'").fetchone()
            if r and r["d"] == marker:
                return False

    def apply():
        with conn() as c:
            c.execute("SET LOCAL lock_timeout = '5s'")
            c.execute(sql)
            c.execute(f"COMMENT ON SCHEMA trade_demo IS '{marker}'")
    with_retry(apply)
    return True


def upsert_policies():
    with_retry(_upsert_policies)


def _upsert_policies():
    fx = json.loads(FIXTURES.read_text())
    with conn() as c:
        for a in fx["accounts"]:
            c.execute("""INSERT INTO trade_demo.account_policies
                (account_id, account_status, max_single_trade_cents, activity_limit_cents, policy_version)
                VALUES (%(account_id)s, %(account_status)s, %(max_single_trade_cents)s, %(activity_limit_cents)s, %(policy_version)s)
                ON CONFLICT (account_id) DO UPDATE SET account_status=EXCLUDED.account_status,
                  max_single_trade_cents=EXCLUDED.max_single_trade_cents, activity_limit_cents=EXCLUDED.activity_limit_cents,
                  policy_version=EXCLUDED.policy_version, updated_at=now()""", a)


def reset_and_new_run() -> str:
    now = datetime.now(timezone.utc)
    run_id = "demo-" + now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
    # DELETE, not TRUNCATE: ClickPipes CDC does not propagate TRUNCATE (verified: a truncated demo_runs
    # row stayed in the trade_demo_cdc mirror). Sequences are not restarted, so audit event_ids never
    # get reused under the mirror's ReplacingMergeTree key.
    def reset():
        with conn() as c:  # one transaction
            c.execute("SET LOCAL trade_demo.allow_reset = 'on'")
            c.execute("SET LOCAL lock_timeout = '5s'")
            for t in ("audit_events", "decisions", "recommendations", "trade_requests", "demo_runs"):  # fixed order
                c.execute(f"DELETE FROM trade_demo.{t}")
            c.execute("INSERT INTO trade_demo.demo_runs(run_id) VALUES (%s)", (run_id,))
    with_retry(reset)
    return run_id


def current_run_id() -> str | None:
    with conn() as c:
        r = c.execute("SELECT run_id FROM trade_demo.demo_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return r["run_id"] if r else None


def get_policy(account_id: int):
    with conn() as c:
        return c.execute("SELECT * FROM trade_demo.account_policies WHERE account_id=%s", (account_id,)).fetchone()


def create_request(run_id, case_label, account_id, symbol, notional_cents) -> dict:
    """Idempotent per (run_id, case_label): a second click returns the existing request."""
    with conn() as c:
        existing = c.execute("SELECT * FROM trade_demo.trade_requests WHERE run_id=%s AND case_label=%s",
                             (run_id, case_label)).fetchone()
        if existing:
            return existing
        tid = uuid.uuid4()
        row = c.execute("""INSERT INTO trade_demo.trade_requests (trade_id, run_id, case_label, account_id, symbol, notional_cents)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (tid, run_id, case_label, account_id, symbol, notional_cents)).fetchone()
        c.execute("INSERT INTO trade_demo.audit_events(trade_id, event_type, actor, payload) VALUES (%s,'REQUESTED','app',%s)",
                  (tid, Jsonb({"account_id": account_id, "notional_cents": notional_cents, "symbol": symbol})))
        return row


def get_request(trade_id):
    with conn() as c:
        return c.execute("SELECT * FROM trade_demo.trade_requests WHERE trade_id=%s", (trade_id,)).fetchone()


def commit_recommendation(trade_id, rec: dict, trace_id: str) -> tuple[dict, bool]:
    """Single transaction: recommendation + initial decision + audit events.
    Returns (decision_row, created). Idempotent on trade_id."""
    with conn() as c:
        c.execute("SELECT 1 FROM trade_demo.trade_requests WHERE trade_id=%s FOR UPDATE", (trade_id,))
        d = c.execute("SELECT * FROM trade_demo.decisions WHERE trade_id=%s", (trade_id,)).fetchone()
        if d:
            return d, False
        c.execute("""INSERT INTO trade_demo.recommendations (trade_id, recommendation, reason_code, prior_notional_cents,
                       candidate_cents, prospective_cents, activity_limit_cents, policy_version, explanation, explanation_mode, analytics_context)
                     VALUES (%(trade_id)s,%(recommendation)s,%(reason_code)s,%(prior_notional_cents)s,%(candidate_cents)s,
                       %(prospective_cents)s,%(activity_limit_cents)s,%(policy_version)s,%(explanation)s,'rule-based',%(ctx)s)""",
                  {**rec, "trade_id": trade_id, "ctx": Jsonb(rec["analytics_context"])})
        final = "AUTO_APPROVED" if rec["recommendation"] == "AUTO_APPROVE" else "PENDING_REVIEW"
        d = c.execute("""INSERT INTO trade_demo.decisions (trade_id, final_disposition, trace_id)
                         VALUES (%s,%s,%s) RETURNING *""", (trade_id, final, trace_id)).fetchone()
        c.execute("INSERT INTO trade_demo.audit_events(trade_id, event_type, actor, payload) VALUES (%s,'RECOMMENDED','rule-based-agent',%s)",
                  (trade_id, Jsonb({"recommendation": rec["recommendation"], "reason_code": rec["reason_code"]})))
        c.execute("INSERT INTO trade_demo.audit_events(trade_id, event_type, actor, payload) VALUES (%s,%s,'decision-service',%s)",
                  (trade_id, "AUTO_APPROVED" if final == "AUTO_APPROVED" else "REVIEW_REQUESTED", Jsonb({"trace_id": trace_id})))
        return d, True


class ReviewConflict(Exception):
    pass


def apply_review(trade_id, decision: str, reviewer: str, comment: str) -> dict:
    """Conditional transition PENDING_REVIEW -> HUMAN_*, plus audit event, in one transaction."""
    final = {"APPROVE": "HUMAN_APPROVED", "REJECT": "HUMAN_REJECTED"}[decision]
    with conn() as c:
        d = c.execute("""UPDATE trade_demo.decisions SET final_disposition=%s, reviewer=%s, review_comment=%s,
                            version=version+1, updated_at=now()
                         WHERE trade_id=%s AND final_disposition='PENDING_REVIEW' RETURNING *""",
                      (final, reviewer, comment, trade_id)).fetchone()
        if not d:
            raise ReviewConflict("trade is not pending review (already decided or unknown)")
        c.execute("INSERT INTO trade_demo.audit_events(trade_id, event_type, actor, payload) VALUES (%s,%s,%s,%s)",
                  (trade_id, final, reviewer, Jsonb({"comment": comment, "version": d["version"]})))
        return d


def trade_detail(trade_id):
    with conn() as c:
        req = c.execute("SELECT * FROM trade_demo.trade_requests WHERE trade_id=%s", (trade_id,)).fetchone()
        if not req:
            return None
        rec = c.execute("SELECT * FROM trade_demo.recommendations WHERE trade_id=%s", (trade_id,)).fetchone()
        dec = c.execute("SELECT * FROM trade_demo.decisions WHERE trade_id=%s", (trade_id,)).fetchone()
        aud = c.execute("SELECT * FROM trade_demo.audit_events WHERE trade_id=%s ORDER BY event_id", (trade_id,)).fetchall()
        pol = c.execute("SELECT * FROM trade_demo.account_policies WHERE account_id=%s", (req["account_id"],)).fetchone()
    return {"request": req, "recommendation": rec, "decision": dec, "audit": aud, "policy": pol}


def list_trades(run_id):
    with conn() as c:
        return c.execute("""SELECT r.trade_id, r.case_label, r.account_id, r.symbol, r.notional_cents, r.requested_at,
                                   d.final_disposition, d.trace_id, rec.recommendation, rec.reason_code
                            FROM trade_demo.trade_requests r
                            LEFT JOIN trade_demo.decisions d USING (trade_id)
                            LEFT JOIN trade_demo.recommendations rec USING (trade_id)
                            WHERE r.run_id=%s ORDER BY r.requested_at""", (run_id,)).fetchall()


def audit_history(run_id, limit=50):
    with conn() as c:
        return c.execute("""SELECT a.event_id, a.trade_id, r.case_label, a.event_type, a.actor, a.payload, a.created_at
                            FROM trade_demo.audit_events a JOIN trade_demo.trade_requests r USING (trade_id)
                            WHERE r.run_id=%s ORDER BY a.event_id DESC LIMIT %s""", (run_id, limit)).fetchall()


def metrics(run_id):
    with conn() as c:
        return c.execute("""SELECT count(*) FILTER (WHERE d.final_disposition='AUTO_APPROVED') AS automated,
                                   count(*) FILTER (WHERE d.final_disposition IN ('HUMAN_APPROVED','HUMAN_REJECTED')) AS human_reviews,
                                   count(*) FILTER (WHERE d.final_disposition='PENDING_REVIEW') AS pending
                            FROM trade_demo.decisions d JOIN trade_demo.trade_requests r USING (trade_id)
                            WHERE r.run_id=%s""", (run_id,)).fetchone()


def pending_count():
    with conn() as c:
        return c.execute("SELECT count(*) AS n FROM trade_demo.decisions WHERE final_disposition='PENDING_REVIEW'").fetchone()["n"]
