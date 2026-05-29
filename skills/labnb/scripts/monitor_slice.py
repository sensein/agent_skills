#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path


PROV_CONTEXT = {
    "prov": "http://www.w3.org/ns/prov#",
    "labnb": "urn:labnb:",
}

# Statuses that count as a failed / crashed iteration when read from results.tsv.
FAILURE_STATUSES = {
    "crash",
    "crashed",
    "error",
    "errored",
    "fail",
    "failed",
    "exception",
    "timeout",
    "oom",
}

DEFAULT_WARN_FRACTION = 0.8

# Lower number = higher priority when several break signals fire at once. This
# decides which status the entry is moved to and which reason is primary.
CATEGORY_PRIORITY = {
    "correctness": 0,
    "budget": 1,
    "engineering": 2,
    "validity": 3,
}

DEFAULT_BREAK_STATUS = {
    "correctness": "crashed",
    "budget": "budget_exhausted",
    "engineering": "stopped",
    "validity": "stopped",
}


@dataclass
class Signal:
    category: str  # budget | engineering | correctness | validity
    reason: str
    severity: str  # warn | break
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Start, check, or finish a monitored labnb loop slice. The check "
            "action can break a slice on budget, engineering (pace/stall/"
            "resource), correctness (repeated failures), or validity (no "
            "improvement / guardrail) signals, and exits non-zero on break so "
            "a shell loop stops on its own."
        )
    )
    parser.add_argument("action", choices=("start", "check", "finish"))
    parser.add_argument("--experiment-dir", required=True)
    parser.add_argument("--now", default="")
    parser.add_argument("--status-on-exhausted", default="budget_exhausted")
    parser.add_argument("--status-on-break", default="")
    parser.add_argument("--final-status", default="completed")

    # Budget hardening.
    parser.add_argument(
        "--reserve-seconds",
        type=int,
        default=0,
        help="Break while this much budget remains, leaving slack for "
        "verification, logging, and summary.",
    )
    parser.add_argument(
        "--warn-fraction",
        type=float,
        default=DEFAULT_WARN_FRACTION,
        help="Emit an advisory warning once this fraction of the loop or "
        "overall budget is consumed (0 disables).",
    )

    # Engineering signals.
    parser.add_argument(
        "--stall-seconds",
        type=int,
        default=0,
        help="Break if no new results.tsv row has been logged for this many "
        "seconds (0 disables).",
    )
    parser.add_argument(
        "--no-pace",
        action="store_true",
        help="Disable the pace projection that breaks when another iteration "
        "at the recent cadence will not fit the remaining loop budget.",
    )
    parser.add_argument(
        "--usage-file",
        default="",
        help="Path to a con/duct info.json or usage.jsonl to read peak "
        "resource usage from.",
    )
    parser.add_argument(
        "--max-rss-bytes",
        type=int,
        default=0,
        help="Break if peak RSS from --usage-file exceeds this many bytes "
        "(0 disables).",
    )
    parser.add_argument(
        "--max-pmem",
        type=float,
        default=0.0,
        help="Break if peak memory percent from --usage-file exceeds this "
        "(0 disables).",
    )

    # Correctness signals.
    parser.add_argument(
        "--max-failures",
        type=int,
        default=0,
        help="Break after this many consecutive failed/crashed iterations in "
        "results.tsv (0 disables).",
    )

    # Validity signals.
    parser.add_argument(
        "--patience",
        type=int,
        default=0,
        help="Break when the metric has not improved for this many logged "
        "iterations (0 disables). Requires a direction.",
    )
    parser.add_argument(
        "--metric-guardrail",
        type=float,
        default=None,
        help="Break when the latest metric is worse than this bound for the "
        "experiment direction.",
    )
    parser.add_argument(
        "--direction",
        default="",
        help="Override the optimization direction (higher|lower); defaults to "
        "the value recorded in metadata.json.",
    )

    # Exit-code contract.
    parser.add_argument(
        "--break-exit-code",
        type=int,
        default=4,
        help="Process exit code to return when a check decides to break.",
    )
    parser.add_argument(
        "--warn-exit-code",
        type=int,
        default=0,
        help="Process exit code to return on an advisory warning.",
    )
    parser.add_argument(
        "--exit-zero",
        action="store_true",
        help="Always exit 0 regardless of decision (legacy behavior).",
    )
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


def parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_metric(value: str) -> float | None:
    text = (value or "").strip()
    if not text or text.upper() == "TBD":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def elapsed_seconds(started_at_utc: str, now: datetime) -> int:
    started = parse_iso(started_at_utc)
    if started is None:
        return 0
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
                "labnb:decision": state.get("decision", "n/a"),
                "labnb:breakCategory": state.get("break_category", ""),
                "labnb:breakReason": state.get("break_reason", ""),
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


# --- observation parsing -------------------------------------------------


def read_results_rows(results_path: Path) -> list[dict[str, object]]:
    if not results_path.exists():
        return []
    lines = results_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    rows: list[dict[str, object]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = line.split("\t")
        record = dict(zip(header, cells))
        rows.append(
            {
                "iteration": record.get("iteration", ""),
                "timestamp_utc": record.get("timestamp_utc", ""),
                "status": (record.get("status", "") or "").strip().lower(),
                "metric": parse_metric(record.get("metric_value", "")),
            }
        )
    return rows


def is_improvement(candidate: float, best: float, direction: str) -> bool:
    if direction == "higher":
        return candidate > best
    if direction == "lower":
        return candidate < best
    return False


def metric_diagnostics(rows: list[dict[str, object]], direction: str) -> dict[str, object]:
    numeric = [r for r in rows if isinstance(r["metric"], float)]
    best: float | None = None
    since_improvement = 0
    for row in numeric:
        value = float(row["metric"])  # type: ignore[arg-type]
        if best is None or is_improvement(value, best, direction):
            best = value
            since_improvement = 0
        else:
            since_improvement += 1

    consecutive_failures = 0
    for row in reversed(rows):
        if row["status"] in FAILURE_STATUSES:
            consecutive_failures += 1
        else:
            break

    latest = float(numeric[-1]["metric"]) if numeric else None  # type: ignore[arg-type]
    return {
        "iterations_logged": len(rows),
        "numeric_iterations": len(numeric),
        "best_metric": best,
        "latest_metric": latest,
        "iterations_since_improvement": since_improvement,
        "consecutive_failures": consecutive_failures,
    }


def pace_diagnostics(rows: list[dict[str, object]], now: datetime) -> dict[str, object]:
    timestamps = [ts for ts in (parse_iso(str(r["timestamp_utc"])) for r in rows) if ts is not None]
    last_row_age: int | None = None
    if timestamps:
        last_row_age = max(int((now - timestamps[-1]).total_seconds()), 0)

    gaps = [
        max(int((b - a).total_seconds()), 0)
        for a, b in zip(timestamps, timestamps[1:])
    ]
    recent = gaps[-5:]
    avg_iter = int(sum(recent) / len(recent)) if recent else None
    return {
        "last_row_age_seconds": last_row_age,
        "avg_iter_seconds": avg_iter,
    }


def extract_peak_usage(usage_path: Path) -> dict[str, float | None]:
    """Best-effort read of peak RSS / %mem / %cpu from a con/duct log."""
    peak: dict[str, float | None] = {"peak_rss": None, "peak_pmem": None, "peak_pcpu": None}
    if not usage_path.exists():
        return peak
    text = usage_path.read_text(encoding="utf-8")

    def take(summary: dict[str, object]) -> None:
        for key in ("peak_rss", "peak_pmem", "peak_pcpu"):
            value = summary.get(key)
            if isinstance(value, (int, float)):
                current = peak[key]
                peak[key] = value if current is None else max(current, float(value))

    try:
        data = json.loads(text)
        summary = data.get("execution_summary", data) if isinstance(data, dict) else {}
        if isinstance(summary, dict):
            take(summary)
        return peak
    except json.JSONDecodeError:
        pass

    # JSON Lines: scan each sample for resource fields.
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            take(payload)
            totals = payload.get("totals")
            if isinstance(totals, dict):
                rss = totals.get("rss")
                pmem = totals.get("pmem")
                pcpu = totals.get("pcpu")
                take({"peak_rss": rss, "peak_pmem": pmem, "peak_pcpu": pcpu})
    return peak


# --- break evaluation ----------------------------------------------------


def evaluate_signals(
    *,
    state: dict[str, object],
    args: argparse.Namespace,
    direction: str,
    metric: dict[str, object],
    pace: dict[str, object],
    usage: dict[str, float | None],
) -> list[Signal]:
    signals: list[Signal] = []

    remaining_loop = int(state["remaining_loop_seconds"])
    remaining_overall = int(state["remaining_overall_seconds"])
    loop_budget = int(state.get("loop_budget_seconds", 0))
    overall_budget = int(state.get("overall_budget_seconds", 0))
    reserve = max(args.reserve_seconds, 0)
    eff_loop = remaining_loop - reserve
    eff_overall = remaining_overall - reserve

    # Budget (always evaluated).
    if eff_loop <= 0:
        signals.append(
            Signal("budget", "loop_budget_exhausted", "break",
                   f"loop budget spent (reserve={reserve}s)")
        )
    if eff_overall <= 0:
        signals.append(
            Signal("budget", "overall_budget_exhausted", "break",
                   f"overall budget spent (reserve={reserve}s)")
        )
    if args.warn_fraction and not any(s.category == "budget" for s in signals):
        for label, spent, budget in (
            ("loop", loop_budget - remaining_loop, loop_budget),
            ("overall", overall_budget - remaining_overall, overall_budget),
        ):
            if budget > 0 and spent >= args.warn_fraction * budget:
                signals.append(
                    Signal("budget", f"{label}_budget_warning", "warn",
                           f"{label} budget {spent}/{budget}s consumed")
                )

    # Engineering: pace projection.
    avg_iter = pace.get("avg_iter_seconds")
    if not args.no_pace and isinstance(avg_iter, int) and avg_iter > 0:
        if avg_iter > max(eff_loop, 0):
            signals.append(
                Signal("engineering", "pace_insufficient_budget", "break",
                       f"next iteration ~{avg_iter}s will not fit remaining "
                       f"{remaining_loop}s (reserve={reserve}s)")
            )

    # Engineering: stall.
    last_age = pace.get("last_row_age_seconds")
    if args.stall_seconds and isinstance(last_age, int) and last_age > args.stall_seconds:
        signals.append(
            Signal("engineering", "stalled", "break",
                   f"no new results row for {last_age}s (>{args.stall_seconds}s)")
        )

    # Engineering: resource ceilings.
    if args.max_rss_bytes and isinstance(usage["peak_rss"], (int, float)):
        if usage["peak_rss"] > args.max_rss_bytes:
            signals.append(
                Signal("engineering", "resource_rss", "break",
                       f"peak RSS {int(usage['peak_rss'])}B > {args.max_rss_bytes}B")
            )
    if args.max_pmem and isinstance(usage["peak_pmem"], (int, float)):
        if usage["peak_pmem"] > args.max_pmem:
            signals.append(
                Signal("engineering", "resource_pmem", "break",
                       f"peak mem {usage['peak_pmem']}% > {args.max_pmem}%")
            )

    # Correctness: repeated failures.
    failures = int(metric["consecutive_failures"])
    if args.max_failures and failures >= args.max_failures:
        signals.append(
            Signal("correctness", "repeated_failures", "break",
                   f"{failures} consecutive failed iterations (>={args.max_failures})")
        )

    # Validity: no improvement (plateau).
    since = int(metric["iterations_since_improvement"])
    if args.patience and direction in ("higher", "lower") and since >= args.patience:
        signals.append(
            Signal("validity", "no_improvement", "break",
                   f"metric flat/regressing for {since} iterations (>={args.patience})")
        )

    # Validity: hard guardrail on the latest metric.
    latest = metric["latest_metric"]
    if args.metric_guardrail is not None and isinstance(latest, float) and direction in ("higher", "lower"):
        violated = (
            (direction == "higher" and latest < args.metric_guardrail)
            or (direction == "lower" and latest > args.metric_guardrail)
        )
        if violated:
            signals.append(
                Signal("validity", "guardrail_violation", "break",
                       f"latest metric {latest} past guardrail {args.metric_guardrail} "
                       f"({direction} is better)")
            )

    return signals


def decide(signals: list[Signal]) -> tuple[str, Signal | None]:
    breaks = [s for s in signals if s.severity == "break"]
    if breaks:
        primary = min(breaks, key=lambda s: CATEGORY_PRIORITY.get(s.category, 99))
        return "break", primary
    if any(s.severity == "warn" for s in signals):
        return "warn", None
    return "continue", None


def run_check(state: dict[str, object], metadata: dict[str, object], args: argparse.Namespace,
              now: datetime, experiment_dir: Path, metadata_path: Path) -> tuple[dict[str, object], dict[str, object], int]:
    state = refresh_state(state, now)
    direction = (args.direction or str(metadata.get("direction", ""))).strip().lower()

    rows = read_results_rows(experiment_dir / "results.tsv")
    metric = metric_diagnostics(rows, direction)
    pace = pace_diagnostics(rows, now)
    usage = (
        extract_peak_usage(Path(args.usage_file).expanduser())
        if args.usage_file
        else {"peak_rss": None, "peak_pmem": None, "peak_pcpu": None}
    )

    signals = evaluate_signals(
        state=state, args=args, direction=direction,
        metric=metric, pace=pace, usage=usage,
    )
    decision, primary = decide(signals)

    state["decision"] = decision
    state["signals"] = [asdict(s) for s in signals]
    state["diagnostics"] = {**metric, **pace, "peak_usage": usage, "direction": direction}
    state["break_category"] = primary.category if primary else ""
    state["break_reason"] = primary.reason if primary else ""

    if decision == "break" and primary is not None:
        if primary.category == "budget":
            state["status"] = "budget_exhausted"
            meta_status = args.status_on_break or args.status_on_exhausted
        else:
            state["status"] = DEFAULT_BREAK_STATUS[primary.category]
            meta_status = args.status_on_break or DEFAULT_BREAK_STATUS[primary.category]
        metadata = update_metadata_status(metadata_path, meta_status)
    else:
        state["status"] = "running"
        metadata = read_json(metadata_path)

    if args.exit_zero:
        code = 0
    elif decision == "break":
        code = args.break_exit_code
    elif decision == "warn":
        code = args.warn_exit_code
    else:
        code = 0
    return state, metadata, code


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

    code = 0
    if args.action == "start":
        if state.get("slice_started_at_utc"):
            raise SystemExit("slice already running")
        state["current_slice_index"] = int(state.get("current_slice_index", 0)) + 1
        state["slice_started_at_utc"] = isoformat_utc(now)
        state["status"] = "running"
        state["decision"] = "n/a"
        state["break_category"] = ""
        state["break_reason"] = ""
        state = refresh_state(state, now)
        metadata = update_metadata_status(metadata_path, "started")
    elif args.action == "check":
        state, metadata, code = run_check(
            state, metadata, args, now, experiment_dir, metadata_path
        )
    else:
        state = refresh_state(state, now)
        state["completed_elapsed_seconds"] = int(state.get("completed_elapsed_seconds", 0)) + int(
            state.get("slice_elapsed_seconds", 0)
        )
        state["slice_started_at_utc"] = ""
        state["slice_elapsed_seconds"] = 0
        state["status"] = args.final_status
        state["decision"] = "n/a"
        state["break_category"] = ""
        state["break_reason"] = ""
        state = refresh_state(state, now)
        metadata = update_metadata_status(metadata_path, args.final_status)

    append_jsonl(
        provenance_path,
        build_prov_record(metadata=metadata, action=args.action, now=now, state=state),
    )
    print(json.dumps(state, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
