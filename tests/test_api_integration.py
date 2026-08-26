from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import CanonicalEvent

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample_data")


@pytest.fixture()
def client():
    # Lifespan (table creation + mapping seed) only fires when TestClient
    # is used as a context manager.
    with TestClient(app) as c:
        yield c


def _upload(client: TestClient, filename: str, stage: str, rail_type: str = "WIRE"):
    path = os.path.join(SAMPLE_DIR, filename)
    with open(path, "rb") as f:
        return client.post(
            "/ingest/file",
            data={"tenant_bank_id": "KEYBANK", "rail_type": rail_type, "settlement_stage": stage},
            files={"file": (filename, f, "text/csv")},
        )


def test_ingest_sample_pre_then_post_merges_into_one_row(client):
    pre = _upload(client, "keybank_wire_pre.csv", "PRE")
    assert pre.status_code == 200, pre.text
    pre_body = pre.json()
    assert pre_body["rows_mapped"] == 2
    assert pre_body["rows_failed"] == 0

    post = _upload(client, "keybank_wire_post.csv", "POST")
    assert post.status_code == 200, post.text
    post_body = post.json()
    assert post_body["rows_mapped"] == 2
    assert post_body["rows_failed"] == 0
    # internal_batch_id has no mapping row -- logged, not dropped/crashed.
    assert any(
        e.get("type") == "unmapped_columns" and "internal_batch_id" in e.get("columns", [])
        for e in post_body["errors"]
    )

    db = SessionLocal()
    try:
        event = db.query(CanonicalEvent).filter_by(
            tenant_bank_id="KEYBANK", rail_type="WIRE", transaction_id="WIRE-1001",
        ).one()

        assert event.snapshot_pre is not None
        assert event.snapshot_post is not None
        assert event.payer_name == "John A. Rutherford"
        assert event.amount == 15000.00
        assert event.fees == 25.00               # only present in POST
        assert event.status == "SETTLED"          # normalized from "COMPLETED"
        assert event.risk_flags == {"ofac_screen_result": "CLEAR", "aml_screen_result": "CLEAR"}
        assert event.source_merchant_id == "MERCH-88214"
        assert event.merchant_id is None  # not set until Step 4's resolve_parties() runs
        assert event.individual_id is None
        assert event.processor_name is None        # never mapped for WIRE
    finally:
        db.close()


def test_reingesting_same_file_is_idempotent(client):
    before = SessionLocal()
    try:
        count_before = before.query(CanonicalEvent).filter_by(
            tenant_bank_id="KEYBANK", rail_type="WIRE",
        ).count()
    finally:
        before.close()

    _upload(client, "keybank_wire_pre.csv", "PRE")
    _upload(client, "keybank_wire_pre.csv", "PRE")

    after = SessionLocal()
    try:
        count_after = after.query(CanonicalEvent).filter_by(
            tenant_bank_id="KEYBANK", rail_type="WIRE",
        ).count()
    finally:
        after.close()

    assert count_after == max(count_before, 2)


def test_ingest_card_then_resolve_then_list_merchants(client):
    pre = _upload(client, "keybank_card_pre.csv", "PRE", rail_type="CARD")
    assert pre.status_code == 200, pre.text
    assert pre.json()["rows_failed"] == 0

    post = _upload(client, "keybank_card_post.csv", "POST", rail_type="CARD")
    assert post.status_code == 200, post.text
    assert post.json()["rows_failed"] == 0

    resolve_resp = client.post("/resolve/parties", json={"tenant_bank_id": "KEYBANK"})
    assert resolve_resp.status_code == 200, resolve_resp.text
    resolve_body = resolve_resp.json()
    # resolve_parties is tenant-scoped, not rail-scoped -- this may also
    # resolve KEYBANK/WIRE rows left over from other tests sharing the same
    # file-backed demo db, so assert "at least" rather than an exact count.
    assert resolve_body["created_new_merchants"] >= 2  # MERCH-CARD-01, MERCH-CARD-02
    assert resolve_body["errors"] == []

    db = SessionLocal()
    try:
        events = db.query(CanonicalEvent).filter_by(tenant_bank_id="KEYBANK", rail_type="CARD").all()
        assert len(events) == 3
        assert all(e.merchant_id is not None for e in events)
    finally:
        db.close()

    list_resp = client.get("/merchants", params={"tenant_bank_id": "KEYBANK"})
    assert list_resp.status_code == 200, list_resp.text
    merchants = {m["source_merchant_id"]: m for m in list_resp.json()["merchants"]}
    assert merchants["MERCH-CARD-01"]["transaction_count"] == 2
    assert merchants["MERCH-CARD-02"]["transaction_count"] == 1

    # Idempotent: re-resolving creates nothing new.
    resolve_again = client.post("/resolve/parties", json={"tenant_bank_id": "KEYBANK"}).json()
    assert resolve_again["created_new_merchants"] == 0
    assert resolve_again["resolved_merchants"] == 0
