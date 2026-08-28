from __future__ import annotations

from app.anomaly.funnel import compute_beneficiary_snapshots
from app.anomaly.models import BeneficiarySnapshot
from app.models import CanonicalEvent


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="WIRE",
        transaction_id="TXN-1",
        payer_name="Payer A",
        payee_name="Mule Account",
        amount=1000.0,
        transaction_occurred_at="2026-05-01T10:00:00Z",
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_funnel_flagged_for_many_recent_new_senders(db_session):
    # 4 distinct senders, all paying the same beneficiary within the last
    # few days -- clears both the distinct-sender and new-sender-ratio bars.
    senders = ["Alice", "Bob", "Carol", "Dave"]
    for i, sender in enumerate(senders):
        _make_event(
            db_session, transaction_id=f"TXN-{i}", payer_name=sender, payee_name="Mule Account",
            transaction_occurred_at=f"2026-05-0{i + 1}T10:00:00Z",
        )

    result = compute_beneficiary_snapshots(db_session, tenant_bank_id="KEYBANK")

    assert result["beneficiaries_processed"] == 1
    assert result["funnel_flagged"] == 1
    row = db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="Mule Account").one()
    assert row.distinct_senders == 4
    assert row.new_sender_ratio == 1.0
    assert row.funnel_flag is True
    assert "possible mule/funnel account" in row.funnel_reason


def test_not_flagged_for_too_few_senders(db_session):
    _make_event(db_session, transaction_id="TXN-1", payer_name="Alice", payee_name="Regular Biller")
    _make_event(db_session, transaction_id="TXN-2", payer_name="Bob", payee_name="Regular Biller")

    result = compute_beneficiary_snapshots(db_session, tenant_bank_id="KEYBANK")

    assert result["funnel_flagged"] == 0
    row = db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="Regular Biller").one()
    assert row.distinct_senders == 2
    assert row.funnel_flag is False


def test_not_flagged_when_senders_are_long_established(db_session):
    # 4 distinct senders, but their first-ever payment to this beneficiary
    # was months before the recent window -- a long-standing biller, not a
    # sudden funnel pattern.
    senders = ["Alice", "Bob", "Carol", "Dave"]
    for i, sender in enumerate(senders):
        _make_event(
            db_session, transaction_id=f"TXN-OLD-{i}", payer_name=sender, payee_name="Established Biller",
            transaction_occurred_at="2026-01-01T10:00:00Z",
        )
    # One more recent transaction from an existing sender -- doesn't add a
    # new sender, so new_sender_ratio should stay low.
    _make_event(
        db_session, transaction_id="TXN-RECENT", payer_name="Alice", payee_name="Established Biller",
        transaction_occurred_at="2026-05-01T10:00:00Z",
    )

    result = compute_beneficiary_snapshots(db_session, tenant_bank_id="KEYBANK")

    row = db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="Established Biller").one()
    assert row.distinct_senders == 4
    assert row.new_sender_ratio == 0.0
    assert row.funnel_flag is False
    assert result["funnel_flagged"] == 0


def test_recompute_replaces_not_duplicates(db_session):
    _make_event(db_session, transaction_id="TXN-1", payer_name="Alice", payee_name="Payee X")

    compute_beneficiary_snapshots(db_session, tenant_bank_id="KEYBANK")
    compute_beneficiary_snapshots(db_session, tenant_bank_id="KEYBANK")

    assert db_session.query(BeneficiarySnapshot).filter_by(tenant_bank_id="KEYBANK").count() == 1


def test_events_without_a_beneficiary_identifier_are_skipped_and_counted(db_session):
    _make_event(db_session, transaction_id="TXN-1", payee_name=None, payee_account_ref=None)

    result = compute_beneficiary_snapshots(db_session, tenant_bank_id="KEYBANK")

    assert result["skipped_no_beneficiary_identifier"] == 1
    assert db_session.query(BeneficiarySnapshot).count() == 0


def test_near_miss_beneficiary_is_surfaced_but_not_flagged(db_session):
    # 2 distinct senders (below the flag threshold of 3), both recent --
    # clears the near-miss band without meeting the actual flag rule.
    _make_event(db_session, transaction_id="TXN-0", payer_name="Alice", payee_name="Almost Mule", transaction_occurred_at="2026-05-01T10:00:00Z")
    _make_event(db_session, transaction_id="TXN-1", payer_name="Bob", payee_name="Almost Mule", transaction_occurred_at="2026-05-02T10:00:00Z")

    result = compute_beneficiary_snapshots(db_session, tenant_bank_id="KEYBANK")

    assert result["funnel_flagged"] == 0
    assert result["near_miss_count"] == 1
    assert result["near_misses"][0]["beneficiary_key"] == "Almost Mule"
    row = db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="Almost Mule").one()
    assert row.funnel_flag is False
