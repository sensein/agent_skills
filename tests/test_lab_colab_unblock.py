"""`unblock` (sensein/lab-colab#37): asks waiting on the caller, ranked, with suggestions. No network."""
from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone

import test_lab_colab_threads as t  # noqa: E402  (fakes for search, comments, reviews)

colab, sync, people = t.colab, t.sync, t.people
import colab_unblock as unblock  # noqa: E402

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
DECISIONS = "@me please decide:\n| A1 | merge order |\n| A2 | keep the cache |\n| A3 | drop py3.10 |\n"


def setUpModule() -> None:
    t.setUpModule()


def tearDownModule() -> None:
    t.tearDownModule()


class UnblockGH(t.ThreadGH):
    """ThreadGH plus review-request search and single-comment reads (for decision lists)."""

    def __init__(self, review_requested=(), bodies=None, **kw):
        super().__init__(**kw)
        self.review_requested, self.bodies = list(review_requested), bodies or {}

    def get(self, path, cache=False, conditional=True):
        if path.startswith("/search/issues") and "review-requested%3A" in path and "involves" not in path:
            return {"items": [{"html_url": u} for u in self.review_requested]}
        if "/issues/comments/" in path:
            return {"body": self.bodies.get(path.rsplit("/", 1)[-1], "")}
        return super().get(path, cache, conditional)


def c(by, at, cid):
    row = t.comment(by, at)
    row["url"] = f"https://api.github.com/repos/sensein/proj/issues/comments/{cid}"
    return row


def board(n, status, lead="", spaces=("proj",), body="", contributions=""):
    return {"status": status, "lead": lead, "spaces": list(spaces), "contributions": contributions, "labels": [],
            "content": {"repository": "sensein/proj", "number": n, "url": f"https://github.com/sensein/proj/issues/{n}",
                        "title": f"item {n}", "body": body}}


def gather(gh, items=None, cfg=None):
    return unblock.gather(gh, cfg or people.pcfg(), "me", now_t=NOW, board_items=items, members={"me", "b", "kab"},
                          budget=sync.Budget(300))


class UnblockTests(unittest.TestCase):
    def test_decision_list_ranks_above_the_stack_it_holds_up(self) -> None:
        hits = [t.hit("sensein/proj", 7, by="b", comments=1)] + [t.hit("sensein/proj", n, by="b", pr=True, comments=1) for n in (8, 9, 10)]
        comments = {"sensein/proj#7": [c("b", "2026-11-22T00:00:00Z", 70)]}
        comments.update({f"sensein/proj#{n}": [c("b", "2026-11-15T00:00:00Z", n)] for n in (8, 9, 10)})
        v = gather(UnblockGH(hits=hits, comments=comments, bodies={"70": DECISIONS}), items=[])
        kinds = [a["kind"] for a in v["asks"]]
        self.assertEqual(kinds[:2], ["decisions", "stack"])
        dec, stack = v["asks"][0], v["asks"][1]
        self.assertEqual((dec["decisions"], dec["holds_up_stack"], stack["items"]), (3, "proj#8..#10", ["proj#8", "proj#9", "proj#10"]))
        self.assertIn("answer these first", dec["suggestion"])
        self.assertNotIn("merge order", json.dumps(v))  # comment text is read to classify, never returned

    def test_review_requested_and_stale_threads_get_their_own_suggestions(self) -> None:
        hits = [t.hit("sensein/proj", 3, by="kab", pr=True, updated="2026-11-01T00:00:00Z"),
                {**t.hit("sensein/proj", 4, by="b", comments=1), "created_at": "2026-08-01T00:00:00Z"}]
        gh = UnblockGH(hits=hits, review_requested=["https://github.com/sensein/proj/pull/3"],
                       comments={"sensein/proj#4": [c("b", "2026-09-01T00:00:00Z", 40)]})
        by = {a["item"]: a for a in gather(gh, items=[])["asks"]}
        self.assertEqual(by["proj#3"]["kind"], "review_requested")
        self.assertIn("decline with a reason, or delegate", by["proj#3"]["suggestion"])  # over 14 days old
        self.assertEqual(by["proj#4"]["kind"], "your_move")
        self.assertIn("close or park", by["proj#4"]["suggestion"])

    def test_my_own_last_word_is_not_an_ask(self) -> None:
        gh = UnblockGH(hits=[t.hit("sensein/proj", 5, by="b", comments=1)], comments={"sensein/proj#5": [c("me", "2026-11-22T00:00:00Z", 50)]})
        self.assertEqual(gather(gh, items=[])["asks"], [])

    def test_board_blocked_depended_on_and_proposed(self) -> None:
        items = [board(20, "Blocked", lead="me"), board(21, "In progress", lead="me"),
                 board(22, "Ready", body="```links\ndepends-on: sensein/proj#21\n```"),
                 board(23, "Ready", body="```links\ndepends-on: sensein/proj#21\n```"),
                 board(24, "Proposed", spaces=("infra",), contributions="kab: request"),
                 board(25, "Proposed", spaces=("proj",))]  # a space I don't steward
        by = {a["item"]: a for a in gather(UnblockGH(), items=items)["asks"]}
        self.assertEqual(by["proj#20"]["kind"], "blocked")
        self.assertEqual((by["proj#21"]["kind"], by["proj#21"]["holds_up"], by["proj#21"]["depended_on_by"]),
                         ("depended_on", 2, ["proj#22", "proj#23"]))
        self.assertEqual((by["proj#24"]["kind"], by["proj#24"]["who"]), ("proposed", ["kab"]))
        self.assertNotIn("proj#25", by)
        self.assertEqual(list(by)[0], "proj#21")  # holds up the most

    def test_restricted_are_counts_and_outsiders_untrusted(self) -> None:
        hits = [t.hit("sensein/proj", 1, by="b", comments=1, labels=["tier:restricted"], title="secret"),
                t.hit("sensein/proj", 2, by="stranger", comments=1)]
        gh = UnblockGH(hits=hits, comments={"sensein/proj#1": [c("b", "2026-11-22T00:00:00Z", 1)],
                                             "sensein/proj#2": [c("stranger", "2026-11-22T00:00:00Z", 2)]})
        v = gather(gh, items=[])
        self.assertEqual(v["restricted"], 1)
        self.assertNotIn("secret", json.dumps(v))
        [a] = v["asks"]
        self.assertTrue(a["untrusted"])
        self.assertIn("1 restricted items also wait on you", "\n".join(unblock.unblock_lines(v)))

    def test_no_board_is_a_note_and_empty_says_so(self) -> None:
        v = gather(UnblockGH())
        self.assertIn("the board was not read", v["notes"][0])
        self.assertEqual(unblock.unblock_lines({"asks": [], "restricted": 0, "notes": []}), ["Nothing is waiting on you."])

    def test_decision_count_rules(self) -> None:
        self.assertEqual(unblock.decision_count(DECISIONS, "me"), 3)
        self.assertEqual(unblock.decision_count(DECISIONS.replace("@me ", ""), "me"), 0)  # not addressed, no mention
        self.assertEqual(unblock.decision_count(DECISIONS.replace("@me ", ""), "me", addressed=True), 3)
        self.assertEqual(unblock.decision_count("@me\n1. a\n2. b\n3. c\n", "me"), 0)  # numbered, but no decision words

    def test_cmd_text_output(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        rc = colab.cmd_unblock(people.pcfg(), text=True, client=(sync, UnblockGH()), login="me", out=out, err=err,
                               now_t=NOW, board_items=[board(20, "Blocked", lead="me")])
        self.assertEqual(rc, 0)
        self.assertTrue(out.getvalue().startswith("1. proj#20: Blocked, you lead it"))
        self.assertIn("unblock: 1 asks waiting on me", err.getvalue())


if __name__ == "__main__":
    unittest.main()
