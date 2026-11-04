"""One Excel workbook for a whole ABCD run, laid out for people to read.

The per-paper Markdown files put 10+ columns into a pipe table and truncate every
quote at ~90 characters, so they are hard to read and there is one of them per
paper. This workbook replaces them: one file, one sheet per kind of thing, one row
per item, every quote in full, and plain-language values where the JSON uses
internal codes. The JSON stays the machine record; nothing here is new data.

Sheets
  README          what each sheet holds and what the coded values mean
  Papers          one row per paper
  Findings        one row per verified finding (claim type, directionality, quote)
  Variables       one row per variable use, with its ABCD dictionary mapping
  Models          one row per analysis
  Constructs      one row per construct mention
  Rejected        claims that failed verification, and why
  Claims          cross-paper claims (synthesis), one row per claim
  Claim evidence  the paper-by-paper evidence behind each claim
  Construct summary  consensus / divergence per construct

Claim types follow Blake's Claim Framework (explicit, implicit, comparison,
correlation, observation), extended with associative, descriptive and causal:
  C. Blake, "Beyond genes, proteins, and abstracts: Identifying scientific claims
  from full-text biomedical articles", Journal of Biomedical Informatics
  43(2):173-189, 2010. doi:10.1016/j.jbi.2009.11.001
"""
from __future__ import annotations

if __package__ in (None, ""):  # executed as a file, not as part of the package
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

# How a variable mention was tied to the ABCD data dictionary, in words.
MATCH_STATUS = {
    "verified": "Exact match in the data dictionary",
    "context_variable": "Matched to one variable from the paper's wording",
    "context_family": "Several variables in one table fit (e.g. one per brain region)",
    "context_domain": "Only the domain is clear; several tables fit",
    "instrument_table": "Paper named the instrument, not a variable",
    "ambiguous": "No clear match",
    "unmatched": "Not matched to the data dictionary",
    "not_a_variable_name": "Not matched to the data dictionary",
    "unverified": "Not found in the data dictionary",
    "no_dictionary_loaded": "Not checked (no dictionary loaded)",
}

DIRECTION_WORDS = {
    "positive": "positive", "negative": "negative", "null": "no effect",
    "mixed": "mixed", "unspecified": "not stated",
}

# Excel cell limit is 32,767 characters; a quote never gets near it, but a
# used_context could, and an over-long cell makes the file unopenable.
_CELL_MAX = 32000
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple, set)):
        value = "; ".join(str(_cell(v)) for v in value if v not in (None, ""))
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    s = _ILLEGAL.sub(" ", str(value)).strip()
    return s[:_CELL_MAX]


def _base_label(doc: dict) -> str:
    """A short handle a reader recognises: 'Aaron 2025' from the file name, else the DOI."""
    meta = doc.get("source_metadata") or {}
    stem = Path(str(meta.get("source_path") or "")).stem
    m = re.match(r"([A-Za-z][A-Za-z'\-]*?)-(\d{4})(?:-|$)", stem)
    if m:
        return f"{m.group(1).replace('-', ' ')} {m.group(2)}"
    return str(doc.get("paper_id") or meta.get("doi") or stem or "?")


def paper_labels(papers: Sequence[dict]) -> List[str]:
    """One label per paper; same author and year get a/b/c, as in a reference list."""
    base = [_base_label(d) for d in papers]
    seen: Dict[str, int] = {}
    out = []
    for b in base:
        if base.count(b) > 1 and re.search(r"\d{4}$", b):
            seen[b] = seen.get(b, 0) + 1
            out.append(b + "abcdefghijklmnopqrstuvwxyz"[(seen[b] - 1) % 26])
        else:
            out.append(b)
    return out


def _ev(item: dict) -> dict:
    return item.get("evidence") or {}


def _abcd_variable(v: dict) -> str:
    return ((v.get("dictionary_match") or {}).get("variable")
            or (v.get("context_mapping") or {}).get("variable") or "")


def _match_status(v: dict) -> str:
    status = v.get("dictionary_status")
    if status != "verified":
        status = (v.get("context_mapping") or {}).get("status") or status
    return MATCH_STATUS.get(str(status or ""), str(status or ""))


def _releases(v: dict) -> List[str]:
    return (v.get("dd_releases_containing")
            or (v.get("context_mapping") or {}).get("dd_releases") or [])


# --------------------------------------------------------------------------- #
# rows
# --------------------------------------------------------------------------- #

PAPER_COLS = ["Paper", "Title", "DOI", "Study", "Data release", "Sample size",
              "Design", "Variables", "Findings", "Rejected claims"]
FINDING_COLS = ["Paper", "Finding", "Claim type", "Directionality", "Direction (sign)",
                "Role", "Construct", "Variables", "Effect size", "Statistic", "n",
                "Subgroup", "Section", "Page", "Quote from paper"]
VARIABLE_COLS = ["Paper", "Mentioned as", "Description", "Role", "Timepoint",
                 "Instrument", "Respondent", "ABCD variable", "ABCD table", "Domain",
                 "How it was matched", "Releases containing it", "Section", "Page",
                 "Quote from paper"]
MODEL_COLS = ["Paper", "Analysis", "Kind", "Hypothesis", "Predictors", "Outcomes",
              "Mediators", "Moderators", "Covariates", "Software", "Section",
              "Quote from paper"]
CONSTRUCT_COLS = ["Paper", "Construct", "Cognitive Atlas id", "Cognitive Atlas label",
                  "Word used in paper", "Section", "Quote from paper"]
REJECTED_COLS = ["Paper", "Item type", "Reason", "What was proposed", "Quote proposed"]
CLAIM_COLS = ["Claim", "Statement", "Verdict", "Papers", "Agreement", "Construct",
              "Measured by", "Caveats"]
CLAIM_EVIDENCE_COLS = ["Claim", "Supports or contradicts", "Paper", "Data release",
                       "Direction (sign)", "Result", "Effect size", "Statistic", "n",
                       "Strength", "Why that strength", "Section", "Quote from paper"]
CONSTRUCT_SUMMARY_COLS = ["Construct", "Verdict", "Papers", "Majority direction",
                          "Agreement", "Directions reported", "Measured by"]


def paper_row(doc: dict, p: str) -> List[Any]:
    meta = doc.get("source_metadata") or {}
    return [p, meta.get("paper_title"), meta.get("doi") or doc.get("paper_id"),
            meta.get("study"), meta.get("data_release"), meta.get("sample_size"),
            meta.get("design"), len(doc.get("variables") or []),
            len(doc.get("findings") or []), len(doc.get("rejected") or [])]


def finding_rows(doc: dict, p: str) -> Iterable[List[Any]]:
    for f in doc.get("findings") or []:
        ev = _ev(f)
        yield [p, f.get("statement"), f.get("claim_type"), f.get("directionality"),
               DIRECTION_WORDS.get(f.get("direction"), f.get("direction")),
               f.get("role"), f.get("construct_label") or f.get("construct"),
               f.get("variables"), f.get("effect_size") or f.get("estimate"),
               f.get("statistic"), f.get("analytic_n"), f.get("subgroup"),
               ev.get("section"), ev.get("page"), ev.get("quote")]


def variable_rows(doc: dict, p: str) -> Iterable[List[Any]]:
    for v in doc.get("variables") or []:
        ev = _ev(v)
        yield [p, v.get("mention_as_written") or v.get("name"), v.get("label"),
               v.get("role_summary") or v.get("role"),
               v.get("timepoint_normalized") or v.get("timepoint"),
               v.get("instrument"), v.get("respondent"), _abcd_variable(v),
               v.get("nda_or_nbdc_table"), v.get("nbdc_domain"), _match_status(v),
               _releases(v), ev.get("section"), ev.get("page"), ev.get("quote")]


def model_rows(doc: dict, p: str) -> Iterable[List[Any]]:
    for m in doc.get("models") or []:
        ev = _ev(m)
        yield [p, m.get("specification"), m.get("kind"), m.get("tests_hypothesis"),
               m.get("predictors"), m.get("outcomes"), m.get("mediators"),
               m.get("moderators"), m.get("covariates"), m.get("software"),
               ev.get("section"), ev.get("quote")]


def construct_rows(doc: dict, p: str) -> Iterable[List[Any]]:
    for c in doc.get("constructs") or []:
        ev = _ev(c)
        in_quote = ev.get("label_in_quote")
        yield [p, c.get("construct"), c.get("construct_id"), c.get("construct_label"),
               "verbatim" if in_quote else ("paraphrased" if in_quote is False else ""),
               ev.get("section"), ev.get("quote")]


def rejected_rows(doc: dict, p: str) -> Iterable[List[Any]]:
    for r in doc.get("rejected") or []:
        claim = r.get("claim") or {}
        what = claim.get("statement") or claim.get("name") or claim.get("variable") \
            or claim.get("construct") or claim
        yield [p, r.get("section"), str(r.get("reason") or "").replace("_", " "),
               what, r.get("claimed_quote")]


def _labels_by_id(papers: Sequence[dict], labels: Sequence[str]) -> Dict[str, str]:
    out = {}
    for d, label in zip(papers, labels):
        meta = d.get("source_metadata") or {}
        for key in (d.get("paper_id"), meta.get("doi")):
            if key:
                out[str(key)] = label
    return out


def claim_rows(syn: dict, labels: Dict[str, str]) -> Iterable[List[Any]]:
    for c in syn.get("claims") or []:
        measured = c.get("measured_by") or c.get("variables_in_findings") or []
        yield [c.get("claim_id"), c.get("claim"), c.get("verdict"),
               [labels.get(str(p), p) for p in c.get("papers") or []],
               c.get("agreement"), c.get("construct_label"),
               [m.get("variable") if isinstance(m, dict) else m for m in measured],
               c.get("caveats")]


def claim_evidence_rows(syn: dict, labels: Dict[str, str]) -> Iterable[List[Any]]:
    for c in syn.get("claims") or []:
        for side, items in (("supports", c.get("evidence")),
                            ("contradicts", c.get("contradictions"))):
            for e in items or []:
                st = e.get("strength") or {}
                yield [c.get("claim_id"), side,
                       labels.get(str(e.get("paper_id")), e.get("paper_id")),
                       e.get("data_release"),
                       DIRECTION_WORDS.get(e.get("direction"), e.get("direction")),
                       e.get("statement"), e.get("effect_size"), e.get("statistic"),
                       e.get("analytic_n"), st.get("level"), st.get("reasons"),
                       e.get("section"), e.get("quote")]


def construct_summary_rows(syn: dict) -> Iterable[List[Any]]:
    for c in syn.get("constructs") or []:
        dirs = c.get("directions") or {}
        yield [c.get("construct_label") or c.get("construct_id_or_key"), c.get("verdict"),
               c.get("paper_count"), c.get("majority_direction"), c.get("agreement"),
               ", ".join(f"{DIRECTION_WORDS.get(k, k)}: {n}" for k, n in dirs.items()),
               [m.get("variable") if isinstance(m, dict) else m
                for m in c.get("measured_by") or []]]


# --------------------------------------------------------------------------- #
# workbook
# --------------------------------------------------------------------------- #

README_ROWS = [
    ("Sheet", "What it holds"),
    ("Papers", "One row per paper in the run."),
    ("Findings", "One row per result the paper reports about its own data. Every quote was found verbatim in the paper."),
    ("Variables", "One row per variable the study used, with the ABCD data-dictionary variable it maps to."),
    ("Models", "One row per analysis (model) the paper ran."),
    ("Constructs", "One row per psychological / biological construct mentioned, with its Cognitive Atlas id when one was found."),
    ("Rejected", "Items the extractor proposed that failed verification (quote not in the paper, or the result belongs to cited work)."),
    ("Claims", "Cross-paper claims: what the set of papers supports for each construct."),
    ("Claim evidence", "The paper-by-paper results behind each claim, including the ones that contradict it."),
    ("Construct summary", "Per construct: whether the papers agree on the direction of the effect."),
    ("", ""),
    ("Claim type", "Meaning"),
    ("explicit", "States directly that one thing changes another, with an effect verb (increases, prevents, reduces)."),
    ("implicit", "Reports a change after an exposure or condition; the relationship is implied, not stated."),
    ("comparison", "Compares a measure between named groups or conditions."),
    ("correlation", "Reports a correlation between two measures (r, rho)."),
    ("observation", "Reports a result in one group or condition without naming the comparator."),
    ("associative", "Links two variables with association wording (associated with, linked to, predicted)."),
    ("descriptive", "Describes the sample or a distribution without relating two variables."),
    ("causal", "Uses causal wording (causes, leads to, results in, due to, mediates)."),
    ("", "explicit, implicit, comparison, correlation and observation: C. Blake, 'Beyond genes, proteins, and abstracts: Identifying scientific claims from full-text biomedical articles', J Biomed Inform 43(2):173-189, 2010, doi:10.1016/j.jbi.2009.11.001. associative, descriptive and causal extend that framework."),
    ("", ""),
    ("Directionality", "Meaning"),
    ("increase", "The claim says the outcome goes up."),
    ("decrease", "The claim says the outcome goes down."),
    ("mixed", "Parts of the claim go in opposite directions."),
    ("support", "The claim affirms a relationship without a sign."),
    ("(blank)", "A reported null / absent effect, or not given."),
    ("", ""),
    ("Direction (sign)", "The sign of the effect used to compare papers: positive, negative, no effect, mixed, not stated."),
]

_WIDE = {"Finding", "Quote from paper", "Description", "Statement", "Result",
         "Title", "Analysis", "Quote proposed", "What was proposed", "Caveats",
         "Why that strength", "What it holds", "Meaning"}
_MEDIUM = {"Section", "Variables", "Predictors", "Outcomes", "Mediators", "Moderators",
           "Covariates", "Measured by", "How it was matched", "Construct",
           "Mentioned as", "Timepoint", "Instrument", "Papers", "Directions reported"}


def _width(header: str) -> int:
    if header in _WIDE:
        return 60
    if header in _MEDIUM:
        return 28
    return max(12, min(20, len(header) + 2))


def _add_sheet(wb, title: str, headers: Sequence[str], rows: Iterable[Sequence[Any]]):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(title)
    ws.append(list(headers))
    for r in rows:
        ws.append([_cell(v) for v in r])
    bold = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="305496")
    for c in ws[1]:
        c.font, c.fill = bold, fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = wrap
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = _width(h)
    ws.freeze_panes = "B2" if len(headers) > 1 else "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    return ws


def build_workbook(papers: Sequence[dict], synthesis: Optional[dict] = None):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
    except ImportError as exc:  # listed in requirements.txt
        raise RuntimeError("the xlsx format needs openpyxl (pip install openpyxl)") from exc

    wb = Workbook()
    readme = wb.active
    readme.title = "README"
    for r in README_ROWS:
        readme.append(list(r))
    for row in readme.iter_rows():
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        if row[0].value in ("Sheet", "Claim type", "Directionality"):
            for c in row:
                c.font = Font(bold=True)
    readme.column_dimensions["A"].width = 22
    readme.column_dimensions["B"].width = 110

    names = paper_labels(papers)

    def rows(fn):
        return (r for d, p in zip(papers, names) for r in fn(d, p))

    _add_sheet(wb, "Papers", PAPER_COLS,
               (paper_row(d, p) for d, p in zip(papers, names)))
    _add_sheet(wb, "Findings", FINDING_COLS, rows(finding_rows))
    _add_sheet(wb, "Variables", VARIABLE_COLS, rows(variable_rows))
    _add_sheet(wb, "Models", MODEL_COLS, rows(model_rows))
    _add_sheet(wb, "Constructs", CONSTRUCT_COLS, rows(construct_rows))
    _add_sheet(wb, "Rejected", REJECTED_COLS, rows(rejected_rows))
    if synthesis:
        labels = _labels_by_id(papers, names)
        _add_sheet(wb, "Claims", CLAIM_COLS, claim_rows(synthesis, labels))
        _add_sheet(wb, "Claim evidence", CLAIM_EVIDENCE_COLS,
                   claim_evidence_rows(synthesis, labels))
        _add_sheet(wb, "Construct summary", CONSTRUCT_SUMMARY_COLS,
                   construct_summary_rows(synthesis))
    return wb


def write_workbook(papers: Sequence[dict], path: Path,
                   synthesis: Optional[dict] = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(papers, synthesis).save(path)
    return path


# --------------------------------------------------------------------------- #
# CLI: build the workbook from an existing output directory
# --------------------------------------------------------------------------- #

def load_dir(out_dir: Path) -> tuple[List[dict], Optional[dict]]:
    papers = [json.loads(p.read_text()) for p in sorted(Path(out_dir).glob("*_abcd.json"))]
    syn_path = Path(out_dir) / "abcd_synthesis.json"
    syn = json.loads(syn_path.read_text()) if syn_path.exists() else None
    return papers, syn


def _cli(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build abcd_results.xlsx from an ABCD output directory "
                    "(the *_abcd.json files and abcd_synthesis.json).")
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("-o", "--output", type=Path,
                    help="workbook path (default: <out_dir>/abcd_results.xlsx)")
    a = ap.parse_args(argv)
    papers, syn = load_dir(a.out_dir)
    if not papers:
        print(f"no *_abcd.json files in {a.out_dir}", file=sys.stderr)
        return 1
    out = write_workbook(papers, a.output or a.out_dir / "abcd_results.xlsx", syn)
    print(f"{len(papers)} papers{' + synthesis' if syn else ''} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
