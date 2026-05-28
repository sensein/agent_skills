#!/usr/bin/env python3
"""Print a compact, dependency-free review summary of a con/duct run.

Reads a duct ``*info.json`` file and reports the headline numbers a reviewer
cares about: exit code, wall-clock time, peak/average memory, and peak/average
CPU. Accepts an ``info.json`` path, an output prefix, or a directory (in which
case the newest ``*info.json`` under it is used).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def resolve_info_path(target: Path) -> Path:
    """Resolve a user-supplied target to a concrete ``info.json`` file."""
    if target.is_file():
        return target
    if target.is_dir():
        candidates = sorted(
            target.glob("**/*info.json"),
            key=lambda p: p.stat().st_mtime,
        )
        if not candidates:
            raise FileNotFoundError(f"no *info.json found under {target}")
        return candidates[-1]
    # Treat as an output prefix, e.g. ".duct/logs/run_".
    prefix_path = Path(str(target) + "info.json")
    if prefix_path.is_file():
        return prefix_path
    raise FileNotFoundError(f"no duct info.json found for {target}")


def human_bytes(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "n/a"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


def pct(value: object) -> str:
    return f"{value:.1f}%" if isinstance(value, (int, float)) else "n/a"


def seconds(value: object) -> str:
    return f"{value:.3f} s" if isinstance(value, (int, float)) else "n/a"


def build_summary(info: dict) -> dict:
    summary = info.get("execution_summary", {})
    return {
        "command": summary.get("command") or info.get("command"),
        "exit_code": summary.get("exit_code"),
        "wall_clock_time": summary.get("wall_clock_time"),
        "peak_rss": summary.get("peak_rss"),
        "average_rss": summary.get("average_rss"),
        "peak_pcpu": summary.get("peak_pcpu"),
        "average_pcpu": summary.get("average_pcpu"),
        "peak_pmem": summary.get("peak_pmem"),
        "num_samples": summary.get("num_samples"),
        "working_directory": summary.get("working_directory")
        or info.get("working_directory"),
    }


def render(summary: dict, info_path: Path) -> str:
    no_samples = not summary.get("num_samples")
    lines = [
        f"duct run: {info_path}",
        f"  command:        {summary['command']}",
        f"  exit code:      {summary['exit_code']}",
        f"  wall clock:     {seconds(summary['wall_clock_time'])}",
        f"  peak memory:    {human_bytes(summary['peak_rss'])} (avg {human_bytes(summary['average_rss'])})",
        f"  peak CPU:       {pct(summary['peak_pcpu'])} (avg {pct(summary['average_pcpu'])})",
        f"  peak mem %:     {pct(summary['peak_pmem'])}",
        f"  samples:        {summary['num_samples']}",
        f"  working dir:    {summary['working_directory']}",
    ]
    if no_samples:
        lines.append(
            "  note:           no resource samples recorded; run was likely "
            "too short. Lower --sample-interval to measure it."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        help="Path to a duct *info.json, an output prefix, or a directory.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the compact summary as JSON instead of text.",
    )
    args = parser.parse_args(argv)

    try:
        info_path = resolve_info_path(Path(args.target))
        info = json.loads(info_path.read_text())
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    summary = build_summary(info)
    if args.json:
        print(json.dumps({"info_path": str(info_path), **summary}, indent=2))
    else:
        print(render(summary, info_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
