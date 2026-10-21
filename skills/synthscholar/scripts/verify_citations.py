#!/usr/bin/env python3
"""Verify the citations in a generated review against the registries.

A DOI that resolves is not proof a citation is right; the failure that matters
in a generated review is a *real* DOI or PMID attached to the *wrong* paper, a
study cited in the prose that the review never included, or a retracted paper
carried into the synthesis. Printing registry metadata and leaving the
comparison to a reader does not catch those at 170 studies, so this script
makes the comparison itself and grades every finding.

Two layers, run over a ``PRISMAReviewResult`` JSON (``review.json``):

1. **Reference metadata** — each included article's identifiers against the
   registry that owns them:

   * DOI → Crossref REST (``/works/{doi}``); not in Crossref → the DOI handle
     server, then CSL-JSON content negotiation for DataCite / mEDRA / … records.
     The registered title, first author and year are compared with what the
     review claims; a title that does not match means the DOI belongs to a
     different paper.
   * Retractions are looked up from the **paper's** side — Crossref's
     ``updated-by`` (Retraction Watch-backed) plus the ``updates:{doi}`` filter,
     which returns the notices pointing at it. ``update-to`` sits on the notice,
     so reading only that misses every cited retracted paper.
   * PMID → NCBI E-utilities ``esummary`` (batched): exists, title matches,
     PubMed's DOI agrees with the claimed one, not a "Retracted Publication".
     BYO-corpus ids (``local_…``) are not PMIDs and are skipped.
   * **Fallback indexes.** When Crossref, the DOI handle server or PubMed
     cannot answer (outage, throttling, a DOI agency with no metadata), the id
     is looked up in **OpenAlex**, then **Semantic Scholar**, and compared the
     same way. They can confirm what a paper is, never that a DOI does not
     exist; an id neither knows stays unverified. OpenAlex carries a retraction
     flag; Semantic Scholar does not, so a record confirmed only there is
     ``retraction-unchecked`` (unverified). DOI-less references are also
     title-searched in both when Crossref has no match. ``OPENALEX_API_KEY`` /
     ``SEMANTIC_SCHOLAR_API_KEY`` raise their rate limits. (Google Scholar has
     no API and forbids automated access, so it is not used.)
   * No DOI → a Crossref bibliographic search proposes a *candidate*, shown only
     when its title matches near-exactly. Candidates are never applied.

2. **In-text citations** — every ``PMID:`` / DOI / ``(Author et al., Year)``
   in the generated prose (synthesis, discussion, per-group answers, GRADE,
   …) must resolve to an included study. A citation of a record the screening
   log excluded, or of an id that appears nowhere in the review, is an error;
   an author–year paired with a PMID that belongs to someone else is a warning.

A rendered Markdown file (``review.md``, or any manuscript) is accepted too;
without the JSON there is no included set, so it verifies the DOIs it finds and
matches author–year citations against the reference list.

Severity: **error** (fails the gate: unregistered DOI, DOI → different paper,
retracted, cited study not included / unknown id), **warning** (partial title,
author or year disagreement, expression of concern, missing DOI, lookup
failed), **info** (correction notices, ±1 year, preprint with a published
version, included-but-never-cited).

Usage:
    python verify_citations.py out/review.json
    python verify_citations.py out/review.json --style vancouver      # + reference list
    python verify_citations.py out/review.json --apply out/review.verified.json
    python verify_citations.py out/review.json --offline              # in-text only
    python verify_citations.py manuscript.md --check-urls

A manuscript (``review.manuscript.md``) with its ``review.json`` beside it, or
named with ``--review``, is checked in **manuscript mode**: the included studies
as above, every other reference-list entry verified as background literature,
and every citation in the manuscript's own prose — the Discussion, prior-review
comparison and figure interpretations exist only there — matched to one or the
other.

Every report ends in one verdict: **PASS** (everything checked, no errors),
**FAIL** (errors), or **INCOMPLETE** (no errors, but something could not be
checked: ``--offline``, a registry lookup that failed, a reference with no DOI
or PMID). It also records the SHA-256 of every input it vouches for, so a gate
(``generate_review_pdf.py``) refuses a report that no longer describes what is
delivered.

Writes ``<base>.citations.json`` and ``<base>.citations.md`` next to the input
(``--outdir`` to change). Exit 0 = PASS, 1 = FAIL (or warnings with
``--strict``), 2 = bad input, 3 = INCOMPLETE.

``--apply`` writes a corrected copy, changing only what a registry confirms:
DOI spelling normalised, empty title/year/journal/authors filled from a record
whose title matched, and a missing DOI taken from PubMed for a verified PMID.
Re-export from the corrected copy; a newly filled DOI changes the article's RDF
IRI (PMID → DOI → hash), so re-ingest rather than patch a graph already loaded.

Contact address for the Crossref polite pool / NCBI: ``--mailto`` or
``SYNTHSCHOLAR_EMAIL`` (also ``NCBI_EMAIL``); ``NCBI_API_KEY`` lifts the
E-utilities rate limit. Needs only ``httpx``.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

import httpx

CROSSREF = "https://api.crossref.org/works"
HANDLE = "https://doi.org/api/handles/"
DOI_RESOLVER = "https://doi.org/"
ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
# Fallback registries: indexes, not DOI agencies. They confirm what a paper is
# when Crossref or PubMed cannot answer, but never that a DOI is *unregistered*
# (only the DOI handle server can say that).
OPENALEX = "https://api.openalex.org/works"
SEMANTIC_SCHOLAR = "https://api.semanticscholar.org/graph/v1/paper"
S2_FIELDS = "title,year,authors,venue,journal,externalIds,publicationTypes"
WAYBACK = "https://archive.org/wayback/available"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
# A page that answers 200 but says it is not there.
SOFT_404 = re.compile(r"\b(page|file|document|resource|item|article)\s+(was\s+|is\s+)?not\s+found\b|"
                      r"\b404\b|doesn[’']?t exist|does not exist|no longer (available|exists)|"
                      r"has been (re)?moved|cannot be found|could not be found", re.I)
MAX_PAGE_BYTES = 6_000_000

# DOI = "10." + registrant + "/" + suffix. The suffix may legally contain
# ".", ";", "(" and ")" (Elsevier's 10.1016/S0140-6736(09)60937-1), so
# sentence punctuation is stripped afterwards by _trim(), not excluded here.
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")
DOI_FORM = re.compile(r"^10\.\d{4,9}/\S+$")
URL_RE = re.compile(r"https?://[^\s<>)\]\"'|]+")
# "PMID: 123", "PMID:123", "PMIDs: 1, 2", "PMID local_ab12cd34"
# A list continues only with PMID-shaped ids (``local_…`` or 5+ digits), so a
# year or count after a comma — "(PMID: 31234567, 2019 cohort)" — is not taken
# for a second PMID. PMIDs under 10000 date from the 1970s; list them each with
# their own "PMID:" if a review really cites one.
PMID_RE = re.compile(r"\bPMIDs?\s*[:#]?\s*((?:local_[0-9a-f]+|\d{1,9})\b"
                     r"(?:\s*(?:,|and|&)\s*(?:local_[0-9a-f]+|\d{5,9})\b)*)",
                     re.IGNORECASE)
PMID_ID = re.compile(r"local_[0-9a-f]+|\d{1,9}", re.IGNORECASE)
REAL_PMID = re.compile(r"^\d{1,9}$")
# "Smith et al., 2023", "Smith and Jones 2021", "Smith & Jones, 2021a", "O'Neil-Ray 2020"
SURNAME = r"[A-Z][\w'’\-]+(?:\s(?:van|von|de|der|da|del|la|le)\s[A-Z][\w'’\-]+)?"
AUTHOR_YEAR_RE = re.compile(
    rf"({SURNAME})(?:\s+et\s+al\.?|\s+(?:and|&)\s+{SURNAME})?,?\s+((?:19|20)\d\d)[a-z]?\b"
)
PAREN_RE = re.compile(r"\(([^()]{4,400})\)")
# Capitalised words that precede a year in prose without being a surname.
NOT_SURNAMES = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "since", "from", "in", "by",
    "before", "after", "until", "during", "through", "between", "version", "wave",
    "phase", "table", "figure", "fig", "supplementary", "prisma", "grade", "rob",
    "cohort", "spring", "summer", "autumn", "fall", "winter", "the", "and", "or",
    "year", "years", "published", "accessed", "updated", "covid", "sars", "who",
}
TRAILING = ".,;:'\""

# Matching thresholds on normalised titles.
TITLE_MATCH = 0.90
TITLE_PARTIAL = 0.70

# Crossref update types, graded. Anything unlisted is informational.
UPDATE_ERROR = {"retraction", "withdrawal", "removal"}
UPDATE_WARNING = {"partial_retraction", "expression_of_concern"}

# Top-level review fields that are inputs, logs or run metadata, not generated
# prose; ids in them are not citations.
NON_PROSE = {
    "included_articles", "screening_log", "protocol", "search_queries", "flow",
    "run_configuration", "plan_iterations", "agent_invocations",
    "search_iterations", "cache_matched_criteria", "quality_checklist",
    "timestamp", "grounding_validation", "cache_hit", "cache_similarity_score",
}
STRUCTURED_ID_KEYS = {"paper_pmid", "pmid"}  # plus any key ending in "_pmids"
# Fields holding text quoted verbatim from the source papers. Citations inside
# a quote belong to the quoted paper's own reference list, so only the
# structured id fields under these are checked.
QUOTED = {"evidence_spans"}

SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
# Findings that mean "this reference was not checked", whatever their severity.
UNVERIFIED_CODES = {"lookup-failed", "doi-missing", "retraction-unchecked",
                    "url-unreachable", "url-unchecked", "url-content-unchecked",
                    "reference-unidentified"}
# Exit codes: the verdict, so a caller can tell a failure from an unfinished check.
EXIT = {"PASS": 0, "FAIL": 1, "INCOMPLETE": 3}
# Preprint servers, by DOI prefix and by venue name. A preprint is cited only
# when no peer-reviewed version exists; see preferred_versions().
PREPRINT_DOI_PREFIXES = (
    "10.1101/",         # bioRxiv, medRxiv
    "10.48550/",        # arXiv
    "10.21203/",        # Research Square
    "10.20944/",        # Preprints.org
    "10.31234/",        # PsyArXiv
    "10.31219/",        # OSF Preprints
    "10.31235/",        # SocArXiv
    "10.26434/",        # ChemRxiv
    "10.36227/",        # TechRxiv
    "10.22541/",        # Authorea
    "10.2139/ssrn",     # SSRN
    "10.1590/scielopreprints",
)
PREPRINT_VENUES = re.compile(
    r"\b(bio|med|chem|tech|psy|soc|edu|meta)?rxiv\b|\barxiv\b|research square|\bssrn\b|"
    r"preprints\.org|\bosf preprints\b|authorea", re.I)


def is_preprint(doi: str, meta: Optional[dict] = None) -> bool:
    """A preprint by registry type, DOI prefix or venue name."""
    m = meta or {}
    if m.get("type") == "posted-content" or m.get("subtype") == "preprint":
        return True
    if any(clean_doi(doi).lower().startswith(p) for p in PREPRINT_DOI_PREFIXES):
        return True
    return bool(PREPRINT_VENUES.search(m.get("journal") or ""))


# Reference-list styles render_reference() knows. generate_review_pdf.py takes
# whatever <stem>.references.<style>.md this wrote, so this is the one list.
STYLES = ("apa", "nature", "vancouver", "ieee")


# ─────────────────────────── normalisation ────────────────────────────

def _trim(doi: str) -> str:
    """Strip sentence punctuation and unbalanced closing parens from a DOI match."""
    doi = doi.rstrip(TRAILING)
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1].rstrip(TRAILING)
    return doi


def clean_doi(doi: str) -> str:
    """Bare DOI from any of the URL / ``doi:`` forms, without trailing junk."""
    d = (doi or "").strip()
    d = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", d, flags=re.IGNORECASE)
    return _trim(d.strip())


def norm_text(s: str) -> str:
    """Lower-case, accent-folded, tag- and punctuation-free text for comparison."""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def title_similarity(a: str, b: str) -> float:
    """Similarity of two titles in [0, 1]; a truncated title or one missing its
    subtitle still scores as a match."""
    na, nb = norm_text(a), norm_text(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 30 and long_.startswith(short):
        return 0.95
    return SequenceMatcher(None, na, nb).ratio()


def title_in_line(title: str, line: str) -> float:
    """Share of a registry title's words present in a free-text reference line."""
    words = norm_text(title).split()
    have = set(norm_text(line).split())
    return sum(w in have for w in words) / len(words) if words else 0.0


def first_year(s: str) -> Optional[int]:
    m = re.search(r"(?:19|20)\d\d", s or "")
    return int(m.group()) if m else None


def years_close(claimed: str, cited: str, tol: int = 1) -> bool:
    y = first_year(claimed)
    return y is not None and abs(y - int(cited)) <= tol


def first_author_segment(authors: str) -> str:
    """The first author out of a free-text author string.

    Handles "Smith J, Jones K" (PubMed), "Smith, J.; Jones, K.", "Smith, J., ..."
    and "John Smith, Jane Doe": the surname is somewhere in the returned words.
    """
    a = (authors or "").strip()
    if ";" in a:
        return a.split(";")[0]
    return a.split(",")[0]


def surname_matches(surname: str, authors: str) -> bool:
    target = norm_text(surname)
    seg = norm_text(first_author_segment(authors))
    return bool(target) and (f" {target} " in f" {seg} " or seg.replace(" ", "") == target.replace(" ", ""))


# ─────────────────────────── HTTP ────────────────────────────

class Registry:
    """Thin client for Crossref, the DOI handle server, DOI content negotiation
    and NCBI E-utilities, with retry on throttling."""

    def __init__(self, mailto: str = "", delay: float = 0.2, timeout: float = 20.0):
        self.mailto = mailto
        self.delay = delay
        ua = "synthscholar-skill-verify-citations/1.0"
        if mailto:
            ua += f" (mailto:{mailto})"
        self.http = httpx.Client(timeout=timeout, follow_redirects=True,
                                 headers={"User-Agent": ua})
        self.ncbi_key = (os.environ.get("NCBI_API_KEY") or "").strip()
        self.openalex_key = (os.environ.get("OPENALEX_API_KEY") or "").strip()
        self.s2_key = (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or "").strip()

    def close(self) -> None:
        self.http.close()

    def _get(self, url: str, params: Optional[dict] = None,
             headers: Optional[dict] = None, tries: int = 3) -> Optional[httpx.Response]:
        """GET with backoff on 429/5xx; None means the transport failed."""
        for attempt in range(tries):
            try:
                r = self.http.get(url, params=params, headers=headers)
            except httpx.HTTPError:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(_retry_after(r.headers.get("Retry-After"), 2 ** attempt))
                continue
            return r
        return None

    def _crossref_params(self, extra: Optional[dict] = None) -> dict:
        p = {"mailto": self.mailto} if self.mailto else {}
        p.update(extra or {})
        return p

    # ---- DOI ----

    def lookup_doi(self, doi: str, check_updates: bool = True) -> tuple[str, dict]:
        """Resolve one DOI → (status, metadata).

        status: ``crossref`` | ``other-registry`` | ``not-found`` | ``error``.
        """
        r = self._get(f"{CROSSREF}/{quote(doi, safe='')}", self._crossref_params())
        time.sleep(self.delay)
        if r is None or (r.status_code != 200 and r.status_code != 404):
            why = "Crossref unreachable" if r is None else f"Crossref HTTP {r.status_code}"
            return self._fallback("doi", doi, why)
        if r.status_code == 200:
            body = _json(r)
            if body is None:
                return "error", {"detail": "Crossref returned a non-JSON response"}
            meta = parse_crossref(body.get("message") or {})
            if check_updates:
                notices = self._notices_for(doi)
                if notices is None:      # the notice query failed: retractions unknown
                    meta["retraction_checked"] = False
                else:
                    meta["updates"] = _merge_updates(meta["updates"], notices)
            return "crossref", meta
        # Not in Crossref: wrong DOI, or a DataCite / mEDRA / JaLC registration.
        h = self._get(HANDLE + quote(doi, safe="/"))
        if h is None:
            return self._fallback("doi", doi, "doi.org handle server unreachable")
        if h.status_code != 200:
            return "not-found", {}
        c = self._get(DOI_RESOLVER + quote(doi, safe="/"),
                      headers={"Accept": "application/vnd.citationstyles.csl+json"})
        csl = _json(c) if c is not None and c.status_code == 200 else None
        if csl is not None:
            return "other-registry", parse_csl(csl)
        # registered, but the agency serves no metadata: an index may know the paper
        status, meta = self._fallback("doi", doi, "no CSL metadata")
        return ("other-registry", meta) if status != "error" else ("other-registry", {"doi": doi})

    # ---- fallback indexes: OpenAlex, then Semantic Scholar ----

    def openalex(self, kind: str, value: str) -> tuple[str, dict]:
        """``kind`` is ``doi`` or ``pmid``. → (``found``|``not-found``|``error``, record)."""
        params = {"mailto": self.mailto} if self.mailto else {}
        if self.openalex_key:
            params["api_key"] = self.openalex_key
        r = self._get(f"{OPENALEX}/{kind}:{quote(value, safe='/')}", params)
        time.sleep(self.delay)
        if r is None or r.status_code not in (200, 404):
            return "error", {}
        body = _json(r) if r.status_code == 200 else None
        return ("found", parse_openalex(body)) if body else ("not-found", {})

    def semantic_scholar(self, kind: str, value: str) -> tuple[str, dict]:
        headers = {"x-api-key": self.s2_key} if self.s2_key else None
        r = self._get(f"{SEMANTIC_SCHOLAR}/{kind.upper()}:{quote(value, safe='/')}",
                      {"fields": S2_FIELDS}, headers=headers)
        time.sleep(self.delay if self.s2_key else 1.0)   # 1 req/s without a key
        if r is None or r.status_code not in (200, 404):
            return "error", {}
        body = _json(r) if r.status_code == 200 else None
        return ("found", parse_semantic_scholar(body)) if body else ("not-found", {})

    def _fallback(self, kind: str, value: str, why: str) -> tuple[str, dict]:
        """Crossref/PubMed could not answer: ask OpenAlex, then Semantic Scholar.

        A record found there is a real answer (status ``openalex`` /
        ``semanticscholar``); neither index knowing the id is *not* proof it is
        unregistered, so that stays an ``error`` (lookup failed → INCOMPLETE).
        """
        for name, fn in (("openalex", self.openalex), ("semanticscholar", self.semantic_scholar)):
            status, meta = fn(kind, value)
            if status == "found":
                meta["fallback_reason"] = why
                return name, meta
        return "error", {"detail": f"{why}; not found in OpenAlex or Semantic Scholar either"}

    def lookup_pmid_fallback(self, pmid: str) -> Optional[dict]:
        """PubMed could not answer for ``pmid``: a PubMed-shaped record from an index."""
        status, meta = self._fallback("pmid", pmid, "PubMed lookup failed")
        if status == "error" or (meta.get("pmid") and meta["pmid"] != pmid):
            return None
        return as_pubmed_record(meta)

    def _notices_for(self, doi: str) -> Optional[list[dict]]:
        """Editorial notices (retraction, correction, …) that point at ``doi``; None if the query failed."""
        r = self._get(CROSSREF, self._crossref_params(
            {"filter": f"updates:{doi}", "rows": 20,
             "select": "DOI,type,update-to,title"}))
        time.sleep(self.delay)
        body = _json(r) if r is not None and r.status_code == 200 else None
        if body is None:
            return None
        out = []
        for item in (body.get("message") or {}).get("items") or []:
            for u in item.get("update-to") or []:
                if clean_doi(u.get("DOI", "")).lower() == doi.lower():
                    out.append({"type": (u.get("type") or "").lower(),
                                "notice_doi": item.get("DOI", ""),
                                "date": _date(u.get("updated")),
                                "source": "crossref-notice"})
        return out

    def search_bibliographic(self, title: str, authors: str, year: str) -> list[dict]:
        """Top Crossref matches for a citation that has no DOI."""
        q = " ".join(x for x in (title, first_author_segment(authors), year) if x)
        if len(norm_text(title)) < 5:
            return []
        r = self._get(CROSSREF, self._crossref_params(
            {"query.bibliographic": q, "rows": 3,
             "select": "DOI,title,author,issued,container-title,type"}))
        time.sleep(self.delay)
        body = _json(r) if r is not None and r.status_code == 200 else None
        if body is None:
            return []
        records = [parse_crossref(item) for item in (body.get("message") or {}).get("items") or []]
        if not any(title_similarity(title, m["title"]) >= TITLE_MATCH for m in records):
            records += self._index_title_search(title)
        cands, seen = [], set()
        for m in records:
            if not m.get("doi") or m["doi"].lower() in seen:
                continue
            seen.add(m["doi"].lower())
            fam = m["authors"][0][0] if m["authors"] else ""
            cands.append({"doi": m["doi"], "title": m["title"], "year": m["year"],
                          "first_author": fam, "source": m.get("registry", "crossref"),
                          "author_match": bool(fam) and surname_matches(fam, authors),
                          "title_similarity": round(title_similarity(title, m["title"]), 3)})
        return sorted(cands, key=lambda c: -c["title_similarity"])

    def _index_title_search(self, title: str) -> list[dict]:
        """OpenAlex and Semantic Scholar title matches, for papers Crossref misses."""
        out = []
        params = {"search": title, "per-page": 3}
        if self.mailto:
            params["mailto"] = self.mailto
        r = self._get(OPENALEX, params)
        time.sleep(self.delay)
        body = _json(r) if r is not None and r.status_code == 200 else None
        out += [parse_openalex(w) for w in (body or {}).get("results") or []]
        headers = {"x-api-key": self.s2_key} if self.s2_key else None
        r = self._get(f"{SEMANTIC_SCHOLAR}/search/match", {"query": title, "fields": S2_FIELDS},
                      headers=headers)
        time.sleep(self.delay if self.s2_key else 1.0)
        body = _json(r) if r is not None and r.status_code == 200 else None
        out += [parse_semantic_scholar(p) for p in (body or {}).get("data") or []]
        return out

    def find_published_version(self, title: str, authors: str, year: str) -> list[dict]:
        """Peer-reviewed records matching a preprint by title, first author and year.

        Candidates only: without a registry link between the two records, a
        matching title is evidence, not proof, that they are the same work.
        """
        if len(norm_text(title)) < 5:
            return []
        q = " ".join(x for x in (title, first_author_segment(authors)) if x)
        r = self._get(CROSSREF, self._crossref_params(
            {"query.bibliographic": q, "rows": 5, "filter": "type:journal-article",
             "select": "DOI,title,author,issued,container-title,type"}))
        time.sleep(self.delay)
        body = _json(r) if r is not None and r.status_code == 200 else None
        if body is None:
            return []
        py = first_year(year)
        out = []
        for item in (body.get("message") or {}).get("items") or []:
            m = parse_crossref(item)
            fam = m["authors"][0][0] if m["authors"] else ""
            if (title_similarity(title, m["title"]) >= TITLE_MATCH and fam
                    and surname_matches(fam, authors) and not is_preprint(m["doi"], m)
                    and (not py or not m["year"] or m["year"] >= py)):
                out.append({"doi": m["doi"], "title": m["title"], "year": m["year"],
                            "journal": m["journal"]})
        return out

    def published_versions(self, doi: str, meta: dict) -> list[str]:
        """Registry-linked peer-reviewed versions of preprint ``doi``.

        The link is usually asserted by the *publisher*: the journal article
        carries ``has-preprint`` → the preprint, while the preprint's own record
        is left empty. So both directions are read: the preprint's
        ``is-preprint-of``, and any work whose ``has-preprint`` names it.
        """
        found = [clean_doi(d) for d in meta.get("published_as") or [] if d]
        r = self._get(CROSSREF, self._crossref_params(
            {"filter": f"relation.type:has-preprint,relation.object:{clean_doi(doi)}",
             "rows": 5, "select": "DOI,type"}))
        time.sleep(self.delay)
        body = _json(r) if r is not None and r.status_code == 200 else None
        for item in ((body or {}).get("message") or {}).get("items") or []:
            d = clean_doi(item.get("DOI", ""))
            if d and item.get("type") != "posted-content" and d.lower() not in {x.lower() for x in found}:
                found.append(d)
        return found

    def fetch_plan(self, doi: str) -> dict:
        """What to cite and what to try reading for ``doi``, best first.

        ``cite`` is the DOI the review cites: the peer-reviewed version when
        one exists. ``tries`` is ``[(doi, version)]`` in reading order: the
        published version(s) first, then the preprint. That holds whichever
        of the two the user supplied, because a registry-linked preprint is a
        legitimate fallback copy when the published text is unreachable.
        ``title`` / ``authors`` describe the cited work, for the
        deposited-copy search.
        """
        doi = clean_doi(doi)
        status, meta = self.lookup_doi(doi, check_updates=False)
        if status not in ("crossref", "other-registry"):
            return {"cite": doi, "tries": [(doi, "as-given")], "title": "", "authors": []}
        if is_preprint(doi, meta):
            pubs = self.published_versions(doi, meta)
            tries = [(d, "published") for d in pubs] + [(doi, "preprint")]
            cite = pubs[0] if pubs else doi
        else:
            pres = [clean_doi(d) for d in meta.get("has_preprint") or [] if d]
            tries = [(doi, "as-given")] + [(d, "preprint") for d in pres]
            cite = doi
        return {"cite": cite, "tries": tries, "title": meta.get("title", ""),
                "authors": meta.get("authors") or []}

    def preferred_versions(self, doi: str) -> list[tuple[str, str]]:
        """``fetch_plan(doi)["tries"]``: DOIs to read, best first."""
        return self.fetch_plan(doi)["tries"]

    def pmid_for_doi(self, doi: str) -> Optional[str]:
        """The PubMed ID registered for ``doi``, if PubMed has exactly one."""
        params = {"db": "pubmed", "term": f"{clean_doi(doi)}[doi]", "retmode": "json",
                  "tool": "synthscholar-skill"}
        if self.mailto:
            params["email"] = self.mailto
        if self.ncbi_key:
            params["api_key"] = self.ncbi_key
        r = self._get(ESEARCH, params)
        time.sleep(0.12 if self.ncbi_key else 0.4)
        body = _json(r) if r is not None and r.status_code == 200 else None
        ids = ((body or {}).get("esearchresult") or {}).get("idlist") or []
        return ids[0] if len(ids) == 1 else None

    # ---- PubMed ----

    def lookup_pmids(self, pmids: list[str]) -> dict[str, Optional[dict]]:
        """PMID → summary (None = not in PubMed); missing keys = lookup failed."""
        out: dict[str, Optional[dict]] = {}
        for i in range(0, len(pmids), 200):
            batch = pmids[i:i + 200]
            params = {"db": "pubmed", "id": ",".join(batch), "retmode": "json",
                      "tool": "synthscholar-skill"}
            if self.mailto:
                params["email"] = self.mailto
            if self.ncbi_key:
                params["api_key"] = self.ncbi_key
            r = self._get(ESUMMARY, params)
            time.sleep(0.12 if self.ncbi_key else 0.4)
            body = _json(r) if r is not None and r.status_code == 200 else None
            if body is None:
                continue  # these PMIDs stay absent → reported as lookup-failed
            res = body.get("result") or {}
            for pmid in batch:
                rec = res.get(pmid)
                out[pmid] = None if (not rec or rec.get("error")) else parse_pubmed(rec)
        return out

    # ---- URLs ----

    def fetch_page(self, url: str) -> dict:
        """GET ``url`` (redirects followed): status, final URL, and the work's
        title and opening text — from HTML metadata, or a PDF's metadata and
        first pages — plus whether it is a "not found" page served as 200."""
        try:
            with self.http.stream("GET", url, headers={
                    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5"}) as r:
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_PAGE_BYTES:
                        break
                status, final, ctype = r.status_code, str(r.url), r.headers.get("content-type", "").lower()
        except httpx.HTTPError as e:
            return {"url": url, "status": 0, "error": f"{type(e).__name__}: {str(e)[:100]}"}
        rec = {"url": url, "status": status, "final_url": final, "content_type": ctype}
        if status >= 400:
            return rec
        if "pdf" in ctype or body[:5] == b"%PDF-":
            rec.update(kind="pdf", **_pdf_title_text(body))
        else:
            doc = body.decode("utf-8", errors="replace")
            rec.update(kind="html", title=_html_title(doc), text=_html_text(doc)[:20000])
        head = f"{rec.get('title') or ''} {(rec.get('text') or '')[:300]}"
        rec["soft_404"] = bool(SOFT_404.search(head))
        return rec

    def wayback(self, url: str) -> Optional[str]:
        """The latest archived capture of ``url`` that answered 200, if any.

        The CDX index is asked for successful captures only: a site that blocks
        crawlers has recent captures of its 403 page, which replay as 403.
        """
        r = self._get(WAYBACK_CDX, {"url": url, "output": "json", "filter": "statuscode:200",
                                    "limit": "-1", "fl": "timestamp"}, tries=2)
        time.sleep(self.delay)
        rows = _json_list(r) if r is not None and r.status_code == 200 else None
        if rows is not None:
            return f"https://web.archive.org/web/{rows[-1][0]}/{url}" if len(rows) > 1 else None
        r = self._get(WAYBACK, {"url": url}, tries=1)
        time.sleep(self.delay)
        body = _json(r) if r is not None and r.status_code == 200 else None
        if body is None:
            # availability API throttled or down: the snapshot address redirects to
            # the closest capture itself, and answers 404 when there is none
            return f"https://web.archive.org/web/2/{url}"
        snap = ((body or {}).get("archived_snapshots") or {}).get("closest") or {}
        return snap.get("url") if snap.get("available") and str(snap.get("status", "")).startswith("2") else None

    def check_url(self, url: str) -> tuple[bool, int]:
        """Reachability. Falls back to GET: many servers reject HEAD."""
        try:
            r = self.http.head(url)
            if r.status_code in (403, 405, 501):
                with self.http.stream("GET", url) as s:
                    return s.status_code < 400, s.status_code
        except httpx.HTTPError:
            return False, 0
        return r.status_code < 400, r.status_code


def _json(r: httpx.Response) -> Optional[dict]:
    """Response body as a JSON object, or None for an HTML error page and the like."""
    try:
        body = r.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _retry_after(value: Optional[str], default: float) -> float:
    if not value:
        return default
    try:
        return min(float(value), 60.0)
    except ValueError:
        try:
            return max(0.0, min(parsedate_to_datetime(value).timestamp() - time.time(), 60.0))
        except (TypeError, ValueError):
            return default


def _date(d: Optional[dict]) -> str:
    parts = ((d or {}).get("date-parts") or [[]])[0]
    return "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p)


def _years(msg: dict, keys: Iterable[str]) -> list[int]:
    ys = []
    for k in keys:
        parts = ((msg.get(k) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            ys.append(int(parts[0]))
    return ys


def _merge_updates(*lists: list[dict]) -> list[dict]:
    seen, out = set(), []
    for u in (x for lst in lists for x in lst):
        key = (u.get("type"), (u.get("notice_doi") or "").lower())
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def _first(v: Any) -> str:
    if isinstance(v, list):
        return str(v[0]) if v else ""
    return str(v or "")


def parse_crossref(msg: dict) -> dict:
    years = _years(msg, ("issued", "published", "published-print",
                         "published-online", "posted"))
    if not years:
        years = _years(msg, ("created",))
    rel = msg.get("relation") or {}
    return {
        "registry": "crossref",
        "doi": msg.get("DOI", ""),
        "title": html.unescape(re.sub(r"<[^>]+>", "", _first(msg.get("title")))).strip(),
        "authors": [(a.get("family") or a.get("name") or "", a.get("given") or "")
                    for a in msg.get("author") or []],
        "year": min(years) if years else None,
        "years": sorted(set(years)),
        "journal": _first(msg.get("container-title")),
        "journal_abbrev": _first(msg.get("short-container-title")),
        "volume": msg.get("volume", ""),
        "issue": msg.get("issue", ""),
        "pages": msg.get("page", "") or msg.get("article-number", ""),
        "type": msg.get("type", ""),
        "publisher": msg.get("publisher", ""),
        # This record is itself a notice when it updates something else.
        "notice_for": [{"type": (u.get("type") or "").lower(), "doi": u.get("DOI", "")}
                       for u in msg.get("update-to") or []],
        "updates": [{"type": (u.get("type") or "").lower(),
                     "notice_doi": u.get("DOI", ""), "date": _date(u.get("updated")),
                     "source": u.get("source", "crossref")}
                    for u in msg.get("updated-by") or []],
        "published_as": [r.get("id", "") for r in rel.get("is-preprint-of") or []
                         if (r.get("id-type") or "doi") == "doi"],
        "subtype": msg.get("subtype", ""),
        "has_preprint": [r.get("id", "") for r in rel.get("has-preprint") or []
                         if (r.get("id-type") or "doi") == "doi"],
    }


def parse_csl(csl: dict) -> dict:
    years = _years(csl, ("issued", "published-print", "published-online"))
    return {
        "registry": "other",
        "doi": csl.get("DOI", ""),
        "title": _first(csl.get("title")).strip(),
        "authors": [(a.get("family") or a.get("literal") or "", a.get("given") or "")
                    for a in csl.get("author") or []],
        "year": min(years) if years else None,
        "years": sorted(set(years)),
        "journal": _first(csl.get("container-title")) or csl.get("publisher", ""),
        "journal_abbrev": _first(csl.get("container-title-short")),
        "volume": str(csl.get("volume", "") or ""),
        "issue": str(csl.get("issue", "") or ""),
        "pages": str(csl.get("page", "") or ""),
        "type": csl.get("type", ""),
        "publisher": csl.get("publisher", ""),
        "notice_for": [], "updates": [], "published_as": [],
    }


def _split_name(name: str) -> tuple[str, str]:
    """"Jane A. Smith" → ("Smith", "Jane A.")."""
    parts = (name or "").strip().split()
    return (parts[-1], " ".join(parts[:-1])) if parts else ("", "")


def parse_openalex(w: dict) -> dict:
    """An OpenAlex work in the shape ``check_registry_record`` reads."""
    year = w.get("publication_year")
    src = ((w.get("primary_location") or {}).get("source") or {})
    ids = w.get("ids") or {}
    return {
        "registry": "openalex",
        "doi": clean_doi(w.get("doi") or ""),
        "title": (w.get("title") or w.get("display_name") or "").strip(),
        "authors": [_split_name((a.get("author") or {}).get("display_name", ""))
                    for a in w.get("authorships") or []],
        "year": year, "years": [year] if year else [],
        "journal": src.get("display_name") or "",
        "type": "posted-content" if w.get("type") == "preprint" else (w.get("type") or ""),
        "subtype": "preprint" if w.get("type") == "preprint" else "",
        "pmid": str(ids.get("pmid") or "").rstrip("/").rsplit("/", 1)[-1],
        # OpenAlex carries a retraction flag (Retraction Watch-backed), not notices
        "updates": [{"type": "retraction", "notice_doi": "", "date": "", "source": "openalex"}]
                   if w.get("is_retracted") else [],
        "retraction_checked": True,
        "notice_for": [], "published_as": [], "has_preprint": [],
    }


def parse_semantic_scholar(p: dict) -> dict:
    """A Semantic Scholar paper in the same shape. It has no retraction data."""
    ext = p.get("externalIds") or {}
    year = p.get("year")
    types = p.get("publicationTypes") or []
    return {
        "registry": "semanticscholar",
        "doi": clean_doi(ext.get("DOI") or ""),
        "title": (p.get("title") or "").strip(),
        "authors": [_split_name(a.get("name", "")) for a in p.get("authors") or []],
        "year": year, "years": [year] if year else [],
        "journal": (p.get("journal") or {}).get("name") or p.get("venue") or "",
        "type": "journal-article" if "JournalArticle" in types else "",
        "subtype": "",
        "pmid": str(ext.get("PubMed") or ""),
        "updates": [], "retraction_checked": False,
        "notice_for": [], "published_as": [], "has_preprint": [],
    }


def as_pubmed_record(meta: dict) -> dict:
    """A fallback registry record in ``parse_pubmed``'s shape, for PMID checks."""
    return {"title": meta.get("title", "").rstrip("."), "year": meta.get("year"),
            "first_author": (meta.get("authors") or [("", "")])[0][0],
            "journal": meta.get("journal", ""), "doi": meta.get("doi", ""),
            "retracted": any(u.get("type") == "retraction" for u in meta.get("updates") or []),
            "source": meta.get("registry", ""),
            "retraction_checked": meta.get("retraction_checked", True)}


def parse_pubmed(rec: dict) -> dict:
    doi = next((i.get("value", "") for i in rec.get("articleids", [])
                if i.get("idtype") == "doi"), "")
    return {
        "title": html.unescape(rec.get("title", "")).rstrip("."),
        "year": first_year(rec.get("pubdate", "") or rec.get("sortpubdate", "")),
        "first_author": (rec.get("authors") or [{}])[0].get("name", ""),
        "journal": rec.get("fulljournalname", "") or rec.get("source", ""),
        "doi": doi,
        "retracted": "Retracted Publication" in (rec.get("pubtype") or []),
    }


# ─────────────────────────── findings ────────────────────────────

def finding(code: str, severity: str, message: str, **extra: Any) -> dict:
    return {"code": code, "severity": severity, "message": message, **extra}


def worst(findings: list[dict]) -> str:
    if not findings:
        return "verified"
    top = max(SEVERITY_RANK[f["severity"]] for f in findings)
    return {0: "verified", 1: "warnings", 2: "failed"}[top]


def check_registry_record(claimed: dict, meta: dict, line_mode: bool = False) -> list[dict]:
    """Compare what the review claims with a registry record."""
    out: list[dict] = []
    reg_title = meta.get("title", "")
    if reg_title:
        sim = (title_in_line(reg_title, claimed["title"]) if line_mode
               else title_similarity(claimed["title"], reg_title))
        meta["title_similarity"] = round(sim, 3)
        if not claimed["title"]:
            pass
        elif sim < TITLE_PARTIAL:
            out.append(finding("doi-title-mismatch", "error",
                               f"DOI is registered to a different paper: “{reg_title}”"))
        elif sim < TITLE_MATCH:
            out.append(finding("doi-title-partial", "warning",
                               f"Title only partly matches the registered “{reg_title}” "
                               f"(similarity {sim:.2f})"))
    title_ok = meta.get("title_similarity", 0) >= TITLE_MATCH

    if meta.get("authors") and claimed["authors"] and not line_mode:
        fam = meta["authors"][0][0]
        if fam and not surname_matches(fam, claimed["authors"]):
            out.append(finding("doi-author-mismatch", "warning",
                               f"Registered first author is {fam}, review has "
                               f"“{first_author_segment(claimed['authors']).strip()}”"))

    cy = first_year(claimed["year"])
    if cy and meta.get("years") and not line_mode:
        gap = min(abs(cy - y) for y in meta["years"])
        if gap > 1:
            out.append(finding("doi-year-mismatch", "warning",
                               f"Review says {cy}; registered {'/'.join(map(str, meta['years']))}"))
        elif gap == 1:
            out.append(finding("doi-year-offby1", "info",
                               f"Review says {cy}; registered {'/'.join(map(str, meta['years']))} "
                               "(online vs print date)"))

    for u in meta.get("updates", []):
        t = u.get("type", "")
        sev = "error" if t in UPDATE_ERROR else "warning" if t in UPDATE_WARNING else "info"
        out.append(finding("retracted" if sev == "error" else f"update-{t or 'notice'}", sev,
                           f"{t.replace('_', ' ').capitalize() or 'Update'} notice"
                           + (f" {u['notice_doi']}" if u.get("notice_doi") else "")
                           + (f" ({u['date']})" if u.get("date") else "")))
    for n in meta.get("notice_for", []):
        sev = "error" if n["type"] in UPDATE_ERROR else "warning"
        out.append(finding("cites-notice", sev,
                           f"This DOI is a {n['type'] or 'notice'} for {n['doi']}, not a study"))
    return out


def check_pubmed_record(claimed: dict, pm: dict) -> list[dict]:
    out: list[dict] = []
    sim = title_similarity(claimed["title"], pm["title"]) if claimed["title"] else 1.0
    pm["title_similarity"] = round(sim, 3)
    if sim < TITLE_PARTIAL:
        out.append(finding("pmid-title-mismatch", "error",
                           f"PMID belongs to a different paper: “{pm['title']}”"))
    elif sim < TITLE_MATCH:
        out.append(finding("pmid-title-partial", "warning",
                           f"Title only partly matches PubMed's “{pm['title']}” ({sim:.2f})"))
    cdoi = clean_doi(claimed["doi"]).lower()
    if cdoi and pm["doi"] and cdoi != pm["doi"].lower():
        out.append(finding("pmid-doi-conflict", "error",
                           f"PubMed lists DOI {pm['doi']} for this PMID, review has {claimed['doi']}"))
    if pm["retracted"]:
        out.append(finding("retracted", "error", "PubMed marks this a Retracted Publication"))
    return out


# ─────────────────────────── in-text citations ────────────────────────────

def _is_id_key(key: str) -> bool:
    return key in STRUCTURED_ID_KEYS or key.endswith("_pmids")


def walk_prose(node: Any, path: str = "", quoted: bool = False) -> Iterable[tuple[str, str, Any]]:
    """Yield (path, key, value) for every leaf of the generated review.

    Under a ``QUOTED`` field only the structured id leaves are yielded.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if not path and k in NON_PROSE:
                continue
            yield from walk_prose(v, f"{path}.{k}" if path else k,
                                  quoted or (not path and k in QUOTED))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_prose(v, f"{path}[{i}]", quoted)
    else:
        key = path.rsplit(".", 1)[-1].split("[")[0]
        if not quoted or _is_id_key(key):
            yield path, key, node


def snippet(text: str, start: int, end: int, pad: int = 60) -> str:
    s = text[max(0, start - pad):end + pad].replace("\n", " ")
    return ("…" if start > pad else "") + s.strip() + ("…" if end + pad < len(text) else "")


def extract_citations(text: str) -> list[dict]:
    """PMID, DOI and author–year citations in one piece of prose, with pairing.

    Inside a parenthetical, "(Smith et al., 2023; PMID: 123)" pairs the author–
    year with the PMID that follows it, so a mislabelled pair can be caught.
    """
    cites: list[dict] = []
    for m in PMID_RE.finditer(text):
        for pid in PMID_ID.findall(m.group(1)):
            cites.append({"kind": "pmid", "id": pid.lower(), "pos": m.start(),
                          "snippet": snippet(text, m.start(), m.end())})
    for m in DOI_RE.finditer(text):
        cites.append({"kind": "doi", "id": _trim(m.group()).lower(), "pos": m.start(),
                      "snippet": snippet(text, m.start(), m.end())})
    for m in AUTHOR_YEAR_RE.finditer(text):
        # Only inside a parenthetical or directly followed by one: "Smith (2023)"
        # matches through the narrative form below, plain prose like "In March 2020"
        # is excluded because "March" is not followed by a citation context.
        if not _in_citation_context(text, m) or m.group(1).lower() in NOT_SURNAMES:
            continue
        cites.append({"kind": "author_year", "surname": m.group(1), "year": m.group(2),
                      "pos": m.start(), "snippet": snippet(text, m.start(), m.end())})
    for m in re.finditer(rf"({SURNAME})(?:\s+et\s+al\.?|\s+(?:and|&)\s+{SURNAME})?\s+\(((?:19|20)\d\d)[a-z]?\)", text):
        if m.group(1).lower() in NOT_SURNAMES:
            continue
        cites.append({"kind": "author_year", "surname": m.group(1), "year": m.group(2),
                      "pos": m.start(), "snippet": snippet(text, m.start(), m.end())})

    # Pair author–year with the PMID that follows it in the same parenthetical.
    for pm in PAREN_RE.finditer(text):
        segs = [s.strip() for s in pm.group(1).split(";")]
        last_ay = None
        for seg in segs:
            ay = AUTHOR_YEAR_RE.search(seg)
            pm_m = PMID_RE.search(seg)
            ids = PMID_ID.findall(pm_m.group(1)) if pm_m else []
            if ay and ay.group(1).lower() not in NOT_SURNAMES:
                last_ay = (ay.group(1), ay.group(2))
            if ids and last_ay:
                for pid in ids:
                    cites.append({"kind": "pair", "id": pid.lower(), "surname": last_ay[0],
                                  "year": last_ay[1], "pos": pm.start(),
                                  "snippet": snippet(text, pm.start(), pm.end())})
                last_ay = None
    return cites


def _in_citation_context(text: str, m: re.Match) -> bool:
    """True when an author–year match sits inside parentheses."""
    before = text[:m.start()]
    return before.rfind("(") > before.rfind(")")


# ─────────────────────────── pages ────────────────────────────

def _json_list(r) -> Optional[list]:
    try:
        v = r.json()
    except ValueError:
        return None
    return v if isinstance(v, list) else None


def _html_title(doc: str) -> str:
    """The work's title: citation/Dublin Core/Open Graph metadata, else <title>."""
    for name in ("citation_title", "dc.title", "DC.title", "og:title", "twitter:title"):
        m = re.search(rf'<meta[^>]+(?:name|property)=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']+)', doc, re.I) \
            or re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:name|property)=["\']{re.escape(name)}["\']', doc, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    m = re.search(r"<title[^>]*>(.*?)</title>", doc, re.I | re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


def _html_text(doc: str) -> str:
    doc = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", doc)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", doc))).strip()


def _pdf_title_text(body: bytes) -> dict:
    """A PDF's metadata title and the text of its first two pages (PyMuPDF)."""
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore
        with fitz.open(stream=body, filetype="pdf") as doc:
            text = " ".join(doc[i].get_text("text") for i in range(min(2, len(doc))))
            return {"title": (doc.metadata or {}).get("title", "") or "", "text": re.sub(r"\s+", " ", text)[:20000]}
    except Exception:
        return {"title": "", "text": "", "pdf_unreadable": True}


def _distinctive_words(line: str) -> list[str]:
    line = URL_RE.sub(" ", line)
    return [w for w in norm_text(line).split() if len(w) >= 5 and not w.isdigit()][:40]


def is_doi_url(u: str) -> bool:
    return bool(re.match(r"https?://(dx\.)?doi\.org/", u, re.I))


# ─────────────────────────── driver ────────────────────────────

class Verifier:
    def __init__(self, registry: Optional[Registry], check_updates: bool = True,
                 search_missing: bool = True):
        self.reg = registry
        self.check_updates = check_updates
        self.search_missing = search_missing

    # ---- review JSON ----

    def verify_review(self, review: dict) -> dict:
        report = self.verify_references(review.get("included_articles") or [])
        refs = [r["claimed"] for r in report["references"]]
        self._verify_in_text(review, refs, report)
        report["grounding_validation"] = _grounding_summary(review.get("grounding_validation"))
        return report

    def verify_references(self, arts: list[dict], mode: str = "review") -> dict:
        """Layer 1 only: each article's identifiers against the registries.

        Also the corpus gate for supplied PDFs (``build_corpus.py --check``,
        ``run_local_review.py``): run before the review, a guessed DOI that
        belongs to another paper is caught before anything is written around it.
        """
        refs = [self._claimed(a) for a in arts]
        report: dict = {"mode": mode, "references": [], "in_text": [],
                        "review_findings": [], "urls": {}}

        # Duplicates across included articles.
        seen: dict[str, str] = {}
        for c in refs:
            d = clean_doi(c["doi"]).lower()
            if d and d in seen:
                report["review_findings"].append(finding(
                    "duplicate-doi", "warning",
                    f"{c['key']} and {seen[d]} share DOI {d} — the same study included twice?"))
            elif d:
                seen[d] = c["key"]

        pubmed = {}
        if self.reg:
            real = [c["pmid"] for c in refs if REAL_PMID.match(c["pmid"])]
            if real:
                print(f"PubMed: {len(real)} PMID(s)…", file=sys.stderr)
                pubmed = self.reg.lookup_pmids(real)

        for n, c in enumerate(refs, 1):
            rec = self._verify_reference(c, pubmed)
            report["references"].append(rec)
            print(f"[{n:>3}/{len(refs)}] {rec['status']:>9}  {c['key']}"
                  + (f"  {rec['doi']}" if rec["doi"] else ""), file=sys.stderr)

        self._flag_duplicate_versions(report)
        return report

    @staticmethod
    def _flag_duplicate_versions(report: dict) -> None:
        """Two included entries that resolve to one work (a preprint and its
        published version): one study counted twice. Neither is switched."""
        by_doi: dict[str, str] = {}
        for r in report["references"]:
            for d in {clean_doi(r["claimed"]["doi"]).lower(), (r["doi"] or "").lower()} - {""}:
                if d in by_doi and by_doi[d] != r["key"]:
                    report["review_findings"].append(finding(
                        "duplicate-study", "error",
                        f"{by_doi[d]} and {r['key']} are the same work ({d}) — a preprint and its "
                        "published version are one study: keep the published one, exclude the "
                        "other, and correct the PRISMA counts"))
                    for rr in report["references"]:
                        if rr["key"] in (by_doi[d], r["key"]):
                            # the fix is an exclusion, not a rewrite: undo the
                            # version switch and identifier fills on both entries
                            for k in rr.get("version_switch", []) + ["doi", "pmid"]:
                                rr["suggested"].pop(k, None)
                else:
                    by_doi.setdefault(d, r["key"])

    @staticmethod
    def _claimed(a: dict) -> dict:
        def text(field: str) -> str:
            v = a.get(field)
            return "" if v is None else str(v)

        pmid = text("pmid").strip()
        url = next((u for u in (text("url"), text("source_url"), text("link")) if u.startswith("http")), "")
        return {"key": pmid or clean_doi(text("doi")) or url or text("title")[:40],
                "pmid": pmid, "doi": text("doi"), "title": text("title"),
                "authors": text("authors"), "year": text("year"),
                "journal": text("journal"), "url": url}

    def _verify_reference(self, c: dict, pubmed: dict) -> dict:
        findings: list[dict] = []
        rec: dict = {"key": c["key"], "pmid": c["pmid"], "doi": "", "claimed": c,
                     "registry": None, "pubmed": None, "candidates": [], "suggested": {}}

        raw = c["doi"].strip()
        doi = clean_doi(raw)
        if doi and not DOI_FORM.match(doi):
            findings.append(finding("doi-invalid", "error", f"“{raw}” is not a DOI"))
            doi = ""
        elif raw and doi != raw:
            findings.append(finding("doi-malformed", "warning",
                                    f"DOI field holds “{raw}”; the bare form is {doi}"))
            rec["suggested"]["doi"] = doi
        rec["doi"] = doi

        if self.reg is None:
            rec["findings"] = findings
            rec["status"] = "offline" if not findings else worst(findings)
            return rec

        # PubMed side.
        if REAL_PMID.match(c["pmid"]) and c["pmid"] not in pubmed:
            fb = getattr(self.reg, "lookup_pmid_fallback", lambda _p: None)(c["pmid"])
            if fb:
                pubmed[c["pmid"]] = fb
                findings.append(finding("pmid-via-index", "info",
                                        f"PubMed did not answer; PMID confirmed via {fb['source']}"))
        if REAL_PMID.match(c["pmid"]):
            if c["pmid"] not in pubmed:
                findings.append(finding("lookup-failed", "warning",
                                        "PubMed lookup failed (OpenAlex and Semantic Scholar "
                                        "did not know it either)"))
            elif pubmed[c["pmid"]] is None:
                findings.append(finding("pmid-not-found", "error",
                                        f"PMID {c['pmid']} is not in PubMed"))
            else:
                pm = pubmed[c["pmid"]]
                rec["pubmed"] = pm
                findings += check_pubmed_record(c, pm)
                if not doi and pm["doi"] and pm["title_similarity"] >= TITLE_MATCH:
                    rec["suggested"]["doi"] = pm["doi"]
                    findings.append(finding("doi-from-pubmed", "info",
                                            f"No DOI in the review; PubMed has {pm['doi']}"))
                    doi = pm["doi"]
                    rec["doi"] = doi

        # DOI side.
        if doi:
            status, meta = self.reg.lookup_doi(doi, self.check_updates)
            if status == "error":
                findings.append(finding("lookup-failed", "warning",
                                        f"DOI lookup failed: {meta.get('detail', '')}"))
            elif status == "not-found":
                findings.append(finding("doi-not-found", "error",
                                        f"{doi} is not registered with any DOI agency"))
            else:
                rec["registry"] = meta
                rec["source"] = status
                if meta.get("fallback_reason"):
                    findings.append(finding("doi-via-index", "info",
                                            f"{meta['fallback_reason']}; DOI confirmed via {status}"))
                if self.check_updates and meta.get("retraction_checked") is False:
                    findings.append(finding("retraction-unchecked", "warning",
                                            f"confirmed via {status}, which has no retraction data "
                                            "— re-run when Crossref answers"))
                findings += check_registry_record(c, meta)
                if _record_confirmed(c, meta):
                    rec["suggested"].update(_fill_empty(c, meta))
                    if is_preprint(doi, meta):
                        findings += self._prefer_published(c, rec, doi, meta)
        elif c.get("url") and not is_doi_url(c["url"]) and (
                not REAL_PMID.match(c["pmid"]) or rec["pubmed"] is None):
            # a work identified by its URL (report, web page, dataset, software)
            u = self.verify_url(c["url"], c["title"], line_mode=False)
            findings += u["findings"]
            rec["url"] = u["url"]
            rec["registry"] = rec["registry"] or u["registry"]
        elif not c["pmid"] or not REAL_PMID.match(c["pmid"]) or rec["pubmed"] is None:
            findings.append(finding("doi-missing", "warning",
                                    "No DOI, PMID or URL to verify this reference against"))

        if (not doi or any(f["code"] == "doi-title-mismatch" for f in findings)) \
                and self.search_missing and c["title"]:
            # A candidate needs the title *and* the first author to agree: short
            # titles ("Deep learning") are shared by many unrelated papers.
            cands = [x for x in self.reg.search_bibliographic(c["title"], c["authors"], c["year"])
                     if x["title_similarity"] >= TITLE_MATCH and x["author_match"]
                     and (not first_year(c["year"]) or not x["year"]
                          or abs(first_year(c["year"]) - x["year"]) <= 1)
                     and x["doi"].lower() != doi.lower()]
            rec["candidates"] = cands
            if cands:
                findings.append(finding("doi-candidate", "info",
                                        f"Crossref match by title: {cands[0]['doi']} "
                                        f"(similarity {cands[0]['title_similarity']:.2f}) — confirm before using"))

        findings += self._add_pmid(c, rec)
        rec["findings"] = findings
        rec["status"] = worst(findings)
        return rec

    def _prefer_published(self, c: dict, rec: dict, doi: str, meta: dict) -> list[dict]:
        """Peer-reviewed version first: switch to it when the registry links it."""
        rec["version"] = "preprint"
        pubs = self.reg.published_versions(doi, meta)
        for pub in pubs:
            status, pm = self.reg.lookup_doi(pub, self.check_updates)
            if status not in ("crossref", "other-registry"):
                continue
            rec["version"] = "published"
            rec["published"] = pm
            rec["version_switch"] = ["doi", "journal", "year", "title"]
            rec["suggested"].update({"doi": pm.get("doi") or pub,
                                     **{k: v for k, v in (("journal", pm.get("journal")),
                                                          ("year", str(pm.get("year") or "")),
                                                          ("title", pm.get("title"))) if v}})
            rec["doi"] = clean_doi(pm.get("doi") or pub)
            out = [finding("preprint-has-published-version", "warning",
                           f"Cites the preprint {doi}; the peer-reviewed version is {rec['doi']} "
                           f"({pm.get('journal') or 'journal'}, {pm.get('year') or 'n.d.'}). Cite that — "
                           "and check the review's extracted numbers against it, since results can "
                           "change in peer review")]
            out += [f for f in check_registry_record({**c, "title": pm.get("title", ""),
                                                      "authors": c["authors"], "year": ""}, pm)
                    if f["code"] in ("retracted", "cites-notice") or f["code"].startswith("update-")]
            return out
        if self.search_missing:
            cands = self.reg.find_published_version(c["title"] or meta.get("title", ""),
                                                    c["authors"], c["year"])
            if cands:
                rec["published_candidates"] = cands
                return [finding("preprint-published-candidate", "warning",
                                f"Cites the preprint {doi}; a peer-reviewed version appears to be "
                                f"{cands[0]['doi']} ({cands[0]['journal']}, {cands[0]['year']}) — "
                                "confirm it is the same work, then cite it")]
        return [finding("preprint-only", "info",
                        f"{doi} is a preprint with no peer-reviewed version found: say in the "
                        "review's limitations that it is not peer-reviewed")]

    def _add_pmid(self, c: dict, rec: dict) -> list[dict]:
        """Record the PubMed ID wherever PubMed has the (cited) article."""
        if self.reg is None or not rec["doi"]:
            return []
        switched = rec.get("version") == "published"
        if REAL_PMID.match(c["pmid"]) and not switched:
            return []
        found = self.reg.pmid_for_doi(rec["doi"])
        if not found or found == c["pmid"]:
            return []
        pm = (self.reg.lookup_pmids([found]) or {}).get(found)
        want = (rec.get("published") or rec.get("registry") or {}).get("title") or c["title"]
        if not pm or title_similarity(want, pm["title"]) < TITLE_MATCH:
            return []
        rec["suggested"]["pmid"] = found
        rec["pubmed"] = pm
        return [finding("pmid-found", "info",
                        f"PubMed has this article as PMID {found}"
                        + (f" (the review uses {c['pmid']})" if c["pmid"] else ""))]

    def _verify_in_text(self, review: dict, refs: list[dict], report: dict) -> None:
        by_pmid = {c["pmid"].lower(): c for c in refs if c["pmid"]}
        by_doi = {clean_doi(c["doi"]).lower(): c for c in refs if c["doi"]}
        for r in report["references"]:  # DOIs learned from PubMed count too
            if r["doi"]:
                by_doi.setdefault(r["doi"].lower(), r["claimed"])
        screened = {str(e.get("pmid", "")).lower(): e for e in review.get("screening_log") or []}
        cited: set[str] = set()

        for path, key, value in walk_prose(review):
            if _is_id_key(key) and isinstance(value, (str, int)):
                ids = [str(value).lower()]
                cites = [{"kind": "pmid", "id": i, "snippet": f"{key} = {value}"} for i in ids if i]
            elif isinstance(value, str) and len(value) > 3:
                cites = extract_citations(value)
            else:
                continue
            for ct in cites:
                f = self._judge_citation(ct, by_pmid, by_doi, refs, screened, cited)
                if f:
                    report["in_text"].append({**f, "location": path, "snippet": ct["snippet"],
                                              "cited": {k: ct[k] for k in ("kind", "id", "surname", "year")
                                                        if k in ct}})

        for c in refs:
            if c["key"].lower() not in cited:
                report["review_findings"].append(finding(
                    "uncited-included", "info",
                    f"{c['key']} ({first_author_segment(c['authors']).strip()} {c['year']}) is "
                    "included but never cited in the generated text", key=c["key"]))

    @staticmethod
    def _judge_citation(ct: dict, by_pmid: dict, by_doi: dict, refs: list[dict],
                        screened: dict, cited: set) -> Optional[dict]:
        kind = ct["kind"]
        if kind in ("pmid", "doi"):
            hit = (by_pmid if kind == "pmid" else by_doi).get(ct["id"])
            if hit:
                cited.add(hit["key"].lower())
                return None
            label = f"{'PMID' if kind == 'pmid' else 'DOI'} {ct['id']}"
            log = screened.get(ct["id"]) if kind == "pmid" else None
            if log:
                return finding("citation-excluded", "error",
                               f"{label} is cited but was excluded at {log.get('stage', '?')}"
                               + (f": {log['reason']}" if log.get("reason") else ""))
            return finding("citation-unknown", "error",
                           f"{label} is cited but appears nowhere in the review — not "
                           "included, not screened (hallucinated or from another review)")
        if kind == "author_year":
            hits = [c for c in refs if surname_matches(ct["surname"], c["authors"])
                    and years_close(c["year"], ct["year"])]
            if hits:
                for h in hits:
                    cited.add(h["key"].lower())
                return None
            return finding("author-year-unmatched", "warning",
                           f"“{ct['surname']} {ct['year']}” matches no included study")
        if kind == "pair":
            c = by_pmid.get(ct["id"])
            if c and not (surname_matches(ct["surname"], c["authors"])
                          and years_close(c["year"], ct["year"])):
                return finding("author-year-pmid-conflict", "warning",
                               f"“{ct['surname']} {ct['year']}” is paired with PMID {ct['id']}, "
                               f"which is {first_author_segment(c['authors']).strip()} {c['year']}")
        return None

    # ---- generic Markdown ----

    def verify_url(self, url: str, claimed: str, line_mode: bool = True, strict: bool = True) -> dict:
        """A URL reference: the page exists and is the work the reference names.

        ``claimed`` is the reference line (``line_mode``) or the work's title.
        The live page is fetched; when it is gone or blocks automated access the
        Wayback Machine's closest snapshot is checked instead. The page's title
        (HTML metadata or the PDF's) must match the reference, or — when the page
        has no usable title — most of the reference's distinctive words must
        appear in its opening text. ``strict`` (reference lists) makes an
        uncheckable page unverified; in-text links are only flagged.
        """
        url = url.rstrip(TRAILING)
        rec = {"key": url, "pmid": "", "doi": "", "url": url, "kind": "url",
               "claimed": {"key": url, "pmid": "", "doi": "", "title": claimed, "authors": "",
                           "year": "", "journal": "", "url": url},
               "registry": None, "pubmed": None, "candidates": [], "suggested": {}}
        findings: list[dict] = []
        fetch = getattr(self.reg, "fetch_page", None) if self.reg else None
        if not callable(fetch):
            rec.update(findings=[finding("url-unchecked", "warning", f"{url} was not checked (offline)")],
                       status="offline" if self.reg is None else "warnings")
            return rec
        live = fetch(url)
        live = live if isinstance(live, dict) else {"status": 0, "error": "no response"}
        content, via = None, None
        if 200 <= int(live.get("status") or 0) < 400 and not live.get("soft_404"):
            content = live
        else:
            snap = self.reg.wayback(url) if callable(getattr(self.reg, "wayback", None)) else None
            arch = fetch(snap) if snap else None
            if isinstance(arch, dict) and 200 <= int(arch.get("status") or 0) < 400 and not arch.get("soft_404"):
                content, via = arch, snap
        gone = int(live.get("status") or 0) in (404, 410) or bool(live.get("soft_404"))
        why = live.get("error") or (f"HTTP {live.get('status')}" if not live.get("soft_404") else "a 'not found' page")
        if content is None:
            if gone:
                findings.append(finding("url-not-found", "error",
                                        f"{url} does not exist ({why}) and has no archived copy"))
            else:
                findings.append(finding("url-unreachable", "warning" if strict else "info",
                                        f"{url} could not be checked ({why}); no archived copy to check instead"))
        else:
            rec["registry"] = {"page_title": content.get("title", ""), "final_url": content.get("final_url"),
                               "kind": content.get("kind"), "archived_copy": via}
            if via and gone:
                findings.append(finding("url-dead-archived", "warning",
                                        f"{url} is gone ({why}); verified against the archived copy {via} — cite it"))
            elif via:
                findings.append(finding("url-via-archive", "info",
                                        f"{url} blocked automated access ({why}); verified against {via}"))
            title = (content.get("title") or "").strip()
            if title and claimed and len(norm_text(title)) >= 8:
                sim = title_in_line(title, claimed) if line_mode else title_similarity(claimed, title)
                rec["registry"]["title_similarity"] = round(sim, 3)
                if sim < TITLE_PARTIAL:
                    words = _distinctive_words(claimed)
                    text = norm_text(content.get("text") or "")
                    hit = sum(w in text for w in words) / max(1, len(words))
                    if hit < 0.6:
                        findings.append(finding("url-title-mismatch", "error",
                                                f"{url} is “{title[:120]}”, not the work the reference names"))
                elif sim < TITLE_MATCH:
                    findings.append(finding("url-title-partial", "warning",
                                            f"page title only partly matches: “{title[:120]}” ({sim:.2f})"))
            elif claimed:
                words = _distinctive_words(claimed)
                text = norm_text(content.get("text") or "")
                hit = sum(w in text for w in words) / max(1, len(words))
                rec["registry"]["word_coverage"] = round(hit, 3)
                if not words or hit < 0.6:
                    findings.append(finding("url-content-unchecked", "warning" if strict else "info",
                                            f"{url} has no title to compare and its text does not show the "
                                            f"reference ({hit:.0%} of its words)"))
        rec["findings"] = findings
        rec["status"] = worst(findings)
        return rec

    def _verify_line_doi(self, doi: str, line: str) -> dict:
        """One DOI from a reference-list line (or the body) against the registries."""
        claimed = {"key": doi, "pmid": "", "doi": doi, "title": line,
                   "authors": "", "year": "", "journal": ""}
        findings: list[dict] = []
        rec = {"key": doi, "pmid": "", "doi": doi, "claimed": claimed, "registry": None,
               "pubmed": None, "candidates": [], "suggested": {}}
        if self.reg:
            status, meta = self.reg.lookup_doi(doi, self.check_updates)
            if status == "error":
                findings.append(finding("lookup-failed", "warning", meta.get("detail", "")))
            elif status == "not-found":
                findings.append(finding("doi-not-found", "error",
                                        f"{doi} is not registered with any DOI agency"))
            else:
                rec["registry"] = meta
                findings += check_registry_record(claimed, meta, line_mode=bool(line))
        rec["findings"] = findings
        rec["status"] = worst(findings) if self.reg else "offline"
        return rec

    @staticmethod
    def _reference_list(text: str) -> tuple[str, list[str]]:
        """Split a Markdown document into its body and its reference-list lines."""
        m = re.search(r"^#{1,3}\s*(?:\d+\.?\s*)?(References|Bibliography)\s*$", text,
                      re.MULTILINE | re.IGNORECASE)
        body, refs_text = (text[:m.start()], text[m.end():]) if m else (text, "")
        # The next top-level heading (an appendix) ends the reference list.
        ref_block = refs_text.splitlines()
        end = next((i for i, ln in enumerate(ref_block)
                    if re.match(r"#{1,2} ", ln) or ln.startswith("<!-- sidecar:")), len(ref_block))
        return body, [ln.strip() for ln in ref_block[:end] if ln.strip()]

    @staticmethod
    def _in_reference_list(ct: dict, ref_lines: list[str]) -> bool:
        return any(surname_matches(ct["surname"], ln.split("(")[0]) or
                   (norm_text(ct["surname"]) in norm_text(ln[:120]).split())
                   for ln in ref_lines if ct["year"] in ln)

    # ---- generic Markdown ----

    def verify_markdown(self, text: str) -> dict:
        report: dict = {"mode": "markdown", "references": [], "in_text": [],
                        "review_findings": [], "urls": {}}
        body, ref_lines = self._reference_list(text)

        lines_by_doi = {}
        for ln in ref_lines:
            for d in DOI_RE.findall(ln):
                lines_by_doi.setdefault(_trim(d).lower(), ln)
        all_dois = []
        for d in DOI_RE.findall(text):
            d = _trim(d)
            if d.lower() not in {x.lower() for x in all_dois}:
                all_dois.append(d)

        for n, doi in enumerate(all_dois, 1):
            rec = self._verify_line_doi(doi, lines_by_doi.get(doi.lower(), ""))
            report["references"].append(rec)
            print(f"[{n:>3}/{len(all_dois)}] {rec['status']:>9}  {doi}", file=sys.stderr)

        handled = self._url_references(ref_lines, report)
        self._in_text_urls(body, handled, report)
        if ref_lines:
            for ct in extract_citations(body):
                if ct["kind"] != "author_year":
                    continue
                if not self._in_reference_list(ct, ref_lines):
                    report["in_text"].append({**finding(
                        "author-year-unmatched", "warning",
                        f"“{ct['surname']} {ct['year']}” matches no reference-list entry"),
                        "location": "body", "snippet": ct["snippet"]})
        return report

    # ---- resource catalog (mapping reviews) ----

    @staticmethod
    def _verify_resource_citations(catalog: list[dict], body: str, refs: list[dict],
                                   report: dict) -> None:
        """A mapping review cites *resources* (``NKG-029``), each standing for its
        publications in ``resource_catalog``. A cited resource credits those
        studies; a resource id not in the catalog, or a catalog link that names
        no included study, is an error — the claim cannot be traced to evidence.
        """
        keys = {c["key"].lower(): c["key"] for c in refs}
        by_doi = {clean_doi(c["doi"]).lower(): c["key"] for c in refs if c["doi"]}

        def resolve(pid: str) -> Optional[str]:
            p = str(pid).strip().lower()
            return keys.get(p) or keys.get(f"local_{p}") or by_doi.get(clean_doi(p).lower())

        studies: dict[str, list[str]] = {}
        for row in catalog:
            rid = str(row.get("resource_id") or "").strip()
            if not rid:
                continue
            pids = row.get("publication_ids") or []
            if isinstance(pids, str):
                pids = [x.strip() for x in re.split(r"[;,]", pids) if x.strip()]
            got = [resolve(x) for x in pids]
            studies[rid.upper()] = [g for g in got if g]
            missing = [str(x) for x, g in zip(pids, got) if not g]
            if missing:
                report["review_findings"].append(finding(
                    "catalog-publication-unknown", "error",
                    f"resource {rid} lists publication(s) {', '.join(missing)} that match no "
                    "included study (use the study's pmid/DOI in publication_ids)"))
            if not pids:
                report["review_findings"].append(finding(
                    "catalog-no-publications", "error", f"resource {rid} lists no publications"))
        prefixes = sorted({m.group(1) for rid in studies
                           if (m := re.match(r"([A-Za-z]+-)\d+$", rid))})
        credited: set[str] = set()
        if prefixes:
            pat = re.compile(rf"\b((?:{'|'.join(map(re.escape, prefixes))})\d+)\b", re.I)
            for m in pat.finditer(body):
                rid = m.group(1).upper()
                if rid in studies:
                    credited.update(k.lower() for k in studies[rid])
                else:
                    report["in_text"].append({**finding(
                        "resource-unknown", "error",
                        f"{m.group(1)} is cited but is not in the resource catalog"),
                        "location": "manuscript", "snippet": snippet(body, m.start(), m.end()),
                        "cited": {"kind": "resource", "id": rid}})
        report["review_findings"] = [
            f for f in report["review_findings"]
            if not (f["code"] == "uncited-included" and f.get("key", "").lower() in credited)]
        report["resources"] = {"catalog": len(studies), "cited_studies_via_resources": len(credited)}

    # ---- assembled manuscript ----

    def _url_references(self, ref_lines: list[str], report: dict) -> set[str]:
        """Reference-list entries without a DOI or PMID: verify them by URL.

        An entry naming an included study is that study's own entry: when the
        study has no DOI/PMID its URL is verified against its title, in its own
        record. Any other entry with a URL is a background reference, verified
        against the line. An entry with no identifier at all cannot be checked.
        Returns the URLs handled, so body links to them are not checked twice.
        """
        included = [r for r in report["references"] if r.get("role") == "included"]
        handled: set[str] = set()
        for ln in ref_lines:
            if DOI_RE.search(ln) or PMID_RE.search(ln) or not re.search(r"\b(19|20)\d\d\b", ln) \
                    or ln.startswith(("**", "#")) or len(ln) < 30:
                continue
            urls = [u.rstrip(TRAILING) for u in URL_RE.findall(ln) if not is_doi_url(u)]
            own = next((r for r in included if r["claimed"]["title"]
                        and title_in_line(r["claimed"]["title"], ln) >= TITLE_MATCH), None)
            if own is not None:
                if urls and any(f["code"] == "doi-missing" for f in own.get("findings", [])):
                    u = self.verify_url(urls[0], own["claimed"]["title"], line_mode=False)
                    own["findings"] = [f for f in own["findings"] if f["code"] != "doi-missing"] + u["findings"]
                    own["status"] = worst(own["findings"])
                    own["url"], own["registry"] = u["url"], own.get("registry") or u["registry"]
                handled.update(urls)
                continue
            if urls:
                rec = self.verify_url(urls[0], ln)
                rec["role"] = "background"
                report["references"].append(rec)
                handled.update(urls)
                print(f"[url] {rec['status']:>9}  {urls[0]}", file=sys.stderr)
            else:
                report["references"].append({
                    "key": ln[:60], "pmid": "", "doi": "", "role": "background", "kind": "unidentified",
                    "claimed": {"key": ln[:60], "pmid": "", "doi": "", "title": ln, "authors": "",
                                "year": "", "journal": ""},
                    "registry": None, "pubmed": None, "candidates": [], "suggested": {},
                    "findings": [finding("reference-unidentified", "warning",
                                         "no DOI, PMID or URL: this reference cannot be verified — "
                                         "add the identifier or the URL it is available at")],
                    "status": "warnings"})
        return handled

    def _in_text_urls(self, body: str, skip: set[str], report: dict) -> None:
        """Links in the prose that are not reference-list entries: they must resolve."""
        seen = set(skip)
        for u in URL_RE.findall(body):
            u = u.rstrip(TRAILING)
            if is_doi_url(u) or u in seen:
                continue
            seen.add(u)
            rec = self.verify_url(u, "", strict=False)
            for f in rec["findings"]:
                report["in_text"].append({**f, "location": "manuscript", "snippet": u,
                                          "cited": {"kind": "url", "id": u}})

    def verify_manuscript(self, review: dict, text: str,
                          catalog: Optional[list[dict]] = None) -> dict:
        """The delivered manuscript, checked against the review it was built from.

        Prose written after the export (Discussion, prior reviews, figure
        interpretations) is only in the manuscript, so this is the check that
        covers what is actually handed over. Every included study is verified
        as in review mode, and every reference-list entry that is not an
        included study is verified as **background** literature. In the body,
        a PMID/DOI must be an included study or a background reference; a
        study the screening excluded is an error either way, unless it is in
        the reference list as a background citation.
        """
        body, ref_lines = self._reference_list(text)
        report = self.verify_references(review.get("included_articles") or [], mode="manuscript")
        for r in report["references"]:
            r["role"] = "included"
        refs = [r["claimed"] for r in report["references"]]
        included_dois = {clean_doi(c["doi"]).lower() for c in refs if c["doi"]}
        included_dois |= {r["doi"].lower() for r in report["references"] if r["doi"]}

        background: dict[str, str] = {}
        background_pmids: set[str] = set()
        for ln in ref_lines:
            for d in DOI_RE.findall(ln):
                d = _trim(d)
                if d.lower() not in included_dois:
                    background.setdefault(d.lower(), ln)
            for m in PMID_RE.finditer(ln):
                background_pmids |= {x.lower() for x in PMID_ID.findall(m.group(1))}
        for n, (doi, line) in enumerate(background.items(), 1):
            rec = self._verify_line_doi(doi, line)
            rec["role"] = "background"
            report["references"].append(rec)
            print(f"[bg {n:>3}/{len(background)}] {rec['status']:>9}  {doi}", file=sys.stderr)

        scope = {"included_articles": review.get("included_articles") or [],
                 "screening_log": review.get("screening_log") or [],
                 "manuscript": body}
        self._verify_in_text(scope, refs, report)

        def is_background(f: dict) -> bool:
            ct = f.get("cited") or {}
            if ct.get("kind") == "doi":
                return ct.get("id", "") in background
            if ct.get("kind") == "pmid":
                return ct.get("id", "") in background_pmids
            if ct.get("kind") == "author_year":
                return self._in_reference_list(ct, ref_lines)
            return False

        report["in_text"] = [f for f in report["in_text"] if not is_background(f)]
        handled = self._url_references(ref_lines, report)
        self._in_text_urls(body, handled, report)
        if catalog:
            self._verify_resource_citations(catalog, body, refs, report)
        if not ref_lines:
            report["review_findings"].append(finding(
                "no-reference-list", "warning", "the manuscript has no References section"))
        report["grounding_validation"] = _grounding_summary(review.get("grounding_validation"))
        return report


def _record_confirmed(c: dict, meta: dict) -> bool:
    """Whether a registry record is shown to be *this* article.

    A matching title suffices. With no title in the review there is nothing to
    match, and a hallucinated DOI resolves to some real paper all the same, so
    the record must instead agree on both first author and year.
    """
    if c["title"]:
        return meta.get("title_similarity", 0) >= TITLE_MATCH
    fam = meta["authors"][0][0] if meta.get("authors") else ""
    cy = first_year(c["year"])
    return bool(fam and surname_matches(fam, c["authors"]) and cy and meta.get("years")
                and min(abs(cy - y) for y in meta["years"]) <= 1)


def _fill_empty(c: dict, meta: dict) -> dict:
    """Fields the review left empty that a matching registry record supplies."""
    s: dict = {}
    if not c["title"] and meta.get("title"):
        s["title"] = meta["title"]
    if not first_year(c["year"]) and meta.get("year"):
        s["year"] = str(meta["year"])
    if not c["journal"] and meta.get("journal"):
        s["journal"] = meta["journal"]
    if not c["authors"].strip() and meta.get("authors"):
        s["authors"] = ", ".join(f"{f} {_initials(g, '')}".strip() for f, g in meta["authors"])
    return s


def _grounding_summary(g: Optional[dict]) -> Optional[dict]:
    if not g:
        return None
    return {k: g.get(k) for k in ("overall_verdict", "hallucinated_citation_count",
                                  "unresolved_citations")}


def summarise(report: dict) -> dict:
    refs = report["references"]
    allf = [f for r in refs for f in r.get("findings", [])] + report["in_text"] + report["review_findings"]
    count = lambda sev: sum(f["severity"] == sev for f in allf)
    by_code: dict[str, int] = {}
    for f in allf:
        by_code[f["code"]] = by_code.get(f["code"], 0) + 1
    statuses: dict[str, int] = {}
    for r in refs:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    # A reference nobody could check is not a verified one: offline runs,
    # registry outages and identifier-less entries make the result INCOMPLETE,
    # never PASS, so a release gate cannot mistake "not checked" for "clean".
    unverified = sorted(r["key"] for r in refs
                        if r["status"] == "offline"
                        or any(f["code"] in UNVERIFIED_CODES for f in r.get("findings", [])))
    errors = count("error")
    if errors:
        verdict = "FAIL"
    elif report.get("offline") or unverified or not refs:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "unverified": unverified,
        "references": len(refs),
        "reference_status": statuses,
        "errors": errors, "warnings": count("warning"), "info": count("info"),
        "by_code": dict(sorted(by_code.items())),
        "retracted": sorted(r["key"] for r in refs
                            if any(f["code"] == "retracted" for f in r.get("findings", []))),
        "dead_urls": sorted(u for u, v in report["urls"].items() if not v["accessible"]),
    }


def sha256_file(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bind_inputs(paths: Iterable[Path]) -> dict:
    """``{path: sha256}`` for every file a report vouches for."""
    return {str(Path(p).resolve()): sha256_file(p) for p in paths if Path(p).is_file()}


def stale_inputs(report: dict) -> list[str]:
    """Inputs the report vouches for that changed or vanished since it was written."""
    inputs = report.get("inputs") or {}
    if not inputs:
        return ["(report records no input hashes — written by an older verify_citations.py)"]
    return [p for p, h in inputs.items() if not Path(p).is_file() or sha256_file(Path(p)) != h]


def manuscript_inputs(md: Path, review_json: Optional[Path]) -> list[Path]:
    """What a manuscript's report must cover: the text, the review, the authored sidecar."""
    out = [md]
    if review_json:
        out.append(review_json)
        stem = review_json.stem
        out += [p for p in (review_json.with_name(f"{stem}.manuscript.json"),) if p.is_file()]
    return out


def review_for_markdown(md: Path) -> Optional[Path]:
    """``review.manuscript.md`` / ``review.md`` → the ``review.json`` beside it, if any."""
    stem = md.stem
    for suffix in (".manuscript", ".delivered"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
    js = md.with_name(f"{stem}.json")
    return js if js.is_file() else None


def load_catalog(review_json: Optional[Path]) -> list:
    """``resource_catalog`` from the authored ``<stem>.manuscript.json``, if any."""
    if not review_json:
        return []
    ms = review_json.with_name(f"{review_json.stem}.manuscript.json")
    try:
        return (json.loads(ms.read_text(encoding="utf-8")).get("resource_catalog") or []) \
            if ms.is_file() else []
    except (OSError, json.JSONDecodeError):
        return []


def gate(data: Any, base: Path, *, corpus: bool = False, manuscript: Optional[str] = None,
         catalog: Optional[list] = None,
         inputs: Iterable[Path] = (), offline: bool = False, mailto: str = "",
         delay: float = 0.2, labels: Optional[dict] = None, registry: Any = None) -> dict:
    """Verify, write ``<base>.citations.json|md``, print the outcome; return the report.

    The entry point the other scripts call:

    * ``corpus=True`` — ``data`` is a list of corpus items; their identifiers only;
    * ``manuscript=<text>`` — ``data`` is the review dict and the text the
      assembled manuscript (``verify_manuscript``);
    * otherwise ``data`` is a review dict, references and in-text citations.

    ``inputs`` are the files the report vouches for; their hashes are recorded
    so a gate can refuse a report that no longer describes what ships.
    ``labels`` maps a reference key to what to print for it (a PDF's filename).
    Callers act on ``report["summary"]["verdict"]``: anything but PASS blocks.
    """
    mailto = mailto or os.environ.get("SYNTHSCHOLAR_EMAIL") or os.environ.get("NCBI_EMAIL") or ""
    own = registry is None and not offline
    reg = None if offline else (registry or Registry(mailto=mailto, delay=delay))
    try:
        v = Verifier(reg)
        if corpus:
            report = v.verify_references(data, mode="corpus")
        elif manuscript is not None:
            # no review JSON to check it against: the Markdown's own DOIs and reference list
            report = (v.verify_manuscript(data, manuscript, catalog) if data
                      else v.verify_markdown(manuscript))
        else:
            report = v.verify_review(data)
    finally:
        if reg and own:
            reg.close()
    report = {"file": str(base), "offline": offline,
              "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "inputs": bind_inputs(inputs), **report}
    report["summary"] = s = summarise(report)
    base.parent.mkdir(parents=True, exist_ok=True)
    Path(f"{base}.citations.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                              encoding="utf-8")
    Path(f"{base}.citations.md").write_text(render_markdown(report, str(base)), encoding="utf-8")

    labels = labels or {}
    print(f"Citations: {s['verdict']} — {s['references']} reference(s), {s['errors']} error(s), "
          f"{s['warnings']} warning(s), {len(s['unverified'])} unverified"
          + (" [offline: nothing checked against the registries]" if offline else ""))
    for r in report["references"]:
        for f in r.get("findings", []):
            if f["severity"] == "error":
                print(f"  ERROR {labels.get(r['key'], r['key'])}: {f['message']}")
    for f in report["in_text"] + report["review_findings"]:
        if f["severity"] == "error":
            print(f"  ERROR {f.get('location', 'review')}: {f['message']}")
    if s["unverified"] and not offline:
        shown = ", ".join(labels.get(k, k) for k in s["unverified"][:10])
        print(f"  UNVERIFIED {len(s['unverified'])}: {shown}"
              + (" …" if len(s["unverified"]) > 10 else "")
              + " — lookup failed or no DOI/PMID; re-run when the registries answer, "
                "or add the identifier from the paper")
    print(f"  report: {base}.citations.md")
    return report


# ─────────────────────────── reference styles ────────────────────────────
# Rendered from registry metadata where the record matched, per
# references/citation_styles.md; unverified entries keep the review's text.

def _initials(given: str, sep: str = ".") -> str:
    parts = []
    for tok in (given or "").replace(".", " ").split():
        sub = [p[0].upper() + sep for p in tok.split("-") if p]
        parts.append("-".join(sub))
    return (" " if sep else "").join(parts)


def _authors_style(authors: list[tuple[str, str]], style: str) -> str:
    if style == "apa":
        names = [f"{f}, {_initials(g)}".rstrip(", ") for f, g in authors]
        if len(names) > 20:
            names = names[:19] + ["…", names[-1]]
            return ", ".join(names[:-1]) + " " + names[-1]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + ", & " + names[-1]
    if style == "nature":
        names = [f"{f}, {_initials(g)}".rstrip(", ") for f, g in authors]
        if len(names) > 5:
            return names[0] + " et al."
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " & " + names[-1]
    if style == "vancouver":
        names = [f"{f} {_initials(g, '').replace(' ', '')}".strip() for f, g in authors]
        return ", ".join(names[:6]) + (", et al" if len(names) > 6 else "")
    if style == "ieee":
        names = [f"{_initials(g)} {f}".strip() for f, g in authors]
        if len(names) > 6:
            return names[0] + " et al."
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + ", and " + names[-1]
    raise ValueError(style)


def render_reference(rec: dict, style: str) -> str:
    """One reference: the peer-reviewed version when there is one, then its
    PMID when PubMed has it; a preprint with no published version is labelled."""
    base = _render_reference(rec, style)
    pmid = rec["suggested"].get("pmid") or (rec["pmid"] if REAL_PMID.match(rec["pmid"] or "") else "")
    if pmid:
        base = base.rstrip(".") + f". PMID: {pmid}."
    if rec.get("version") == "preprint":
        base += " [Preprint — not peer-reviewed]"
    return base


def _render_reference(rec: dict, style: str) -> str:
    m = rec.get("published") or rec.get("registry") or {}
    trusted = m and (rec.get("published") is m or m.get("title_similarity", 0) >= TITLE_MATCH) \
        and m.get("authors")
    c = rec["claimed"]
    if not trusted:
        doi = f" https://doi.org/{rec['doi']}" if rec["doi"] else ""
        return f"{c['authors']} ({c['year'] or 'n.d.'}). {c['title']}. *{c['journal']}*.{doi} [unverified]"
    a = _authors_style(m["authors"], style)
    t, y = m["title"].rstrip("."), m.get("year") or "n.d."
    j = m["journal"]
    jab = m.get("journal_abbrev") or j
    v, i, p, d = m.get("volume", ""), m.get("issue", ""), m.get("pages", ""), m["doi"]
    if style == "apa":
        s = f"{a} ({y}). {t}."
        if j:
            s += f" *{j}*"
            s += f", *{v}*" if v else ""
            s += f"({i})" if i else ""
            s += f", {p}" if p else ""
            s += "."
        return s + f" https://doi.org/{d}"
    if style == "nature":
        s = f"{a} {t}. *{jab}*" + (f" **{v}**," if v else "") + (f" {p}" if p else "")
        return s + f" ({y}). https://doi.org/{d}"
    if style == "vancouver":
        s = f"{a}. {t}. {jab.replace('.', '')}. {y}"
        s += f";{v}" if v else ""
        s += f"({i})" if i else ""
        s += f":{p}" if p else ""
        return s + f". doi:{d}"
    s = f"{a}, “{t},” *{jab}*"
    s += f", vol. {v}" if v else ""
    s += f", no. {i}" if i else ""
    s += f", pp. {p}" if p else ""
    return s + f", {y}, doi: {d}."


def render_reference_list(report: dict, style: str, order: list[str]) -> str:
    recs = {r["key"]: r for r in report["references"]}
    keyed = [recs[k] for k in order if k in recs] + [r for r in report["references"] if r["key"] not in order]
    if style == "apa":
        keyed.sort(key=lambda r: norm_text(
            (r.get("registry") or {}).get("authors", [("", "")])[0][0]
            if (r.get("registry") or {}).get("authors") else r["claimed"]["authors"]))
        lines = [render_reference(r, style) for r in keyed]
    else:
        lines = [f"{n}. {render_reference(r, style)}" if style != "ieee"
                 else f"[{n}] {render_reference(r, style)}" for n, r in enumerate(keyed, 1)]
    return f"## References ({style})\n\n" + "\n\n".join(lines) + "\n"


def citation_order(review: dict, refs: list[dict]) -> list[str]:
    """Included-article keys in order of first citation, uncited ones last."""
    by_pmid = {r["pmid"].lower(): r["key"] for r in refs if r["pmid"]}
    order: list[str] = []
    for _, _, value in walk_prose(review):
        if isinstance(value, str):
            for ct in sorted(extract_citations(value), key=lambda x: x["pos"]):
                k = by_pmid.get(ct.get("id", ""))
                if k and k not in order:
                    order.append(k)
    return order


# ─────────────────────────── report output ────────────────────────────

def render_markdown(report: dict, source: str) -> str:
    s = report["summary"]
    out = [f"# Citation verification — {Path(source).name}\n"]
    out.append(f"**Verdict: {s.get('verdict', '?')}**"
               + (" — nothing was checked against the registries (offline)" if report.get("offline")
                  else f" — {len(s.get('unverified') or [])} reference(s) could not be checked"
                  if s.get("unverified") else "") + "\n")
    if s.get("unverified") and not report.get("offline"):
        out.append("Unverified (lookup failed, or no DOI/PMID): "
                   + ", ".join(f"`{k}`" for k in s["unverified"]) + "\n")
    out.append(f"**{s['errors']} error(s), {s['warnings']} warning(s), {s['info']} note(s)** "
               f"across {s['references']} reference(s).  ")
    out.append("Reference status: " + ", ".join(f"{k} {v}" for k, v in sorted(s["reference_status"].items())) + "\n")
    if s["retracted"]:
        out.append(f"**Retracted:** {', '.join(s['retracted'])}\n")
    g = report.get("grounding_validation")
    if g:
        out.append(f"LLM grounding pass for comparison: verdict {g.get('overall_verdict')}, "
                   f"{g.get('hallucinated_citation_count')} hallucinated citation(s).\n")

    def table(rows: list[tuple]) -> list[str]:
        t = ["| Severity | Item | Finding |", "| --- | --- | --- |"]
        t += [f"| {sev} | {item} | {msg.replace('|', '/')} |" for sev, item, msg in rows]
        return t + [""]

    rank = lambda f: -SEVERITY_RANK[f["severity"]]
    ref_rows = [(f["severity"], f"`{r['key']}`", f["message"])
                for r in report["references"] for f in sorted(r["findings"], key=rank)
                if f["severity"] != "info"]
    if ref_rows:
        out += ["## Reference metadata\n"] + table(ref_rows)
    txt_rows = [(f["severity"], f"`{f['location']}`", f"{f['message']}<br>{f['snippet']}")
                for f in sorted(report["in_text"], key=rank)]
    if txt_rows:
        out += ["## In-text citations\n"] + table(txt_rows)
    rev_rows = [(f["severity"], "review", f["message"])
                for f in sorted(report["review_findings"], key=rank) if f["severity"] != "info"]
    if rev_rows:
        out += ["## Review-level\n"] + table(rev_rows)
    info = [(f["severity"], f"`{r['key']}`", f["message"])
            for r in report["references"] for f in r["findings"] if f["severity"] == "info"]
    info += [("info", "review", f["message"]) for f in report["review_findings"] if f["severity"] == "info"]
    if info:
        out += ["<details><summary>Notes ({})</summary>\n".format(len(info))] + table(info) + ["</details>\n"]
    if s["dead_urls"]:
        out += ["## Unreachable URLs\n"] + [f"- {u}" for u in s["dead_urls"]] + [""]
    if not (ref_rows or txt_rows or rev_rows):
        out.append("No errors or warnings.\n")
    return "\n".join(out)


def apply_fixes(review: dict, report: dict) -> list[dict]:
    """Write registry-confirmed corrections into ``review`` in place."""
    recs = {r["key"]: r for r in report["references"]}
    applied = []
    for a in review.get("included_articles") or []:
        rec = recs.get(Verifier._claimed(a)["key"])
        if not rec:
            continue
        for field, value in rec["suggested"].items():
            if str(a.get(field) or "") != value:
                old = a.get(field, "")
                a[field] = value
                entry = {"key": rec["key"], "field": field, "old": old, "new": value}
                if field == "pmid" and old:
                    entry["references_updated"] = _rekey(review, str(old), value, skip=a)
                if field == "doi" and rec.get("version") == "published":
                    entry["version_switch"] = True
                    # the text read so far — clearing the change needs a different one
                    entry["old_text_sha256"] = hashlib.sha256("\n".join(
                        x for x in (a.get("abstract"), a.get("full_text")) if x).encode(
                        "utf-8", errors="replace")).hexdigest()
                applied.append(entry)
        for e in applied:
            if e["key"] == rec["key"] and e.get("version_switch"):
                e["study"] = str(a.get("pmid") or rec["key"])   # its id after any re-key
    return applied


def record_version_changes(applied: list[dict], review_json: Path) -> Optional[Path]:
    """Write ``<stem>.revalidation.json``: studies whose cited version changed.

    A switch from a preprint to its published version changes what the review
    is reporting, but the charting, appraisal and evidence spans were extracted
    from the version read before. ``audit_grounding.py`` fails each listed study
    until its evidence is re-extracted from the new version.
    """
    changes = [{"pmid": e["study"], "old_doi": e["old"], "new_doi": e["new"],
                "old_text_sha256": e.get("old_text_sha256", ""),
                "reason": "preprint replaced by its published version (verify_citations --apply)",
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
               for e in applied if e.get("version_switch")]
    if not changes:
        return None
    out = review_json.with_name(f"{review_json.stem}.revalidation.json")
    try:
        prev = json.loads(out.read_text(encoding="utf-8")).get("changes") or [] if out.is_file() else []
    except (OSError, json.JSONDecodeError):
        prev = []
    keep = [c for c in prev if c.get("pmid") not in {x["pmid"] for x in changes}]
    out.write_text(json.dumps({"changes": keep + changes}, indent=2) + "\n", encoding="utf-8")
    return out


def _rekey(review: dict, old: str, new: str, skip: dict) -> int:
    """Replace article id ``old`` with ``new`` across the review; count replacements.

    The id is the article's key everywhere: screening log, evidence spans,
    rubrics, per-group lists and "PMID: …" citations in the prose. A ``local_``
    id is a unique hash, so it is replaced wherever it appears as a token; a
    numeric one only as a whole value or after "PMID".
    """
    tok = re.compile(rf"(?<![\w]){re.escape(old)}(?![\w])") if old.startswith("local_") \
        else re.compile(rf"(\bPMIDs?\s*[:#]?\s*(?:[\w]+\s*(?:,|and|&)\s*)*){re.escape(old)}\b", re.I)
    count = 0

    def fix(node: Any) -> Any:
        nonlocal count
        if node is skip:
            return node
        if isinstance(node, dict):
            for k, v in node.items():
                node[k] = fix(v)
            return node
        if isinstance(node, list):
            return [fix(v) for v in node]
        if isinstance(node, str):
            if node == old:
                count += 1
                return new
            out, n = (tok.subn(new, node) if old.startswith("local_")
                      else tok.subn(lambda m: m.group(1) + new, node))
            count += n
            return out
        return node

    fix(review)
    return count


# ─────────────────────────── CLI ────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="review JSON (PRISMAReviewResult) or a Markdown file")
    ap.add_argument("--outdir", help="report directory (default: next to the input)")
    ap.add_argument("--mailto", default=(os.environ.get("SYNTHSCHOLAR_EMAIL")
                                         or os.environ.get("NCBI_EMAIL") or ""),
                    help="contact address for Crossref's polite pool and NCBI")
    ap.add_argument("--offline", action="store_true",
                    help="no network: DOI syntax and in-text citations only")
    ap.add_argument("--no-retraction-check", action="store_true",
                    help="skip the per-DOI Crossref notice query (halves requests)")
    ap.add_argument("--no-search", action="store_true",
                    help="don't search Crossref for DOI candidates of DOI-less references")
    ap.add_argument("--check-urls", action="store_true", help="also test non-DOI URLs")
    ap.add_argument("--style", choices=STYLES,
                    help="also write a reference list in this style from verified metadata")
    ap.add_argument("--apply", metavar="OUT_JSON",
                    help="write a copy of the review with registry-confirmed fixes")
    ap.add_argument("--review", metavar="REVIEW_JSON",
                    help="for a Markdown manuscript: the review it was built from (default: "
                         "<stem>.json beside it) — checks the manuscript against its included "
                         "studies, with other reference-list entries verified as background")
    ap.add_argument("--strict", action="store_true", help="warnings also fail the exit code")
    ap.add_argument("--delay", type=float, default=0.2, help="seconds between Crossref calls")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        print(f"no such file: {src}", file=sys.stderr)
        return 2
    text = src.read_text(encoding="utf-8")
    is_json = src.suffix.lower() == ".json"
    review: dict = {}
    if is_json:
        try:
            review = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"{src}: not valid JSON ({e})", file=sys.stderr)
            return 2
        if not isinstance(review, dict) or "included_articles" not in review:
            print(f"{src}: not a review result (no included_articles)", file=sys.stderr)
            return 2
    if (args.apply or args.style) and not is_json:
        print("--apply and --style need the review JSON", file=sys.stderr)
        return 2
    review_json = None if is_json else (Path(args.review) if args.review else review_for_markdown(src))
    if review_json:
        try:
            review = json.loads(review_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"{review_json}: unreadable ({e})", file=sys.stderr)
            return 2
        print(f"manuscript mode: checking {src.name} against {review_json.name}", file=sys.stderr)

    reg = None if args.offline else Registry(mailto=args.mailto, delay=args.delay)
    if reg and not args.mailto:
        print("note: no --mailto / SYNTHSCHOLAR_EMAIL — using Crossref's public pool (slower)",
              file=sys.stderr)
    v = Verifier(reg, check_updates=not args.no_retraction_check,
                 search_missing=not args.no_search)
    try:
        report = (v.verify_review(review) if is_json
                  else v.verify_manuscript(review, text, load_catalog(review_json)) if review_json
                  else v.verify_markdown(text))
        if args.check_urls and reg:
            prose = (" ".join(str(x[2]) for x in walk_prose(review) if isinstance(x[2], str))
                     if is_json else text)
            urls = list(dict.fromkeys(u.rstrip(TRAILING) for u in URL_RE.findall(prose)
                                      if "doi.org" not in u))
            for u in urls:
                ok, code = reg.check_url(u)
                report["urls"][u] = {"accessible": ok, "status_code": code}
                print(f"[{'ok' if ok else 'dead':>9}] {code} {u}", file=sys.stderr)
    finally:
        if reg:
            reg.close()

    inputs = [src] if is_json else manuscript_inputs(src, review_json)
    report = {"file": str(src), "offline": args.offline,
              "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "inputs": bind_inputs(inputs), **report}
    report["summary"] = summarise(report)

    outdir = Path(args.outdir) if args.outdir else src.parent
    outdir.mkdir(parents=True, exist_ok=True)
    base = outdir / src.stem
    if args.apply:
        report["applied"] = apply_fixes(review, report)
        Path(args.apply).write_text(json.dumps(review, indent=2, ensure_ascii=False),
                                    encoding="utf-8")
        print(f"applied {len(report['applied'])} fix(es) → {args.apply}", file=sys.stderr)
        reval = record_version_changes(report["applied"], src)
        if reval:
            print(f"version changes recorded → {reval}: re-extract those studies from the "
                  "published version before release (audit_grounding.py fails them until then)",
                  file=sys.stderr)
    if args.style:
        refs = [r["claimed"] for r in report["references"]]
        ref_md = render_reference_list(report, args.style, citation_order(review, refs))
        Path(f"{base}.references.{args.style}.md").write_text(ref_md, encoding="utf-8")
    Path(f"{base}.citations.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                              encoding="utf-8")
    Path(f"{base}.citations.md").write_text(render_markdown(report, str(src)), encoding="utf-8")

    s = report["summary"]
    print(f"\nVerdict: {s['verdict']}"
          + (f" — {len(s['unverified'])} reference(s) unverified" if s["unverified"] else ""),
          file=sys.stderr)
    print(f"{s['references']} reference(s): "
          + ", ".join(f"{k} {n}" for k, n in sorted(s["reference_status"].items()))
          + f" | {s['errors']} error(s), {s['warnings']} warning(s)", file=sys.stderr)
    for code, n in s["by_code"].items():
        print(f"  {code}: {n}", file=sys.stderr)
    print(f"report: {base}.citations.md  ({base}.citations.json)", file=sys.stderr)
    if args.style:
        print(f"references: {base}.references.{args.style}.md", file=sys.stderr)

    if s["verdict"] == "PASS" and args.strict and s["warnings"]:
        return 1
    return EXIT[s["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
