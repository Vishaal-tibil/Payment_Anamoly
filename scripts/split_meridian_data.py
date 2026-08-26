"""One-off preprocessing: splits the combined Meridian Trust Bank Excel files
(Raw_data/pre_settlement.xlsx, post_settlement.xlsx -- anomaly-ready schema,
all 5 rails mixed together) into per-rail CSVs, matching the /ingest/file
contract of one rail per upload.

Usage: .venv/Scripts/python.exe scripts/split_meridian_data.py
"""
from __future__ import annotations

import os

import pandas as pd

RAW_DIR = "Raw_data"
OUT_DIR = os.path.join(RAW_DIR, "split")

TENANT_SLUG = "meridian_trust_bank"

_FILES = [
    (os.path.join(RAW_DIR, "pre_settlement (1).xlsx"), "pre"),
    (os.path.join(RAW_DIR, "post_settlement (1).xlsx"), "post"),
]


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    for path, stage in _FILES:
        df = pd.read_excel(path, dtype=str)
        for rail, group in df.groupby("rail"):
            out_name = f"{TENANT_SLUG}_{rail.lower()}_{stage}.csv"
            out_path = os.path.join(OUT_DIR, out_name)
            group.to_csv(out_path, index=False)
            print(f"{out_path}: {len(group)} rows")


if __name__ == "__main__":
    main()
