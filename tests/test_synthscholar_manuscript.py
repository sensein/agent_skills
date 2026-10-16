"""Regression checks for SynthScholar's manuscript assembler
(skills/synthscholar/scripts/assemble_manuscript.py). Stdlib only."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "synthscholar" / "scripts"))

import assemble_manuscript  # noqa: E402


class PerGroupSummaryTests(unittest.TestCase):
    """GroupAnalysisEntry.summary is a GroupSummary object, so the manuscript
    must print its finding, not the dict."""

    def test_summary_object_renders_as_text(self):
        text = assemble_manuscript._group_summary({
            "label": "Chemotherapy sample", "n_studies": 3,
            "aggregate_finding": "Three of three studies reported lower anxiety.",
            "representative_pmids": ["1", "2", "3"], "caveats": "Abstracts only."})
        self.assertEqual(text, "Three of three studies reported lower anxiety. Caveats: Abstracts only.")
        self.assertNotIn("{", text)

    def test_summary_without_caveats_or_as_plain_string(self):
        self.assertEqual(assemble_manuscript._group_summary(
            {"aggregate_finding": "Finding.", "caveats": ""}), "Finding.")
        self.assertEqual(assemble_manuscript._group_summary("  Already text.  "), "Already text.")
        self.assertEqual(assemble_manuscript._group_summary(None), "")

    def test_assembled_manuscript_has_no_raw_dict(self):
        review = {"protocol": {"title": "T", "objective": "O"}, "flow": {},
                  "per_group_analysis": {"dimension": "disorder_cohort", "groups": [{
                      "label": "Group A", "n_studies": 2,
                      "summary": {"label": "Group A", "n_studies": 2,
                                  "aggregate_finding": "Two of two studies agreed.",
                                  "representative_pmids": [], "caveats": ""},
                      "answers": []}]}}
        with tempfile.TemporaryDirectory() as tmp:
            m = assemble_manuscript.Manuscript(review, {"review_type": "effect"}, [], Path(tmp),
                                               "review", None, write_files=False)
            md = m.build()
        self.assertIn("**Group A** (n = 2). Two of two studies agreed.", md)
        self.assertNotIn("'aggregate_finding'", md)


if __name__ == "__main__":
    unittest.main()
