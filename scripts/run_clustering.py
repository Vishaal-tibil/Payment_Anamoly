"""Runs Track D's HDBSCAN clustering against real data and prints a full
inspection report: per-segment cluster distribution, and which merchants
(if any) changed cluster week to week.

Re-runnable any time -- cluster_and_score() is fully idempotent, so this
just recomputes and overwrites cluster_id/cluster_changed on the existing
EntitySnapshot rows in data/payments.db. Safe to run repeatedly.

Usage: python -m scripts.run_clustering [tenant_bank_id]
       (defaults to MERIDIAN_TRUST_BANK if omitted; run from the repo root)
"""
from __future__ import annotations

import sys

from app.anomaly.clustering import cluster_and_score
from app.anomaly.models import EntitySnapshot
from app.database import SessionLocal

DEFAULT_TENANT_BANK_ID = "MERIDIAN_TRUST_BANK"


def _print_segment_summary(segment: str, summary: dict) -> None:
    print(f"\n=== {segment} ===")
    print(f"  rows clustered:   {summary['rows_clustered']}")
    print(f"  distinct parties: {summary['distinct_parties']}")
    print(f"  clusters found:   {summary['n_clusters']}")
    print(f"  noise (-1) rows:  {summary['noise_count']}")
    print("  cluster sizes:", dict(sorted(summary["cluster_sizes"].items())))


def _print_cluster_changed_report(db, tenant_bank_id: str) -> None:
    rows = (
        db.query(EntitySnapshot)
        .filter_by(tenant_bank_id=tenant_bank_id, segment="MERCHANT", window_type="WEEKLY")
        .order_by(EntitySnapshot.party_id, EntitySnapshot.window_start)
        .all()
    )
    if not rows:
        return

    none_count = sum(1 for r in rows if r.cluster_changed is None)
    false_count = sum(1 for r in rows if r.cluster_changed is False)
    changed = [r for r in rows if r.cluster_changed]

    print("\n=== cluster_changed (merchant weekly rows) ===")
    print(f"  total weekly rows: {len(rows)}")
    print(f"  None (first week per merchant): {none_count}")
    print(f"  False (no change): {false_count}")
    print(f"  True (changed): {len(changed)}")

    if changed:
        changed_parties = sorted({r.party_id for r in changed})
        print(f"\n  {len(changed_parties)} merchant(s) changed cluster at least once:")
        for party_id in changed_parties:
            party_rows = [r for r in rows if r.party_id == party_id]
            sequence = " -> ".join(str(r.cluster_id) for r in party_rows)
            print(f"    {party_id}: {sequence}")


def main() -> None:
    tenant_bank_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TENANT_BANK_ID
    print(f"Running cluster_and_score for tenant_bank_id={tenant_bank_id!r} ...")

    db = SessionLocal()
    try:
        result = cluster_and_score(db, tenant_bank_id=tenant_bank_id)

        for segment, summary in result["segments"].items():
            _print_segment_summary(segment, summary)

        if result["errors"]:
            print("\n=== errors/warnings ===")
            for err in result["errors"]:
                print(f"  [{err['type']}] {err.get('detail', err)}")
        else:
            print("\n=== errors/warnings ===\n  none")

        _print_cluster_changed_report(db, tenant_bank_id)
    finally:
        db.close()


if __name__ == "__main__":
    main()
