from __future__ import annotations

from app.models import CanonicalEvent, Individual, Merchant
from app.resolution import resolve_parties


def _make_event(db, **overrides):
    defaults = dict(
        tenant_bank_id="KEYBANK",
        rail_type="CARD",
        transaction_id="TXN-1",
        payer_name="Alice Buyer",
        payee_name="Acme Merchant LLC",
        payee_account_ref="****1234",
        amount=10.0,
        currency="USD",
        processor_name="Airwallex",
        onboarded_by="BRANCH_OPS",
    )
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    db.commit()
    return event


def test_new_merchant_created_on_first_resolution(db_session):
    _make_event(db_session, transaction_id="TXN-1", source_merchant_id="SRC-M1")

    result = resolve_parties(db_session)

    assert result["created_new_merchants"] == 1
    assert result["resolved_merchants"] == 1
    assert result["errors"] == []

    merchant = db_session.query(Merchant).filter_by(source_merchant_id="SRC-M1", tenant_bank_id="KEYBANK").one()
    event = db_session.query(CanonicalEvent).filter_by(transaction_id="TXN-1").one()
    assert event.merchant_id == merchant.merchant_id
    assert merchant.merchant_id.startswith("MER-")
    # legal_name comes from payer_name only -- no payee_name fallback,
    # since payee_name can name a different party entirely on rails with
    # no entity-name field of their own (see test below).
    assert merchant.legal_name == "Alice Buyer"
    assert merchant.processor_name == "Airwallex"
    assert merchant.onboarded_by == "BRANCH_OPS"


def test_merchant_with_no_payer_name_stays_null_not_mislabeled(db_session):
    # Card/Cheque-shaped row: no payer_name available at all, only
    # payee_name (a different party, the counterparty). legal_name must
    # stay null rather than silently taking the counterparty's name.
    _make_event(
        db_session, transaction_id="TXN-1", source_merchant_id="SRC-M1",
        payer_name=None, payee_name="Some Counterparty Inc",
    )

    resolve_parties(db_session)

    merchant = db_session.query(Merchant).filter_by(source_merchant_id="SRC-M1").one()
    assert merchant.legal_name is None


def test_merchant_enriched_by_later_row_with_a_name(db_session):
    # First row for this merchant has no payer_name (e.g. a Card/Cheque
    # row); a later row for the SAME merchant does. The merchant should
    # end up named, not permanently stuck at whatever the first row had.
    _make_event(
        db_session, transaction_id="TXN-1", source_merchant_id="SRC-M1",
        rail_type="CARD", payer_name=None,
    )
    _make_event(
        db_session, transaction_id="TXN-2", source_merchant_id="SRC-M1",
        rail_type="ACH", payer_name="Real Entity Name",
    )

    resolve_parties(db_session)

    merchant = db_session.query(Merchant).filter_by(source_merchant_id="SRC-M1").one()
    assert merchant.legal_name == "Real Entity Name"
    assert db_session.query(Merchant).count() == 1  # still just the one merchant, enriched not duplicated


def test_existing_merchant_reused_not_duplicated(db_session):
    _make_event(db_session, transaction_id="TXN-1", source_merchant_id="SRC-M1")
    _make_event(db_session, transaction_id="TXN-2", source_merchant_id="SRC-M1")

    result = resolve_parties(db_session)

    assert result["created_new_merchants"] == 1
    assert result["resolved_merchants"] == 2
    assert db_session.query(Merchant).count() == 1

    events = db_session.query(CanonicalEvent).all()
    assert events[0].merchant_id == events[1].merchant_id


def test_individual_created_from_source_individual_id(db_session):
    _make_event(
        db_session, transaction_id="TXN-1", rail_type="ACH",
        payer_name="John Doe", payer_account_ref="****9876",
        source_merchant_id=None, source_individual_id="SRC-IND1",
    )

    result = resolve_parties(db_session)

    assert result["created_new_individuals"] == 1
    assert result["resolved_individuals"] == 1

    individual = db_session.query(Individual).filter_by(source_individual_id="SRC-IND1", tenant_bank_id="KEYBANK").one()
    event = db_session.query(CanonicalEvent).filter_by(transaction_id="TXN-1").one()
    assert event.individual_id == individual.individual_id
    assert individual.individual_id.startswith("IND-")
    assert individual.full_name == "John Doe"
    assert individual.account_ref == "****9876"


def test_tenant_isolation_same_source_id_two_tenants(db_session):
    _make_event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1", source_merchant_id="SHARED-ID")
    _make_event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2", source_merchant_id="SHARED-ID")

    result = resolve_parties(db_session)

    assert result["created_new_merchants"] == 2
    merchants = db_session.query(Merchant).filter_by(source_merchant_id="SHARED-ID").all()
    assert len(merchants) == 2
    ids = {m.merchant_id for m in merchants}
    assert len(ids) == 2
    tenants = {m.tenant_bank_id for m in merchants}
    assert tenants == {"KEYBANK", "MTB"}


def test_mutual_exclusivity_merchant_wins(db_session):
    _make_event(
        db_session, transaction_id="TXN-1",
        source_merchant_id="SRC-M1", source_individual_id="SRC-IND1",
    )

    result = resolve_parties(db_session)

    assert result["created_new_merchants"] == 1
    assert result["created_new_individuals"] == 0
    assert any(e["type"] == "mutual_exclusivity_warning" for e in result["errors"])

    event = db_session.query(CanonicalEvent).filter_by(transaction_id="TXN-1").one()
    assert event.merchant_id is not None
    assert event.individual_id is None


def test_resolution_is_idempotent(db_session):
    _make_event(db_session, transaction_id="TXN-1", source_merchant_id="SRC-M1")
    _make_event(
        db_session, transaction_id="TXN-2", rail_type="ACH",
        source_merchant_id=None, source_individual_id="SRC-IND1",
    )

    first = resolve_parties(db_session)
    second = resolve_parties(db_session)

    assert second["created_new_merchants"] == 0
    assert second["created_new_individuals"] == 0
    assert second["resolved_merchants"] == 0
    assert second["resolved_individuals"] == 0
    assert db_session.query(Merchant).count() == 1
    assert db_session.query(Individual).count() == 1

    event1 = db_session.query(CanonicalEvent).filter_by(transaction_id="TXN-1").one()
    event2 = db_session.query(CanonicalEvent).filter_by(transaction_id="TXN-2").one()
    assert event1.merchant_id is not None
    assert event2.individual_id is not None


def test_rows_with_no_source_id_skipped_cleanly(db_session):
    _make_event(db_session, transaction_id="TXN-1", source_merchant_id=None, source_individual_id=None)

    result = resolve_parties(db_session)

    assert result["skipped_already_resolved"] == 1
    assert result["resolved_merchants"] == 0
    assert result["resolved_individuals"] == 0
    event = db_session.query(CanonicalEvent).filter_by(transaction_id="TXN-1").one()
    assert event.merchant_id is None
    assert event.individual_id is None
