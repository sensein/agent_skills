"""The nightly digest: builder, token fallback (default GITHUB_TOKEN vs a supplied token), tier safety, the leak scan
before posting, and the one write (the digest issue). No network."""
from __future__ import annotations

import io
import unittest
import urllib.error
from datetime import datetime, timezone

import test_lab_colab_orgview as ov  # noqa: E402  (fakes for repos, search and releases)

colab, sync, people = ov.colab, ov.sync, ov.people
import colab_digest as digest  # noqa: E402

NOW = datetime(2026, 11, 25, 6, 0, tzinfo=timezone.utc)


def setUpModule() -> None:
    ov.setUpModule()


def tearDownModule() -> None:
    ov.tearDownModule()


class DigestGH(ov.SearchGH):
    """SearchGH plus repo visibility per token, requested reviewers, the meta repo's issues, /user, and writes."""

    def __init__(self, readable=("proj", "tools", "lab-colab"), issues=(), user="me", reviewers=None, **kw):
        super().__init__(kw.pop("results", {}), **kw)
        self.readable, self.meta_issues, self.user, self.reviewers = set(readable), list(issues), user, reviewers or {}
        self.sent = []

    def get(self, path, cache=False, conditional=True):
        if path == "/user":
            if self.user is None:
                raise urllib.error.HTTPError(path, 403, "Resource not accessible by integration", {}, None)
            return {"login": self.user}
        if path.startswith("/repos/sensein/lab-colab/issues?"):
            return self.meta_issues if "page=1" in path else []
        if path.endswith("/requested_reviewers"):
            return {"users": [{"login": u} for u in self.reviewers.get(path.split("/pulls/")[1].split("/")[0], [])], "teams": []}
        parts = path.split("?")[0].split("/")
        if len(parts) == 4 and parts[1] == "repos":  # /repos/<org>/<name>: visibility, per this token
            if parts[3] in self.readable:
                return {"visibility": "private"}
            raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)
        return super().get(path, cache, conditional)

    def send(self, method, path, body):
        self.sent.append((method, path, body))
        return {"html_url": "https://github.com/sensein/lab-colab/issues/50", "number": 50}


def item(n, at="2026-11-01T00:00:00Z", repo="proj"):
    return {**ov.sitem(repo, n), "updated_at": at}


class DigestBuildTests(unittest.TestCase):
    def test_default_token_lists_unreadable_repos_and_never_names_restricted_ones(self) -> None:
        gh = DigestGH(readable=("proj",), results={"waiting_review": [item(3), item(4, repo="sailsprep"), item(5, at="2024-01-01T00:00:00Z")]},
                      reviewers={"3": ["kab"]})
        d = digest.build(gh, ov.brief_cfg(), now_t=NOW, token_kind="default GITHUB_TOKEN")
        md = d["markdown"]
        self.assertTrue(md.startswith(digest.MARKER))
        self.assertIn("with a default GITHUB_TOKEN token", md)
        self.assertIn("not readable by this token (1): tools", md)
        self.assertIn("not readable by this token: 1 restricted repos (count only)", md)
        self.assertIn("COLAB_DIGEST_TOKEN", md)
        self.assertIn("- kab: proj#3 (24 days)", md)
        self.assertIn("plus 1 in restricted repos (count only)", md)
        self.assertIn("plus 1 untouched for over 180 days", md)
        self.assertNotIn("sailsprep", md)
        self.assertEqual(gh.sent, [])  # building never writes

    def test_supplied_token_reads_everything_it_can(self) -> None:
        gh = DigestGH(readable=("proj", "tools", "sailsprep"))
        d = digest.build(gh, ov.brief_cfg(), now_t=NOW, token_kind="supplied")
        self.assertEqual((d["coverage"]["readable"], d["coverage"]["unreadable"]), (3, []))
        self.assertNotIn("not readable", d["markdown"])

    def test_refuses_when_something_restricted_would_be_shown(self) -> None:
        orig = ov.orgview.org_brief
        try:
            ov.orgview.org_brief = lambda *a, **k: {**orig(*a, **k), "tier": "restricted"}
            with self.assertRaises(ValueError):
                digest.build(DigestGH(), ov.brief_cfg(), now_t=NOW)
        finally:
            ov.orgview.org_brief = orig


class DigestPostTests(unittest.TestCase):
    def run_cmd(self, gh, cfg=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        rc = colab.cmd_digest(cfg or ov.brief_cfg(), client=(sync, gh), out=out, err=err, now_t=NOW, board_items=[], **kw)
        return rc, out.getvalue(), err.getvalue()

    def test_opens_once_then_replaces_only_its_own_issue(self) -> None:
        theirs = {"number": 7, "user": {"login": "someone"}, "body": digest.MARKER + " quoted"}
        gh = DigestGH(user=None, issues=[theirs])
        rc, _, err = self.run_cmd(gh, do_post=True, env={"COLAB_DIGEST_TOKEN_KIND": "default GITHUB_TOKEN"})
        self.assertEqual(rc, 0, err)
        [(method, path, body)] = gh.sent
        self.assertEqual((method, path, body["title"]), ("POST", "/repos/sensein/lab-colab/issues", "Nightly digest"))
        mine = {"number": 9, "user": {"login": "github-actions[bot]"},
                "body": digest.MARKER + "\n<!-- colab-digest-at 2026-11-24T06:00:00Z -->"}
        gh = DigestGH(user=None, issues=[theirs, mine])
        rc, out, err = self.run_cmd(gh, do_post=True, env={})
        [(method, path, body)] = gh.sent
        self.assertEqual((method, path), ("PATCH", "/repos/sensein/lab-colab/issues/9"))
        self.assertIn("Since 2026-11-24 06:00 UTC", body["body"])  # the period starts at the last digest
        self.assertIn("updated", err)

    def test_without_post_nothing_is_written(self) -> None:
        gh = DigestGH()
        rc, out, err = self.run_cmd(gh, env={})
        self.assertEqual((rc, gh.sent), (0, []))
        self.assertIn("# Nightly digest: 2026-11-25", out)

    def test_leak_scan_finding_blocks_the_post(self) -> None:
        cfg = ov.brief_cfg()
        cfg["scan"]["patterns"]["path"] = ["proj"]  # any text that matches a scan pattern stops the post
        gh = DigestGH()
        rc, out, err = self.run_cmd(gh, cfg=cfg, do_post=True, env={})
        self.assertEqual((rc, gh.sent, out), (3, [], ""))
        self.assertIn("not posting", err)


if __name__ == "__main__":
    unittest.main()
