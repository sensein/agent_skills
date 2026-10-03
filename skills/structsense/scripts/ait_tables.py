"""Column contract for the AIT mapping mode: read, write, derive, join and validate.

`prompts/extractor-cell-type-ait-mapping.md` emits seven CSV tables whose column names
and order are a contract (`schemas/ait-mapping-columns.json`). Everything here is
deterministic — the parts of the output that must not be model judgement:

* `derive`  fills the columns the prompt defines as functions of other columns:
  `match_confidence` from `skos_relation`, and the crosswalk copies
  (`cell_type_name_as_in_paper`, `species_experimental`) from `entities.csv`.
* `review-sheet`  writes `review_sheet.csv` as the join of entities + methods +
  mappings. It introduces no new values, and keeps only evidence sentences whose
  `evidence_verified` is `exact` or `exact_offset_corrected`.
* `validate`  checks headers, types, vocabularies, multi-value formatting, the
  crosswalk, the quarantine rule, the SKOS/match_confidence crosswalk, the
  same-taxonomy `exactMatch` house rule, and that `review_sheet.csv` equals the join.
* `init`  writes header-only files so an extraction starts from the contract.

    python -m scripts.ait_tables init out/
    python -m scripts.ait_tables derive out/
    python -m scripts.ait_tables review-sheet out/
    python -m scripts.ait_tables validate out/        # exit 1 on any error

Multi-valued cells are `|`-separated with no surrounding spaces; a literal `|` inside a
value (a Markdown table row quoted as evidence, say) is written `\\|`. Evidence columns
are *aligned*: the n-th element of `evidence_sentence`, `evidence_verified`,
`evidence_char_start`, … all describe the same sentence.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from scripts import ait_taxonomy

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "ait-mapping-columns.json"

TABLES = ("paper.csv", "sources.csv", "entities.csv", "methods.csv", "mappings.csv",
          "review_sheet.csv", "entity_cards.csv")

NA = "NA"
VERIFIED = ("exact", "exact_offset_corrected")
PROVENANCE_LABELS = ("code:pdf_parse", "code:regex", "code:lexical_verify",
                     "code:ontology_lookup", "code:supplement_table", "llm:extract",
                     "llm:review", "human:curator")
PROVENANCE_MIN_ENTITY = ("label_verbatim", "marker_genes", "brain_region", "species")

SKOS_TO_CONFIDENCE = {
    "skos:exactMatch": "exact",
    "skos:closeMatch": "partial",
    "skos:broadMatch": "partial",
    "skos:narrowMatch": "partial",
    "skos:relatedMatch": "partial",
    "none": "none",
}
# exactMatch is reserved for same-taxonomy identity: the paper's label must itself come
# from the target taxonomy (the paper says so, or a deposited mapping table does).
EXACT_MATCH_BASIS = {"author_statement", "supplementary_mapping"}

_MULTI_SPLIT = re.compile(r"(?<!\\)\|")
_MENTION_ID = re.compile(r"^(?P<paper>.+):M\d{3,}$")
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


# --------------------------------------------------------------------------- #
# schema and I/O
# --------------------------------------------------------------------------- #

def load_schema(path: Optional[Path] = None) -> dict:
    with open(path or SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def columns(table: str, schema: Optional[dict] = None) -> List[str]:
    schema = schema or load_schema()
    return [c["name"] for c in schema["tables"][table]["columns"]]


def split_multi(value: str) -> List[str]:
    """`a|b\\|c` -> ['a', 'b|c']. Empty string -> []."""
    if value is None or value == "":
        return []
    return [part.replace("\\|", "|") for part in _MULTI_SPLIT.split(value)]


def join_multi(values: Iterable[str]) -> str:
    return "|".join(str(v).replace("|", "\\|") for v in values)


def read_table(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    if not rows:
        return [], []
    header = rows[0]
    return header, [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]


def write_table(path: Path, table: str, rows: Sequence[Dict[str, str]],
                schema: Optional[dict] = None) -> None:
    cols = columns(table, schema)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerow(cols)
        for row in rows:
            writer.writerow([row.get(c, "") if row.get(c) is not None else "" for c in cols])


def load_dir(out_dir: Path) -> Dict[str, List[Dict[str, str]]]:
    """table name -> rows, for every table present in `out_dir`."""
    data = {}
    for table in TABLES:
        p = Path(out_dir) / table
        if p.exists():
            data[table] = read_table(p)[1]
    return data


def parse_provenance(value: str) -> Dict[str, List[str]]:
    """`label_verbatim=llm:extract;code:lexical_verify|species=code:regex` -> dict."""
    out: Dict[str, List[str]] = {}
    for pair in split_multi(value):
        field, _, labels = pair.partition("=")
        out[field] = [x for x in labels.split(";") if x]
    return out


def format_provenance(prov: Dict[str, List[str]]) -> str:
    return join_multi(f"{field}={';'.join(labels)}" for field, labels in prov.items())


def add_provenance(value: str, field: str, label: str) -> str:
    prov = parse_provenance(value)
    labels = prov.setdefault(field, [])
    if label not in labels:
        labels.append(label)
    return format_provenance(prov)


# --------------------------------------------------------------------------- #
# derivations and joins
# --------------------------------------------------------------------------- #

def current_mappings(mappings: Sequence[Dict[str, str]]) -> List[Dict[str, str]]:
    """Mapping rows not superseded by a later row. Revisions append; nothing is overwritten."""
    superseded = {m.get("supersedes_mapping_id") for m in mappings if m.get("supersedes_mapping_id")}
    return [m for m in mappings if m.get("mapping_id") not in superseded]


def is_quarantined(entity: Dict[str, str]) -> bool:
    """Only evidence is `not_found` (or the row is already flagged): never an edge."""
    if entity.get("extraction_flag") == "unverified_evidence":
        return True
    statuses = split_multi(entity.get("evidence_verified", ""))
    return bool(statuses) and all(s == "not_found" for s in statuses)


def experimental_species(entity: Dict[str, str]) -> str:
    return entity.get("species", "") if entity.get("species_role") == "experimental" else ""


def derive(data: Dict[str, List[Dict[str, str]]]) -> Dict[str, int]:
    """Fill derived columns in place. Returns per-column change counts."""
    changed: Dict[str, int] = {}

    def put(row, col, value):
        if row.get(col, "") != value:
            row[col] = value
            changed[col] = changed.get(col, 0) + 1

    entities = {e["mention_id"]: e for e in data.get("entities.csv", [])}
    for table in ("mappings.csv", "entity_cards.csv"):
        for row in data.get(table, []):
            skos = row.get("skos_relation", "")
            if skos in SKOS_TO_CONFIDENCE:
                put(row, "match_confidence", SKOS_TO_CONFIDENCE[skos])
            ent = entities.get(row.get("mention_id", ""))
            if ent is not None:
                put(row, "cell_type_name_as_in_paper", ent.get("label_verbatim", ""))
                if table == "mappings.csv":
                    put(row, "species_experimental", experimental_species(ent))
    return changed


def build_review_sheet(data: Dict[str, List[Dict[str, str]]]) -> List[Dict[str, str]]:
    """The deterministic join. One row per current mapping row of a cell-type entity
    that has at least one strictly verified evidence sentence."""
    entities = {e["mention_id"]: e for e in data.get("entities.csv", [])}
    assays = {m["assay_id"]: m for m in data.get("methods.csv", [])}
    out = []
    for m in current_mappings(data.get("mappings.csv", [])):
        ent = entities.get(m.get("mention_id", ""))
        if ent is None or ent.get("entity_type") != "cell_type":
            continue
        sentences = split_multi(ent.get("evidence_sentence", ""))
        statuses = split_multi(ent.get("evidence_verified", ""))
        sections = split_multi(ent.get("evidence_section", ""))
        keep = [i for i, s in enumerate(statuses) if s in VERIFIED and i < len(sentences)]
        if not keep:
            continue
        assay_names = [assays[a]["assay_name"] for a in split_multi(ent.get("assay_id", ""))
                       if a in assays]
        out.append({
            "cell_type_name": ent.get("label_verbatim", ""),
            "ait_match": "" if m.get("match_confidence") == "none" else m.get("ait_cell_type_label", ""),
            "ait_taxonomy_used": m.get("ait_taxonomy_used", ""),
            "evidence_sentence": join_multi(sentences[i] for i in keep),
            "asserted_or_inferred": ent.get("assertion_type", ""),
            "source_in_paper": join_multi(sections[i] for i in keep if i < len(sections)),
            "brain_region": ent.get("brain_region", ""),
            "marker_genes": ent.get("marker_genes", ""),
            "assay": join_multi(assay_names),
            "species": experimental_species(ent),
            "hierarchy_level": ent.get("hierarchy_level", ""),
            "notes": m.get("notes", ""),
            "paper_id": ent.get("paper_id", ""),
        })
    return out


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

@dataclass
class Issue:
    level: str          # "error" | "warning"
    table: str
    row: Optional[int]  # 1-based data row, None for file-level
    column: Optional[str]
    message: str

    def __str__(self) -> str:
        where = self.table + (f" row {self.row}" if self.row else "") + (f" [{self.column}]" if self.column else "")
        return f"{self.level.upper():7} {where}: {self.message}"


def _type_ok(kind: str, value: str) -> bool:
    if kind == "integer":
        return re.fullmatch(r"-?\d+", value) is not None
    if kind == "float":
        try:
            float(value)
            return True
        except ValueError:
            return False
    if kind == "boolean":
        return value in ("true", "false")
    if kind == "date":
        return _DATE.match(value) is not None
    if kind == "datetime":
        return _DATETIME.match(value) is not None
    return True


def _check_cells(table: str, rows, schema, issues: List[Issue]) -> None:
    spec = schema["tables"][table]
    vocab = schema["vocabularies"]
    for n, row in enumerate(rows, 1):
        for col in spec["columns"]:
            name, value = col["name"], row.get(col["name"], "")
            if value == "":
                if col["required"]:
                    issues.append(Issue("error", table, n, name, "required value is empty"))
                continue
            if value == NA:
                if col["required"]:
                    issues.append(Issue("error", table, n, name, "required value is NA"))
                continue
            parts = split_multi(value) if col["multivalued"] else [value]
            for part in parts:
                if part != part.strip():
                    issues.append(Issue("error", table, n, name, f"space around multi-value element {part!r}"))
                if part in ("", NA) and col["multivalued"]:
                    continue
                if not _type_ok(col["type"], part):
                    issues.append(Issue("error", table, n, name, f"{part!r} is not a valid {col['type']}"))
                if col["vocabulary"] and part not in vocab[col["vocabulary"]]:
                    issues.append(Issue("error", table, n, name, f"{part!r} not in vocabulary {col['vocabulary']}"))
        lengths = {c: len(split_multi(row.get(c, ""))) for c in spec["aligned_multivalue"]
                   if row.get(c, "") not in ("", NA)}
        if len(set(lengths.values())) > 1:
            issues.append(Issue("error", table, n, None, f"aligned evidence columns differ in length: {lengths}"))
        if "field_provenance" in row and row["field_provenance"]:
            for field, labels in parse_provenance(row["field_provenance"]).items():
                if not labels:
                    issues.append(Issue("error", table, n, "field_provenance", f"{field!r} has no label"))
                for label in labels:
                    if label not in PROVENANCE_LABELS:
                        issues.append(Issue("error", table, n, "field_provenance", f"unknown provenance label {label!r}"))


def validate(out_dir: Path, schema: Optional[dict] = None, catalog: Optional[Path] = None) -> List[Issue]:
    schema = schema or load_schema()
    out_dir = Path(out_dir)
    issues: List[Issue] = []
    data: Dict[str, List[Dict[str, str]]] = {}

    for table in TABLES:
        p = out_dir / table
        if not p.exists():
            issues.append(Issue("error", table, None, None, "file missing"))
            continue
        header, rows = read_table(p)
        expected = columns(table, schema)
        if header != expected:
            missing = [c for c in expected if c not in header]
            extra = [c for c in header if c not in expected]
            msg = "header does not match the contract"
            if missing or extra:
                msg += f" (missing {missing}, unexpected {extra})"
            else:
                msg += " (columns out of order)"
            issues.append(Issue("error", table, None, None, msg))
            continue
        data[table] = rows
        _check_cells(table, rows, schema, issues)
    if not (out_dir / "run_report.md").exists():
        issues.append(Issue("warning", "run_report.md", None, None, "file missing"))

    paper_ids = {r.get("paper_id") for r in data.get("paper.csv", [])}
    if "paper.csv" in data and len(data["paper.csv"]) != 1:
        issues.append(Issue("error", "paper.csv", None, None, f"expected one row, found {len(data['paper.csv'])}"))
    for table, rows in data.items():
        for n, row in enumerate(rows, 1):
            if paper_ids and row.get("paper_id") not in paper_ids:
                issues.append(Issue("error", table, n, "paper_id", f"{row.get('paper_id')!r} is not the paper in paper.csv"))

    entities = data.get("entities.csv", [])
    by_mention: Dict[str, Dict[str, str]] = {}
    for n, e in enumerate(entities, 1):
        mid = e.get("mention_id", "")
        m = _MENTION_ID.match(mid)
        if not m or m.group("paper") != e.get("paper_id"):
            issues.append(Issue("error", "entities.csv", n, "mention_id", f"{mid!r} is not {{paper_id}}:M{{nnn}}"))
        if mid in by_mention:
            issues.append(Issue("error", "entities.csv", n, "mention_id", f"duplicate mention_id {mid!r}"))
        by_mention[mid] = e
        if is_quarantined(e) and e.get("extraction_flag") != "unverified_evidence":
            issues.append(Issue("error", "entities.csv", n, "extraction_flag",
                                "all evidence is not_found but the row is not flagged unverified_evidence"))
        if e.get("entity_type") == "cell_type" and e.get("field_provenance"):
            prov = parse_provenance(e["field_provenance"])
            missing = [f for f in PROVENANCE_MIN_ENTITY if f not in prov]
            if missing:
                issues.append(Issue("warning", "entities.csv", n, "field_provenance", f"no provenance for {missing}"))

    for n, m in enumerate(data.get("methods.csv", []), 1):
        if m.get("targeted") == "true" and m.get("gene_panel_size", "") == "":
            issues.append(Issue("error", "methods.csv", n, "gene_panel_size", "targeted assay needs gene_panel_size"))

    mappings = data.get("mappings.csv", [])
    mapping_ids = [m.get("mapping_id") for m in mappings]
    for n, m in enumerate(mappings, 1):
        t = "mappings.csv"
        skos, node = m.get("skos_relation", ""), m.get("ait_node_id", "")
        if mapping_ids.count(m.get("mapping_id")) > 1:
            issues.append(Issue("error", t, n, "mapping_id", f"duplicate mapping_id {m.get('mapping_id')!r}"))
        sup = m.get("supersedes_mapping_id", "")
        if sup and sup not in mapping_ids:
            issues.append(Issue("error", t, n, "supersedes_mapping_id", f"{sup!r} is not a mapping_id"))
        if skos in SKOS_TO_CONFIDENCE and m.get("match_confidence") != SKOS_TO_CONFIDENCE[skos]:
            issues.append(Issue("error", t, n, "match_confidence",
                                f"{skos} derives {SKOS_TO_CONFIDENCE[skos]!r}, found {m.get('match_confidence')!r} (run derive)"))
        if skos == "none":
            if node:
                issues.append(Issue("error", t, n, "ait_node_id", "skos_relation none must leave ait_node_id blank"))
            if not m.get("no_match_reason"):
                issues.append(Issue("error", t, n, "no_match_reason", "required when skos_relation is none"))
        elif skos and not node:
            issues.append(Issue("error", t, n, "ait_node_id", f"{skos} edge has no ait_node_id"))
        if node and not m.get("ait_cell_type_label"):
            issues.append(Issue("error", t, n, "ait_cell_type_label", "populate both ait_node_id and ait_cell_type_label"))

        ent = by_mention.get(m.get("mention_id", ""))
        if ent is None:
            issues.append(Issue("error", t, n, "mention_id", f"{m.get('mention_id')!r} not in entities.csv"))
            continue
        if m.get("cell_type_name_as_in_paper") != ent.get("label_verbatim"):
            issues.append(Issue("error", t, n, "cell_type_name_as_in_paper", "differs from entities.label_verbatim (run derive)"))
        if m.get("species_experimental", "") != experimental_species(ent):
            issues.append(Issue("error", t, n, "species_experimental",
                                "must equal entities.species where species_role = experimental, else empty (run derive)"))
        if node and is_quarantined(ent):
            issues.append(Issue("error", t, n, "ait_node_id", "entity is quarantined (unverified evidence) and cannot be an edge"))

        ait_id = m.get("ait_id", "")
        covered = None
        if node:
            entry = ait_taxonomy.find(ait_id, catalog) if ait_id else None
            if entry is None:
                issues.append(Issue("warning", t, n, "ait_id", f"{ait_id!r} not in data/allen_taxonomies.json"))
            elif m.get("species_experimental"):
                covered = ait_taxonomy.species_covered(ait_id, m["species_experimental"], catalog)
        if skos == "skos:exactMatch":
            basis = set(split_multi(m.get("basis_for_match", "")))
            if not basis & EXACT_MATCH_BASIS:
                issues.append(Issue("error", t, n, "skos_relation",
                                    "exactMatch is reserved for same-taxonomy identity: basis_for_match needs "
                                    "author_statement or supplementary_mapping, otherwise use closeMatch"))
            if covered is False:
                issues.append(Issue("error", t, n, "skos_relation",
                                    "exactMatch across a species boundary; use closeMatch"))

    edges = {(m["mention_id"], m.get("ait_node_id")): m for m in current_mappings(mappings) if m.get("ait_node_id")}
    for n, c in enumerate(data.get("entity_cards.csv", []), 1):
        t = "entity_cards.csv"
        m = edges.get((c.get("mention_id"), c.get("ait_node_id")))
        if m is None:
            issues.append(Issue("error", t, n, "ait_node_id", "no current mapping edge for this mention_id + ait_node_id"))
        elif c.get("skos_relation") != m.get("skos_relation"):
            issues.append(Issue("error", t, n, "skos_relation", "differs from mappings.csv"))
        skos = c.get("skos_relation", "")
        if skos in SKOS_TO_CONFIDENCE and c.get("match_confidence") != SKOS_TO_CONFIDENCE[skos]:
            issues.append(Issue("error", t, n, "match_confidence", "not derived from skos_relation"))
        sets = {}
        for key in ("shared", "paper_only", "taxonomy_only"):
            raw = c.get(f"genes_{key}", "")
            sets[key] = None if raw == NA else split_multi(raw)
            count = c.get(f"n_{key}", "")
            if sets[key] is not None and count not in ("", NA) and count.lstrip("-").isdigit() and int(count) != len(sets[key]):
                issues.append(Issue("error", t, n, f"n_{key}", f"{count} != {len(sets[key])} genes listed"))
        if all(s is not None for s in sets.values()) and c.get("jaccard", "") not in ("", NA):
            union = len(sets["shared"]) + len(sets["paper_only"]) + len(sets["taxonomy_only"])
            expected = len(sets["shared"]) / union if union else 0.0
            try:
                if abs(float(c["jaccard"]) - expected) > 1e-3:
                    issues.append(Issue("error", t, n, "jaccard", f"{c['jaccard']} != {expected:.4f} from the gene sets"))
            except ValueError:
                pass
        if c.get("panel_limited") == "true" and c.get("jaccard_panel_restricted", "") == "":
            issues.append(Issue("error", t, n, "jaccard_panel_restricted", "required (or NA) when panel_limited is true"))

    if "review_sheet.csv" in data and {"entities.csv", "mappings.csv"} <= data.keys():
        expected_rows = build_review_sheet(data)
        cols = columns("review_sheet.csv", schema)
        key = lambda r: tuple(r.get(c, "") for c in cols)  # noqa: E731
        have = sorted(key(r) for r in data["review_sheet.csv"])
        want = sorted(key(r) for r in expected_rows)
        if have != want:
            extra = len(set(have) - set(want))
            missing = len(set(want) - set(have))
            issues.append(Issue("error", "review_sheet.csv", None, None,
                                f"is not the deterministic join of entities + methods + mappings "
                                f"({extra} unexpected rows, {missing} missing; run review-sheet)"))
    return issues


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("init", "write header-only files for all seven tables"),
                           ("derive", "fill match_confidence and the crosswalk copies"),
                           ("review-sheet", "write review_sheet.csv from the join"),
                           ("validate", "check every table against the contract")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("out_dir", type=Path)
    args = ap.parse_args(argv)
    out_dir: Path = args.out_dir

    if args.cmd == "init":
        out_dir.mkdir(parents=True, exist_ok=True)
        for table in TABLES:
            if not (out_dir / table).exists():
                write_table(out_dir / table, table, [])
        print(f"wrote headers to {out_dir}")
        return 0

    if args.cmd == "derive":
        data = load_dir(out_dir)
        changed = derive(data)
        for table in ("mappings.csv", "entity_cards.csv"):
            if table in data:
                write_table(out_dir / table, table, data[table])
        print("derived:", json.dumps(changed) if changed else "nothing to change")
        return 0

    if args.cmd == "review-sheet":
        rows = build_review_sheet(load_dir(out_dir))
        write_table(out_dir / "review_sheet.csv", "review_sheet.csv", rows)
        print(f"review_sheet.csv: {len(rows)} rows")
        return 0

    issues = validate(out_dir)
    for issue in issues:
        print(issue)
    errors = sum(1 for i in issues if i.level == "error")
    warnings = len(issues) - errors
    print(f"{errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
