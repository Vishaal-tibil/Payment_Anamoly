from __future__ import annotations

from datetime import datetime, timezone

from app.investigation.models import InvestigationCase
from app.investigation.patterns import get_ai_identified_patterns
from app.models import CanonicalEvent
from app.reconciliation.models import ReconciliationBreak

_TENANT = "KEYBANK"


def _case(db, **overrides):
    defaults = dict(
        case_code=f"CNO-{overrides.get('id', 1)}", tenant_bank_id=_TENANT, category="CONFIRMED_BREAK",
        payment_rail="ACH", title="Test case", current_exposure=100.0, transactions_affected=1,
        contributing_alerts_count=1, priority_level="Critical",
        validation_status="PENDING", opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return InvestigationCase(**defaults)


def _event(db, **overrides):
    defaults = dict(tenant_bank_id=_TENANT, rail_type="ACH", transaction_id="TXN-1", amount=100.0)
    defaults.update(overrides)
    db.add(CanonicalEvent(**defaults))


def test_excludes_categories_with_no_real_trend_baseline(db_session):
    # Only one real event -> category_weekly_trend() returns None (< 2 weeks).
    _event(db_session, transaction_id="TXN-1", transaction_occurred_at="2026-08-01T00:00:00Z")
    db_session.add(ReconciliationBreak(tenant_bank_id=_TENANT, transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    db_session.add(_case(db_session, category="CONFIRMED_BREAK", payment_rail="ACH", current_exposure=50.0))
    db_session.commit()

    result = get_ai_identified_patterns(db_session, _TENANT)

    assert result["patterns"] == []
    assert result["total"] == 0


def test_includes_pattern_with_real_trend_and_real_exposure(db_session):
    # Week Mondays: 2026-08-03, 2026-08-10, 2026-08-17 -- counts 1, 1, 3 ->
    # clearly above the prior average -> "increasing".
    for i, occurred_at in enumerate(["2026-08-03T00:00:00Z", "2026-08-10T00:00:00Z", "2026-08-17T00:00:00Z", "2026-08-18T00:00:00Z", "2026-08-19T00:00:00Z"]):
        txn_id = f"TXN-{i}"
        _event(db_session, transaction_id=txn_id, transaction_occurred_at=occurred_at)
        db_session.add(ReconciliationBreak(tenant_bank_id=_TENANT, transaction_id=txn_id, rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    db_session.add(_case(db_session, id=1, category="CONFIRMED_BREAK", payment_rail="ACH", current_exposure=500.0, priority_level="Critical"))
    db_session.commit()

    result = get_ai_identified_patterns(db_session, _TENANT)

    assert result["total"] == 1
    pattern = result["patterns"][0]
    assert pattern["title"] == "ACH Confirmed reconciliation break"
    assert pattern["direction"] == "increasing"
    assert pattern["vs_baseline_percent"] > 0
    assert pattern["exposure"] == 500.0
    assert pattern["severity"] == "Critical"
    assert pattern["case_count"] == 1


def test_exposure_sums_across_multiple_cases_in_same_category_rail(db_session):
    for i, occurred_at in enumerate(["2026-08-01T00:00:00Z", "2026-08-08T00:00:00Z"]):
        txn_id = f"TXN-{i}"
        _event(db_session, transaction_id=txn_id, transaction_occurred_at=occurred_at)
        db_session.add(ReconciliationBreak(tenant_bank_id=_TENANT, transaction_id=txn_id, rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    db_session.add(_case(db_session, id=1, category="CONFIRMED_BREAK", payment_rail="ACH", current_exposure=100.0, priority_level="Medium"))
    db_session.add(_case(db_session, id=2, category="CONFIRMED_BREAK", payment_rail="ACH", current_exposure=200.0, priority_level="Critical"))
    db_session.commit()

    result = get_ai_identified_patterns(db_session, _TENANT)

    pattern = result["patterns"][0]
    assert pattern["exposure"] == 300.0
    assert pattern["severity"] == "Critical"  # worst across both cases
    assert pattern["case_count"] == 2
