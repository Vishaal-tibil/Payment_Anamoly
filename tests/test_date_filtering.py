from __future__ import annotations

from datetime import date, datetime, timezone

from app.anomaly.models import EntitySnapshot
from app.dashboard import get_detection_performance, get_overview, get_rail_stats
from app.date_filter import date_in_range, datetime_bounds, occurred_at_bounds
from app.exposure import get_exposure_by_domain, get_mitigation_progress, get_payment_normalcy, get_payment_value_by_rail
from app.models import CanonicalEvent
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak
from app.review.service import set_review


def _event(db, **overrides):
    defaults = dict(tenant_bank_id="KEYBANK", rail_type="ACH", status="SETTLED", amount=100.0)
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    return event


# -- date_filter.py unit tests -----------------------------------------------


def test_occurred_at_bounds_end_date_is_inclusive_of_whole_day():
    lower, upper = occurred_at_bounds(date(2026, 8, 1), date(2026, 8, 7))
    assert lower == "2026-08-01"
    assert upper == "2026-08-08"  # exclusive next-day boundary
    assert "2026-08-07T23:59:59" < upper


def test_occurred_at_bounds_one_sided():
    assert occurred_at_bounds(date(2026, 8, 1), None) == ("2026-08-01", None)
    assert occurred_at_bounds(None, date(2026, 8, 7)) == (None, "2026-08-08")
    assert occurred_at_bounds(None, None) == (None, None)


def test_datetime_bounds_end_date_is_inclusive_of_whole_day():
    lower, upper = datetime_bounds(date(2026, 8, 1), date(2026, 8, 7))
    assert lower == datetime(2026, 8, 1)
    assert upper == datetime(2026, 8, 8)


def test_date_in_range_no_filter_active_always_true():
    assert date_in_range(None, None, None) is True
    assert date_in_range(date(2026, 1, 1), None, None) is True


def test_date_in_range_excludes_unknown_date_once_filter_active():
    assert date_in_range(None, date(2026, 8, 1), date(2026, 8, 7)) is False


def test_date_in_range_boundaries_inclusive():
    assert date_in_range(date(2026, 8, 1), date(2026, 8, 1), date(2026, 8, 7)) is True
    assert date_in_range(date(2026, 8, 7), date(2026, 8, 1), date(2026, 8, 7)) is True
    assert date_in_range(date(2026, 7, 31), date(2026, 8, 1), date(2026, 8, 7)) is False
    assert date_in_range(date(2026, 8, 8), date(2026, 8, 1), date(2026, 8, 7)) is False


def test_date_in_range_accepts_datetime_values():
    assert date_in_range(datetime(2026, 8, 3, 23, 59), date(2026, 8, 1), date(2026, 8, 7)) is True


# -- get_overview -------------------------------------------------------------


def test_overview_start_end_date_narrows_transaction_counts(db_session):
    _event(db_session, transaction_id="TXN-1", transaction_occurred_at="2026-08-03T10:00:00Z", status="SETTLED")
    _event(db_session, transaction_id="TXN-2", transaction_occurred_at="2026-09-01T10:00:00Z", status="SETTLED")
    db_session.commit()

    result = get_overview(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    assert result["total_transactions"] == 1
    assert result["settled_transactions"] == 1


def test_overview_date_range_start_end_stay_unfiltered(db_session):
    _event(db_session, transaction_id="TXN-1", transaction_occurred_at="2026-06-01T10:00:00Z")
    _event(db_session, transaction_id="TXN-2", transaction_occurred_at="2026-09-01T10:00:00Z")
    db_session.commit()

    result = get_overview(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    # The real full addressable range, not narrowed to the selection --
    # it's what the UI's own date picker bounds itself to.
    assert result["date_range_start"] == "2026-06-01T10:00:00Z"
    assert result["date_range_end"] == "2026-09-01T10:00:00Z"


def test_overview_operational_issue_counts_scope_by_underlying_transaction_date(db_session):
    _event(db_session, transaction_id="TXN-1", transaction_occurred_at="2026-08-03T10:00:00Z")
    _event(db_session, transaction_id="TXN-2", transaction_occurred_at="2026-09-01T10:00:00Z")
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-1"))
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-2"))
    db_session.commit()

    result = get_overview(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    assert result["operational_issue_counts"] == {"DUPLICATE_PAYMENT": 1}


def test_overview_reconciliation_break_counts_scope_by_underlying_transaction_date(db_session):
    _event(db_session, transaction_id="TXN-1", transaction_occurred_at="2026-08-03T10:00:00Z")
    _event(db_session, transaction_id="TXN-2", transaction_occurred_at="2026-09-01T10:00:00Z")
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    db_session.commit()

    result = get_overview(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    assert result["reconciliation_break_counts"] == {"CONFIRMED_BREAK": 1}


def test_overview_anomaly_band_counts_scope_by_window_end(db_session):
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 8, 3, tzinfo=timezone.utc), anomaly_band="Critical",
    ))
    db_session.add(EntitySnapshot(
        party_id="MER-2", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 9, 1, tzinfo=timezone.utc), anomaly_band="Critical",
    ))
    db_session.commit()

    result = get_overview(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    assert result["anomaly_band_counts"] == {"Critical": 1}


def test_overview_rate_based_spike_issue_scoped_by_own_window(db_session):
    db_session.add(OperationalIssue(
        issue_type="FORMAT_REJECTION_SPIKE", tenant_bank_id="KEYBANK", reference_type="PARTY", reference_id="MER-1",
        window_start=datetime(2026, 8, 1, tzinfo=timezone.utc), window_end=datetime(2026, 8, 7, tzinfo=timezone.utc),
    ))
    db_session.commit()

    in_range = get_overview(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))
    out_of_range = get_overview(db_session, "KEYBANK", start_date=date(2026, 9, 1), end_date=date(2026, 9, 7))

    assert in_range["operational_issue_counts"] == {"FORMAT_REJECTION_SPIKE": 1}
    assert out_of_range["operational_issue_counts"] == {}


# -- get_rail_stats -------------------------------------------------------------


def test_rail_stats_narrows_by_date(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", transaction_occurred_at="2026-08-03T10:00:00Z")
    _event(db_session, transaction_id="TXN-2", rail_type="ACH", transaction_occurred_at="2026-09-01T10:00:00Z")
    db_session.commit()

    result = get_rail_stats(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    ach = next(r for r in result["rails"] if r["rail_type"] == "ACH")
    assert ach["transaction_count"] == 1


# -- get_detection_performance ---------------------------------------------


def test_detection_performance_coverage_scopes_by_date(db_session):
    _event(db_session, transaction_id="TXN-1", merchant_id="MER-1", transaction_occurred_at="2026-08-03T10:00:00Z")
    _event(db_session, transaction_id="TXN-2", merchant_id="MER-1", transaction_occurred_at="2026-09-01T10:00:00Z")
    db_session.commit()

    result = get_detection_performance(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    assert result["total_transactions"] == 1
    assert result["covered_transactions"] == 1


def test_detection_performance_confirmation_rate_not_scoped_by_date(db_session):
    """Review timing is a different real timeline than transaction date --
    see get_detection_performance's own docstring for why this stays
    unfiltered.
    """
    db_session.add(OperationalIssue(id=1, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-A"))
    db_session.commit()
    set_review(db_session, "operational_issue", "1", "KEYBANK", "CONFIRMED")

    unfiltered = get_detection_performance(db_session, "KEYBANK")
    filtered = get_detection_performance(db_session, "KEYBANK", start_date=date(2026, 1, 1), end_date=date(2026, 1, 7))

    assert unfiltered["confirmation_rate"] == filtered["confirmation_rate"] == 1.0


# -- app/exposure.py ----------------------------------------------------------


def test_exposure_by_domain_narrows_by_date(db_session):
    _event(db_session, transaction_id="TXN-1", amount=500.0, transaction_occurred_at="2026-08-03T00:00:00Z")
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    _event(db_session, transaction_id="TXN-2", amount=700.0, transaction_occurred_at="2026-09-01T00:00:00Z")
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=700.0))
    db_session.commit()

    result = get_exposure_by_domain(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    by_id = {d["id"]: d["amount"] for d in result["domains"]}
    assert by_id["reconciliation_break"] == 500.0
    assert result["total"] == 500.0


def test_exposure_by_domain_fraud_anomaly_scopes_by_window_end(db_session):
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 8, 3, tzinfo=timezone.utc), anomaly_band="Critical", amount_total=1000.0,
    ))
    db_session.add(EntitySnapshot(
        party_id="MER-2", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 9, 1, tzinfo=timezone.utc), anomaly_band="Critical", amount_total=2000.0,
    ))
    db_session.commit()

    result = get_exposure_by_domain(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    by_id = {d["id"]: d["amount"] for d in result["domains"]}
    assert by_id["fraud_anomaly"] == 1000.0


def test_mitigation_progress_narrows_by_date(db_session):
    _event(db_session, transaction_id="TXN-1", amount=500.0, transaction_occurred_at="2026-08-03T00:00:00Z")
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    db_session.commit()
    set_review(db_session, "reconciliation_break", "1", "KEYBANK", "CONFIRMED")

    in_range = get_mitigation_progress(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))
    out_of_range = get_mitigation_progress(db_session, "KEYBANK", start_date=date(2026, 9, 1), end_date=date(2026, 9, 7))

    assert in_range["total"] == 500.0
    assert in_range["mitigated"] == 500.0
    assert out_of_range["total"] == 0.0


def test_payment_value_by_rail_narrows_by_date(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=1000.0, transaction_occurred_at="2026-08-03T00:00:00Z")
    _event(db_session, transaction_id="TXN-2", rail_type="ACH", amount=2000.0, transaction_occurred_at="2026-09-01T00:00:00Z")
    db_session.commit()

    result = get_payment_value_by_rail(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    ach = next(r for r in result["rails"] if r["rail_type"] == "ACH")
    assert ach["total_amount"] == 1000.0


def test_payment_normalcy_narrows_by_date_with_consistent_numerator_and_denominator(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", transaction_occurred_at="2026-08-03T00:00:00Z")
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    _event(db_session, transaction_id="TXN-2", rail_type="ACH", transaction_occurred_at="2026-08-04T00:00:00Z")
    _event(db_session, transaction_id="TXN-3", rail_type="ACH", transaction_occurred_at="2026-09-01T00:00:00Z")
    db_session.commit()

    result = get_payment_normalcy(db_session, "KEYBANK", start_date=date(2026, 8, 1), end_date=date(2026, 8, 7))

    assert result["total_transactions"] == 2
    assert result["touched_transactions"] == 1
    assert result["rate"] == 0.5
