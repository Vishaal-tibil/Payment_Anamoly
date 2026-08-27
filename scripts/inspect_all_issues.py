"""Runs every fraud/operational detector built so far and prints one
report covering all 8 named categories from the requirements doc's
Section 2 (2.1 Fraud, 2.2 Operational). Each section is labeled with what
kind of detection actually backs it -- a trained signal (clustering,
time-series drift), a ranked raw statistic (not yet fed into a model), or
a deterministic rule -- so the output is never mistaken for "all 8
equally modeled."

Re-runnable any time -- every detector called here is fully idempotent,
recomputing and overwriting its own output each run. Safe to run
repeatedly.

Usage: python -m scripts.inspect_all_issues [tenant_bank_id]
       (defaults to MERIDIAN_TRUST_BANK if omitted; run from the repo root)
"""
from __future__ import annotations

import sys

from app.anomaly.clustering import cluster_and_score
from app.anomaly.funnel import compute_beneficiary_snapshots
from app.anomaly.models import BeneficiarySnapshot, EntitySnapshot
from app.anomaly.timeseries import score_drift
from app.database import SessionLocal
from app.operational.duplicate_detection import detect_duplicate_payments
from app.operational.format_rejection import detect_format_rejections
from app.operational.settlement import detect_unsettled_batches
from app.operational.timeout_detection import detect_timeouts

DEFAULT_TENANT_BANK_ID = "MERIDIAN_TRUST_BANK"
TOP_N = 5


def _header(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _subheader(title: str) -> None:
    print(f"\n-- {title} --")


def _print_flagged(flagged: list[dict], top_n: int = TOP_N) -> None:
    if not flagged:
        print("  (none)")
        return
    for item in flagged[:top_n]:
        print(f"  {item}")
    if len(flagged) > top_n:
        print(f"  ... and {len(flagged) - top_n} more")


def main() -> None:
    tenant_bank_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TENANT_BANK_ID
    print(f"Inspecting all 8 fraud/operational categories for tenant_bank_id={tenant_bank_id!r}")

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

        _header("2.2 OPERATIONAL (Step 6b -- deterministic checks, no ML anywhere in this section)")

        _subheader("Network/Processor Timeout")
        result = detect_timeouts(db, tenant_bank_id=tenant_bank_id)
        print(f"  checked={result['transactions_checked']}, flagged={result['timeouts_flagged']}")
        _print_flagged(result["flagged"])

        _subheader("Batch/File Not Reaching Settlement")
        result = detect_unsettled_batches(db, tenant_bank_id=tenant_bank_id)
        print(f"  batches_checked={result['batches_checked']}, overdue_unsettled={result['batches_overdue_unsettled']}")
        _print_flagged(result["flagged"])

        _subheader("Duplicate Payment (Retry)")
        result = detect_duplicate_payments(db, tenant_bank_id=tenant_bank_id)
        print(f"  candidate_groups={result['candidate_groups_checked']}, flagged_pairs={result['duplicate_pairs_flagged']}")
        _print_flagged(result["flagged"])

        _subheader("Format/Message Rejection")
        result = detect_format_rejections(db, tenant_bank_id=tenant_bank_id)
        print(f"  rejected_transactions={result['rejected_transactions']}")
        _print_flagged(result["flagged"])
    finally:
        db.close()


if __name__ == "__main__":
    main()
