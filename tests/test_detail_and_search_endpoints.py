from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Individual, Merchant

# Distinct tenant so this doesn't collide with real KeyBank/Meridian data
# in the shared file-backed demo db -- same pattern test_api_integration.py
# and test_operations_api.py already use.
_TENANT = "DETAIL-SEARCH-TEST-BANK"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _seed_merchant(merchant_id: str, legal_name: str) -> None:
    db = SessionLocal()
    try:
        existing = db.get(Merchant, merchant_id)
        if existing is None:
            db.add(Merchant(merchant_id=merchant_id, source_merchant_id=merchant_id, tenant_bank_id=_TENANT, legal_name=legal_name))
            db.commit()
    finally:
        db.close()


def _seed_individual(individual_id: str, full_name: str) -> None:
    db = SessionLocal()
    try:
        existing = db.get(Individual, individual_id)
        if existing is None:
            db.add(Individual(individual_id=individual_id, source_individual_id=individual_id, tenant_bank_id=_TENANT, full_name=full_name))
            db.commit()
    finally:
        db.close()


def test_operational_issue_detail_404_for_unknown_id(client):
    resp = client.get("/operations/issues/999999999", params={"tenant_bank_id": _TENANT})
    assert resp.status_code == 404


def test_reconciliation_break_detail_404_for_unknown_id(client):
    resp = client.get("/reconciliation/breaks/999999999", params={"tenant_bank_id": _TENANT})
    assert resp.status_code == 404


def test_snapshot_detail_404_for_unknown_id(client):
    resp = client.get("/anomaly/snapshots/999999999", params={"tenant_bank_id": _TENANT})
    assert resp.status_code == 404


def test_beneficiary_snapshot_detail_404_for_unknown_id(client):
    resp = client.get("/anomaly/beneficiary-snapshots/999999999", params={"tenant_bank_id": _TENANT})
    assert resp.status_code == 404


def test_merchant_search_matches_legal_name(client):
    _seed_merchant("MER-SEARCH-TEST-1", "Northwind Traders Ltd")

    resp = client.get("/merchants", params={"tenant_bank_id": _TENANT, "search": "Northwind"})

    assert resp.status_code == 200
    body = resp.json()
    assert any(m["merchant_id"] == "MER-SEARCH-TEST-1" for m in body["merchants"])


def test_merchant_search_matches_merchant_id(client):
    _seed_merchant("MER-UNIQUE-XYZ", "Some Merchant")

    resp = client.get("/merchants", params={"tenant_bank_id": _TENANT, "search": "UNIQUE-XYZ"})

    assert resp.status_code == 200
    body = resp.json()
    assert any(m["merchant_id"] == "MER-UNIQUE-XYZ" for m in body["merchants"])


def test_merchant_search_excludes_non_matches(client):
    _seed_merchant("MER-SEARCH-TEST-2", "Acme Corp")

    resp = client.get("/merchants", params={"tenant_bank_id": _TENANT, "search": "ZzzNoSuchThing"})

    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_individual_search_matches_full_name(client):
    _seed_individual("IND-SEARCH-TEST-1", "Jamie Rivera")

    resp = client.get("/individuals", params={"tenant_bank_id": _TENANT, "search": "Rivera"})

    assert resp.status_code == 200
    body = resp.json()
    assert any(i["individual_id"] == "IND-SEARCH-TEST-1" for i in body["individuals"])


def test_operational_issues_priority_level_filter(client):
    resp = client.get("/operations/issues", params={"tenant_bank_id": "MERIDIAN_TRUST_BANK", "priority_level": "critical"})

    assert resp.status_code == 200
    body = resp.json()
    assert all(issue["priority_level"] == "Critical" for issue in body["issues"])
