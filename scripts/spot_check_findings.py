"""Manual precision/accuracy spot-check: for each of the 8 fraud/
operational categories, pulls the single top-flagged item and prints the
RAW canonical_events rows behind it, so a human can judge by eye whether
the flag actually makes sense.

This is not a metric -- there's no ground-truth fraud label anywhere in
this dataset (the whole point of this engine is unsupervised detection),
so there's no accuracy/precision number to compute. This is face-validity
checking: does the raw evidence actually support what the detector claims?

Usage: python -m scripts.spot_check_findings [tenant_bank_id]
       (defaults to MERIDIAN_TRUST_BANK if omitted; run from the repo root)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from app.anomaly.funnel import compute_beneficiary_snapshots
from app.anomaly.models import BeneficiarySnapshot, EntitySnapshot
from app.anomaly.timeseries import score_drift
from app.database import SessionLocal
from app.models import CanonicalEvent
from app.operational.duplicate_detection import detect_duplicate_payments
from app.operational.format_rejection import detect_format_rejections
from app.operational.settlement import detect_unsettled_batches
from app.operational.timeout_detection import detect_timeouts

DEFAULT_TENANT_BANK_ID = "MERIDIAN_TRUST_BANK"


def _header(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def _print_event(e: CanonicalEvent) -> None:
    print(
        f"    {e.transaction_id} | {e.transaction_occurred_at} | amount={e.amount} | "
        f"payer={e.payer_name} | payee={e.payee_name} | rail={e.rail_type}"
    )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _events_for_snapshot_row(db, row: EntitySnapshot) -> list[CanonicalEvent]:
    """Raw events actually behind one EntitySnapshot row -- scoped to its
    window when it's a WEEKLY row (a merchant has many rows, one per
    week; pulling the party's whole history would show transactions that
    have nothing to do with the specific ratio being checked). TO_DATE
    rows have no window, so party_id alone is the full scope already.
    """
    query = db.query(CanonicalEvent).filter(
        (CanonicalEvent.merchant_id == row.party_id) | (CanonicalEvent.individual_id == row.party_id)
    )
    events = query.all()
    if row.window_type == "WEEKLY" and row.window_start is not None:
        # SQLite doesn't actually persist tzinfo even for a
        # DateTime(timezone=True) column -- what comes back is naive, so
        # normalize to UTC (same convention _parse_ts already applies)
        # before comparing.
        window_start = row.window_start if row.window_start.tzinfo else row.window_start.replace(tzinfo=timezone.utc)
        window_end = row.window_end if row.window_end.tzinfo else row.window_end.replace(tzinfo=timezone.utc)
        events = [
            e for e in events
            if (ts := _parse_ts(e.transaction_occurred_at)) is not None and window_start <= ts < window_end
        ]
    return events


def main() -> None:
    tenant_bank_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TENANT_BANK_ID
    db = SessionLocal()
    try:
        # --- Structuring ---
        _header("STRUCTURING -- top near_threshold_ratio entity")
        rows = db.query(EntitySnapshot).filter_by(tenant_bank_id=tenant_bank_id).all()
        top = max((r for r in rows if r.near_threshold_ratio), key=lambda r: r.near_threshold_ratio, default=None)
        if top:
            window_desc = f"week {top.window_start.date()}-{top.window_end.date()}" if top.window_type == "WEEKLY" else "to-date"
            print(f"  {top.party_id} near_threshold_ratio={top.near_threshold_ratio:.3f} ({window_desc}, {top.transaction_count} txns in this row)")
            events = _events_for_snapshot_row(db, top)
            print(f"  raw transaction amounts THIS ROW ACTUALLY COVERS (check how many sit just under $10,000/$3,000):")
            for e in sorted(events, key=lambda e: e.transaction_occurred_at or ""):
                print(f"    {e.transaction_id} | {e.transaction_occurred_at} | amount={e.amount}")

        # --- New Payee Risk ---
        _header("NEW PAYEE RISK -- top new_counterparty_ratio entity")
        top = max((r for r in rows if r.new_counterparty_ratio), key=lambda r: r.new_counterparty_ratio, default=None)
        if top:
            window_desc = f"week {top.window_start.date()}-{top.window_end.date()}" if top.window_type == "WEEKLY" else "to-date"
            print(f"  {top.party_id} new_counterparty_ratio={top.new_counterparty_ratio:.3f} ({window_desc})")
            events = _events_for_snapshot_row(db, top)
            print("  chronological payee sequence THIS ROW ACTUALLY COVERS (check how many are first-time names):")
            for e in sorted(events, key=lambda e: e.transaction_occurred_at or ""):
                print(f"    {e.transaction_occurred_at} | payee={e.payee_name}")

        # --- Funnel Account ---
        _header("FUNNEL ACCOUNT -- top flagged beneficiary")
        compute_beneficiary_snapshots(db, tenant_bank_id=tenant_bank_id)
        top_b = (
            db.query(BeneficiarySnapshot)
            .filter_by(tenant_bank_id=tenant_bank_id, funnel_flag=True)
            .order_by(BeneficiarySnapshot.distinct_senders.desc())
            .first()
        )
        if top_b:
            print(f"  {top_b.beneficiary_key}: {top_b.funnel_reason}")
            events = db.query(CanonicalEvent).filter(CanonicalEvent.payee_name == top_b.beneficiary_key).all()
            print("  raw payments to this beneficiary (check: multiple different senders?):")
            for e in sorted(events, key=lambda e: e.transaction_occurred_at or ""):
                _print_event(e)
        else:
            print("  (no beneficiary currently flagged)")

        # --- Velocity ---
        _header("VELOCITY -- top timeseries_drift_score row")
        score_drift(db, tenant_bank_id=tenant_bank_id)
        top_drift = (
            db.query(EntitySnapshot)
            .filter(EntitySnapshot.tenant_bank_id == tenant_bank_id, EntitySnapshot.timeseries_drift_score.isnot(None))
            .order_by(EntitySnapshot.timeseries_drift_score.desc())
            .first()
        )
        if top_drift:
            print(f"  {top_drift.party_id} week={top_drift.window_start.date()} drift_score={top_drift.timeseries_drift_score:.1f}")
            all_weeks = (
                db.query(EntitySnapshot)
                .filter_by(party_id=top_drift.party_id, tenant_bank_id=tenant_bank_id)
                .order_by(EntitySnapshot.window_start)
                .all()
            )
            print("  this merchant's full weekly sequence (check: does the flagged week actually stand out?):")
            for w in all_weeks:
                marker = " <-- FLAGGED" if w.id == top_drift.id else ""
                print(f"    week={w.window_start.date()} txn_count={w.transaction_count} amount_total={w.amount_total:.2f}{marker}")

        # --- Network Timeout ---
        _header("NETWORK TIMEOUT -- top flagged transaction")
        result = detect_timeouts(db, tenant_bank_id=tenant_bank_id)
        if result["flagged"]:
            top_t = result["flagged"][0]
            print(f"  {top_t}")
        else:
            print("  (none flagged -- timeout_ratio is constant 0 in this dataset, expected)")

        # --- Settlement ---
        _header("SETTLEMENT -- top flagged batch")
        result = detect_unsettled_batches(db, tenant_bank_id=tenant_bank_id)
        if result["flagged"]:
            top_batch = result["flagged"][0]
            print(f"  {top_batch}")
            events = db.query(CanonicalEvent).filter(CanonicalEvent.batch_id == top_batch["batch_id"]).all()
            print("  raw transactions in this batch:")
            for e in events:
                print(f"    {e.transaction_id} | file_reached_settlement={e.file_reached_settlement} | expected={e.expected_settlement_at}")

        # --- Duplicate Payment ---
        _header("DUPLICATE PAYMENT -- top flagged pair")
        result = detect_duplicate_payments(db, tenant_bank_id=tenant_bank_id)
        if result["flagged"]:
            pair = result["flagged"][0]
            print(f"  {pair}")
            for txn_id in (pair["transaction_id_1"], pair["transaction_id_2"]):
                e = db.query(CanonicalEvent).filter_by(transaction_id=txn_id).first()
                if e:
                    _print_event(e)

        # --- Format Rejection ---
        _header("FORMAT REJECTION -- top flagged transaction")
        result = detect_format_rejections(db, tenant_bank_id=tenant_bank_id)
        if result["flagged"]:
            print(f"  {result['flagged'][0]}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
