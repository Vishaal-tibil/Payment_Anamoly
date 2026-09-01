"""Real 4-band priority levels (Critical/High/Medium/Low) for Operational
Issues and Reconciliation Breaks -- Incidents Centre's "Priority Level"
filter.

Before this module, severity was a frontend-only heuristic
(incidentsAdapter.js's severityForIssue()) that only ever produced
"critical" or "high" -- DUPLICATE_PAYMENT was hardcoded critical, every
other type defaulted to high, and Medium/Low never existed anywhere.
That's fixed here: every issue/break gets a real 0-100 severity_score
computed from whichever real dimension it actually has (dollar amount,
days overdue, or an existing rate z-score), then banded into 4 real
tiers -- not a filter option with nothing behind it.

Computed on read (like app/anomaly/categories.py's category tags), not
stored -- these tables have no severity_score/priority_level column of
their own to keep in sync, and recomputing against the whole real
population on each list call is cheap at this data volume.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .models import CanonicalEvent
from .operations.models import OperationalIssue
from .reconciliation.models import ReconciliationBreak

CRITICAL = "Critical"
HIGH = "High"
MEDIUM = "Medium"
LOW = "Low"
PRIORITY_LEVELS = (CRITICAL, HIGH, MEDIUM, LOW)

# Documented v1 cutoffs, same "revisit later, not final" status as
# anomaly_band's / health_band's own cutoffs elsewhere in this codebase.
_CRITICAL_MIN = 85.0
_HIGH_MIN = 60.0
_MEDIUM_MIN = 35.0

_AMOUNT_BASED_ISSUE_TYPES = ("DUPLICATE_PAYMENT", "FORMAT_REJECTION")
_SCORED_ISSUE_TYPES = ("FORMAT_REJECTION_SPIKE", "NETWORK_TIMEOUT_SPIKE")

# A confirmed break is never truly "Low" priority regardless of dollar
# size -- the source system already validated the discrepancy is real,
# unlike a PROVISIONAL_VARIANCE (still unconfirmed, genuinely fine to
# be Low if the variance is tiny). This floor is the only place
# detection_type -- not just dollar amount -- affects the score.
_CONFIRMED_BREAK_SCORE_FLOOR = 50.0


def _band_for_score(score: float) -> str:
    if score >= _CRITICAL_MIN:
        return CRITICAL
    if score >= _HIGH_MIN:
        return HIGH
    if score >= _MEDIUM_MIN:
        return MEDIUM
    return LOW


def _percentile_rank(value: float, population: list[float]) -> float:
    """Where `value` sits within `population`, as 0-100 -- the % of the
    real population at or below it. Peer-relative, same reasoning as
    app/anomaly/categories.py's segment percentiles: a $500 duplicate
    payment is a real outlier in a population where most are $50-100,
    and a real non-event in one where most are $5,000+.
    """
    if not population:
        return 0.0
    at_or_below = sum(1 for v in population if v <= value)
    return (at_or_below / len(population)) * 100


def priority_levels_for_issues(db: Session, tenant_bank_id: str) -> dict[int, dict[str, Any]]:
    """Returns {OperationalIssue.id: {"severity_score": float, "priority_level": str}}
    for every issue this tenant has.
    """
    issues = db.query(OperationalIssue).filter_by(tenant_bank_id=tenant_bank_id).all()

    # Real dollar amount per amount-based issue, via the same reference_id
    # join app/exposure.py already uses -- not a new join pattern.
    amount_by_issue_id: dict[int, float] = {}
    for issue in issues:
        if issue.issue_type in _AMOUNT_BASED_ISSUE_TYPES:
            event = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id, transaction_id=issue.reference_id).first()
            if event and event.amount is not None:
                amount_by_issue_id[issue.id] = abs(event.amount)

    days_overdue_by_issue_id: dict[int, float] = {}
    for issue in issues:
        if issue.issue_type == "BATCH_NOT_SETTLED":
            days_overdue = (issue.details or {}).get("days_overdue")
            if days_overdue is not None:
                days_overdue_by_issue_id[issue.id] = float(days_overdue)

    # Populations to rank against -- each issue_type's own real values,
    # never mixed with another type's (a $500 duplicate payment and a
    # 5-day-overdue batch aren't on the same scale).
    amounts_by_type: dict[str, list[float]] = {}
    for issue in issues:
        if issue.id in amount_by_issue_id:
            amounts_by_type.setdefault(issue.issue_type, []).append(amount_by_issue_id[issue.id])
    days_overdue_population = list(days_overdue_by_issue_id.values())

    result: dict[int, dict[str, Any]] = {}
    for issue in issues:
        if issue.issue_type in _SCORED_ISSUE_TYPES and issue.severity_score is not None:
            score = issue.severity_score
        elif issue.id in amount_by_issue_id:
            score = _percentile_rank(amount_by_issue_id[issue.id], amounts_by_type.get(issue.issue_type, []))
        elif issue.id in days_overdue_by_issue_id:
            score = _percentile_rank(days_overdue_by_issue_id[issue.id], days_overdue_population)
        else:
            # No real dimension to score against (e.g. the join found no
            # matching CanonicalEvent) -- mid-band rather than a silent 0,
            # so a data gap doesn't masquerade as "definitely Low priority".
            score = 50.0
        result[issue.id] = {"severity_score": round(score, 1), "priority_level": _band_for_score(score)}
    return result


def priority_levels_for_breaks(db: Session, tenant_bank_id: str) -> dict[int, dict[str, Any]]:
    """Returns {ReconciliationBreak.id: {"severity_score": float, "priority_level": str}}
    for every break this tenant has.
    """
    breaks = db.query(ReconciliationBreak).filter_by(tenant_bank_id=tenant_bank_id).all()

    def _sizing_amount(brk: ReconciliationBreak) -> float | None:
        value = brk.variance_amount if brk.variance_amount else brk.amount
        return abs(value) if value is not None else None

    population = [a for a in (_sizing_amount(b) for b in breaks) if a is not None]

    result: dict[int, dict[str, Any]] = {}
    for brk in breaks:
        amount = _sizing_amount(brk)
        score = _percentile_rank(amount, population) if amount is not None else 50.0
        if brk.detection_type == "CONFIRMED_BREAK":
            score = max(score, _CONFIRMED_BREAK_SCORE_FLOOR)
        result[brk.id] = {"severity_score": round(score, 1), "priority_level": _band_for_score(score)}
    return result
