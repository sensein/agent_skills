#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


PROV_CONTEXT = {
    "prov": "http://www.w3.org/ns/prov#",
    "labnb": "urn:labnb:",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote a labnb idea entry into a new experiment entry."
    )
    parser.add_argument("--lab-root", required=True)
    parser.add_argument("--idea-id", required=True)
    parser.add_argument("--project-root", default="")
    parser.add_argument("--experiment-slug", default="")
    parser.add_argument("--metric-name", default="")
    parser.add_argument("--direction", default="")
    parser.add_argument("--verify-command", default="")
    parser.add_argument("--overall-budget", required=True)
    parser.add_argument("--loop-budget", required=True)
    parser.add_argument("--workspace-root", default="")
    parser.add_argument("--source-id", action="append", default=[])
    return parser.parse_args()


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def idea_dir_for_id(lab_root: Path, idea_id: str) -> Path:
    return lab_root / "ideas" / idea_id


def register_script_path() -> Path:
    return Path(__file__).resolve().with_name("register_experiment.py")


def build_promotion_prov(
    *,
    idea_metadata: dict[str, object],
    experiment_id: str,
    timestamp: str,
) -> dict[str, object]:
    idea_id = str(idea_metadata["entry_id"])
    activity_id = f"urn:labnb:activity:promote-idea:{idea_id}:{timestamp}"
    idea_entity_id = f"urn:labnb:entity:entry:{idea_id}"
    experiment_entity_id = f"urn:labnb:entity:entry:{experiment_id}"
    agent_id = "urn:labnb:agent:software:promote-idea"
    return {
        "@context": PROV_CONTEXT,
        "prov:type": ["prov:Bundle", "labnb:PromoteIdeaBundle"],
        "prov:entity": [
            {
                "prov:id": idea_entity_id,
                "prov:type": ["prov:Entity", "labnb:IdeaEntry"],
                "prov:atLocation": str(idea_metadata["entry_dir"]),
            },
            {
                "prov:id": experiment_entity_id,
                "prov:type": ["prov:Entity", "labnb:ExperimentEntry"],
            },
        ],
        "prov:activity": {
            "prov:id": activity_id,
            "prov:type": ["prov:Activity", "labnb:PromoteIdea"],
            "prov:startedAtTime": timestamp,
            "prov:endedAtTime": timestamp,
            "prov:used": [idea_entity_id],
        },
        "prov:agent": {
            "prov:id": agent_id,
            "prov:type": ["prov:Agent", "prov:SoftwareAgent", "labnb:LabNB"],
        },
        "prov:wasGeneratedBy": {
            "prov:entity": experiment_entity_id,
            "prov:activity": activity_id,
        },
        "prov:wasAssociatedWith": {
            "prov:activity": activity_id,
            "prov:agent": agent_id,
        },
        "labnb:ideaId": idea_id,
        "labnb:experimentId": experiment_id,
        "labnb:status": "promoted",
    }


def main() -> int:
    args = parse_args()
    lab_root = Path(args.lab_root).expanduser().resolve()
    idea_dir = idea_dir_for_id(lab_root, args.idea_id)
    metadata_path = idea_dir / "metadata.json"
    if not metadata_path.exists():
        raise SystemExit(f"Idea metadata not found: {metadata_path}")

    idea_metadata = read_json(metadata_path)
    if idea_metadata.get("entry_kind") != "idea":
        raise SystemExit(f"Entry is not an idea: {args.idea_id}")

    project_root = args.project_root or str(idea_metadata.get("project_root", ""))
    experiment_slug = args.experiment_slug or str(idea_metadata.get("entry_slug", ""))
    source_ids = [value.strip() for value in args.source_id if value.strip()]
    if args.idea_id not in source_ids:
        source_ids.insert(0, args.idea_id)

    command = [
        sys.executable,
        str(register_script_path()),
        "--lab-root",
        str(lab_root),
        "--project-root",
        project_root,
        "--project-slug",
        str(idea_metadata["project_slug"]),
        "--experiment-slug",
        experiment_slug,
        "--objective",
        str(idea_metadata["objective"]),
        "--entry-kind",
        "experiment",
        "--metric-name",
        args.metric_name,
        "--direction",
        args.direction,
        "--verify-command",
        args.verify_command,
        "--overall-budget",
        args.overall_budget,
        "--loop-budget",
        args.loop_budget,
    ]
    if args.workspace_root:
        command.extend(["--workspace-root", args.workspace_root])
    for source_id in source_ids:
        command.extend(["--source-id", source_id])

    result = subprocess.run(command, check=True, capture_output=True, text=True)
    experiment_dir = Path(result.stdout.strip())
    experiment_metadata = read_json(experiment_dir / "metadata.json")

    idea_metadata["status"] = "promoted"
    write_json(metadata_path, idea_metadata)

    timestamp = isoformat_utc(datetime.now(timezone.utc))
    append_jsonl(
        idea_dir / "provenance.jsonl",
        build_promotion_prov(
            idea_metadata=idea_metadata,
            experiment_id=str(experiment_metadata["entry_id"]),
            timestamp=timestamp,
        ),
    )

    print(str(experiment_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
