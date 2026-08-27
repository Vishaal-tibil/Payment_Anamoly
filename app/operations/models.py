"""Step 6b: Operational Issues engine -- output contract.

One flat table, one row per detected issue instance, across whichever
issue types are implemented -- keeps everything queryable from one place
for the eventual serving API (Step 8), even though each issue type is
detected by completely different logic (rules.py's deterministic checks
vs. drift.py's rolling z-score).

Only two issue_type values are populated so far: "BATCH_NOT_SETTLED"
(rules.py) and "NETWORK_TIMEOUT_SPIKE" (drift.py). The table stays
general-purpose for future issue types (Duplicate Payment, Formatting
Rejection) -- not built in this pass, deliberately out of scope.
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
    # "NETWORK_TIMEOUT_SPIKE" | "BATCH_NOT_SETTLED" -- see module docstring

    tenant_bank_id = Column(String, nullable=False, index=True)
    reference_type = Column(String, nullable=False)  # "BATCH" | "PARTY"
    reference_id = Column(String, nullable=False, index=True)
    # batch_id for BATCH_NOT_SETTLED, party_id for NETWORK_TIMEOUT_SPIKE

    window_start = Column(DateTime(timezone=True), nullable=True)  # NETWORK_TIMEOUT_SPIKE only
    window_end = Column(DateTime(timezone=True), nullable=True)

    severity_score = Column(Float, nullable=True)  # 0-100 for NETWORK_TIMEOUT_SPIKE; null for BATCH_NOT_SETTLED (binary, not scored)
    details = Column(JSON, nullable=True)

    detected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
