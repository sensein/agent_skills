"""Org-wide views (sensein/lab-colab#34): the org's repos in no space. No network: an in-memory fake."""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
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


def sitem(repo, n, title="t", private=False):
    return {"number": n, "title": title, "repository_url": f"https://api.github.com/repos/sensein/{repo}",
            "html_url": f"https://github.com/sensein/{repo}/issues/{n}", "repository": {"private": private}}


class SearchGH(OrgGH):
    """OrgGH plus issue search (by which qualifier the query carries) and releases."""

    def __init__(self, results, releases=None, total=None, **kw):
        super().__init__(REPOS, **kw)
        self.results, self.releases, self.total = results, releases or {}, total or {}

    def get(self, path, cache=False, conditional=True):
        if path.startswith("/search/issues"):
            self.gets.append(path)
            import urllib.parse
            q = urllib.parse.unquote(path.split("q=")[1].split("&")[0])
            key = next(k for k, marker in (("merged", "is:merged"), ("waiting_review", "review:required"), ("open_prs", "is:pr is:open"),
                                          ("opened", "created:>="), ("closed", "closed:>="), ("updated", "updated:>=")) if marker in q)
            items = self.results.get(key, []) if "page=1" in path else []
            return {"total_count": self.total.get(key, len(self.results.get(key, []))), "items": items}
        if "/releases?" in path:
            self.gets.append(path)
            return self.releases.get(path.split("/repos/sensein/")[1].split("/")[0], [])
        return super().get(path, cache, conditional)


def brief_cfg():
    c = cfg()
    c["spaces"]["sails"] = {"stewards": ["me"], "repos": ["sailsprep"]}
    c["spaces"]["ext"] = {"stewards": ["me"], "repos": ["other/thing"]}
    c["tiers"]["restricted"] = ["sailsprep", "hush"]
    return c


class OrgBriefTests(unittest.TestCase):
    def view(self, gh, board=None, **kw):
        return orgview.org_brief(gh, brief_cfg(), "2026-11-17T00:00:00Z", now_t=NOW, board_items=board, **kw)

    def test_rows_per_space_restricted_counts_only_and_no_space_activity(self) -> None:
        gh = SearchGH({"updated": [sitem("proj", 1), sitem("sailsprep", 2, "secret plan"), sitem("ontoreview", 3), sitem("hush", 4, "x")],
                       "merged": [sitem("proj", 5)], "open_prs": [sitem("sailsprep", 6)], "opened": [sitem("proj", 1)]},
                      releases={"proj": [{"tag_name": "v2", "published_at": "2026-11-20T00:00:00Z", "html_url": "u"}],
                                "sailsprep": [{"tag_name": "secret", "published_at": "2026-11-20T00:00:00Z"}]})
        board = [{"status": "Blocked", "spaces": ["proj"]}, {"status": "Ready", "spaces": ["proj"]}]
        v = self.view(gh, board=board)
        self.assertEqual((v["spaces"]["proj"]["updated"], v["spaces"]["proj"]["merged"], v["spaces"]["proj"]["blocked"]), (1, 1, 1))
        self.assertTrue(v["spaces"]["sails"]["restricted"])
        self.assertEqual(v["spaces"]["sails"]["open_prs"], 1)
        self.assertTrue(v["spaces"]["ext"]["external_only"])
        self.assertEqual(list(v["no_space"]), ["ontoreview"])  # the restricted repo outside spaces is never named
        self.assertEqual([r["tag"] for r in v["releases"]], ["v2"])  # restricted repos' releases are skipped
        text = "\n".join(orgview.brief_lines(v))
        for leak in ("secret", "sailsprep", "hush"):
            self.assertNotIn(leak, json.dumps(v["releases"]) + text)
        self.assertIn("sails (restricted: counts only)", text)
        self.assertIn("not covered (their repos are in other orgs): ext", text)
        self.assertIn("Active outside any space: ontoreview (1 updated)", text)
        self.assertEqual(v["tier"], "lab")

    def test_board_missing_truncation_and_budget_are_notes(self) -> None:
        gh = SearchGH({"updated": [sitem("proj", 1)]}, total={"updated": 5000})
        v = self.view(gh)
        self.assertIn("the board was not read: Blocked counts are missing", v["notes"])
        self.assertEqual(v["truncated"], ["updated: 1 of 5000 read"])
        v = self.view(SearchGH({}), budget=sync.Budget(2))
        self.assertIn("the call budget ran out; the brief is partial", v["notes"])

    def test_empty_org_is_public_and_quiet(self) -> None:
        v = self.view(SearchGH({}), board=[])
        self.assertEqual(v["tier"], "public")
        self.assertIn("- quiet: proj, infra, sails", "\n".join(orgview.brief_lines(v)))  # config order

    def test_cmd_marks_last_look_unless_peek_or_since(self) -> None:
        gh = SearchGH({"updated": [sitem("proj", 1)]})
        run = lambda **kw: colab.cmd_brief_org(brief_cfg(), client=(sync, gh), login="me", out=io.StringIO(), err=io.StringIO(),
                                               now_t=NOW, board_items=[], **kw)
        path = sync.state_path("brief", "sensein", "me")
        if path.exists():
            path.unlink()
        self.assertEqual(run(peek=True), 0)
        self.assertFalse(path.exists())
        self.assertEqual(run(since="3d"), 0)
        self.assertFalse(path.exists())
        self.assertEqual(run(), 0)
        self.assertEqual(sync.load_state(path)["looks"]["org"], "2026-11-24T12:00:00Z")
        out = io.StringIO()
        colab.cmd_brief_org(brief_cfg(), client=(sync, gh), login="me", out=out, err=io.StringIO(), now_t=NOW, board_items=[],
                            text=True, peek=True)
        self.assertTrue(out.getvalue().startswith("What's new in sensein since 2026-11-24"))


class OrgBriefErrorTests(unittest.TestCase):
    def test_one_unreadable_repo_skips_only_its_releases(self) -> None:
        class Flaky(SearchGH):
            def get(self, path, cache=False, conditional=True):
                if "/repos/sensein/proj/releases" in path:
                    raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
                return super().get(path, cache, conditional)
        gh = Flaky({}, releases={"tools": [{"tag_name": "v9", "published_at": "2026-11-20T00:00:00Z", "html_url": "u"}]})
        c = brief_cfg()
        c["spaces"]["infra"]["repos"] = ["tools"]
        v = orgview.org_brief(gh, c, "2026-11-17T00:00:00Z", now_t=NOW, board_items=[])
        self.assertEqual([r["tag"] for r in v["releases"]], ["v9"])
        self.assertFalse(any("stopped early" in n for n in v["notes"]))
