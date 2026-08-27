"""Step 7 output: cached LLM narratives.

Generating a narrative costs a real API call (money + latency), so
each (signal_type, reference_id) pair is generated once and cached
here -- same "compute once, persist, serve many" shape as every other
engine in this platform, not a special case for this one.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, UniqueConstraint

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentNarrative(Base):
    __tablename__ = "agent_narratives"
    __table_args__ = (
        UniqueConstraint("signal_type", "reference_id", "tenant_bank_id", name="uq_agent_narrative_signal"),
    )

    id = Column(String, primary_key=True)  # f"{signal_type}:{tenant_bank_id}:{reference_id}"
    signal_type = Column(String, nullable=False, index=True)  # "operational_issue" | "reconciliation_break" | "fraud_anomaly"
    reference_id = Column(String, nullable=False)  # the source row's own id, as a string
    tenant_bank_id = Column(String, nullable=False, index=True)

    title = Column(String, nullable=False)
    description = Column(String, nullable=False)
    recommended_action_title = Column(String, nullable=False)
    recommended_action_description = Column(String, nullable=False)

    model = Column(String, nullable=False)  # which Mistral model produced this, for auditability
    generated_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
