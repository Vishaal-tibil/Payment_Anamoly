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

from ..anomaly.models import EntitySnapshot
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
2. Do not assert a root cause unless the facts directly support it. If the facts don't \
clearly indicate why something happened, describe what was observed without claiming \
a cause.
3. If you don't have enough information to recommend a specific action, say exactly \
that ("insufficient detail to recommend a specific action -- review manually") rather \
than inventing generic-sounding advice with fabricated specifics.
4. Never state a resolution timeframe, success probability, or recovered-amount \
estimate unless it is directly computable from the given input.

Respond with a JSON object with exactly these keys: "title" (string, 60 characters or \
fewer), "description" (string, 1-2 sentences), "recommended_action_title" (string, 8 \
words or fewer), "recommended_action_description" (string, 1 sentence)."""

_REQUIRED_KEYS = {"title", "description", "recommended_action_title", "recommended_action_description"}


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


async def narrate(facts: dict[str, Any]) -> dict[str, Any]:
    """Calls Mistral to generate a narrative grounded strictly in
    `facts`. Raises on an API error or a malformed/incomplete response
    -- callers should treat a raised exception as "narration
    unavailable right now" and fall back to the plain-facts display,
    never show a broken or partially-fabricated narrative.

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

    return {
        "title": parsed["title"],
        "description": parsed["description"],
        "recommended_action_title": parsed["recommended_action_title"],
        "recommended_action_description": parsed["recommended_action_description"],
    }


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
