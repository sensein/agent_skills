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
