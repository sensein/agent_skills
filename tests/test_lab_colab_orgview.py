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


class UnassignedTests(unittest.TestCase):
    def test_split_into_new_active_ignored_restricted_and_dormant(self) -> None:
        c = cfg(ignored={"repos": {"KeepOut": "a fork we don't track"}})
        c["tiers"]["restricted"] = ["secret"]
        view = orgview.unassigned_repos(OrgGH(REPOS), c, now_t=NOW, since="2026-11-11")
        self.assertEqual([r["name"] for r in view["new"]], ["fresh"])
        self.assertEqual([r["name"] for r in view["active"]], ["busy"])
        self.assertEqual(view["ignored"][0]["reason"], "a fork we don't track")
        self.assertEqual((view["restricted"], view["dormant"], view["dormant_names"]), (1, 1, ["old"]))
        self.assertEqual((view["total"], view["in_spaces"]), (7, 2))  # archived left out; Proj matched case-insensitively
        self.assertNotIn("secret", json.dumps(view))  # a declared-restricted repo is a count only
        lines = orgview.unassigned_lines(view)
        self.assertTrue(lines[0].startswith("1 new since the spaces were last revised (2026-11-11): fresh (private"))
        self.assertIn("1 restricted repos in no space (count only)", lines)

    def test_revision_date_from_config_or_last_config_commit(self) -> None:
        gh = OrgGH(REPOS, commits=[{"commit": {"committer": {"date": "2026-11-19T16:06:40Z"}}}])
        self.assertEqual(orgview.unassigned_repos(gh, cfg(), now_t=NOW)["since"], "2026-11-19T16:06:40Z")
        c = cfg()
        c["org"]["spaces_revised"] = "2026-11-11"
        self.assertEqual(orgview.unassigned_repos(OrgGH(REPOS), c, now_t=NOW)["since"], "2026-11-11")

    def test_budget_and_errors_are_notes(self) -> None:
        view = orgview.unassigned_repos(OrgGH(REPOS), cfg(), now_t=NOW, since="2026-11-11", budget=sync.Budget(0))
        self.assertIn("budget ran out", view["notes"][0])

    def test_cmd_prints_json_and_lines_and_hides_dormant_names_unless_all(self) -> None:
        for show_all, names in ((False, []), (True, ["old"])):
            out, err = io.StringIO(), io.StringIO()
            rc = colab.cmd_unassigned(cfg(), since="2026-11-11", show_all=show_all, client=(sync, OrgGH(REPOS)),
                                      out=out, err=err, now_t=NOW)
            self.assertEqual(rc, 0)
            view = json.loads(out.getvalue())
            self.assertEqual(view["dormant_names"], names)
            self.assertIn("unassigned: 3 new since", err.getvalue())


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
