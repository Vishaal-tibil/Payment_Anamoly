"""Step 6b: Operational Issues engine -- output contract.

One flat table, one row per detected issue instance, across whichever
issue types are implemented -- keeps everything queryable from one place
for the eventual serving API (Step 8), even though each issue type is
detected by completely different logic (rules.py's deterministic checks,
drift.py's rolling z-score, duplicate_payment.py's exact-key join,
format_rejection.py's plain filter).

Four issue_type values are populated: "BATCH_NOT_SETTLED" (rules.py),
"NETWORK_TIMEOUT_SPIKE" (drift.py), "DUPLICATE_PAYMENT"
(duplicate_payment.py), "FORMAT_REJECTION" (format_rejection.py -- the
listing half only; a statistical "is the reject RATE spiking" half is a
separate future enhancement, not built here).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OperationalIssue(Base):
    __tablename__ = "operational_issues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    issue_type = Column(String, nullable=False, index=True)
    # "NETWORK_TIMEOUT_SPIKE" | "BATCH_NOT_SETTLED" | "DUPLICATE_PAYMENT" | "FORMAT_REJECTION"

    tenant_bank_id = Column(String, nullable=False, index=True)
    reference_type = Column(String, nullable=False)  # "BATCH" | "PARTY" | "TRANSACTION"
    reference_id = Column(String, nullable=False, index=True)
    # batch_id for BATCH_NOT_SETTLED, party_id for NETWORK_TIMEOUT_SPIKE,
    # transaction_id for DUPLICATE_PAYMENT/FORMAT_REJECTION

    window_start = Column(DateTime(timezone=True), nullable=True)  # NETWORK_TIMEOUT_SPIKE only
    window_end = Column(DateTime(timezone=True), nullable=True)

    severity_score = Column(Float, nullable=True)  # 0-100 for NETWORK_TIMEOUT_SPIKE; null for the other three (binary/listing, not scored)
    details = Column(JSON, nullable=True)

    detected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
