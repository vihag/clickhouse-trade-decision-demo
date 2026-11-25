"""Rule-based decision agent: one real MCP call for analytics context, a deterministic
policy, one Postgres transaction for the outcome, one Langfuse trace per trade."""
from app import postgres_store as pg
from app import mcp_client
from app.telemetry import flush_trace, langfuse, propagate_attributes, trace_id_for, trace_url

EXPLAIN = {
    "ACCOUNT_RESTRICTED": "Account {acct} is RESTRICTED by policy {pv}; restricted accounts always go to a human.",
    "CONTEXT_UNAVAILABLE": "Recent analytics context for account {acct} was unavailable ({status}); failing closed to human review.",
    "SINGLE_TRADE_LIMIT": "Candidate ${cand:,.0f} exceeds the single-trade cap ${cap:,.0f}.",
    "ACTIVITY_THRESHOLD_EXCEEDED": "Recent 15-min activity ${prior:,.0f} + candidate ${cand:,.0f} = ${pros:,.0f}, above the ${lim:,.0f} activity limit.",
    "WITHIN_ACTIVITY_POLICY": "Recent 15-min activity ${prior:,.0f} + candidate ${cand:,.0f} = ${pros:,.0f}, within the ${lim:,.0f} activity limit.",
}


def decide(policy: dict, ctx: dict, candidate_cents: int) -> dict:
    """Deterministic policy (spec 4.3). Candidate is added exactly once to prior activity."""
    ok_ctx = ctx.get("status") == "ok" and "recent_notional_cents" in ctx  # no data => fail closed
    prior = ctx.get("recent_notional_cents") if ok_ctx else None
    pros = prior + candidate_cents if prior is not None else None
    if policy is None:
        rec, reason = "REVIEW", "CONTEXT_UNAVAILABLE"
    elif policy["account_status"] != "ACTIVE":
        rec, reason = "REVIEW", "ACCOUNT_RESTRICTED"
    elif not ok_ctx:
        rec, reason = "REVIEW", "CONTEXT_UNAVAILABLE"
    elif candidate_cents > policy["max_single_trade_cents"]:
        rec, reason = "REVIEW", "SINGLE_TRADE_LIMIT"
    elif pros > policy["activity_limit_cents"]:
        rec, reason = "REVIEW", "ACTIVITY_THRESHOLD_EXCEEDED"
    else:
        rec, reason = "AUTO_APPROVE", "WITHIN_ACTIVITY_POLICY"
    lim = policy["activity_limit_cents"] if policy else 0
    explanation = EXPLAIN[reason].format(
        acct=ctx.get("account_id"), pv=(policy or {}).get("policy_version"), status=ctx.get("status"),
        cand=candidate_cents / 100, cap=(policy or {}).get("max_single_trade_cents", 0) / 100,
        prior=(prior or 0) / 100, pros=(pros or 0) / 100, lim=lim / 100)
    return {"recommendation": rec, "reason_code": reason, "prior_notional_cents": prior,
            "candidate_cents": candidate_cents, "prospective_cents": pros, "activity_limit_cents": lim,
            "policy_version": (policy or {}).get("policy_version", "unknown"),
            "explanation": explanation, "analytics_context": ctx}


async def evaluate_trade(trade_id: str) -> dict:
    req = pg.get_request(trade_id)
    if req is None:
        raise KeyError(trade_id)
    existing = pg.trade_detail(trade_id)
    if existing["decision"]:  # idempotent: never re-evaluate or re-trace
        return {**existing, "idempotent_replay": True}
    trace_id = trace_id_for(trade_id)
    acct, cand = req["account_id"], req["notional_cents"]
    with langfuse.start_as_current_observation(
        as_type="span", name="evaluate-trade", trace_context={"trace_id": trace_id},
        input={"trade_id": str(trade_id), "case": req["case_label"], "account_id": acct,
               "candidate_cents": cand, "symbol": req["symbol"], "synthetic": True},
    ) as root, propagate_attributes(
        session_id=req["run_id"], trace_name="trade-decision",
        tags=["synthetic", "trade-demo", f"case-{req['case_label']}", "rule-based"],
        metadata={"trade_id": str(trade_id), "account_id": str(acct), "case": req["case_label"]},
    ):
        with langfuse.start_as_current_observation(as_type="tool", name="get-recent-activity",
                input={"tool": "get_recent_activity", "transport": "MCP stdio", "account_id": acct}) as t:
            try:
                ctx = await mcp_client.call_get_recent_activity(acct)
            except Exception as e:
                ctx = {"account_id": acct, "status": "error", "error": f"MCP call failed: {e}"[:300]}
            t.update(output=ctx, level="DEFAULT" if ctx.get("status") in ("ok", "no_recent_activity") else "WARNING",
                     metadata={"clickhouse_user": ctx.get("clickhouse_user"), "read_rows": ctx.get("read_rows")})
        with langfuse.start_as_current_observation(as_type="retriever", name="load-account-policy",
                input={"account_id": acct, "store": "Postgres trade_demo.account_policies"}) as p:
            policy = pg.get_policy(acct)
            p.update(output={k: (str(v) if k == "updated_at" else v) for k, v in (policy or {}).items()})
        with langfuse.start_as_current_observation(as_type="guardrail", name="apply-decision-policy",
                input={"account_status": (policy or {}).get("account_status"), "prior_cents": ctx.get("recent_notional_cents"),
                       "candidate_cents": cand, "activity_limit_cents": (policy or {}).get("activity_limit_cents"),
                       "max_single_trade_cents": (policy or {}).get("max_single_trade_cents")}) as g:
            rec = decide(policy, ctx, cand)
            g.update(output={"recommendation": rec["recommendation"], "reason_code": rec["reason_code"],
                             "prospective_cents": rec["prospective_cents"], "explanation": rec["explanation"],
                             "explanation_mode": "rule-based (no LLM)"})
        with langfuse.start_as_current_observation(as_type="span", name="commit-decision",
                input={"store": "Postgres", "tables": ["recommendations", "decisions", "audit_events"]}) as c:
            dec, created = pg.commit_recommendation(trade_id, rec, trace_id)
            c.update(output={"final_disposition": dec["final_disposition"], "version": dec["version"], "created": created})
        root.update(output={"recommendation": rec["recommendation"], "reason_code": rec["reason_code"],
                            "final_disposition": dec["final_disposition"], "explanation": rec["explanation"]})
    flush_trace()  # explicit: the trace is exported before the HTTP response
    return {**pg.trade_detail(trade_id), "trace_url": trace_url(trace_id), "idempotent_replay": False}


def review_trade(trade_id: str, decision: str, comment: str, reviewer: str = "presenter") -> dict:
    """Postgres commit first (authoritative); Langfuse enrichment after, best-effort."""
    d = pg.apply_review(trade_id, decision, reviewer, comment)
    detail = pg.trade_detail(trade_id)
    trace_id = d["trace_id"]
    langfuse_ok = True
    try:
        with langfuse.start_as_current_observation(
            as_type="span", name="record-human-review", trace_context={"trace_id": trace_id},
            input={"decision": decision, "comment": comment, "reviewer": reviewer,
                   "agent_recommendation": (detail["recommendation"] or {}).get("recommendation")},
            output={"final_disposition": d["final_disposition"], "version": d["version"], "store": "Postgres"},
        ), propagate_attributes(session_id=detail["request"]["run_id"]):
            pass
        langfuse.create_score(name="human_approved", value=1 if decision == "APPROVE" else 0, data_type="BOOLEAN",
                              trace_id=trace_id, comment=comment or None,
                              metadata={"reviewer": reviewer, "reason_code": (detail["recommendation"] or {}).get("reason_code")})
        flush_trace()
    except Exception:
        langfuse_ok = False
    return {**detail, "langfuse_enriched": langfuse_ok, "trace_url": trace_url(trace_id)}
