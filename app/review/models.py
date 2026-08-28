from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, UniqueConstraint

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


PENDING = "PENDING"
CONFIRMED = "CONFIRMED"
DISMISSED = "DISMISSED"
STATUSES = (PENDING, CONFIRMED, DISMISSED)


class AnalystReview(Base):
    """One row per (signal_type, tenant_bank_id, reference_id) -- the
    review status of one detected claim. Rows are created lazily on the
    first review action; a signal with no row here is implicitly
    PENDING (see review/service.py's get_review()), so this table only
    ever holds claims someone has actually acted on plus whatever a
    listing endpoint needs -- not a pre-seeded row for every detection.
    """

    __tablename__ = "analyst_reviews"
    __table_args__ = (
        UniqueConstraint("signal_type", "reference_id", "tenant_bank_id", name="uq_analyst_review_signal"),
    )

    id = Column(String, primary_key=True)  # f"{signal_type}:{tenant_bank_id}:{reference_id}"
    signal_type = Column(String, nullable=False, index=True)  # "operational_issue" | "reconciliation_break" | "fraud_anomaly"
    reference_id = Column(String, nullable=False)
    tenant_bank_id = Column(String, nullable=False, index=True)

    status = Column(String, nullable=False, default=PENDING, index=True)
    reviewed_by = Column(String, nullable=True)  # analyst identifier/email; null while PENDING
    notes = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
