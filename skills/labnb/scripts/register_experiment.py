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

KNOWN_STATUSES = (
    "ideation",
    "planned",
    "started",
    "stopped",
    "completed",
    "terminated",
    "crashed",
    "deferred",
    "promoted",
    "archived",
)

PROV_CONTEXT = {
    "prov": "http://www.w3.org/ns/prov#",
    "labnb": "urn:labnb:",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register a new experiment or idea in the global lab notebook."
    )
    parser.add_argument("--lab-root", required=True)
    parser.add_argument("--project-root", default="")
    parser.add_argument("--project-slug", required=True)
    parser.add_argument("--experiment-slug", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--entry-kind", choices=("experiment", "idea"), default="experiment")
    parser.add_argument("--status", choices=KNOWN_STATUSES, default="")
    parser.add_argument("--metric-name", default="")
    parser.add_argument("--direction", default="")
    parser.add_argument("--verify-command", default="")
    parser.add_argument("--overall-budget", default="")
    parser.add_argument("--loop-budget", default="")
    parser.add_argument("--workspace-root", default="")
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--parent-id", default="")
    return parser


def parse_args() -> argparse.Namespace:
    return build_parser().parse_args()


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
    for relative in ("experiments", "ideas", "workspaces", "index", "locks"):
        (lab_root / relative).mkdir(parents=True, exist_ok=True)


def write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def sanitize_tsv_field(value: object) -> str:
    return str(value).replace("\t", " ").replace("\n", " ").strip()


def normalize_index_row(row: list[str]) -> list[str] | None:
    if len(row) >= 10:
        return row[:10]
    if len(row) >= 8:
        exp_id, created_at, project_slug, experiment_slug, objective, exp_path, project_root, parent_id = row[:8]
        return [
            exp_id,
            created_at,
            "experiment",
            "started",
            project_slug,
            experiment_slug,
            objective,
            exp_path,
            project_root,
            parent_id,
        ]
    return None


def render_index(rows: list[list[str]]) -> str:
    normalized_rows = [normalized for row in rows if (normalized := normalize_index_row(row)) is not None]
    idea_count = sum(1 for row in normalized_rows if row[2] == "idea")
    experiment_count = sum(1 for row in normalized_rows if row[2] == "experiment")
    lines = [
        "# Lab Index",
        "",
        f"Total entries: {len(normalized_rows)}",
        f"Ideas: {idea_count}",
        f"Experiments: {experiment_count}",
        "",
        "| Entry ID | Kind | Status | Project | Slug | Created (UTC) | Objective | Path |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in normalized_rows:
        exp_id, created_at, entry_kind, status, project_slug, experiment_slug, objective, exp_path, _, _ = row
        lines.append(
            f"| `{exp_id}` | `{entry_kind}` | `{status}` | `{project_slug}` | `{experiment_slug}` | `{created_at}` | {objective} | `{exp_path}` |"
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


def append_jsonl(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def path_entity_id(label: str, value: str) -> str:
    return f"urn:labnb:entity:{label}:{value}"


def build_prov_record(
    *,
    entry_id: str,
    entry_kind: str,
    status: str,
    created_at_utc: str,
    entry_dir: Path,
    project_root: str,
    workspace_dir: str,
    source_ids: list[str],
) -> dict[str, object]:
    activity_id = f"urn:labnb:activity:register-entry:{entry_id}"
    entry_entity_id = f"urn:labnb:entity:entry:{entry_id}"
    agent_id = "urn:labnb:agent:software:register-experiment"

    used_entities: list[str] = []
    if project_root:
        used_entities.append(path_entity_id("project-root", project_root))
    if workspace_dir:
        used_entities.append(path_entity_id("workspace-dir", workspace_dir))
    used_entities.extend(f"urn:labnb:entity:entry:{source_id}" for source_id in source_ids)

    entities: list[dict[str, object]] = [
        {
            "prov:id": entry_entity_id,
            "prov:type": ["prov:Entity", f"labnb:{entry_kind.title()}Entry"],
            "prov:generatedAtTime": created_at_utc,
            "prov:atLocation": str(entry_dir),
        }
    ]
    if project_root:
        entities.append(
            {
                "prov:id": path_entity_id("project-root", project_root),
                "prov:type": ["prov:Entity", "labnb:ProjectRoot"],
                "prov:atLocation": project_root,
            }
        )
    if workspace_dir:
        entities.append(
            {
                "prov:id": path_entity_id("workspace-dir", workspace_dir),
                "prov:type": ["prov:Entity", "labnb:WorkspaceDir"],
                "prov:atLocation": workspace_dir,
            }
        )
    for source_id in source_ids:
        entities.append(
            {
                "prov:id": f"urn:labnb:entity:entry:{source_id}",
                "prov:type": ["prov:Entity", "labnb:SourceEntry"],
            }
        )

    return {
        "@context": PROV_CONTEXT,
        "prov:type": ["prov:Bundle", "labnb:RegisterEntryBundle"],
        "prov:entity": entities,
        "prov:activity": {
            "prov:id": activity_id,
            "prov:type": ["prov:Activity", "labnb:RegisterEntry"],
            "prov:startedAtTime": created_at_utc,
            "prov:endedAtTime": created_at_utc,
            "prov:used": used_entities,
            "labnb:status": status,
        },
        "prov:agent": {
            "prov:id": agent_id,
            "prov:type": ["prov:Agent", "prov:SoftwareAgent", "labnb:LabNB"],
        },
        "prov:wasGeneratedBy": {
            "prov:entity": entry_entity_id,
            "prov:activity": activity_id,
        },
        "prov:wasAssociatedWith": {
            "prov:activity": activity_id,
            "prov:agent": agent_id,
        },
        "labnb:entryKind": entry_kind,
        "labnb:status": status,
        "labnb:sourceIds": source_ids,
        "labnb:note": "Best-effort provenance. External changes may occur outside labnb tracking.",
    }


def initialize_workspace(lab_root: Path, exp_id: str, workspace_root: str) -> tuple[Path, Path]:
    workspace_link = lab_root / "workspaces" / exp_id
    if not workspace_root:
        workspace_dir = workspace_link
        workspace_dir.mkdir(parents=False, exist_ok=False)
        return workspace_dir, workspace_link

    external_root = Path(workspace_root).expanduser().resolve()
    external_root.mkdir(parents=True, exist_ok=True)
    workspace_dir = external_root / exp_id
    workspace_dir.mkdir(parents=False, exist_ok=False)
    workspace_link.symlink_to(workspace_dir, target_is_directory=True)
    return workspace_dir, workspace_link


def default_status(entry_kind: str, status: str) -> str:
    if status:
        return status
    return "ideation" if entry_kind == "idea" else "started"


def normalized_source_ids(args: argparse.Namespace) -> list[str]:
    source_ids = [value.strip() for value in args.source_id if value.strip()]
    if args.parent_id and args.parent_id not in source_ids:
        source_ids.append(args.parent_id)
    return source_ids


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.entry_kind == "experiment":
        if not args.overall_budget:
            parser.error("--overall-budget is required when --entry-kind experiment")
        if not args.loop_budget:
            parser.error("--loop-budget is required when --entry-kind experiment")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    validate_args(parser, args)
    lab_root = Path(args.lab_root).expanduser().resolve()
    project_root = Path(args.project_root).expanduser().resolve() if args.project_root else Path()
    ensure_layout(lab_root)

    exp_id = experiment_id(args.project_slug, args.experiment_slug)
    entry_kind = args.entry_kind
    status = default_status(entry_kind, args.status)
    source_ids = normalized_source_ids(args)
    entry_dir = lab_root / ("ideas" if entry_kind == "idea" else "experiments") / exp_id
    entry_dir.mkdir(parents=False, exist_ok=False)
    workspace_dir = Path()
    workspace_link = Path()
    if entry_kind == "experiment":
        workspace_dir, workspace_link = initialize_workspace(
            lab_root=lab_root,
            exp_id=exp_id,
            workspace_root=args.workspace_root,
        )
        (entry_dir / "artifacts").mkdir()

    metadata = {
        "entry_id": exp_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "entry_kind": entry_kind,
        "status": status,
        "project_slug": args.project_slug,
        "entry_slug": args.experiment_slug,
        "objective": args.objective,
        "provenance_mode": "prov-o-best-effort",
        "metric_name": args.metric_name,
        "direction": args.direction,
        "verify_command": args.verify_command,
        "overall_budget": args.overall_budget,
        "loop_budget": args.loop_budget,
        "project_root": str(project_root) if args.project_root else "",
        "entry_dir": str(entry_dir),
        "workspace_dir": str(workspace_dir) if entry_kind == "experiment" else "",
        "workspace_link": str(workspace_link) if entry_kind == "experiment" else "",
        "source_ids": source_ids,
        "parent_id": source_ids[0] if source_ids else "",
        "experiment_id": exp_id,
        "experiment_slug": args.experiment_slug,
        "experiment_dir": str(entry_dir),
    }
    atomic_write(entry_dir / "metadata.json", json.dumps(metadata, indent=2) + "\n")
    append_jsonl(
        entry_dir / "provenance.jsonl",
        build_prov_record(
            entry_id=exp_id,
            entry_kind=entry_kind,
            status=status,
            created_at_utc=metadata["created_at_utc"],
            entry_dir=entry_dir,
            project_root=metadata["project_root"],
            workspace_dir=metadata["workspace_dir"],
            source_ids=source_ids,
        ),
    )
    write_if_missing(
        entry_dir / "provenance.md",
        "\n".join(
            [
                "# Provenance",
                "",
                "- Model: W3C PROV-O terms serialized as JSON lines",
                "- Mode: prov-o-best-effort",
                "- Deletions require explicit user confirmation before labnb performs them.",
                "- External file changes may still happen outside labnb tracking.",
                "",
            ]
        ),
    )
    if entry_kind == "idea":
        idea_md = "\n".join(
            [
                "# Experiment Idea",
                "",
                f"- Goal: {args.objective}",
                f"- Status: {status}",
                "- Provenance: W3C PROV-O best-effort; external changes may exist outside labnb tracking",
                f"- Project root: {project_root if args.project_root else 'TBD'}",
                f"- Source entries: {', '.join(source_ids) if source_ids else 'None'}",
                f"- Overall budget: {args.overall_budget or 'TBD'}",
                f"- Loop budget: {args.loop_budget or 'TBD'}",
                "",
                "## Why This Might Matter",
                "",
                "- ",
                "",
                "## Evidence Or Prior Runs To Revisit",
                "",
                "- ",
                "",
                "## Pickup Plan",
                "",
                "- Smallest useful first slice:",
                "- Promote to experiment when:",
                "- Out of scope for current budget:",
                "",
            ]
        )
        write_if_missing(entry_dir / "idea.md", idea_md)
    else:
        plan_md = "\n".join(
            [
                "# Experiment Plan",
                "",
                f"- Goal: {args.objective}",
                f"- Status: {status}",
                f"- Metric: {args.metric_name or 'TBD'}",
                f"- Direction: {args.direction or 'TBD'}",
                f"- Verify command: {args.verify_command or 'TBD'}",
                f"- Overall budget: {args.overall_budget or 'TBD'}",
                f"- Loop budget: {args.loop_budget or 'TBD'}",
                "- Provenance: W3C PROV-O best-effort; external changes may exist outside labnb tracking",
                f"- Project root: {project_root}",
                f"- Workspace dir: {workspace_dir}",
                f"- Workspace link: {workspace_link}",
                f"- Source entries: {', '.join(source_ids) if source_ids else 'None'}",
                f"- Budget rule: Treat the budget as a ceiling, not as time to fill",
                "",
                "## Feasibility And First Slice",
                "",
                "- Smallest useful iteration:",
                "- Continue only if:",
                "- Budget infeasible when:",
                "- Parallel or downstream work outside this budget:",
                "",
                "## Existing Context Summary",
                "",
                "- Prior experiments or ideas worth revisiting:",
                "- Best place to pick up from:",
                "",
                "## Next Hypothesis",
                "",
                "- ",
                "",
            ]
        )
        write_if_missing(entry_dir / "plan.md", plan_md)
        write_if_missing(entry_dir / "log.md", "# Experiment Log\n\n")
        write_if_missing(
            entry_dir / "results.tsv",
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
        write_if_missing(entry_dir / "summary.md", "# Summary\n\n")

    index_tsv = lab_root / "index" / "experiments.tsv"
    index_md = lab_root / "index" / "index.md"
    lock_dir = lab_root / "locks" / "index.lock"

    with directory_lock(lock_dir):
        if not index_tsv.exists():
            atomic_write(
                index_tsv,
                "\t".join(
                    [
                        "entry_id",
                        "created_at_utc",
                        "entry_kind",
                        "status",
                        "project_slug",
                        "experiment_slug",
                        "objective",
                        "experiment_path",
                        "project_root",
                        "source_ids",
                    ]
                )
                + "\n",
            )
        with index_tsv.open("a", encoding="utf-8") as handle:
            row_data = [
                exp_id,
                metadata["created_at_utc"],
                entry_kind,
                status,
                args.project_slug,
                args.experiment_slug,
                args.objective,
                entry_dir,
                project_root if args.project_root else "",
                ",".join(source_ids),
            ]
            handle.write("\t".join(sanitize_tsv_field(field) for field in row_data) + "\n")
        rows = read_rows(index_tsv)
        atomic_write(index_md, render_index(rows))

    print(str(entry_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
