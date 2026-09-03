"""Investigation Cases -- output contract. See package docstring."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from ..database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InvestigationCase(Base):
    """One row per clustered case. Fully derived from OperationalIssue/
    ReconciliationBreak/EntitySnapshot -- see cases.py's compute_cases().
    Persisted (not recomputed fresh each request) so case_code/opened_at
    stay stable across reruns, unlike this backend's other computed-
    on-request views.
    """

    __tablename__ = "investigation_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_code = Column(String, nullable=False, unique=True, index=True)
    tenant_bank_id = Column(String, nullable=False, index=True)
    category = Column(String, nullable=False, index=True)
    # issue_type / detection_type / fraud-band key this case was clustered on
    payment_rail = Column(String, nullable=True)  # null for party-level rate-spike issues -- no single rail
    title = Column(String, nullable=False)

    current_exposure = Column(Float, nullable=True)
    transactions_affected = Column(Integer, nullable=False, default=0)
    contributing_alerts_count = Column(Integer, nullable=False, default=0)

    # Real peer-relative priority, sourced from app/priority.py's
    # priority_levels_for_issues/priority_levels_for_breaks -- the case's
    # priority is that of its single most severe contributing alert (a
    # case is at least as urgent as its worst constituent). Never a
    # separate heuristic, so this can't disagree with an analyst's own
    # per-item Incidents Centre view. Fraud-sourced alerts (Critical/High
    # anomaly_band -- the only bands compute_cases() ever clusters) use a
    # representative band-ceiling score, since EntitySnapshot has no
    # peer-relative severity_score of its own in this same 0-100 scale.
    severity_score = Column(Float, nullable=True)
    priority_level = Column(String, nullable=True)  # "Critical" | "High" | "Medium" | "Low"
    # Plain-language "why" behind severity_score/priority_level above --
    # see app/investigation/cases.py's _priority_reason(). None only when
    # severity_score/priority_level are also None (no scored alert in the
    # cluster to point to).
    priority_reason = Column(String, nullable=True)

    # Display-only -- see module docstring. PENDING | VALID | INVALID.
    validation_status = Column(String, nullable=False, default="PENDING")

    opened_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class InvestigationCaseAlert(Base):
    """One row per raw signal folded into a case -- powers the Case
    Details page's Alerts tab. Carries tenant_bank_id directly
    (denormalized from its case) so it can be queried/deleted
    independently of InvestigationCase without a join.
    """

    __tablename__ = "investigation_case_alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, ForeignKey("investigation_cases.id"), nullable=False, index=True)
    tenant_bank_id = Column(String, nullable=False, index=True)

    alert_code = Column(String, nullable=False)
    source_type = Column(String, nullable=False)  # "OPERATIONAL_ISSUE" | "RECONCILIATION_BREAK" | "ANOMALY_SNAPSHOT"
    source_id = Column(Integer, nullable=False)

    transaction_id = Column(String, nullable=True)
    payment_rail = Column(String, nullable=True)
    anomaly_category = Column(String, nullable=False)  # "Operational" | "Reconciliation" | "Fraud"
    anomaly_type = Column(String, nullable=False)
    description = Column(String, nullable=False)

    detected_at = Column(DateTime(timezone=True), nullable=False)
