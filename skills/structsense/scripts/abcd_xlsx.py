"""One Excel workbook for a whole ABCD run, laid out for people to read.

Per-paper Markdown put 10+ columns into pipe tables and cut every quote short, one
file per paper. This workbook is the readable output instead: one file, every
quote in full, plain words where the JSON uses codes. The JSON stays the machine
record; nothing here is new data.

Sheets, in reading order
  README       how to read the workbook; what each coded value means
  Cross-paper  one block per relationship (e.g. family conflict <-> externalizing):
               a heading row with the verdict, then every paper's finding on it,
               so where and how papers agree or disagree is on one screen
  Findings     every verified finding, paper by paper in reading order, with its
               claim type, directionality and the cross-paper block it belongs to
  Papers, Variables, Models, Constructs, Rejected (only when something was)

Findings and Cross-paper hold the same results seen two ways: Findings is what
each paper said; Cross-paper groups those same rows by relationship. Earlier
versions also had Claims, Claim evidence and Construct summary sheets, which
repeated the findings under construct-level verdicts and read as three copies of
one table; they are gone.

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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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

VERDICT_WORDS = {
    "consensus": "Papers agree",
    "divergent": "Papers disagree",
    "mixed": "Mixed results",
    "insufficient_papers": "One paper only",
    "no_directional_finding": "No direction reported",
    "change_over_time": "Change over time (not compared)",
}

ROLE_ORDER = {"predictor": 0, "mediator": 1, "moderator": 2, "outcome": 3,
              "covariate": 4, "confounder": 5, "control": 6, "instrument": 7}

# Fill colours (ARGB-less hex) for the cross-paper heading rows.
FILL = {"divergent": "F8CBAD", "consensus": "C6EFCE", "within": "FFE699",
        "default": "D9E1F2", "header": "305496", "band": "F2F2F2"}

_CELL_MAX = 32000          # Excel's per-cell limit is 32,767 characters
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_WS = re.compile(r"\s+")


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
    return _ILLEGAL.sub(" ", str(value)).strip()[:_CELL_MAX]


# --------------------------------------------------------------------------- #
# paper names
# --------------------------------------------------------------------------- #

def _base_label(doc: dict) -> str:
    """'Aaron 2025' from the file name, else the DOI."""
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


def _ids_to_labels(papers: Sequence[dict], labels: Sequence[str]) -> Dict[str, str]:
    out = {}
    for d, label in zip(papers, labels):
        meta = d.get("source_metadata") or {}
        for key in (d.get("paper_id"), meta.get("doi")):
            if key:
                out[str(key)] = label
    return out


# --------------------------------------------------------------------------- #
# small readers
# --------------------------------------------------------------------------- #

def _ev(item: dict) -> dict:
    return item.get("evidence") or {}


def _where(ev: dict) -> str:
    sec, page = ev.get("section"), ev.get("page")
    return ", ".join(x for x in (str(sec) if sec else "", f"p. {page}" if page else "") if x)


def _sentence(ev: dict) -> str:
    """The whole sentence a short quote sits in, from the stored context.

    Extractors quote fragments ("conflict mediated the path between financial
    strain at"); the context the verifier kept around the quote finishes the
    sentence. Returned only when it adds something.
    """
    quote = _WS.sub(" ", str(ev.get("quote") or "")).strip()
    ctx = _WS.sub(" ", str(ev.get("used_context") or "")).strip()
    if not quote or not ctx:
        return ""
    i = ctx.find(quote)
    if i < 0:
        return ""
    start = max(ctx.rfind(". ", 0, i), ctx.rfind("? ", 0, i))
    start = 0 if start < 0 else start + 2
    end_m = re.search(r"[.?!](?:\s|$)", ctx[i + len(quote) - 1:])
    end = len(ctx) if not end_m else i + len(quote) - 1 + end_m.end()
    sent = ctx[start:end].strip()
    if ev.get("quote_widened_to_sentence"):
        return ""          # the quote already is the whole sentence
    return sent if len(sent) > len(quote) + 10 else ""


def _quote(ev: dict) -> str:
    """The paper's sentence; a trailing ellipsis where layout cut it off."""
    q = str(ev.get("quote") or "")
    return q + " \u2026" if q and ev.get("sentence_cut_by_layout") else q


def _context(ev: dict) -> str:
    """What makes a table row readable: caption, columns, group, footnote, row labels."""
    ctx = ev.get("table_context") or {}
    if not ctx:
        return ""
    parts = [ctx.get("caption"), ctx.get("columns") and f"Columns: {ctx['columns']}",
             ctx.get("group") and f"Row group: {ctx['group']}",
             ctx.get("footnote") and f"Footnote {ctx['footnote']}",
             ctx.get("row_labels") and "Rows: " + "; ".join(ctx["row_labels"])]
    return "\n".join(p for p in parts if p)


def _checks(f: dict) -> List[str]:
    ev = _ev(f)
    out = []
    for c in f.get("cited_table_check") or []:
        out.append("PAPER INCONSISTENT WITH ITS TABLE: " + str(c.get("detail")))
    if f.get("mirror_of") is not None:
        out.append("same pair as another row of this table, printed in the other order: "
                   "counted once in Cross-paper")
    if ev.get("quote_cut_to_one_sentence"):
        msg = ("the extractor's quote joined sentences "
               + ", ".join(str(x) for x in sorted([*(f.get("sentence_ids") or []),
                                                   *(ev.get("other_sentences_in_quote") or [])]))
               + "; cut to the sentence this finding comes from")
        if f.get("variables_dropped_with_other_sentence"):
            msg += (" (dropped " + ", ".join(f["variables_dropped_with_other_sentence"])
                    + ", named only in the other sentence)")
        out.append(msg)
    if ev.get("context_sentence"):
        out.append(f"continues sentence {ev['context_sentence']}, which gives its subgroup")
    if f.get("direction_not_in_sentence"):
        out.append(f"the sentence states no sign; the extractor's "
                   f"'{f.get('direction_as_extracted')}' came from elsewhere (heading or table)")
    gap = f.get("summary_not_in_evidence") or {}
    missing = list(gap.get("variables") or []) + list(gap.get("statistics") or [])
    if missing:
        out.append("summary names what the quote does not: " + ", ".join(missing)
                   + " (rely on the quote)")
    if f.get("combines_several_results"):
        out.append("combines several results: split into one finding per result")
    if ev.get("sentence_cut_by_layout"):
        out.append("sentence interrupted by a figure, table or page break")
    return out


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


def _strength(e: dict) -> str:
    """'MODERATE' then the grading steps, one per line."""
    st = e.get("strength") or {}
    if not st:
        return ""
    return str(st.get("level") or "").upper() + "\n" + "\n".join(st.get("reasons") or [])


# --------------------------------------------------------------------------- #
# sheet contents
# --------------------------------------------------------------------------- #

# The quote is the paper's own words, verified against its text. The summary is
# the extractor's one-sentence paraphrase: useful to scan, never evidence, so it
# is labelled as such and comes after the quote.
QUOTE_COL = "Quote from paper (verbatim)"
SUMMARY_COL = "Summary (paraphrase, not verbatim)"
CROSS_COLS = ["ID", "Claim status", "Relationship", "Paper", "Direction", QUOTE_COL,
              SUMMARY_COL, "Claim type", "Effect size", "Statistic", "n",
              "Evidence quality", "Where in paper"]
FINDING_COLS = ["Paper", "Finding ID", "Sentence ID", "Section", QUOTE_COL, SUMMARY_COL,
                "Claim type", "Directionality", "Effect sign", "Causal language", "Hedged",
                "Effect size", "Statistic", "n", "Subgroup",
                "Table context (verbatim)", "Full sentence", "Check", "Relationship",
                "Cross-paper ID", "Variables",
                "Construct", "Role"]
# The columns of a sentence-level human annotation, so the two can be laid side by
# side; the last three tie each sentence to the Findings sheet.
SENTENCE_COLS = ["paper_id", "sentence_id", "section", "text", "is_claim", "claim_type",
                 "causal_language", "direction", "hedged", "notes", "Finding IDs"]
PAPER_COLS = ["Paper", "Title", "DOI", "Study", "Data release", "Sample size",
              "Design", "Findings", "Variables", "Models", "Rejected items"]
VARIABLE_COLS = ["Paper", "Variable (as written)", "Role", "Description", "Timepoint",
                 "Instrument", "Respondent", "ABCD variable", "ABCD table", "Domain",
                 "Dictionary match", "Releases containing it", "Where in paper",
                 "Quote from paper"]
MODEL_COLS = ["Paper", "Analysis", "Kind", "Hypothesis", "Predictors", "Mediators",
              "Moderators", "Outcomes", "Covariates", "Software", "Where in paper",
              "Quote from paper"]
CONSTRUCT_COLS = ["Paper", "Construct", "Cognitive Atlas id", "Cognitive Atlas label",
                  "Wording", "Where in paper", "Quote from paper"]
REJECTED_COLS = ["Paper", "Item type", "Reason", "Why", "What was proposed", "Quote proposed"]

# First row of every sheet: what the sheet is for, in one sentence.
SHEET_NOTES = {
    "Cross-paper": ("Compares papers. Each coloured row is one relationship between "
                    "variables with its claim status; the rows under it are each "
                    "paper's finding on it, with effect size, evidence quality and "
                    "quote. A \u2194 B means A is associated with B, with no direction "
                    "implied (see README). Green = agree, red = disagree, yellow = a "
                    "paper contradicts itself."),
    "Findings": ("Every result each paper reported about its own data, in the order "
                 "it appears in the paper. One row = one finding. The quote is the "
                 "paper's own words, checked against its text; the summary next to "
                 "it is the extractor's paraphrase. 'Cross-paper ID' links to the "
                 "Cross-paper sheet."),
    "Sentences": ("The paper read sentence by sentence, in the columns of a human "
                  "sentence-level annotation (sentence_id, section, text, is_claim, "
                  "claim_type, causal_language, direction, hedged, notes). 'Finding IDs' "
                  "points to the Findings rows from that sentence; table rows are in "
                  "Findings only. Yellow = the paper is inconsistent here (with another "
                  "sentence or with its own table). Orange = reads as a result but no "
                  "finding was extracted."),
    "Papers": "One row per paper in this run: title, DOI, ABCD release, sample, design.",
    "Variables": ("The measures each paper used, the role each played in the analysis "
                  "(predictor, mediator, outcome, covariate, ...) and the ABCD "
                  "data-dictionary variable it maps to."),
    "Models": ("The statistical analyses each paper ran: one row per model, with the "
               "variables that went in as predictors, mediators, moderators, "
               "outcomes and covariates."),
    "Constructs": ("The broader concepts the variables measure (e.g. 'externalizing "
                   "symptoms', 'working memory') as each paper names them, with the "
                   "Cognitive Atlas id when one was found. Used to recognise that "
                   "different papers' variables measure the same thing."),
    "Rejected": ("Items the extractor proposed that the verifier removed: the quote "
                 "is not in the paper, the result belongs to cited work, or the "
                 "summary names something its quote does not contain. Kept so you "
                 "can see what was dropped and why; none of it is used anywhere else."),
}


def _finding_index_to_rel(synthesis: Optional[dict]) -> Dict[Tuple[str, int], dict]:
    out: Dict[Tuple[str, int], dict] = {}
    for r in (synthesis or {}).get("relationships") or []:
        for e in r.get("evidence") or []:
            if e.get("finding_index") is not None:
                out[(str(e.get("paper_id")), int(e["finding_index"]))] = r
    return out


def finding_ids(doc: dict) -> Dict[int, str]:
    """F1, F2, ... in reading order (where the quote sits in the paper)."""
    items = list(enumerate(doc.get("findings") or []))
    items.sort(key=lambda t: (_ev(t[1]).get("start") is None, _ev(t[1]).get("start") or 0))
    return {i: f"F{n}" for n, (i, _) in enumerate(items, start=1)}


def finding_rows(doc: dict, p: str, rel_of: Dict[Tuple[str, int], dict]):
    pid = str(doc.get("paper_id"))
    ids = finding_ids(doc)
    items = sorted(enumerate(doc.get("findings") or []), key=lambda t: int(ids[t[0]][1:]))
    for i, f in items:
        ev = _ev(f)
        rel = rel_of.get((pid, i)) or {}
        hedge = f.get("hedged")
        yield [p, ids[i], f.get("sentence_ids"), ev.get("paper_section") or _where(ev),
               _quote(ev), f.get("statement"), f.get("claim_type"),
               f.get("directionality"),
               DIRECTION_WORDS.get(f.get("direction"), f.get("direction")),
               f.get("causal_language"), None if hedge is None else bool(hedge),
               f.get("effect_size") or f.get("estimate"),
               f.get("statistic"), f.get("analytic_n"), f.get("subgroup"),
               _context(ev), _sentence(ev), _checks(f), rel.get("label"),
               rel.get("relationship_id"), f.get("variables"),
               f.get("construct_label") or f.get("construct"), f.get("role")]


def sentence_rows(doc: dict, p: str):
    pid = str(doc.get("paper_id"))
    ids = finding_ids(doc)
    for r in doc.get("sentences") or []:
        yield [pid, r.get("sentence_id"), r.get("section"), r.get("text"), r.get("is_claim"),
               r.get("claim_type"), r.get("causal_language"), r.get("direction"),
               r.get("hedged"), r.get("notes"),
               [ids[i] for i in r.get("findings") or [] if i in ids]]


def paper_row(doc: dict, p: str) -> List[Any]:
    meta = doc.get("source_metadata") or {}
    return [p, meta.get("paper_title"), meta.get("doi") or doc.get("paper_id"),
            meta.get("study"), meta.get("data_release"), meta.get("sample_size"),
            meta.get("design"), len(doc.get("findings") or []),
            len(doc.get("variables") or []), len(doc.get("models") or []),
            len(doc.get("rejected") or [])]


def variable_rows(doc: dict, p: str):
    vs = sorted(doc.get("variables") or [],
                key=lambda v: ROLE_ORDER.get(str(v.get("role") or ""), 9))
    for v in vs:
        ev = _ev(v)
        yield [p, v.get("mention_as_written") or v.get("name"),
               v.get("role_summary") or v.get("role"), v.get("label"),
               v.get("timepoint_normalized") or v.get("timepoint"),
               v.get("instrument"), v.get("respondent"), _abcd_variable(v),
               v.get("nda_or_nbdc_table"), v.get("nbdc_domain"), _match_status(v),
               _releases(v), _where(ev), ev.get("quote")]


def model_rows(doc: dict, p: str):
    for m in doc.get("models") or []:
        ev = _ev(m)
        yield [p, m.get("specification"), m.get("kind"), m.get("tests_hypothesis"),
               m.get("predictors"), m.get("mediators"), m.get("moderators"),
               m.get("outcomes"), m.get("covariates"), m.get("software"),
               _where(ev), ev.get("quote")]


def construct_rows(doc: dict, p: str):
    for c in doc.get("constructs") or []:
        ev = _ev(c)
        in_quote = ev.get("label_in_quote")
        yield [p, c.get("construct"), c.get("construct_id"), c.get("construct_label"),
               "verbatim" if in_quote else ("paraphrased" if in_quote is False else ""),
               _where(ev), ev.get("quote")]


def rejected_rows(doc: dict, p: str):
    for r in doc.get("rejected") or []:
        claim = r.get("claim") or {}
        what = (claim.get("statement") or claim.get("name") or claim.get("variable")
                or claim.get("construct") or claim)
        yield [p, r.get("section"), str(r.get("reason") or "").replace("_", " "),
               r.get("detail"), what, r.get("claimed_quote")]


# --------------------------------------------------------------------------- #
# workbook
# --------------------------------------------------------------------------- #

README_ROWS = [
    ("How to read this workbook", ""),
    ("Cross-paper", "Start here. Each coloured row is one relationship between variables (e.g. family conflict ↔ externalizing) with a verdict. The rows under it are every paper's finding on that relationship: direction, effect size, where in the paper and the exact quote. Papers 'disagree' only when they report opposite signs for the same relationship."),
    ("Findings", "Every finding each paper reported about its own data, in the order it appears in the paper (Finding ID F1, F2, ...). 'Sentence ID' links to the Sentences sheet; 'Section' says where in the paper, and for a table row the section that cites the table and the table ('Results - 3.3 ... > Table 3'). 'Check' lists what the verifier found: the sentence disagreeing with the table it cites, a pair printed in both orders, a joined quote cut to one sentence, a sign the sentence does not state. 'Quote from paper (verbatim)' is the paper's own sentence, checked against its text. 'Summary (paraphrase, not verbatim)' is the extractor's one-line restatement, there to scan quickly; rely on the quote. 'Cross-paper ID' points to the block in the Cross-paper sheet that groups it with other papers."),
    ("Sentences", "Every prose sentence of each paper with an id and its section, in the same columns as a sentence-level human annotation, so the two can be compared row by row. is_claim: yes (a finding or a sentence claim quotes it), no, possibly (reads as a result in Results but nothing was extracted: check it), not assessed (abstract or discussion, when the extractor gave no sentence-level claims). Notes give restatements ('restates 97'), inconsistencies ('INCONSISTENT with 134: ...', or with the table the sentence cites) and how many findings came from the sentence. Sentence ids are numbered by this tool over the extracted text, so they can differ from another annotator's numbering."),
    ("Papers", "One row per paper: title, DOI, ABCD release, sample, design."),
    ("Variables", "The measures each paper used, the role each played (predictor, mediator, outcome, covariate, ...) and the ABCD data-dictionary variable it maps to."),
    ("Models", "The statistical analyses each paper ran, one row per model, with which variables were predictors, mediators, moderators, outcomes and covariates. Use it to see how a finding was obtained."),
    ("Constructs", "The broader concepts the variables measure (e.g. 'externalizing symptoms', 'working memory'), with the Cognitive Atlas id when found. This is how variables from different papers are recognised as measuring the same thing."),
    ("Rejected", "Present only when the verifier rejected something (quote not in the paper, or a result that belongs to cited work)."),
    ("", ""),
    ("Relationship notation", "Meaning (the 'Relationship' column)"),
    ("A \u2194 B", "A is associated with B. The arrow has two heads on purpose: it says the two variables are related, NOT which one predicts or causes the other. One block pools every finding about the pair: correlations (no direction), A predicting B, and B predicting A. Read the individual findings under the block for direction."),
    ("A / B / C", "A finding about three or more variables at once, usually a mediation path (e.g. financial strain \u2192 family conflict \u2192 externalizing). The order is alphabetical, not the causal order; the path is in the finding's quote and summary."),
    ("A over time", "Change or stability of one variable across waves (e.g. family conflict decreasing over time, or prior externalizing predicting later externalizing). Not an association between two variables, so it is listed but not graded."),
    ("A and B over time", "Two variables each changing over time (\"both decreased over time\"). Two trends, not an association between them."),
    ("A as moderator", "A finding about a moderator where the paper does not name the other variables in that sentence."),
    ("\u2192 in a summary", "Inside a finding's summary or quote, \u2192 is the paper's own direction (A \u2192 B: A predicts B, as the paper states it). It is never added by the cross-paper grouping."),
    ("", ""),
    ("Verdict", "Meaning"),
    ("Papers agree", "Two or more papers report the same direction for this relationship."),
    ("Papers disagree", "Two or more papers report opposite directions (positive vs negative) for the same relationship."),
    ("Mixed results", "Two or more papers, but no direction reaches 70% of the results (e.g. positive and no-effect)."),
    ("One paper only", "Only one paper reports this relationship, so there is nothing to compare."),
    ("Change over time (not compared)", "A trend or stability of one variable over time. Listed for context; a mean decline and a stability coefficient are different quantities, so they are not judged against each other."),
    ("Yellow row", "The same paper reports both positive and negative for this relationship, usually the abstract contradicting the results."),
    ("", ""),
    ("Claim status", "Meaning (Cross-paper heading rows). Scale after https://www.ncbi.nlm.nih.gov/books/NBK543866/"),
    ("Established", "Community standards are met for adding the claim to the body of evidence: high-quality evidence warrants high confidence, nothing against it, replicated in independent samples (2+ cohorts). All-ABCD corpora cannot reach this: the papers share one cohort."),
    ("Provisionally established", "Moderate-quality evidence warrants a high level of confidence in the claim (nothing against it)."),
    ("Arguably true", "Neither of the above, but moderate-or-better evidence warrants significantly more confidence in the claim than in its negation, or low-quality evidence warrants high confidence in it."),
    ("Speculative", "None of the other categories (e.g. evidence on both sides, or only very low quality)."),
    ("Arguably false", "Mirror of arguably true, for the negation (the opposite sign, or no effect)."),
    ("Provisionally ruled out", "Moderate-quality evidence warrants a high level of confidence in the negation of the claim."),
    ("Ruled out", "High-quality evidence, replicated in independent samples, warrants high confidence in the negation."),
    ("", ""),
    ("Evidence quality", "Meaning (each finding). Levels after Atkins et al. 2004 (GRADE Working Group, BMJ 328:1490)"),
    ("High", "Further research is highly unlikely to have a significant impact on our confidence in the claim."),
    ("Moderate", "Further research is moderately unlikely to have a significant impact on our confidence in the claim."),
    ("Low", "Further research is moderately likely to have a significant impact on our confidence in the claim."),
    ("Very low", "Further research is highly likely to have a significant impact on our confidence in the claim."),
    ("How a finding is graded", "Observational studies start at Low (randomised at High), as in GRADE. +1 longitudinal and adjusted for covariates; +1 large standardized effect (|r| or |beta| >= .30, d >= .80, ratio >= 2); -1 no effect size or statistic; -1 n < 1,000; -1 subgroup only. The steps are listed in each row's 'Evidence quality' cell."),
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
    ("Source", "explicit, implicit, comparison, correlation, observation: C. Blake, 'Beyond genes, proteins, and abstracts: Identifying scientific claims from full-text biomedical articles', J Biomed Inform 43(2):173-189, 2010, doi:10.1016/j.jbi.2009.11.001. associative, descriptive and causal extend that framework."),
    ("", ""),
    ("Directionality", "Meaning"),
    ("increase", "The claim says the outcome goes up."),
    ("decrease", "The claim says the outcome goes down."),
    ("mixed", "Parts of the claim go in opposite directions."),
    ("support", "The claim affirms a relationship without a sign."),
    ("(blank)", "A reported null / absent effect, or not given."),
    ("Effect sign", "The sign used to compare papers: positive, negative, no effect, mixed, not stated."),
]

_WIDE = {"Finding", "Quote from paper", QUOTE_COL, SUMMARY_COL, "Full sentence",
         "Table context (verbatim)", "Check", "Description", "Title",
         "Analysis", "Quote proposed", "What was proposed"}
_MEDIUM = {"Relationship", "Where in paper", "Variables", "Predictors", "Outcomes",
           "Mediators", "Moderators", "Covariates", "Dictionary match", "Construct",
           "Variable (as written)", "Timepoint", "Instrument", "Evidence quality",
           "Verdict", "Claim status", "Design"}


def _width(header: str) -> int:
    if header in _WIDE:
        return 55
    if header in _MEDIUM:
        return 26
    return max(11, min(18, len(header) + 2))


def _styles():
    from openpyxl.styles import Alignment, Font, PatternFill
    return Alignment, Font, PatternFill


def _drop_empty_columns(headers: Sequence[str], rows: List[List[Any]],
                        keep: Sequence[str] = ("Paper",)) -> Tuple[List[str], List[List[Any]]]:
    cols = [i for i, h in enumerate(headers)
            if h in keep or any(r[i] not in ("", None) for r in rows)]
    return [headers[i] for i in cols], [[r[i] for i in cols] for r in rows]


def _table_sheet(wb, title: str, headers: Sequence[str], rows: Iterable[Sequence[Any]],
                 *, band_by: Optional[int] = 0):
    Alignment, Font, PatternFill = _styles()
    from openpyxl.utils import get_column_letter

    data = [[_cell(v) for v in r] for r in rows]
    headers, data = _drop_empty_columns(list(headers), data)
    ws = wb.create_sheet(title)
    _note_row(ws, title, len(headers))
    ws.append(headers)
    for r in data:
        ws.append(r)
    for c in ws[2]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=FILL["header"])
        c.alignment = Alignment(vertical="center", wrap_text=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    band = PatternFill("solid", fgColor=FILL["band"])
    last, shade = None, False
    for row in ws.iter_rows(min_row=3):
        if band_by is not None:
            key = row[band_by].value
            if key != last:
                shade, last = not shade, key
        for c in row:
            c.alignment = wrap
            if band_by is not None and shade:
                c.fill = band
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = _width(h)
    ws.freeze_panes = "C3" if len(headers) > 2 else "A3"
    if ws.max_row > 2:
        ws.auto_filter.ref = f"A2:{get_column_letter(len(headers))}{ws.max_row}"
    return ws


def _note_row(ws, title: str, width: int) -> None:
    """Row 1: one sentence on what the sheet is for, across the table's width."""
    Alignment, Font, _ = _styles()
    ws.append([SHEET_NOTES.get(title, "")])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(width, 1))
    c = ws.cell(row=1, column=1)
    c.font = Font(italic=True, color="404040")
    c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[1].height = 45


def _cross_paper_sheet(wb, synthesis: dict, labels: Dict[str, str]):
    """Heading row per relationship, its evidence underneath, one paper per row."""
    Alignment, Font, PatternFill = _styles()
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet("Cross-paper", 1)
    _note_row(ws, "Cross-paper", len(CROSS_COLS))
    ws.append(CROSS_COLS)
    for c in ws[2]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=FILL["header"])
        c.alignment = Alignment(vertical="center", wrap_text=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    grey = Font(color="808080")

    for r in synthesis.get("relationships") or []:
        papers = [labels.get(str(p), p) for p in r.get("papers") or []]
        cs = r.get("claim_status") or {}
        verdict = ((str(cs.get("status")).capitalize() + "\n(" if cs else "Not graded\n(")
                   + VERDICT_WORDS.get(r.get("verdict"), str(r.get("verdict"))) + ")")
        within = [labels.get(str(p), p) for p in r.get("within_paper_conflict") or []]
        if within:
            verdict += " · conflict within " + ", ".join(within)
        claim = str(r.get("claim") or "")
        if cs:
            claim += (f"  Claim: {cs.get('claim')}. Status: {cs.get('status')}, "
                      f"because {cs.get('why')}.")
        caveats = [c for c in r.get("caveats") or []
                   if not c.startswith("change over time")]
        if caveats:
            claim += "  Caveats: " + "; ".join(caveats) + "."
        ws.append([_cell(v) for v in (r.get("relationship_id"), verdict, r.get("label"),
                                      papers, "", claim)] + [""] * (len(CROSS_COLS) - 6))
        head = ws.max_row
        colour = (FILL["within"] if within else
                  FILL.get(r.get("verdict"), FILL["default"]))
        for c in ws[head]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor=colour)
            c.alignment = wrap
        ws.merge_cells(start_row=head, start_column=6, end_row=head,
                       end_column=len(CROSS_COLS))
        for e in r.get("evidence") or []:
            ws.append([_cell(v) for v in (
                r.get("relationship_id"), "", "", labels.get(str(e.get("paper_id")),
                                                             e.get("paper_id")),
                DIRECTION_WORDS.get(e.get("direction"), e.get("direction")),
                "\n\n".join(x for x in (e.get("quote"), _context(e)) if x),
                "\n".join(_checks({**e, "evidence": e}) and
                          [str(e.get("statement") or ""), "CHECK: " + "; ".join(_checks({**e, "evidence": e}))]
                          or [str(e.get("statement") or "")]),
                e.get("claim_type"),
                e.get("effect_size"), e.get("statistic"), e.get("analytic_n"),
                _strength(e), _where(e))])
            row = ws[ws.max_row]
            for c in row:
                c.alignment = wrap
            row[0].font = grey   # the id stays on evidence rows so filters work
        ws.append([])            # a blank line between blocks
    for i, h in enumerate(CROSS_COLS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = _width(h)
    ws.column_dimensions["A"].width = 6
    ws.freeze_panes = "D3"
    return ws


def build_workbook(papers: Sequence[dict], synthesis: Optional[dict] = None):
    try:
        from openpyxl import Workbook
    except ImportError as exc:  # listed in requirements.txt
        raise RuntimeError("the xlsx format needs openpyxl (pip install openpyxl)") from exc
    Alignment, Font, _ = _styles()

    wb = Workbook()
    readme = wb.active
    readme.title = "README"
    for r in README_ROWS:
        readme.append(list(r))
    for row in readme.iter_rows():
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
        if row[0].value in ("How to read this workbook", "Verdict", "Claim type",
                            "Directionality", "Claim status", "Evidence quality",
                            "Relationship notation"):
            for c in row:
                c.font = Font(bold=True)
    readme.column_dimensions["A"].width = 30
    readme.column_dimensions["B"].width = 110

    names = paper_labels(papers)
    labels = _ids_to_labels(papers, names)
    has_cross = bool(synthesis and synthesis.get("relationships")) and len(papers) > 1
    if has_cross:
        _cross_paper_sheet(wb, synthesis, labels)
    rel_of = _finding_index_to_rel(synthesis if has_cross else None)

    def rows(fn, *extra):
        return (r for d, p in zip(papers, names) for r in fn(d, p, *extra))

    fws = _table_sheet(wb, "Findings", FINDING_COLS, rows(finding_rows, rel_of))
    ordered = [doc["findings"][i] for doc in papers
               for i in sorted(finding_ids(doc), key=lambda k: int(finding_ids(doc)[k][1:]))]
    _, _, PatternFill = _styles()
    yellow = PatternFill("solid", fgColor=FILL["within"])
    for row, f in zip(fws.iter_rows(min_row=3), ordered):
        if f.get("cited_table_check"):
            for c in row:
                c.fill = yellow
    if any(d.get("sentences") for d in papers):
        ws = _table_sheet(wb, "Sentences", SENTENCE_COLS, rows(sentence_rows))
        _mark_sentence_rows(ws, [r for d in papers for r in d.get("sentences") or []])
    _table_sheet(wb, "Papers", PAPER_COLS,
                 (paper_row(d, p) for d, p in zip(papers, names)), band_by=None)
    _table_sheet(wb, "Variables", VARIABLE_COLS, rows(variable_rows))
    _table_sheet(wb, "Models", MODEL_COLS, rows(model_rows))
    _table_sheet(wb, "Constructs", CONSTRUCT_COLS, rows(construct_rows))
    rejected = list(rows(rejected_rows))
    if rejected:
        _table_sheet(wb, "Rejected", REJECTED_COLS, rejected)
    return wb


def _mark_sentence_rows(ws, sentences: Sequence[dict]) -> None:
    """Yellow where the paper is inconsistent, orange where a result may be missing."""
    _, _, PatternFill = _styles()
    yellow = PatternFill("solid", fgColor=FILL["within"])
    orange = PatternFill("solid", fgColor="F4B183")
    for row, s in zip(ws.iter_rows(min_row=3), sentences):
        fill = yellow if s.get("inconsistent") else (
            orange if str(s.get("is_claim") or "").startswith("possibly") else None)
        if fill:
            for c in row:
                c.fill = fill


def write_workbook(papers: Sequence[dict], path: Path,
                   synthesis: Optional[dict] = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(papers, synthesis).save(path)
    return path


# --------------------------------------------------------------------------- #
# CLI: build the workbook from an existing output directory
# --------------------------------------------------------------------------- #

def load_dir(out_dir: Path) -> Tuple[List[dict], Optional[dict]]:
    """Per-paper JSON from `out_dir`, and a synthesis that has relationships.

    A synthesis written before relationships existed is recomputed from the
    per-paper files rather than shown without its cross-paper sheet.
    """
    papers = [json.loads(p.read_text()) for p in sorted(Path(out_dir).glob("*_abcd.json"))]
    syn_path = Path(out_dir) / "abcd_synthesis.json"
    syn = json.loads(syn_path.read_text()) if syn_path.exists() else None
    if len(papers) > 1 and not (syn or {}).get("relationships"):
        from scripts.abcd_synthesize import synthesize
        syn = synthesize(papers)
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
    print(f"{len(papers)} papers{' + cross-paper' if syn else ''} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
