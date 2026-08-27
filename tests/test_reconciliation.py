from __future__ import annotations

from app.models import CanonicalEvent
from app.reconciliation.breaks import detect_reconciliation_breaks
from app.reconciliation.models import ReconciliationBreak


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="ACH",
        transaction_id="TXN-1",
        amount=500.0,
        reconciliation_status="MATCHED",
        reconciliation_variance_amount=0.0,
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_matched_transaction_is_not_flagged(db_session):
    _make_event(db_session, reconciliation_status="MATCHED", reconciliation_variance_amount=0.0)

    result = detect_reconciliation_breaks(db_session)

    assert result["confirmed_breaks"] == 0
    assert result["provisional_variances"] == 0
    assert db_session.query(ReconciliationBreak).count() == 0


def test_source_flagged_break_is_confirmed_even_with_zero_variance(db_session):
    # Real data shows 12 of 30 BREAK-status transactions have a variance
    # of exactly 0.0 -- flagged for reasons the dollar amount doesn't
    # capture. Must still be caught: never infer CONFIRMED_BREAK from
    # variance alone.
    _make_event(db_session, reconciliation_status="BREAK", reconciliation_variance_amount=0.0)

    result = detect_reconciliation_breaks(db_session)

    assert result["confirmed_breaks"] == 1
    issue = db_session.query(ReconciliationBreak).one()
    assert issue.detection_type == "CONFIRMED_BREAK"
    assert issue.variance_amount == 0.0


def test_source_flagged_break_with_nonzero_variance_is_confirmed_not_provisional(db_session):
    _make_event(db_session, reconciliation_status="BREAK", reconciliation_variance_amount=-22.0)

    result = detect_reconciliation_breaks(db_session)

    assert result["confirmed_breaks"] == 1
    assert result["provisional_variances"] == 0


def test_unconfirmed_nonzero_variance_is_provisional(db_session):
    # A NOT_YET_RECONCILED transaction already showing a real variance --
    # an early-warning signal ahead of the source's own official verdict.
    _make_event(db_session, reconciliation_status="NOT_YET_RECONCILED", reconciliation_variance_amount=18.25)

    result = detect_reconciliation_breaks(db_session)

    assert result["provisional_variances"] == 1
    assert result["confirmed_breaks"] == 0
    issue = db_session.query(ReconciliationBreak).one()
    assert issue.detection_type == "PROVISIONAL_VARIANCE"
    assert issue.source_reconciliation_status == "NOT_YET_RECONCILED"


def test_unconfirmed_zero_variance_is_not_flagged(db_session):
    _make_event(db_session, reconciliation_status="NOT_YET_RECONCILED", reconciliation_variance_amount=0.0)

    result = detect_reconciliation_breaks(db_session)

    assert result["confirmed_breaks"] == 0
    assert result["provisional_variances"] == 0


def test_pending_and_not_applicable_are_not_flagged(db_session):
    _make_event(db_session, transaction_id="TXN-PENDING", reconciliation_status="PENDING", reconciliation_variance_amount=0.0)
    _make_event(db_session, transaction_id="TXN-NA", reconciliation_status="NOT_APPLICABLE", reconciliation_variance_amount=None)

    result = detect_reconciliation_breaks(db_session)

    assert result["confirmed_breaks"] == 0
    assert result["provisional_variances"] == 0
    assert result["transactions_checked"] == 2  # both still counted -- reconciliation_status is set on both


def test_transactions_with_no_reconciliation_status_are_excluded_from_query(db_session):
    _make_event(db_session, reconciliation_status=None, reconciliation_variance_amount=None)

    result = detect_reconciliation_breaks(db_session)

    assert result["transactions_checked"] == 0


def test_tenant_isolation(db_session):
    _make_event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-A", reconciliation_status="BREAK", reconciliation_variance_amount=10.0)
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="TXN-B", reconciliation_status="BREAK", reconciliation_variance_amount=10.0)

    result = detect_reconciliation_breaks(db_session, tenant_bank_id="KEYBANK")

    assert result["transactions_checked"] == 1
    assert db_session.query(ReconciliationBreak).filter_by(tenant_bank_id="KEYBANK").count() == 1
    assert db_session.query(ReconciliationBreak).filter_by(tenant_bank_id="MTB").count() == 0


def test_rerun_with_no_breaks_clears_stale_rows(db_session):
    # Same delete-scope bug class already caught in duplicate_payment.py:
    # a rerun that finds nothing must still clear rows from a previous run.
    event = _make_event(db_session, reconciliation_status="BREAK", reconciliation_variance_amount=10.0)
    detect_reconciliation_breaks(db_session, tenant_bank_id="KEYBANK")
    assert db_session.query(ReconciliationBreak).count() == 1

    event.reconciliation_status = "MATCHED"
    event.reconciliation_variance_amount = 0.0
    db_session.commit()

    result = detect_reconciliation_breaks(db_session, tenant_bank_id="KEYBANK")

    assert result["confirmed_breaks"] == 0
    assert db_session.query(ReconciliationBreak).count() == 0


def test_recompute_replaces_not_duplicates(db_session):
    _make_event(db_session, reconciliation_status="BREAK", reconciliation_variance_amount=10.0)

    detect_reconciliation_breaks(db_session)
    detect_reconciliation_breaks(db_session)

    assert db_session.query(ReconciliationBreak).count() == 1
