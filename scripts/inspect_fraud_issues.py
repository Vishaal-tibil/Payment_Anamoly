"""Runs every Fraud detector built so far (Section 2.1 only -- New Payee
Risk, Funnel Account, Velocity, Structuring) and prints one report. Each
section is labeled with what kind of detection actually backs it -- a
trained signal (clustering, time-series drift) or a ranked raw statistic
not yet fed into a model -- so the output is never mistaken for "all
categories equally modeled."

Operational Issues (Section 2.2) are a deliberately separate, not-yet-
built engine -- out of scope here.

Re-runnable any time -- every detector called here is fully idempotent,
recomputing and overwriting its own output each run. Safe to run
repeatedly.

Usage: python -m scripts.inspect_fraud_issues [tenant_bank_id]
       (defaults to MERIDIAN_TRUST_BANK if omitted; run from the repo root)
"""
from __future__ import annotations

import sys

from app.anomaly.clustering import cluster_and_score
from app.anomaly.funnel import compute_beneficiary_snapshots
from app.anomaly.models import BeneficiarySnapshot, EntitySnapshot
from app.anomaly.timeseries import score_drift
from app.database import SessionLocal

DEFAULT_TENANT_BANK_ID = "MERIDIAN_TRUST_BANK"
TOP_N = 5


def _header(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _subheader(title: str) -> None:
    print(f"\n-- {title} --")


def main() -> None:
    tenant_bank_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TENANT_BANK_ID
    print(f"Inspecting Fraud categories (Section 2.1) for tenant_bank_id={tenant_bank_id!r}")

    db = SessionLocal()
    try:
        _header("2.1 FRAUD")

        _subheader("New Payee Risk -- ranked new_counterparty_ratio (weak signal, feeds clustering, no dedicated detector)")
        rows = db.query(EntitySnapshot).filter_by(tenant_bank_id=tenant_bank_id).all()
        top_new_payee = sorted(
            (r for r in rows if r.new_counterparty_ratio is not None), key=lambda r: r.new_counterparty_ratio, reverse=True,
        )[:TOP_N]
        for r in top_new_payee:
            print(f"  {r.party_id} ({r.segment}) new_counterparty_ratio={r.new_counterparty_ratio:.3f}")

        _subheader("Funnel Account -- rule-based (BeneficiarySnapshot), v1, not ML")
        result = compute_beneficiary_snapshots(db, tenant_bank_id=tenant_bank_id)
        print(f"  beneficiaries processed: {result['beneficiaries_processed']}, flagged: {result['funnel_flagged']}")
        for b in db.query(BeneficiarySnapshot).filter_by(tenant_bank_id=tenant_bank_id, funnel_flag=True).all():
            print(f"    {b.beneficiary_key}: {b.funnel_reason}")

        _subheader("Velocity Checks -- timeseries_drift_score (trained signal, Track C)")
        result = score_drift(db, tenant_bank_id=tenant_bank_id)
        print(f"  {result}")
        top_drift = (
            db.query(EntitySnapshot)
            .filter(EntitySnapshot.tenant_bank_id == tenant_bank_id, EntitySnapshot.timeseries_drift_score.isnot(None))
            .order_by(EntitySnapshot.timeseries_drift_score.desc())
            .limit(TOP_N)
            .all()
        )
        for r in top_drift:
            print(f"    {r.party_id} week={r.window_start.date()} drift_score={r.timeseries_drift_score:.1f}")

        _subheader("Structuring -- ranked near_threshold_ratio (real feature, deliberately excluded from clustering -- see clustering.py)")
        top_structuring = sorted(
            (r for r in rows if r.near_threshold_ratio), key=lambda r: r.near_threshold_ratio, reverse=True,
        )[:TOP_N]
        for r in top_structuring:
            print(f"  {r.party_id} ({r.segment}) near_threshold_ratio={r.near_threshold_ratio:.3f}")

        _subheader("(clustering, for context -- feeds New Payee/Structuring weakly, everything else not at all)")
        cluster_result = cluster_and_score(db, tenant_bank_id=tenant_bank_id)
        for segment, summary in cluster_result["segments"].items():
            print(f"    {segment}: {summary['n_clusters']} clusters, {summary['noise_count']}/{summary['rows_clustered']} noise")
    finally:
        db.close()


if __name__ == "__main__":
    main()
