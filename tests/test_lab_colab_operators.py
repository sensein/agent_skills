"""Operator-run buses, the colab-state branch, the lease, delivery keys, and the plain-words setup (lab-colab).

No network: every GitHub call goes to an in-memory fake.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills" / "lab-colab"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, SKILL / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


colab = _load("colab", "scripts/colab.py")
bus = _load("colab_bus", "scripts/colab_bus.py")

HERMETIC_DROP = ("COLAB_CONFIG", "COLAB_BUS_ID", "GITHUB_TOKEN", "GH_TOKEN", "GH_CONFIG_DIR", "CLAUDECODE")
_HERMETIC: dict = {}


def setUpModule() -> None:
    home = tempfile.mkdtemp(prefix="colab-op-home-")
    env = {k: v for k, v in os.environ.items() if k not in HERMETIC_DROP}
    env.update(HOME=home, XDG_CACHE_HOME=f"{home}/cache", XDG_STATE_HOME=f"{home}/state", XDG_CONFIG_HOME=f"{home}/config",
               XDG_DATA_HOME=f"{home}/data")
    _HERMETIC["home"], _HERMETIC["patch"] = home, mock.patch.dict(os.environ, env, clear=True)
    _HERMETIC["patch"].start()


def tearDownModule() -> None:
    import shutil
    _HERMETIC["patch"].stop()
    shutil.rmtree(_HERMETIC["home"], ignore_errors=True)


T0 = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
BUSES = [{"id": "satra-orcd", "operator": "satra", "host": "MIT ORCD", "role": "primary"},
         {"id": "kab-hpc", "operator": "kabilar", "host": "Partner HPC", "role": "standby", "order": 1}]


def cfg(**extra) -> dict:
    c = {"org": {"name": "sensein", "meta_repo": "lab-colab"}, "spaces": {"proj": {"repos": ["proj"]}},
         "bus": {"write_interval_seconds": 0, "poll_seconds": 300}, "buses": [dict(b) for b in BUSES]}
    c.update(extra)
    return c


# --------------------------------------------------------------------------- an in-memory git for colab-state


class FakeGit:
    """Refs, commits, and trees of the meta repo, shared by several buses (each a FakeBusGH with its own login)."""

    def __init__(self, team=("satra", "kabilar"), branch=True):
        self.team = set(team)
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.ref: str | None = None
        self.ref_updates = 0
        self.variables: dict[str, str] = {}
        self.clock = T0  # GitHub's own time, stamped on every commit
        self._n = 0
        if branch:
            tree = self._tree({"README.md": "state\n"})
            self.ref = self._commit(tree, [], "admin")

    def _sha(self, kind: str) -> str:
        self._n += 1
        return hashlib.sha1(f"{kind}{self._n}".encode()).hexdigest()

    def _tree(self, files: dict) -> str:
        sha = self._sha("tree")
        self.trees[sha] = dict(files)
        return sha

    def _commit(self, tree: str, parents: list, author: str, message: str = "") -> str:
        sha = self._sha("commit")
        self.commits[sha] = {"tree": tree, "parents": parents, "author": author, "message": message,
                             "date": self.clock.isoformat().replace("+00:00", "Z")}
        return sha

    def files(self) -> dict:
        return self.trees[self.commits[self.ref]["tree"]] if self.ref else {}

    def last_change(self, sha: str, path: str) -> str | None:
        """The newest commit reachable from sha (first parents) that changed path."""
        while sha:
            c = self.commits[sha]
            parent = c["parents"][0] if c["parents"] else None
            mine = self.trees[c["tree"]].get(path)
            before = self.trees[self.commits[parent]["tree"]].get(path) if parent else None
            if mine is not None and mine != before:
                return sha
            sha = parent
        return None

    def push(self, author: str, files: dict) -> str:
        """A commit made outside the bus (e.g. someone else), on top of the head."""
        tree = self._tree({**self.files(), **files})
        self.ref = self._commit(tree, [self.ref], author)
        return self.ref


class FakeBusGH:
    BASE = "/repos/sensein/lab-colab"

    def __init__(self, git: FakeGit, login: str, items=None):
        self.git, self.login, self.items = git, login, items or {}
        self.writes: list = []
        self.before_ref_update = None  # a hook to simulate another bus committing meanwhile
        self.fail = ()  # path prefixes whose reads fail (a transient network error)

    def get(self, path, cache=False, conditional=True, **kw):
        g, b = self.git, self.BASE
        if any(path.startswith(b + f) for f in self.fail):
            raise urllib.error.URLError("temporary failure")
        if path == "/user":
            return {"login": self.login}
        if path == f"{b}/git/ref/heads/colab-state":
            return {"object": {"sha": g.ref}} if g.ref else None
        if path.startswith(f"{b}/git/commits/"):
            c = g.commits.get(path.rsplit("/", 1)[-1])
            return {"tree": {"sha": c["tree"]}} if c else None
        if path.startswith(f"{b}/contents/"):
            rel, _, ref = path[len(f"{b}/contents/"):].partition("?ref=")
            c = g.commits.get(ref)
            text = g.trees[c["tree"]].get(rel) if c else None
            return None if text is None else {"content": base64.b64encode(text.encode()).decode(), "sha": "blob"}
        if path.startswith(f"{b}/commits?"):
            from urllib.parse import parse_qs as _pq
            q = {k: v[0] for k, v in _pq(path.split("?", 1)[1]).items()}
            found = g.last_change(q["sha"], q["path"])
            return [{"sha": found, "commit": {"committer": {"date": g.commits[found]["date"]}}}] if found else []
        if path.startswith(f"{b}/commits/"):
            c = g.commits.get(path.rsplit("/", 1)[-1])
            return {"author": {"login": c["author"]}} if c else None
        if path.startswith("/orgs/sensein/teams/colab-bus-operators/memberships/"):
            return {"state": "active"} if path.rsplit("/", 1)[-1] in g.team else None
        if "/actions/variables/" in path:
            name = path.rsplit("/", 1)[-1]
            return {"name": name, "value": g.variables[name]} if name in g.variables else None
        for prefix, issues in self.items.items():
            if path.startswith(prefix):
                return issues
        return []

    def send(self, method, path, body):
        g, b = self.git, self.BASE
        self.writes.append((method, path, body))
        if path == f"{b}/git/trees":
            base = dict(g.trees[body["base_tree"]])
            base.update({e["path"]: e["content"] for e in body["tree"]})
            return {"sha": g._tree(base)}
        if path == f"{b}/git/commits":
            return {"sha": g._commit(body["tree"], body["parents"], self.login, body["message"])}
        if path == f"{b}/git/refs/heads/colab-state":
            if self.before_ref_update:
                hook, self.before_ref_update = self.before_ref_update, None
                hook()
            new = g.commits[body["sha"]]
            if body.get("force") is not False or new["parents"] != [g.ref]:
                raise urllib.error.HTTPError(path, 422, "Update is not a fast forward", {}, None)
            g.ref, g.ref_updates = body["sha"], g.ref_updates + 1
            return {}
        if "/actions/variables" in path:
            g.variables[body["name"]] = body["value"]
            return {}
        return {"number": 1}


def stub_poll(calls: list, publish: bool = True):
    """A poll that records how it was called and publishes the security state (into the store when given)."""
    def poll(gh, config, rollout, subs, state, state_dir, bot, save=lambda: None, store=None, bus_id="", operator="", writers=None):
        calls.append({"bus_id": bus_id, "operator": operator, "store": store, "bot": bot, "writers": writers})
        stats = {"envelopes": 0, "deliveries": 0, "drops": 0, "dry_run": 0, "errors": 0}
        if publish:
            bus.publish_security_state(gh, config, state, state_dir, stats, store)
        return stats
    return poll


def run_cycle(gh, config, state, tmp, bus_id, now_t=T0, calls=None, **kw):
    calls = calls if calls is not None else []
    gh.git.clock = now_t  # GitHub stamps commits with its own time; tests keep it in step with the bus
    return bus.cycle(gh, config, {"spaces": {}}, [], state, Path(tmp), gh.login, bus_id=bus_id, now_t=now_t,
                     poll=stub_poll(calls), **kw)


def audit_lines(tmp) -> list[dict]:
    p = Path(tmp) / "audit.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# --------------------------------------------------------------------------- config and the lease decision


class BusesConfigTests(unittest.TestCase):
    def test_buses_are_validated_and_ordered(self) -> None:
        entries = colab.buses({"buses": [BUSES[1], BUSES[0], {"id": "z", "operator": "x", "role": "standby", "order": 0}]})
        self.assertEqual([b["id"] for b in entries], ["satra-orcd", "z", "kab-hpc"])
        self.assertEqual(colab.bus_operators(cfg()), ["kabilar", "satra"])
        self.assertEqual(colab.buses({}), [])
        for bad in ([{"id": "A b", "operator": "x"}], [{"id": "a", "operator": "x"}, {"id": "a", "operator": "y"}],
                    [{"id": "a", "operator": "x", "role": "primary"}, {"id": "b", "operator": "y", "role": "primary"}],
                    [{"id": "a", "operator": "x", "role": "leader"}], [{"id": "a", "operator": "not a login"}]):
            with self.assertRaises(colab.BusConfigError):
                colab.buses({"buses": bad})

    def test_bundled_config_parses_and_has_no_live_bus(self) -> None:
        bundled = colab.load_config()
        self.assertEqual(colab.buses(bundled), [])
        text = (SKILL / "assets" / "lab-colab.toml").read_text()
        self.assertIn("# [[buses]]", text)
        self.assertIn("lease_ttl_minutes", text)

    def test_lease_decision(self) -> None:
        entries = colab.buses(cfg())
        primary, standby = entries
        ttl = 15.0

        def lease(holder, minutes_ago):
            return {"holder": holder, "operator": "x", "renewed_at": colab._iso(T0 - timedelta(minutes=minutes_ago)), "ttl_minutes": ttl}
        self.assertEqual(colab.lease_decision(None, standby, entries, T0, ttl)[0], "active")
        self.assertEqual(colab.lease_decision(lease("satra-orcd", 1), primary, entries, T0, ttl)[0], "active")
        self.assertEqual(colab.lease_decision(lease("satra-orcd", 1), standby, entries, T0, ttl)[0], "passive")
        # stale: the first bus in order other than the holder takes over at once
        self.assertEqual(colab.lease_decision(lease("satra-orcd", 20), standby, entries, T0, ttl)[0], "active")
        # a returning primary stays passive while the standby's lease is fresh (until handed back)
        self.assertEqual(colab.lease_decision(lease("kab-hpc", 2), primary, entries, T0, ttl)[0], "passive")
        # a handback writes the lease naming the primary
        self.assertEqual(colab.lease_decision(lease("satra-orcd", 0), primary, entries, T0, ttl)[0], "active")
        # three buses: the second in order waits one more lease period
        three = colab.buses({"buses": BUSES + [{"id": "c", "operator": "c", "role": "standby", "order": 2}]})
        c = three[2]
        self.assertEqual(colab.lease_decision(lease("satra-orcd", 20), c, three, T0, ttl)[0], "passive")
        self.assertEqual(colab.lease_decision(lease("satra-orcd", 31), c, three, T0, ttl)[0], "active")

    def test_ttl_default_is_three_polls_at_least_fifteen_minutes(self) -> None:
        self.assertEqual(colab.lease_ttl_minutes({"bus": {"poll_seconds": 60}}), 15.0)
        self.assertEqual(colab.lease_ttl_minutes({"bus": {"poll_seconds": 600}}), 30.0)
        self.assertEqual(colab.lease_ttl_minutes({"bus": {"lease_ttl_minutes": 7}}), 7.0)

    def test_delivery_marker_names_bus_operator_and_key(self) -> None:
        m = colab.delivery_marker("satra-orcd", "satra", "proj:issue:3:opened")
        self.assertEqual(m, "<!-- colab-delivery bus=satra-orcd operator=satra key=proj:issue:3:opened -->")
        self.assertTrue(bus.MARKED.search(m) and bus.MARKED.search(bus.DELIVERY_MARKER))
        self.assertEqual(bus.DELIVERY_KEY.search(m)["key"], "proj:issue:3:opened")
        self.assertTrue(bus.is_delivery({"body": m}))


# --------------------------------------------------------------------------- the bus on colab-state


class StateBranchTests(unittest.TestCase):
    def test_one_commit_with_cas(self) -> None:
        git = FakeGit()
        a, b = bus.StateBranch(FakeBusGH(git, "satra"), "sensein", "lab-colab"), bus.StateBranch(FakeBusGH(git, "kabilar"), "sensein", "lab-colab")
        self.assertTrue(a.load() and b.load())
        a.stage("x.json", "1")
        a.stage("y.json", "2")
        self.assertTrue(a.commit("m"))
        self.assertEqual(git.ref_updates, 1)  # two files, one commit
        self.assertEqual(git.files()["x.json"], "1")
        b.stage("z.json", "3")
        with self.assertRaises(bus.StateConflict):
            b.commit("m")  # the head moved: not a fast-forward
        self.assertTrue(b.load())
        self.assertEqual(b.read("x.json"), "1")
        b.commit("m")
        self.assertEqual(set(git.files()), {"README.md", "x.json", "y.json", "z.json"})
        self.assertIsNone(b.commit("nothing staged"))

    def test_missing_branch_is_unavailable(self) -> None:
        self.assertFalse(bus.StateBranch(FakeBusGH(FakeGit(branch=False), "satra"), "sensein", "lab-colab").load())


class CycleTests(unittest.TestCase):
    def test_primary_writes_lease_heartbeat_and_security_in_one_commit(self) -> None:
        git = FakeGit()
        gh, state, calls = FakeBusGH(git, "satra"), {}, []
        with tempfile.TemporaryDirectory() as tmp:
            stats = run_cycle(gh, cfg(), state, tmp, "satra-orcd", calls=calls)
        self.assertTrue(stats["active"])
        self.assertEqual(git.ref_updates, 1)
        files = git.files()
        lease = json.loads(files["lease.json"])
        self.assertEqual((lease["holder"], lease["operator"]), ("satra-orcd", "satra"))
        beat = json.loads(files["heartbeat/satra-orcd.json"])
        self.assertTrue(beat["active"])
        self.assertEqual(beat["operator"], "satra")
        self.assertEqual(json.loads(files["security/state.json"])["v"], 1)
        self.assertEqual(state["security_published"], files["security/state.json"])
        self.assertEqual(state["state_branch_last"], git.ref)
        self.assertIs(calls[0]["store"].available, True)
        self.assertEqual(calls[0]["bot"], "")  # operator-run: own deliveries are skipped by marker, not by login
        self.assertEqual(git.variables, {})  # no Actions variables once colab-state exists

    def test_standby_stays_passive_and_throttles_its_heartbeat(self) -> None:
        git = FakeGit()
        with tempfile.TemporaryDirectory() as tmp:
            run_cycle(FakeBusGH(git, "satra"), cfg(), {}, tmp, "satra-orcd")
        lease_before = git.files()["lease.json"]
        calls, state = [], {}
        with tempfile.TemporaryDirectory() as tmp:
            stats = run_cycle(FakeBusGH(git, "kabilar"), cfg(), state, tmp, "kab-hpc", now_t=T0 + timedelta(minutes=1), calls=calls)
            self.assertTrue(stats["passive"])
            self.assertEqual(calls, [])  # a standby never polls
            self.assertEqual(git.files()["lease.json"], lease_before)
            self.assertFalse(json.loads(git.files()["heartbeat/kab-hpc.json"])["active"])
            updates = git.ref_updates
            run_cycle(FakeBusGH(git, "kabilar"), cfg(), state, tmp, "kab-hpc", now_t=T0 + timedelta(minutes=2))
            self.assertEqual(git.ref_updates, updates)  # nothing new to say: no commit

    def test_standby_takes_over_a_stale_lease_and_inherits_the_state(self) -> None:
        git = FakeGit()
        primary_state = {"security": {"proj#5": {"flagged_at": "2026-11-12T11:00:00Z", "reason": "rules: override"}},
                         "security_restricted_demo": True, "cursors": {"proj": "2026-11-12T11:58:00Z"}}
        primary_state["security"]["secret#2"] = {"flagged_at": "2026-11-12T11:00:00Z", "reason": "rules: hidden", "restricted": True}
        with tempfile.TemporaryDirectory() as tmp:
            run_cycle(FakeBusGH(git, "satra"), cfg(), primary_state, tmp, "satra-orcd")
        state, calls = {}, []
        with tempfile.TemporaryDirectory() as tmp:
            stats = run_cycle(FakeBusGH(git, "kabilar"), cfg(), state, tmp, "kab-hpc", now_t=T0 + timedelta(minutes=16), calls=calls)
            self.assertTrue(any("lease_taken" in a for a in audit_lines(tmp)))
        self.assertTrue(stats["active"])
        self.assertEqual(stats["took_over_from"], "satra-orcd")
        self.assertEqual(json.loads(git.files()["lease.json"])["holder"], "kab-hpc")
        self.assertIn("proj#5", state["security"])  # plain flag inherited
        published = json.loads(git.files()["security/state.json"])
        self.assertIn("sensein/proj#5", published["flags"])
        self.assertIn(colab.hashed_ref("sensein/secret#2"), published["flags"])  # restricted: carried by hash
        self.assertEqual(state["cursors"]["proj"], "2026-11-12T10:58:00Z")  # minus the 60-minute look-back
        self.assertEqual(state["baseline_at"]["proj"], "2026-11-12T10:58:00Z")
        # the returning primary stays passive while the standby's lease is fresh
        with tempfile.TemporaryDirectory() as tmp:
            back = run_cycle(FakeBusGH(git, "satra"), cfg(), primary_state, tmp, "satra-orcd", now_t=T0 + timedelta(minutes=17))
        self.assertTrue(back["passive"])

    def test_handback_returns_the_lease_to_the_primary(self) -> None:
        git = FakeGit()
        with tempfile.TemporaryDirectory() as tmp:
            run_cycle(FakeBusGH(git, "kabilar"), cfg(), {}, tmp, "kab-hpc")
        lease = colab.compose_bus_lease(colab.buses(cfg())[0], T0 + timedelta(minutes=1), 15.0, handed_by="kabilar")
        lease["handed_from"] = "kab-hpc"
        git.push("kabilar", {"lease.json": json.dumps(lease)})
        primary_state, calls = {"bus_mode": "passive"}, []
        with tempfile.TemporaryDirectory() as tmp:
            stats = run_cycle(FakeBusGH(git, "satra"), cfg(), primary_state, tmp, "satra-orcd", now_t=T0 + timedelta(minutes=2), calls=calls)
        self.assertTrue(stats["active"])
        self.assertEqual(stats["took_over_from"], "kab-hpc")
        self.assertEqual(len(calls), 1)
        with tempfile.TemporaryDirectory() as tmp:
            old = run_cycle(FakeBusGH(git, "kabilar"), cfg(), {"bus_mode": "active"}, tmp, "kab-hpc", now_t=T0 + timedelta(minutes=3))
        self.assertTrue(old["passive"])

    def test_race_loser_backs_off_and_unstages(self) -> None:
        git = FakeGit()
        loser = FakeBusGH(git, "kabilar")
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            loser.before_ref_update = lambda: run_cycle(FakeBusGH(git, "satra"), cfg(), {}, tmp_a, "satra-orcd")
            state = {}
            stats = run_cycle(loser, cfg(), state, tmp_b, "kab-hpc")  # both saw no lease; satra committed first
        self.assertTrue(stats.get("lease_lost"))
        self.assertEqual(json.loads(git.files()["lease.json"])["holder"], "satra-orcd")
        self.assertNotIn("security_published", state)  # its security state never landed
        self.assertFalse(json.loads(git.files()["heartbeat/kab-hpc.json"])["active"])

    def test_foreign_commit_is_detected_and_reasserted(self) -> None:
        git = FakeGit()
        gh, state = FakeBusGH(git, "satra"), {}
        with tempfile.TemporaryDirectory() as tmp:
            run_cycle(gh, cfg(), state, tmp, "satra-orcd")
            git.push("mallory", {"security/state.json": json.dumps({"v": 1, "flags": {}})})
            stats = run_cycle(gh, cfg(), state, tmp, "satra-orcd", now_t=T0 + timedelta(minutes=5))
            self.assertTrue(any(a.get("author") == "mallory" for a in audit_lines(tmp)))
        self.assertGreaterEqual(stats["security_foreign_edits"], 1)
        self.assertEqual(stats["state_branch_foreign"], "mallory")
        self.assertEqual(git.commits[git.ref]["author"], "satra")  # re-asserted on top
        self.assertNotEqual(json.loads(git.files()["security/state.json"]).get("issued_at"), None)
        # another registered operator's commit (a standby's heartbeat) is not foreign
        git.push("kabilar", {"heartbeat/kab-hpc.json": "{}"})
        with tempfile.TemporaryDirectory() as tmp:
            stats = run_cycle(gh, cfg(), state, tmp, "satra-orcd", now_t=T0 + timedelta(minutes=10))
        self.assertNotIn("state_branch_foreign", stats)

    def test_lease_freshness_uses_githubs_commit_date_not_the_writers_clock(self) -> None:
        git = FakeGit()
        forged = {"holder": "kab-hpc", "operator": "kabilar", "renewed_at": "2099-01-01T00:00:00Z", "ttl_minutes": 15}
        git.clock = T0 - timedelta(hours=1)
        git.push("kabilar", {"lease.json": json.dumps(forged)})  # written an hour ago, claims to be fresh until 2099
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            stats = run_cycle(FakeBusGH(git, "satra"), cfg(), {}, tmp, "satra-orcd", calls=calls)
        self.assertTrue(stats["active"])
        self.assertEqual(stats["took_over_from"], "kab-hpc")
        # and a lease whose writer's clock lags is still fresh when GitHub stamped it just now
        git2 = FakeGit()
        git2.clock = T0
        git2.push("kabilar", {"lease.json": json.dumps({**forged, "renewed_at": "2000-01-01T00:00:00Z"})})
        with tempfile.TemporaryDirectory() as tmp:
            stats = run_cycle(FakeBusGH(git2, "satra"), cfg(), {}, tmp, "satra-orcd", now_t=T0 + timedelta(minutes=1))
        self.assertTrue(stats["passive"])

    def test_a_transient_read_failure_keeps_the_current_role(self) -> None:
        git = FakeGit()
        gh, state = FakeBusGH(git, "satra"), {}
        with tempfile.TemporaryDirectory() as tmp:
            run_cycle(gh, cfg(), state, tmp, "satra-orcd")
            updates = git.ref_updates
            for failing in ("/git/commits/", "/contents/lease.json", "/commits?"):
                gh.fail, calls = (failing,), []
                stats = run_cycle(gh, cfg(), state, tmp, "satra-orcd", now_t=T0 + timedelta(minutes=5), calls=calls)
                self.assertTrue(stats["active"] and stats["state_read_failed"], (failing, stats))
                self.assertEqual(len(calls), 1)  # still polls (delivery keys stop duplicates) ...
                self.assertIsInstance(calls[0]["store"], bus.HoldStore)
                self.assertEqual(git.ref_updates, updates)  # ... but writes nothing to colab-state
                self.assertEqual(git.variables, {})  # and never falls back to the Actions variables
            self.assertTrue(any("kept_role" in a for a in audit_lines(tmp)))
            standby = FakeBusGH(git, "kabilar")
            standby.fail = ("/git/commits/",)
            calls = []
            stats = run_cycle(standby, cfg(), {"bus_mode": "passive"}, tmp, "kab-hpc", now_t=T0 + timedelta(hours=2), calls=calls)
            self.assertTrue(stats["passive"])  # a stale lease it cannot read is not "no lease"
            self.assertEqual(calls, [])

    def test_deliveries_count_only_registered_operators(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            run_cycle(FakeBusGH(FakeGit(), "satra"), cfg(), {}, tmp, "satra-orcd", calls=calls)
        self.assertEqual(calls[0]["writers"], {"satra", "kabilar"})

    def test_wrong_operator_or_not_in_team_stays_passive_and_writes_nothing(self) -> None:
        for login, team in (("mallory", ("satra", "mallory")), ("satra", ("kabilar",))):
            git = FakeGit(team=team)
            gh, calls = FakeBusGH(git, login), []
            with tempfile.TemporaryDirectory() as tmp:
                stats = run_cycle(gh, cfg(), {}, tmp, "satra-orcd", calls=calls)
                self.assertTrue(any("bus_passive" in a for a in audit_lines(tmp)))
            self.assertTrue(stats["passive"])
            self.assertEqual((calls, gh.writes, git.ref_updates), ([], [], 0))

    def test_no_branch_falls_back_to_variables_for_one_release(self) -> None:
        git = FakeGit(branch=False)
        gh, calls = FakeBusGH(git, "satra"), []
        with tempfile.TemporaryDirectory() as tmp:
            stats = run_cycle(gh, cfg(), {}, tmp, "satra-orcd", calls=calls)
            self.assertTrue(any(a.get("state_branch_missing") for a in audit_lines(tmp)))
        self.assertIn("missing", stats["state_branch"])
        self.assertIsNone(calls[0]["store"])
        self.assertIn("COLAB_SECURITY_STATE", git.variables)
        self.assertEqual(json.loads(git.variables["COLAB_BUS_HEARTBEAT"])["bus"], "satra-orcd")

    def test_without_buses_a_single_implicit_bus_runs(self) -> None:
        git = FakeGit(team=())
        c = cfg()
        c.pop("buses")
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            stats = run_cycle(FakeBusGH(git, "colab-bot"), c, {}, tmp, None, calls=calls)
        self.assertTrue(stats["active"])
        self.assertEqual(calls[0]["bot"], "colab-bot")  # a dedicated identity: its own events are ignored, as before
        self.assertEqual(json.loads(git.files()["lease.json"])["holder"], "default")


class DeliveryKeyTests(unittest.TestCase):
    ISSUE = {"number": 5, "id": 5, "title": "t", "labels": [], "state": "open", "created_at": "2026-11-09T01:00:00Z",
             "updated_at": "2026-11-09T01:00:00Z", "html_url": "u", "user": {"login": "a"}}

    def gh(self, dest_issues):
        git = FakeGit()
        gh = FakeBusGH(git, "satra", items={"/repos/sensein/proj/issues?": [self.ISSUE],
                                           "/repos/sensein/dest/issues?": dest_issues})
        real_get = gh.get

        def get(path, cache=False, conditional=True, **kw):
            name = path.rsplit("/", 1)[-1]
            if path.count("/") == 3 and name in ("proj", "dest", "lab-colab"):
                return {"visibility": "private"}
            return real_get(path, cache, conditional)
        gh.get = get
        return gh

    def poll(self, gh):
        cfg_ = cfg(spaces={"proj": {"repos": ["proj"]}, "dest": {"repos": ["dest"]}})
        state = {"cursors": {"proj": "2026-11-08T00:00:00Z", "dest": "2026-11-09T05:00:00Z"},
                 "baseline_at": {"proj": "2026-11-08T00:00:00Z", "dest": "2026-11-08T00:00:00Z"}}
        with tempfile.TemporaryDirectory() as tmp:
            return bus.poll_once(gh, cfg_, {"spaces": {"dest": {"bus": "live"}}}, [{"topic": "repo:proj", "subscriber": "dest"}],
                                 state, Path(tmp), "", bus_id="satra-orcd", operator="satra", writers={"satra", "kabilar"})

    def test_new_delivery_carries_bus_operator_and_key(self) -> None:
        gh = self.gh([])
        stats = self.poll(gh)
        [(method, path, body)] = [w for w in gh.writes if w[1].endswith("/dest/issues")]
        self.assertEqual(stats["deliveries"], 1)
        self.assertIn("<!-- colab-delivery bus=satra-orcd operator=satra key=proj:issue:5:opened -->", body["body"])
        self.assertIn(bus.DELIVERY_MARKER, body["body"])  # older buses still recognise it

    def test_existing_key_from_any_bus_is_not_delivered_again(self) -> None:
        done = [{"number": 9, "body": "<!-- colab-delivery bus=kab-hpc operator=kabilar key=proj:issue:5:opened -->",
                 "user": {"login": "kabilar"}, "state": "open", "created_at": "2026-11-09T02:00:00Z",
                 "updated_at": "2026-11-09T02:00:00Z", "labels": []}]
        gh = self.gh(done)
        stats = self.poll(gh)
        self.assertEqual([w for w in gh.writes if w[1].endswith("/dest/issues")], [])
        self.assertEqual(stats["deliveries_skipped"], 1)

    def test_a_marker_from_anyone_else_never_suppresses_a_delivery(self) -> None:
        forged = [{"number": 9, "body": "<!-- colab-delivery bus=satra-orcd operator=satra key=proj:issue:5:opened -->",
                   "user": {"login": "mallory"}, "state": "open", "created_at": "2026-11-09T02:00:00Z",
                   "updated_at": "2026-11-09T02:00:00Z", "labels": []}]
        gh = self.gh(forged)
        stats = self.poll(gh)
        self.assertEqual(len([w for w in gh.writes if w[1].endswith("/dest/issues")]), 1)
        self.assertNotIn("deliveries_skipped", stats)
        self.assertFalse(bus.already_delivered(gh, "/repos/sensein/dest", "proj:issue:5:opened", "x"))  # no writers: never


class MissingProjectFieldTests(unittest.TestCase):
    """Regression (live bus on #70): a Project with Spaces but no Contributions/Security failed the whole lookup
    query (NOT_FOUND on a named field), which also stopped the Spaces sync."""

    class GH:
        def __init__(self):
            self.mutations, self.queries = [], []

        def graphql(self, query, variables=None):
            self.queries.append(query)
            if query.lstrip().startswith("mutation"):
                name = query.split("{", 2)[1].split("(")[0].strip()
                self.mutations.append((name, variables))
                if name == "addProjectV2ItemById":
                    return {"addProjectV2ItemById": {"item": {"id": "ITEM-1"}}}
                return {}
            for missing in ("Contributions", "Security"):
                if f'field(name: "{missing}")' in query:  # what GitHub does for a missing named field
                    raise bus.GraphQLError(json.dumps([{"type": "NOT_FOUND", "message": "Could not resolve to a "
                                                        f"Unions::ProjectV2FieldConfiguration with the name {missing}."}]))
            if "projectV2(number" in query:
                return {"organization": {"projectV2": {"id": "PVT", "field": {"id": "FLD", "options": [
                    {"id": "o-proj", "name": "proj"}]}}}}
            return {"node": {"items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
                {"id": "ITEM-2", "content": {"__typename": "Issue", "number": 2, "repository": {"name": "proj", "owner": {"login": "sensein"}},
                                             "labels": {"nodes": []}, "assignees": {"nodes": []}},
                 "fieldValueByName": {"options": []}}]}}}

    def sync(self, gh, state, tmp):
        c = cfg()
        c["org"]["project_number"] = 7
        c["bus"].update(project_sync=True)
        stats = {"errors": 0}
        return bus.ProjectSync(gh, c, state, Path(tmp), stats, {"proj": ["proj"]}), stats

    def test_spaces_keep_syncing_and_missing_fields_are_audited_once(self) -> None:
        gh, state = self.GH(), {}
        with tempfile.TemporaryDirectory() as tmp:
            project, stats = self.sync(gh, state, tmp)
            project.add({"id": "proj:issue:1:opened", "node_id": "I_1", "kind": "issue", "spaces": ["proj"], "transition": "opened"})
            project.sweep()
            self.assertEqual(stats["errors"], 0)
            sets = [v for n, v in gh.mutations if n == "updateProjectV2ItemFieldValue"]
            self.assertEqual([v["item"] for v in sets], ["ITEM-1", "ITEM-2"])  # add-time and sweep Spaces writes
            self.assertTrue(all(v["field"] == "FLD" for v in sets))
            self.assertIsNone(project.ids()["security"])
            missing = [a["project_field_missing"] for a in audit_lines(tmp) if "project_field_missing" in a]
            self.assertEqual(sorted(missing), ["Contributions", "Security"])
            self.assertFalse(any("project_write_failed" in a for a in audit_lines(tmp)))
            again, _ = self.sync(gh, state, tmp)  # the next poll, same day: no second audit line
            again.sweep()
            self.assertEqual(len([a for a in audit_lines(tmp) if "project_field_missing" in a]), 2)


# --------------------------------------------------------------------------- the CLI reads colab-state


class FakeCli:
    """Stands in for colab._gh_json: the meta config, colab-state files, and who wrote them."""

    PROTECTED = [{"type": "update", "ruleset_id": 11}, {"type": "deletion", "ruleset_id": 11}]

    def __init__(self, state=None, writer="satra", meta_buses=True, variable=None, rules="protected"):
        self.state, self.writer, self.variable = state, writer, variable
        self.rules = self.PROTECTED if rules == "protected" else rules
        meta = '[security]\n' + ('[[buses]]\nid = "satra-orcd"\noperator = "satra"\nrole = "primary"\n' if meta_buses else "")
        self.meta = {"content": base64.b64encode(meta.encode()).decode()}
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        path = args[1] if len(args) > 1 else ""
        if path.endswith("contents/.colab/config.toml"):
            return self.meta
        if path.startswith("repos/sensein/lab-colab/contents/security/state.json?ref=colab-state"):
            return None if self.state is None else {"content": base64.b64encode(json.dumps(self.state).encode()).decode()}
        if path.startswith("repos/sensein/lab-colab/commits?sha=colab-state&path=security/state.json"):
            return [{"author": {"login": self.writer}}]
        if path == "repos/sensein/lab-colab/rules/branches/colab-state":
            return self.rules
        if "actions/variables/COLAB_SECURITY_STATE" in path:
            return None if self.variable is None else {"value": json.dumps(self.variable)}
        return None


def fresh_state(**kw) -> dict:
    return {"v": 1, "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "seen": {}, "flags": {}, "clears": {}, "ignored": {}, **kw}


class CliStateTests(unittest.TestCase):
    CFG = {"org": {"name": "sensein", "meta_repo": "lab-colab"}}

    def test_reads_the_branch_first(self) -> None:
        value, why = colab.load_security_state(self.CFG, FakeCli(state=fresh_state(), variable=fresh_state(flags={"x": {}})))
        self.assertEqual((value["flags"], why), ({}, ""))
        self.assertTrue(colab.STATE_SOURCE["source"].startswith("colab-state"))

    def test_a_writer_who_runs_no_bus_fails_closed(self) -> None:
        value, why = colab.load_security_state(self.CFG, FakeCli(state=fresh_state(), writer="mallory"))
        self.assertIsNone(value)
        self.assertIn("mallory", why)

    def test_an_unprotected_branch_fails_closed(self) -> None:
        for rules in ([], [{"type": "update"}], None):
            value, why = colab.load_security_state(self.CFG, FakeCli(state=fresh_state(), rules=rules))
            self.assertIsNone(value)
            self.assertTrue("not protected" in why or "could not read" in why, why)
        sec_cfg = {"org": {"name": "sensein", "meta_repo": "lab-colab"}, "spaces": {"p": {"repos": ["proj"]}}}
        cli = FakeCli(state=fresh_state(), rules=[])
        gate = colab.gate_status(sec_cfg, "sensein/proj#3", gh=lambda a: {"login": "me"} if a == ["api", "user"] else cli(a))
        self.assertTrue(gate["unavailable"])  # fails closed above public

    def test_falls_back_to_the_variable_marked_deprecated(self) -> None:
        value, why = colab.load_security_state(self.CFG, FakeCli(state=None, variable=fresh_state()))
        self.assertEqual(why, "")
        self.assertIn("deprecated", colab.STATE_SOURCE["source"])
        value, why = colab.load_security_state(self.CFG, FakeCli(state=None, variable=None))
        self.assertIsNone(value)
        self.assertIn("unreadable", why)

    def test_stale_branch_state_still_fails_closed(self) -> None:
        old = fresh_state(issued_at="2026-01-01T00:00:00Z")
        self.assertIn("minutes old", colab.load_security_state(self.CFG, FakeCli(state=old))[1])


class PlainGateMessageTests(unittest.TestCase):
    def test_lease_refusal_leads_with_plain_words(self) -> None:
        c = {"org": {"name": "sensein", "meta_repo": "lab-colab"}, "spaces": {"p": {"repos": ["proj", "other/x"]}}}
        with mock.patch.object(colab, "gate_status", return_value={"state": "flagged", "unavailable": False, "reason": "flagged by the bus"}):
            msg = colab.lease_refusal(c, "sensein/proj#3", gh=lambda a: {"login": "me"})
        first, *rest = msg.splitlines()
        self.assertEqual(first, colab.GATE_PLAIN["flagged"])
        self.assertTrue(any(line.strip().startswith("Next:") for line in rest))
        self.assertTrue(any("details:" in line and "flagged by the bus" in line for line in rest))
        self.assertTrue(colab.lease_refusal(c, "other/x#1").startswith(colab.GATE_PLAIN["external"]))

    def test_run_gate_errors_lead_with_plain_words(self) -> None:
        sys.path.insert(0, str(SKILL / "scripts"))
        run = _load("colab_run", "scripts/colab_run.py")
        with self.assertRaises(run.RunError) as ctx:
            run.check_gate("flagged", "flagged by the bus", "lab", False, "sensein/proj#3")
        self.assertTrue(str(ctx.exception).startswith(colab.GATE_PLAIN["flagged"]))
        self.assertIn("security clear", str(ctx.exception))


class BusCommandTests(unittest.TestCase):
    def gh(self, lease=None):
        files = {"lease.json": lease} if lease else {}
        beats = {"satra-orcd": {"at": colab._iso(T0 - timedelta(minutes=2)), "active": True}}

        def gh(args):
            path = args[1]
            if path.startswith("repos/sensein/lab-colab/contents/heartbeat?"):
                return [{"name": "satra-orcd.json"}]
            for name, value in [*files.items(), *((f"heartbeat/{k}.json", v) for k, v in beats.items())]:
                if path.startswith(f"repos/sensein/lab-colab/contents/{name}?ref=colab-state"):
                    return {"content": base64.b64encode(json.dumps(value).encode()).decode(), "sha": "blob1"}
            return None
        return gh

    def test_status_in_plain_words(self) -> None:
        lease = colab.compose_bus_lease(colab.buses(cfg())[0], T0 - timedelta(minutes=2), 15.0)
        out = io.StringIO()
        self.assertEqual(colab.cmd_bus(cfg(), "status", gh=self.gh(lease), now=T0, out=out), 0)
        self.assertIn("The bus is running: satra-orcd (operator satra)", out.getvalue())
        out = io.StringIO()
        self.assertEqual(colab.cmd_bus(cfg(), "status", gh=self.gh(lease), now=T0 + timedelta(hours=1), out=out), 1)
        self.assertIn("No bus is running", out.getvalue())
        self.assertIn("kabilar, satra", out.getvalue())

    def test_handback_writes_the_lease_with_compare_and_set(self) -> None:
        lease = colab.compose_bus_lease(colab.buses(cfg())[1], T0 - timedelta(minutes=2), 15.0)
        writes = []
        rc = colab.cmd_bus(cfg(), "handback", "satra-orcd", gh=self.gh(lease), gh_input=lambda a, p: writes.append((a, p)) or {},
                           login_fn=lambda: "kabilar", now=T0, out=io.StringIO())
        self.assertEqual(rc, 0)
        [(args, payload)] = writes
        self.assertEqual(args, ["api", "-X", "PUT", "repos/sensein/lab-colab/contents/lease.json"])
        self.assertEqual((payload["sha"], payload["branch"]), ("blob1", "colab-state"))
        new = json.loads(base64.b64decode(payload["content"]))
        self.assertEqual((new["holder"], new["handed_from"], new["handed_by"]), ("satra-orcd", "kab-hpc", "kabilar"))
        err = io.StringIO()
        self.assertEqual(colab.cmd_bus(cfg(), "handback", "satra-orcd", gh=self.gh(lease), gh_input=lambda a, p: writes.append(1),
                                       login_fn=lambda: "mallory", now=T0, err=err), 2)
        self.assertEqual(len(writes), 1)
        self.assertEqual(colab.cmd_bus(cfg(), "handback", "nope", gh=self.gh(lease), login_fn=lambda: "satra", err=io.StringIO()), 2)
        out = io.StringIO()
        self.assertEqual(colab.cmd_bus(cfg(), "handback", "satra-orcd", gh=self.gh(lease), gh_input=lambda a, p: writes.append(1),
                                       login_fn=lambda: "satra", now=T0, dry_run=True, out=out), 0)
        self.assertEqual(len(writes), 1)


# --------------------------------------------------------------------------- plan and apply


def op_snapshot(**kw) -> dict:
    base = {"org": "sensein", "repos": [{"name": "lab-colab", "visibility": "private"}], "meta_repo_exists": True,
            "issue_types": [], "projects": [], "project_fields": {}, "forms": {}, "rollout": colab.empty_rollout(),
            "operators_team": None, "state_branch": False, "rulesets": []}
    base.update(kw)
    return base


class OperatorPlanTests(unittest.TestCase):
    def ops(self, snap, config=None):
        return colab.operator_actions(config or cfg(), snap)

    def test_fresh_org_order_team_manual_rulesets_then_branch(self) -> None:
        acts = self.ops(op_snapshot())
        descs = [a["desc"] for a in acts]
        self.assertTrue(descs[0].startswith("Create the team colab-bus-operators"))
        adds = [a for a in acts if "memberships" in " ".join(a["cmd"] or [])]
        self.assertEqual([a["kind"] for a in adds], ["manual", "manual"])  # never applied: membership grants push rights
        self.assertEqual([a["cmd"][4].rsplit("/", 1)[-1] for a in adds], ["kabilar", "satra"])
        rulesets = [a for a in acts if "rulesets" in " ".join(a["cmd"] or [])]
        self.assertEqual([json.loads(a["input"])["name"] for a in rulesets], [colab.RULESET_DEFAULT, colab.RULESET_STATE])
        [branch] = [a for a in acts if a.get("fn") == "state_init"]
        self.assertEqual(acts[-1], branch)  # the branch comes after both rulesets
        self.assertLess(acts.index(rulesets[1]), acts.index(branch))
        state_rs = json.loads(rulesets[1]["input"])
        self.assertEqual(state_rs["conditions"]["ref_name"]["include"], ["refs/heads/colab-state"])
        self.assertEqual({r["type"] for r in state_rs["rules"]}, {"update", "deletion", "non_fast_forward"})
        self.assertEqual(state_rs["bypass_actors"], [{"actor_id": colab.TEAM_ID_PLACEHOLDER, "actor_type": "Team", "bypass_mode": "always"}])
        default_rs = json.loads(rulesets[0]["input"])
        self.assertEqual(default_rs["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        pr = next(r for r in default_rs["rules"] if r["type"] == "pull_request")
        self.assertEqual(pr["parameters"]["required_approving_review_count"], 1)
        self.assertEqual(default_rs["bypass_actors"], [])
        self.assertFalse(any(a["kind"] == "gh" and "memberships" in " ".join(a["cmd"] or []) for a in colab.build_plan({**colab.load_config(), "buses": BUSES}, op_snapshot())))

    def test_converged_org_proposes_nothing_and_other_rulesets_are_never_touched(self) -> None:
        team = {"id": 42, "slug": "colab-bus-operators", "members": ["kabilar", "satra"]}
        want = colab.desired_rulesets(42)
        rulesets = [{"id": 1, **want[colab.RULESET_STATE]}, {"id": 2, **want[colab.RULESET_DEFAULT]},
                    {"id": 3, "name": "someone else's", "foreign": True}]
        self.assertEqual(self.ops(op_snapshot(operators_team=team, state_branch=True, rulesets=rulesets)), [])
        stricter = json.loads(json.dumps(want[colab.RULESET_DEFAULT]))
        stricter["rules"][2]["parameters"]["required_approving_review_count"] = 2
        stricter["rules"].append({"type": "required_signatures"})
        rulesets[1] = {"id": 2, **stricter}
        self.assertEqual(self.ops(op_snapshot(operators_team=team, state_branch=True, rulesets=rulesets)), [])

    def test_updates_only_add_and_never_remove(self) -> None:
        team = {"id": 42, "members": ["kabilar", "satra"]}
        want = colab.desired_rulesets(42)
        partial = json.loads(json.dumps(want[colab.RULESET_STATE]))
        partial["rules"] = [{"type": "update"}, {"type": "required_linear_history"}]  # deletion missing; one extra
        partial["conditions"]["ref_name"]["exclude"] = ["refs/heads/keep"]
        [act] = self.ops(op_snapshot(operators_team=team, state_branch=True,
                                     rulesets=[{"id": 7, **partial}, {"id": 2, **want[colab.RULESET_DEFAULT]}]))
        self.assertEqual(act["cmd"][:5], ["gh", "api", "-X", "PUT", "repos/sensein/lab-colab/rulesets/7"])
        body = json.loads(act["input"])
        types = [r["type"] for r in body["rules"]]
        self.assertEqual(types[:2], ["update", "required_linear_history"])  # existing rules kept, in place
        self.assertEqual(set(types), {"update", "required_linear_history", "deletion", "non_fast_forward"})
        self.assertEqual(body["conditions"]["ref_name"]["exclude"], ["refs/heads/keep"])
        self.assertEqual(body["bypass_actors"], want[colab.RULESET_STATE]["bypass_actors"])

    def test_conflicts_become_manual_notes_not_replacements(self) -> None:
        team = {"id": 42, "members": ["kabilar", "satra"]}
        want = colab.desired_rulesets(42)
        loose = json.loads(json.dumps(want[colab.RULESET_STATE]))
        loose["bypass_actors"].append({"actor_id": 9, "actor_type": "Team", "bypass_mode": "always"})
        weak = json.loads(json.dumps(want[colab.RULESET_DEFAULT]))
        weak["rules"][2]["parameters"]["required_approving_review_count"] = 0
        hidden = {k: v for k, v in want[colab.RULESET_DEFAULT].items() if k != "bypass_actors"}
        for rulesets in ([{"id": 7, **loose}, {"id": 2, **want[colab.RULESET_DEFAULT]}],
                         [{"id": 1, **want[colab.RULESET_STATE]}, {"id": 2, **weak}],
                         [{"id": 1, **want[colab.RULESET_STATE]}, {"id": 2, **hidden}],
                         [{"id": 1, **want[colab.RULESET_STATE], "enforcement": "evaluate"}, {"id": 2, **want[colab.RULESET_DEFAULT]}]):
            acts = self.ops(op_snapshot(operators_team=team, state_branch=True, rulesets=rulesets))
            self.assertEqual([a["kind"] for a in acts], ["manual"], acts)
            self.assertIn("apply only ever adds", acts[0]["desc"])

    def test_extra_team_members_get_a_manual_note_not_a_removal(self) -> None:
        team = {"id": 42, "members": ["kabilar", "satra", "breakglass"]}
        acts = self.ops(op_snapshot(operators_team=team, state_branch=True))
        self.assertTrue(any(a["kind"] == "manual" and "breakglass" in a["desc"] for a in acts))
        self.assertFalse(any("DELETE" in (a["cmd"] or []) for a in acts))

    def test_build_plan_only_with_an_observed_snapshot(self) -> None:
        base = colab.load_config()
        old = op_snapshot()
        for k in ("operators_team", "state_branch", "rulesets"):
            old.pop(k)
        self.assertFalse(any("colab-bus-operators" in a["desc"] for a in colab.build_plan(base, old)))
        self.assertTrue(any("colab-bus-operators" in a["desc"] for a in colab.build_plan(base, op_snapshot())))
        self.assertTrue(any(a["kind"] == "manual" and "[[buses]]" in a["desc"] for a in colab.build_plan(base, op_snapshot())))

    def test_observe_operators_reads_only_our_rulesets(self) -> None:
        calls = []

        def gh(args):
            calls.append(args[1])
            path = args[1]
            if path == "orgs/sensein/teams/colab-bus-operators":
                return {"id": 42, "slug": "colab-bus-operators"}
            if path.startswith("orgs/sensein/teams/colab-bus-operators/members"):
                return [{"login": "Satra"}]
            if path == "repos/sensein/lab-colab/branches/colab-state":
                return {"name": "colab-state"}
            if path.startswith("repos/sensein/lab-colab/rulesets?"):
                return [{"id": 1, "name": colab.RULESET_STATE}, {"id": 5, "name": "other"}]
            if path == "repos/sensein/lab-colab/rulesets/1":
                return {"id": 1, "name": colab.RULESET_STATE, "target": "branch", "rules": []}
            return None
        snap = colab.observe_operators(cfg(), gh)
        self.assertEqual(snap["operators_team"]["members"], ["satra"])
        self.assertTrue(snap["state_branch"])
        self.assertEqual([r["name"] for r in snap["rulesets"]], [colab.RULESET_STATE, "other"])
        self.assertNotIn("repos/sensein/lab-colab/rulesets/5", calls)

    def test_apply_resolves_the_team_id_and_runs_the_branch_step_last(self) -> None:
        acts = self.ops(op_snapshot())
        order = []

        def fake_run(cmd, **kw):
            order.append(("run", cmd, kw.get("input")))
            return mock.Mock(returncode=0, stdout="{}", stderr="")
        with mock.patch.object(colab.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(colab, "_gh_json", return_value={"id": 42}), \
                mock.patch.dict(colab.BUILTIN_STEPS, {"state_init": lambda c: order.append(("init", None, None)) or (True, "")}), \
                mock.patch("sys.stdout", new=io.StringIO()):
            rc = colab.apply(acts, yes=True, throttle=0, org="sensein", config_for_apply=cfg())
        self.assertEqual(rc, 0)
        self.assertEqual(order[-1][0], "init")
        posted = [json.loads(i) for kind, c, i in order if i]
        self.assertEqual([p["name"] for p in posted], [colab.RULESET_DEFAULT, colab.RULESET_STATE])
        self.assertEqual(posted[1]["bypass_actors"][0]["actor_id"], 42)
        self.assertFalse(any("memberships" in " ".join(c) for kind, c, i in order if c))

    def rules_gh(self, rules, bypass=None):
        def gh(args):
            path = args[1]
            if path == "repos/sensein/lab-colab/rules/branches/colab-state":
                return rules
            if path == "repos/sensein/lab-colab/rulesets/11":
                return {"id": 11, **({"bypass_actors": bypass} if bypass is not None else {})}
            if path == "orgs/sensein/teams/colab-bus-operators":
                return {"id": 42}
            return None
        return gh

    PROTECTED = [{"type": "update", "ruleset_id": 11}, {"type": "deletion", "ruleset_id": 11}, {"type": "non_fast_forward", "ruleset_id": 11}]

    def test_state_init_refuses_until_the_ruleset_is_active(self) -> None:
        calls = []

        def gh_input(args, payload):
            calls.append((args[-1], payload))
            return {"sha": f"s{len(calls)}", "ref": "refs/heads/colab-state"}
        ok, why = colab.state_init(cfg(), gh_input, gh=self.rules_gh([]))
        self.assertFalse(ok)
        self.assertIn("ruleset first", why)
        self.assertEqual(calls, [])
        ok, _ = colab.state_init(cfg(), gh_input, gh=self.rules_gh(self.PROTECTED, [{"actor_id": 42, "actor_type": "Team"}]))
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in calls], ["repos/sensein/lab-colab/git/trees", "repos/sensein/lab-colab/git/commits",
                                                  "repos/sensein/lab-colab/git/refs"])
        self.assertNotIn("base_tree", calls[0][1])
        self.assertEqual(calls[1][1]["parents"], [])

    def test_rules_refusal(self) -> None:
        self.assertIsNone(colab.state_rules_refusal(cfg(), self.rules_gh(self.PROTECTED)))  # bypass list not visible: rules suffice
        self.assertIsNone(colab.state_rules_refusal(cfg(), self.rules_gh(self.PROTECTED, [{"actor_id": 42, "actor_type": "Team"}])))
        self.assertIn("deletion", colab.state_rules_refusal(cfg(), self.rules_gh(self.PROTECTED[:1])))
        self.assertIn("could not read", colab.state_rules_refusal(cfg(), self.rules_gh(None)))
        why = colab.state_rules_refusal(cfg(), self.rules_gh(self.PROTECTED, [{"actor_id": 42, "actor_type": "Team"},
                                                                              {"actor_id": 1, "actor_type": "RepositoryRole"}]))
        self.assertIn("RepositoryRole", why)


# --------------------------------------------------------------------------- the operator's lesser token


class TokenUrlTests(unittest.TestCase):
    def test_prefilled_url_grants_only_the_lesser_permissions(self) -> None:
        url = colab.assistant_token_url("sensein")
        parsed = urlparse(url)
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://github.com/settings/personal-access-tokens/new")
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.assertEqual(q["target_name"], "sensein")
        self.assertLessEqual(len(q["name"]), 40)
        self.assertEqual(q["expires_in"], "90")
        self.assertEqual(q["contents"], "read")
        self.assertEqual(q["issues"], "write")
        self.assertEqual(q["organization_projects"], "write")
        self.assertEqual(q["metadata"], "read")
        for forbidden in colab.ASSISTANT_TOKEN_FORBIDDEN:
            self.assertNotIn(forbidden, q)
        self.assertNotIn("admin", q.values())
        steps = " ".join(colab.assistant_token_steps("sensein"))
        self.assertIn("Contents: Read-only", steps)
        self.assertIn("Variables", steps)


def fake_http(over=None):
    """GitHub as seen by a token; `over` replaces replies by (method, path prefix)."""
    replies = {("GET", "/user"): (200, {"login": "satra"}, {}),
               ("GET", "/repos/sensein/lab-colab/issues"): (200, [], {}),
               ("POST", "/graphql"): (200, {"data": {"organization": {"projectsV2": {"totalCount": 1}}}}, {}),
               ("GET", "/repos/sensein/lab-colab/actions/variables"): (403, None, {}),
               ("GET", "/repos/sensein/lab-colab/keys"): (403, None, {}),
               ("PATCH", "/repos/sensein/lab-colab/git/refs/heads/colab-state"): (403, None, {})}
    replies.update(over or {})
    seen = []

    def http(method, path, token, body=None):
        seen.append((method, path, body))
        for (m, prefix), reply in replies.items():
            if m == method and path.startswith(prefix):
                return reply
        return (404, None, {})
    http.seen = seen
    return http


GOOD = "github_pat_" + "A" * 60


class VerifyTokenTests(unittest.TestCase):
    def test_a_lesser_token_passes_and_nothing_is_written(self) -> None:
        http = fake_http()
        self.assertEqual(colab.verify_assistant_token(GOOD, "sensein", "lab-colab", "satra", http), [])
        writes = [(m, p, b) for m, p, b in http.seen if m not in ("GET",) and p != "/graphql"]
        self.assertEqual(writes, [("PATCH", "/repos/sensein/lab-colab/git/refs/heads/colab-state", {"sha": "0" * 40, "force": False})])

    def test_too_powerful_or_wrong_tokens_are_refused(self) -> None:
        cases = {
            "push to colab-state": fake_http({("PATCH", "/repos/sensein/lab-colab/git/refs/heads/colab-state"): (422, None, {})}),
            "Actions variables": fake_http({("GET", "/repos/sensein/lab-colab/actions/variables"): (200, {}, {})}),
            "admin rights": fake_http({("GET", "/repos/sensein/lab-colab/keys"): (200, [], {})}),
            "belongs to mallory": fake_http({("GET", "/user"): (200, {"login": "mallory"}, {})}),
            "Project board": fake_http({("POST", "/graphql"): (200, {"errors": [{"message": "no"}]}, {})}),
            "lab's items": fake_http({("GET", "/repos/sensein/lab-colab/issues"): (404, None, {})}),
        }
        for words, http in cases.items():
            problems = colab.verify_assistant_token(GOOD, "sensein", "lab-colab", "satra", http)
            self.assertTrue(any(words in p for p in problems), (words, problems))
        self.assertTrue(any("not a fine-grained" in p for p in colab.verify_assistant_token("ghp_" + "a" * 36, "sensein", "lab-colab", "satra", fake_http())))
        bad = colab.verify_assistant_token(GOOD, "sensein", "lab-colab", "satra", fake_http({("GET", "/user"): (401, None, {})}))
        self.assertEqual(len(bad), 1)
        self.assertIn("does not accept", bad[0])


# --------------------------------------------------------------------------- setup in plain words


GH_OK = "github.com\n  - Logged in\n  - Token scopes: 'gist', 'project', 'read:org', 'repo'\n"


def setup_runner(login="satra", org_state="active", team_state="active", clone_into=None):
    ran = []

    def runner(cmd, **kw):
        ran.append(cmd)
        if cmd[:3] == ["gh", "auth", "status"]:
            return mock.Mock(returncode=0, stdout=GH_OK, stderr="")
        if cmd[:3] == ["gh", "api", "user"]:
            return mock.Mock(returncode=0, stdout=login + "\n", stderr="")
        if cmd[:2] == ["gh", "api"] and cmd[2].startswith("user/memberships/orgs/"):
            if org_state is None:
                return mock.Mock(returncode=1, stdout="", stderr="HTTP 404")
            return mock.Mock(returncode=0, stdout=json.dumps({"state": org_state}), stderr="")
        if cmd[:2] == ["gh", "api"] and "/teams/colab-bus-operators/memberships/" in cmd[2]:
            return mock.Mock(returncode=0 if team_state else 1, stdout=json.dumps({"state": team_state}) if team_state else "", stderr="")
        if cmd[:3] == ["gh", "repo", "clone"]:
            dest = Path(cmd[4])
            (dest / ".colab").mkdir(parents=True)
            (dest / ".colab" / "config.toml").write_text('[org]\nname = "sensein"\nmeta_repo = "lab-colab"\n')
            return mock.Mock(returncode=0, stdout="", stderr="")
        return mock.Mock(returncode=0, stdout="1\n", stderr="")
    runner.ran = ran
    return runner


def write_cfg(tmp, buses_toml=True) -> Path:
    path = Path(tmp) / "meta" / ".colab" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[org]\nname = "sensein"\nmeta_repo = "lab-colab"\n'
                    + ('[[buses]]\nid = "satra-orcd"\noperator = "satra"\nhost = "MIT ORCD"\nrole = "primary"\n' if buses_toml else ""))
    return path


class PlainSetupTests(unittest.TestCase):
    def setup(self, home, **kw):
        runner = kw.pop("runner", None) or setup_runner()
        checks, rc = colab.cmd_setup(kw.pop("cfg", None), kw.pop("install", False), home=home, env=kw.pop("env", {}),
                                     runner=runner, which=lambda n: f"/usr/bin/{n}", skill_dir=Path("/opt/skills/lab-colab"), **kw)
        return {c["name"]: c for c in checks}, rc, runner

    def test_every_failure_has_one_plain_step_and_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            got, rc, _ = self.setup(Path(tmp), runner=setup_runner(org_state="pending"))
        self.assertEqual(rc, 1)
        for c in got.values():
            self.assertTrue(c["say"] and not c["say"].startswith("gh "), c)
            if c["status"] == "fail":
                self.assertTrue(c["step"], c)
                self.assertTrue(c["details"], c)
        self.assertEqual(got["org membership"]["link"], "https://github.com/orgs/sensein/invitation")
        self.assertIn("gh repo clone sensein/lab-colab", got["COLAB_CONFIG"]["step"])
        out = io.StringIO()
        colab.print_setup(list(got.values()), out=out)
        text = out.getvalue()
        self.assertIn("✗ You have an invitation to sensein waiting.", text)
        self.assertIn("    Next: accept the invitation", text)
        self.assertIn("    Link: https://github.com/orgs/sensein/invitation", text)
        self.assertIn("    details (org membership)", text)
        self.assertNotIn("details (python)", text)  # GitHub terms only for failures
        js = io.StringIO()
        colab.print_setup(list(got.values()), as_json=True, out=js)
        self.assertFalse(json.loads(js.getvalue())["ok"])

    def test_not_signed_in_offers_the_one_browser_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = setup_runner()

            def r(cmd, **kw):
                if cmd[:3] == ["gh", "auth", "status"]:
                    return mock.Mock(returncode=1, stdout="", stderr="not logged in")
                return runner(cmd, **kw)
            got, _, _ = self.setup(Path(tmp), runner=r)
        self.assertEqual(got["gh auth"]["step"].split("  ")[0], "gh auth login --web -s repo,read:org,project")
        self.assertNotIn("org membership", got)

    def test_clone_puts_the_meta_repo_in_the_default_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {"XDG_DATA_HOME": str(home / "data")}
            got, _, runner = self.setup(home, env=env, clone=True)
            dest = home / "data" / "lab-colab" / "sensein" / "lab-colab"
            self.assertIn(["gh", "repo", "clone", "sensein/lab-colab", str(dest)], runner.ran)
            self.assertEqual(got["COLAB_CONFIG"]["status"], "ok")
            self.assertIn(f"export COLAB_CONFIG={dest}/.colab/config.toml", got["COLAB_CONFIG"]["note"])
            got, _, runner = self.setup(home, env=env)  # found again without --clone
            self.assertEqual(got["COLAB_CONFIG"]["status"], "ok")
            self.assertFalse(any(c[:3] == ["gh", "repo", "clone"] for c in runner.ran))


class OperatorSetupTests(unittest.TestCase):
    def setup(self, tmp, reader=None, http=None, runner=None, settings=None, buses_toml=True):
        home = Path(tmp) / "home"
        if settings is not None:
            (home / ".claude").mkdir(parents=True, exist_ok=True)
            (home / ".claude" / "settings.json").write_text(json.dumps(settings))
        env = {"XDG_CONFIG_HOME": str(home / "cfg")}
        cfg_path = Path(tmp) / "meta" / ".colab" / "config.toml"
        if not cfg_path.exists():
            write_cfg(tmp, buses_toml)
        checks, rc = colab.cmd_setup(cfg_path, False, home=home, env=env, runner=runner or setup_runner(),
                                     which=lambda n: f"/usr/bin/{n}", skill_dir=Path("/opt/skills/lab-colab"),
                                     operator=True, token_reader=reader, http=http or fake_http())
        return {c["name"]: c for c in checks}, rc, home

    def test_without_a_token_it_offers_the_prefilled_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            got, rc, _ = self.setup(tmp)
        c = got["assistant token"]
        self.assertEqual(c["status"], "fail")
        self.assertEqual(c["link"], colab.assistant_token_url("sensein"))
        self.assertIn("paste it", c["step"])
        self.assertIn("contents=read", c["details"])
        self.assertEqual(got["bus entry"]["status"], "ok")
        self.assertEqual(got["operators team"]["status"], "ok")

    def test_a_good_token_is_stored_privately_and_used_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            got, _, home = self.setup(tmp, reader=lambda: GOOD + "\n", settings={"model": "opus", "env": {"A": "1"}})
            token_file = home / "cfg" / "lab-colab" / "assistant-token"
            hosts = home / "cfg" / "lab-colab" / "gh-assistant" / "hosts.yml"
            self.assertEqual(token_file.read_text().strip(), GOOD)
            for p in (token_file, hosts):
                self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(token_file.parent.stat().st_mode), 0o700)
            self.assertIn(f"oauth_token: {GOOD}", hosts.read_text())
            self.assertIn("user: satra", hosts.read_text())
            data = json.loads((home / ".claude" / "settings.json").read_text())
            self.assertEqual(data["env"], {"A": "1", "GH_CONFIG_DIR": str(hosts.parent)})
            self.assertEqual(data["model"], "opus")
            self.assertEqual(len(list((home / ".claude").glob("settings.json.colab-bak-*"))), 1)
            self.assertEqual(got["assistant token"]["status"], "ok")
            self.assertNotIn("cannot change", got["assistant token"]["say"])  # not a boundary: no such claim
            self.assertIn("not a boundary", got["assistant token"]["details"])
            self.assertIn("start the bus", got)
            self.assertIn("sbatch", got["start the bus"]["note"])
            # re-run: the stored token is re-verified, nothing else changes
            got, rc, _ = self.setup(tmp, reader=lambda: self.fail("must not ask again"))
            self.assertEqual(got["assistant uses it"]["status"], "ok")
            self.assertEqual(len(list((home / ".claude").glob("settings.json.colab-bak-*"))), 1)

    def test_a_too_powerful_token_is_never_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            http = fake_http({("PATCH", "/repos/sensein/lab-colab/git/refs/heads/colab-state"): (422, None, {})})
            got, rc, home = self.setup(tmp, reader=lambda: GOOD, http=http)
            self.assertEqual(rc, 1)
            self.assertIn("push to colab-state", got["assistant token"]["details"])
            self.assertFalse((home / "cfg" / "lab-colab" / "assistant-token").exists())
            self.assertFalse((home / ".claude" / "settings.json").exists())

    def test_missing_bus_entry_and_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            got, rc, _ = self.setup(tmp, runner=setup_runner(team_state=None), buses_toml=False)
        self.assertEqual(got["bus entry"]["status"], "fail")
        self.assertIn("[[buses]]", got["bus entry"]["step"])
        self.assertEqual(got["operators team"]["link"], "https://github.com/orgs/sensein/teams/colab-bus-operators")


# --------------------------------------------------------------------------- templates


class TemplateTests(unittest.TestCase):
    def watchdog_script(self) -> str:
        text = (SKILL / "assets" / "templates" / "colab-watchdog.yml").read_text()
        body = text.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        return textwrap.dedent(body)

    def run_watchdog(self, files: dict, env: dict | None = None) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "check.py"
            script.write_text(self.watchdog_script())
            work = Path(tmp) / "work"
            (work / "beats").mkdir(parents=True)
            for name, text in files.items():
                (work / name).write_text(text)
            out = work / "out.txt"
            proc = subprocess.run([sys.executable, "-I", str(script)], cwd=work, capture_output=True, text=True,
                                  env={"GITHUB_OUTPUT": str(out), "PATH": os.environ.get("PATH", ""), **(env or {})})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return out.read_text(), (work / "alert.txt").read_text()

    def test_watchdog_alerts_on_a_stale_lease_naming_operators(self) -> None:
        now = datetime.now(timezone.utc)
        config = '[[buses]]\nid = "satra-orcd"\noperator = "satra"\n[[buses]]\nid = "kab-hpc"\noperator = "kabilar"\n'
        stale = {"holder": "satra-orcd", "operator": "satra", "renewed_at": colab._iso(now - timedelta(hours=2)), "ttl_minutes": 15}
        out, alert = self.run_watchdog({"branch.txt": "colab-state\n", "config.toml": config, "lease.json": json.dumps(stale)})
        self.assertIn("stale=true", out)
        self.assertIn("@kabilar, @satra", alert)
        fresh = {**stale, "renewed_at": colab._iso(now - timedelta(minutes=2))}
        out, _ = self.run_watchdog({"branch.txt": "colab-state\n", "config.toml": config, "lease.json": json.dumps(fresh),
                                    "beats/satra-orcd.json": json.dumps({"active": True})})
        self.assertIn("stale=false", out)
        out, alert = self.run_watchdog({"branch.txt": "colab-state\n", "config.toml": config, "lease.json": json.dumps(fresh),
                                        "beats/satra-orcd.json": json.dumps({"security_foreign_edits": 1})})
        self.assertIn("stale=true", out)
        self.assertIn("security_foreign_edits=1", alert)

    def test_watchdog_falls_back_to_the_variable(self) -> None:
        beat = json.dumps({"at": datetime.now(timezone.utc).isoformat()})
        out, _ = self.run_watchdog({"config.toml": ""}, {"HEARTBEAT": beat})
        self.assertIn("stale=false", out)
        out, _ = self.run_watchdog({"config.toml": ""}, {"HEARTBEAT": ""})
        self.assertIn("stale=true", out)

    def test_sbatch_runs_as_the_operator_not_the_assistant(self) -> None:
        text = (SKILL / "assets" / "templates" / "colab-bus.sbatch").read_text()
        self.assertIn("{{BUS_ID}}", text)
        self.assertIn('--bus-id "$COLAB_BUS_ID"', text)
        self.assertIn("unset GH_TOKEN GITHUB_TOKEN", text)
        self.assertIn('GH_CONFIG_DIR="${COLAB_BUS_GH_CONFIG_DIR:-$HOME/.config/gh}"', text)
        self.assertNotIn("--token-file {{TOKEN_FILE}}", text)


if __name__ == "__main__":
    unittest.main()
