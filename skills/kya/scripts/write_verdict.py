#!/usr/bin/env python3
"""Write a labnb-consumable governance verdict JSON.

Deterministic and tool-agnostic: it validates the fields and writes the
canonical schema that ``labnb``'s ``monitor_slice.py check --governance-file``
reads. Drive it from a veldt-kya result (``score_agent`` / ``check_consensus`` /
``detect_drift``) or any other governance source.

Schema (all keys optional, but at least one of decision/trust_score/drift
should be set to be useful):

    {"decision": "allow|warn|block", "trust_score": 0.0, "drift": false,
     "reasons": ["..."]}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DECISIONS = ("allow", "warn", "block")


def build_verdict(args: argparse.Namespace) -> dict[str, object]:
    verdict: dict[str, object] = {}
    if args.decision is not None:
        verdict["decision"] = args.decision
    if args.trust_score is not None:
        verdict["trust_score"] = args.trust_score
    if args.drift is not None:
        verdict["drift"] = args.drift
    if args.reason:
        verdict["reasons"] = list(args.reason)
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        required=True,
        help="Path to write the verdict JSON (parent dirs are created).",
    )
    parser.add_argument("--decision", choices=DECISIONS, default=None)
    parser.add_argument(
        "--trust-score",
        type=float,
        default=None,
        help="Trust score in the inclusive range 0.0–1.0.",
    )
    drift = parser.add_mutually_exclusive_group()
    drift.add_argument("--drift", dest="drift", action="store_true", default=None)
    drift.add_argument("--no-drift", dest="drift", action="store_false")
    parser.add_argument(
        "--reason",
        action="append",
        default=[],
        help="Human-readable reason (repeatable).",
    )
    args = parser.parse_args(argv)

    if args.trust_score is not None and not (0.0 <= args.trust_score <= 1.0):
        print("error: --trust-score must be between 0.0 and 1.0", file=sys.stderr)
        return 2
    if not any(v is not None for v in (args.decision, args.trust_score, args.drift)) and not args.reason:
        print("error: provide at least one of --decision/--trust-score/--drift/--reason", file=sys.stderr)
        return 2

    verdict = build_verdict(args)
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(verdict, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(verdict, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
