#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import string
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register a new experiment in the global lab notebook."
    )
    parser.add_argument("--lab-root", required=True)
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--project-slug", required=True)
    parser.add_argument("--experiment-slug", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--metric-name", default="")
    parser.add_argument("--direction", default="")
    parser.add_argument("--verify-command", default="")
    parser.add_argument("--parent-id", default="")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def random_suffix(length: int = 8) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(length))


def experiment_id(project_slug: str, experiment_slug: str) -> str:
    return f"{utc_now()}--{project_slug}--{experiment_slug}--{random_suffix()}"


@contextmanager
def directory_lock(lock_dir: Path, timeout_s: float = 30.0, poll_s: float = 0.05):
    deadline = time.time() + timeout_s
    while True:
        try:
            lock_dir.mkdir(parents=False, exist_ok=False)
            owner = {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            (lock_dir / "owner.json").write_text(
                json.dumps(owner, indent=2) + "\n",
                encoding="utf-8",
            )
            break
        except FileExistsError:
            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for lock: {lock_dir}")
            time.sleep(poll_s)
    try:
        yield
    finally:
        owner_file = lock_dir / "owner.json"
        if owner_file.exists():
            owner_file.unlink()
        lock_dir.rmdir()


def ensure_layout(lab_root: Path) -> None:
    for relative in ("experiments", "index", "locks"):
        (lab_root / relative).mkdir(parents=True, exist_ok=True)


def write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def sanitize_tsv_field(value: object) -> str:
    return str(value).replace("\t", " ").replace("\n", " ").strip()


def render_index(rows: list[list[str]]) -> str:
    lines = [
        "# Lab Index",
        "",
        f"Total experiments: {len(rows)}",
        "",
        "| Experiment ID | Project | Experiment | Created (UTC) | Objective | Path |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        if len(row) < 6:
            continue
        exp_id, created_at, project_slug, experiment_slug, objective, exp_path = row[:6]
        lines.append(
            f"| `{exp_id}` | `{project_slug}` | `{experiment_slug}` | `{created_at}` | {objective} | `{exp_path}` |"
        )
    lines.append("")
    return "\n".join(lines)


def read_rows(index_tsv: Path) -> list[list[str]]:
    if not index_tsv.exists():
        return []
    rows: list[list[str]] = []
    lines = index_tsv.read_text(encoding="utf-8").splitlines()
    for line in lines[1:]:
        if not line.strip():
            continue
        rows.append(line.split("\t"))
    return rows


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=path.parent
    ) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, path)


def main() -> int:
    args = parse_args()
    lab_root = Path(args.lab_root).expanduser().resolve()
    project_root = Path(args.project_root).expanduser().resolve()
    ensure_layout(lab_root)

    exp_id = experiment_id(args.project_slug, args.experiment_slug)
    exp_dir = lab_root / "experiments" / exp_id
    exp_dir.mkdir(parents=False, exist_ok=False)
    (exp_dir / "artifacts").mkdir()

    metadata = {
        "experiment_id": exp_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_slug": args.project_slug,
        "experiment_slug": args.experiment_slug,
        "objective": args.objective,
        "metric_name": args.metric_name,
        "direction": args.direction,
        "verify_command": args.verify_command,
        "project_root": str(project_root),
        "experiment_dir": str(exp_dir),
        "parent_id": args.parent_id,
    }
    atomic_write(exp_dir / "metadata.json", json.dumps(metadata, indent=2) + "\n")
    plan_md = "\n".join(
        [
            "# Experiment Plan",
            "",
            f"- Goal: {args.objective}",
            f"- Metric: {args.metric_name or 'TBD'}",
            f"- Direction: {args.direction or 'TBD'}",
            f"- Verify command: {args.verify_command or 'TBD'}",
            f"- Project root: {project_root}",
            f"- Parent experiment: {args.parent_id or 'None'}",
            "",
            "## Next Hypothesis",
            "",
            "- ",
            "",
        ]
    )
    write_if_missing(exp_dir / "plan.md", plan_md)
    write_if_missing(exp_dir / "log.md", "# Experiment Log\n\n")
    write_if_missing(
        exp_dir / "results.tsv",
        "\t".join(
            [
                "iteration",
                "timestamp_utc",
                "status",
                "metric_value",
                "commit",
                "notes",
            ]
        )
        + "\n",
    )
    write_if_missing(exp_dir / "summary.md", "# Summary\n\n")

    index_tsv = lab_root / "index" / "experiments.tsv"
    index_md = lab_root / "index" / "index.md"
    lock_dir = lab_root / "locks" / "index.lock"

    with directory_lock(lock_dir):
        if not index_tsv.exists():
            atomic_write(
                index_tsv,
                "\t".join(
                    [
                        "experiment_id",
                        "created_at_utc",
                        "project_slug",
                        "experiment_slug",
                        "objective",
                        "experiment_path",
                        "project_root",
                        "parent_id",
                    ]
                )
                + "\n",
            )
        with index_tsv.open("a", encoding="utf-8") as handle:
            row_data = [
                exp_id,
                metadata["created_at_utc"],
                args.project_slug,
                args.experiment_slug,
                args.objective,
                exp_dir,
                project_root,
                args.parent_id,
            ]
            handle.write("\t".join(sanitize_tsv_field(field) for field in row_data) + "\n")
        rows = read_rows(index_tsv)
        atomic_write(index_md, render_index(rows))

    print(str(exp_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
