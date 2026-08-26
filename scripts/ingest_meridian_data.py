"""Ingests the split Meridian Trust Bank CSVs (Raw_data/split/) through the
real Task 2 pipeline, in PRE-then-POST order per rail, and prints a summary.

Usage: .venv/Scripts/python.exe scripts/ingest_meridian_data.py
"""
from __future__ import annotations

import os

from app.database import Base, SessionLocal, engine
from app.ingestion import process_file

TENANT_BANK_ID = "MERIDIAN_TRUST_BANK"
TENANT_SLUG = "meridian_trust_bank"
RAIL_TYPES = ["CHEQUE", "ACH", "WIRE", "FEDNOW", "CARD"]
SPLIT_DIR = os.path.join("Raw_data", "split")


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        for rail_type in RAIL_TYPES:
            for stage in ("pre", "post"):
                fname = f"{TENANT_SLUG}_{rail_type.lower()}_{stage}.csv"
                path = os.path.join(SPLIT_DIR, fname)
                with open(path, "rb") as f:
                    content = f.read()
                log = process_file(
                    db,
                    tenant_bank_id=TENANT_BANK_ID,
                    rail_type=rail_type,
                    settlement_stage=stage.upper(),
                    filename=fname,
                    content=content,
                )
                unmapped = next((e["columns"] for e in log.errors if e.get("type") == "unmapped_columns"), [])
                row_errors = [e for e in log.errors if e.get("type") == "row_error"]
                print(
                    f"{fname}: rows={log.row_count} mapped={log.rows_mapped} failed={log.rows_failed} "
                    f"unmapped_cols={len(unmapped)} row_errors={len(row_errors)}"
                )
                if unmapped:
                    print("  unmapped:", sorted(unmapped))
                if row_errors:
                    print("  sample row error:", row_errors[0])
    finally:
        db.close()


if __name__ == "__main__":
    main()
