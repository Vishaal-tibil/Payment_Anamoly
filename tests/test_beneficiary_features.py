from __future__ import annotations

from app.anomaly.beneficiary_features import compute_beneficiary_snapshots
from app.anomaly.models import BeneficiarySnapshot
from app.models import CanonicalEvent


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="ACH",
        transaction_id="TXN-1",
        payee_name="Beneficiary Co",
        payee_account_ref="ACCT-BEN-1",
        merchant_id="MER-SENDER-1",
        amount=500.0,
        transaction_occurred_at="2026-05-04T10:00:00Z",  # a Monday
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_distinct_senders_and_new_sender_ratio_across_weeks(db_session):
    # Week 1 (May 4-10): senders A and B pay this beneficiary -- both new.
    _make_event(db_session, transaction_id="T1", merchant_id="MER-A", transaction_occurred_at="2026-05-04T10:00:00Z")
    _make_event(db_session, transaction_id="T2", merchant_id="MER-B", transaction_occurred_at="2026-05-05T10:00:00Z")
    # Week 2 (May 11-17): A returns, C is new.
    _make_event(db_session, transaction_id="T3", merchant_id="MER-A", transaction_occurred_at="2026-05-11T10:00:00Z")
    _make_event(db_session, transaction_id="T4", merchant_id="MER-C", transaction_occurred_at="2026-05-12T10:00:00Z")

    result = compute_beneficiary_snapshots(db_session)

    assert result["errors"] == []
    rows = (
        db_session.query(BeneficiarySnapshot)
        .filter_by(beneficiary_key="ACCT-BEN-1")
        .order_by(BeneficiarySnapshot.window_start)
        .all()
    )
    assert len(rows) == 2

    week1, week2 = rows
    assert week1.distinct_senders == 2
    assert week1.distinct_new_senders == 2  # both A and B never seen before
    assert week1.new_sender_ratio == 1.0

    assert week2.distinct_senders == 2
    assert week2.distinct_new_senders == 1  # only C is new; A already seen in week 1
    assert week2.new_sender_ratio == 0.5


def test_beneficiary_key_prefers_account_ref_over_name(db_session):
    _make_event(db_session, payee_account_ref="ACCT-X", payee_name="Same Display Name")
    _make_event(db_session, transaction_id="T2", payee_account_ref=None, payee_name="Different Payee")

    compute_beneficiary_snapshots(db_session)

    keys = {r.beneficiary_key for r in db_session.query(BeneficiarySnapshot).all()}
    assert "ACCT-X" in keys  # from the row that had an account ref
    assert "Different Payee" in keys  # fell back to name when no account ref was present


def test_sender_key_prefers_resolved_id_over_payer_name(db_session):
    _make_event(db_session, merchant_id="MER-RESOLVED", individual_id=None, payer_name="Some Raw Name")

    compute_beneficiary_snapshots(db_session)

    row = db_session.query(BeneficiarySnapshot).one()
    assert row.sender_party_types == ["MERCHANT"]


def test_values_unaffected_by_flags_it_must_not_use(db_session):
    # Same tenant, same beneficiary, same week -- only the forbidden flags
    # differ between two otherwise-identical beneficiaries so any
    # difference in the computed numbers can only come from leakage.
    common = dict(tenant_bank_id="KEYBANK", amount=100.0)
    _make_event(db_session, transaction_id="T1", payee_account_ref="ACCT-LEAK-CLEAN", merchant_id="MER-A", **common)
    _make_event(
        db_session, transaction_id="T2", payee_account_ref="ACCT-LEAK-DIRTY", merchant_id="MER-A", **common,
        funnel_account_flag=True, new_payee_risk_flag=True,
        fraud_risk_details={"distinct_originating_accounts_24h": 99},
    )

    compute_beneficiary_snapshots(db_session)

    clean = db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="ACCT-LEAK-CLEAN").one()
    dirty = db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="ACCT-LEAK-DIRTY").one()
    for field in ("transaction_count", "amount_total", "distinct_senders", "distinct_new_senders", "new_sender_ratio"):
        assert getattr(clean, field) == getattr(dirty, field), f"{field} differs -- possible leakage"


def test_two_tenants_sharing_a_beneficiary_key_stay_separate(db_session):
    # Different tenants can coincidentally share an account ref or payee
    # display name -- must not be merged into one snapshot, and a
    # tenant-scoped recompute must not delete the other tenant's rows.
    _make_event(db_session, tenant_bank_id="KEYBANK", transaction_id="T1", payee_account_ref="ACCT-SAME", merchant_id="MER-A")
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="T2", payee_account_ref="ACCT-SAME", merchant_id="MER-B")

    compute_beneficiary_snapshots(db_session)

    rows = db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="ACCT-SAME").all()
    assert len(rows) == 2
    assert {r.tenant_bank_id for r in rows} == {"KEYBANK", "MTB"}
    for row in rows:
        assert row.distinct_senders == 1  # each tenant only sees its own sender

    # Recomputing just KEYBANK must not wipe MTB's row.
    compute_beneficiary_snapshots(db_session, tenant_bank_id="KEYBANK")
    rows_after = db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="ACCT-SAME").all()
    assert len(rows_after) == 2


def test_tenant_isolation(db_session):
    _make_event(db_session, tenant_bank_id="KEYBANK", payee_account_ref="ACCT-SHARED", merchant_id="MER-A")
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="T2", payee_account_ref="ACCT-SHARED", merchant_id="MER-B")

    result = compute_beneficiary_snapshots(db_session, tenant_bank_id="KEYBANK")

    assert result["beneficiaries_processed"] == 1
    rows = db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="ACCT-SHARED").all()
    assert len(rows) == 1
    assert rows[0].tenant_bank_id == "KEYBANK"


def test_recompute_replaces_not_duplicates(db_session):
    _make_event(db_session)
    compute_beneficiary_snapshots(db_session)
    compute_beneficiary_snapshots(db_session)

    assert db_session.query(BeneficiarySnapshot).filter_by(beneficiary_key="ACCT-BEN-1").count() == 1


def test_rows_with_unparseable_timestamp_are_skipped(db_session):
    # payee_name/account_ref missing rows are excluded at the SQL level
    # (the query itself requires payee_name IS NOT NULL), so the only way
    # to reach the skip-counting branch is a present-but-unparseable
    # transaction_occurred_at.
    _make_event(db_session, transaction_occurred_at="not-a-real-timestamp")

    result = compute_beneficiary_snapshots(db_session)

    assert result["skipped_no_beneficiary_key"] == 1
    assert db_session.query(BeneficiarySnapshot).count() == 0


def test_rows_missing_payee_name_never_reach_a_beneficiary(db_session):
    _make_event(db_session, payee_account_ref=None, payee_name=None)

    result = compute_beneficiary_snapshots(db_session)

    assert result["beneficiaries_processed"] == 0
    assert db_session.query(BeneficiarySnapshot).count() == 0
