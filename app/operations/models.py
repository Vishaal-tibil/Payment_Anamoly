"""Step 6b: Operational Issues engine output.

A different engine from the fraud/anomaly one (app/anomaly/) -- see the
README's Operational Issues section for the full contract. The key
difference to keep in mind while reading this module: the fraud engine
has a hard rule against reading the source's own pre-computed risk
flags as input (that would leak the answer). That rule does NOT apply
here -- network_timeout_flag, file_reached_settlement,
duplicate_check_status, format_validation_status, etc. are operational
facts, not fraud verdicts being reverse-engineered, and reading them
directly is the entire point of this engine.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OperationalIssue(Base):
    """One row per detected issue instance, across all four issue types
    (Network/Processor Timeout, Batch Never Settles, Duplicate Payment,
    Formatting Rejection) -- a single flat table so anything downstream
    (Step 8's serving API) can query every operational issue for a
    tenant from one place, even though the four types are detected by
    different logic (two deterministic rule/join checks, two rolling
    z-score checks reusing Track A's EntitySnapshot rates).
    """

    __tablename__ = "operational_issues"

    id = Column(Integer, primary_key=True, autoincrement=True)
    issue_type = Column(String, nullable=False, index=True)
    # "NETWORK_TIMEOUT_SPIKE" | "BATCH_NOT_SETTLED" | "DUPLICATE_PAYMENT" |
    # "FORMAT_REJECTION" | "FORMAT_REJECTION_SPIKE"

    tenant_bank_id = Column(String, nullable=False, index=True)
    reference_type = Column(String, nullable=False)  # "TRANSACTION" | "BATCH" | "PARTY"
    reference_id = Column(String, nullable=False, index=True)
    # transaction_id for duplicates/format rejections, batch_id for stuck
    # batches, party_id for rate-spike issues (timeout/format rejection rate)

    window_start = Column(DateTime(timezone=True), nullable=True)  # rate-based issues only
    window_end = Column(DateTime(timezone=True), nullable=True)

    severity_score = Column(Float, nullable=True)  # 0-100 for the two z-score issues; null for the deterministic ones (binary, not scored)
    details = Column(JSON, nullable=True)

    detected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
