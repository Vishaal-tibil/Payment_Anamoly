from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.anomaly.models import EntitySnapshot
from app.dashboard import get_detection_performance, get_overview, get_rail_stats
from app.models import CanonicalEvent, Individual, Merchant
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak
from app.review.service import set_review


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="ACH",
        transaction_id="TXN-1",
        amount=100.0,
        status="SETTLED",
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_overview_counts_transactions_and_settlement_rate(db_session):
    _make_event(db_session, transaction_id="TXN-1", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-2", status="PENDING")

    result = get_overview(db_session, "KEYBANK")

    assert result["total_transactions"] == 2
    assert result["settled_transactions"] == 1
    assert result["settlement_rate"] == 0.5


def test_overview_with_no_transactions_has_null_settlement_rate(db_session):
    result = get_overview(db_session, "KEYBANK")

    assert result["total_transactions"] == 0
    assert result["settlement_rate"] is None
    assert result["date_range_start"] is None
    assert result["date_range_end"] is None


def test_overview_computes_real_date_range(db_session):
    _make_event(db_session, transaction_id="TXN-1", transaction_occurred_at="2026-06-01T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-2", transaction_occurred_at="2026-08-15T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-3", transaction_occurred_at="2026-07-01T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-4", transaction_occurred_at=None)  # must not break MIN/MAX

    result = get_overview(db_session, "KEYBANK")

    assert result["date_range_start"] == "2026-06-01T10:00:00Z"
    assert result["date_range_end"] == "2026-08-15T10:00:00Z"


def test_overview_counts_merchants_and_individuals(db_session):
    db_session.add(Merchant(merchant_id="MER-1", source_merchant_id="S-1", tenant_bank_id="KEYBANK"))
    db_session.add(Individual(individual_id="IND-1", source_individual_id="S-2", tenant_bank_id="KEYBANK"))
    db_session.commit()

    result = get_overview(db_session, "KEYBANK")

    assert result["total_merchants"] == 1
    assert result["total_individuals"] == 1


def test_overview_aggregates_anomaly_bands(db_session):
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Critical",
    ))
    db_session.add(EntitySnapshot(
        party_id="MER-2", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Normal",
    ))
    db_session.add(EntitySnapshot(
        party_id="MER-3", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band=None,  # not yet scored
    ))
    db_session.commit()

    result = get_overview(db_session, "KEYBANK")

    assert result["anomaly_band_counts"] == {"Critical": 1, "Normal": 1}  # unscored row excluded, not miscounted


def test_overview_aggregates_operational_issues_and_reconciliation_breaks(db_session):
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-1"))
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-2"))
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-3", rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    db_session.commit()

    result = get_overview(db_session, "KEYBANK")

    assert result["operational_issue_counts"] == {"DUPLICATE_PAYMENT": 2}
    assert result["reconciliation_break_counts"] == {"CONFIRMED_BREAK": 1}


def test_overview_tenant_isolation(db_session):
    _make_event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1")
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2")

    result = get_overview(db_session, "KEYBANK")

    assert result["total_transactions"] == 1


def test_rail_stats_groups_by_real_rail_types(db_session):
    _make_event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=100.0, status="SETTLED")
    _make_event(db_session, transaction_id="TXN-2", rail_type="ACH", amount=200.0, status="PENDING")
    _make_event(db_session, transaction_id="TXN-3", rail_type="WIRE", amount=5000.0, status="SETTLED")

    result = get_rail_stats(db_session, "KEYBANK")

    by_rail = {r["rail_type"]: r for r in result["rails"]}
    assert set(by_rail.keys()) == {"ACH", "WIRE"}
    assert by_rail["ACH"]["transaction_count"] == 2
    assert by_rail["ACH"]["settled_count"] == 1
    assert by_rail["ACH"]["settlement_rate"] == 0.5
    assert by_rail["ACH"]["total_amount"] == 300.0
    assert by_rail["WIRE"]["transaction_count"] == 1
    assert by_rail["WIRE"]["total_amount"] == 5000.0


def test_rail_stats_includes_reconciliation_break_counts(db_session):
    _make_event(db_session, transaction_id="TXN-1", rail_type="ACH")
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    db_session.commit()

    result = get_rail_stats(db_session, "KEYBANK")

    ach = next(r for r in result["rails"] if r["rail_type"] == "ACH")
    assert ach["reconciliation_break_count"] == 1


def test_rail_stats_tenant_isolation(db_session):
    _make_event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH")
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2", rail_type="WIRE")

    result = get_rail_stats(db_session, "KEYBANK")

    assert len(result["rails"]) == 1
    assert result["rails"][0]["rail_type"] == "ACH"


def test_detection_performance_coverage_counts_resolved_transactions_only(db_session):
    _make_event(db_session, transaction_id="TXN-1", merchant_id="MER-1")
    _make_event(db_session, transaction_id="TXN-2", merchant_id=None, individual_id=None)  # unresolved

    result = get_detection_performance(db_session, "KEYBANK")

    assert result["total_transactions"] == 2
    assert result["covered_transactions"] == 1
    assert result["coverage_rate"] == 0.5


def test_detection_performance_coverage_by_rail(db_session):
    _make_event(db_session, transaction_id="TXN-1", rail_type="ACH", merchant_id="MER-1")
    _make_event(db_session, transaction_id="TXN-2", rail_type="WIRE", merchant_id=None)

    result = get_detection_performance(db_session, "KEYBANK")

    by_rail = {r["rail_type"]: r["coverage_rate"] for r in result["coverage_by_rail"]}
    assert by_rail["ACH"] == 1.0
    assert by_rail["WIRE"] == 0.0


def test_detection_performance_exposure_identified_early_sums_provisional_variances(db_session):
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="PROVISIONAL_VARIANCE", amount=500.0))
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=9999.0))
    db_session.commit()

    result = get_detection_performance(db_session, "KEYBANK")

    assert result["exposure_identified_early"] == 500.0  # CONFIRMED_BREAK excluded -- not "caught early"
    assert result["provisional_variance_count"] == 1


def test_detection_performance_confirmation_and_false_positive_rate_from_real_reviews(db_session):
    db_session.add(OperationalIssue(id=1, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-A"))
    db_session.add(OperationalIssue(id=2, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-B"))
    db_session.add(OperationalIssue(id=3, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-C"))
    db_session.commit()
    set_review(db_session, "operational_issue", "1", "KEYBANK", "CONFIRMED")
    set_review(db_session, "operational_issue", "2", "KEYBANK", "CONFIRMED")
    set_review(db_session, "operational_issue", "3", "KEYBANK", "DISMISSED")

    result = get_detection_performance(db_session, "KEYBANK")

    assert result["reviewed_count"] == 3
    assert result["confirmation_rate"] == pytest.approx(2 / 3)
    assert result["false_positive_rate"] == pytest.approx(1 / 3)


def test_detection_performance_rates_are_null_with_no_reviews_yet(db_session):
    result = get_detection_performance(db_session, "KEYBANK")

    assert result["reviewed_count"] == 0
    assert result["confirmation_rate"] is None
    assert result["false_positive_rate"] is None
