"""Unit tests for the structsense ABCD sentence layer and its checks.

Covers scripts/abcd_sentences.py and the parts of scripts/abcd_verify.py that use
it: section headings read from the paper (numbered, unnumbered, letter-spaced,
APA papers with no Introduction heading, back matter that the column layout puts
in the middle of the Discussion), section roles for the scope gate, abbreviations
and network pairs as surface forms of a variable, quotes that refer back ("These
models ..."), significance, the change-over-time direction convention, prediction
claims and the crosswalk to a human claim scheme. Standard library only.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "structsense"))

from scripts import abcd_sentences as S  # noqa: E402
from scripts.abcd_verify import TextIndex, verify_payload  # noqa: E402

PAPER = """Journal of Testing 12 (2025) 1-10

A B S T R A C T

We studied prenatal tobacco exposure (PTE) in the ABCD cohort. PTE was associated with higher scores.

Keywords: tobacco, connectivity

1. Introduction

Prior work linked smoking in pregnancy to attention problems (Smith et al., 2020). The present study tests this in a large cohort.

2. Methods

2.1. Participants

A final cohort of 6674 subjects was used. An FDR of < 0.05 was considered significant.

3. Findings

With PTE, there was lower connectivity within the default mode network and higher connectivity between the default mode and dorsal attention networks (Table 5).

These models can also mutually predict each other, with correlations ranging from .03 to .08.

The correlation at 4th year was r = .14, p = .01, smaller than at baseline (r = .20).

Funding

This work was supported by grant 12345 from the Foundation.

Another of our findings worthy of discussion is that the correlation between the two tasks was higher at baseline; by comparing baseline and the 4th year data, we found a decreased similarity between the tasks, suggesting that the components differentiate during these years of adolescence.

References

Smith, A. B., Jones, C. 2020. Smoking and attention. Journal 3: 1-10.
"""


class TestOutline(unittest.TestCase):
    def setUp(self):
        self.walked = S.walk(TextIndex(PAPER).nfkc)
        self.heads = {h["title"]: h for h in self.walked["outline"].headings}

    def test_headings_are_read_from_the_paper(self):
        for title in ("Abstract", "Introduction", "Methods", "Participants", "Findings",
                      "Funding", "References"):
            self.assertIn(title, self.heads, title)
        # The journal masthead before the abstract is not a heading.
        self.assertFalse(any("Journal of Testing" in t for t in self.heads))

    def test_roles_without_a_fixed_list(self):
        self.assertEqual(self.heads["Findings"]["role"], "own_study")
        self.assertEqual(self.heads["Participants"]["role"], "own_study")
        self.assertEqual(self.heads["Introduction"]["role"], "introduction")
        self.assertEqual(self.heads["Funding"]["role"], "back_matter")

    def test_discussion_after_funding_is_kept(self):
        texts = [s["text"] for s in self.walked["sentences"]]
        self.assertTrue(any(t.startswith("Another of our findings") for t in texts))
        self.assertFalse(any("supported by grant" in t for t in texts))
        self.assertFalse(any(t.startswith("Smith, A. B.") for t in texts))

    def test_numbered_matrix_rows_are_not_headings(self):
        text = ("Methods\n\nWe used a model.\n\nResults\n\nTable 1\n\n1 Financial strain (1)\n\n"
                "2 Family conflict (1)\n\n3 Family conflict (2)\n\nDiscussion\n\n"
                "Strain predicted conflict in this study, as expected from the model.\n")
        titles = [h["title"] for h in S.outline(text).headings]
        self.assertNotIn("Financial strain (1)", titles)
        self.assertIn("Discussion", titles)


class TestSurfaceForms(unittest.TestCase):
    def test_abbreviation_and_network_pair(self):
        text = ("Methods\n\nPrenatal tobacco exposure (PTE) was coded from interviews. "
                "Networks included the default mode network (DMN) and the dorsal attention "
                "network (DAN).\n\nResults\n\nPTE was associated with connectivity between "
                "the DMN and DAN in this study.\n")
        payload = {
            "variables": [
                {"name": "prenatal tobacco exposure", "evidence": {"quote": "Prenatal tobacco exposure (PTE) was coded from interviews", "section": "Methods"}},
            ],
            "findings": [{
                "statement": "PTE was associated with DMN-DAN connectivity.",
                "direction": "unspecified",
                "variables": ["prenatal tobacco exposure", "Default mode–Dorsal attention"],
                "evidence": {"quote": "PTE was associated with connectivity between the DMN and DAN in this study.", "section": ""},
            }],
            "constructs": [], "models": [],
        }
        out = verify_payload(payload, text)
        self.assertEqual(len(out["findings"]), 1, out["rejected"])
        ev = out["findings"][0]["evidence"]
        self.assertEqual(ev["referenced_as"]["prenatal tobacco exposure"], "pte")
        self.assertIn("dmn", ev["referenced_as"]["Default mode–Dorsal attention"])
        # The empty section label did not matter: the paper's heading decided.
        self.assertEqual(out["findings"][0]["scope_signals"]["section_class_from"], "paper heading")

    def test_quote_that_refers_back(self):
        text = ("Results\n\nCPM models of the Flanker task predicted performance in this study. "
                "These models can also mutually predict each other, with correlations ranging "
                "from .03 to .08.\n")
        payload = {
            "variables": [{"name": "Flanker", "evidence": {"quote": "CPM models of the Flanker task predicted performance", "section": "Results"}}],
            "findings": [{
                "statement": "Models predicted each other.", "direction": "positive",
                "variables": ["Flanker"],
                "evidence": {"quote": "These models can also mutually predict each other, with correlations ranging from .03 to .08.", "section": "Results"},
            }],
            "constructs": [], "models": [],
        }
        out = verify_payload(payload, text)
        self.assertEqual(len(out["findings"]), 1, out["rejected"])
        self.assertEqual(out["findings"][0]["evidence"]["variables_named_in_previous_sentence"],
                         ["Flanker"])


class TestClaimLanguage(unittest.TestCase):
    def test_significance(self):
        self.assertIs(S.significance("r = .50 (p = 1.35e-1)")["significant"], False)
        self.assertIs(S.significance("r = .79 (p = 2.76e-2)")["significant"], True)
        self.assertEqual(S.significance("p = .003/.47 and p = .47")["significant"], "partial")
        self.assertIs(S.significance("was not significantly associated")["significant"], False)
        self.assertEqual(S.paper_alpha("An FDR of < 0.01 was considered significant."), 0.01)

    def test_change_over_time_convention(self):
        cases = {
            "significantly higher than .14 of 4th year (p = 6.9e-03)": "negative",
            "at 4th year was r = .14, still significant but smaller than at baseline": "negative",
            "Most brain regions remained in the 4th year.": "null",
            "some regions only showed in 4th year later such as the frontal sulcus": "positive",
            "Family conflict decreased over time.": "negative",
            "The three tasks were correlated at baseline, all p < .001.": None,
        }
        for text, want in cases.items():
            self.assertEqual(S.change_over_time(text), want, text)

    def test_sign_from_statistics(self):
        self.assertEqual(S.sign_from_statistics("BMI and List were correlated, r = -.03"), "negative")
        self.assertEqual(S.sign_from_statistics("with correlations ranging from .03 to .08"), "positive")
        self.assertIsNone(S.sign_from_statistics("PTE was significantly associated with all scales"))

    def test_prediction_and_crosswalk(self):
        self.assertEqual(S.human_claim_type("correlation", "The correlation between predicted and actual performance ranged from .03 to .09"), "prediction")
        self.assertEqual(S.human_claim_type("associative", "Food insecurity was a strong predictor of poor scores"), "association")
        self.assertEqual(S.human_claim_type("comparison", "Boys had poorer scores than girls"), "group_difference")
        self.assertEqual(S.human_claim_type("associative", "Family conflict decreased over time"), "change_over_time")

    def test_hedging_and_causal_language(self):
        self.assertTrue(S.hedged("These results suggest PTE to be associated with networks."))
        self.assertFalse(S.hedged("PTE was associated with networks."))
        self.assertEqual(S.causal_language("PTE may impact brain networks"), "conditional_causal")
        self.assertEqual(S.causal_language("A college degree did not impact scores"), "correlational")


if __name__ == "__main__":
    unittest.main()
