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
    """SearchGH plus repo visibility per token, review searches, requested reviewers, the meta repo's issues, /user,
    and writes."""

    def __init__(self, readable=("proj", "tools", "lab-colab"), issues=(), user="me", reviewers=None, unreviewed=(),
                 required=(), stale=0, meta_vis="private", fail=(), **kw):
        super().__init__(kw.pop("results", {}), **kw)
        self.readable, self.meta_issues, self.user, self.reviewers = set(readable), list(issues), user, reviewers or {}
        self.unreviewed, self.required, self.stale, self.meta_vis, self.fail = list(unreviewed), list(required), stale, meta_vis, set(fail)
        self.sent = []

    def get(self, path, cache=False, conditional=True):
        import urllib.parse
        for f in self.fail:
            if f in path:
                raise urllib.error.HTTPError(path, 403, "Resource not accessible by integration", {}, None)
        if path == "/user":
            if self.user is None:
                raise urllib.error.HTTPError(path, 403, "Resource not accessible by integration", {}, None)
            return {"login": self.user}
        if path.startswith("/search/issues"):
            q = urllib.parse.unquote(path.split("q=")[1].split("&")[0])
            if "review:none" in q and "updated:<" in q:
                return {"total_count": self.stale, "items": []}
            if "review:none" in q:
                return {"total_count": len(self.unreviewed), "items": self.unreviewed if "page=1" in path else []}
            if "review:required" in q and ".." in q:
                return {"total_count": len(self.required), "items": self.required if "page=1" in path else []}
        if path.startswith("/repos/sensein/lab-colab/issues?"):
            return self.meta_issues if "page=1" in path else []
        if path == "/repos/sensein/lab-colab":
            return {"visibility": self.meta_vis}
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


def item(n, at="2026-11-01T00:00:00Z", repo="proj", labels=()):
    return {**ov.sitem(repo, n), "updated_at": at, "labels": [{"name": x} for x in labels]}


class DigestBuildTests(unittest.TestCase):
    def test_default_token_lists_unreadable_repos_and_never_names_restricted_ones(self) -> None:
        gh = DigestGH(readable=("proj",), unreviewed=[item(3), item(4, repo="sailsprep"), item(6, labels=["tier:restricted"])],
                      reviewers={"3": ["kab"], "4": ["kab"], "6": ["kab"]}, stale=2)
        d = digest.build(gh, ov.brief_cfg(), now_t=NOW, token_kind="default GITHUB_TOKEN")
        md = d["markdown"]
        self.assertTrue(md.startswith(digest.MARKER))
        self.assertIn("with the default GITHUB_TOKEN;", md)
        self.assertIn("not readable by this token (1): tools", md)
        self.assertIn("not readable by this token: 1 restricted repos (count only)", md)
        self.assertIn("COLAB_DIGEST_TOKEN", md)
        self.assertIn("- kab: proj#3 (24 days)", md)  # asked for a review, none yet (no branch rule needed)
        self.assertIn("plus 2 in restricted repos or items (count only)", md)  # the repo and the tier:restricted label
        self.assertIn("plus 2 open PRs with no review, untouched for over 180 days", md)
        self.assertNotIn("proj#6", md)
        self.assertNotIn("sailsprep", md)
        self.assertEqual(gh.sent, [])  # building never writes

    def test_unreviewed_prs_nobody_was_asked_about_are_left_out_unless_a_rule_requires_review(self) -> None:
        gh = DigestGH(unreviewed=[item(3)], required=[item(5)])
        md = digest.build(gh, ov.brief_cfg(), now_t=NOW)["markdown"]
        self.assertNotIn("proj#3", md)
        self.assertIn("- review required, nobody requested: proj#5", md)

    def test_supplied_token_reads_everything_it_can(self) -> None:
        gh = DigestGH(readable=("proj", "tools", "sailsprep", "lab-colab"))
        d = digest.build(gh, ov.brief_cfg(), now_t=NOW, token_kind="supplied (COLAB_DIGEST_TOKEN)")
        self.assertEqual((d["coverage"]["readable"], d["coverage"]["unreadable"]), (3, []))
        self.assertNotIn("not readable", d["markdown"])

    def test_errors_become_notes_not_crashes(self) -> None:
        gh = DigestGH(unreviewed=[item(3)], reviewers={"3": ["kab"]}, fail=("/requested_reviewers", "/search/issues?q=org%3Asensein%20is%3Apr%20is%3Aopen%20review%3Anone%20updated%3A%3C"))
        md = digest.build(gh, ov.brief_cfg(), now_t=NOW)["markdown"]
        self.assertIn("review requests not read", md)

    def test_tier_includes_what_the_digest_adds_and_is_checked_against_the_destination(self) -> None:
        gh = DigestGH(readable=("proj",))
        d = digest.build(gh, ov.brief_cfg(), now_t=NOW)
        self.assertEqual(d["tier"], "lab")  # "tools" is named under coverage, and private repos are in no space
        with self.assertRaises(ValueError):
            digest.build(DigestGH(readable=("proj",)), ov.brief_cfg(), now_t=NOW, dest_tier="public")

    def test_refuses_when_something_restricted_would_be_shown(self) -> None:
        orig = ov.orgview.org_brief
        try:
            ov.orgview.org_brief = lambda *a, **k: {**orig(*a, **k), "tier": "restricted"}
            with self.assertRaises(ValueError):
                digest.build(DigestGH(), ov.brief_cfg(), now_t=NOW)
        finally:
            ov.orgview.org_brief = orig

    def test_state_block_is_carried_and_first_seen_kept(self) -> None:
        previous = {"repos": {"busy": {"first_seen": "2026-11-01", "last_reported": "2026-11-24"}}}
        d = digest.build(DigestGH(), ov.brief_cfg(), now_t=NOW, previous=previous)
        self.assertEqual(d["state"]["repos"]["busy"], {"first_seen": "2026-11-01", "last_reported": "2026-11-25"})
        self.assertEqual(ov.orgview.parse_state(d["markdown"]), d["state"])
        self.assertIn("Still unassigned", d["markdown"])
        self.assertNotIn("sailsprep", ov.orgview.render_state(d["state"]))


class DigestPostTests(unittest.TestCase):
    def run_cmd(self, gh, cfg=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        rc = colab.cmd_digest(cfg or ov.brief_cfg(), client=(sync, gh), out=out, err=err, now_t=NOW, board_items=[], **kw)
        return rc, out.getvalue(), err.getvalue()

    def test_opens_once_then_replaces_only_its_own_or_the_bots_issue(self) -> None:
        theirs = {"number": 7, "user": {"login": "someone"}, "body": digest.MARKER + " quoted"}
        gh = DigestGH(user=None, issues=[theirs])
        rc, _, err = self.run_cmd(gh, do_post=True, env={"COLAB_DIGEST_TOKEN_KIND": "default GITHUB_TOKEN"})
        self.assertEqual(rc, 0, err)
        [(method, path, body)] = gh.sent
        self.assertEqual((method, path, body["title"]), ("POST", "/repos/sensein/lab-colab/issues", "Nightly digest"))
        bots = {"number": 9, "user": {"login": "github-actions[bot]"}, "updated_at": "2026-11-24T06:00:00Z",
                "body": digest.MARKER + "\n<!-- colab-digest-at 2026-11-24T06:00:00Z -->"}
        gh = DigestGH(user="satra", issues=[theirs, bots])  # a supplied token: /user is a person, the bot's issue is adopted
        rc, out, err = self.run_cmd(gh, do_post=True, env={})
        [(method, path, body)] = gh.sent
        self.assertEqual((method, path), ("PATCH", "/repos/sensein/lab-colab/issues/9"))
        self.assertIn("Since 2026-11-24 06:00 UTC", body["body"])  # the period starts at the last digest
        self.assertIn("updated", err)

    def test_a_malformed_stamp_is_ignored(self) -> None:
        self.assertIsNone(digest.previous_at({"body": "<!-- colab-digest-at 2026-11-24 OR is:public -->"}))

    def test_without_post_nothing_is_written(self) -> None:
        gh = DigestGH()
        rc, out, err = self.run_cmd(gh, env={})
        self.assertEqual((rc, gh.sent), (0, []))
        self.assertIn("# Nightly digest: 2026-11-25", out)

    def test_public_meta_repo_refuses_a_lab_digest(self) -> None:
        gh = DigestGH(meta_vis="public")
        rc, out, err = self.run_cmd(gh, do_post=True, env={})
        self.assertEqual((rc, gh.sent, out), (3, [], ""))
        self.assertIn("meta repo is public", err)

    def test_leak_scan_finding_blocks_the_post(self) -> None:
        cfg = ov.brief_cfg()
        cfg["scan"]["patterns"]["path"] = ["proj"]  # any text that matches a scan pattern stops the post
        gh = DigestGH()
        rc, out, err = self.run_cmd(gh, cfg=cfg, do_post=True, env={})
        self.assertEqual((rc, gh.sent, out), (3, [], ""))
        self.assertIn("not posting", err)

    def test_restricted_names_are_a_tripwire(self) -> None:
        orig = digest.build
        try:
            digest.build = lambda *a, **k: {**orig(*a, **k), "markdown": "oops: sailsprep\n"}
            gh = DigestGH()
            rc, out, err = self.run_cmd(gh, do_post=True, env={})
        finally:
            digest.build = orig
        self.assertEqual((rc, gh.sent), (3, []))
        self.assertIn("restricted_name", err)

if __name__ == "__main__":
    unittest.main()
