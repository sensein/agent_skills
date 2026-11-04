"""Regression tests for the citation release gate and corpus duplicate check.

The registries are replaced by a stub, so these run offline and deterministically:

    python -m pytest skills/synthscholar/tests -q

Covered: fabricated identifiers, registry outages, offline runs, references
with no identifier, citations in manuscript prose (included / background /
excluded / unknown), stale and missing reports at the PDF gate, and
content-based duplicate detection.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_corpus  # noqa: E402
import generate_review_pdf  # noqa: E402
import verify_citations as vc  # noqa: E402


def record(doi: str, title: str, family: str, year: int) -> dict:
    """A registry record shaped like ``parse_crossref`` output."""
    return {"registry": "crossref", "doi": doi, "title": title, "authors": [(family, "J")],
            "year": year, "years": [year], "journal": "Journal of Tests", "type": "journal-article",
            "updates": [], "notice_for": [], "published_as": [], "has_preprint": [], "subtype": ""}


class StubRegistry:
    """Answers DOI lookups from a dict; anything unlisted is not registered."""

    def __init__(self, records: dict | None = None, outage: bool = False):
        self.records = {k.lower(): v for k, v in (records or {}).items()}
        self.outage = outage

    def lookup_doi(self, doi, check_updates=True):
        if self.outage:
            return "error", {"detail": "HTTP 503"}
        rec = self.records.get(doi.lower())
        return ("crossref", dict(rec)) if rec else ("not-found", {})

    def lookup_pmids(self, pmids):
        return {}  # absent → "PubMed lookup failed"

    def search_bibliographic(self, *a):
        return []

    def find_published_version(self, *a):
        return []

    def published_versions(self, *a):
        return []

    def pmid_for_doi(self, doi):
        return None

    def close(self):
        pass


INCLUDED = {"pmid": "local_aa11", "doi": "10.1234/inc.1", "title": "Included study title",
            "authors": "Doe J", "year": "2023", "journal": "Journal of Tests"}
GOOD = {"10.1234/inc.1": record("10.1234/inc.1", "Included study title", "Doe", 2023),
        "10.9999/bg.1": record("10.9999/bg.1", "Background paper title", "Smith", 2020)}


def review(*arts, log=()):
    return {"included_articles": list(arts), "screening_log": list(log)}


def verdict(data, tmp_path, registry=None, **kw):
    rep = vc.gate(data, tmp_path / "out", registry=registry, **kw)
    return rep["summary"]["verdict"], rep


# ── verdicts ──────────────────────────────────────────────────────────

def test_pass_when_every_reference_checks_out(tmp_path):
    v, _ = verdict(review(INCLUDED), tmp_path, StubRegistry(GOOD))
    assert v == "PASS"


def test_fabricated_doi_fails(tmp_path):
    # a real DOI attached to the wrong paper — the BBQS failure
    wrong = {"10.1234/inc.1": record("10.1234/inc.1", "An unrelated paper about gerbils", "Roe", 2019)}
    v, rep = verdict(review(INCLUDED), tmp_path, StubRegistry(wrong))
    assert v == "FAIL"
    assert rep["summary"]["by_code"].get("doi-title-mismatch") == 1


def test_unregistered_doi_fails(tmp_path):
    v, rep = verdict(review(INCLUDED), tmp_path, StubRegistry({}))
    assert v == "FAIL" and "doi-not-found" in rep["summary"]["by_code"]


def test_registry_outage_is_incomplete_not_pass(tmp_path):
    v, rep = verdict(review(INCLUDED), tmp_path, StubRegistry(GOOD, outage=True))
    assert v == "INCOMPLETE"
    assert rep["summary"]["unverified"] == ["local_aa11"]


def test_pubmed_outage_is_incomplete(tmp_path):
    art = {**INCLUDED, "pmid": "31234567"}
    v, _ = verdict(review(art), tmp_path, StubRegistry(GOOD))
    assert v == "INCOMPLETE"


def test_offline_is_incomplete(tmp_path):
    v, _ = verdict(review(INCLUDED), tmp_path, offline=True)
    assert v == "INCOMPLETE"


def test_reference_without_identifier_is_incomplete(tmp_path):
    v, _ = verdict(review({**INCLUDED, "doi": ""}), tmp_path, StubRegistry(GOOD))
    assert v == "INCOMPLETE"


def test_empty_review_is_incomplete(tmp_path):
    v, _ = verdict(review(), tmp_path, StubRegistry(GOOD))
    assert v == "INCOMPLETE"


def test_cli_exit_code_follows_verdict(tmp_path):
    js = tmp_path / "review.json"
    js.write_text(json.dumps(review(INCLUDED)))
    r = subprocess.run([sys.executable, str(SCRIPTS / "verify_citations.py"), str(js), "--offline"],
                       capture_output=True, text=True)
    assert r.returncode == vc.EXIT["INCOMPLETE"] == 3


# ── manuscript prose ─────────────────────────────────────────────────

MANUSCRIPT = """# A review

## 1. Introduction

Earlier work framed the problem (Smith et al., 2020; https://doi.org/10.9999/bg.1).
The included study reported the effect (Doe et al., 2023; https://doi.org/10.1234/inc.1).
{extra}

## References

- Doe J (2023). Included study title. https://doi.org/10.1234/inc.1
- Smith A (2020). Background paper title. https://doi.org/10.9999/bg.1
"""


def test_manuscript_background_reference_is_allowed_and_verified(tmp_path):
    v, rep = verdict(review(INCLUDED), tmp_path, StubRegistry(GOOD),
                     manuscript=MANUSCRIPT.format(extra=""))
    assert v == "PASS"
    roles = {r["key"]: r.get("role") for r in rep["references"]}
    assert roles == {"local_aa11": "included", "10.9999/bg.1": "background"}


def test_manuscript_unknown_doi_in_prose_fails(tmp_path):
    text = MANUSCRIPT.format(extra="A later claim rests on https://doi.org/10.5555/made.up.")
    v, rep = verdict(review(INCLUDED), tmp_path, StubRegistry(GOOD), manuscript=text)
    assert v == "FAIL"
    assert any(f["code"] == "citation-unknown" for f in rep["in_text"])


def test_manuscript_citing_an_excluded_study_fails(tmp_path):
    log = [{"pmid": "local_e0e0", "stage": "title_abstract", "decision": "exclude",
            "reason": "animal model"}]
    text = MANUSCRIPT.format(extra="An excluded record agreed (PMID: local_e0e0).")
    v, rep = verdict(review(INCLUDED, log=log), tmp_path, StubRegistry(GOOD), manuscript=text)
    assert v == "FAIL"
    assert any(f["code"] == "citation-excluded" for f in rep["in_text"])


def test_manuscript_background_doi_that_does_not_resolve_fails(tmp_path):
    only_included = {"10.1234/inc.1": GOOD["10.1234/inc.1"]}
    v, _ = verdict(review(INCLUDED), tmp_path, StubRegistry(only_included),
                   manuscript=MANUSCRIPT.format(extra=""))
    assert v == "FAIL"


# ── the PDF gate ─────────────────────────────────────────────────────

@pytest.fixture
def delivered(tmp_path, monkeypatch):
    """A review.json + review.manuscript.json + review.manuscript.md, registries stubbed."""
    stub = StubRegistry(GOOD)
    monkeypatch.setattr(vc, "Registry", lambda **kw: stub)
    (tmp_path / "review.json").write_text(json.dumps(review(INCLUDED)))
    (tmp_path / "review.manuscript.json").write_text(json.dumps({"discussion": "…"}))
    md = tmp_path / "review.manuscript.md"
    md.write_text(MANUSCRIPT.format(extra=""))
    return md, stub


def test_pdf_gate_verifies_a_missing_report_then_passes(delivered):
    md, _ = delivered
    assert generate_review_pdf.citation_gate(md, None, allow=False) == (None, None)
    rep = json.loads(md.with_name("review.manuscript.citations.json").read_text())
    assert rep["summary"]["verdict"] == "PASS"
    assert {Path(p).name for p in rep["inputs"]} == {
        "review.manuscript.md", "review.json", "review.manuscript.json"}


def test_pdf_gate_refuses_missing_report_when_it_cannot_verify(delivered):
    md, _ = delivered
    refusal, _ = generate_review_pdf.citation_gate(md, None, allow=False, verify=False)
    assert refusal and "missing or stale" in refusal


@pytest.mark.parametrize("edit", ["review.manuscript.md", "review.manuscript.json", "review.json"])
def test_pdf_gate_detects_any_changed_input(delivered, edit):
    md, _ = delivered
    generate_review_pdf.citation_gate(md, None, allow=False)
    f = md.with_name(edit)
    f.write_text(f.read_text() + "\n")  # same mtime granularity would not catch this; hashes do
    refusal, _ = generate_review_pdf.citation_gate(md, None, allow=False, verify=False)
    assert refusal and "stale" in refusal


def test_pdf_gate_reverifies_a_stale_report(delivered):
    md, _ = delivered
    generate_review_pdf.citation_gate(md, None, allow=False)
    md.write_text(MANUSCRIPT.format(extra="New prose cites https://doi.org/10.5555/made.up."))
    refusal, _ = generate_review_pdf.citation_gate(md, None, allow=False)
    assert refusal and "FAIL" in refusal


def test_pdf_gate_refuses_incomplete_and_labels_an_override(delivered):
    md, stub = delivered
    stub.outage = True
    refusal, _ = generate_review_pdf.citation_gate(md, None, allow=False)
    assert refusal and "INCOMPLETE" in refusal
    refusal, notice = generate_review_pdf.citation_gate(md, None, allow=True)
    assert refusal is None and notice and "not verified" in notice


def test_pdf_gate_treats_a_report_without_hashes_as_stale(delivered):
    md, _ = delivered
    old = {"summary": {"errors": 0, "warnings": 0, "references": 1}}  # pre-verdict format
    md.with_name("review.manuscript.citations.json").write_text(json.dumps(old))
    refusal, _ = generate_review_pdf.citation_gate(md, None, allow=False, verify=False)
    assert refusal


# ── content-based duplicates ─────────────────────────────────────────

WORDS = ("participants completed the task while neural activity was recorded across "
         "sessions and the decoder predicted choices from population firing rates ")


def item(pmid, text, **kw):
    return {"pmid": pmid, "full_text": text, "title": kw.pop("title", f"Title of {pmid} study"),
            "doi": kw.pop("doi", ""), "_pdf_path": f"pdfs/{pmid}.pdf",
            "_pdf_sha256": pmid, "content_sha256": pmid, **kw}


def test_preprint_and_published_version_are_flagged_by_text():
    paper = " ".join(f"{WORDS} trial {i}" for i in range(200))
    preprint = item("local_p1", paper[: int(len(paper) * 0.7)], title="Preprint working title")
    published = item("local_p2", paper, title="Published title, after review")
    unrelated = item("local_p3", " ".join(f"rodents explored the maze arm {i} for reward" for i in range(300)))
    pairs = build_corpus.find_duplicates([preprint, published, unrelated])
    assert [(i, j) for i, j, _ in pairs] == [(0, 1)]
    assert "shared text" in pairs[0][2]


def test_same_doi_different_files_is_flagged():
    a = item("local_d1", "alpha " * 50, doi="10.1/X", title="Grid cells in the entorhinal cortex")
    b = item("local_d2", "omega " * 50, doi="https://doi.org/10.1/x", title="Vocal learning in songbirds")
    assert build_corpus.find_duplicates([a, b])[0][2] == "same DOI"


def test_distinct_from_resolves_a_flagged_pair():
    paper = WORDS * 40
    a, b = item("local_c1", paper), item("local_c2", paper, _distinct_from=["local_c1"])
    assert build_corpus.mark_duplicates([a, b]) == 0
