"""Step 6c: Reconciliation engine output.

A separate engine from both the fraud/anomaly engine (app/anomaly/) and
Operational Issues (app/operations/) -- see README's Reconciliation
section. Like Operational Issues, this reads the source's own
pre-computed facts directly (reconciliation_status,
reconciliation_variance_amount) -- these are a completed comparison the
source already ran (network-settled amount vs ledger-posted amount),
not a fraud verdict being reverse-engineered, so there's no leakage
concern in reading them.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReconciliationBreak(Base):
    """One row per transaction with a detected reconciliation problem.

    detection_type distinguishes two severities, both confirmed against
    real data before this was built:
    - CONFIRMED_BREAK: the source itself already called this
      reconciliation_status="BREAK". Not every BREAK carries a nonzero
      variance_amount (some are flagged for other reasons -- a missing
      ledger entry, a reference mismatch -- not captured by the amount
      alone), so this is never inferred from variance_amount alone.
    - PROVISIONAL_VARIANCE: the source has NOT yet called this a break
      (reconciliation_status is something else, typically
      "NOT_YET_RECONCILED"), but reconciliation_variance_amount is
      already nonzero -- an early-warning signal ahead of the source's
      own official verdict. Independently checking the raw variance
      number rather than only trusting the status label is the same
      "verify, don't just trust the flag" approach
      detect_duplicate_payments() takes with duplicate_check_status.
    """

    __tablename__ = "reconciliation_breaks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_bank_id = Column(String, nullable=False, index=True)
    transaction_id = Column(String, nullable=False, index=True)
    rail_type = Column(String, nullable=False)

    detection_type = Column(String, nullable=False, index=True)  # "CONFIRMED_BREAK" | "PROVISIONAL_VARIANCE"
    source_reconciliation_status = Column(String, nullable=True)
    variance_amount = Column(Float, nullable=True)
    amount = Column(Float, nullable=True)  # transaction amount, for sizing/context

    details = Column(JSON, nullable=True)  # the raw reconciliation_details JSON from CanonicalEvent
    detected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
