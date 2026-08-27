from __future__ import annotations

from app.models import CanonicalEvent
from app.operations.duplicate_payment import detect_duplicate_payments
from app.operations.models import OperationalIssue


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="ACH",
        transaction_id="TXN-1",
        amount=500.0,
        status="SETTLED",
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_two_settled_rows_sharing_idempotency_key_are_flagged(db_session):
    _make_event(db_session, transaction_id="TXN-ORIG", idempotency_key="IDEMP-1", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-RETRY", idempotency_key="IDEMP-1", status="SETTLED",
                is_retry=True, original_transaction_id="TXN-ORIG")

    result = detect_duplicate_payments(db_session)

    assert result["duplicate_payments_flagged"] == 1
    issue = db_session.query(OperationalIssue).filter_by(issue_type="DUPLICATE_PAYMENT").one()
    assert set(issue.details["settled_transaction_ids"]) == {"TXN-ORIG", "TXN-RETRY"}
    assert issue.severity_score is None  # deterministic, not scored


def test_retry_still_pending_is_not_flagged(db_session):
    # Only the original settled; the retry is still in flight -- this is
    # the system working correctly, not a duplicate.
    _make_event(db_session, transaction_id="TXN-ORIG", idempotency_key="IDEMP-1", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-RETRY", idempotency_key="IDEMP-1", status="PENDING",
                is_retry=True, original_transaction_id="TXN-ORIG")

    result = detect_duplicate_payments(db_session)

    assert result["duplicate_payments_flagged"] == 0
    assert db_session.query(OperationalIssue).count() == 0


def test_unrelated_transactions_with_distinct_keys_not_flagged(db_session):
    _make_event(db_session, transaction_id="TXN-A", idempotency_key="IDEMP-A", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-B", idempotency_key="IDEMP-B", status="SETTLED")

    result = detect_duplicate_payments(db_session)

    assert result["duplicate_payments_flagged"] == 0
    assert result["groups_checked"] == 0  # neither key has more than 1 row


def test_fallback_link_by_original_transaction_id_when_keys_differ(db_session):
    # idempotency_key missing/different on the retry -- must still be
    # caught via the original_transaction_id link, per the README's
    # explicit fallback-path design.
    _make_event(db_session, transaction_id="TXN-ORIG", idempotency_key="IDEMP-ORIG", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-RETRY", idempotency_key=None, status="SETTLED",
                is_retry=True, original_transaction_id="TXN-ORIG")

    result = detect_duplicate_payments(db_session)

    assert result["duplicate_payments_flagged"] == 1


def test_tenant_isolation(db_session):
    _make_event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-ORIG", idempotency_key="IDEMP-1", status="SETTLED")
    _make_event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-RETRY", idempotency_key="IDEMP-1", status="SETTLED",
                is_retry=True, original_transaction_id="TXN-ORIG")
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="TXN-ORIG2", idempotency_key="IDEMP-1", status="SETTLED")
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="TXN-RETRY2", idempotency_key="IDEMP-1", status="SETTLED",
                is_retry=True, original_transaction_id="TXN-ORIG2")

    result = detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")

    assert result["duplicate_payments_flagged"] == 1
    issue = db_session.query(OperationalIssue).one()
    assert issue.tenant_bank_id == "KEYBANK"


def test_rerun_with_no_duplicates_clears_stale_rows(db_session):
    # Regression test for the delete-scope bug class found in
    # beneficiary_features.py: a rerun that finds ZERO duplicates must
    # still clear out issues flagged by a previous run, not leave them
    # stale because no group formed this time.
    _make_event(db_session, transaction_id="TXN-ORIG", idempotency_key="IDEMP-1", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-RETRY", idempotency_key="IDEMP-1", status="SETTLED",
                is_retry=True, original_transaction_id="TXN-ORIG")
    detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")
    assert db_session.query(OperationalIssue).count() == 1

    # Now the retry's status is corrected to VOIDED (no longer a real duplicate).
    retry = db_session.query(CanonicalEvent).filter_by(transaction_id="TXN-RETRY").one()
    retry.status = "VOIDED"
    db_session.commit()

    result = detect_duplicate_payments(db_session, tenant_bank_id="KEYBANK")

    assert result["duplicate_payments_flagged"] == 0
    assert db_session.query(OperationalIssue).count() == 0


def test_recompute_replaces_not_duplicates(db_session):
    _make_event(db_session, transaction_id="TXN-ORIG", idempotency_key="IDEMP-1", status="SETTLED")
    _make_event(db_session, transaction_id="TXN-RETRY", idempotency_key="IDEMP-1", status="SETTLED",
                is_retry=True, original_transaction_id="TXN-ORIG")

    detect_duplicate_payments(db_session)
    detect_duplicate_payments(db_session)

    assert db_session.query(OperationalIssue).filter_by(issue_type="DUPLICATE_PAYMENT").count() == 1
