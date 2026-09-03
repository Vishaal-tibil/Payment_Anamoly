"""Fact -> narrative generation, and the caching layer around it.

The system prompt is the most important part of this file. Every
other engine in this platform earned trust by never inventing a
number it didn't compute; this is the one place in the whole system
where an LLM could plausibly fabricate something that reads as
authoritative, so the prompt is written to make that explicit and
gives the model an honest way out ("say you don't have enough
information") rather than forcing it to always produce a confident-
sounding recommendation.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy.orm import Session

from ..anomaly.models import BeneficiarySnapshot, EntitySnapshot
from ..investigation.models import InvestigationCase, InvestigationCaseAlert
from ..investigation.trend import category_weekly_trend
from ..operations.models import OperationalIssue
from ..reconciliation.models import ReconciliationBreak
from .client import MODEL, get_client
from .models import AgentNarrative

SYSTEM_PROMPT = """You are a payment-operations analyst assistant for a bank's internal \
fraud/operations dashboard. You will be given a JSON object of REAL, already-computed \
facts about one detected issue (a fraud/anomaly signal, an operational issue, a \
reconciliation break, or an investigation case -- a cluster of several such signals \
grouped together) produced by deterministic detection code -- not by you.

Your job: write a short, professional narrative someone reviewing this dashboard would \
read in seconds -- a clear title, a one-to-two sentence description, and 1-3 ranked \
recommended actions.

Hard rules, in order of importance:
1. Use ONLY the facts given to you. Never invent a dollar amount, percentage, date, \
transaction id, or any other specific number that is not present in the input.
2. Every identifier in the input (party_id, transaction_id, reference_id, case_code, or \
similar) MUST appear in your title or description reproduced EXACTLY, character for \
character -- copy it, never abbreviate, shorten, or "clean up" any part of it. \
"MER-20A71A0D" must stay "MER-20A71A0D", never "MER-20A71A" or any other truncated/ \
reformatted version. Exception: for an investigation case, only the case's own \
"case_code" must be reproduced this way -- the individual alerts listed under "alerts" \
are supporting detail to summarize (their shared category, rail, count), not each one's \
transaction_id individually.
3. Every specific number given to you (amount_total, variance_amount, transaction_count, \
a date, etc.) MUST be stated as its actual value somewhere in your output if you \
reference it at all. Never write vague filler in place of a real number you were given \
-- phrases like "the given amount", "the specified time window", "the relevant \
transactions", or "an anomaly score" are forbidden whenever the actual value is present \
in the input; if you mention that a dollar amount, date, or count exists, state its real \
value, not a placeholder description of it.
4. Do not assert a root cause unless the facts directly support it. If the facts don't \
clearly indicate why something happened, describe what was observed without claiming \
a cause.
5. If you don't have enough information to recommend a specific action, say exactly \
that ("insufficient detail to recommend a specific action -- review manually") rather \
than inventing generic-sounding advice with fabricated specifics.
6. Never state a resolution timeframe, success probability, or recovered-amount \
estimate unless it is directly computable from the given input. This applies to each \
recommended action's "why" too: it may only cite a real amount/count/percentage that is \
literally present in the input facts -- never a projected/future estimate ("could \
prevent $X over the next hour", "would protect an estimated Y%") unless that exact \
projected value is itself already present in the input.
7. Recommend as many distinct actions as the facts genuinely support, from 1 to 3, \
ranked most-important first -- never pad to 3 with a redundant or generic filler action \
just to fill the list. A single simple signal (one operational issue, one reconciliation \
break, one fraud/funnel snapshot) usually only supports one real action. An investigation \
case (a cluster of several alerts) may support up to 3 if the facts clearly justify \
distinct next steps; if they don't, give fewer.
8. An investigation case's facts may include a "category_trend" object (real tenant-wide \
weekly counts of this same category/rail, not just this one case's own alerts) -- if it is \
present, you may reference its "direction" ("increasing"/"decreasing"/"stable"), \
"latest_week_count", and "prior_weeks_average_count" verbatim, since those are real \
computed numbers. If "category_trend" is null/absent, do not mention a trend, rate of \
change, or week-over-week comparison at all -- there is no real data behind one.

Respond with a JSON object with exactly these keys: "title" (string, 60 characters or \
fewer), "description" (string, 1-2 sentences), "recommended_actions" (array of 1 to 3 \
objects, ranked most-important first, each with "title" (string, 8 words or fewer), \
"description" (string, 1 sentence), and "why" (string, 1 sentence grounded only in the \
given facts per rule 6 above)."""

_REQUIRED_KEYS = {"title", "description", "recommended_actions"}
_REQUIRED_ACTION_KEYS = {"title", "description", "why"}

# Which fact is this signal type's real-world identifier -- the thing a
# human would actually go look up afterward, so getting it wrong (or
# silently truncating it) is a correctness bug, not a style nitpick.
_IDENTIFIER_FACT_KEY = {
    "operational_issue": "reference_id",
    "reconciliation_break": "transaction_id",
    "fraud_anomaly": "party_id",
    "funnel_account": "beneficiary_key",
    "investigation_case": "case_code",
}


def facts_for_operational_issue(issue: OperationalIssue) -> dict[str, Any]:
    return {
        "signal_type": "operational_issue",
        "issue_type": issue.issue_type,
        "reference_type": issue.reference_type,
        "reference_id": issue.reference_id,
        "severity_score": issue.severity_score,
        "details": issue.details,
        "detected_at": issue.detected_at.isoformat() if issue.detected_at else None,
    }


def facts_for_reconciliation_break(brk: ReconciliationBreak) -> dict[str, Any]:
    return {
        "signal_type": "reconciliation_break",
        "detection_type": brk.detection_type,
        "transaction_id": brk.transaction_id,
        "rail_type": brk.rail_type,
        "source_reconciliation_status": brk.source_reconciliation_status,
        "variance_amount": brk.variance_amount,
        "amount": brk.amount,
        "detected_at": brk.detected_at.isoformat() if brk.detected_at else None,
    }


def facts_for_entity_snapshot(snapshot: EntitySnapshot) -> dict[str, Any]:
    return {
        "signal_type": "fraud_anomaly",
        "party_id": snapshot.party_id,
        "party_type": snapshot.party_type,
        "window_start": snapshot.window_start.isoformat() if snapshot.window_start else None,
        "window_end": snapshot.window_end.isoformat() if snapshot.window_end else None,
        "transaction_count": snapshot.transaction_count,
        "amount_total": snapshot.amount_total,
        "isolation_forest_score": snapshot.isolation_forest_score,
        "cluster_changed": snapshot.cluster_changed,
        "timeseries_drift_score": snapshot.timeseries_drift_score,
        "final_anomaly_score": snapshot.final_anomaly_score,
        "anomaly_band": snapshot.anomaly_band,
    }


def facts_for_beneficiary_snapshot(snapshot: BeneficiarySnapshot) -> dict[str, Any]:
    return {
        "signal_type": "funnel_account",
        "beneficiary_key": snapshot.beneficiary_key,
        "beneficiary_name": snapshot.beneficiary_name,
        "window_start": snapshot.window_start.isoformat() if snapshot.window_start else None,
        "window_end": snapshot.window_end.isoformat() if snapshot.window_end else None,
        "transaction_count": snapshot.transaction_count,
        "amount_total": snapshot.amount_total,
        "distinct_senders": snapshot.distinct_senders,
        "distinct_new_senders": snapshot.distinct_new_senders,
        "new_sender_ratio": snapshot.new_sender_ratio,
        "sender_party_types": snapshot.sender_party_types,
        "funnel_drift_score": snapshot.funnel_drift_score,
    }


def facts_for_investigation_case(db: Session, case: InvestigationCase, alerts: list[InvestigationCaseAlert]) -> dict[str, Any]:
    return {
        "signal_type": "investigation_case",
        "case_code": case.case_code,
        "category": case.category,
        "payment_rail": case.payment_rail,
        "current_exposure": case.current_exposure,
        "transactions_affected": case.transactions_affected,
        "contributing_alerts_count": case.contributing_alerts_count,
        "opened_at": case.opened_at.isoformat() if case.opened_at else None,
        "alerts": [
            {
                "anomaly_type": a.anomaly_type,
                "anomaly_category": a.anomaly_category,
                "payment_rail": a.payment_rail,
                "detected_at": a.detected_at.isoformat() if a.detected_at else None,
            }
            for a in alerts
        ],
        # Real tenant-wide weekly count for this same (category,
        # payment_rail), by each alert's real underlying event time --
        # never this case's own detected_at (a batch-compute-run
        # timestamp, not a real event time). None when there isn't 2+
        # real weeks of history to compare -- see trend.py's docstring.
        "category_trend": category_weekly_trend(db, case.tenant_bank_id, case.category, case.payment_rail),
    }


# Confirmed by directly hammering /agent/narrate with 5 back-to-back real
# calls: 3 succeeded, 2 failed with the IDENTICAL "403 tier_not_allowed /
# This model is not available in your subscription tier" error. A real
# subscription restriction would fail every call, not 2 of 5 -- this is
# the shared/free-tier API key's burst rate limit, mis-worded by Mistral
# as a tier error instead of a 429. Retrying after a short pause is
# empirically the fix, not an account/billing change.
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 3.0


async def _complete_with_retry(client, facts: dict[str, Any]):
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return await client.chat.complete_async(
                model=MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(facts)},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,  # low -- this summarizes given facts, it doesn't creatively write
            )
        except Exception as exc:  # transient rate-limit-shaped API error -- see comment above
            last_exc = exc
            if attempt < _MAX_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
    raise last_exc


async def narrate(facts: dict[str, Any]) -> dict[str, Any]:
    """Calls Mistral to generate a narrative grounded strictly in
    `facts`. Raises on an API error persisting across all retries, a
    malformed/incomplete response, or a failed grounding check --
    callers should treat a raised exception as "narration unavailable
    right now" and fall back to the plain-facts display, never show a
    broken or ungrounded narrative.

    Async, not sync -- confirmed against real Meridian data before
    writing this the sync way that a single call can take on the order
    of minutes end to end from this environment. A synchronous call
    inside main.py's `async def` endpoint would block the whole FastAPI
    event loop for that entire duration, stalling every other request
    the server is handling. complete_async (the SDK's own async method,
    not a manual thread-pool workaround) avoids that.
    """
    client = get_client()
    response = await _complete_with_retry(client, facts)
    content = response.choices[0].message.content
    parsed = json.loads(content)

    missing = _REQUIRED_KEYS - parsed.keys()
    if missing:
        raise ValueError(f"Mistral response missing required keys: {sorted(missing)}")

    actions = parsed["recommended_actions"]
    if not isinstance(actions, list) or not (1 <= len(actions) <= 3):
        raise ValueError(f"recommended_actions must be a list of 1-3 items, got: {actions!r}")
    for action in actions:
        if not isinstance(action, dict) or (_REQUIRED_ACTION_KEYS - action.keys()):
            raise ValueError(f"each recommended_actions item must have {sorted(_REQUIRED_ACTION_KEYS)}, got: {action!r}")

    result = {
        "title": parsed["title"],
        "description": parsed["description"],
        "recommended_actions": [
            {"title": a["title"], "description": a["description"], "why": a["why"]} for a in actions
        ],
    }

    # Confirmed against real Meridian data that the prompt alone isn't
    # 100% reliable here: a live call on party_id "MER-20A71A0D" came
    # back with the truncated "MER-20A71A" and vague filler ("totaling
    # the given amount") in place of the real $243,864.42 it was given.
    # A wrong/incomplete identifier is actively dangerous for an
    # operational tool (someone could look up the wrong entity), so
    # this is checked in code, not just asked for in the prompt --
    # same "verify, don't just trust" pattern every detector in this
    # platform already follows for the source's own claims.
    identifier_key = _IDENTIFIER_FACT_KEY.get(facts.get("signal_type"))
    identifier = facts.get(identifier_key) if identifier_key else None
    if identifier:
        combined_text = f"{result['title']} {result['description']}"
        if identifier not in combined_text:
            raise ValueError(
                f"Mistral response failed grounding check: {identifier_key}={identifier!r} "
                f"does not appear verbatim in the generated title/description -- likely "
                f"truncated, paraphrased, or dropped. Not caching this response."
            )

    return result


async def get_or_create_narrative(
    db: Session, signal_type: str, reference_id: str, tenant_bank_id: str, facts: dict[str, Any], force: bool = False,
) -> AgentNarrative:
    """Cached wrapper around narrate() -- one API call per
    (signal_type, reference_id, tenant_bank_id), ever, unless
    force=True. Same idempotent-by-key shape as everything else in
    this platform's output tables, just keyed by a composite string id
    instead of autoincrement since there's no natural single-column key
    across three different source row types.
    """
    narrative_id = f"{signal_type}:{tenant_bank_id}:{reference_id}"
    existing = db.get(AgentNarrative, narrative_id)
    if existing and not force:
        return existing

    result = await narrate(facts)
    primary = result["recommended_actions"][0]

    if existing:
        existing.title = result["title"]
        existing.description = result["description"]
        existing.recommended_action_title = primary["title"]
        existing.recommended_action_description = primary["description"]
        existing.recommended_actions = result["recommended_actions"]
        existing.model = MODEL
        db.commit()
        return existing

    narrative = AgentNarrative(
        id=narrative_id,
        signal_type=signal_type,
        reference_id=reference_id,
        tenant_bank_id=tenant_bank_id,
        title=result["title"],
        description=result["description"],
        recommended_action_title=primary["title"],
        recommended_action_description=primary["description"],
        recommended_actions=result["recommended_actions"],
        model=MODEL,
    )
    db.add(narrative)
    db.commit()
    return narrative
