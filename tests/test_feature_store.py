from __future__ import annotations

from app.feature_store import compute_features
from app.models import CanonicalEvent, PartyFeatures


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="CARD",
        transaction_id="TXN-1",
        merchant_id="MER-1",
        payee_name="Some Counterparty",
        amount=100.0,
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_aggregates_across_multiple_transactions(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", rail_type="CARD", amount=100.0,
        payee_name="Alpha Co", new_payee_risk_flag=True,
    )
    _make_event(
        db_session, transaction_id="TXN-2", rail_type="ACH", amount=50.0,
        payee_name="Beta Co", new_payee_risk_flag=False,
    )

    result = compute_features(db_session)

    assert result["merchants_computed"] == 1
    assert result["errors"] == []

    features = db_session.query(PartyFeatures).filter_by(party_id="MER-1").one()
    assert features.transaction_count == 2
    assert features.total_amount == 150.0
    assert features.avg_amount == 75.0
    assert features.rails_active == ["ACH", "CARD"]
    assert features.distinct_counterparties == 2
    assert features.new_payee_risk_rate == 0.5  # 1 True out of 2 evaluated


def test_rate_excludes_inapplicable_not_counts_as_false(db_session):
    # funnel_account_flag is never set (None) on either row -- rate must
    # be None (not applicable), never 0.0 (which would read as "never
    # flagged" rather than "never evaluated").
    _make_event(db_session, transaction_id="TXN-1", funnel_account_flag=None)
    _make_event(db_session, transaction_id="TXN-2", funnel_account_flag=None)

    compute_features(db_session)

    features = db_session.query(PartyFeatures).filter_by(party_id="MER-1").one()
    assert features.funnel_account_rate is None


def test_recompute_replaces_not_duplicates(db_session):
    _make_event(db_session, transaction_id="TXN-1", amount=100.0)
    compute_features(db_session)

    _make_event(db_session, transaction_id="TXN-2", amount=200.0)
    compute_features(db_session)

    assert db_session.query(PartyFeatures).filter_by(party_id="MER-1").count() == 1
    features = db_session.query(PartyFeatures).filter_by(party_id="MER-1").one()
    assert features.transaction_count == 2
    assert features.total_amount == 300.0


def test_tenant_isolation(db_session):
    _make_event(db_session, tenant_bank_id="KEYBANK", merchant_id="MER-SHARED", transaction_id="TXN-1")
    _make_event(db_session, tenant_bank_id="MTB", merchant_id="MER-SHARED", transaction_id="TXN-2")

    result = compute_features(db_session, tenant_bank_id="KEYBANK")

    assert result["merchants_computed"] == 1
    features = db_session.query(PartyFeatures).filter_by(party_id="MER-SHARED").one()
    assert features.tenant_bank_id == "KEYBANK"
    assert features.transaction_count == 1  # only the KEYBANK row, not MTB's


def test_unresolved_rows_excluded(db_session):
    _make_event(db_session, transaction_id="TXN-1", merchant_id=None, individual_id=None)

    result = compute_features(db_session)

    assert result["parties_computed"] == 0
    assert db_session.query(PartyFeatures).count() == 0


def test_individual_features_computed_separately(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", merchant_id=None, individual_id="IND-1",
        payee_name="Grocery Store",
    )

    result = compute_features(db_session)

    assert result["individuals_computed"] == 1
    features = db_session.query(PartyFeatures).filter_by(party_id="IND-1").one()
    assert features.party_type == "INDIVIDUAL"
    assert features.transaction_count == 1
