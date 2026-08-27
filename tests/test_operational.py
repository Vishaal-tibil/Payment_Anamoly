from __future__ import annotations

from app.models import CanonicalEvent
from app.operational.duplicate_detection import detect_duplicate_payments
from app.operational.format_rejection import detect_format_rejections
from app.operational.settlement import detect_unsettled_batches
from app.operational.timeout_detection import detect_timeouts


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="CARD",
        transaction_id="TXN-1",
        merchant_id="MER-1",
        payer_name="Payer A",
        payee_name="Counterparty A",
        amount=100.0,
        transaction_occurred_at="2026-05-01T10:00:00Z",
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


# --- Network/Processor Timeout ---

def test_detect_timeouts_flags_response_over_sla(db_session):
    _make_event(
        db_session, transaction_id="TXN-SLOW",
        network_response_details={"network_response_control.response_time_ms": 9000, "network_response_control.expected_response_sla_ms": 8000},
    )
    _make_event(
        db_session, transaction_id="TXN-FAST",
        network_response_details={"network_response_control.response_time_ms": 100, "network_response_control.expected_response_sla_ms": 8000},
    )

    result = detect_timeouts(db_session, tenant_bank_id="KEYBANK")

    assert result["transactions_checked"] == 2
    assert result["timeouts_flagged"] == 1
    assert result["flagged"][0]["transaction_id"] == "TXN-SLOW"
    assert result["flagged"][0]["overage_ms"] == 1000


def test_detect_timeouts_ignores_rows_without_response_data(db_session):
    _make_event(db_session, transaction_id="TXN-NO-DATA", network_response_details=None)

    result = detect_timeouts(db_session, tenant_bank_id="KEYBANK")

    assert result["transactions_checked"] == 0
    assert result["timeouts_flagged"] == 0


# --- Batch/File Not Reaching Settlement ---

def test_detect_unsettled_batches_flags_overdue_batch(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", batch_id="BATCH-1",
        expected_settlement_at="2020-01-01T00:00:00Z", file_reached_settlement=False,
    )
    _make_event(
        db_session, transaction_id="TXN-2", batch_id="BATCH-1",
        expected_settlement_at="2020-01-01T00:00:00Z", file_reached_settlement=True,
    )

    result = detect_unsettled_batches(db_session, tenant_bank_id="KEYBANK")

    assert result["batches_checked"] == 1
    assert result["batches_overdue_unsettled"] == 1
    assert result["flagged"][0]["batch_id"] == "BATCH-1"
    assert result["flagged"][0]["unsettled_transactions"] == 1


def test_detect_unsettled_batches_ignores_batch_not_yet_due(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", batch_id="BATCH-FUTURE",
        expected_settlement_at="2099-01-01T00:00:00Z", file_reached_settlement=False,
    )

    result = detect_unsettled_batches(db_session, tenant_bank_id="KEYBANK")

    assert result["batches_overdue_unsettled"] == 0


def test_detect_unsettled_batches_ignores_fully_settled_batch(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", batch_id="BATCH-OK",
        expected_settlement_at="2020-01-01T00:00:00Z", file_reached_settlement=True,
    )

    result = detect_unsettled_batches(db_session, tenant_bank_id="KEYBANK")

    assert result["batches_overdue_unsettled"] == 0


# --- Duplicate Payment (Retry) ---

def test_detect_duplicate_payments_flags_close_matching_transactions(db_session):
    _make_event(
        db_session, transaction_id="TXN-ORIG", payer_name="Alice", payee_name="Bob", amount=500.0,
        transaction_occurred_at="2026-05-01T10:00:00Z",
    )
    _make_event(
        db_session, transaction_id="TXN-DUP", payer_name="Alice", payee_name="Bob", amount=500.0,
        transaction_occurred_at="2026-05-01T10:05:00Z", is_retry=True,
    )

    result = detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")

    assert result["duplicate_pairs_flagged"] == 1
    pair = result["flagged"][0]
    assert {pair["transaction_id_1"], pair["transaction_id_2"]} == {"TXN-ORIG", "TXN-DUP"}
    assert pair["either_marked_retry"] is True
    assert pair["seconds_apart"] == 300.0


def test_detect_duplicate_payments_ignores_pairs_far_apart(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", payer_name="Alice", payee_name="Bob", amount=500.0,
        transaction_occurred_at="2026-05-01T10:00:00Z",
    )
    _make_event(
        db_session, transaction_id="TXN-2", payer_name="Alice", payee_name="Bob", amount=500.0,
        transaction_occurred_at="2026-05-01T23:00:00Z",  # 13 hours later -- not a duplicate
    )

    result = detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")

    assert result["duplicate_pairs_flagged"] == 0


def test_detect_duplicate_payments_ignores_different_amounts(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", payer_name="Alice", payee_name="Bob", amount=500.0,
        transaction_occurred_at="2026-05-01T10:00:00Z",
    )
    _make_event(
        db_session, transaction_id="TXN-2", payer_name="Alice", payee_name="Bob", amount=501.0,
        transaction_occurred_at="2026-05-01T10:05:00Z",
    )

    result = detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")

    assert result["duplicate_pairs_flagged"] == 0


# --- Format Rejection ---

def test_detect_format_rejections_lists_failed_transactions(db_session):
    _make_event(
        db_session, transaction_id="TXN-BAD", format_validation_status="FAILED",
        format_validation_errors={"code": "INVALID_IBAN", "field": "payee_account_ref"},
    )
    _make_event(db_session, transaction_id="TXN-OK", format_validation_status="PASSED")

    result = detect_format_rejections(db_session, tenant_bank_id="KEYBANK")

    assert result["rejected_transactions"] == 1
    assert result["flagged"][0]["transaction_id"] == "TXN-BAD"
    assert result["flagged"][0]["errors"] == {"code": "INVALID_IBAN", "field": "payee_account_ref"}
