"""One-off preprocessing: splits the combined Huntington-pilot Excel files
(Raw_data/Pre_Settlement_Dataset.xlsx, Post_Settlement_Dataset.xlsx -- one
file each holding all 5 rails mixed together) into per-rail CSVs, matching
the /ingest/file contract of one rail per upload.

Usage: .venv/Scripts/python.exe scripts/split_pilot_data.py
"""
from __future__ import annotations

import os

import pandas as pd

RAW_DIR = "Raw_data"
OUT_DIR = os.path.join(RAW_DIR, "split")

TENANT_SLUG = "pilot_bank"  # matches the fixed tenant_bank_id used for this dataset

_FILES = [
    (os.path.join(RAW_DIR, "Pre_Settlement_Dataset.xlsx"), "pre"),
    (os.path.join(RAW_DIR, "Post_Settlement_Dataset.xlsx"), "post"),
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
