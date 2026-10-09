#!/usr/bin/env python3
"""Check an Extraction Core dataset beyond what JSON Schema can express.

LinkML validation (``linkml-validate``) checks types, enums, patterns and
required slots. This script runs that validation and then the cross-record
rules that make a dataset reproducible:

  - every reference (paper_id, generated_by, extracted_by, target_assertion, ...)
    resolves to an object defined in the same dataset
  - a record's ``extracted_by`` model is one of its run's ``models``
  - a review's ``review_run`` lists the reviewing model among its ``models``
  - a record whose grounding was ``not_found`` is not ``accepted``
  - an ``accepted`` concept mapping was not ``model_proposed_unverified``

Usage:
  python schemas/extraction_core/check_dataset.py DATA.yaml [DATA2.json ...]
  python schemas/extraction_core/check_dataset.py --json DATA.yaml

Exit code is non-zero if any file has a problem. Requires ``linkml`` for the
schema-validation step; the cross-record rules need only PyYAML (or JSON input).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "extraction_core.yaml"
ROOT_CLASS = "ExtractionDataset"


def load_instance(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    import yaml  # PyYAML ships with linkml; only needed for YAML input

    return yaml.safe_load(text)


def schema_problems(instance: dict) -> list[str]:
    """Run LinkML (JSON Schema) validation against the Extraction Core schema."""
    from linkml.validator import validate

    report = validate(instance, str(SCHEMA_PATH), ROOT_CLASS)
    return [f"schema: {r.message}" for r in report.results]


def _reference_slots(sv):
    """Map class name -> {slot name: (range class, multivalued)} for id references."""
    refs: dict[str, dict[str, tuple[str, bool]]] = {}
    for cname in sv.all_classes():
        for slot in sv.class_induced_slots(cname):
            rng = slot.range
            if rng not in sv.all_classes() or slot.inlined or slot.inlined_as_list:
                continue
            if not sv.get_identifier_slot(rng):
                continue
            refs.setdefault(cname, {})[slot.name] = (rng, bool(slot.multivalued))
    descendants = {c: set(sv.class_descendants(c)) for c in sv.all_classes()}
    return refs, descendants


def _walk(obj, cname, nested, out):
    """Yield (class, object) for every nested object we know the class of."""
    if not isinstance(obj, dict):
        return
    out.append((cname, obj))
    for key, value in obj.items():
        rng = nested.get((cname, key))
        if rng is None:
            continue
        for item in value if isinstance(value, list) else [value]:
            _walk(item, rng, nested, out)


def integrity_problems(instance: dict) -> list[str]:
    from linkml_runtime.utils.schemaview import SchemaView

    sv = SchemaView(str(SCHEMA_PATH))
    refs, descendants = _reference_slots(sv)

    # Inlined (nested) slot ranges, so we can reach SourceLocation, ReviewDecision, ...
    nested = {}
    for cname in sv.all_classes():
        for slot in sv.class_induced_slots(cname):
            if slot.range in sv.all_classes() and (slot.inlined or slot.inlined_as_list
                                                   or not sv.get_identifier_slot(slot.range)):
                nested[(cname, slot.name)] = slot.range

    objects: list[tuple[str, dict]] = []
    _walk(instance, ROOT_CLASS, nested, objects)

    index: dict[str, str] = {}  # id -> class
    problems: list[str] = []
    for cname, obj in objects:
        oid = obj.get("id")
        if oid is None or cname == ROOT_CLASS:
            continue
        if oid in index:
            problems.append(f"duplicate id {oid}")
        index[oid] = cname

    for cname, obj in objects:
        where = obj.get("id") or cname
        for slot, (rng, _multi) in refs.get(cname, {}).items():
            value = obj.get(slot)
            if value is None:
                continue
            for target in value if isinstance(value, list) else [value]:
                if target not in index:
                    problems.append(f"{where}: {slot} -> {target} is not defined in this dataset")
                elif index[target] not in descendants[rng]:
                    problems.append(f"{where}: {slot} -> {target} is a {index[target]}, expected {rng}")

    runs = {r["id"]: r for r in instance.get("extraction_runs") or [] if "id" in r}
    for cname, obj in objects:
        where = obj.get("id") or cname
        run = runs.get(obj.get("generated_by"))
        model = obj.get("extracted_by")
        if run and model and model not in (run.get("models") or []):
            problems.append(f"{where}: extracted_by {model} is not among the models of run {run['id']}")
        if obj.get("evidence_verification") == "not_found" and obj.get("record_status") == "accepted":
            problems.append(f"{where}: grounding not_found but record_status is accepted")
        for review in obj.get("reviews") or []:
            rrun = runs.get(review.get("review_run"))
            reviewer = review.get("reviewer")
            if rrun and reviewer and index.get(reviewer) == "ModelVersion" \
                    and reviewer not in (rrun.get("models") or []):
                problems.append(f"{where}: reviewer {reviewer} is not among the models of run {rrun['id']}")
        for mapping in obj.get("concept_mappings") or []:
            if mapping.get("mapping_status") == "accepted" \
                    and mapping.get("alignment_method") == "model_proposed_unverified":
                problems.append(f"{where}: accepted mapping to {mapping.get('mapped_curie')} "
                                "was never tool-verified")
    return problems


def check(path: Path) -> list[str]:
    instance = load_instance(path)
    if not isinstance(instance, dict):
        return ["top level must be a mapping (an ExtractionDataset)"]
    problems = schema_problems(instance)
    problems += integrity_problems(instance)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    args = parser.parse_args(argv)

    report = {str(p): check(p) for p in args.files}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for name, problems in report.items():
            status = "ok" if not problems else f"{len(problems)} problem(s)"
            print(f"{name}: {status}")
            for problem in problems:
                print(f"  - {problem}")
    return 1 if any(report.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
