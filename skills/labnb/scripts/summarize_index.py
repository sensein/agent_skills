#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize relevant idea and experiment entries from the global lab notebook index."
    )
    parser.add_argument("--lab-root", required=True)
    parser.add_argument("--project-slug", required=True)
    parser.add_argument("--limit", type=int, default=5)
    return parser.parse_args()


def normalize_row(row: list[str]) -> dict[str, str] | None:
    if len(row) >= 10:
        return {
            "entry_id": row[0],
            "created_at_utc": row[1],
            "entry_kind": row[2],
            "status": row[3],
            "project_slug": row[4],
            "entry_slug": row[5],
            "objective": row[6],
            "entry_path": row[7],
            "project_root": row[8],
            "parent_id": row[9],
        }
    if len(row) >= 8:
        return {
            "entry_id": row[0],
            "created_at_utc": row[1],
            "entry_kind": "experiment",
            "status": "active",
            "project_slug": row[2],
            "entry_slug": row[3],
            "objective": row[4],
            "entry_path": row[5],
            "project_root": row[6],
            "parent_id": row[7],
        }
    return None


def main() -> int:
    args = parse_args()
    index_tsv = Path(args.lab_root).expanduser().resolve() / "index" / "experiments.tsv"
    if not index_tsv.exists():
        print("# Relevant Lab Entries\n\nNo notebook index found yet.")
        return 0

    rows: list[dict[str, str]] = []
    with index_tsv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        next(reader, None)
        for row in reader:
            normalized = normalize_row(row)
            if normalized is None:
                continue
            if normalized["project_slug"] != args.project_slug:
                continue
            rows.append(normalized)

    rows.sort(key=lambda row: row["created_at_utc"], reverse=True)
    shown = rows[: max(args.limit, 0)]
    idea_count = sum(1 for row in rows if row["entry_kind"] == "idea")
    experiment_count = sum(1 for row in rows if row["entry_kind"] == "experiment")

    print("# Relevant Lab Entries")
    print()
    print(f"Project: `{args.project_slug}`")
    print(f"Matches: {len(rows)}")
    print(f"Ideas: {idea_count}")
    print(f"Experiments: {experiment_count}")
    print()
    if not shown:
        print("No existing ideas or experiments matched this project slug.")
        return 0

    print("## Recent Entries")
    print()
    for row in shown:
        print(
            f"- `{row['entry_id']}` [{row['entry_kind']}/{row['status']}] "
            f"`{row['entry_slug']}`: {row['objective']}"
        )
        print(f"  Path: `{row['entry_path']}`")
        if row["parent_id"]:
            print(f"  Parent: `{row['parent_id']}`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
