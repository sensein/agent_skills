#!/usr/bin/env python3
"""Audit whether everything a review delivers is grounded in its sources.

``verify_citations.py`` proves the references are real. This proves the
content is: every statement traces to verbatim text of a paper or to an
explicit computation over the review's own data, and nothing else is delivered.
It runs over ``review.json`` and the **delivered** Markdown —
``<stem>.delivered.md`` (what ``generate_review_pdf.py --prepare`` assembles:
styled references and appendices included), else ``<stem>.manuscript.md``,
else ``<stem>.md`` — and checks:

0. **Full text only.** Every included study must have its full text; one read
   on the abstract alone cannot be evaluated and blocks delivery.
1. **Evidence spans.** Each span is verbatim text of its paper (normalisation
   folds case, quotes, whitespace, hyphenation and separators, but keeps signs,
   decimals, comparators and %). It must come from a section actually read (an
   abstract-only paper supplies no results), from the version cited, and every
   included study must have at least one.
2. **Structured values.** Every charting answer, appraisal item rating,
   risk-of-bias judgement and extracted field, narrative cell and GRADE domain
   rating needs verbatim evidence in ``<stem>.evidence-ledger.json``
   (``extract_evidence.py``). Any value still ungrounded is an error; it must
   be grounded or removed (``extract_evidence.py apply``) before delivery.
3. **Claims.** Every sentence, table row and caption of the delivered text is
   a claim in ``<stem>.claims.json`` (``--init-claims``). Each one is judged:
   ``supported`` / ``partially_supported`` (verbatim quotes of the studies),
   ``derived`` (each number is an explicit metric over a population, computed
   here from the review's data), ``not_empirical`` (aims, definitions,
   structure: no number and no citation allowed), or ``contradicted`` /
   ``insufficient_evidence`` (fails). Every number in a claim must be in one
   of its quotes or produced by one of its derivations. Nothing is judged by
   default. Resource-catalog table rows are claims like any other: copying a
   record is not evidence that it is true.
4. **Numbers.** PRISMA flow arithmetic, the screening log against the flow,
   "N of M (P%)" percentages and stated totals.
5. **Version changes** (``<stem>.revalidation.json``, from
   ``verify_citations.py --apply``). A study is cleared only when its text is
   a new artifact (hash differs from the one read before, retrieved after the
   switch), ``extract_evidence.py refresh`` logged a re-extraction of that
   exact text, and its spans and charting carry the new DOI.
6. **Studies vs reports** (``<stem>.studies.json``). Reports sharing a trial
   registration or dataset accession, or first author and sample size, are
   grouped into one study or declared distinct.
7. **Appendices.** Each sidecar appended to the delivered text must equal what
   the exporter renders from the current ``review.json`` right now.

Verdict PASS / FAIL / INCOMPLETE; exit 0 / 1 / 3 (2 = bad input). Writes
``<stem>.grounding.json|md`` binding the SHA-256 of every input.

Usage:
    python audit_grounding.py out/review.json --init-claims   # write / refresh the ledger
    python audit_grounding.py out/review.json                 # audit

Semantic judgements are recorded, not made: the audit guarantees that each
exists, that its quotes are real text of the cited paper, that its numbers are
quoted or computed, and that its derivations evaluate as stated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from verify_citations import (  # noqa: E402
        AUTHOR_YEAR_RE, DOI_RE, PMID_ID, PMID_RE, _trim, bind_inputs, clean_doi,
        stale_inputs, surname_matches, years_close,
    )
except ImportError as _e:  # httpx, via verify_citations
    sys.exit(f"audit_grounding.py needs httpx ({_e}). Install it (pip install httpx) — "
             "run scripts/check_deps.py — and re-run; the audit cannot proceed without it.")
from grounding_core import (  # noqa: E402
    ABSTRACT_SECTIONS, check_page, check_section, locate, norm, pairs, quantities, quantity_in,
    rubric_for, same_quantity, sha256_text, source_text, text_sha256, unpaired, verbatim,
)
import extract_evidence  # noqa: E402

EXIT = {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3}
SUPPORT = ("supported", "partially_supported", "derived", "not_empirical",
           "contradicted", "insufficient_evidence")
SHARED_ID_RE = re.compile(
    r"\b(NCT\d{8}|ISRCTN\d{8}|ACTRN\d{14}|ChiCTR[-\w]{6,}|DRKS\d{8}|EUCTR[-\d]{10,}|"
    r"GSE\d{3,}|PRJNA\d{3,}|PRJEB\d{3,}|EGA[SD]\d{8,}|phs\d{6}|ds\d{6}|PXD\d{6})\b")
# (Not RRIDs such as SCR_…: those name software, which unrelated studies share.)
PCT_RE = re.compile(r"(\d[\d,]*)\s*(?:of|out of|/)\s*(\d[\d,]*)((?:\s+[\w-]+){0,6})\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*[);,]")
TOTAL_RE = re.compile(r"\b(\d[\d,]*)\s+(?:included\s+)?(studies|publications|reports|articles|papers)"
                      r"\s+(?:were\s+)?included\b|\bincluded\s+(\d[\d,]*)\s+(studies|publications|"
                      r"reports|articles|papers)\b", re.I)
SIDECAR_RE = re.compile(r"^<!-- sidecar: (?P<name>\S+) sha256=(?P<sha>[0-9a-f]{64}) -->$", re.M)
SIDECAR_FORMATS = {".charting.md": "charting", ".appraisal.md": "appraisal",
                   ".per-group.md": "per-group", ".narrative.md": "narrative"}
# What a count over each source can be a count *of*.
NOUNS = {
    "included_articles": {"study", "studies", "publication", "publications", "article", "articles",
                          "paper", "papers", "report", "reports", "record", "records", "trial", "trials"},
    "studies": {"study", "studies", "cohort", "cohorts", "trial", "trials"},
    "resource_catalog": {"resource", "resources", "system", "systems", "graph", "graphs", "kg", "kgs",
                         "tool", "tools", "platform", "platforms", "database", "databases",
                         "ontology", "ontologies", "knowledge", "infrastructure", "infrastructures"},
    "screening_log": {"record", "records", "report", "reports", "article", "articles", "publication",
                      "publications", "citation", "citations", "study", "studies", "decision",
                      "decisions", "exclusion", "exclusions"},
    "evidence_spans": {"span", "spans", "passage", "passages", "excerpt", "excerpts", "quote", "quotes"},
}
NOUNS["charting"] = NOUNS["included_articles"]
FILLER = {"of", "the", "a", "an", "were", "was", "are", "is", "had", "have", "has", "with", "in",
          "per", "and", "or", "out", "more", "fewer", "than", "new", "unique", "distinct", "included",
          "eligible", "additional", "other", "such", "all", "full", "text", "full-text", "these",
          "those", "them", "which", "that", "to", "from", "by", "for", "on", "at", "as", "each",
          "only", "about", "around", "approximately", "nearly", "over", "under", "least", "most"}
LABEL_HEADERS = re.compile(r"^(#|no\.?|n°|item|items|id|rq|section|ref|step|criterion)$", re.I)


def f_(code: str, severity: str, where: str, message: str) -> dict:
    return {"code": code, "severity": severity, "location": where, "message": message}


def snippet(text: str, a: int, b: int, pad: int = 50) -> str:
    return text[max(0, a - pad):b + pad].replace("\n", " ").strip()


def load_json(p: Path, default: Any = None) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default
    except (OSError, ValueError):
        return default


def parse_time(s: str) -> float:
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


# ─────────────────────────── 1. evidence spans + 5. versions ────────────────────────────

def audit_spans(review: dict, arts: dict, findings: list, pages: Optional[dict] = None) -> dict:
    stats: Counter = Counter()
    norm_cache: dict[str, str] = {}
    out_spans = []
    verbatim_by_study: Counter = Counter()
    for i, sp in enumerate(review.get("evidence_spans") or []):
        pid = str(sp.get("paper_pmid") or "")
        a = arts.get(pid)
        where = f"evidence_spans[{i}] ({pid})"
        if not a:
            findings.append(f_("span-unknown-paper", "error", where,
                               f"span cites {pid or 'no paper'}, which is not an included study"))
            stats["unknown-paper"] += 1
            continue
        src = source_text(a)
        ns = norm_cache.setdefault(pid, norm(src))
        loc = locate(sp.get("text", ""), src, ns)
        stats[loc["match"]] += 1
        out_spans.append({"index": i, "paper": pid, "section": sp.get("section", ""),
                          "source_sha256": sha256_text(src), **loc})
        if loc["match"] in ("exact", "normalized"):
            verbatim_by_study[pid] += 1
        elif loc["match"] == "absent":
            findings.append(f_("span-not-in-source", "error", where,
                               f"not found in the paper's stored text: “{sp.get('text', '')[:140]}”"))
        else:
            findings.append(f_("span-paraphrased", "warning", where,
                               f"only {loc['coverage']:.0%} of the span is the paper's own wording — "
                               "a paraphrase is not evidence; it does not count toward the study's evidence"))
        section = str(sp.get("section") or "").strip().lower()
        if loc["match"] in ("exact", "normalized"):
            where_sec = check_section(section, sp.get("text", ""), a)
            if where_sec == "mismatch":
                findings.append(f_("span-section-mismatch", "error", where,
                                   f"attributed to {section}, but the text is in another section of the paper"))
            elif where_sec == "unverifiable":
                findings.append(f_("span-section-unverifiable", "unresolved", where,
                                   f"attributed to {section}, but the stored text has no detectable sections"))
            pg = check_page(sp.get("page"), sp.get("text", ""), (pages or {}).get(pid))
            if pg == "mismatch":
                findings.append(f_("span-page-mismatch", "error", where, f"not on page {sp.get('page')}"))
            elif pg == "unverifiable":
                findings.append(f_("span-page-unverifiable", "unresolved", where,
                                   f"page {sp.get('page')} given, but there is no page map for {pid}"))
        if not (a.get("full_text") or "").strip() and section not in ABSTRACT_SECTIONS:
            findings.append(f_("span-reading-basis", "error", where,
                               f"paper was read as abstract only, but the span is attributed to its {section} section"))
        sdoi, adoi = clean_doi(sp.get("doi") or "").lower(), clean_doi(a.get("doi") or "").lower()
        if sdoi and adoi and sdoi != adoi:
            findings.append(f_("span-other-version", "error", where,
                               f"extracted from DOI {sdoi}, but the study is now {adoi} — re-extract "
                               "(extract_evidence.py refresh)"))
    for pid, a in arts.items():
        if not (a.get("full_text") or "").strip():
            findings.append(f_("included-without-full-text", "error", pid,
                               "included but read on the abstract only: without its full text the study "
                               "cannot be evaluated — retrieve it, or drop it (extract_evidence.py apply "
                               "moves it to reports not retrieved)"))
    for pid in arts:
        if not verbatim_by_study[pid]:
            findings.append(f_("study-without-evidence", "error", pid,
                               "no evidence span of this study is verbatim text of it — extract its "
                               "evidence (extract_evidence.py spans --only) or exclude it"))
    return {"counts": dict(stats), "spans": out_spans,
            "studies_with_evidence": sum(1 for p in arts if verbatim_by_study[p])}


def audit_versions(review: dict, arts: dict, revalidate: dict, log: dict, findings: list) -> None:
    """A version change clears only on a newly retrieved text, re-extracted and logged."""
    rub = rubric_for(review, arts)
    runs = log.get("runs") or []
    for pid, ch in revalidate.items():
        a = arts.get(pid)
        if not a:
            continue
        new = clean_doi(ch.get("new_doi") or "").lower()
        at = parse_time(ch.get("recorded_at", ""))
        text = source_text(a)
        sha = text_sha256(a)
        unmet = []
        if clean_doi(a.get("doi") or "").lower() != new:
            unmet.append(f"the study's DOI is not {new}")
        if not (a.get("full_text") or "").strip():
            unmet.append("no full text of the new version is stored")
        if ch.get("old_text_sha256") and sha == ch["old_text_sha256"]:
            unmet.append("the stored text is the one read before the switch (same SHA-256)")
        if parse_time(a.get("full_text_retrieved_at", "")) <= at:
            unmet.append("the text was not retrieved after the switch (full_text_retrieved_at)")
        need = {"spans", "risk_of_bias", "charting", "appraisal", "narrative"}
        if not any(r.get("pmid") == pid and r.get("text_sha256") == sha and parse_time(r.get("extracted_at")) > at
                   and clean_doi(r.get("doi") or "").lower() == new and need <= set(r.get("stages") or [])
                   for r in runs):
            unmet.append("no extract_evidence.py refresh of this text after the switch")
        spans = [s for s in review.get("evidence_spans") or [] if str(s.get("paper_pmid")) == pid]
        if not spans or any(clean_doi(s.get("doi") or "").lower() != new or not verbatim(s.get("text", ""), text)
                            for s in spans):
            unmet.append("its evidence spans do not all carry the new DOI and occur in the new text")
        r = rub.get(pid)
        if review.get("data_charting_rubrics") and (
                r is None or clean_doi(r.get("doi") or "").lower() not in (new, "")):
            unmet.append("its charting rubric was not re-charted from the new version")
        if new and new not in norm(text).replace(" ", "") and new not in text.lower():
            findings.append(f_("version-doi-not-in-text", "warning", pid,
                               f"the stored text does not print {new}; check it is the published version"))
        if unmet:
            findings.append(f_("version-changed", "error", pid,
                               f"cited version changed {ch.get('old_doi') or '?'} → {new}: " + "; ".join(unmet)))


# ─────────────────────────── 2. structured values ────────────────────────────

def audit_structured(review: dict, review_json: Path, findings: list, pages: Optional[dict] = None) -> dict:
    ledger = load_json(review_json.with_name(f"{review_json.stem}.evidence-ledger.json"), {"items": []})
    items = extract_evidence.verify_items(review, ledger, pages)
    c = Counter((x["kind"], x["status"]) for x in items)
    bad = [x for x in items if x["status"] == "ungrounded"]
    by_kind = Counter(x["kind"] for x in bad)
    for kind, n in by_kind.items():
        ex = next(x for x in bad if x["kind"] == kind)
        findings.append(f_("structured-ungrounded", "error", kind,
                           f"{n} {kind} value(s) have no verified evidence (e.g. {ex['study'] or ''} "
                           f"{ex['label']}: {ex['reason']}). Ground them (extract_evidence.py quote, or "
                           "evidence in the ledger) or remove them (extract_evidence.py apply)"))
    return {"values": len(items), "by_status": {f"{k}:{s}": n for (k, s), n in sorted(c.items())}}


# ─────────────────────────── 6. studies vs reports ────────────────────────────

def audit_studies(review: dict, arts: dict, study_map: dict, findings: list) -> dict:
    grouped: dict[str, str] = {}
    for g in study_map.get("studies") or []:
        for r in g.get("reports") or []:
            if str(r) not in arts:
                findings.append(f_("study-map-unknown-report", "error", g.get("study_id", "?"),
                                   f"report {r} is not an included study"))
            grouped[str(r)] = g.get("study_id", "?")
    distinct = {frozenset(map(str, d.get("reports") or [] if isinstance(d, dict) else d))
                for d in study_map.get("distinct") or [] if isinstance(d, (list, dict))}

    def resolved(members: set[str]) -> bool:
        if all(m in grouped for m in members) and len({grouped[m] for m in members}) == 1:
            return True
        return any(members <= d for d in distinct)

    by_id: dict[str, set[str]] = {}
    for pid, a in arts.items():
        for m in set(SHARED_ID_RE.findall(source_text(a))):
            by_id.setdefault(m, set()).add(pid)
    rub = rubric_for(review, arts)
    by_author_n: dict[tuple, set[str]] = {}
    for pid, a in arts.items():
        first = (re.split(r"[;,]", a.get("authors") or "")[0].strip().split() or [""])[0].lower()
        n = str((rub.get(pid) or {}).get("n_disordered") or "").strip()
        if first and re.fullmatch(r"\d{2,}", n):
            by_author_n.setdefault((first, n), set()).add(pid)
    candidates = [(f"shared identifier {k}", v) for k, v in by_id.items() if len(v) > 1]
    candidates += [(f"same first author {k[0]} and sample size {k[1]}", v)
                   for k, v in by_author_n.items() if len(v) > 1]
    unresolved = []
    for basis, members in candidates:
        if not resolved(members):
            unresolved.append({"basis": basis, "reports": sorted(members)})
            findings.append(f_("study-overlap-unresolved", "unresolved", basis,
                               f"{len(members)} reports ({', '.join(sorted(members)[:6])}) may describe the "
                               "same participants — group them under one study_id in <stem>.studies.json, "
                               "or list them under \"distinct\" with the reason"))
    n_studies = len({grouped[p] for p in arts if p in grouped}) + sum(1 for p in arts if p not in grouped)
    groups = [{"study_id": g.get("study_id"), "reports": g.get("reports") or []}
              for g in study_map.get("studies") or []]
    groups += [{"study_id": p, "reports": [p]} for p in arts if p not in grouped]
    return {"reports": len(arts), "studies": n_studies, "candidate_groups": len(candidates),
            "unresolved": unresolved, "groups": groups}


# ─────────────────────────── 4. numbers ────────────────────────────

def audit_numbers(review: dict, text: str, study_info: dict, findings: list) -> dict:
    flow = review.get("flow") or {}
    n_inc = len(review.get("included_articles") or [])
    checks = 0

    def need(cond: bool, code: str, msg: str, sev: str = "error") -> None:
        nonlocal checks
        checks += 1
        if not cond:
            findings.append(f_(code, sev, "flow", msg))

    g = lambda k: int(flow.get(k) or 0)  # noqa: E731
    if flow:
        need(g("included_synthesis") == n_inc, "flow-included-mismatch",
             f"flow.included_synthesis is {g('included_synthesis')}, but {n_inc} studies are included")
        if g("sought_fulltext"):
            need(g("sought_fulltext") - g("not_retrieved") == g("assessed_eligibility"), "flow-arithmetic",
                 f"sought {g('sought_fulltext')} − not retrieved {g('not_retrieved')} ≠ assessed "
                 f"{g('assessed_eligibility')}")
        if g("assessed_eligibility"):
            need(g("assessed_eligibility") - g("excluded_eligibility") == g("included_synthesis"),
                 "flow-arithmetic", f"assessed {g('assessed_eligibility')} − excluded "
                 f"{g('excluded_eligibility')} ≠ included {g('included_synthesis')}")
        if g("total_identified") and g("after_dedup"):
            need(g("total_identified") - g("duplicates_removed") == g("after_dedup"), "flow-arithmetic",
                 f"identified {g('total_identified')} − duplicates {g('duplicates_removed')} ≠ "
                 f"after deduplication {g('after_dedup')}", "warning")
        log = review.get("screening_log") or []
        if any(str(e.get("stage")).endswith("full_text") for e in log):
            # only decisions on the full text are eligibility assessments; one made on
            # an abstract is a report not retrieved (full evaluation needs the report)
            ft_ex = sum(1 for e in log if str(e.get("stage")).endswith("full_text")
                        and e.get("assessed_on", "full_text") != "abstract_only"
                        and str(e.get("decision")).lower() == "exclude")
            need(ft_ex == g("excluded_eligibility"), "flow-log-mismatch",
                 f"screening_log has {ft_ex} full-text exclusions, flow says {g('excluded_eligibility')}")
    for m in PCT_RE.finditer(text):
        n, d, p = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", "")), float(m.group(4))
        checks += 1
        where = snippet(text, m.start(), m.end())
        if d == 0 or n > d:
            findings.append(f_("percentage-impossible", "error", where, f"{n} of {d} cannot be a share"))
        elif abs(100 * n / d - p) > (0.06 if "." in m.group(4) else 0.51):
            findings.append(f_("percentage-mismatch", "error", where,
                               f"{n} of {d} is {100 * n / d:.1f}%, not {m.group(4)}%"))
    for m in TOTAL_RE.finditer(text):
        n = int((m.group(1) or m.group(3)).replace(",", ""))
        unit = (m.group(2) or m.group(4)).lower()
        want = study_info.get("studies", n_inc) if unit == "studies" else n_inc
        checks += 1
        if n != want:
            findings.append(f_("count-mismatch", "error", snippet(text, m.start(), m.end()),
                               f"says {n} included {unit}; the data has {want}"))
    return {"checks": checks}


# ─────────────────────────── 3. claims ────────────────────────────

ABBREV = re.compile(r"\b(et al|e\.g|i\.e|vs|fig|figs|approx|ca|cf|no|nos|ref|refs|sec|eq|[A-Z])\.", re.I)


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def delivered_units(md: str) -> list[dict]:
    """Every claim-bearing unit of the delivered Markdown: prose sentences, table
    rows, captions. Skips headings, code, comments, front matter, the reference
    list and appended sidecars (checked separately)."""
    units: list[dict] = []
    heading, buf, fence, stop = "", [], False, False
    lines = md.splitlines()
    if lines and lines[0].strip() == "---":                      # YAML front matter
        end = next((i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---"), 0)
        lines = lines[end + 1:]
    table: list[list[str]] = []

    def flush_par():
        if buf:
            units.append({"kind": "prose", "location": heading, "text": " ".join(buf)})
            buf.clear()

    def flush_table():
        if len(table) >= 2:
            head = table[0]
            for row in table[1:]:
                if all(re.fullmatch(r":?-{2,}:?", c or "-") for c in row):
                    continue
                units.append({"kind": "table", "location": heading, "header": head, "row": row,
                              "text": "; ".join(f"{h}: {c}" for h, c in zip(head, row) if c)})
        table.clear()

    for line in lines:
        s = line.strip()
        if SIDECAR_RE.match(s):
            flush_par(); flush_table()
            break                                                   # appendices are checked as sidecars
        if s.startswith("```"):
            fence = not fence
            continue
        if fence or s.startswith("<!--"):
            continue
        if s.startswith("|"):
            flush_par()
            table.append(_cells(s))
            continue
        flush_table()
        if s.startswith("#"):
            flush_par()
            heading = s.lstrip("#").strip()
            stop = bool(re.match(r"(\d+\.?\s*)?(references|bibliography)\b", heading, re.I))
            continue
        if stop:
            continue
        if not s:
            flush_par()
            continue
        m = re.match(r"!\[([^\]]*)\]\([^)]*\)", s)
        if m:
            flush_par()
            if m.group(1).strip():
                units.append({"kind": "caption", "location": heading, "text": m.group(1).strip()})
            continue
        if re.match(r"\*\*(Figure|Table)\s*\d*\.?\*\*", s):
            flush_par()
            units.append({"kind": "caption", "location": heading, "text": re.sub(r"\*\*", "", s)})
            continue
        buf.append(re.sub(r"^[-*>]\s+|^\d+\.\s+", "", s))
    flush_par(); flush_table()
    out = []
    for u in units:
        if u["kind"] == "prose":
            for sent in sentences(u["text"]):
                if len(re.findall(r"[A-Za-z]{2,}", sent)) >= 3:
                    out.append({**u, "text": sent})
        else:
            out.append(u)
    return out


def sentences(par: str) -> list[str]:
    protected = ABBREV.sub(lambda m: m.group(0).replace(".", "․"), par)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\[*])", protected)
    return [p.replace("․", ".").strip() for p in parts if p.strip()]


def claim_id(text: str) -> str:
    return "C" + hashlib.sha1(norm(text).encode()).hexdigest()[:10]


def catalog_studies(ms: dict, arts: dict) -> dict[str, list[str]]:
    lower = {k.lower(): k for k in arts}
    out = {}
    for row in ms.get("resource_catalog") or []:
        pids = row.get("publication_ids") or []
        if isinstance(pids, str):
            pids = re.split(r"[;,]\s*", pids)
        res = [lower.get(str(p).lower()) or lower.get(f"local_{str(p).lower()}") for p in pids]
        out[str(row.get("resource_id") or "")] = [r for r in res if r]
    return {k: v for k, v in out.items() if k}


def cited_studies(text: str, arts: dict, catalog_map: dict) -> list[str]:
    keys: list[str] = []
    by_doi = {clean_doi(a.get("doi") or "").lower(): k for k, a in arts.items() if a.get("doi")}
    for m in PMID_RE.finditer(text):
        keys += [p for p in PMID_ID.findall(m.group(1)) if p in arts]
    for d in DOI_RE.findall(text):
        k = by_doi.get(_trim(d).lower())
        if k:
            keys.append(k)
    for m in AUTHOR_YEAR_RE.finditer(text):
        keys += [k for k, a in arts.items() if surname_matches(m.group(1), a.get("authors") or "")
                 and years_close(str(a.get("year") or ""), m.group(2))]
    for rid, studies in catalog_map.items():
        if re.search(rf"\b{re.escape(rid)}\b", text, re.I):
            keys += studies
    return list(dict.fromkeys(keys))


def _plain(cell: str) -> str:
    return re.sub(r"[\[\]*_`]", "", str(cell)).strip().lower()


def unit_quantities(unit: dict) -> list[dict]:
    if unit["kind"] != "table":
        return quantities(unit["text"])
    out = []
    for h, c in zip(unit["header"], unit["row"]):
        if not LABEL_HEADERS.match(_plain(h)):
            out += quantities(c)
    return out


def build_claims(md: str, arts: dict, ms: dict) -> list[dict]:
    cmap = catalog_studies(ms, arts)
    claims, seen = [], set()
    for u in delivered_units(md):
        # Reproducing a catalog value is not evidence that the value is true.
        # Catalog rows require the same judgement and quotes as other rows.
        cid = claim_id(f"{u['location']}|{u['text']}")
        if cid in seen:
            continue
        seen.add(cid)
        qs = unit_quantities(u)
        claims.append({"id": cid, "kind": u["kind"], "location": u["location"], "text": u["text"],
                       "numbers": [q["text"] for q in qs], "_q": qs,
                       "cites": cited_studies(u["text"], arts, cmap)})
    return claims


# ---- derivations ----

def sources(review: dict, ms: dict, arts: dict, study_info: dict) -> dict[str, list]:
    rub = rubric_for(review, arts)
    charting = [{**r, "study": pid} for pid, r in rub.items()]
    return {"included_articles": list(arts.values()), "charting": charting,
            "resource_catalog": ms.get("resource_catalog") or [],
            "screening_log": review.get("screening_log") or [],
            "evidence_spans": review.get("evidence_spans") or [],
            "studies": study_info.get("groups") or []}


def _field(row: Any, path: str) -> Any:
    for k in str(path).split("."):
        if isinstance(row, dict):
            row = row.get(k)
        elif isinstance(row, list) and k.isdigit() and int(k) < len(row):
            row = row[int(k)]
        else:
            return None
    return row


def _match(v: Any, cond: Any) -> bool:
    if isinstance(v, list):
        return any(_match(x, cond) for x in v)
    sv = str(v if v is not None else "").strip().lower()
    if isinstance(cond, list):
        return sv in {str(c).strip().lower() for c in cond}
    if isinstance(cond, dict):
        for op, x in cond.items():
            if op == "contains" and str(x).lower() not in sv:
                return False
            if op == "regex" and not re.search(str(x), str(v or ""), re.I):
                return False
            if op == "not" and _match(v, x):
                return False
            if op == "empty" and bool(sv) == bool(x):
                return False
            if op in ("gt", "gte", "lt", "lte"):
                try:
                    f = float(sv)
                except ValueError:
                    return False
                if not {"gt": f > x, "gte": f >= x, "lt": f < x, "lte": f <= x}[op]:
                    return False
        return True
    return sv == str(cond).strip().lower()


def evaluate(expr: Any, ctx: dict, root: dict) -> Optional[float]:
    """Evaluate a derivation expression; None when it is malformed or names nothing."""
    if not isinstance(expr, dict) or len(expr) != 1:
        return None
    (op, arg), = expr.items()
    try:
        if op in ("count", "distinct", "sum", "mean", "median", "min", "max"):
            rows = [r for r in ctx.get(arg.get("from"), []) or []
                    if all(_match(_field(r, k), c) for k, c in (arg.get("where") or {}).items())]
            if op == "count":
                return float(len(rows))
            vals = [_field(r, arg["field"]) for r in rows]
            if op == "distinct":
                flat = {str(x).strip().lower() for v in vals for x in (v if isinstance(v, list) else [v]) if x}
                return float(len(flat))
            nums = [float(str(v).replace(",", "")) for v in vals if re.fullmatch(r"-?\d+(\.\d+)?", str(v).replace(",", ""))]
            if not nums:
                return None
            return float({"sum": sum, "mean": statistics.mean, "median": statistics.median,
                          "min": min, "max": max}[op](nums))
        if op in ("percent", "ratio"):
            a, b = evaluate(arg[0], ctx, root), evaluate(arg[1], ctx, root)
            if a is None or not b:
                return None
            return 100 * a / b if op == "percent" else a / b
        if op == "value":
            v = _field(root, arg["path"])
            return float(str(v).replace(",", "")) if v is not None and re.fullmatch(
                r"-?\d+(\.\d+)?", str(v).replace(",", "")) else None
        if op == "len":
            v = _field(root, arg["path"])
            return float(len(v)) if isinstance(v, (list, dict, str)) else None
        if op == "recorded":
            v = _field(root, arg["path"])
            hay = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            if str(arg.get("text") or "") and str(arg["text"]) in hay:
                qs = quantities(str(arg["text"]), keep_years=True)
                return qs[0]["value"] * (-1 if qs and qs[0]["sign"] else 1) if qs else None
            return None
    except (KeyError, TypeError, ValueError, IndexError, statistics.StatisticsError):
        return None
    return None


def expr_source(expr: Any) -> Optional[str]:
    if isinstance(expr, dict) and len(expr) == 1:
        (op, arg), = expr.items()
        if op in ("count", "distinct") and isinstance(arg, dict):
            return arg.get("from")
    return None


def noun_after(text: str, q: dict) -> str:
    """The word a quantity counts ("103 resources" → resources), skipping fillers and numbers."""
    rest = text[q["pos"]:]
    rest = re.sub(r"^[<>≤≥±+-]?\s*\d[\d,.]*%?", "", rest)
    for w in re.findall(r"[A-Za-z][\w-]*|\d[\d,.]*%?", rest)[:6]:
        lw = w.lower()
        if lw in FILLER or re.match(r"\d", lw):
            continue
        return lw
    return ""


def derivation_unit(expr: Any, ctx: dict, root: dict) -> Optional[str]:
    """Infer dimensions from source data, never from the claim's own label.

    Bare numeric fields have unknown dimensions. Measurement fields can record
    a sibling ``<field>_unit`` (or ``unit``); recorded text carries its own unit.
    Counts are dimensionless. Percent and ratio require compatible operands.
    """
    if not isinstance(expr, dict) or len(expr) != 1:
        return None
    (op, arg), = expr.items()

    def field_unit(row, field):
        parent_path, _, name = field.rpartition(".")
        parent = _field(row, parent_path) if parent_path else row
        if not isinstance(parent, dict):
            return None
        label = parent.get(f"{name}_unit", parent.get("unit"))
        qs = quantities(f"1 {label}", True) if label else []
        return ("%" if qs[0]["pct"] else qs[0].get("unit") or None) if qs else None

    try:
        if op in ("count", "distinct", "len"):
            return ""
        if op in ("percent", "ratio"):
            a, b = (derivation_unit(x, ctx, root) for x in arg)
            return ("%" if op == "percent" else "") if a is not None and a == b else None
        if op == "recorded":
            qs = quantities(str(arg.get("text") or ""), True)
            return ("%" if qs[0]["pct"] else qs[0].get("unit") or None) if qs else None
        if op == "value":
            return field_unit(root, arg["path"])
        if op in ("sum", "mean", "median", "min", "max"):
            rows = [r for r in ctx.get(arg["from"], [])
                    if all(_match(_field(r, k), c) for k, c in (arg.get("where") or {}).items())]
            units = {field_unit(r, arg["field"]) for r in rows}
            return next(iter(units)) if len(units) == 1 else None
    except (KeyError, TypeError, ValueError):
        return None
    return None


def check_derivation(claim_text: str, q: dict, d: dict, ctx: dict, root: dict) -> Optional[str]:
    """None when derivation ``d`` produces quantity ``q``; otherwise why not."""
    for f in ("metric", "population"):
        if not str(d.get(f) or "").strip():
            return f"derivation for {q['text']} names no {f}"
    got = evaluate(d.get("expr"), ctx, root)
    if got is None:
        return f"derivation for {q['text']} does not evaluate ({json.dumps(d.get('expr'))[:120]})"
    tol = 0.5 * 10 ** -q["decimals"] + 1e-9
    want = -q["value"] if q["sign"] else q["value"]
    if abs(got - want) > tol:
        return f"derivation for {q['text']} computes {got:.4g}"
    unit = derivation_unit(d.get("expr"), ctx, root)
    wanted_unit = "%" if q["pct"] else q.get("unit", "")
    if wanted_unit and unit != wanted_unit:
        return f"derivation for {q['text']} has unit {unit or 'unknown/unitless'}, not {wanted_unit}"
    src = expr_source(d.get("expr"))
    if src in NOUNS and not q["pct"]:
        noun = noun_after(claim_text, q)
        if noun and noun not in NOUNS[src] and noun.rstrip("s") not in NOUNS[src]:
            return (f"{q['text']} counts “{noun}”, but the derivation counts rows of {src} "
                    f"({', '.join(sorted(NOUNS[src])[:6])}…)")
    return None


def suggestions(q: dict, ctx: dict) -> list[dict]:
    """Derivations over the data that produce ``q`` — proposals, never accepted automatically."""
    out = []
    if q["pct"] or q["sign"] or q["op"]:
        return out
    v = q["value"]
    for src, rows in ctx.items():
        if float(len(rows)) == v:
            out.append({"count": {"from": src}})
    for src in ("charting", "resource_catalog"):
        vals: dict[tuple, int] = Counter()
        for r in ctx.get(src) or []:
            for k, x in r.items():
                if isinstance(x, (str, int)) and str(x).strip() and len(str(x)) < 60:
                    vals[(k, str(x).strip())] += 1
        out += [{"count": {"from": src, "where": {k: x}}} for (k, x), n in vals.items() if n == v][:3]
    return out[:5]


def init_claims(review: dict, md_path: Path, ms: dict, ledger_path: Path,
                study_info: Optional[dict] = None) -> dict:
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    md = md_path.read_text(encoding="utf-8")
    old = {}
    if ledger_path.is_file():
        try:
            old = {c["id"]: c for c in json.loads(ledger_path.read_text(encoding="utf-8")).get("claims") or []}
        except (OSError, ValueError, KeyError):
            old = {}
    ctx = sources(review, ms, arts, study_info or {"groups": [{"study_id": p, "reports": [p]} for p in arts]})
    current = build_claims(md, arts, ms)
    claims = []
    for c in current:
        c.pop("_q", None)
        prev = old.get(c["id"])
        if prev:
            c.update({k: prev[k] for k in ("support", "evidence", "derivations", "explanation", "judged_by")
                      if k in prev})
        else:
            c.update({"support": None, "evidence": [], "derivations": [], "explanation": "", "judged_by": ""})
            if c["cites"]:
                words = set(norm(c["text"]).split())
                spans = [s for s in review.get("evidence_spans") or [] if str(s.get("paper_pmid")) in c["cites"]]
                c["candidate_evidence"] = [{"study": s.get("paper_pmid"), "quote": s.get("text", "")}
                                           for s in sorted(spans, key=lambda s: -len(words & set(norm(s.get("text", "")).split())))[:4]]
            sug = {q: suggestions(x, ctx) for q, x in zip(c["numbers"], quantities(c["text"]))}
            if any(sug.values()):
                c["suggested_derivations"] = {k: v for k, v in sug.items() if v}
        claims.append(c)
    ledger = {"delivered": str(md_path.resolve()), "delivered_sha256": sha256_text(md),
              "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "instructions": (
                  "Judge every claim. support: supported | partially_supported (evidence: [{study, quote}] "
                  "copied verbatim from that study), derived (derivations: [{value, metric, population, "
                  "expr}] — one per number; expr is evaluated against the review data), not_empirical "
                  "(aims, definitions, structure; no numbers or citations allowed), contradicted | "
                  "insufficient_evidence (the sentence must then be corrected or removed). Every number "
                  "must be in a quote or produced by a derivation. Fill explanation and judged_by. "
                  "suggested_derivations and candidate_evidence are proposals to check, not answers."),
              "claims": claims}
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ledger


def audit_claims(review: dict, md: str, md_path: Path, ms: dict, ledger: Optional[dict],
                 study_info: dict, findings: list, pages: Optional[dict] = None) -> dict:
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    current = build_claims(md, arts, ms)
    stats: Counter = Counter()
    if ledger is None:
        findings.append(f_("claims-not-audited", "unresolved", md_path.name,
                           f"no claim ledger: run audit_grounding.py --init-claims and judge all "
                           f"{len(current)} claims"))
        return {"claims": len(current)}
    if ledger.get("delivered_sha256") != sha256_text(md):
        findings.append(f_("claims-stale", "unresolved", md_path.name,
                           "the delivered text changed since the ledger was written: re-run --init-claims "
                           "(unchanged claims keep their judgement) and judge the new ones"))
    judged = {c["id"]: c for c in ledger.get("claims") or []}
    ctx = sources(review, ms, arts, study_info)
    root = {"review": review, "manuscript": ms, "flow": review.get("flow") or {},
            "protocol": review.get("protocol") or {}, "search_queries": review.get("search_queries") or []}
    cache: dict[str, tuple[str, str]] = {}
    for c in current:
        j = judged.get(c["id"])
        where = f"{c['id']} ({c['location'] or 'body'}, {c['kind']})"
        quote_text = c["text"][:120]
        if not j or j.get("support") not in SUPPORT:
            stats["unjudged"] += 1
            findings.append(f_("claim-unjudged", "unresolved", where, f"“{quote_text}”"))
            continue
        sup = j["support"]
        stats[sup] += 1
        if sup in ("contradicted", "insufficient_evidence"):
            findings.append(f_(f"claim-{sup.replace('_', '-')}", "error", where,
                               f"{j.get('explanation') or 'no explanation'} — correct or remove: “{quote_text}”"))
            continue
        qs = c["_q"]
        if sup == "not_empirical":
            if qs or c["cites"]:
                findings.append(f_("not-empirical-misused", "error", where,
                                   "marked not_empirical but states numbers or cites studies"))
            elif not (j.get("explanation") or "").strip():
                findings.append(f_("not-empirical-unexplained", "unresolved", where,
                                   "not_empirical needs an explanation (aim, definition, structure, …)"))
            else:
                findings.append(f_("not-empirical", "warning", where,
                                   f"{j['explanation'][:80]}: “{quote_text}”"))
            continue
        quotes = []
        for e in j.get("evidence") or []:
            sid = str(e.get("study") or "")
            if sid not in arts:
                findings.append(f_("evidence-unknown-study", "error", where,
                                   f"evidence names {sid or 'no study'}, not an included study"))
                continue
            if c["cites"] and sid not in c["cites"]:
                findings.append(f_("evidence-not-cited", "warning", where,
                                   f"evidence from {sid}, which the claim does not cite"))
            src, ns = cache.setdefault(sid, (source_text(arts[sid]), norm(source_text(arts[sid]))))
            if verbatim(e.get("quote", ""), src, ns):
                quotes.append(e.get("quote", ""))
                for kind, res in (("section", check_section(str(e.get("section") or ""), e.get("quote", ""), arts[sid])),
                                  ("page", check_page(e.get("page"), e.get("quote", ""), (pages or {}).get(sid)))):
                    if res == "mismatch":
                        findings.append(f_(f"evidence-{kind}-mismatch", "error", where,
                                           f"quote from {sid} is not in its stated {kind} {e.get(kind)}"))
                    elif res == "unverifiable":
                        findings.append(f_(f"evidence-{kind}-unverifiable", "unresolved", where,
                                           f"{kind} {e.get(kind)} of {sid} cannot be checked "
                                           f"({'no page map' if kind == 'page' else 'no detectable sections'})"))
            else:
                findings.append(f_("quote-not-in-source", "error", where,
                                   f"quote is not text of {sid}: “{str(e.get('quote'))[:120]}”"))
        if sup in ("supported", "partially_supported") and not quotes:
            findings.append(f_("claim-no-evidence", "error", where, f"judged {sup} but no quote is verbatim text"))
            continue
        if sup == "derived" and not qs:
            findings.append(f_("derived-without-numbers", "error", where,
                               "derived claims must state numbers; use supported or not_empirical"))
            continue
        if not c["cites"] and sup in ("supported", "partially_supported") and not qs:
            findings.append(f_("uncited-claim", "warning", where,
                               "supported by quotes but the sentence cites none of the studies"))
        joined = "\n".join(quotes)
        ders = j.get("derivations") or []
        derived_vals = {float(x["value"]) for d in ders for x in quantities(str(d.get("value") or ""), True)}
        for miss in unpaired(c["text"], quotes):
            n, d = (float(x) for x in miss.split(" of "))
            if not (n in derived_vals and d in derived_vals):
                findings.append(f_("denominator-unpaired", "error", where,
                                   f"“{miss}”: the count and its denominator are not stated together in "
                                   "one quote (nor both derived)"))
        for q in qs:
            if quantity_in(q, joined):
                continue
            ds = [d for d in ders if any(same_quantity(q, x) for x in quantities(str(d.get("value") or ""), True))]
            if not ds:
                findings.append(f_("number-ungrounded", "error", where,
                                   f"{q['text']} is in no quote and has no derivation: “{quote_text}”"))
                continue
            why = [check_derivation(c["text"], q, d, ctx, root) for d in ds]
            if all(w is not None for w in why):
                findings.append(f_("derivation-wrong", "error", where, "; ".join(w for w in why if w)))
        if sup == "partially_supported":
            findings.append(f_("claim-partial", "warning", where,
                               f"partially supported: {j.get('explanation') or 'no explanation'}"))
        if not (j.get("judged_by") or "").strip():
            findings.append(f_("claim-judge-unrecorded", "warning", where, "judged_by is empty"))
    return {"claims": len(current),
            "by_kind": dict(Counter(c["kind"] for c in current)), "outcomes": dict(stats),
            "ledger_entries_no_longer_in_text": len(set(judged) - {c["id"] for c in current})}


# ─────────────────────────── 7. appendices ────────────────────────────

def audit_sidecars(review_json: Path, md: str, md_path: Path, findings: list) -> dict:
    """Each appended sidecar must be exactly what the exporter renders from review.json now."""
    marks = list(SIDECAR_RE.finditer(md))
    if not marks:
        return {"sidecars": 0}
    to_render = []
    for m in marks:
        name = m.group("name")
        side = md_path.with_name(name)
        if not side.is_file() or hashlib.sha256(side.read_bytes()).hexdigest() != m.group("sha"):
            findings.append(f_("sidecar-changed", "error", name,
                               "the appended file is missing or changed since the text was assembled"))
        elif any(name.endswith(s) for s in SIDECAR_FORMATS):
            to_render.append(name)
    if not to_render:
        return {"sidecars": len(marks)}
    fmts = sorted({SIDECAR_FORMATS[s] for n in to_render for s in SIDECAR_FORMATS if n.endswith(s)})
    try:
        from export_review import load_result, write_exports
        with tempfile.TemporaryDirectory() as tmp:
            write_exports(load_result(str(review_json)), tmp, fmts, base=review_json.stem)
            rendered = {p.name: p.read_text(encoding="utf-8") for p in Path(tmp).glob("*.md")}
    except (Exception, SystemExit) as e:  # synthscholar missing, or the review does not load
        findings.append(f_("sidecar-unverifiable", "unresolved", md_path.name,
                           f"appendices cannot be re-rendered from {review_json.name} ({e!r:.160}); "
                           "install the synthscholar dev checkout (check_deps.py) and export a valid review"))
        return {"sidecars": len(marks)}
    for name in to_render:
        if rendered.get(name) != md_path.with_name(name).read_text(encoding="utf-8"):
            findings.append(f_("sidecar-not-from-review", "error", name,
                               f"not what the exporter renders from the current {review_json.name}: "
                               "re-export it (export_review.py --formats …) before delivery"))
    return {"sidecars": len(marks)}


# ─────────────────────────── driver ────────────────────────────

def delivered_markdown(review_json: Path) -> Optional[Path]:
    stem = review_json.stem
    for name in (f"{stem}.delivered.md", f"{stem}.manuscript.md", f"{stem}.md"):
        p = review_json.with_name(name)
        if p.is_file():
            return p
    return None


def sidecars(review_json: Path) -> dict[str, Path]:
    stem = review_json.stem
    return {k: review_json.with_name(f"{stem}.{k}.json")
            for k in ("manuscript", "claims", "studies", "revalidation", "evidence-ledger", "extraction",
                      "pages")}


def audit(review_json: Path, md_path: Optional[Path] = None) -> dict:
    review = json.loads(review_json.read_text(encoding="utf-8"))
    side = sidecars(review_json)
    md_path = md_path or delivered_markdown(review_json)
    ms = load_json(side["manuscript"], {}) or {}
    ledger = load_json(side["claims"])
    study_map = load_json(side["studies"], {}) or {}
    revalidate = {str(x.get("pmid")): x for x in (load_json(side["revalidation"], {}) or {}).get("changes") or []}
    log = load_json(side["extraction"], {}) or {}
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}

    findings: list[dict] = []
    pages = {k: v.get("pages") if isinstance(v, dict) else v
             for k, v in (load_json(side["pages"], {}) or {}).items()}
    span_info = audit_spans(review, arts, findings, pages)
    audit_versions(review, arts, revalidate, log, findings)
    struct_info = audit_structured(review, review_json, findings, pages)
    study_info = audit_studies(review, arts, study_map, findings)
    md = md_path.read_text(encoding="utf-8") if md_path else ""
    num_info = audit_numbers(review, md, study_info, findings)
    if md_path:
        claim_info = audit_claims(review, md, md_path, ms, ledger, study_info, findings, pages)
        side_info = audit_sidecars(review_json, md, md_path, findings)
    else:
        claim_info, side_info = {}, {}
        findings.append(f_("no-delivered-text", "unresolved", review_json.name,
                           "no <stem>.delivered.md / .manuscript.md / .md to audit — export first"))
    errors = sum(f["severity"] == "error" for f in findings)
    unresolved = sum(f["severity"] == "unresolved" for f in findings)
    verdict = "FAIL" if errors else "INCOMPLETE" if unresolved or not arts else "PASS"
    inputs = [review_json] + ([md_path] if md_path else []) + [p for p in side.values() if p.is_file()]
    inputs += [md_path.with_name(m.group("name")) for m in SIDECAR_RE.finditer(md)] if md_path else []
    return {
        "review": str(review_json), "delivered": str(md_path.resolve()) if md_path else None,
        "audited_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": bind_inputs(inputs),
        "summary": {"verdict": verdict, "errors": errors, "unresolved": unresolved,
                    "warnings": sum(f["severity"] == "warning" for f in findings),
                    "by_code": dict(Counter(f["code"] for f in findings).most_common())},
        "evidence_spans": span_info["counts"], "studies_with_evidence": span_info["studies_with_evidence"],
        "structured": struct_info, "studies": {k: v for k, v in study_info.items() if k != "groups"},
        "numbers": num_info, "claims": claim_info, "appendices": side_info,
        "findings": findings, "span_locations": span_info["spans"],
    }


def render(rep: dict) -> str:
    s = rep["summary"]
    cl = rep["claims"] or {}
    out = [f"# Grounding audit — {Path(rep['review']).name}\n",
           f"**Verdict: {s['verdict']}** — {s['errors']} error(s), {s['unresolved']} unresolved, "
           f"{s['warnings']} warning(s). Delivered text: "
           f"`{Path(rep['delivered']).name if rep['delivered'] else 'none'}`.\n",
           "| Check | Result |", "| --- | --- |",
           f"| Evidence spans | {', '.join(f'{k} {v}' for k, v in sorted(rep['evidence_spans'].items())) or 'none'}; "
           f"{rep['studies_with_evidence']} of {rep['studies']['reports']} studies with verbatim evidence |",
           f"| Structured values | {rep['structured']['values']}: "
           f"{', '.join(f'{k} {v}' for k, v in rep['structured']['by_status'].items())} |",
           f"| Studies vs reports | {rep['studies']['reports']} reports → {rep['studies']['studies']} studies; "
           f"{len(rep['studies']['unresolved'])} unresolved overlap group(s) |",
           f"| Numbers | {rep['numbers'].get('checks', 0)} checks |",
           f"| Claims | {cl.get('claims', 0)} ({', '.join(f'{k} {v}' for k, v in (cl.get('by_kind') or {}).items())}); "
           f"{', '.join(f'{k} {v}' for k, v in sorted((cl.get('outcomes') or {}).items())) or '—'}; " "|",
           f"| Appendices | {rep['appendices'].get('sidecars', 0)} |", ""]
    order = {"error": 0, "unresolved": 1, "warning": 2}
    rows = sorted(rep["findings"], key=lambda f: order.get(f["severity"], 3))
    if rows:
        out += ["| Severity | Code | Where | Finding |", "| --- | --- | --- | --- |"]
        out += [f"| {f['severity']} | `{f['code']}` | {f['location'][:60].replace('|', '/')} | "
                f"{f['message'].replace('|', '/')} |" for f in rows[:600]]
        if len(rows) > 600:
            out.append(f"\n… {len(rows) - 600} more in the JSON report.")
    out.append("\nJudgements in the claim ledger and evidence ledger were made by a model or a person "
               "(`judged_by`, `source`); this audit verifies their quotes, numbers and derivations.")
    return "\n".join(out) + "\n"


def write(rep: dict, review_json: Path) -> Path:
    base = review_json.with_name(f"{review_json.stem}.grounding")
    Path(f"{base}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    Path(f"{base}.md").write_text(render(rep), encoding="utf-8")
    return base


def is_current(review_json: Path, md_path: Optional[Path] = None) -> Optional[dict]:
    """The grounding report if it exists, covers ``md_path`` and no input changed."""
    rep = load_json(review_json.with_name(f"{review_json.stem}.grounding.json"))
    if not rep or stale_inputs(rep):
        return None
    md = md_path or delivered_markdown(review_json)
    if md and rep.get("delivered") != str(md.resolve()):
        return None
    seen = set(rep.get("inputs") or {})
    news = [p for p in list(sidecars(review_json).values()) + ([md] if md else [])
            if p.is_file() and str(p.resolve()) not in seen]
    return None if news else rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("review", help="review JSON (PRISMAReviewResult)")
    ap.add_argument("--markdown", help="delivered Markdown (default: <stem>.delivered.md, .manuscript.md, .md)")
    ap.add_argument("--init-claims", action="store_true",
                    help="write / refresh <stem>.claims.json from the delivered text, keeping judgements "
                         "of unchanged claims, then audit")
    args = ap.parse_args()
    src = Path(args.review)
    if not src.is_file():
        print(f"no such file: {src}", file=sys.stderr)
        return 2
    md = Path(args.markdown) if args.markdown else delivered_markdown(src)
    if args.init_claims:
        if not md:
            print("no delivered Markdown to take claims from — export / assemble first", file=sys.stderr)
            return 2
        review = json.loads(src.read_text(encoding="utf-8"))
        ms = load_json(sidecars(src)["manuscript"], {}) or {}
        arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
        study_info = audit_studies(review, arts, load_json(sidecars(src)["studies"], {}) or {}, [])
        led = init_claims(review, md, ms, sidecars(src)["claims"], study_info)
        todo = sum(1 for c in led["claims"] if c.get("support") not in SUPPORT)
        print(f"claims: {len(led['claims'])} → {sidecars(src)['claims']} ({todo} to judge)", file=sys.stderr)
    rep = audit(src, md)
    base = write(rep, src)
    s = rep["summary"]
    print(f"Grounding: {s['verdict']} — {s['errors']} error(s), {s['unresolved']} unresolved, "
          f"{s['warnings']} warning(s)", file=sys.stderr)
    for code, n in list(s["by_code"].items())[:15]:
        print(f"  {code}: {n}", file=sys.stderr)
    print(f"report: {base}.md  ({base}.json)", file=sys.stderr)
    return EXIT[s["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
