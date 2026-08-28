from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, Integer, String

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentHealthScore(Base):
    """One row per tenant bank -- the current bank-wide Payment Health
    rollup. Recomputed and replaced wholesale on each run (upsert on
    tenant_bank_id), same "compute once, persist, serve many" shape as
    every other engine's output table, just keyed at the tenant level
    instead of per-entity since this is a bank-wide culmination, not a
    per-merchant/per-individual signal (see scoring.py).
    """

    __tablename__ = "payment_health_scores"

    tenant_bank_id = Column(String, primary_key=True)

    health_score = Column(Float, nullable=False)  # 0-100, 100 = healthiest
    health_band = Column(String, nullable=False)  # "Healthy" | "Watch" | "At Risk" | "Critical"

    # Each component is its own 0-100 sub-score so the rollup is never a
    # black box -- a senior viewer (or an analyst) can see exactly which
    # of the three engines is dragging the number down.
    settlement_component = Column(Float, nullable=False)
    anomaly_component = Column(Float, nullable=False)
    operational_component = Column(Float, nullable=False)
    reconciliation_component = Column(Float, nullable=False)

    # The raw counts each component was computed from -- kept alongside
    # the score so the number is always traceable back to real facts,
    # never just a bare score with nothing to check it against.
    total_transactions = Column(Integer, nullable=False, default=0)
    settled_transactions = Column(Integer, nullable=False, default=0)
    total_scored_entities = Column(Integer, nullable=False, default=0)
    critical_anomaly_count = Column(Integer, nullable=False, default=0)
    high_anomaly_count = Column(Integer, nullable=False, default=0)
    operational_issue_count = Column(Integer, nullable=False, default=0)
    reconciliation_break_count = Column(Integer, nullable=False, default=0)

    computed_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
