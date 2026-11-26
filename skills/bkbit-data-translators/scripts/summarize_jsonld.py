#!/usr/bin/env python3
"""Sanity-check and summarize JSON-LD files written by the bkbit data translators.

bkbit translators often print an error and keep going instead of exiting
non-zero, so a "successful" run can still produce an empty or partial graph.
This script checks each file for a ``@context`` and a non-empty ``@graph``,
counts graph objects by type, and flags text bkbit writes when something
failed. Standard library only.

Usage:
    python summarize_jsonld.py output.jsonld [more.jsonld ...] [--json]

Exit code is 0 if every file looks valid, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

# Text bkbit emits in place of (or alongside) real output when a step fails.
ERROR_MARKERS = ("Error during conversion:", "Traceback (most recent call last)")


def object_type(obj: dict) -> str:
    """Return the most specific type label available on a graph object."""
    for key in ("@type", "type", "category"):
        value = obj.get(key)
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, str) and value:
            return value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return "<untyped>"


def summarize(path: Path) -> dict:
    """Summarize one JSON-LD file; ``problems`` is empty when it looks valid."""
    report: dict = {"file": str(path), "problems": [], "types": {}, "objects": 0}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report["problems"].append(f"cannot read file: {exc}")
        return report

    for marker in ERROR_MARKERS:
        if marker in text:
            report["problems"].append(f"contains error text: {marker!r}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        report["problems"].append(
            f"not valid JSON ({exc.msg} at line {exc.lineno}); "
            "stdout may have mixed log output into the file"
        )
        return report

    if not isinstance(data, dict):
        report["problems"].append("top level is not a JSON object")
        return report
    context = data.get("@context")
    if not context:
        report["problems"].append("missing @context")
    report["context"] = context if isinstance(context, str) else "<inline>"

    graph = data.get("@graph")
    if not isinstance(graph, list):
        report["problems"].append("missing @graph list")
        return report
    if not graph:
        report["problems"].append(
            "@graph is empty: check the translator's printed errors"
        )

    types = Counter(
        object_type(obj) if isinstance(obj, dict) else "<non-object>"
        for obj in graph
    )
    ids = [obj.get("id") or obj.get("@id") for obj in graph if isinstance(obj, dict)]
    missing_ids = sum(1 for i in ids if not i)
    duplicate_ids = len([i for i in ids if i]) - len({i for i in ids if i})
    if duplicate_ids:
        report["problems"].append(f"{duplicate_ids} duplicate object id(s)")

    report["objects"] = len(graph)
    report["types"] = dict(sorted(types.items()))
    report["objects_without_id"] = missing_ids
    return report


def print_report(report: dict) -> None:
    status = "FAIL" if report["problems"] else "ok"
    print(f"[{status}] {report['file']}: {report['objects']} object(s)")
    if report.get("context"):
        print(f"  @context: {report['context']}")
    for name, count in report["types"].items():
        print(f"  {count:>8}  {name}")
    if report.get("objects_without_id"):
        print(f"  note: {report['objects_without_id']} object(s) have no id")
    for problem in report["problems"]:
        print(f"  problem: {problem}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path, help="JSON-LD files to check")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)

    reports = [summarize(path) for path in args.files]
    if args.json:
        json.dump(reports, sys.stdout, indent=2)
        print()
    else:
        for report in reports:
            print_report(report)
    return 1 if any(r["problems"] for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
