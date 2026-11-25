"""Org-wide views (sensein/lab-colab#34): the org's repos in no space. No network: an in-memory fake."""
from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone

import test_lab_colab_people as people  # noqa: E402  (loads colab and colab_sync with the hermetic setup)

colab, sync = people.colab, people.sync
import colab_orgview as orgview  # noqa: E402

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def setUpModule() -> None:
    people.setUpModule()


def tearDownModule() -> None:
    people.tearDownModule()


def repo(name, created, pushed=None, vis="public", archived=False):
    return {"name": name, "visibility": vis, "created_at": created + "T00:00:00Z",
            "pushed_at": (pushed or created) + "T00:00:00Z", "archived": archived, "html_url": f"https://github.com/sensein/{name}"}


class OrgGH:
    def __init__(self, repos, commits=None):
        self.repos, self.commits, self.gets = repos, commits, []

    def get(self, path, cache=False, conditional=True):
        self.gets.append(path)
        if path.startswith("/orgs/sensein/repos"):
            page = int(path.split("page=")[-1])
            return self.repos[(page - 1) * 100:page * 100]
        if "/commits?path=.colab/config.toml" in path:
            return self.commits or []
        return None


def cfg(**extra):
    c = people.pcfg()
    c["spaces"]["proj"]["repos"] = ["Proj", "up/lib"]  # case differs from GitHub's name on purpose
    c.update(extra)
    return c


REPOS = [repo("proj", "2020-01-01", "2026-11-20"), repo("tools", "2021-01-01"), repo("fresh", "2026-11-20", vis="private"),
         repo("busy", "2022-01-01", "2026-11-15"), repo("old", "2019-01-01"), repo("gone", "2026-11-21", archived=True),
         repo("keepout", "2026-11-22"), repo("secret", "2026-11-23", vis="private")]


def cfg2():
    """cfg() with keepout ignored and secret restricted, so only busy and fresh are unassigned."""
    c = cfg(ignored={"repos": {"KeepOut": "a fork we don't track"}})
    c["tiers"]["restricted"] = ["secret"]
    return c


class UnassignedTests(unittest.TestCase):
    def view(self, c=None, state=None, gh=None, **kw):
        return orgview.unassigned_repos(gh or OrgGH(REPOS), c or cfg(), now_t=NOW, state=state, **kw)

    def test_unassigned_is_active_repos_in_no_space_not_ignored(self) -> None:
        c = cfg(ignored={"repos": {"KeepOut": "a fork we don't track"}})
        c["tiers"]["restricted"] = ["secret"]
        v = self.view(c)
        self.assertEqual(sorted(e["name"] for e in v["unassigned"]), ["busy", "fresh"])
        self.assertEqual(v["ignored"][0]["reason"], "a fork we don't track")
        self.assertEqual((v["restricted"], v["dormant"], v["dormant_names"]), (1, 1, ["old"]))
        self.assertEqual((v["total"], v["in_spaces"]), (7, 2))  # archived left out; Proj matched case-insensitively
        self.assertNotIn("secret", json.dumps(v))  # a declared-restricted repo is a count only

    def test_first_seen_defaults_to_created_and_new_is_the_last_7_days(self) -> None:
        by = {e["name"]: e for e in self.view()["unassigned"]}
        self.assertEqual((by["fresh"]["first_seen"], by["fresh"]["new"], by["fresh"]["age_days"]), ("2026-11-20", True, 4))
        self.assertFalse(by["busy"]["new"])
        self.assertEqual(by["busy"]["first_seen"], "2022-01-01")

    def test_first_seen_from_state_and_since_override_and_floor(self) -> None:
        state = {"repos": {"busy": {"first_seen": "2026-11-22", "last_reported": "2026-11-23"}}}
        by = {e["name"]: e for e in self.view(state=state)["unassigned"]}
        self.assertTrue(by["busy"]["new"])  # first seen two days ago, whatever its creation date
        by = {e["name"]: e for e in self.view(since="2026-11-23", state=state)["unassigned"]}
        self.assertFalse(by["busy"]["new"])
        c = cfg()
        c["org"]["spaces_revised"] = "2026-11-23"  # optional floor: never new before it
        by = {e["name"]: e for e in self.view(c, state=state)["unassigned"]}
        self.assertFalse(by["busy"]["new"])

    def test_lines_group_new_and_still_unassigned_with_age(self) -> None:
        lines = orgview.unassigned_lines(self.view(cfg2()))
        self.assertTrue(lines[0].startswith("New (first seen in the last 7 days), 1: fresh (private, created 2026-11-20)"))
        self.assertTrue(lines[1].startswith("Still unassigned, 1: busy ("))

    def test_budget_and_errors_are_notes(self) -> None:
        v = self.view(budget=sync.Budget(0))
        self.assertIn("budget ran out", v["notes"][0])

    def test_cmd_reads_the_digest_state_when_readable(self) -> None:
        class WithDigest(OrgGH):
            def get(self, path, cache=False, conditional=True):
                if path.startswith("/repos/sensein/lab-colab/issues?"):
                    body = orgview.MARKER + "\n" + orgview.render_state(
                        {"repos": {"busy": {"first_seen": "2026-11-23", "last_reported": "2026-11-24"}}})
                    return [{"number": 50, "user": {"login": "github-actions[bot]"}, "body": body}]
                return super().get(path, cache, conditional)
        for gh, src, show_all, names in ((OrgGH(REPOS), "creation dates", False, []),
                                         (WithDigest(REPOS), "digest state", True, ["keepout", "old", "secret"][1:2])):
            out, err = io.StringIO(), io.StringIO()
            rc = colab.cmd_unassigned(cfg(), show_all=show_all, client=(sync, gh), out=out, err=err, now_t=NOW)
            self.assertEqual(rc, 0)
            view = json.loads(out.getvalue())
            self.assertEqual((view["first_seen_from"], view["dormant_names"]), (src, names))
        self.assertIn("busy", view["new"])


class StateBlockTests(unittest.TestCase):
    def test_round_trip_and_dashes_cannot_close_the_comment(self) -> None:
        state = {"repos": {"a--b": {"first_seen": "2026-11-01", "last_reported": "2026-11-24"}}}
        block = orgview.render_state(state)
        self.assertNotIn("--", block[len("<!-- "):-len(" -->")])
        self.assertEqual(orgview.parse_state("text\n" + block + "\nmore"), state)

    def test_missing_malformed_or_hand_edited_blocks_start_fresh(self) -> None:
        for body in (None, "", "no block here", "<!-- colab-digest-state {not json} -->",
                     '<!-- colab-digest-state {"repos": [1, 2]} -->', '<!-- colab-digest-state ["x"] -->'):
            self.assertEqual(orgview.parse_state(body), {"repos": {}})
        edited = ('<!-- colab-digest-state {"repos": {"ok": {"first_seen": "2026-11-01"}, "bad date": {"first_seen": "soon"},'
                  ' "../x": {"first_seen": "2026-11-01"}, "n": "x"}} -->')
        self.assertEqual(orgview.parse_state(edited),
                         {"repos": {"ok": {"first_seen": "2026-11-01", "last_reported": "2026-11-01"}}})

    def test_next_state_sets_first_seen_once_keeps_listed_and_drops_cleared(self) -> None:
        view = orgview.unassigned_repos(OrgGH(REPOS), cfg2(), now_t=NOW)
        first = orgview.next_state(view, {"repos": {}}, NOW)
        self.assertEqual(first["repos"]["fresh"], {"first_seen": "2026-11-20", "last_reported": "2026-11-24"})
        later = NOW.replace(day=30)
        again = orgview.next_state(orgview.unassigned_repos(OrgGH(REPOS), cfg2(), now_t=later, state=first), first, later)
        self.assertEqual(again["repos"]["fresh"]["first_seen"], "2026-11-20")  # kept: being reported does not clear it
        self.assertEqual(again["repos"]["fresh"]["last_reported"], "2026-11-30")
        c = cfg2()
        c["ignored"]["repos"]["busy"] = "not ours"
        c["spaces"]["infra"]["repos"] = ["tools", "fresh"]
        cleared = orgview.next_state(orgview.unassigned_repos(OrgGH(REPOS), c, now_t=later, state=again), again, later)
        self.assertEqual(cleared["repos"], {})  # in a space, or ignored: gone

    def test_restricted_names_are_never_written(self) -> None:
        c = cfg()
        c["tiers"]["restricted"] = ["fresh", "busy"]
        block = orgview.render_state(orgview.next_state(orgview.unassigned_repos(OrgGH(REPOS), c, now_t=NOW), {"repos": {}}, NOW))
        for name in ("fresh", "busy"):
            self.assertNotIn(name, block)

    def test_triage_asks_only_for_its_stewards_after_14_days(self) -> None:
        c = cfg()
        c["spaces"]["lab-infra"] = {"stewards": ["me"], "repos": ["tools"]}
        view = orgview.unassigned_repos(OrgGH(REPOS), c, now_t=NOW)
        asks = orgview.triage_asks(view, c, "me")
        self.assertEqual([a["item"] for a in asks], ["busy"])  # fresh is 4 days old
        self.assertEqual(orgview.triage_asks(view, c, "b"), [])

if __name__ == "__main__":
    unittest.main()
