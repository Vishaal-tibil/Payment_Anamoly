"""Read-only aggregation views for the frontend dashboard.

Unlike app/anomaly, app/operations, app/reconciliation, this module is
not an engine -- it writes nothing, has no output table of its own.
Every function here is a live query reshaping already-computed engine
output (EntitySnapshot, OperationalIssue, ReconciliationBreak) plus raw
CanonicalEvent facts, for direct UI consumption. If a number here looks
wrong, the fix belongs in the engine that computed the underlying data,
not here.

Every rail-level figure uses the five real rail_type values in this
schema (ACH, WIRE, CARD, FEDNOW, CHEQUE) -- never the frontend
prototype's placeholder names (RTP, SWIFT, CHIPS, Fedwire), which don't
exist anywhere in the actual data.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from .anomaly.models import EntitySnapshot
from .models import CanonicalEvent, Individual, Merchant
from .operations.models import OperationalIssue
from .reconciliation.models import ReconciliationBreak


def get_overview(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    total_transactions = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id).count()
    settled_transactions = (
        db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id, status="SETTLED").count()
    )
    total_merchants = db.query(Merchant).filter_by(tenant_bank_id=tenant_bank_id).count()
    total_individuals = db.query(Individual).filter_by(tenant_bank_id=tenant_bank_id).count()

    anomaly_band_counts: dict[str, int] = defaultdict(int)
    for (band,) in db.query(EntitySnapshot.anomaly_band).filter_by(tenant_bank_id=tenant_bank_id).all():
        if band:
            anomaly_band_counts[band] += 1

    operational_issue_counts: dict[str, int] = defaultdict(int)
    for (issue_type,) in db.query(OperationalIssue.issue_type).filter_by(tenant_bank_id=tenant_bank_id).all():
        operational_issue_counts[issue_type] += 1

    reconciliation_break_counts: dict[str, int] = defaultdict(int)
    for (detection_type,) in db.query(ReconciliationBreak.detection_type).filter_by(tenant_bank_id=tenant_bank_id).all():
        reconciliation_break_counts[detection_type] += 1

    return {
        "total_transactions": total_transactions,
        "settled_transactions": settled_transactions,
        "settlement_rate": (settled_transactions / total_transactions) if total_transactions else None,
        "total_merchants": total_merchants,
        "total_individuals": total_individuals,
        "anomaly_band_counts": dict(anomaly_band_counts),
        "operational_issue_counts": dict(operational_issue_counts),
        "reconciliation_break_counts": dict(reconciliation_break_counts),
    }


def get_rail_stats(db: Session, tenant_bank_id: str) -> dict[str, Any]:
    events = db.query(CanonicalEvent).filter_by(tenant_bank_id=tenant_bank_id).all()

    by_rail: dict[str, list[CanonicalEvent]] = defaultdict(list)
    for event in events:
        by_rail[event.rail_type].append(event)

    break_counts_by_rail: dict[str, int] = defaultdict(int)
    for (rail_type,) in db.query(ReconciliationBreak.rail_type).filter_by(tenant_bank_id=tenant_bank_id).all():
        break_counts_by_rail[rail_type] += 1

    rails = []
    for rail_type in sorted(by_rail.keys()):
        rail_events = by_rail[rail_type]
        settled_count = sum(1 for e in rail_events if e.status == "SETTLED")
        amounts = [e.amount for e in rail_events if e.amount is not None]
        rails.append({
            "rail_type": rail_type,
            "transaction_count": len(rail_events),
            "settled_count": settled_count,
            "settlement_rate": (settled_count / len(rail_events)) if rail_events else None,
            "total_amount": sum(amounts) if amounts else None,
            "reconciliation_break_count": break_counts_by_rail.get(rail_type, 0),
        })

    return {"rails": rails}


def get_anomaly_detection_categories() -> dict[str, Any]:
    """Static -- describes what the platform's three detection engines
    actually screen for and how, matching the recursive category-tree
    shape Settings > Anomaly Detection expects. Not a DB query: this is
    documentation-as-an-endpoint, grounded in the real detection logic
    (app/anomaly/, app/operations/, app/reconciliation/), not invented
    copy. Read-only -- no toggles/thresholds exist to write back yet.
    """
    return {
        "title": "Anomaly Detection Settings",
        "subtitle": "What this platform screens for, and how each category is actually detected",
        "items": [
            {
                "id": "fraud-anomaly",
                "title": "Fraud & Anomaly Detection",
                "description": (
                    "Unsupervised, profile-based detection -- Isolation Forest, "
                    "HDBSCAN clustering, and time-series drift, combined into one "
                    "0-100 score per merchant/individual per week."
                ),
                "categories": [
                    {
                        "id": "new-payee-risk", "code": "FRAUD-1", "title": "New Payee Risk",
                        "description": "First-time payments to a counterparty -- Isolation Forest's new_counterparty_ratio feature.",
                    },
                    {
                        "id": "funnel-account", "code": "FRAUD-2", "title": "Funnel Account",
                        "description": "Multiple distinct senders suddenly paying the same beneficiary -- weekly z-score drift on distinct_senders/new_sender_ratio.",
                    },
                    {
                        "id": "velocity-checks", "code": "FRAUD-3", "title": "Velocity Checks",
                        "description": "Unusual increase in transaction frequency -- time-series z-score drift on transaction_count.",
                    },
                    {
                        "id": "structuring", "code": "FRAUD-4", "title": "Structuring",
                        "description": "Amounts clustered just under the $10,000 CTR reporting threshold -- Isolation Forest's near_threshold_ratio feature.",
                    },
                ],
            },
            {
                "id": "operational-issues",
                "title": "Operational Issues",
                "description": (
                    "Did the payment pipeline itself work correctly -- mostly "
                    "deterministic fact-checks against the source's own operational "
                    "data, one rolling z-score check."
                ),
                "categories": [
                    {
                        "id": "duplicate-payment", "code": "OPS-1", "title": "Duplicate Payment",
                        "description": "A retry and its original both reached SETTLED status -- an idempotency-key join.",
                    },
                    {
                        "id": "formatting-rejection", "code": "OPS-2", "title": "Formatting Rejection",
                        "description": "Transactions that failed format validation, plus a rolling z-score on the reject rate to catch a spike.",
                    },
                    {
                        "id": "batch-never-settles", "code": "OPS-3", "title": "Batch Never Settles",
                        "description": "A batch past its expected settlement time with unsettled transactions.",
                    },
                    {
                        "id": "network-processor-timeout", "code": "OPS-4", "title": "Network/Processor Timeout",
                        "description": "A merchant's network timeout rate spiking above its own historical baseline.",
                    },
                ],
            },
            {
                "id": "reconciliation",
                "title": "Reconciliation",
                "description": (
                    "Does the settled amount match what the ledger posted -- reads "
                    "the source's own completed network-vs-ledger comparison."
                ),
                "categories": [
                    {
                        "id": "confirmed-break", "code": "REC-1", "title": "Confirmed Break",
                        "description": "The source's own reconciliation process already flagged this as a break.",
                    },
                    {
                        "id": "provisional-variance", "code": "REC-2", "title": "Provisional Variance",
                        "description": "Not yet confirmed as a break, but the variance amount is already nonzero -- an early-warning signal.",
                    },
                ],
            },
        ],
    }
