from __future__ import annotations

from app.canonical_store import upsert_canonical_event
from app.models import CanonicalEvent


def _count(db):
    return db.query(CanonicalEvent).count()


def test_fresh_insert(db_session):
    event = upsert_canonical_event(
        db_session, "KEYBANK", "WIRE", "WIRE-1",
        {"payer_name": "Alice", "amount": 100.0, "currency": "USD"},
        {"raw": "pre-row"},
        is_pre_settlement=True,
    )

    assert _count(db_session) == 1
    assert event.payer_name == "Alice"
    assert event.amount == 100.0
    assert event.snapshot_pre == {"raw": "pre-row"}
    assert event.snapshot_post is None
    assert event.first_seen_at is not None
    assert event.last_updated_at is not None


def test_merge_on_second_file_arrival(db_session):
    upsert_canonical_event(
        db_session, "KEYBANK", "WIRE", "WIRE-2",
        {"payer_name": "Bob", "amount": 50.0, "status": "PENDING"},
        {"stage": "pre"},
        is_pre_settlement=True,
    )
    event = upsert_canonical_event(
        db_session, "KEYBANK", "WIRE", "WIRE-2",
        {"fees": 2.5, "status": "SETTLED"},
        {"stage": "post"},
        is_pre_settlement=False,
    )

    assert _count(db_session) == 1
    assert event.payer_name == "Bob"     # preserved from PRE, untouched by POST
    assert event.amount == 50.0          # preserved from PRE, untouched by POST
    assert event.fees == 2.5             # added by POST
    assert event.status == "SETTLED"     # updated by POST
    assert event.snapshot_pre == {"stage": "pre"}
    assert event.snapshot_post == {"stage": "post"}


def test_dict_valued_fields_merge_keys_instead_of_replacing(db_session):
    # risk_flags (built via JSON_MERGE) must combine PRE's and POST's keys,
    # not have POST's dict wholesale-replace PRE's.
    upsert_canonical_event(
        db_session, "KEYBANK", "WIRE", "WIRE-5",
        {"risk_flags": {"ofac_screen_result": "CLEAR", "aml_screen_result": "CLEAR"}},
        {"stage": "pre"},
        is_pre_settlement=True,
    )
    event = upsert_canonical_event(
        db_session, "KEYBANK", "WIRE", "WIRE-5",
        {"risk_flags": {"fraud_review_status_post": "PASSED"}},
        {"stage": "post"},
        is_pre_settlement=False,
    )

    assert event.risk_flags == {
        "ofac_screen_result": "CLEAR",
        "aml_screen_result": "CLEAR",
        "fraud_review_status_post": "PASSED",
    }


def test_out_of_order_arrival(db_session):
    # POST arrives before PRE for the same transaction.
    upsert_canonical_event(
        db_session, "KEYBANK", "WIRE", "WIRE-3",
        {"fees": 3.0, "status": "SETTLED"},
        {"stage": "post"},
        is_pre_settlement=False,
    )
    event = upsert_canonical_event(
        db_session, "KEYBANK", "WIRE", "WIRE-3",
        {"payer_name": "Cara", "amount": 75.0},
        {"stage": "pre"},
        is_pre_settlement=True,
    )

    assert _count(db_session) == 1
    assert event.payer_name == "Cara"
    assert event.amount == 75.0
    assert event.fees == 3.0
    assert event.status == "SETTLED"
    assert event.snapshot_pre == {"stage": "pre"}
    assert event.snapshot_post == {"stage": "post"}


def test_idempotent_reingestion(db_session):
    payload = {"payer_name": "Dana", "amount": 20.0}
    for _ in range(2):
        upsert_canonical_event(
            db_session, "KEYBANK", "WIRE", "WIRE-4", payload,
            {"stage": "pre"},
            is_pre_settlement=True,
        )

    assert _count(db_session) == 1
    event = db_session.query(CanonicalEvent).filter_by(transaction_id="WIRE-4").one()
    assert event.payer_name == "Dana"
    assert event.amount == 20.0
    assert event.snapshot_pre == {"stage": "pre"}


def test_composite_key_uniqueness_across_tenants(db_session):
    # Same transaction_id, different tenant_bank_id -- must NOT merge.
    upsert_canonical_event(
        db_session, "KEYBANK", "WIRE", "SHARED-ID", {"payer_name": "X"},
        {"stage": "pre"}, is_pre_settlement=True,
    )
    upsert_canonical_event(
        db_session, "MTBANK", "WIRE", "SHARED-ID", {"payer_name": "Y"},
        {"stage": "pre"}, is_pre_settlement=True,
    )

    assert _count(db_session) == 2
    rows = db_session.query(CanonicalEvent).filter_by(transaction_id="SHARED-ID").all()
    tenants = {r.tenant_bank_id for r in rows}
    assert tenants == {"KEYBANK", "MTBANK"}
