from __future__ import annotations

from app.canonical_event_lookup import CanonicalEventLookup
from app.models import CanonicalEvent

_TENANT = "KEYBANK"


def _event(db, **overrides):
    defaults = dict(tenant_bank_id=_TENANT, rail_type="ACH", transaction_id="TXN-1", amount=100.0)
    defaults.update(overrides)
    event = CanonicalEvent(**defaults)
    db.add(event)
    return event


def test_first_by_transaction_id_returns_none_when_missing(db_session):
    lookup = CanonicalEventLookup(db_session, _TENANT)
    assert lookup.first_by_transaction_id("TXN-MISSING") is None
    assert lookup.first_by_transaction_id(None) is None


def test_first_and_all_by_transaction_id(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH")
    _event(db_session, transaction_id="TXN-1", rail_type="WIRE")  # same transaction_id, different rail
    _event(db_session, transaction_id="TXN-2", rail_type="ACH")
    db_session.commit()

    lookup = CanonicalEventLookup(db_session, _TENANT)

    assert len(lookup.all_by_transaction_id("TXN-1")) == 2
    assert lookup.first_by_transaction_id("TXN-1").transaction_id == "TXN-1"
    assert len(lookup.all_by_transaction_id("TXN-2")) == 1


def test_all_by_batch_id_groups_every_event_in_the_batch(db_session):
    _event(db_session, transaction_id="TXN-1", batch_id="BATCH-1")
    _event(db_session, transaction_id="TXN-2", batch_id="BATCH-1")
    _event(db_session, transaction_id="TXN-3", batch_id="BATCH-2")
    db_session.commit()

    lookup = CanonicalEventLookup(db_session, _TENANT)

    assert len(lookup.all_by_batch_id("BATCH-1")) == 2
    assert len(lookup.all_by_batch_id("BATCH-2")) == 1
    assert lookup.all_by_batch_id("BATCH-MISSING") == []
    assert lookup.all_by_batch_id(None) == []


def test_by_rail_and_transaction_id_disambiguates_same_transaction_id_across_rails(db_session):
    _event(db_session, transaction_id="TXN-1", rail_type="ACH", amount=100.0)
    _event(db_session, transaction_id="TXN-1", rail_type="WIRE", amount=999.0)
    db_session.commit()

    lookup = CanonicalEventLookup(db_session, _TENANT)

    assert lookup.by_rail_and_transaction_id("ACH", "TXN-1").amount == 100.0
    assert lookup.by_rail_and_transaction_id("WIRE", "TXN-1").amount == 999.0
    assert lookup.by_rail_and_transaction_id("CARD", "TXN-1") is None
    assert lookup.by_rail_and_transaction_id(None, "TXN-1") is None


def test_tenant_isolation(db_session):
    _event(db_session, tenant_bank_id="KEYBANK", transaction_id="TXN-1")
    _event(db_session, tenant_bank_id="MTB", transaction_id="TXN-2")
    db_session.commit()

    lookup = CanonicalEventLookup(db_session, "KEYBANK")

    assert lookup.first_by_transaction_id("TXN-1") is not None
    assert lookup.first_by_transaction_id("TXN-2") is None
