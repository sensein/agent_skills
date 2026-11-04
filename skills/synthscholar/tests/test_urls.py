"""URL references: a cited web page, report, dataset or PDF must exist and be the work named.

    python -m pytest skills/synthscholar/tests -q

The registry is a stub serving fixed pages, so these run offline.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import verify_citations as vc  # noqa: E402

LINE = "WHO (2021). Guidelines on physical activity and sedentary behaviour. https://example.org/who-guidelines"


class Pages:
    """``pages`` maps a URL to a fetch result; ``archive`` maps a URL to its snapshot URL."""

    def __init__(self, pages: dict, archive: dict | None = None):
        self.pages, self.archive = pages, archive or {}

    def fetch_page(self, url):
        return self.pages.get(url, {"url": url, "status": 404})

    def wayback(self, url):
        return self.archive.get(url)

    def lookup_doi(self, doi, check_updates=True):
        return "not-found", {}

    def lookup_pmids(self, pmids):
        return {}

    def __getattr__(self, name):
        return lambda *a, **k: [] if name != "pmid_for_doi" else None


def page(title="", text="", status=200, soft_404=False, kind="html"):
    return {"status": status, "title": title, "text": text, "soft_404": soft_404, "kind": kind,
            "final_url": "x", "content_type": "text/html"}


URL = "https://example.org/who-guidelines"
GOOD = page("Guidelines on physical activity and sedentary behaviour")


def codes(rec):
    return {f["code"] for f in rec["findings"]}


def check(pages, archive=None, claimed=LINE, **kw):
    return vc.Verifier(Pages(pages, archive)).verify_url(URL, claimed, **kw)


def test_page_that_is_the_cited_work_verifies():
    rec = check({URL: GOOD})
    assert rec["status"] == "verified" and not rec["findings"]


def test_page_that_is_another_work_fails():
    rec = check({URL: page("Welcome to Example Domain", "This domain is for use in examples.")})
    assert "url-title-mismatch" in codes(rec) and rec["status"] == "failed"


def test_missing_page_without_archive_fails():
    assert "url-not-found" in codes(check({}))


def test_soft_404_counts_as_missing():
    assert "url-not-found" in codes(check({URL: page("Page not found", soft_404=True)}))


def test_dead_page_with_archived_copy_is_verified_against_the_copy():
    snap = "https://web.archive.org/web/2021/" + URL
    rec = check({snap: GOOD}, {URL: snap})
    assert "url-dead-archived" in codes(rec) and rec["status"] == "warnings"
    assert rec["registry"]["archived_copy"] == snap


def test_blocked_page_with_archive_verifies_via_the_archive():
    snap = "https://web.archive.org/web/2021/" + URL
    rec = check({URL: {"url": URL, "status": 403}, snap: GOOD}, {URL: snap})
    assert codes(rec) == {"url-via-archive"} and rec["status"] == "verified"


def test_blocked_page_without_archive_is_unverified_not_passed():
    rec = check({URL: {"url": URL, "status": 403}})
    assert "url-unreachable" in codes(rec)
    assert "url-unreachable" in vc.UNVERIFIED_CODES


def test_pdf_without_title_is_checked_against_its_text():
    ok = check({URL: page("", "Guidelines on physical activity and sedentary behaviour. WHO 2021", kind="pdf")})
    assert ok["status"] == "verified"
    bad = check({URL: page("", "Quarterly financial statement of an unrelated company", kind="pdf")})
    assert "url-content-unchecked" in codes(bad)


def test_offline_url_is_unverified():
    rec = vc.Verifier(None).verify_url(URL, LINE)
    assert "url-unchecked" in codes(rec) and rec["status"] == "offline"


def gate(text: str, pages: dict, review=None):
    return vc.gate(review, Path("/tmp/urltest"), manuscript=text, registry=Pages(pages))


def test_manuscript_url_reference_is_verified_and_a_wrong_one_fails(tmp_path):
    text = f"# R\n\n## 1. Introduction\n\nActivity guidance exists (WHO, 2021).\n\n## References\n\n- {LINE}\n"
    rep = vc.gate(None, tmp_path / "a", manuscript=text, registry=Pages({URL: GOOD}))
    assert rep["summary"]["verdict"] == "PASS"
    rep = vc.gate(None, tmp_path / "b", manuscript=text,
                  registry=Pages({URL: page("Buy cheap watches", "cheap watches")}))
    assert rep["summary"]["verdict"] == "FAIL"


def test_reference_without_any_identifier_is_unverified(tmp_path):
    text = "# R\n\n## References\n\n- Jones K (2019). A book with no identifier at all. Oxford University Press.\n"
    rep = vc.gate(None, tmp_path / "c", manuscript=text, registry=Pages({}))
    assert rep["summary"]["verdict"] == "INCOMPLETE"
    assert "reference-unidentified" in rep["summary"]["by_code"]


def test_included_study_identified_only_by_url_is_verified_through_its_entry(tmp_path):
    review = {"included_articles": [{"pmid": "local_w1", "doi": "", "authors": "WHO", "year": "2021",
                                     "title": "Guidelines on physical activity and sedentary behaviour"}],
              "screening_log": []}
    text = f"# R\n\n## 3. Results\n\nGuidance (WHO, 2021).\n\n## References\n\n- {LINE}\n"
    rep = vc.gate(review, tmp_path / "d", manuscript=text, registry=Pages({URL: GOOD}))
    own = next(r for r in rep["references"] if r["key"] == "local_w1")
    assert "doi-missing" not in codes(own) and own["status"] == "verified"
    assert rep["summary"]["verdict"] == "PASS"


def test_review_article_with_url_field_is_verified_by_url(tmp_path):
    review = {"included_articles": [{"pmid": "local_w1", "doi": "", "url": URL, "authors": "WHO", "year": "2021",
                                     "title": "Guidelines on physical activity and sedentary behaviour"}]}
    rep = vc.gate(review, tmp_path / "e", registry=Pages({URL: GOOD}))
    assert rep["summary"]["verdict"] == "PASS" and rep["references"][0]["url"] == URL


def test_dead_link_in_the_text_is_reported(tmp_path):
    text = "# R\n\n## 1. Introduction\n\nThe resource (https://example.org/gone) is offline.\n\n## References\n"
    rep = vc.gate(None, tmp_path / "f", manuscript=text, registry=Pages({}))
    assert any(f["code"] == "url-not-found" for f in rep["in_text"])


@pytest.mark.parametrize("doc,title", [
    ('<meta name="citation_title" content="A Paper"><title>Site | A Paper</title>', "A Paper"),
    ('<meta property="og:title" content="OG Title">', "OG Title"),
    ("<title>\n  Plain &amp; Simple </title>", "Plain & Simple"),
])
def test_html_title_prefers_scholarly_metadata(doc, title):
    assert vc._html_title(doc) == title
