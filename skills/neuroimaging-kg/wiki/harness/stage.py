"""Prepare a task directory's starting state for the WikiSkill rollouts.

Usage: python stage.py <task_dir> <stage-item> [...]
Items: caltech_inputs, caltech_built, caltech_local_table, caltech_messy_table.
The defects planted in the two lab tables are listed in DEFECTS so check.py can score against them.
"""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE.parents[1] / "examples" / "abide_one_site"

DEFECTS = {
    "caltech_local_table": {"zero_padded_ids": True, "age_plus_5": ["51460", "51470"], "dropped": ["51493"],
                            "extra": ["9999999"]},
    "caltech_messy_table": {"bom": True, "whitespace_id": "51465", "duplicate": "51462", "age_plus_10": ["51475"],
                            "fiq_missing": "-9999"},
}


def caltech_rows(task: Path) -> list[dict]:
    with open(task / "data" / "Phenotypic_V1_0b_preprocessed1.csv", newline="") as fh:
        return [r for r in csv.DictReader(fh) if r["SITE_ID"] == "CALTECH"]


def main() -> None:
    task = Path(sys.argv[1]).resolve()
    task.mkdir(parents=True, exist_ok=True)
    for item in sys.argv[2:]:
        if item == "caltech_inputs":
            subprocess.run(["uv", "run", "-q", str(DEMO / "fetch_inputs.py"), "--site", "Caltech", "--dest", "data"],
                           cwd=task, check=True, stdout=subprocess.DEVNULL)
        elif item == "caltech_built":
            subprocess.run(["bash", str(DEMO / "run_demo.sh"), "Caltech"], cwd=task, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif item == "caltech_local_table":
            d = DEFECTS[item]
            rows = [r for r in caltech_rows(task) if str(int(r["SUB_ID"])) not in d["dropped"]]
            with open(task / "caltech_local.csv", "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["subject", "age_years", "group"])
                for r in rows:
                    sid = str(int(r["SUB_ID"]))
                    age = float(r["AGE_AT_SCAN"]) + (5 if sid in d["age_plus_5"] else 0)
                    w.writerow([sid.zfill(7), f"{age:g}", r["DX_GROUP"]])
                w.writerow(["9999999", "30", "2"])
        elif item == "caltech_messy_table":
            d = DEFECTS[item]
            out = []
            for r in caltech_rows(task):
                sid = str(int(r["SUB_ID"]))
                age = float(r["AGE_AT_SCAN"]) + (10 if sid in d["age_plus_10"] else 0)
                fiq = r["FIQ"]
                out.append([f" {sid} " if sid == d["whitespace_id"] else sid, f"{age:g}", r["DX_GROUP"], fiq])
                if sid == d["duplicate"]:
                    out.append([sid, f"{age + 3:g}", r["DX_GROUP"], fiq])
            with open(task / "caltech_messy.csv", "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["SUB_ID", "AGE_AT_SCAN", "DX_GROUP", "FIQ"])
                w.writerows(out)
        else:
            sys.exit(f"unknown stage item {item}")


if __name__ == "__main__":
    main()
