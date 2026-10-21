"""Regression tests for source grounding: audit_grounding.py, extract_evidence.py,
grounding_core.py, and the registry fallbacks.

    python -m pytest skills/synthscholar/tests -q

Each false-PASS reported in review is pinned here: uncited prose, tables,
"aggregates" that only match a count, sign-flipped quotes, version clearance
without a new source, and appendices added after validation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_grounding as ag  # noqa: E402
import extract_evidence as ee  # noqa: E402
import generate_review_pdf  # noqa: E402
import grounding_core as gc  # noqa: E402
import verify_citations as vc  # noqa: E402

FULL = ("We decoded choices.\nMethods\nWe recruited 120 participants from two clinics and gave 5 mg daily.\n"
        "Results\nThe model reached an accuracy of 91.2% on 120 participants; 12 of 40 responders "
        "improved, and the score changed by −5 points. Trial NCT01234567.\nDiscussion\nIt worked.")
A = {"pmid": "local_a1", "doi": "10.1000/a", "title": "Decoding study A", "authors": "Doe J; Roe K",
     "year": "2023", "abstract": "We decoded choices.", "full_text": FULL,
     "full_text_retrieved_at": "2026-01-01T00:00:00Z"}
B = {"pmid": "local_b2", "doi": "10.1000/b", "title": "Follow-up study B", "authors": "Poe L",
     "year": "2022", "abstract": "Follow-up of trial NCT01234567 with 40 patients.",
     "full_text": "Follow-up of trial NCT01234567 with 40 patients.\nMethods\nWe followed 40 patients for a year."}
SPANS = [{"paper_pmid": "local_a1", "text": "accuracy of 91.2% on 120 participants", "section": "results",
          "doi": "10.1000/a"},
         {"paper_pmid": "local_b2", "text": "Follow-up of trial NCT01234567 with 40 patients.",
          "section": "abstract", "doi": "10.1000/b"}]
MD = """# Review

## 3. Results

The classifier reached 91.2% accuracy in 120 participants (Doe et al., 2023).
{extra}

## References

- Doe J (2023). Decoding study A.
"""
SUPPORTED = {"support": "supported",
             "evidence": [{"study": "local_a1", "quote": "accuracy of 91.2% on 120 participants"}]}
DISTINCT = {"distinct": [{"reports": ["local_a1", "local_b2"], "reason": "different cohorts"}]}


def write_review(tmp: Path, spans=None, extra: str = "", review_extra=None) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    review = {"included_articles": [dict(A), dict(B)], "screening_log": [],
              "flow": {"included_synthesis": 2}, "evidence_spans": spans if spans is not None else SPANS,
              **(review_extra or {})}
    js = tmp / "review.json"
    js.write_text(json.dumps(review))
    (tmp / "review.manuscript.md").write_text(MD.format(extra=extra))
    js.with_name("review.studies.json").write_text(json.dumps(DISTINCT))
    return js


def init(js: Path) -> dict:
    review = json.loads(js.read_text())
    return ag.init_claims(review, js.with_name("review.manuscript.md"), {}, js.with_name("review.claims.json"))


def judge(js: Path, **by_text):
    p = js.with_name("review.claims.json")
    led = json.loads(p.read_text())
    for c in led["claims"]:
        for frag, j in by_text.items():
            if frag in c["text"]:
                c.update({"judged_by": "test", "explanation": "x", **j})
    p.write_text(json.dumps(led))


def run(js: Path) -> dict:
    return ag.audit(js, js.with_name("review.manuscript.md"))


def codes(rep: dict) -> set[str]:
    return {f["code"] for f in rep["findings"]}


@pytest.fixture
def js(tmp_path):
    return write_review(tmp_path)


def test_a_fully_grounded_review_passes(js):
    init(js)
    judge(js, **{"91.2%": SUPPORTED})
    rep = run(js)
    assert rep["summary"]["verdict"] == "PASS", rep["findings"]


# ── the false PASSes from review ─────────────────────────────────────

def test_uncited_prose_is_a_claim_and_needs_a_judgement(tmp_path):
    js = write_review(tmp_path, extra="The intervention cures depression.")
    init(js)
    judge(js, **{"91.2%": SUPPORTED})
    rep = run(js)
    assert rep["summary"]["verdict"] == "INCOMPLETE" and "claim-unjudged" in codes(rep)


def test_table_rows_are_claims(tmp_path):
    js = write_review(tmp_path, extra="| Outcome | Rate |\n| --- | --- |\n| Cure | 99% |")
    led = init(js)
    assert any(c["kind"] == "table" and "99%" in c["numbers"] for c in led["claims"])
    judge(js, **{"91.2%": SUPPORTED})
    assert run(js)["summary"]["verdict"] == "INCOMPLETE"


def test_captions_are_claims(tmp_path):
    js = write_review(tmp_path, extra="**Figure 1.** Cure rates reached 99% in all trials.")
    assert any(c["kind"] == "caption" for c in init(js)["claims"])


def test_a_count_of_articles_cannot_derive_patients(tmp_path):
    js = write_review(tmp_path, extra="The intervention cured 2 patients.")
    init(js)
    judge(js, **{"91.2%": SUPPORTED,
                 "cured": {"support": "derived", "derivations": [
                     {"value": "2", "metric": "patients cured", "population": "included articles",
                      "expr": {"count": {"from": "included_articles"}}}]}})
    rep = run(js)
    assert any("patients" in f["message"] for f in rep["findings"] if f["code"] == "derivation-wrong")


def test_a_derivation_that_matches_meaning_and_value_passes(tmp_path):
    js = write_review(tmp_path, extra="Of the 2 included studies, 2 had full text (100%).")
    init(js)
    has_ft = {"count": {"from": "included_articles", "where": {"full_text": {"empty": False}}}}
    judge(js, **{"91.2%": SUPPORTED,
                 "Of the 2": {"support": "derived", "derivations": [
                     {"value": "2", "metric": "included studies", "population": "included set",
                      "expr": {"count": {"from": "included_articles"}}},
                     {"value": "2", "metric": "studies with full text", "population": "included set",
                      "expr": has_ft},
                     {"value": "100%", "metric": "share with full text", "population": "included set",
                      "expr": {"percent": [has_ft, {"count": {"from": "included_articles"}}]}}]}})
    rep = run(js)
    assert rep["summary"]["verdict"] == "PASS", rep["findings"]


def test_bag_of_numbers_no_longer_passes(tmp_path):
    js = write_review(tmp_path, extra="Across trials, 2 graphs were public.")
    init(js)
    judge(js, **{"91.2%": SUPPORTED, "graphs": {"support": "derived", "derivations": []}})
    assert "number-ungrounded" in codes(run(js))


def test_sign_is_preserved_in_matching():
    assert gc.locate("the score changed by +5 points", FULL)["match"] == "absent"
    assert gc.verbatim("the score changed by -5 points", FULL)
    assert [q["text"] for q in gc.quantities("changed by -5 points")] == ["-5 points"]


@pytest.mark.parametrize("quote,source", [
    ("5 mg", "15 mg"), ("5 mg", "-5 mg"), ("5 mg", "0.5 mg"),
    ("5", "5%"), ("5", "50"),
    ("Recruitment began 2025-09-30.", "Recruitment began 2020-01-01."),
])
def test_changed_dates_and_partial_numbers_are_not_verbatim(quote, source):
    assert not gc.verbatim(quote, source)


def test_matching_can_find_a_later_complete_numeric_token():
    assert gc.locate("5 mg", "15 mg, then 5 mg")["offset"] == 12
    assert gc.verbatim("Recruitment began 2020/01/01.", "Recruitment began 2020-01-01.")


@pytest.mark.parametrize("claim,value,expr", [
    ("The mean change was -2 mg.", "-2 mg", {"value": {"path": "flow.included_synthesis"}}),
    ("The mean dose was 2 mg.", "2 mg", {"value": {"path": "flow.included_synthesis"}}),
    ("The mean dose was 2 mg.", "2 mg", {"count": {"from": "included_articles"}}),
])
def test_derivation_cannot_change_sign_or_turn_counts_into_doses(tmp_path, claim, value, expr):
    js = write_review(tmp_path, extra=claim)
    init(js)
    judge(js, **{"91.2%": SUPPORTED, claim: {
        "support": "derived", "derivations": [{"value": value, "metric": "dose",
        "population": "participants", "expr": expr}]}})
    rep = run(js)
    assert rep["summary"]["verdict"] == "FAIL"
    assert "derivation-wrong" in codes(rep)


def test_signed_measurement_derivation_uses_source_unit():
    q = gc.quantities("-2 mg")[0]
    d = {"metric": "dose change", "population": "participants",
         "expr": {"value": {"path": "dose"}}}
    assert ag.check_derivation("The change was -2 mg.", q, d, {},
                               {"dose": -2, "dose_unit": "mg"}) is None
    assert ag.check_derivation("The change was -2 mg.", q, d, {},
                               {"dose": -2, "dose_unit": "g"}) is not None
    positive = gc.quantities("2 mg")[0]
    assert ag.check_derivation("The change was 2 mg.", positive, d, {},
                               {"dose": -2, "dose_unit": "mg"}) is not None


def test_catalog_copy_still_requires_source_evidence(tmp_path):
    js = write_review(tmp_path, extra=(
        "| resource_id | description |\n| --- | --- |\n"
        "| NKG-001 | Cures every cancer with 99% success |"))
    ms = {"resource_catalog": [{"resource_id": "NKG-001",
          "description": "Cures every cancer with 99% success", "publication_ids": ["local_a1"]}]}
    js.with_name("review.manuscript.json").write_text(json.dumps(ms))
    ag.init_claims(json.loads(js.read_text()), js.with_name("review.manuscript.md"), ms,
                   js.with_name("review.claims.json"))
    judge(js, **{"91.2%": SUPPORTED})
    rep = run(js)
    assert rep["summary"]["verdict"] == "INCOMPLETE"
    assert rep["claims"]["by_kind"]["table"] == 1
    assert "claim-unjudged" in codes(rep)


def test_quote_with_flipped_sign_fails(tmp_path):
    js = write_review(tmp_path, extra="Scores rose by +5 points (Doe et al., 2023).")
    init(js)
    judge(js, **{"91.2%": SUPPORTED,
                 "rose": {"support": "supported", "evidence": [
                     {"study": "local_a1", "quote": "the score changed by +5 points"}]}})
    assert {"quote-not-in-source", "claim-no-evidence"} <= codes(run(js))


@pytest.mark.parametrize("a,b,equal", [
    ("p < 0.05", "p = 0.05", False), ("5 mg", "5 g", False), ("5 mg", "5 mg", True),
    ("±2.1", "2.1", False), ("34,771", "34771", True), ("12%", "12", False),
])
def test_quantity_identity(a, b, equal):
    assert gc.quantity_in(gc.quantities(a)[0], b) is equal


# ── sections, pages, denominators, units ─────────────────────────────

def test_span_in_the_wrong_section_fails(tmp_path):
    js = write_review(tmp_path, spans=[{**SPANS[0], "section": "methods"}, SPANS[1]])
    init(js)
    assert "span-section-mismatch" in codes(run(js))


def test_evidence_page_is_checked_against_the_page_map(tmp_path):
    js = write_review(tmp_path, extra="Dosing was 5 mg daily (Doe et al., 2023).")
    js.with_name("review.pages.json").write_text(json.dumps(
        {"local_a1": {"pages": ["We decoded choices.",
                                "We recruited 120 participants from two clinics and gave 5 mg daily."]}}))
    init(js)
    judge(js, **{"91.2%": SUPPORTED, "Dosing": {"support": "supported", "evidence": [
        {"study": "local_a1", "quote": "gave 5 mg daily", "page": 1}]}})
    assert "evidence-page-mismatch" in codes(run(js))
    judge(js, **{"Dosing": {"support": "supported", "evidence": [
        {"study": "local_a1", "quote": "gave 5 mg daily", "page": 2}]}})
    assert "evidence-page-mismatch" not in codes(run(js))


def test_page_without_a_page_map_is_unverifiable(tmp_path):
    js = write_review(tmp_path, extra="Dosing was 5 mg daily (Doe et al., 2023).")
    init(js)
    judge(js, **{"91.2%": SUPPORTED, "Dosing": {"support": "supported", "evidence": [
        {"study": "local_a1", "quote": "gave 5 mg daily", "page": 2}]}})
    assert "evidence-page-unverifiable" in codes(run(js))


def test_unit_must_match(tmp_path):
    js = write_review(tmp_path, extra="Dosing was 5 g daily (Doe et al., 2023).")
    init(js)
    judge(js, **{"91.2%": SUPPORTED, "Dosing": {"support": "supported", "evidence": [
        {"study": "local_a1", "quote": "gave 5 mg daily"}]}})
    assert "number-ungrounded" in codes(run(js))


def test_denominator_quoted_with_its_count_passes(tmp_path):
    js = write_review(tmp_path, extra="Responders improved in 12 of 40 cases (Doe et al., 2023).")
    init(js)
    judge(js, **{"91.2%": SUPPORTED, "Responders": {"support": "supported", "evidence": [
        {"study": "local_a1", "quote": "12 of 40 responders improved"}]}})
    assert "denominator-unpaired" not in codes(run(js))


def test_denominator_from_another_sentence_fails(tmp_path):
    js = write_review(tmp_path, extra="Responders improved in 12 of 120 cases (Doe et al., 2023).")
    init(js)
    judge(js, **{"91.2%": SUPPORTED, "Responders": {"support": "supported", "evidence": [
        {"study": "local_a1", "quote": "12 of 40 responders improved"},
        {"study": "local_a1", "quote": "We recruited 120 participants"}]}})
    assert "denominator-unpaired" in codes(run(js))


def test_n_slash_m_percentage_is_recomputed(tmp_path):
    js = write_review(tmp_path, extra="Full text was read for 1/2 studies (40%).")
    init(js)
    assert "percentage-mismatch" in codes(run(js))


# ── evidence coverage and spans ──────────────────────────────────────

def test_every_study_needs_verbatim_evidence(tmp_path):
    js = write_review(tmp_path, spans=[SPANS[0]])
    init(js)
    assert "study-without-evidence" in codes(run(js))


@pytest.mark.parametrize("span,code", [
    ({"paper_pmid": "local_a1", "text": "the graph has 258.4 million edges", "section": "results"},
     "span-not-in-source"),
    ({"paper_pmid": "local_a1", "text": "accuracy of 91.2%", "section": "results", "doi": "10.1101/old"},
     "span-other-version"),
])
def test_bad_spans_fail(tmp_path, span, code):
    js = write_review(tmp_path, spans=SPANS + [span])
    init(js)
    assert code in codes(run(js))


# ── structured values: charting, appraisal, RoB, GRADE ───────────────

def rubric_review(tmp_path):
    rubric = {"source_id": "S-001", "title": "Decoding study A", "doi": "10.1000/a",
              "study_design": "Cohort", "n_disordered": "120", "summary_key_findings": "Accuracy 91.2%",
              "dataset_openly_available": "Not reported"}
    grade = {"accuracy": {"outcome": "accuracy", "overall_certainty": "Moderate",
                          "domains": {"imprecision": {"rating": "Serious", "explanation": "one study of 120"}}}}
    appraisal = [{"source_id": "S-001", "domains": [
        {"domain_name": "Sample", "domain_concern": "Low",
         "item_ratings": [{"item_text": "Sample described?", "rating": "Yes"}]}]}]
    return write_review(tmp_path, review_extra={"data_charting_rubrics": [rubric], "grade_assessments": grade,
                                                "structured_appraisal_results": appraisal})


def test_every_structured_value_needs_evidence(tmp_path):
    js = rubric_review(tmp_path)
    init(js)
    judge(js, **{"91.2%": SUPPORTED})
    rep = run(js)
    kinds = {f["location"] for f in rep["findings"] if f["code"] == "structured-ungrounded"}
    assert {"charting", "appraisal", "grade"} <= kinds and rep["summary"]["verdict"] == "FAIL"


def test_grounded_values_pass_and_ungrounded_ones_are_removed(tmp_path):
    js = rubric_review(tmp_path)
    review = json.loads(js.read_text())
    items = ee.verify_items(review, {"items": []})
    by_label = {i["label"]: i for i in items}
    by_label["n_disordered"]["evidence"] = [{"study": "local_a1", "quote": "We recruited 120 participants"}]
    by_label["summary_key_findings"]["evidence"] = [{"study": "local_a1", "quote": "an accuracy of 91.2%"}]
    by_label["study_design"]["evidence"] = [{"study": "local_a1", "quote": "a cohort we invented"}]
    checked = {i["label"]: i for i in ee.verify_items(review, {"items": items})}
    assert checked["n_disordered"]["status"] == "grounded"
    assert checked["summary_key_findings"]["status"] == "grounded"
    assert checked["study_design"]["status"] == "ungrounded"
    assert checked["dataset_openly_available"]["status"] == "absence"
    out, removed = ee.apply_grounding(review, list(checked.values()))
    r = out["data_charting_rubrics"][0]
    assert r["study_design"] == gc.UNGROUNDED and r["n_disordered"] == "120"
    assert out["structured_appraisal_results"][0]["domains"][0]["item_ratings"] == []
    g = out["grade_assessments"]["accuracy"]
    assert g["overall_certainty"] == "Not assessed" and "imprecision" not in g["domains"]
    assert {x["label"] for x in removed} >= {"study_design", "GRADE accuracy: imprecision"}


def test_structured_value_number_must_be_in_its_quote(tmp_path):
    js = rubric_review(tmp_path)
    review = json.loads(js.read_text())
    items = ee.verify_items(review, {"items": []})
    for i in items:
        if i["label"] == "summary_key_findings":
            i["evidence"] = [{"study": "local_a1", "quote": "We recruited 120 participants"}]
    checked = {i["label"]: i for i in ee.verify_items(review, {"items": items})}
    assert checked["summary_key_findings"]["status"] == "ungrounded"
    assert "91.2%" in checked["summary_key_findings"]["reason"]


def test_structured_quote_in_the_wrong_section_does_not_ground(tmp_path):
    js = rubric_review(tmp_path)
    review = json.loads(js.read_text())
    items = ee.verify_items(review, {"items": []})
    for i in items:
        if i["label"] == "n_disordered":
            i["evidence"] = [{"study": "local_a1", "quote": "We recruited 120 participants", "section": "results"}]
    checked = {i["label"]: i for i in ee.verify_items(review, {"items": items})}
    assert checked["n_disordered"]["status"] == "ungrounded"


# ── version changes ──────────────────────────────────────────────────

def version_change(js: Path) -> None:
    js.with_name("review.revalidation.json").write_text(json.dumps({"changes": [
        {"pmid": "local_a1", "old_doi": "10.1000/a", "new_doi": "10.2000/pub",
         "old_text_sha256": gc.text_sha256(A), "recorded_at": "2026-06-01T00:00:00Z"}]}))


def test_relabelling_old_text_does_not_clear_a_version_change(js):
    init(js)
    judge(js, **{"91.2%": SUPPORTED})
    version_change(js)
    data = json.loads(js.read_text())   # relabel only: same text, new DOI on article and spans
    data["included_articles"][0]["doi"] = "10.2000/pub"
    for s in data["evidence_spans"]:
        if s["paper_pmid"] == "local_a1":
            s["doi"] = "10.2000/pub"
    js.write_text(json.dumps(data))
    msg = next(f["message"] for f in run(js)["findings"] if f["code"] == "version-changed")
    assert "same SHA-256" in msg and "refresh" in msg


def test_new_text_with_a_logged_refresh_clears_the_version_change(js):
    init(js)
    judge(js, **{"91.2%": SUPPORTED})
    version_change(js)
    data = json.loads(js.read_text())
    art = data["included_articles"][0]
    art.update(doi="10.2000/pub", full_text=FULL + "\nPublished version, doi 10.2000/pub.",
               full_text_retrieved_at="2026-07-01T00:00:00Z")
    for s in data["evidence_spans"]:
        if s["paper_pmid"] == "local_a1":
            s["doi"] = "10.2000/pub"
    js.write_text(json.dumps(data))
    js.with_name("review.extraction.json").write_text(json.dumps({"runs": [
        {"pmid": "local_a1", "doi": "10.2000/pub", "text_sha256": gc.text_sha256(art),
         "extracted_at": "2026-07-02T00:00:00Z",
         "stages": ["spans", "risk_of_bias", "charting", "appraisal", "narrative"]}]}))
    assert "version-changed" not in codes(run(js))


def test_apply_records_version_switches_with_the_old_text_hash(tmp_path):
    applied = [{"key": "k", "field": "doi", "old": "10.1101/pre", "new": "10.1000/pub",
                "version_switch": True, "study": "local_a1", "old_text_sha256": "abc"}]
    ch = json.loads(vc.record_version_changes(applied, tmp_path / "review.json").read_text())["changes"][0]
    assert ch["new_doi"] == "10.1000/pub" and ch["old_text_sha256"] == "abc" and ch["recorded_at"]


# ── studies vs reports ───────────────────────────────────────────────

def test_reports_sharing_a_trial_id_must_be_resolved(js):
    js.with_name("review.studies.json").unlink()
    init(js)
    assert "study-overlap-unresolved" in codes(run(js))


def test_grouped_reports_count_as_one_study(tmp_path):
    js = write_review(tmp_path, extra="We included 1 studies after grouping reports.")
    js.with_name("review.studies.json").write_text(json.dumps(
        {"studies": [{"study_id": "ST-1", "reports": ["local_a1", "local_b2"], "basis": "NCT01234567"}]}))
    init(js)
    rep = run(js)
    assert "study-overlap-unresolved" not in codes(rep) and rep["studies"]["studies"] == 1
    assert "count-mismatch" not in codes(rep)


# ── delivered text, appendices and the PDF gate ──────────────────────

def charting_sidecar(js: Path, body: str = "| Study | n |\n| --- | --- |\n| A | 120 |\n") -> Path:
    side = js.with_name("review.charting.md")
    side.write_text("# Data charting\n\n" + body)
    return side


def test_prepare_delivery_includes_and_marks_appendices(js):
    charting_sidecar(js)
    path, text = generate_review_pdf.prepare_delivery(js.with_name("review.manuscript.md"), "typst", None, ["charting"])
    assert path.name == "review.delivered.md" and "<!-- sidecar: review.charting.md sha256=" in text


def test_changed_appendix_is_caught(js):
    side = charting_sidecar(js)
    path, _ = generate_review_pdf.prepare_delivery(js.with_name("review.manuscript.md"), "typst", None, ["charting"])
    side.write_text("# Data charting\n\n| Study | n |\n| --- | --- |\n| A | 999 |\n")
    assert "sidecar-changed" in codes(ag.audit(js, path))


def test_appendix_must_be_what_the_exporter_renders(js):
    charting_sidecar(js, "A hand-edited appendix claiming 99% cure.\n")
    path, _ = generate_review_pdf.prepare_delivery(js.with_name("review.manuscript.md"), "typst", None, ["charting"])
    assert {"sidecar-not-from-review", "sidecar-unverifiable"} & codes(ag.audit(js, path))


def test_pdf_gate_requires_a_passing_current_audit(js):
    md = js.with_name("review.manuscript.md")
    refusal, _ = generate_review_pdf.grounding_gate(md, allow=False)
    assert refusal and "INCOMPLETE" in refusal
    init(js)
    judge(js, **{"91.2%": SUPPORTED})
    assert generate_review_pdf.grounding_gate(md, allow=False) == (None, None)
    md.write_text(md.read_text() + "\nAnother claim of 9 cohorts (Doe et al., 2023).\n")
    refusal, _ = generate_review_pdf.grounding_gate(md, allow=False)
    assert refusal
    refusal, notice = generate_review_pdf.grounding_gate(md, allow=True)
    assert refusal is None and "Grounding not verified" in notice


def test_not_empirical_cannot_hide_numbers(tmp_path):
    js = write_review(tmp_path, extra="This review aims to map the field.\n\nCure rates were 99%.")
    init(js)
    judge(js, **{"91.2%": SUPPORTED,
                 "aims": {"support": "not_empirical", "explanation": "aim of the review"},
                 "Cure": {"support": "not_empirical", "explanation": "context"}})
    rep = run(js)
    assert "not-empirical-misused" in codes(rep) and "not-empirical" in codes(rep)


# ── resource-catalog citations and fallback registries ───────────────

def test_resource_ids_credit_their_studies_and_unknown_links_fail():
    arts = [{"pmid": "local_R01", "doi": "", "title": "T", "authors": "A", "year": "2020"}]
    rep = vc.Verifier(None).verify_manuscript(
        {"included_articles": arts}, "Graphs grew (NKG-001; NKG-009).\n",
        catalog=[{"resource_id": "NKG-001", "publication_ids": ["R01"]},
                 {"resource_id": "NKG-002", "publication_ids": ["R77"]}])
    found = {f["code"] for f in rep["in_text"] + rep["review_findings"]}
    assert {"resource-unknown", "catalog-publication-unknown"} <= found and "uncited-included" not in found


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
    assert rep["summary"]["verdict"] == "INCOMPLETE" and "retraction-unchecked" in rep["summary"]["by_code"]


# ── releases: the manuscript (and PDF) saved as numbered versions ────

class _Registry:
    """Every DOI resolves to the study it is attached to."""

    def __init__(self, **kw):
        pass

    def lookup_doi(self, doi, check_updates=True):
        art = A if doi.lower() == A["doi"] else B
        return "crossref", {"registry": "crossref", "doi": doi, "title": art["title"],
                            "authors": [(art["authors"].split()[0], "")], "year": int(art["year"]),
                            "years": [int(art["year"])], "journal": "J", "type": "journal-article",
                            "updates": [], "notice_for": [], "published_as": [], "has_preprint": []}

    def lookup_pmids(self, pmids):
        return {}

    def __getattr__(self, name):
        return lambda *a, **k: [] if name != "pmid_for_doi" else None


def deliver(js: Path, **kw) -> bool:
    return generate_review_pdf.generate_pdf(str(js.with_name("review.manuscript.md")), no_pdf=True, **kw)


def ready(js: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vc, "Registry", _Registry)
    assert generate_review_pdf.generate_pdf(str(js.with_name("review.manuscript.md")), prepare_only=True)
    delivered = js.with_name("review.delivered.md")
    ag.init_claims(json.loads(js.read_text()), delivered, {}, js.with_name("review.claims.json"))
    judge(js, **{"91.2%": SUPPORTED})
    return delivered


def test_a_passing_delivery_is_released_as_numbered_versions(js, monkeypatch):
    ready(js, monkeypatch)
    assert deliver(js)
    assert deliver(js)
    rel = js.parent / "releases"
    v1, v2 = rel / "review-v1", rel / "review-v2"
    m1 = json.loads((v1 / "manifest.json").read_text())
    m2 = json.loads((v2 / "manifest.json").read_text())
    assert m1["verified"] and m1["citations_verdict"] == "PASS" and m1["grounding_verdict"] == "PASS"
    assert (v1 / "review.manuscript.md").read_text() == js.with_name("review.delivered.md").read_text()
    assert m2["previous_version"] == "review-v1"
    assert m2["previous_manifest_sha256"] == __import__("hashlib").sha256((v1 / "manifest.json").read_bytes()).hexdigest()
    assert all(__import__("hashlib").sha256((v1 / n).read_bytes()).hexdigest() == h for n, h in m1["files"].items())


def test_a_failing_delivery_is_not_released(js, monkeypatch):
    monkeypatch.setattr(vc, "Registry", _Registry)
    assert not deliver(js)                       # claims never judged → grounding INCOMPLETE
    assert not (js.parent / "releases").exists() or not any((js.parent / "releases").iterdir())


def test_an_override_delivery_is_released_marked_unverified(js, monkeypatch):
    monkeypatch.setattr(vc, "Registry", _Registry)
    assert deliver(js, allow_ungrounded=True)
    rel = next((js.parent / "releases").iterdir())
    m = json.loads((rel / "manifest.json").read_text())
    assert rel.name.endswith("-UNVERIFIED") and not m["verified"] and "Grounding not verified" in m["override_notice"]


# ── deterministic locations ──────────────────────────────────────────

def test_locate_relabels_spans_by_where_the_text_is(tmp_path):
    js = write_review(tmp_path, spans=[{**SPANS[0], "section": "methods"}, SPANS[1]])
    review = json.loads(js.read_text())
    ledger = {"items": []}
    stats = ee.locate_all(review, ledger, {"local_a1": ["We decoded choices.", FULL]})
    sp = review["evidence_spans"][0]
    assert stats["relabelled"] >= 1 and sp["section"] == "results" and sp["page"] == 2
    assert sp["source_sha256"] == gc.text_sha256(A) and sp["offset"] == FULL.find(sp["text"])
    assert sp["offset_in"] == "full_text"


def test_apply_drops_spans_that_are_not_verbatim(tmp_path):
    js = write_review(tmp_path, spans=SPANS + [{"paper_pmid": "local_a1", "text": "invented 258.4 million edges"}])
    review = json.loads(js.read_text())
    out, removed = ee.apply_grounding(review, ee.verify_items(review, {"items": []}))
    assert len(out["evidence_spans"]) == 2 and any(r["kind"] == "evidence_span" for r in removed)


def test_dates_are_one_token_not_quantities():
    assert [q["text"] for q in gc.quantities("Searches ran on 2026-10-15; 12 of 40.")] == ["12", "40"]
    assert gc.verbatim("ran on 2026/10/15", "searches ran on 2026-10-15 daily")
    assert not gc.verbatim("ran on 2026-10-15", "searches ran on 2026-10-16 daily")



# ── full text only ───────────────────────────────────────────────────

def abstract_only(js: Path) -> None:
    data = json.loads(js.read_text())
    data["included_articles"][1]["full_text"] = ""
    data["flow"].update(assessed_eligibility=2, excluded_eligibility=0, not_retrieved=0,
                        assessed_on_abstract_only=1, included_abstract_only=1)
    js.write_text(json.dumps(data))


def test_an_included_study_without_full_text_blocks_delivery(js):
    init(js)
    judge(js, **{"91.2%": SUPPORTED})
    abstract_only(js)
    rep = run(js)
    assert "included-without-full-text" in codes(rep) and rep["summary"]["verdict"] == "FAIL"


def test_an_abstract_only_study_cannot_supply_later_sections(tmp_path):
    js = write_review(tmp_path, spans=SPANS + [
        {"paper_pmid": "local_b2", "text": "Follow-up of trial NCT01234567", "section": "results"}])
    abstract_only(js)
    init(js)
    assert "span-reading-basis" in codes(run(js))


def test_apply_moves_abstract_only_studies_to_not_retrieved(tmp_path):
    js = rubric_review(tmp_path)
    abstract_only(js)
    data = json.loads(js.read_text())
    data["data_charting_rubrics"].append({"source_id": "S-002", "title": "Follow-up study B", "doi": "10.1000/b"})
    data["narrative_rows"] = [{"source_id": "S-001", "methods": "x"}, {"source_id": "S-002", "methods": "y"}]
    out, removed = ee.apply_grounding(data, ee.verify_items(data, {"items": []}))
    assert [a["pmid"] for a in out["included_articles"]] == ["local_a1"]
    assert [r["source_id"] for r in out["data_charting_rubrics"]] == ["S-001"]
    assert [r["source_id"] for r in out["narrative_rows"]] == ["S-001"]
    assert all(s["paper_pmid"] != "local_b2" for s in out["evidence_spans"])
    f = out["flow"]
    assert (f["included_synthesis"], f["not_retrieved"], f["assessed_eligibility"], f["included_abstract_only"]) == (1, 1, 1, 0)
    assert f["assessed_eligibility"] - f["excluded_eligibility"] == f["included_synthesis"]
    assert any(r["kind"] == "study" and r["study"] == "local_b2" for r in removed)


def test_corpus_entries_without_text_never_reach_the_pipeline():
    import run_local_review as rl
    src = Path(rl.__file__).read_text()
    assert "unreadable = [a for a in articles if not (a.full_text or \"\").strip()]" in src
    assert "articles = [a for a in articles if (a.full_text or \"\").strip()]" in src


def test_eligibility_decided_on_abstracts_counts_as_not_retrieved():
    log = ([{"pmid": f"f{i}", "stage": "full_text", "decision": "include", "assessed_on": "full_text"} for i in range(3)]
           + [{"pmid": f"x{i}", "stage": "full_text", "decision": "exclude", "assessed_on": "full_text"} for i in range(2)]
           + [{"pmid": f"a{i}", "stage": "full_text", "decision": "include", "assessed_on": "abstract_only"} for i in range(4)]
           + [{"pmid": f"b{i}", "stage": "full_text", "decision": "exclude", "assessed_on": "abstract_only"} for i in range(5)])
    review = {"included_articles": [{"pmid": f"f{i}", "full_text": "text"} for i in range(3)]
              + [{"pmid": f"a{i}", "full_text": ""} for i in range(4)],
              "screening_log": log,
              "flow": {"sought_fulltext": 14, "not_retrieved": 9, "assessed_eligibility": 14,
                       "excluded_eligibility": 7, "included_synthesis": 7, "assessed_on_abstract_only": 9}}
    ee.drop_without_full_text(review)
    f = review["flow"]
    assert (f["assessed_eligibility"], f["excluded_eligibility"], f["included_synthesis"], f["not_retrieved"]) == (5, 2, 3, 9)
    assert f["sought_fulltext"] - f["not_retrieved"] == f["assessed_eligibility"]
    assert f["assessed_eligibility"] - f["excluded_eligibility"] == f["included_synthesis"]


def test_extraction_refuses_without_rapidfuzz(monkeypatch):
    # the app would score every span as grounded; the skill does not run it
    monkeypatch.setitem(sys.modules, "rapidfuzz", None)
    with pytest.raises(SystemExit) as e:
        ee._deps({}, type("A", (), {"api_key": "k", "model": "m"})())
    assert "rapidfuzz" in str(e.value)
