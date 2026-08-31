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

import json
from typing import Any

from sqlalchemy.orm import Session

from ..anomaly.models import BeneficiarySnapshot, EntitySnapshot
from ..operations.models import OperationalIssue
from ..reconciliation.models import ReconciliationBreak
from .client import MODEL, get_client
from .models import AgentNarrative

SYSTEM_PROMPT = """You are a payment-operations analyst assistant for a bank's internal \
fraud/operations dashboard. You will be given a JSON object of REAL, already-computed \
facts about one detected issue (a fraud/anomaly signal, an operational issue, or a \
reconciliation break) produced by deterministic detection code -- not by you.

Your job: write a short, professional narrative someone reviewing this dashboard would \
read in seconds -- a clear title, a one-to-two sentence description, and a recommended \
next action.

Hard rules, in order of importance:
1. Use ONLY the facts given to you. Never invent a dollar amount, percentage, date, \
transaction id, or any other specific number that is not present in the input.
2. Every identifier in the input (party_id, transaction_id, reference_id, or similar) \
MUST appear in your title or description reproduced EXACTLY, character for character -- \
copy it, never abbreviate, shorten, or "clean up" any part of it. "MER-20A71A0D" must \
stay "MER-20A71A0D", never "MER-20A71A" or any other truncated/reformatted version.
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
estimate unless it is directly computable from the given input.

Respond with a JSON object with exactly these keys: "title" (string, 60 characters or \
fewer), "description" (string, 1-2 sentences), "recommended_action_title" (string, 8 \
words or fewer), "recommended_action_description" (string, 1 sentence)."""

_REQUIRED_KEYS = {"title", "description", "recommended_action_title", "recommended_action_description"}

# Which fact is this signal type's real-world identifier -- the thing a
# human would actually go look up afterward, so getting it wrong (or
# silently truncating it) is a correctness bug, not a style nitpick.
_IDENTIFIER_FACT_KEY = {
    "operational_issue": "reference_id",
    "reconciliation_break": "transaction_id",
    "fraud_anomaly": "party_id",
    "funnel_account": "beneficiary_key",
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


async def narrate(facts: dict[str, Any]) -> dict[str, Any]:
    """Calls Mistral to generate a narrative grounded strictly in
    `facts`. Raises on an API error, a malformed/incomplete response,
    or a failed grounding check -- callers should treat a raised
    exception as "narration unavailable right now" and fall back to
    the plain-facts display, never show a broken or ungrounded
    narrative.

    Async, not sync -- confirmed against real Meridian data before
    writing this the sync way that a single call can take on the order
    of minutes end to end from this environment. A synchronous call
    inside main.py's `async def` endpoint would block the whole FastAPI
    event loop for that entire duration, stalling every other request
    the server is handling. complete_async (the SDK's own async method,
    not a manual thread-pool workaround) avoids that.
    """
    client = get_client()
    response = await client.chat.complete_async(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(facts)},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,  # low -- this summarizes given facts, it doesn't creatively write
    )
    content = response.choices[0].message.content
    parsed = json.loads(content)

    missing = _REQUIRED_KEYS - parsed.keys()
    if missing:
        raise ValueError(f"Mistral response missing required keys: {sorted(missing)}")

    result = {
        "title": parsed["title"],
        "description": parsed["description"],
        "recommended_action_title": parsed["recommended_action_title"],
        "recommended_action_description": parsed["recommended_action_description"],
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

    if existing:
        existing.title = result["title"]
        existing.description = result["description"]
        existing.recommended_action_title = result["recommended_action_title"]
        existing.recommended_action_description = result["recommended_action_description"]
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
        recommended_action_title=result["recommended_action_title"],
        recommended_action_description=result["recommended_action_description"],
        model=MODEL,
    )
    db.add(narrative)
    db.commit()
    return narrative
