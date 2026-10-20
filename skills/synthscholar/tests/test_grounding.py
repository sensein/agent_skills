"""Regression tests for audit_grounding.py and the registry fallbacks.

    python -m pytest skills/synthscholar/tests -q

Covered: invented / paraphrased / wrong-basis / wrong-version evidence spans,
the claim ledger (unjudged, supported with real quotes, invented quotes,
unsourced numbers, contradicted, stale), aggregate numbers and percentages,
stated totals, version changes from --apply, overlapping reports, the PDF
grounding gate, resource-catalog citations, and OpenAlex / Semantic Scholar
fallback parsing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_grounding as ag  # noqa: E402
import generate_review_pdf  # noqa: E402
import verify_citations as vc  # noqa: E402

FULL = ("Methods. We recruited 120 participants from two clinics. Results. The model reached an "
        "accuracy of 91.2% on 120 participants, and sensitivity was 0.88. Trial NCT01234567.")
A = {"pmid": "local_a1", "doi": "10.1000/a", "title": "Decoding study A", "authors": "Doe J; Roe K",
     "year": "2023", "abstract": "We decoded choices.", "full_text": FULL}
B = {"pmid": "local_b2", "doi": "10.1000/b", "title": "Abstract-only study B", "authors": "Poe L",
     "year": "2022", "abstract": "Follow-up of trial NCT01234567 with 40 patients.", "full_text": ""}

MD = """# Review

## 3. Results

The classifier reached 91.2% accuracy in 120 participants (PMID: local_a1).
Of 2 included studies, 1 was read in full text (50%).
{extra}

## References

- Doe J (2023). Decoding study A.
"""


def write_review(tmp: Path, spans=None, extra: str = "", **flow) -> Path:
    review = {"included_articles": [A, B], "screening_log": [],
              "flow": {"included_synthesis": 2, **flow},
              "evidence_spans": spans if spans is not None else [
                  {"paper_pmid": "local_a1", "text": "accuracy of 91.2% on 120 participants",
                   "section": "results", "doi": "10.1000/a"}]}
    js = tmp / "review.json"
    js.write_text(json.dumps(review))
    (tmp / "review.manuscript.md").write_text(MD.format(extra=extra))
    return js


def judge(js: Path, **by_text):
    """Fill the ledger: by_text maps a substring of a claim to its judgement."""
    led_path = js.with_name("review.claims.json")
    led = json.loads(led_path.read_text())
    for c in led["claims"]:
        for frag, j in by_text.items():
            if frag in c["text"]:
                c.update({"judged_by": "test", "explanation": "x", **j})
    led_path.write_text(json.dumps(led))


def run(js: Path, init: bool = False) -> dict:
    md = js.with_name("review.manuscript.md")
    if init:
        review = json.loads(js.read_text())
        ag.init_claims(review, md, {}, js.with_name("review.claims.json"))
    return ag.audit(js, md)


def codes(rep: dict) -> set[str]:
    return {f["code"] for f in rep["findings"]}


SUPPORTED = {"support": "supported",
             "evidence": [{"study": "local_a1", "quote": "accuracy of 91.2% on 120 participants"}]}

# B shares A's trial id: resolve that by default so other tests isolate one thing.
DISTINCT = {"distinct": [{"reports": ["local_a1", "local_b2"], "reason": "different cohorts"}]}


@pytest.fixture
def js(tmp_path):
    p = write_review(tmp_path)
    p.with_name("review.studies.json").write_text(json.dumps(DISTINCT))
    return p


# ── evidence spans ───────────────────────────────────────────────────

def test_passes_when_spans_claims_and_numbers_are_grounded(js):
    run(js, init=True)
    judge(js, **{"91.2%": SUPPORTED})
    rep = run(js)
    assert rep["summary"]["verdict"] == "PASS", rep["findings"]


@pytest.mark.parametrize("span,code", [
    ({"paper_pmid": "local_a1", "text": "the graph has 258.4 million edges", "section": "results"},
     "span-not-in-source"),
    ({"paper_pmid": "local_b2", "text": "Follow-up of trial NCT01234567", "section": "results"},
     "span-reading-basis"),
    ({"paper_pmid": "local_a1", "text": "accuracy of 91.2%", "section": "results", "doi": "10.1101/old"},
     "span-other-version"),
    ({"paper_pmid": "local_zz", "text": "anything", "section": "results"}, "span-unknown-paper"),
])
def test_bad_spans_fail(tmp_path, span, code):
    js = write_review(tmp_path, spans=[span])
    rep = run(js, init=True)
    assert code in codes(rep) and rep["summary"]["verdict"] == "FAIL"


def test_paraphrased_span_is_only_a_warning():
    loc = ag.locate("We recruited 120 participants from two clinics. Results. The model reached an "
                    "accuracy of 91.2% across all of the people enrolled", FULL)
    assert loc["match"] == "paraphrase"


def test_normalised_match_survives_hyphenation_and_quotes():
    assert ag.locate("“sensitiv-\nity was 0.88”", FULL)["match"] in ("exact", "normalized")


# ── claim ledger ─────────────────────────────────────────────────────

def test_no_ledger_is_incomplete(js):
    rep = run(js)
    assert rep["summary"]["verdict"] == "INCOMPLETE" and "claims-not-audited" in codes(rep)


def test_unjudged_claim_is_incomplete(js):
    rep = run(js, init=True)
    assert rep["summary"]["verdict"] == "INCOMPLETE" and "claim-unjudged" in codes(rep)


def test_invented_quote_fails(js):
    run(js, init=True)
    judge(js, **{"91.2%": {"support": "supported", "evidence": [
        {"study": "local_a1", "quote": "accuracy of 91.2% on 120 participants in three countries"}]}})
    assert "quote-not-in-source" in codes(run(js))


def test_number_missing_from_the_quotes_fails(js):
    run(js, init=True)
    judge(js, **{"91.2%": {"support": "supported", "evidence": [
        {"study": "local_a1", "quote": "The model reached an accuracy of 91.2%"}]}})
    rep = run(js)
    assert "number-unsourced" in codes(rep)
    assert any("120" in f["message"] for f in rep["findings"] if f["code"] == "number-unsourced")


@pytest.mark.parametrize("support", ["contradicted", "insufficient_evidence"])
def test_contradicted_or_insufficient_claim_fails(js, support):
    run(js, init=True)
    judge(js, **{"91.2%": {"support": support, "evidence": []}})
    assert run(js)["summary"]["verdict"] == "FAIL"


def test_edited_manuscript_makes_the_ledger_stale_but_keeps_judgements(js):
    run(js, init=True)
    judge(js, **{"91.2%": SUPPORTED})
    md = js.with_name("review.manuscript.md")
    md.write_text(md.read_text().replace("## References", "A new claim cites 7 cohorts (PMID: local_a1).\n\n## References"))
    rep = run(js)
    assert "claims-stale" in codes(rep) and rep["summary"]["verdict"] == "INCOMPLETE"
    run(js, init=True)  # refresh: the unchanged sentence keeps its judgement
    led = json.loads(js.with_name("review.claims.json").read_text())
    old = next(c for c in led["claims"] if "91.2%" in c["text"])
    assert old["support"] == "supported"


def test_aggregate_with_unrecomputable_number_needs_a_source(tmp_path):
    js = write_review(tmp_path, extra="Across the corpus, 17 graphs were public.")
    js.with_name("review.studies.json").write_text(json.dumps(DISTINCT))
    run(js, init=True)
    judge(js, **{"91.2%": SUPPORTED})
    assert "aggregate-unsourced" in codes(run(js))
    judge(js, **{"17 graphs": {"derived_from": "resource_catalog availability column"}})
    rep = run(js)
    assert "aggregate-unsourced" not in codes(rep) and "aggregate-asserted" in codes(rep)


# ── numbers ──────────────────────────────────────────────────────────

def test_percentage_that_does_not_compute_fails(tmp_path):
    js = write_review(tmp_path, extra="Full text was read for 1 of 2 studies (40%).")
    assert "percentage-mismatch" in codes(run(js, init=True))


def test_stated_total_must_match_the_included_set(tmp_path):
    js = write_review(tmp_path, extra="We included 3 studies.")
    assert "count-mismatch" in codes(run(js, init=True))


def test_flow_must_match_the_included_set(tmp_path):
    js = write_review(tmp_path)
    data = json.loads(js.read_text())
    data["flow"]["included_synthesis"] = 5
    js.write_text(json.dumps(data))
    assert "flow-included-mismatch" in codes(run(js, init=True))


# ── version changes ──────────────────────────────────────────────────

def test_version_change_fails_until_re_extracted(js):
    run(js, init=True)
    judge(js, **{"91.2%": SUPPORTED})
    js.with_name("review.revalidation.json").write_text(json.dumps(
        {"changes": [{"pmid": "local_a1", "old_doi": "10.1000/a", "new_doi": "10.2/published"}]}))
    assert "version-changed" in codes(run(js))
    data = json.loads(js.read_text())  # re-extracted from the published version
    data["included_articles"][0]["doi"] = "10.2/published"
    for s in data["evidence_spans"]:
        s["doi"] = "10.2/published"
    js.write_text(json.dumps(data))
    assert "version-changed" not in codes(run(js))


def test_apply_records_version_switches(tmp_path):
    applied = [{"key": "k", "field": "doi", "old": "10.1101/pre", "new": "10.1/pub",
                "version_switch": True, "study": "local_a1"},
               {"key": "k", "field": "journal", "old": "", "new": "J"}]
    out = vc.record_version_changes(applied, tmp_path / "review.json")
    assert json.loads(out.read_text())["changes"][0]["new_doi"] == "10.1/pub"


# ── studies vs reports ───────────────────────────────────────────────

def test_reports_sharing_a_trial_id_must_be_resolved(tmp_path):
    js = write_review(tmp_path)
    rep = run(js, init=True)
    assert "study-overlap-unresolved" in codes(rep)


def test_grouped_reports_count_as_one_study(tmp_path):
    js = write_review(tmp_path, extra="We included 1 studies after grouping reports.")
    js.with_name("review.studies.json").write_text(json.dumps(
        {"studies": [{"study_id": "ST-1", "reports": ["local_a1", "local_b2"], "basis": "NCT01234567"}]}))
    rep = run(js, init=True)
    assert "study-overlap-unresolved" not in codes(rep)
    assert rep["studies"]["studies"] == 1 and "count-mismatch" not in codes(rep)


# ── PDF grounding gate ───────────────────────────────────────────────

def test_pdf_gate_requires_a_passing_current_audit(js):
    md = js.with_name("review.manuscript.md")
    refusal, _ = generate_review_pdf.grounding_gate(md, allow=False)
    assert refusal and "INCOMPLETE" in refusal
    run(js, init=True)
    judge(js, **{"91.2%": SUPPORTED})
    assert generate_review_pdf.grounding_gate(md, allow=False) == (None, None)
    md.write_text(md.read_text() + "\nAnother claim of 9 cohorts (PMID: local_a1).\n")
    refusal, _ = generate_review_pdf.grounding_gate(md, allow=False)
    assert refusal  # stale → re-audited → the new claim is unjudged
    refusal, notice = generate_review_pdf.grounding_gate(md, allow=True)
    assert refusal is None and "Grounding not verified" in notice


# ── resource-catalog citations ───────────────────────────────────────

def test_resource_ids_credit_their_studies_and_unknown_links_fail():
    arts = [{"pmid": "local_R01", "doi": "", "title": "T", "authors": "A", "year": "2020"}]
    v = vc.Verifier(None)
    rep = v.verify_manuscript({"included_articles": arts}, "Graphs grew (NKG-001; NKG-009).\n",
                              catalog=[{"resource_id": "NKG-001", "publication_ids": ["R01"]},
                                       {"resource_id": "NKG-002", "publication_ids": ["R77"]}])
    found = {f["code"] for f in rep["in_text"] + rep["review_findings"]}
    assert "resource-unknown" in found            # NKG-009 is not in the catalog
    assert "catalog-publication-unknown" in found  # R77 is no included study
    assert "uncited-included" not in found         # NKG-001 credits local_R01


# ── fallback registries ──────────────────────────────────────────────

def test_openalex_and_semantic_scholar_records_parse_to_the_registry_shape():
    oa = vc.parse_openalex({"doi": "https://doi.org/10.1/X", "title": "A paper", "publication_year": 2021,
                            "authorships": [{"author": {"display_name": "Jane Q. Smith"}}],
                            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/123"}, "is_retracted": True})
    assert oa["doi"] == "10.1/X" and oa["authors"][0][0] == "Smith" and oa["pmid"] == "123"
    assert oa["updates"][0]["type"] == "retraction"
    s2 = vc.parse_semantic_scholar({"title": "A paper", "year": 2021, "authors": [{"name": "Jane Smith"}],
                                    "externalIds": {"DOI": "10.1/x", "PubMed": "123"}})
    assert s2["retraction_checked"] is False and s2["pmid"] == "123"


def test_fallback_answers_when_the_primary_registry_cannot(monkeypatch):
    reg = vc.Registry()
    try:
        monkeypatch.setattr(reg, "openalex", lambda k, v: ("not-found", {}))
        monkeypatch.setattr(reg, "semantic_scholar",
                            lambda k, v: ("found", vc.parse_semantic_scholar({"title": "T", "year": 2020})))
        status, meta = reg._fallback("doi", "10.1/x", "Crossref HTTP 503")
        assert status == "semanticscholar" and meta["fallback_reason"] == "Crossref HTTP 503"
        monkeypatch.setattr(reg, "semantic_scholar", lambda k, v: ("not-found", {}))
        assert reg._fallback("doi", "10.1/x", "Crossref HTTP 503")[0] == "error"
    finally:
        reg.close()


def test_record_only_in_semantic_scholar_is_retraction_unchecked(tmp_path):
    class S2Only:
        def lookup_doi(self, doi, check_updates=True):
            m = vc.parse_semantic_scholar({"title": "Decoding study A", "year": 2023,
                                           "authors": [{"name": "J Doe"}], "externalIds": {"DOI": doi}})
            m["fallback_reason"] = "Crossref unreachable"
            return "semanticscholar", m

        def __getattr__(self, name):
            return lambda *a, **k: [] if name != "pmid_for_doi" else None

    rep = vc.gate({"included_articles": [A]}, tmp_path / "x", registry=S2Only())
    assert rep["summary"]["verdict"] == "INCOMPLETE"
    assert "retraction-unchecked" in rep["summary"]["by_code"]
