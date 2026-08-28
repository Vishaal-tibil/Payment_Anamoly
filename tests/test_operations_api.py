from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import CanonicalEvent

# Distinct tenant so this doesn't collide with real KeyBank/Meridian data
# in the shared file-backed demo db (same db the client fixture hits, per
# test_api_integration.py's own pattern).
_TENANT = "OPS-API-TEST-BANK"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed_overdue_batch(tenant_bank_id: str, transaction_id: str) -> None:
    # Idempotent seed -- this hits the same real, shared file-backed db
    # test_api_integration.py's client fixture does, so re-running the
    # suite must not collide with a row a prior run already committed.
    db = SessionLocal()
    try:
        existing = db.query(CanonicalEvent).filter_by(
            tenant_bank_id=tenant_bank_id, rail_type="ACH", transaction_id=transaction_id,
        ).one_or_none()
        if existing is None:
            db.add(CanonicalEvent(
                tenant_bank_id=tenant_bank_id, rail_type="ACH", transaction_id=transaction_id,
                batch_id=f"{tenant_bank_id}-{transaction_id}-BATCH", expected_settlement_at="2020-01-01T00:00:00Z",
                file_reached_settlement=False,
            ))
            db.commit()
    finally:
        db.close()


def test_compute_endpoint_returns_all_four_issue_type_summaries(client):
    _seed_overdue_batch(_TENANT, "TXN-COMPUTE-1")

    resp = client.post("/operations/issues/compute", json={"tenant_bank_id": _TENANT})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"batch_not_settled", "network_timeout_spike", "duplicate_payment", "format_rejection"}
    # >= 1, not == 1: this tenant's rows accumulate across repeated runs
    # against the same real, shared file-backed db (same reasoning
    # test_api_integration.py's own assertions use).
    assert body["batch_not_settled"]["batches_flagged"] >= 1


def test_list_endpoint_filters_by_issue_type(client):
    _seed_overdue_batch(_TENANT, "TXN-LIST-1")
    client.post("/operations/issues/compute", json={"tenant_bank_id": _TENANT})

    resp = client.get("/operations/issues", params={"tenant_bank_id": _TENANT, "issue_type": "BATCH_NOT_SETTLED"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert all(issue["issue_type"] == "BATCH_NOT_SETTLED" for issue in body["issues"])


def test_list_endpoint_requires_tenant_bank_id(client):
    resp = client.get("/operations/issues")

    assert resp.status_code == 422  # FastAPI validation error -- tenant_bank_id is a required query param
