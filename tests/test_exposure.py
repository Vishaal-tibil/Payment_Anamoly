from __future__ import annotations

from datetime import datetime, timezone

from app.anomaly.models import EntitySnapshot
from app.exposure import (
    get_exposure_by_domain,
    get_exposure_trend,
    get_mitigation_progress,
    get_mitigation_progress_by_domain,
    get_payment_normalcy,
    get_payment_value_by_rail,
)
from app.models import CanonicalEvent
from app.operations.models import OperationalIssue
from app.reconciliation.models import ReconciliationBreak
from app.review.service import set_review


def _event(db, **overrides):
    defaults = dict(tenant_bank_id="KEYBANK", rail_type="ACH", status="SETTLED", amount=100.0)
    defaults.update(overrides)
    defaults.setdefault("transaction_id", f"TXN-{overrides.get('transaction_id', id(overrides))}")
    event = CanonicalEvent(**defaults)
    db.add(event)
    return event


def test_exposure_by_domain_sums_real_amounts_per_engine(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=500.0, transaction_occurred_at="2026-06-01T00:00:00Z")
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=500.0))
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Critical", amount_total=1000.0,
    ))
    db_session.commit()

    result = get_exposure_by_domain(db_session, "KEYBANK")

    by_id = {d["id"]: d["amount"] for d in result["domains"]}
    assert by_id["reconciliation_break"] == 500.0
    assert by_id["fraud_anomaly"] == 1000.0
    assert result["total"] == 1500.0


def test_exposure_by_domain_excludes_normal_and_low_medium_bands(db_session):
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 5, 1, tzinfo=timezone.utc), anomaly_band="Normal", amount_total=99999.0,
    ))
    db_session.commit()

    result = get_exposure_by_domain(db_session, "KEYBANK")

    by_id = {d["id"]: d["amount"] for d in result["domains"]}
    assert by_id["fraud_anomaly"] == 0.0


def test_exposure_by_domain_excludes_rate_based_spike_issues(db_session):
    db_session.add(OperationalIssue(issue_type="FORMAT_REJECTION_SPIKE", tenant_bank_id="KEYBANK", reference_type="PARTY", reference_id="MER-1"))
    db_session.commit()

    result = get_exposure_by_domain(db_session, "KEYBANK")

    by_id = {d["id"]: d["amount"] for d in result["domains"]}
    assert by_id["operational_issue"] == 0.0  # no identifiable transaction amount for a rate problem


def test_operational_exposure_sums_transaction_level_issue(db_session):
    _event(db_session, transaction_id="TXN-DUP", amount=250.0)
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-DUP"))
    db_session.commit()

    result = get_exposure_by_domain(db_session, "KEYBANK")

    by_id = {d["id"]: d["amount"] for d in result["domains"]}
    assert by_id["operational_issue"] == 250.0


def test_operational_exposure_sums_every_transaction_in_overdue_batch(db_session):
    _event(db_session, transaction_id="TXN-A", batch_id="BATCH-1", amount=100.0)
    _event(db_session, transaction_id="TXN-B", batch_id="BATCH-1", amount=150.0)
    db_session.add(OperationalIssue(issue_type="BATCH_NOT_SETTLED", tenant_bank_id="KEYBANK", reference_type="BATCH", reference_id="BATCH-1"))
    db_session.commit()

    result = get_exposure_by_domain(db_session, "KEYBANK")

    by_id = {d["id"]: d["amount"] for d in result["domains"]}
    assert by_id["operational_issue"] == 250.0


def test_exposure_trend_buckets_by_real_transaction_week_not_detection_date(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=100.0, transaction_occurred_at="2026-06-01T00:00:00Z")  # Monday
    _event(db_session, transaction_id="TXN-2", rail_type="ACH", amount=200.0, transaction_occurred_at="2026-06-08T00:00:00Z")  # next Monday
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=100.0))
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=200.0))
    db_session.commit()

    result = get_exposure_trend(db_session, "KEYBANK")

    assert result["points"] == [
        {"week_start": "2026-06-01", "amount": 100.0},
        {"week_start": "2026-06-08", "amount": 200.0},
    ]


def test_exposure_trend_mixes_naive_and_aware_timestamps_without_crashing(db_session):
    # Real bug: EntitySnapshot.window_end is timezone-aware (a real
    # DateTime(timezone=True) column) while transaction_occurred_at is a
    # bare string with no timezone suffix in real data -- sorting week
    # buckets built from both used to raise
    # "can't compare offset-naive and offset-aware datetimes".
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=100.0, transaction_occurred_at="2026-06-01 00:00:00")
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=100.0))
    db_session.add(EntitySnapshot(
        party_id="MER-1", party_type="MERCHANT", tenant_bank_id="KEYBANK", segment="MERCHANT",
        window_type="WEEKLY", window_end=datetime(2026, 6, 8, tzinfo=timezone.utc), anomaly_band="Critical", amount_total=200.0,
    ))
    db_session.commit()

    result = get_exposure_trend(db_session, "KEYBANK")  # must not raise

    assert len(result["points"]) == 2


def test_mitigation_progress_splits_by_review_status(db_session):
    db_session.add(OperationalIssue(id=1, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-A"))
    db_session.add(OperationalIssue(id=2, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-B"))
    _event(db_session, transaction_id="TXN-A", amount=300.0)
    _event(db_session, transaction_id="TXN-B", amount=700.0)
    db_session.commit()
    set_review(db_session, "operational_issue", "1", "KEYBANK", "CONFIRMED")

    result = get_mitigation_progress(db_session, "KEYBANK")

    assert result["mitigated"] == 300.0
    assert result["residual"] == 700.0
    assert result["effectiveness_rate"] == 0.3  # 300 / 1000 -- "Mitigation Effectiveness"


def test_mitigation_effectiveness_rate_is_null_with_no_exposure(db_session):
    result = get_mitigation_progress(db_session, "KEYBANK")

    assert result["total"] == 0
    assert result["effectiveness_rate"] is None


def test_mitigation_progress_by_domain_splits_per_engine(db_session):
    db_session.add(OperationalIssue(id=1, issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-A"))
    _event(db_session, transaction_id="TXN-A", amount=300.0)
    db_session.add(ReconciliationBreak(id=1, tenant_bank_id="KEYBANK", transaction_id="TXN-B", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=700.0))
    _event(db_session, transaction_id="TXN-B", amount=700.0)
    db_session.commit()
    set_review(db_session, "operational_issue", "1", "KEYBANK", "CONFIRMED")

    result = get_mitigation_progress_by_domain(db_session, "KEYBANK")

    by_id = {d["id"]: d for d in result["domains"]}
    assert by_id["operational_issue"]["mitigated"] == 300.0
    assert by_id["operational_issue"]["residual"] == 0.0
    assert by_id["reconciliation_break"]["residual"] == 700.0
    assert by_id["reconciliation_break"]["mitigated"] == 0.0


def test_payment_value_by_rail_protected_is_total_minus_impacted(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=1000.0)
    _event(db_session, transaction_id="TXN-2", rail_type="ACH", amount=500.0)
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-1", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=1000.0))
    db_session.commit()

    result = get_payment_value_by_rail(db_session, "KEYBANK")

    ach = next(r for r in result["rails"] if r["rail_type"] == "ACH")
    assert ach["total_amount"] == 1500.0
    assert ach["impacted_amount"] == 1000.0
    assert ach["protected_amount"] == 500.0


def test_payment_value_by_rail_never_shows_a_rail_outside_the_real_five(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=100.0)
    db_session.commit()

    result = get_payment_value_by_rail(db_session, "KEYBANK")

    real_rails = {"ACH", "WIRE", "CARD", "FEDNOW", "CHEQUE"}
    assert all(r["rail_type"] in real_rails for r in result["rails"])


def test_payment_normalcy_excludes_transactions_touched_by_any_issue(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=100.0)  # untouched
    _event(db_session, transaction_id="TXN-2", rail_type="ACH", amount=100.0)  # reconciliation break
    _event(db_session, transaction_id="TXN-3", rail_type="ACH", amount=100.0)  # duplicate payment
    _event(db_session, transaction_id="TXN-4", rail_type="ACH", amount=100.0, batch_id="BATCH-1")  # overdue batch
    db_session.add(ReconciliationBreak(tenant_bank_id="KEYBANK", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK"))
    db_session.add(OperationalIssue(issue_type="DUPLICATE_PAYMENT", tenant_bank_id="KEYBANK", reference_type="TRANSACTION", reference_id="TXN-3"))
    db_session.add(OperationalIssue(issue_type="BATCH_NOT_SETTLED", tenant_bank_id="KEYBANK", reference_type="BATCH", reference_id="BATCH-1"))
    db_session.commit()

    result = get_payment_normalcy(db_session, "KEYBANK")

    assert result["total_transactions"] == 4
    assert result["touched_transactions"] == 3
    assert result["rate"] == 0.25  # only TXN-1 is untouched


def test_payment_normalcy_ignores_rate_based_spike_issues(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=100.0)
    db_session.add(OperationalIssue(issue_type="FORMAT_REJECTION_SPIKE", tenant_bank_id="KEYBANK", reference_type="PARTY", reference_id="MER-1"))
    db_session.commit()

    result = get_payment_normalcy(db_session, "KEYBANK")

    assert result["touched_transactions"] == 0  # no specific transaction to mark


def test_payment_normalcy_with_no_transactions_is_null(db_session):
    result = get_payment_normalcy(db_session, "KEYBANK")

    assert result["rate"] is None
    assert result["total_transactions"] == 0


def test_tenant_isolation(db_session):
    _event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1", amount=100.0)
    _event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2", amount=999.0)
    db_session.add(ReconciliationBreak(tenant_bank_id="MTB", transaction_id="TXN-2", rail_type="ACH", detection_type="CONFIRMED_BREAK", amount=999.0))
    db_session.commit()

    result = get_exposure_by_domain(db_session, "KEYBANK")

    by_id = {d["id"]: d["amount"] for d in result["domains"]}
    assert by_id["reconciliation_break"] == 0.0
