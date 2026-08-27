"""Formatting Rejection: deterministic listing (every rejected
transaction, no scoring needed -- format_validation_status is a fact)
plus rolling z-score for the "is the reject RATE spiking" half (only
piece that needs statistics).

The spike half reuses Track A's EntitySnapshot.format_reject_ratio
(already computed per merchant per week) rather than re-aggregating
canonical_events from scratch, and reuses Track C's z-score core
algorithm (_score_sequence) rather than reimplementing it -- per the
README's explicit direction. Reading EntitySnapshot here is fine (this
engine only ever writes to OperationalIssue, never to EntitySnapshot),
same as clustering.py/timeseries.py read it without owning it.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from ..anomaly.models import EntitySnapshot
from ..anomaly.timeseries import _score_sequence
from ..models import CanonicalEvent
from .models import OperationalIssue

_REJECTED_STATUS = "FAILED"

_FORMAT_REJECTION_FEATURES = ("format_reject_ratio",)

# Only worth a row in OperationalIssue once a merchant's reject rate is
# meaningfully elevated versus its own history, not on every computed
# week (most weeks are 0.0 -- writing a row for those would flood the
# table with non-issues). 60.0 matches the fraud engine's own "High"
# band cutoff -- a score below that is closer to routine noise than an
# actual operational problem worth a human's attention.
_SPIKE_SEVERITY_THRESHOLD = 60.0


def list_format_rejections(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds OperationalIssue rows of type FORMAT_REJECTION -- one row
    per transaction whose format_validation_status indicates a reject.
    Deterministic: format_validation_status is a fact, not a judgment
    call, so there's nothing to score here, just list them.
    """
    query = db.query(CanonicalEvent).filter(CanonicalEvent.format_validation_status == _REJECTED_STATUS)
    if tenant_bank_id:
        query = query.filter(CanonicalEvent.tenant_bank_id == tenant_bank_id)
    rejected = query.all()

    delete_query = db.query(OperationalIssue).filter(OperationalIssue.issue_type == "FORMAT_REJECTION")
    if tenant_bank_id:
        delete_query = delete_query.filter(OperationalIssue.tenant_bank_id == tenant_bank_id)
    delete_query.delete(synchronize_session=False)

    for event in rejected:
        db.add(OperationalIssue(
            issue_type="FORMAT_REJECTION",
            tenant_bank_id=event.tenant_bank_id,
            reference_type="TRANSACTION",
            reference_id=event.transaction_id,
            severity_score=None,
            details={"format_validation_errors": event.format_validation_errors},
        ))

    db.commit()

    return {"rejections_listed": len(rejected)}


def score_format_rejection_drift(db: Session, tenant_bank_id: str | None = None) -> dict[str, Any]:
    """Rebuilds OperationalIssue rows of type FORMAT_REJECTION_SPIKE --
    one row per (merchant, week) where format_reject_ratio drifted
    meaningfully above that same merchant's own recent history, per the
    same z-score-against-own-baseline approach as everything else in
    this platform.
    """
    query = db.query(EntitySnapshot).filter(
        EntitySnapshot.party_type == "MERCHANT",
        EntitySnapshot.window_type == "WEEKLY",
    )
    if tenant_bank_id:
        query = query.filter(EntitySnapshot.tenant_bank_id == tenant_bank_id)
    rows = query.all()

    delete_query = db.query(OperationalIssue).filter(OperationalIssue.issue_type == "FORMAT_REJECTION_SPIKE")
    if tenant_bank_id:
        delete_query = delete_query.filter(OperationalIssue.tenant_bank_id == tenant_bank_id)
    delete_query.delete(synchronize_session=False)

    by_party: dict[str, list[EntitySnapshot]] = defaultdict(list)
    for row in rows:
        by_party[row.party_id].append(row)

    weeks_scored = 0
    flagged = 0
    for party_id, party_rows in by_party.items():
        rows_sorted = sorted(party_rows, key=lambda r: r.window_start)
        scores = _score_sequence(rows_sorted, _FORMAT_REJECTION_FEATURES)
        for row, score in zip(rows_sorted, scores):
            if score is None:
                continue
            weeks_scored += 1
            if score < _SPIKE_SEVERITY_THRESHOLD:
                continue
            flagged += 1
            db.add(OperationalIssue(
                issue_type="FORMAT_REJECTION_SPIKE",
                tenant_bank_id=row.tenant_bank_id,
                reference_type="PARTY",
                reference_id=party_id,
                window_start=row.window_start,
                window_end=row.window_end,
                severity_score=score,
                details={"format_reject_ratio": row.format_reject_ratio},
            ))

    db.commit()

    return {
        "weeks_scored": weeks_scored,
        "spikes_flagged": flagged,
    }
