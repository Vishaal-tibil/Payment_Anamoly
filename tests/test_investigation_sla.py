from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.canonical_event_lookup import CanonicalEventLookup
from app.investigation.models import InvestigationCase, InvestigationCaseAlert
from app.investigation.sla import resolve_real_case_anchor, get_cases_approaching_sla
from app.models import CanonicalEvent

_TENANT = "KEYBANK"


def _case(db, **overrides):
    defaults = dict(
        case_code=f"CNO-{overrides.get('id', 1)}", tenant_bank_id=_TENANT, category="CONFIRMED_BREAK",
        payment_rail="ACH", title="Test case", current_exposure=100.0, transactions_affected=1,
        contributing_alerts_count=1, priority_level="Critical", severity_score=90.0,
        validation_status="PENDING", opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    case = InvestigationCase(**defaults)
    db.add(case)
    db.flush()
    return case


def test_ignores_cases_with_plenty_of_real_time_left(db_session):
    case = _case(db_session, priority_level="Low")  # 20-day window
    db_session.add(CanonicalEvent(tenant_bank_id=_TENANT, rail_type="ACH", transaction_id="TXN-1", transaction_occurred_at="2026-08-20T00:00:00Z"))
    db_session.add(InvestigationCaseAlert(
        case_id=case.id, tenant_bank_id=_TENANT, alert_code="ALT-1", source_type="RECONCILIATION_BREAK",
        source_id=1, transaction_id="TXN-1", payment_rail="ACH", anomaly_category="Reconciliation", anomaly_type="Confirmed reconciliation break",
        description="d", detected_at=datetime(2026, 9, 1, tzinfo=timezone.utc),  # batch artifact -- must be ignored
    ))
    db_session.commit()

    result = get_cases_approaching_sla(db_session, _TENANT)

    assert result["cases"] == []
    assert result["urgent_count"] == 0


def test_anchor_uses_real_transaction_time_not_opened_at_batch_artifact(db_session):
    # opened_at claims 2026-09-02 (a batch-compute-run artifact) -- the
    # real anchor must come from the alert's real transaction_occurred_at
    # (2026-08-01) instead, a full month earlier.
    case = _case(db_session, priority_level="Critical", opened_at=datetime(2026, 9, 2, tzinfo=timezone.utc))
    db_session.add(CanonicalEvent(tenant_bank_id=_TENANT, rail_type="ACH", transaction_id="TXN-1", transaction_occurred_at="2026-08-01T00:00:00Z"))
    alert = InvestigationCaseAlert(
        case_id=case.id, tenant_bank_id=_TENANT, alert_code="ALT-1", source_type="RECONCILIATION_BREAK",
        source_id=1, transaction_id="TXN-1", payment_rail="ACH", anomaly_category="Reconciliation", anomaly_type="Confirmed reconciliation break",
        description="d", detected_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    db_session.add(alert)
    db_session.commit()

    anchor = resolve_real_case_anchor(case, [alert], CanonicalEventLookup(db_session, _TENANT))

    assert anchor == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_marks_overdue_case_correctly(db_session):
    old_case = _case(db_session, id=1, priority_level="Critical")  # 2-day window
    recent_case = _case(db_session, id=2, priority_level="Critical")
    db_session.add(CanonicalEvent(tenant_bank_id=_TENANT, rail_type="ACH", transaction_id="TXN-OLD", transaction_occurred_at="2026-08-01T00:00:00Z"))
    db_session.add(CanonicalEvent(tenant_bank_id=_TENANT, rail_type="ACH", transaction_id="TXN-NEW", transaction_occurred_at="2026-08-10T00:00:00Z"))
    db_session.add(InvestigationCaseAlert(
        case_id=old_case.id, tenant_bank_id=_TENANT, alert_code="A1", source_type="RECONCILIATION_BREAK",
        source_id=1, transaction_id="TXN-OLD", payment_rail="ACH", anomaly_category="Reconciliation", anomaly_type="Confirmed reconciliation break",
        description="d", detected_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    ))
    db_session.add(InvestigationCaseAlert(
        case_id=recent_case.id, tenant_bank_id=_TENANT, alert_code="A2", source_type="RECONCILIATION_BREAK",
        source_id=2, transaction_id="TXN-NEW", payment_rail="ACH", anomaly_category="Reconciliation", anomaly_type="Confirmed reconciliation break",
        description="d", detected_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    ))
    db_session.commit()

    result = get_cases_approaching_sla(db_session, _TENANT)

    # reference_now = max real anchor = 2026-08-10 (recent_case's real event).
    # old_case is 9 real days before that -- way past its 2-day Critical window -> overdue.
    by_code = {c["case_code"]: c for c in result["cases"]}
    assert by_code["CNO-1"]["status"] == "overdue"
    assert result["urgent_count"] == 1


def test_falls_back_to_opened_at_for_party_level_issue_with_no_transaction_id(db_session):
    # Party-level issue (no transaction_id, not fraud) -- no better real
    # anchor exists, so this case's opened_at (2026-08-01) is used as-is.
    old_case = _case(db_session, id=1, priority_level="Critical", opened_at=datetime(2026, 8, 1, tzinfo=timezone.utc))
    db_session.add(InvestigationCaseAlert(
        case_id=old_case.id, tenant_bank_id=_TENANT, alert_code="ALT-1", source_type="OPERATIONAL_ISSUE",
        source_id=1, transaction_id=None, anomaly_category="Operational", anomaly_type="Failure-rate spike",
        description="d", detected_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    ))
    # A second, more recent real case establishes a reference point later
    # than 2026-08-01, so the fallback-anchored case reads as overdue.
    recent_case = _case(db_session, id=2, priority_level="Critical")
    db_session.add(CanonicalEvent(tenant_bank_id=_TENANT, rail_type="ACH", transaction_id="TXN-NEW", transaction_occurred_at="2026-08-10T00:00:00Z"))
    db_session.add(InvestigationCaseAlert(
        case_id=recent_case.id, tenant_bank_id=_TENANT, alert_code="ALT-2", source_type="RECONCILIATION_BREAK",
        source_id=2, transaction_id="TXN-NEW", payment_rail="ACH", anomaly_category="Reconciliation", anomaly_type="Confirmed reconciliation break",
        description="d", detected_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    ))
    db_session.commit()

    result = get_cases_approaching_sla(db_session, _TENANT)

    by_code = {c["case_code"]: c for c in result["cases"]}
    assert by_code["CNO-1"]["status"] == "overdue"


def test_no_pending_cases_returns_empty(db_session):
    _case(db_session, validation_status="VALID")
    db_session.commit()

    result = get_cases_approaching_sla(db_session, _TENANT)

    assert result == {"cases": [], "urgent_count": 0}
