#!/usr/bin/env python3
"""Audit whether a review's delivered claims follow from its evidence.

``verify_citations.py`` proves the references are real and belong to the
papers named. This proves the *content* is grounded in them. It runs over
``review.json`` plus the delivered Markdown (``<stem>.manuscript.md``, else
``<stem>.md``) and checks five things:

1. **Evidence spans** (``evidence_spans``) — every span is looked up in the
   stored text of its paper: verbatim, or verbatim after normalising case,
   quotes, whitespace and PDF hyphenation. A span found nowhere is an error
   (invented evidence); one only partly found is a paraphrase, not a quote.
   Reading basis: a paper read as abstract only cannot supply a span from its
   methods or results. Version: a span recording a DOI other than the
   article's was extracted from a different version of the paper.
2. **Claims** — every substantive sentence of the delivered Markdown (one with
   a number or a citation) gets an id in a ledger, ``<stem>.claims.json``.
   ``--init-claims`` writes it; the agent (or a person) judges each claim that
   cites a study as ``supported``, ``partially_supported``, ``contradicted`` or
   ``insufficient_evidence``, with the quotes that bear on it and a one-line
   explanation. The audit then *verifies* the judgement's evidence: every quote
   must occur in the cited study's stored text, and every number in the claim
   must appear in a quote. Contradicted or insufficient claims fail until the
   prose is corrected. A claim citing no study is an aggregate: it passes when
   each of its numbers is recomputed from the structured data (flow counts,
   included set, charting fields, resource catalog); otherwise it needs a
   ``derived_from`` and is listed for human review.
3. **Numbers** — PRISMA flow arithmetic, the screening log against the flow,
   "N of M (P%)" percentages, and stated totals of included studies/reports.
4. **Version changes** — ``verify_citations.py --apply`` records each
   preprint → published switch in ``<stem>.revalidation.json``. The study fails
   until its evidence spans carry the new DOI and are found in the current text.
5. **Studies vs reports** — reports sharing a trial registration or a dataset
   accession, or the same first author and sample size, may describe one set of
   participants. Each such group must be resolved in ``<stem>.studies.json``
   (grouped into one ``study_id`` or declared distinct); "N studies" in the
   prose is then checked against the number of studies, not of reports.

Verdict, as for citations: **PASS**, **FAIL** (errors) or **INCOMPLETE**
(something unjudged, unresolved or stale). Writes ``<stem>.grounding.json|md``
binding the SHA-256 of every input. ``generate_review_pdf.py`` refuses a PDF
unless this is PASS and current. Exit 0 = PASS, 1 = FAIL, 3 = INCOMPLETE,
2 = bad input.

Usage:
    python audit_grounding.py out/review.json --init-claims   # write / refresh the ledger
    # …judge each claim in out/review.claims.json…
    python audit_grounding.py out/review.json                 # audit → out/review.grounding.md

Semantic judgements are not made here. They are recorded, with their evidence
and who made them; what this script guarantees is that each one exists, that
its quotes are real text of the cited paper, and that its numbers are sourced.
A judgement that agrees with the authoring model is not independent evidence,
so the report lists every judgement for human review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
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

EXIT = {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3}
SUPPORT = ("supported", "partially_supported", "contradicted", "insufficient_evidence")
# Sections a paper read only as an abstract can still supply.
ABSTRACT_SECTIONS = {"", "abstract", "title", "summary", "keywords"}
# Share of a span's word 6-grams found in the source for it to count as a paraphrase.
PARAPHRASE = 0.5
# Identifiers that name one set of participants or one dataset.
SHARED_ID_RE = re.compile(
    r"\b(NCT\d{8}|ISRCTN\d{8}|ACTRN\d{14}|ChiCTR[-\w]{6,}|DRKS\d{8}|EUCTR[-\d]{10,}|"
    r"GSE\d{3,}|PRJNA\d{3,}|PRJEB\d{3,}|EGA[SD]\d{8,}|phs\d{6}|ds\d{6}|PXD\d{6})\b")
# (Not RRIDs such as SCR_…: those name software and tools, which unrelated
# studies share without sharing participants.)
NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![\w])")
# Words before a number that make it a label, not a quantity.
LABEL_BEFORE = re.compile(r"(?:section|sections|table|tables|figure|fig\.|appendix|item|items|"
                          r"rq|ec|ic|step|phase|version|v|pmid|pmids|grade|level)\s*$", re.I)
# "N of M <up to six words> (P%)" — the percentage closes the parenthetical, so
# "(20% of the 44 …)" is a different share and is not paired with N of M.
PCT_RE = re.compile(r"(\d[\d,]*)\s+of\s+(\d[\d,]*)((?:\s+[\w-]+){0,6})\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*[);,]")
PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def recomputed_shares(text: str, totals: set[int]) -> set[str]:
    """Percentages in ``text`` that equal some count in it over another count in
    it or over a review total (``103 resources (32%)`` with 324 resources)."""
    counts = [int(x.replace(",", "")) for x in NUM_RE.findall(text) if "." not in x]
    denoms = set(counts) | totals
    out = set()
    for p in PERCENT_RE.findall(text):
        tol = 0.06 if "." in p else 0.51
        if any(0 < t and n <= t and abs(100 * n / t - float(p)) <= tol
               for n in counts for t in denoms):
            out.add(p)
    return out
TOTAL_RE = re.compile(r"\b(\d[\d,]*)\s+(?:included\s+)?(studies|publications|reports|articles|papers)"
                      r"\s+(?:were\s+)?included\b|\bincluded\s+(\d[\d,]*)\s+(studies|publications|"
                      r"reports|articles|papers)\b", re.I)


# ─────────────────────────── text ────────────────────────────

def norm(s: str) -> str:
    """Case, quotes, whitespace, PDF hyphenation and punctuation folded away."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)
    s = re.sub(r"[‘’“”'\"`´]", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="replace")).hexdigest()


def source_text(a: dict) -> str:
    return "\n".join(x for x in (a.get("title"), a.get("abstract"), a.get("full_text")) if x)


def locate(quote: str, source: str, nsource: Optional[str] = None) -> dict:
    """Where ``quote`` occurs in ``source``: exact / normalized / paraphrase / absent."""
    q = (quote or "").strip().strip("\"'“”‘’")
    if not q:
        return {"match": "absent", "coverage": 0.0}
    i = source.find(q)
    if i >= 0:
        return {"match": "exact", "offset": i, "length": len(q), "coverage": 1.0}
    nq, ns = norm(q), nsource if nsource is not None else norm(source)
    if nq and nq in ns:
        return {"match": "normalized", "normalized_offset": ns.find(nq), "coverage": 1.0}
    # quoted fragments inside a span ("'D2RQ' was used…") must each be present
    frags = [f for f in re.findall(r"[\"“']([^\"”']{12,})[\"”']", q)]
    w = nq.split()
    grams = {" ".join(w[i:i + 6]) for i in range(max(1, len(w) - 5))}
    cov = sum(g in ns for g in grams) / max(1, len(grams))
    if frags and all(norm(f) in ns for f in frags) and cov >= PARAPHRASE:
        return {"match": "paraphrase", "coverage": round(cov, 3), "quoted_fragments_found": True}
    return {"match": "paraphrase" if cov >= PARAPHRASE else "absent", "coverage": round(cov, 3)}


def numbers_in(text: str) -> list[str]:
    """Quantities in ``text``: not years, not section/RQ/table labels."""
    out = []
    for m in NUM_RE.finditer(text):
        raw = m.group(1)
        v = raw.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", v):
            continue
        if LABEL_BEFORE.search(text[max(0, m.start() - 12):m.start()]):
            continue
        if text[m.end():m.end() + 1] == "." and text[m.end() + 1:m.end() + 2].isdigit():
            continue
        # part of a date or an identifier: 2000/01/01, 2026-10-15, NKG-004
        if re.search(r"[/\-–]$", text[max(0, m.start() - 1):m.start()]) or \
                re.match(r"[/\-–]\d", text[m.end():m.end() + 2]):
            continue
        out.append(v)
    return out


def num_in(n: str, text: str) -> bool:
    """``n`` appears as a number in ``text`` (``34,771`` == ``34771``)."""
    return n in {x.replace(",", "") for x in NUM_RE.findall(text or "")}


# ─────────────────────────── 1. evidence spans ────────────────────────────

def audit_spans(review: dict, arts: dict, findings: list, revalidate: dict) -> dict:
    stats: Counter = Counter()
    norm_cache: dict[str, str] = {}
    out_spans = []
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
        rec = {"index": i, "paper": pid, "section": sp.get("section", ""),
               "source_sha256": sha256_text(src), **loc}
        out_spans.append(rec)
        if loc["match"] == "absent":
            findings.append(f_("span-not-in-source", "error", where,
                               f"not found in the paper's stored text: “{sp.get('text', '')[:140]}”"))
        elif loc["match"] == "paraphrase":
            findings.append(f_("span-paraphrased", "warning", where,
                               f"only {loc['coverage']:.0%} of the span is the paper's own wording — "
                               "a paraphrase cannot be quoted as evidence"))
        section = str(sp.get("section") or "").strip().lower()
        if not (a.get("full_text") or "").strip() and section not in ABSTRACT_SECTIONS:
            findings.append(f_("span-reading-basis", "error", where,
                               f"paper was read as abstract only, but the span is attributed to "
                               f"its {section} section"))
        sdoi, adoi = clean_doi(sp.get("doi") or "").lower(), clean_doi(a.get("doi") or "").lower()
        if sdoi and adoi and sdoi != adoi:
            findings.append(f_("span-other-version", "error", where,
                               f"extracted from DOI {sdoi}, but the study is now {adoi} — "
                               "re-extract from the version cited"))
    # 4. version changes recorded by verify_citations --apply
    for pid, ch in revalidate.items():
        a = arts.get(pid)
        if not a:
            continue
        new = clean_doi(ch.get("new_doi") or a.get("doi") or "").lower()
        spans = [s for s in review.get("evidence_spans") or [] if str(s.get("paper_pmid")) == pid]
        fresh = spans and all(clean_doi(s.get("doi") or "").lower() == new and
                              locate(s.get("text", ""), source_text(a))["match"] in ("exact", "normalized")
                              for s in spans)
        if not fresh:
            findings.append(f_("version-changed", "error", pid,
                               f"cited version changed {ch.get('old_doi') or '?'} → {new}; its "
                               "extraction, appraisal and evidence spans predate it. Re-extract "
                               "from the new version (spans must carry its DOI), then re-audit"))
    return {"counts": dict(stats), "spans": out_spans}


# ─────────────────────────── 5. studies vs reports ────────────────────────────

def rubric_for(review: dict, arts: dict) -> dict[str, dict]:
    """Included study → its charting rubric, matched by DOI, then title, then position.

    The rubric carries its own (LLM-extracted) metadata and a ``source_id`` that
    is not the study id, so the link is re-derived rather than trusted.
    """
    rubrics = review.get("data_charting_rubrics") or []
    by_doi = {clean_doi(r.get("doi") or "").lower(): r for r in rubrics if r.get("doi")}
    by_title = {norm(r.get("title") or ""): r for r in rubrics if r.get("title")}
    out = {}
    for i, (pid, a) in enumerate(arts.items()):
        r = by_doi.get(clean_doi(a.get("doi") or "").lower()) or by_title.get(norm(a.get("title") or ""))
        if r is None and len(rubrics) == len(arts):
            r = rubrics[i]
        if r is not None:
            out[pid] = r
    return out


def audit_studies(review: dict, arts: dict, study_map: dict, findings: list) -> dict:
    groups = study_map.get("studies") or []
    grouped: dict[str, str] = {}
    for g in groups:
        for r in g.get("reports") or []:
            if str(r) not in arts:
                findings.append(f_("study-map-unknown-report", "error", g.get("study_id", "?"),
                                   f"report {r} is not an included study"))
            grouped[str(r)] = g.get("study_id", "?")
    distinct = {frozenset(map(str, d.get("reports") or [] if isinstance(d, dict) else d))
                for d in study_map.get("distinct") or [] if isinstance(d, (list, dict))}

    def resolved(members: set[str]) -> bool:
        if len({grouped.get(m) for m in members}) == 1 and all(m in grouped for m in members):
            return True
        return any(members <= d for d in distinct)

    # candidate overlap groups
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
                               f"{len(members)} reports ({', '.join(sorted(members)[:6])}) may describe "
                               "the same participants — group them under one study_id in "
                               "<stem>.studies.json, or list them under \"distinct\" with the reason"))
    n_studies = len({grouped[p] for p in arts if p in grouped}) + sum(1 for p in arts if p not in grouped)
    return {"reports": len(arts), "studies": n_studies, "candidate_groups": len(candidates),
            "unresolved": unresolved}


# ─────────────────────────── 3. numbers ────────────────────────────

def facts(review: dict, ms: dict, study_info: dict) -> set[str]:
    """Every count the structured data supports, as strings."""
    out: set[int] = set()
    flow = review.get("flow") or {}
    for v in flow.values():
        if isinstance(v, int):
            out.add(v)
        elif isinstance(v, dict):
            out.update(x for x in v.values() if isinstance(x, int))
    arts = review.get("included_articles") or []
    out |= {len(arts), study_info.get("studies", 0), len(review.get("evidence_spans") or []),
            sum(1 for a in arts if (a.get("full_text") or "").strip()),
            sum(1 for a in arts if not (a.get("full_text") or "").strip())}
    out |= set(Counter(str(a.get("year") or "") for a in arts).values())
    fields: dict[str, Counter] = {}
    for rub in review.get("data_charting_rubrics") or []:
        for k, v in rub.items():
            if isinstance(v, (str, int)) and str(v).strip():
                fields.setdefault(k, Counter())[str(v).strip().lower()] += 1
    for c in fields.values():
        out |= set(c.values())
        out.add(sum(c.values()))
    cat = ms.get("resource_catalog") or []
    out.add(len(cat))
    cols: dict[str, Counter] = {}
    for row in cat:
        for k, v in row.items():
            if k == "publication_ids":
                continue
            for part in (v if isinstance(v, list) else re.split(r",\s*", str(v))):
                cols.setdefault(k, Counter())[str(part).strip().lower()] += 1
    for c in cols.values():
        out |= set(c.values())
    for q in review.get("search_queries") or []:
        out |= {int(x.replace(",", "")) for x in re.findall(r"\b(\d[\d,]*)\s+records?\b", str(q))}
    log = review.get("screening_log") or []
    out |= set(Counter((e.get("stage"), e.get("decision")) for e in log).values())
    return {str(x) for x in out if isinstance(x, int)}


def audit_numbers(review: dict, text: str, study_info: dict, findings: list) -> dict:
    flow = review.get("flow") or {}
    arts = review.get("included_articles") or []
    n_inc = len(arts)
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
        if g("assessed_eligibility"):
            need(g("assessed_eligibility") - g("excluded_eligibility") == g("included_synthesis"),
                 "flow-arithmetic", f"assessed {g('assessed_eligibility')} − excluded "
                 f"{g('excluded_eligibility')} ≠ included {g('included_synthesis')}")
        if g("total_identified") and g("after_dedup"):
            need(g("total_identified") - g("duplicates_removed") == g("after_dedup"),
                 "flow-arithmetic", f"identified {g('total_identified')} − duplicates "
                 f"{g('duplicates_removed')} ≠ after deduplication {g('after_dedup')}", "warning")
        log = review.get("screening_log") or []
        ft_ex = sum(1 for e in log if str(e.get("stage")).endswith("full_text")
                    and str(e.get("decision")).lower() == "exclude")
        if log and any(str(e.get("stage")).endswith("full_text") for e in log):
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
                               f"says {n} included {unit}; the data has {want}"
                               + (" (studies after grouping reports)" if unit == "studies"
                                  and want != n_inc else "")))
    return {"checks": checks}


# ─────────────────────────── 2. claims ────────────────────────────

ABBREV = re.compile(r"\b(et al|e\.g|i\.e|vs|fig|figs|approx|ca|cf|no|nos|ref|refs|sec|eq|"
                    r"[A-Z])\.", re.I)


def delivered_text(md: str) -> list[tuple[str, str]]:
    """``(heading, paragraph)`` for the prose of a Markdown review: no tables,
    code, headings, reference list or appendices."""
    out, heading, buf, fence = [], "", [], False
    for line in md.splitlines():
        s = line.strip()
        if s.startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if s.startswith("#"):
            if buf:
                out.append((heading, " ".join(buf)))
                buf = []
            heading = s.lstrip("#").strip()
            if re.match(r"(references|bibliography|appendix)", heading, re.I):
                heading = "__stop__"
            continue
        if heading == "__stop__":
            continue
        if not s or s.startswith(("|", "<!--", "![", "<")):
            if buf:
                out.append((heading, " ".join(buf)))
                buf = []
            continue
        buf.append(re.sub(r"^[-*>]\s+|^\d+\.\s+", "", s))
    if buf and heading != "__stop__":
        out.append((heading, " ".join(buf)))
    return out


def sentences(par: str) -> list[str]:
    protected = ABBREV.sub(lambda m: m.group(0).replace(".", "․"), par)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])", protected)
    return [p.replace("․", ".").strip() for p in parts if p.strip()]


def claim_id(text: str) -> str:
    return "C" + hashlib.sha1(norm(text).encode()).hexdigest()[:10]


def cited_studies(text: str, arts: dict, catalog_map: dict) -> list[str]:
    """Included-study keys a sentence cites: PMID, DOI, author–year, resource id."""
    keys: list[str] = []
    by_doi = {clean_doi(a.get("doi") or "").lower(): k for k, a in arts.items() if a.get("doi")}
    for m in PMID_RE.finditer(text):
        keys += [p for p in PMID_ID.findall(m.group(1)) if p in arts]
    for d in DOI_RE.findall(text):
        k = by_doi.get(_trim(d).lower())
        if k:
            keys.append(k)
    before = text
    for m in AUTHOR_YEAR_RE.finditer(before):
        keys += [k for k, a in arts.items()
                 if surname_matches(m.group(1), a.get("authors") or "")
                 and years_close(str(a.get("year") or ""), m.group(2))]
    for rid, studies in catalog_map.items():
        if re.search(rf"\b{re.escape(rid)}\b", text, re.I):
            keys += studies
    return list(dict.fromkeys(keys))


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


def build_claims(md: str, arts: dict, ms: dict) -> list[dict]:
    cmap = catalog_studies(ms, arts)
    claims, seen = [], set()
    for heading, par in delivered_text(md):
        for sent in sentences(par):
            nums = numbers_in(sent)
            cites = cited_studies(sent, arts, cmap)
            has_cite = bool(cites) or bool(PMID_RE.search(sent) or DOI_RE.search(sent))
            if not (nums or has_cite):
                continue
            cid = claim_id(sent)
            if cid in seen:
                continue
            seen.add(cid)
            claims.append({"id": cid, "location": heading, "text": sent, "numbers": nums,
                           "cites": cites, "kind": "study" if cites else "aggregate"})
    return claims


def candidates_for(claim: dict, review: dict, k: int = 4) -> list[dict]:
    """Evidence spans of the cited studies that share the most words with the claim."""
    words = set(norm(claim["text"]).split())
    spans = [s for s in review.get("evidence_spans") or [] if str(s.get("paper_pmid")) in claim["cites"]]
    scored = sorted(spans, key=lambda s: -len(words & set(norm(s.get("text", "")).split())))
    return [{"study": s.get("paper_pmid"), "quote": s.get("text", ""), "section": s.get("section", "")}
            for s in scored[:k]]


def init_claims(review: dict, md_path: Path, ms: dict, ledger_path: Path) -> dict:
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    md = md_path.read_text(encoding="utf-8")
    old = {}
    if ledger_path.is_file():
        try:
            old = {c["id"]: c for c in json.loads(ledger_path.read_text(encoding="utf-8")).get("claims") or []}
        except (OSError, ValueError, KeyError):
            old = {}
    claims = []
    for c in build_claims(md, arts, ms):
        prev = old.get(c["id"])
        if prev:  # same sentence: keep the judgement
            c.update({k: prev[k] for k in ("support", "evidence", "explanation", "judged_by",
                                            "derived_from") if k in prev})
        else:
            c.update({"support": None, "evidence": [], "explanation": "", "judged_by": ""})
            if c["kind"] == "study":
                c["candidates"] = candidates_for(c, review)
            else:
                c["derived_from"] = ""
        claims.append(c)
    ledger = {"manuscript": str(md_path.resolve()), "manuscript_sha256": sha256_text(md),
              "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "instructions": ("For each claim with kind=study set support to one of "
                               f"{', '.join(SUPPORT)}; give evidence as [{{study, quote}}] with quotes "
                               "copied verbatim from the cited paper (candidates are suggestions), a "
                               "one-line explanation, and judged_by. For kind=aggregate whose numbers "
                               "the audit cannot recompute, set derived_from to the data it comes from."),
              "claims": claims}
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return ledger


def audit_claims(review: dict, md: str, md_path: Path, ms: dict, ledger: Optional[dict],
                 known: set[str], findings: list) -> dict:
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}
    current = build_claims(md, arts, ms)
    stats: Counter = Counter()
    if ledger is None:
        findings.append(f_("claims-not-audited", "unresolved", str(md_path.name),
                           f"no claim ledger: run audit_grounding.py --init-claims and judge the "
                           f"{sum(c['kind'] == 'study' for c in current)} study claims"))
        return {"claims": len(current), "judged": 0}
    if ledger.get("manuscript_sha256") != sha256_text(md):
        findings.append(f_("claims-stale", "unresolved", str(md_path.name),
                           "the manuscript changed since the ledger was written: re-run --init-claims "
                           "(judgements of unchanged sentences are kept) and judge the new ones"))
    judged = {c["id"]: c for c in ledger.get("claims") or []}
    totals = {len(arts), len(ms.get("resource_catalog") or [])} - {0}
    src_cache: dict[str, tuple[str, str]] = {}
    for c in current:
        j = judged.get(c["id"])
        where = f"{c['id']} ({c['location'] or 'body'})"
        if c["kind"] == "aggregate":
            shares = recomputed_shares(c["text"], totals)
            missing = [n for n in c["numbers"] if n not in known and n not in shares]
            if not missing:
                stats["aggregate-recomputed"] += 1
                continue
            if j and (j.get("derived_from") or "").strip():
                stats["aggregate-asserted"] += 1
                findings.append(f_("aggregate-asserted", "warning", where,
                                   f"numbers {', '.join(missing)} are not recomputable from the review "
                                   f"data; asserted from “{j['derived_from']}” — check by hand"))
            else:
                stats["aggregate-unsourced"] += 1
                findings.append(f_("aggregate-unsourced", "unresolved", where,
                                   f"numbers {', '.join(missing)} match no count in the review data and "
                                   f"no derived_from is given: “{c['text'][:120]}”"))
            continue
        if not j or j.get("support") not in SUPPORT:
            stats["unjudged"] += 1
            findings.append(f_("claim-unjudged", "unresolved", where, f"“{c['text'][:120]}”"))
            continue
        sup = j["support"]
        stats[sup] += 1
        if sup in ("contradicted", "insufficient_evidence"):
            findings.append(f_(f"claim-{sup.replace('_', '-')}", "error", where,
                               f"{j.get('explanation') or 'no explanation'} — correct or qualify: "
                               f"“{c['text'][:120]}”"))
            continue
        ev = j.get("evidence") or []
        if not ev:
            findings.append(f_("claim-no-evidence", "error", where, f"judged {sup} but gives no quotes"))
            continue
        quotes = []
        for e in ev:
            sid = str(e.get("study") or "")
            if sid not in arts:
                findings.append(f_("evidence-unknown-study", "error", where,
                                   f"evidence names {sid or 'no study'}, not an included study"))
                continue
            if sid not in c["cites"]:
                findings.append(f_("evidence-not-cited", "warning", where,
                                   f"evidence from {sid}, which the sentence does not cite"))
            src, nsrc = src_cache.setdefault(sid, (source_text(arts[sid]), norm(source_text(arts[sid]))))
            loc = locate(e.get("quote", ""), src, nsrc)
            if loc["match"] not in ("exact", "normalized"):
                findings.append(f_("quote-not-in-source", "error", where,
                                   f"quote is not text of {sid}: “{str(e.get('quote'))[:120]}”"))
                continue
            quotes.append(e.get("quote", ""))
        unsourced = [n for n in c["numbers"] if not any(num_in(n, q) for q in quotes)]
        if quotes and unsourced:
            findings.append(f_("number-unsourced", "error", where,
                               f"{', '.join(unsourced)} appear in the claim but in none of its quotes"))
        if sup == "partially_supported":
            findings.append(f_("claim-partial", "warning", where,
                               f"partially supported: {j.get('explanation') or 'no explanation given'}"))
        if not (j.get("judged_by") or "").strip():
            findings.append(f_("claim-judge-unrecorded", "warning", where, "judged_by is empty"))
    stale = set(judged) - {c["id"] for c in current}
    return {"claims": len(current), "study_claims": sum(c["kind"] == "study" for c in current),
            "aggregate_claims": sum(c["kind"] == "aggregate" for c in current),
            "outcomes": dict(stats), "ledger_entries_no_longer_in_text": len(stale)}


# ─────────────────────────── driver ────────────────────────────

def f_(code: str, severity: str, where: str, message: str) -> dict:
    return {"code": code, "severity": severity, "location": where, "message": message}


def snippet(text: str, a: int, b: int, pad: int = 50) -> str:
    return text[max(0, a - pad):b + pad].replace("\n", " ").strip()


def load_json(p: Path, default: Any = None) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default
    except (OSError, ValueError):
        return default


def delivered_markdown(review_json: Path) -> Optional[Path]:
    stem = review_json.stem
    for name in (f"{stem}.manuscript.md", f"{stem}.md"):
        p = review_json.with_name(name)
        if p.is_file():
            return p
    return None


def sidecars(review_json: Path) -> dict[str, Path]:
    stem = review_json.stem
    return {k: review_json.with_name(f"{stem}.{k}.json")
            for k in ("manuscript", "claims", "studies", "revalidation")}


def audit(review_json: Path, md_path: Optional[Path] = None) -> dict:
    review = json.loads(review_json.read_text(encoding="utf-8"))
    side = sidecars(review_json)
    md_path = md_path or delivered_markdown(review_json)
    ms = load_json(side["manuscript"], {}) or {}
    ledger = load_json(side["claims"])
    study_map = load_json(side["studies"], {}) or {}
    revalidate = {str(x.get("pmid")): x for x in (load_json(side["revalidation"], {}) or {}).get("changes") or []}
    arts = {str(a.get("pmid")): a for a in review.get("included_articles") or []}

    findings: list[dict] = []
    span_info = audit_spans(review, arts, findings, revalidate)
    study_info = audit_studies(review, arts, study_map, findings)
    md = md_path.read_text(encoding="utf-8") if md_path else ""
    num_info = audit_numbers(review, md, study_info, findings)
    if md_path:
        claim_info = audit_claims(review, md, md_path, ms, ledger, facts(review, ms, study_info), findings)
    else:
        claim_info = {}
        findings.append(f_("no-delivered-text", "unresolved", review_json.name,
                           "no <stem>.manuscript.md or <stem>.md to audit — export first"))

    errors = sum(f["severity"] == "error" for f in findings)
    unresolved = sum(f["severity"] == "unresolved" for f in findings)
    verdict = "FAIL" if errors else "INCOMPLETE" if unresolved or not arts else "PASS"
    inputs = [review_json] + ([md_path] if md_path else []) + [p for p in side.values() if p.is_file()]
    return {
        "review": str(review_json), "delivered": str(md_path.resolve()) if md_path else None,
        "audited_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": bind_inputs(inputs),
        "summary": {"verdict": verdict, "errors": errors, "unresolved": unresolved,
                    "warnings": sum(f["severity"] == "warning" for f in findings),
                    "by_code": dict(Counter(f["code"] for f in findings).most_common())},
        "evidence_spans": span_info["counts"], "studies": study_info,
        "numbers": num_info, "claims": claim_info,
        "findings": findings, "span_locations": span_info["spans"],
    }


def render(rep: dict) -> str:
    s = rep["summary"]
    out = [f"# Grounding audit — {Path(rep['review']).name}\n",
           f"**Verdict: {s['verdict']}** — {s['errors']} error(s), {s['unresolved']} unresolved, "
           f"{s['warnings']} warning(s). Delivered text: `{Path(rep['delivered']).name if rep['delivered'] else 'none'}`.\n",
           "| Check | Result |", "| --- | --- |",
           f"| Evidence spans | {', '.join(f'{k} {v}' for k, v in sorted(rep['evidence_spans'].items())) or 'none'} |",
           f"| Studies vs reports | {rep['studies']['reports']} reports → {rep['studies']['studies']} studies; "
           f"{len(rep['studies']['unresolved'])} unresolved overlap group(s) |",
           f"| Numbers | {rep['numbers'].get('checks', 0)} checks |",
           f"| Claims | {rep['claims'].get('claims', 0)} substantive ({rep['claims'].get('study_claims', 0)} citing "
           f"studies); {', '.join(f'{k} {v}' for k, v in sorted((rep['claims'].get('outcomes') or {}).items())) or '—'} |",
           ""]
    order = {"error": 0, "unresolved": 1, "warning": 2}
    rows = sorted(rep["findings"], key=lambda f: order.get(f["severity"], 3))
    if rows:
        out += ["| Severity | Code | Where | Finding |", "| --- | --- | --- | --- |"]
        out += [f"| {f['severity']} | `{f['code']}` | {f['location'][:60].replace('|', '/')} | "
                f"{f['message'].replace('|', '/')} |" for f in rows[:500]]
        if len(rows) > 500:
            out.append(f"\n… {len(rows) - 500} more in the JSON report.")
    out.append("\nSemantic judgements in the claim ledger were made by the agent or a person "
               "(`judged_by`); this audit verifies their quotes and numbers, not their reasoning.")
    return "\n".join(out) + "\n"


def write(rep: dict, review_json: Path) -> Path:
    base = review_json.with_name(f"{review_json.stem}.grounding")
    Path(f"{base}.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    Path(f"{base}.md").write_text(render(rep), encoding="utf-8")
    return base


def is_current(review_json: Path) -> Optional[dict]:
    """The grounding report if it exists and every input it bound is unchanged."""
    rep = load_json(review_json.with_name(f"{review_json.stem}.grounding.json"))
    if not rep or stale_inputs(rep):
        return None
    # a sidecar created after the audit is an input it never saw
    seen = set(rep.get("inputs") or {})
    md = delivered_markdown(review_json)
    news = [p for p in list(sidecars(review_json).values()) + ([md] if md else [])
            if p.is_file() and str(p.resolve()) not in seen]
    return None if news else rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("review", help="review JSON (PRISMAReviewResult)")
    ap.add_argument("--markdown", help="delivered Markdown (default: <stem>.manuscript.md, else <stem>.md)")
    ap.add_argument("--init-claims", action="store_true",
                    help="write / refresh <stem>.claims.json from the delivered Markdown, keeping "
                         "judgements of unchanged sentences, then audit")
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
        led = init_claims(review, md, ms, sidecars(src)["claims"])
        todo = sum(1 for c in led["claims"] if c["kind"] == "study" and c.get("support") not in SUPPORT)
        print(f"claims: {len(led['claims'])} substantive sentence(s) → {sidecars(src)['claims']} "
              f"({todo} study claim(s) to judge)", file=sys.stderr)
    rep = audit(src, md)
    base = write(rep, src)
    s = rep["summary"]
    print(f"Grounding: {s['verdict']} — {s['errors']} error(s), {s['unresolved']} unresolved, "
          f"{s['warnings']} warning(s)", file=sys.stderr)
    for code, n in s["by_code"].items():
        print(f"  {code}: {n}", file=sys.stderr)
    print(f"report: {base}.md  ({base}.json)", file=sys.stderr)
    return EXIT[s["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
