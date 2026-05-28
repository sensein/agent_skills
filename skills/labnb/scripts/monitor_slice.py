#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


PROV_CONTEXT = {
    "prov": "http://www.w3.org/ns/prov#",
    "labnb": "urn:labnb:",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start, check, or finish a monitored labnb loop slice."
    )
    parser.add_argument("action", choices=("start", "check", "finish"))
    parser.add_argument("--experiment-dir", required=True)
    parser.add_argument("--now", default="")
    parser.add_argument("--status-on-exhausted", default="budget_exhausted")
    parser.add_argument("--final-status", default="completed")
    return parser.parse_args()


def utc_now(now: str) -> datetime:
    if now:
        return datetime.fromisoformat(now.replace("Z", "+00:00")).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def elapsed_seconds(started_at_utc: str, now: datetime) -> int:
    if not started_at_utc:
        return 0
    started = datetime.fromisoformat(started_at_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    return max(int((now - started).total_seconds()), 0)


def build_prov_record(
    *,
    metadata: dict[str, object],
    action: str,
    now: datetime,
    state: dict[str, object],
) -> dict[str, object]:
    timestamp = isoformat_utc(now)
    entry_id = str(metadata["entry_id"])
    activity_id = f"urn:labnb:activity:{action}:{entry_id}:{int(now.timestamp())}"
    entry_entity_id = f"urn:labnb:entity:entry:{entry_id}"
    state_entity_id = f"urn:labnb:entity:loop-state:{entry_id}"
    agent_id = "urn:labnb:agent:software:monitor-slice"
    return {
        "@context": PROV_CONTEXT,
        "prov:type": ["prov:Bundle", "labnb:SliceMonitorBundle"],
        "prov:entity": [
            {
                "prov:id": entry_entity_id,
                "prov:type": ["prov:Entity", "labnb:ExperimentEntry"],
                "prov:atLocation": str(metadata["entry_dir"]),
            },
            {
                "prov:id": state_entity_id,
                "prov:type": ["prov:Entity", "labnb:LoopState"],
                "prov:generatedAtTime": timestamp,
                "labnb:status": state["status"],
                "labnb:remainingLoopSeconds": state["remaining_loop_seconds"],
                "labnb:remainingOverallSeconds": state["remaining_overall_seconds"],
            },
        ],
        "prov:activity": {
            "prov:id": activity_id,
            "prov:type": ["prov:Activity", f"labnb:{action.title()}Slice"],
            "prov:startedAtTime": timestamp,
            "prov:endedAtTime": timestamp,
            "prov:used": [entry_entity_id, state_entity_id],
        },
        "prov:agent": {
            "prov:id": agent_id,
            "prov:type": ["prov:Agent", "prov:SoftwareAgent", "labnb:LabNB"],
        },
        "prov:wasGeneratedBy": {
            "prov:entity": state_entity_id,
            "prov:activity": activity_id,
        },
        "prov:wasAssociatedWith": {
            "prov:activity": activity_id,
            "prov:agent": agent_id,
        },
        "labnb:action": action,
        "labnb:stateSnapshot": state,
    }


def refresh_state(state: dict[str, object], now: datetime) -> dict[str, object]:
    started_at_utc = str(state.get("slice_started_at_utc", ""))
    slice_elapsed = elapsed_seconds(started_at_utc, now)
    completed_elapsed = int(state.get("completed_elapsed_seconds", 0))
    overall_elapsed = completed_elapsed + slice_elapsed
    loop_budget_seconds = int(state.get("loop_budget_seconds", 0))
    overall_budget_seconds = int(state.get("overall_budget_seconds", 0))
    refreshed = dict(state)
    refreshed["slice_elapsed_seconds"] = slice_elapsed
    refreshed["overall_elapsed_seconds"] = overall_elapsed
    refreshed["remaining_loop_seconds"] = max(loop_budget_seconds - slice_elapsed, 0)
    refreshed["remaining_overall_seconds"] = max(overall_budget_seconds - overall_elapsed, 0)
    refreshed["last_checked_at_utc"] = isoformat_utc(now)
    return refreshed


def update_metadata_status(metadata_path: Path, status: str) -> dict[str, object]:
    metadata = read_json(metadata_path)
    metadata["status"] = status
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


def initial_state(metadata: dict[str, object]) -> dict[str, object]:
    return {
        "status": "idle",
        "overall_budget": metadata["overall_budget"],
        "overall_budget_seconds": int(metadata["overall_budget_seconds"]),
        "loop_budget": metadata["loop_budget"],
        "loop_budget_seconds": int(metadata["loop_budget_seconds"]),
        "completed_elapsed_seconds": 0,
        "current_slice_index": 0,
        "slice_started_at_utc": "",
        "slice_elapsed_seconds": 0,
        "overall_elapsed_seconds": 0,
        "remaining_loop_seconds": int(metadata["loop_budget_seconds"]),
        "remaining_overall_seconds": int(metadata["overall_budget_seconds"]),
        "last_checked_at_utc": str(metadata["created_at_utc"]),
    }


def load_state_from_provenance(provenance_path: Path, metadata: dict[str, object]) -> dict[str, object]:
    if not provenance_path.exists():
        return initial_state(metadata)
    lines = provenance_path.read_text(encoding="utf-8").splitlines()
    for line in reversed(lines):
        if not line.strip():
            continue
        payload = json.loads(line)
        snapshot = payload.get("labnb:stateSnapshot")
        if isinstance(snapshot, dict):
            return dict(snapshot)
    return initial_state(metadata)


def main() -> int:
    args = parse_args()
    now = utc_now(args.now)
    experiment_dir = Path(args.experiment_dir).expanduser().resolve()
    metadata_path = experiment_dir / "metadata.json"
    provenance_path = experiment_dir / "provenance.jsonl"

    metadata = read_json(metadata_path)
    if metadata.get("entry_kind") != "experiment":
        raise SystemExit("monitor_slice only supports experiment entries")
    state = load_state_from_provenance(provenance_path, metadata)

    if args.action == "start":
        if state.get("slice_started_at_utc"):
            raise SystemExit("slice already running")
        state["current_slice_index"] = int(state.get("current_slice_index", 0)) + 1
        state["slice_started_at_utc"] = isoformat_utc(now)
        state["status"] = "running"
        state = refresh_state(state, now)
        metadata = update_metadata_status(metadata_path, "started")
    elif args.action == "check":
        state = refresh_state(state, now)
        if state["remaining_loop_seconds"] == 0 or state["remaining_overall_seconds"] == 0:
            state["status"] = "budget_exhausted"
            metadata = update_metadata_status(metadata_path, args.status_on_exhausted)
        else:
            metadata = read_json(metadata_path)
    else:
        state = refresh_state(state, now)
        state["completed_elapsed_seconds"] = int(state.get("completed_elapsed_seconds", 0)) + int(
            state.get("slice_elapsed_seconds", 0)
        )
        state["slice_started_at_utc"] = ""
        state["slice_elapsed_seconds"] = 0
        state["status"] = args.final_status
        state = refresh_state(state, now)
        metadata = update_metadata_status(metadata_path, args.final_status)

    append_jsonl(
        provenance_path,
        build_prov_record(metadata=metadata, action=args.action, now=now, state=state),
    )
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
