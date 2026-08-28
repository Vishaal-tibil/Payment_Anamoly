from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import CanonicalEvent
from app.operations.drift import detect_timeout_spikes
from app.operations.duplicate_payment import detect_duplicate_payments
from app.operations.format_rejection import detect_format_rejections
from app.operations.models import OperationalIssue
from app.operations.rules import detect_unsettled_batches
from app.anomaly.models import EntitySnapshot


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="ACH",
        transaction_id="TXN-1",
        amount=100.0,
        transaction_occurred_at="2026-05-01T10:00:00Z",
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def _make_weekly_snapshot(db, **overrides):
    defaults = dict(
        party_id="MER-1",
        party_type="MERCHANT",
        tenant_bank_id="KEYBANK",
        segment="MERCHANT",
        window_type="WEEKLY",
        window_start=datetime(2026, 5, 4, tzinfo=timezone.utc),
        window_end=datetime(2026, 5, 11, tzinfo=timezone.utc),
        transaction_count=10,
        timeout_ratio=0.0,
        split="train",
    )
    defaults.update(overrides)
    snap = EntitySnapshot(**defaults)
    db.add(snap)
    db.commit()
    return snap


def _weeks(count: int):
    base = datetime(2026, 5, 4, tzinfo=timezone.utc)
    return [base + timedelta(weeks=i) for i in range(count)]


# --- Batch Never Settles ---

def test_overdue_unsettled_batch_is_flagged(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", batch_id="BATCH-OVERDUE",
        expected_settlement_at="2020-01-01T00:00:00Z", file_reached_settlement=False,
    )

    result = detect_unsettled_batches(db_session, tenant_bank_id="KEYBANK")

    assert result["batches_checked"] == 1
    assert result["batches_flagged"] == 1
    issue = db_session.query(OperationalIssue).filter_by(issue_type="BATCH_NOT_SETTLED").one()
    assert issue.reference_id == "BATCH-OVERDUE"
    assert issue.reference_type == "BATCH"
    assert issue.severity_score is None
    assert issue.details["unsettled_transactions"] == 1


def test_batch_not_yet_due_is_not_flagged(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", batch_id="BATCH-FUTURE",
        expected_settlement_at="2099-01-01T00:00:00Z", file_reached_settlement=False,
    )

    result = detect_unsettled_batches(db_session, tenant_bank_id="KEYBANK")

    assert result["batches_flagged"] == 0
    assert db_session.query(OperationalIssue).count() == 0


def test_fully_settled_batch_is_not_flagged(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", batch_id="BATCH-OK",
        expected_settlement_at="2020-01-01T00:00:00Z", file_reached_settlement=True,
    )

    result = detect_unsettled_batches(db_session, tenant_bank_id="KEYBANK")

    assert result["batches_flagged"] == 0


def test_batch_recompute_replaces_not_duplicates(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", batch_id="BATCH-OVERDUE",
        expected_settlement_at="2020-01-01T00:00:00Z", file_reached_settlement=False,
    )

    detect_unsettled_batches(db_session, tenant_bank_id="KEYBANK")
    detect_unsettled_batches(db_session, tenant_bank_id="KEYBANK")

    assert db_session.query(OperationalIssue).filter_by(issue_type="BATCH_NOT_SETTLED").count() == 1


# --- Network/Processor Timeout spike ---

def test_stable_timeout_rate_is_not_flagged(db_session):
    for week_start in _weeks(5):
        _make_weekly_snapshot(db_session, window_start=week_start, window_end=week_start, timeout_ratio=0.05)

    result = detect_timeout_spikes(db_session, tenant_bank_id="KEYBANK")

    assert result["weeks_checked"] > 0
    assert result["weeks_flagged"] == 0
    assert db_session.query(OperationalIssue).filter_by(issue_type="NETWORK_TIMEOUT_SPIKE").count() == 0


def test_timeout_spike_is_flagged(db_session):
    weeks = _weeks(5)
    for week_start in weeks[:4]:
        _make_weekly_snapshot(db_session, window_start=week_start, window_end=week_start, timeout_ratio=0.02)
    # Week 5: timeout rate spikes far above the stable history.
    _make_weekly_snapshot(db_session, window_start=weeks[4], window_end=weeks[4], timeout_ratio=0.95)

    result = detect_timeout_spikes(db_session, tenant_bank_id="KEYBANK")

    assert result["weeks_flagged"] == 1
    issue = db_session.query(OperationalIssue).filter_by(issue_type="NETWORK_TIMEOUT_SPIKE").one()
    assert issue.reference_id == "MER-1"
    assert issue.reference_type == "PARTY"
    assert issue.severity_score is not None and issue.severity_score > 0
    assert issue.details["timeout_ratio"] == 0.95


def test_timeout_first_weeks_with_insufficient_history_are_never_flagged(db_session):
    weeks = _weeks(2)
    for week_start in weeks:
        _make_weekly_snapshot(db_session, window_start=week_start, window_end=week_start, timeout_ratio=0.9)

    result = detect_timeout_spikes(db_session, tenant_bank_id="KEYBANK")

    # _zscore requires >= 2 prior weeks -- with only 2 total rows, neither
    # has enough prior history to be scored at all.
    assert result["weeks_checked"] == 0
    assert result["weeks_flagged"] == 0


def test_timeout_recompute_replaces_not_duplicates(db_session):
    weeks = _weeks(5)
    for week_start in weeks[:4]:
        _make_weekly_snapshot(db_session, window_start=week_start, window_end=week_start, timeout_ratio=0.02)
    _make_weekly_snapshot(db_session, window_start=weeks[4], window_end=weeks[4], timeout_ratio=0.95)

    detect_timeout_spikes(db_session, tenant_bank_id="KEYBANK")
    detect_timeout_spikes(db_session, tenant_bank_id="KEYBANK")

    assert db_session.query(OperationalIssue).filter_by(issue_type="NETWORK_TIMEOUT_SPIKE").count() == 1


def test_timeout_severity_is_not_diluted_by_a_bigger_spike_in_another_tenant(db_session):
    # Tenant KEYBANK has a modest spike; Tenant OTHERBANK has a much
    # bigger one. A global rescale would crush KEYBANK's spike near 0 --
    # per-tenant rescale should let KEYBANK's own top severity reach 100.
    weeks = _weeks(5)
    for week_start in weeks[:4]:
        _make_weekly_snapshot(db_session, party_id="MER-KEY", tenant_bank_id="KEYBANK", window_start=week_start, window_end=week_start, timeout_ratio=0.02)
    _make_weekly_snapshot(db_session, party_id="MER-KEY", tenant_bank_id="KEYBANK", window_start=weeks[4], window_end=weeks[4], timeout_ratio=0.10)
    for week_start in weeks[:4]:
        _make_weekly_snapshot(db_session, party_id="MER-OTHER", tenant_bank_id="OTHERBANK", window_start=week_start, window_end=week_start, timeout_ratio=0.02)
    _make_weekly_snapshot(db_session, party_id="MER-OTHER", tenant_bank_id="OTHERBANK", window_start=weeks[4], window_end=weeks[4], timeout_ratio=0.99)

    detect_timeout_spikes(db_session)  # no tenant_bank_id -- both tenants in one call

    key_issue = db_session.query(OperationalIssue).filter_by(tenant_bank_id="KEYBANK", issue_type="NETWORK_TIMEOUT_SPIKE").one()
    other_issue = db_session.query(OperationalIssue).filter_by(tenant_bank_id="OTHERBANK", issue_type="NETWORK_TIMEOUT_SPIKE").one()
    assert key_issue.severity_score == 100.0
    assert other_issue.severity_score == 100.0


# --- Duplicate Payment ---

def test_duplicate_payment_flagged_via_idempotency_key(db_session):
    _make_event(db_session, transaction_id="TXN-1", idempotency_key="IDEMP-1", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-2", idempotency_key="IDEMP-1", status="SETTLED")

    result = detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")

    assert result["duplicates_flagged"] == 1
    issue = db_session.query(OperationalIssue).filter_by(issue_type="DUPLICATE_PAYMENT").one()
    assert issue.details["match_key_type"] == "idempotency_key"
    assert set(issue.details["settled_transaction_ids"]) == {"TXN-1", "TXN-2"}


def test_duplicate_payment_flagged_via_original_transaction_id_for_retries(db_session):
    _make_event(db_session, transaction_id="TXN-ORIG", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-RETRY", original_transaction_id="TXN-ORIG", is_retry=True, status="SETTLED")

    result = detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")

    assert result["duplicates_flagged"] == 1
    issue = db_session.query(OperationalIssue).filter_by(issue_type="DUPLICATE_PAYMENT").one()
    assert issue.details["match_key_type"] == "original_transaction_id"


def test_legitimate_single_retry_is_not_flagged(db_session):
    # Original failed, retry settled -- only ONE row ever reached settled,
    # so this is a legitimate retry, not a duplicate payment.
    _make_event(db_session, transaction_id="TXN-ORIG", status="FAILED")
    _make_event(db_session, transaction_id="TXN-RETRY", original_transaction_id="TXN-ORIG", is_retry=True, status="SETTLED")

    result = detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")

    assert result["duplicates_flagged"] == 0


def test_duplicate_payment_recompute_replaces_not_duplicates(db_session):
    _make_event(db_session, transaction_id="TXN-1", idempotency_key="IDEMP-1", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-2", idempotency_key="IDEMP-1", status="SETTLED")

    detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")
    detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")

    assert db_session.query(OperationalIssue).filter_by(issue_type="DUPLICATE_PAYMENT").count() == 1


# --- Format Rejection ---

def test_format_rejection_flagged_for_failed_validation(db_session):
    _make_event(
        db_session, transaction_id="TXN-BAD", format_validation_status="FAILED",
        format_validation_errors={"code": "INVALID_IBAN"},
    )
    _make_event(db_session, transaction_id="TXN-OK", format_validation_status="PASSED")

    result = detect_format_rejections(db_session, tenant_bank_id="KEYBANK")

    assert result["rejected_transactions_flagged"] == 1
    issue = db_session.query(OperationalIssue).filter_by(issue_type="FORMAT_REJECTION").one()
    assert issue.reference_id == "TXN-BAD"
    assert issue.details["errors"] == {"code": "INVALID_IBAN"}
    assert issue.severity_score is None


def test_format_rejection_recompute_replaces_not_duplicates(db_session):
    _make_event(db_session, transaction_id="TXN-BAD", format_validation_status="FAILED")

    detect_format_rejections(db_session, tenant_bank_id="KEYBANK")
    detect_format_rejections(db_session, tenant_bank_id="KEYBANK")

    assert db_session.query(OperationalIssue).filter_by(issue_type="FORMAT_REJECTION").count() == 1
