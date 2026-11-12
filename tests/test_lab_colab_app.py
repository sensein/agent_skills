"""The bus as a scheduled GitHub Actions workflow, signed in as the org's GitHub App (lab-colab).

No network: GitHub is an in-memory fake (shared with test_lab_colab_operators). openssl, when present, signs and seals
for real with throwaway keys made in a temporary folder.
"""
from __future__ import annotations

import base64
import html.parser
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_lab_colab_operators as ops  # noqa: E402  (loads colab and colab_bus once, with the hermetic setup)

colab, bus = ops.colab, ops.bus
T0, SKILL = ops.T0, ops.SKILL
setUpModule, tearDownModule = ops.setUpModule, ops.tearDownModule

BOT = "colab-bus[bot]"
ACTIONS = {"id": "actions", "kind": "actions", "app": "colab-bus", "app_id": 4242, "role": "primary"}
STANDBY = {"id": "satra-orcd", "operator": "satra", "host": "MIT ORCD", "role": "standby", "order": 1}
HAS_OPENSSL = shutil.which("openssl") is not None


def acfg(**extra) -> dict:
    c = {"org": {"name": "sensein", "meta_repo": "lab-colab"},
         "spaces": {"proj": {"repos": ["proj", "secret-repo", "partner/private-tool"], "stewards": ["satra"]}},
         "tiers": {"restricted": ["secret-repo"]},
         "bus": {"write_interval_seconds": 0, "poll_seconds": 600}, "buses": [dict(ACTIONS), dict(STANDBY)]}
    c.update(extra)
    return c


_KEYS: dict = {}


def rsa_key() -> tuple[str, Path]:
    """A throwaway RSA key (PEM text) and its public key file, made once per test run."""
    if not _KEYS:
        folder = Path(tempfile.mkdtemp(prefix="colab-app-test-"))
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out",
                        str(folder / "k.pem")], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(folder / "k.pem"), "-pubout", "-out", str(folder / "pub.pem")],
                       check=True, capture_output=True)
        _KEYS["pem"], _KEYS["pub"], _KEYS["dir"] = (folder / "k.pem").read_text(), folder / "pub.pem", folder
    return _KEYS["pem"], _KEYS["pub"]


def b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class AppGH(ops.FakeBusGH):
    """The fake GitHub, plus the rules endpoint and creating a branch from nothing."""

    def __init__(self, git, login=BOT, rules=("creation", "update", "deletion"), **kw):
        super().__init__(git, login, **kw)
        self.rules, self.reads = list(rules), []

    def get(self, path, cache=False, conditional=True, **kw):
        self.reads.append(path)
        if path == f"{self.BASE}/rules/branches/colab-state":
            return [{"type": t} for t in self.rules]
        return super().get(path, cache, conditional, **kw)

    def send(self, method, path, body):
        g, b = self.git, self.BASE
        if path == f"{b}/git/trees" and "base_tree" not in body:
            self.writes.append((method, path, body))
            return {"sha": g._tree({e["path"]: e["content"] for e in body["tree"]})}
        if path == f"{b}/git/refs":
            self.writes.append((method, path, body))
            if g.ref:
                raise urllib.error.HTTPError(path, 422, "Reference already exists", {}, None)
            g.ref = body["sha"]
            return {"ref": body["ref"]}
        return super().send(method, path, body)


def run(gh, config, state, tmp, now_t=T0, material=b"k" * 32, calls=None, run_id="r1", poll=None, bus_id="actions"):
    gh.git.clock = now_t
    remote = bus.BusStateStore(config, "actions", material, run=run_id)
    calls = calls if calls is not None else []
    return bus.cycle(gh, config, {"spaces": {}}, [], state, Path(tmp), gh.login, now_t=now_t, bus_id=bus_id,
                     poll=poll or ops.stub_poll(calls), remote=remote), remote


# --------------------------------------------------------------------------- config


class ActionsBusConfigTests(unittest.TestCase):
    def test_actions_entry_runs_as_the_apps_bot(self) -> None:
        entries = colab.buses(acfg())
        self.assertEqual([b["id"] for b in entries], ["actions", "satra-orcd"])
        a = entries[0]
        self.assertEqual((a["kind"], a["app"], a["app_id"], a["operator"], a["host"]),
                         ("actions", "colab-bus", 4242, BOT, "GitHub Actions"))
        self.assertEqual(entries[1]["kind"], "operator")
        self.assertEqual(colab.bus_operators(acfg()), ["satra"])  # the team: people only
        self.assertEqual(colab.bus_writers(acfg()), [BOT, "satra"])
        self.assertEqual(colab.bus_app_ids(acfg()), [4242])
        self.assertIsNone(colab.buses({"buses": [{**ACTIONS, "app_id": 0}]})[0]["app_id"])
        # scheduled runs can start late: longer default lease and security-state age with an Actions bus
        self.assertEqual(colab.lease_ttl_minutes({"bus": {"poll_seconds": 600}, "buses": [ACTIONS]}), 45.0)
        self.assertEqual(colab.lease_ttl_minutes({"bus": {"poll_seconds": 600, "lease_ttl_minutes": 20}, "buses": [ACTIONS]}), 20.0)
        self.assertEqual(colab.lease_ttl_minutes({"bus": {"poll_seconds": 600, "lease_ttl_minutes": 0}}), 30.0)
        for bad in ({**ACTIONS, "operator": "satra"}, {**ACTIONS, "app": "Not A Slug"}, {**ACTIONS, "app": ""},
                    {**ACTIONS, "app_id": "abc"}, {**ACTIONS, "kind": "robot"}):
            with self.assertRaises(colab.BusConfigError, msg=bad):
                colab.buses({"buses": [bad]})

    def test_bundled_config_shows_an_actions_bus_first(self) -> None:
        text = (SKILL / "assets" / "lab-colab.toml").read_text()
        block = text[text.index("# Buses."):text.index("[security]")]
        self.assertLess(block.index('# kind = "actions"'), block.index('# operator = "handle"'))
        self.assertIn('# app = "colab-bus"', block)
        self.assertEqual(colab.buses(colab.load_config()), [])
        example = "\n".join(line[2:] for line in block.splitlines() if line.startswith("# [[buses]]") or
                            re.match(r"^# [a-z_]+ = ", line))
        import tomllib
        parsed = colab.buses(tomllib.loads(example))
        self.assertEqual([(b["id"], b["kind"]) for b in parsed], [("actions", "actions"), ("handle-orcd", "operator")])


# --------------------------------------------------------------------------- App sign-in


@unittest.skipUnless(HAS_OPENSSL, "openssl is not installed")
class JwtTests(unittest.TestCase):
    def test_rs256_jwt_verifies_with_the_public_key(self) -> None:
        pem, pub = rsa_key()
        jwt = bus.app_jwt(123, pem, now_ts=1_700_000_000)
        head, body, sig = jwt.split(".")
        self.assertEqual(json.loads(b64d(head)), {"alg": "RS256", "typ": "JWT"})
        self.assertEqual(json.loads(b64d(body)), {"iat": 1_700_000_000 - 60, "exp": 1_700_000_000 + 540, "iss": "123"})
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sig").write_bytes(b64d(sig))
            (Path(tmp) / "data").write_bytes(f"{head}.{body}".encode())
            proc = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(Path(tmp) / "sig"),
                                   str(Path(tmp) / "data")], capture_output=True, text=True)
        self.assertIn("Verified OK", proc.stdout)

    def test_the_key_file_is_private_and_gone_after_signing(self) -> None:
        pem, _ = rsa_key()
        seen = {}
        real = subprocess.run

        def spy(cmd, **kw):
            key = Path(cmd[cmd.index("-sign") + 1])
            seen["mode"], seen["path"] = key.stat().st_mode & 0o777, key
            seen["dir_mode"] = key.parent.stat().st_mode & 0o777
            return real(cmd, **kw)
        bus.rs256_sign(b"data", pem, runner=spy)
        self.assertEqual((seen["mode"], seen["dir_mode"]), (0o600, 0o700))
        self.assertFalse(seen["path"].exists())
        self.assertFalse(seen["path"].parent.exists())

    def test_falls_back_to_cryptography_without_openssl(self) -> None:
        pem, _ = rsa_key()
        with_openssl = bus.rs256_sign(b"data", pem)
        try:
            import cryptography  # noqa: F401
        except ImportError:
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(bus.AppAuthError) as err:
                    bus.rs256_sign(b"data", pem)
            self.assertIn("openssl is not on PATH", str(err.exception))
            return
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(bus.rs256_sign(b"data", pem), with_openssl)  # PKCS#1 v1.5 is deterministic

    def test_a_bad_key_is_a_clear_error_not_a_traceback(self) -> None:
        with mock.patch.dict(sys.modules, {"cryptography": None, "cryptography.hazmat.primitives": None}):
            with self.assertRaises(bus.AppAuthError):
                bus.rs256_sign(b"data", "-----BEGIN PRIVATE " + "KEY-----\nnot a key\n-----END PRIVATE " + "KEY-----\n")


class FakeAppHttp:
    def __init__(self, expires="2027-01-15T09:00:00Z", fail=None):  # an hour after the tests' clock
        self.calls, self.n, self.expires, self.fail = [], 0, expires, fail

    def __call__(self, method, path, jwt, body=None):
        self.calls.append((method, path))
        if self.fail:
            raise urllib.error.HTTPError(path, self.fail, "no", {}, None)
        if path == "/app":
            return {"slug": "colab-bus", "id": 4242}
        if path == "/orgs/sensein/installation":
            return {"id": 7}
        if path == "/app/installations/7/access_tokens" and method == "POST":
            self.n += 1
            return {"token": f"ghs_test{self.n}", "expires_at": self.expires}
        raise AssertionError(path)


class AppAuthTests(unittest.TestCase):
    PEM = "-----BEGIN RSA PRIVATE " + "KEY-----\nAAAA\n-----END RSA PRIVATE " + "KEY-----\n"

    def auth(self, http, clock):
        return bus.AppAuth("4242", self.PEM, "sensein", http=http, clock=clock, signer=lambda data, key: b"sig")

    def test_mints_an_installation_token_for_the_org_and_reuses_it(self) -> None:
        http, t = FakeAppHttp(), {"now": 1_800_000_000.0}
        auth = self.auth(http, lambda: t["now"])
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            out = io.StringIO()
            with redirect_stdout(out):
                first = auth.token()
        self.assertEqual(first, "ghs_test1")
        self.assertEqual(out.getvalue(), "::add-mask::ghs_test1\n")  # hidden in the job log
        self.assertEqual(auth.login, BOT)
        self.assertEqual(http.calls, [("GET", "/app"), ("GET", "/orgs/sensein/installation"),
                                      ("POST", "/app/installations/7/access_tokens")])
        t["now"] += 49 * 60
        self.assertEqual(auth.token(), "ghs_test1")
        t["now"] += 2 * 60  # past 50 minutes: a new one, without asking for the App or installation again
        self.assertEqual(auth.token(), "ghs_test2")
        self.assertEqual(http.calls[-1], ("POST", "/app/installations/7/access_tokens"))
        self.assertEqual(len(http.calls), 4)

    def test_mints_again_close_to_expiry_and_never_prints_outside_actions(self) -> None:
        http, t = FakeAppHttp(), {"now": 1_800_000_000.0}
        auth = self.auth(http, lambda: t["now"])
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
            out = io.StringIO()
            with redirect_stdout(out):
                auth.token()
        self.assertEqual(out.getvalue(), "")
        auth._expires = t["now"] + 300  # GitHub says it expires in 5 minutes
        self.assertEqual(auth.token(), "ghs_test2")

    def test_jwt_carries_the_app_id_and_the_token_is_used_by_the_client(self) -> None:
        seen = []
        auth = bus.AppAuth("4242", self.PEM, "sensein", http=lambda m, p, jwt, b=None: (seen.append(jwt), FakeAppHttp()(m, p, jwt, b))[1],
                           clock=lambda: 1_800_000_000.0, signer=lambda data, key: b"sig")
        gh = bus.GitHub(auth.token, {})
        self.assertEqual(gh._headers()["Authorization"], "Bearer ghs_test1")
        self.assertEqual(json.loads(b64d(seen[0].split(".")[1]))["iss"], "4242")
        self.assertEqual(bus.GitHub("plain", {})._headers()["Authorization"], "Bearer plain")

    def test_refusals_are_plain(self) -> None:
        for code, words in ((401, "App id or private key is wrong"), (404, "not installed on sensein")):
            with self.assertRaises(bus.AppAuthError) as err:
                self.auth(FakeAppHttp(fail=code), lambda: 0.0).token()
            self.assertIn(words, str(err.exception))
        with self.assertRaises(bus.AppAuthError):
            bus.AppAuth("", self.PEM, "sensein")
        with self.assertRaises(bus.AppAuthError):
            bus.AppAuth("12", "not a key", "sensein")
        a, b = (bus.AppAuth("1", k, "o") for k in (self.PEM, self.PEM.replace("AAAA", "BBBB")))
        self.assertNotEqual(a.key_material(), b.key_material())
        self.assertNotIn(b"AAAA", a.key_material())


# --------------------------------------------------------------------------- the bus's own state on colab-state


def sample_state() -> dict:
    flag = {"flagged_at": "2026-11-12T10:00:00Z", "where": "body", "reason": "rules: override", "node_id": "N1"}
    return {
        "cursors": {"proj": "2026-11-12T11:00:00Z", "secret-repo": "2026-11-12T11:00:00Z"},
        "seen": ["proj:issue:1:opened", "secret-repo:issue:9:opened", "partner/private-tool:issue:3:opened"],
        "scanned": {"proj#1:body": "aa", "secret-repo#9:body": "bb", "proj#4:body": "cc"},
        "links_checked": {"proj#1": "aa", "secret-repo#9": "bb"},
        "security": {"proj#1": {**flag, "ref": "sensein/proj#1", "restricted": False},
                     "secret-repo#9": {**flag, "ref": "sensein/secret-repo#9", "restricted": True},
                     "proj#4": {**flag, "ref": "sensein/proj#4", "restricted": True}},  # a tier:restricted label in a lab repo
        "security_verified": {"clears": {"sensein/proj#1": {"by": "satra", "at": "2026-11-12T10:30:00Z"},
                                         "sensein/proj#4": {"by": "satra", "at": "2026-11-12T10:30:00Z"}}, "ignored": {}},
        "security_hot": {"sensein/proj#1": "x", "sensein/secret-repo#9": "x"},
        "outsider_flags": {"eve|sensein/secret-repo#9": {"day": "2026-11-12", "n": 1}},
        "etags": {"/repos/sensein/proj": "e1", "/repos/sensein/proj/issues?x": "e2", "/repos/sensein/secret-repo/issues?y": "e3"},
        "cache": {"/repos/sensein/proj": {"visibility": "private", "description": "CACHED BODY TEXT"}},
        "hook_pending": [{"key": "proj#1:body", "text": "WAITING ISSUE TEXT"}],
        "security_published": '{"v":1}',
        "_visibility": {"partner/private-tool": "private"},
        "_seen_index": {"x"},
        "bus_mode": "active",
    }


class BusStateStoreTests(unittest.TestCase):
    def store(self, material=b"m" * 32):
        return bus.BusStateStore(acfg(), "actions", material, run="r1")

    def test_keeps_no_item_text_and_drops_rebuildable_caches(self) -> None:
        text = self.store(None).dump(sample_state(), '{"v":1}')
        data = json.loads(text)
        for gone in ("CACHED BODY TEXT", "WAITING ISSUE TEXT", "_visibility", "_seen_index", '"cache"', "hook_pending"):
            self.assertNotIn(gone, text)
        self.assertNotIn("security_published", {k for k in data if k != "security_published_sha256"})
        self.assertEqual(data["etags"], {"/repos/sensein/proj/issues?x": "e2"})  # a cached path's ETag would return nothing
        self.assertEqual((data["v"], data["bus"], data["run"]), (1, "actions", "r1"))

    @unittest.skipUnless(HAS_OPENSSL, "openssl is not installed")
    def test_restricted_entries_are_sealed_and_come_back(self) -> None:
        store = self.store()
        text = store.dump(sample_state(), '{"v":1}')
        for hidden in ("secret-repo#9", "secret-repo:issue", "proj#4", "partner/private-tool:issue", "eve|", "/repos/sensein/secret-repo"):
            self.assertNotIn(hidden, text)
        data = json.loads(text)
        self.assertIn("proj#1", data["security"])
        self.assertIn("proj:issue:1:opened", data["seen"])
        self.assertEqual(data["sealed"]["alg"], bus.SEAL_ALG)
        git = ops.FakeGit()
        git.push(BOT, {store.path: text, "security/state.json": '{"v":1}'})
        sb = bus.StateBranch(AppGH(git), "sensein", "lab-colab")
        sb.load()
        state = {"etags": {"/keep": "e"}}
        etags = state["etags"]
        self.assertEqual(store.adopt(state, sb), "loaded")
        self.assertIs(state["etags"], etags)  # in place: the client holds this dict
        self.assertIn("/repos/sensein/secret-repo/issues?y", etags)
        self.assertEqual(set(state["security"]), {"proj#1", "secret-repo#9", "proj#4"})
        self.assertIn("secret-repo:issue:9:opened", state["seen"])
        self.assertIn("sensein/proj#4", state["security_verified"]["clears"])
        self.assertEqual(state["security_published"], '{"v":1}')  # matched its hash
        self.assertEqual(state["state_branch_last"], git.ref)
        self.assertEqual(store.adopted_run, "r1")

    @unittest.skipUnless(HAS_OPENSSL, "openssl is not installed")
    def test_a_rotated_key_or_tampering_drops_only_the_sealed_part_and_keeps_flags_hashed(self) -> None:
        text = self.store(b"old" * 11).dump(sample_state(), "x")
        published = json.dumps({"v": 1, "flags": {colab.hashed_ref("sensein/secret-repo#9"): {"at": "2026-11-12T10:00:00Z", "cat": "override"}},
                                "clears": {}, "ignored": {}, "seen": {}})
        for label, blob_text, material in (("rotated", text, b"new" * 11),
                                           ("tampered", text.replace('"tag":"', '"tag":"00'), b"old" * 11)):
            git = ops.FakeGit()
            git.push(BOT, {"bus/actions/state.json": blob_text, "security/state.json": published})
            sb = bus.StateBranch(AppGH(git), "sensein", "lab-colab")
            sb.load()
            state, log = {}, []
            self.assertEqual(self.store(material).adopt(state, sb, audit=log.append), "loaded-unsealed", label)
            self.assertNotIn("secret-repo#9", state["security"])
            self.assertIn(colab.hashed_ref("sensein/secret-repo#9"), state["security_inherited"]["flags"])
            self.assertEqual(state["security_published"], "")  # not what this bus published: counted as a foreign edit
            kinds = {next(iter(r)) for r in log}
            self.assertIn("bus_state_sealed_unreadable", kinds)
            self.assertEqual("bus_state_tampered" in kinds, label == "tampered")

    def test_without_a_seal_restricted_flags_are_kept_as_hashes(self) -> None:
        log = []
        with mock.patch.object(bus, "_openssl_enc", side_effect=bus.SealError("openssl is missing")):
            text = self.store().dump(sample_state(), None, audit=log.append)
        data = json.loads(text)
        self.assertNotIn("sealed", data)
        for hidden in ("secret-repo#9", "proj#4"):
            self.assertNotIn(hidden, text)
        carried = data["security_inherited"]
        self.assertEqual(set(carried["flags"]), {colab.hashed_ref("sensein/secret-repo#9"), colab.hashed_ref("sensein/proj#4")})
        self.assertIn(colab.hashed_ref("sensein/proj#4"), carried["clears"])  # its verified clear is kept too
        self.assertEqual(next(iter(log[0])), "bus_state_unsealed")
        # the published security state built from the adopted state lists them exactly as before
        before, _, _ = bus.security_state_value(acfg(), sample_state(), now_t=T0)
        after, _, _ = bus.security_state_value(acfg(), {**data, "security_verified": data.get("security_verified")}, now_t=T0)
        self.assertEqual(json.loads(before)["flags"], json.loads(after)["flags"])
        self.assertEqual(json.loads(before)["clears"], json.loads(after)["clears"])

    def test_size_is_capped_without_touching_the_security_state(self) -> None:
        state = sample_state()
        state["seen"] = [f"proj:issue:{i}:opened-{'x' * 40}" for i in range(20000)]
        state["scanned"] = {f"proj#{i}:comment:{i}": "a" * 16 for i in range(20000)}
        with mock.patch.object(bus, "BUS_STATE_CAP", 200_000):
            text = self.store(None).dump(state)
        data = json.loads(text)
        self.assertLessEqual(len(text.encode()), 200_001)
        self.assertEqual(data["seen"][-1], state["seen"][-1])  # the newest are kept
        self.assertIn("proj#1", data["security"])
        self.assertEqual(data["cursors"], state["cursors"])


# --------------------------------------------------------------------------- the cycle of an Actions bus


class ActionsCycleTests(unittest.TestCase):
    def test_one_commit_holds_lease_heartbeat_security_and_the_buss_state(self) -> None:
        git = ops.FakeGit()
        gh, calls = AppGH(git), []
        with tempfile.TemporaryDirectory() as tmp:
            stats, remote = run(gh, acfg(), {}, tmp, calls=calls)
        self.assertTrue(stats["active"], stats)
        self.assertEqual(git.ref_updates, 1)
        files = git.files()
        lease = json.loads(files["lease.json"])
        self.assertEqual((lease["holder"], lease["operator"]), ("actions", BOT))
        self.assertTrue(json.loads(files["heartbeat/actions.json"])["active"])
        self.assertIn("security/state.json", files)
        saved = json.loads(files["bus/actions/state.json"])
        self.assertEqual((saved["bus_mode"], saved["run"]), ("active", "r1"))
        self.assertEqual(git.commits[git.ref]["author"], BOT)
        self.assertFalse(any("/teams/" in p for p in gh.reads))  # an App is in no team; the ruleset is what lets it write
        self.assertEqual(calls[0]["writers"], {BOT, "satra"})

    def test_the_next_run_starts_from_the_saved_state(self) -> None:
        git = ops.FakeGit()

        def poll_with_cursor(gh, config, rollout, subs, state, state_dir, bot, save=lambda: None, store=None, **kw):
            state.setdefault("cursors", {})["proj"] = "2026-11-12T12:00:00Z"
            state.setdefault("seen", []).append("proj:issue:1:opened")
            stats = {"envelopes": 0, "deliveries": 0, "drops": 0, "dry_run": 0, "errors": 0}
            bus.publish_security_state(gh, config, state, state_dir, stats, store)
            return stats
        with tempfile.TemporaryDirectory() as tmp:
            run(AppGH(git), acfg(), {}, tmp, poll=poll_with_cursor)
        seen_state = {}

        def poll_check(gh, config, rollout, subs, state, state_dir, bot, save=lambda: None, store=None, **kw):
            seen_state.update(json.loads(json.dumps({k: v for k, v in state.items() if not k.startswith("_")})))
            stats = {"envelopes": 0, "deliveries": 0, "drops": 0, "dry_run": 0, "errors": 0}
            bus.publish_security_state(gh, config, state, state_dir, stats, store)
            return stats
        with tempfile.TemporaryDirectory() as tmp:
            stats, _ = run(AppGH(git), acfg(), {}, tmp, now_t=T0 + timedelta(minutes=10), poll=poll_check, run_id="r2")
            lines = ops.audit_lines(tmp)
        self.assertEqual(seen_state["cursors"]["proj"], "2026-11-12T12:00:00Z")
        self.assertIn("proj:issue:1:opened", seen_state["seen"])
        self.assertNotIn("security_foreign_edits", stats)
        self.assertFalse(any("lease_taken" in r for r in lines))  # still the holder: nothing re-inherited
        self.assertEqual(json.loads(git.files()["bus/actions/state.json"])["run"], "r2")

    def test_an_overlapping_run_of_the_same_bus_writes_nothing(self) -> None:
        git = ops.FakeGit()
        with tempfile.TemporaryDirectory() as tmp:
            run(AppGH(git), acfg(), {}, tmp)
        first = AppGH(git)

        def other_run():
            with tempfile.TemporaryDirectory() as tmp2:
                run(AppGH(git), acfg(), {}, tmp2, now_t=T0 + timedelta(minutes=10), run_id="r3")
        first.before_ref_update = other_run
        with tempfile.TemporaryDirectory() as tmp:
            stats, _ = run(first, acfg(), {}, tmp, now_t=T0 + timedelta(minutes=10), run_id="r2")
            lines = ops.audit_lines(tmp)
        self.assertTrue(stats.get("overlap"), stats)
        self.assertEqual(json.loads(git.files()["bus/actions/state.json"])["run"], "r3")  # the other run's commit stands
        self.assertTrue(any("bus_run_overlap" in r for r in lines))

    def test_a_foreign_commit_between_runs_is_reported(self) -> None:
        git = ops.FakeGit()
        with tempfile.TemporaryDirectory() as tmp:
            run(AppGH(git), acfg(), {}, tmp)
        git.push("mallory", {"security/state.json": '{"v":1,"flags":{}}'})
        with tempfile.TemporaryDirectory() as tmp:
            stats, _ = run(AppGH(git), acfg(), {}, tmp, now_t=T0 + timedelta(minutes=10), run_id="r2")
        self.assertEqual(stats.get("state_branch_foreign"), "mallory")
        self.assertGreaterEqual(stats.get("security_foreign_edits", 0), 1)

    def test_creates_colab_state_only_when_the_ruleset_is_active(self) -> None:
        git = ops.FakeGit(branch=False)
        with tempfile.TemporaryDirectory() as tmp:
            stats, _ = run(AppGH(git, rules=("update",)), acfg(), {}, tmp)
        self.assertTrue(stats["passive"])
        self.assertIn("ruleset is not active", stats["reason"])
        self.assertIsNone(git.ref)
        self.assertEqual(git.variables, {})  # never the deprecated Actions variables
        with tempfile.TemporaryDirectory() as tmp:
            stats, _ = run(AppGH(git), acfg(), {}, tmp)
        self.assertTrue(stats["active"], stats)
        self.assertIn("README.md", git.files())
        self.assertIn("lease.json", git.files())
        self.assertEqual(git.variables, {})

    def test_another_login_cannot_run_the_actions_bus(self) -> None:
        git = ops.FakeGit()
        with tempfile.TemporaryDirectory() as tmp:
            stats, _ = run(AppGH(git, login="satra"), acfg(), {}, tmp)
            self.assertTrue(stats["passive"])
            self.assertIn("runs as the App colab-bus", stats["reason"])
            stats, _ = run(AppGH(git, login="satra"), acfg(), {}, tmp, bus_id="satra-orcd")  # --app never runs an operator's bus
            self.assertTrue(stats["passive"])
            self.assertIn("--app runs only an Actions bus", stats["reason"])
        self.assertEqual(git.ref_updates, 0)

    def test_the_operator_standby_takes_over_when_the_workflow_stops(self) -> None:
        git = ops.FakeGit(team=("satra",))
        with tempfile.TemporaryDirectory() as tmp:
            run(AppGH(git), acfg(), {}, tmp)
        with tempfile.TemporaryDirectory() as tmp:
            stats = ops.run_cycle(ops.FakeBusGH(git, "satra"), acfg(), {}, tmp, "satra-orcd", now_t=T0 + timedelta(minutes=5))
            self.assertTrue(stats["passive"])  # the App's lease is fresh
            stats = ops.run_cycle(ops.FakeBusGH(git, "satra"), acfg(), {}, tmp, "satra-orcd", now_t=T0 + timedelta(minutes=40))
            self.assertTrue(stats["passive"])  # a late scheduled run is not a dead bus: 45 minutes with an Actions bus
            stats = ops.run_cycle(ops.FakeBusGH(git, "satra"), acfg(), {}, tmp, "satra-orcd", now_t=T0 + timedelta(minutes=50))
        self.assertTrue(stats["active"], stats)
        self.assertEqual(stats.get("took_over_from"), "actions")
        self.assertNotIn("bus/satra-orcd/state.json", git.files())  # an operator's bus keeps its state on its host

    def test_deliveries_by_the_apps_bot_count_for_the_key(self) -> None:
        marker = colab.delivery_marker("actions", BOT, "proj:issue:3:opened")
        gh = ops.FakeBusGH(ops.FakeGit(), BOT, items={"/repos/sensein/sub/issues?": [{"user": {"login": BOT}, "body": marker}]})
        self.assertTrue(bus.already_delivered(gh, "/repos/sensein/sub", "proj:issue:3:opened", "2026-11-12T00:00:00Z", {BOT, "satra"}))
        self.assertFalse(bus.already_delivered(gh, "/repos/sensein/sub", "proj:issue:3:opened", "2026-11-12T00:00:00Z", {"satra"}))


# --------------------------------------------------------------------------- members' gate: who may write colab-state


class GateTrustTests(unittest.TestCase):
    CFG = {"org": {"name": "sensein", "meta_repo": "lab-colab"}}

    def cli(self, bypass, app_id="4242", writer=BOT):
        meta = ('[[buses]]\nid = "actions"\nkind = "actions"\napp = "colab-bus"\n' + (f"app_id = {app_id}\n" if app_id else "")
                + 'role = "primary"\n')
        cli = ops.FakeCli(state=ops.fresh_state(), writer=writer)
        cli.meta = {"content": base64.b64encode(meta.encode()).decode()}
        inner = cli.__call__

        def call(args):
            path = args[1] if len(args) > 1 else ""
            if path == "repos/sensein/lab-colab/rulesets/11":
                return {"bypass_actors": bypass}
            if path == "orgs/sensein/teams/colab-bus-operators":
                return {"id": 99}
            return inner(args)
        return call

    def test_the_apps_bot_and_its_integration_bypass_are_trusted(self) -> None:
        both = [{"actor_type": "Team", "actor_id": 99}, {"actor_type": "Integration", "actor_id": 4242}]
        value, why = colab.load_security_state(self.CFG, self.cli(both))
        self.assertEqual(why, "")
        self.assertIsNotNone(value)

    def test_another_app_or_a_missing_app_id_fails_closed(self) -> None:
        other = [{"actor_type": "Integration", "actor_id": 777}]
        self.assertIn("Integration 777", colab.load_security_state(self.CFG, self.cli(other))[1])
        mine = [{"actor_type": "Integration", "actor_id": 4242}]
        self.assertIn("Integration 4242", colab.load_security_state(self.CFG, self.cli(mine, app_id=None))[1])
        self.assertIn("mallory", colab.load_security_state(self.CFG, self.cli(mine, writer="mallory"))[1])


# --------------------------------------------------------------------------- plan and apply


class AppPlanTests(unittest.TestCase):
    def snapshot(self, **kw) -> dict:
        snap = {"meta_repo_exists": True, "operators_team": {"id": 99, "slug": "colab-bus-operators", "members": ["satra"]},
                "state_branch": True, "rulesets": [], "apps": {}, "meta_secrets": [], "bus_workflow": False}
        snap.update(kw)
        return snap

    def test_rulesets_let_the_app_write_colab_state(self) -> None:
        want = colab.desired_rulesets(99, [4242])[colab.RULESET_STATE]["bypass_actors"]
        self.assertEqual(want, [{"actor_id": 99, "actor_type": "Team", "bypass_mode": "always"},
                                {"actor_id": 4242, "actor_type": "Integration", "bypass_mode": "always"}])
        have = {"id": 11, "name": colab.RULESET_STATE, "target": "branch", "enforcement": "active",
                **{k: v for k, v in colab.desired_rulesets(99)[colab.RULESET_STATE].items() if k in ("conditions", "rules", "bypass_actors")}}
        body, conflicts = colab.merge_ruleset(have, colab.desired_rulesets(99, [4242])[colab.RULESET_STATE])
        self.assertEqual(conflicts, [])
        self.assertEqual([x["actor_type"] for x in body["bypass_actors"]], ["Team", "Integration"])  # only added
        acts = colab.operator_actions(acfg(), self.snapshot(rulesets=[have]))
        put = next(a for a in acts if a["kind"] == "gh" and colab.RULESET_STATE in a["desc"])
        self.assertIn('"Integration"', put["input"])

    def test_owner_steps_in_order_all_manual_but_the_seed(self) -> None:
        cfg = acfg(buses=[{**ACTIONS, "app_id": None}])
        acts = colab.app_actions(cfg, self.snapshot())
        descs = [a["desc"] for a in acts]
        self.assertEqual([a["kind"] for a in acts], ["manual", "manual", "manual", "manual", "local"])
        self.assertIn("app-manifest", acts[0]["cmd"])
        self.assertIn("app_id", descs[1])
        self.assertIn("Install", descs[2])
        self.assertIn("secret-repo", descs[2])  # selected repos: the meta repo and every space repo in the org
        self.assertNotIn("partner/private-tool", descs[2])
        self.assertIn("gh secret set COLAB_APP_PRIVATE_KEY --repo sensein/lab-colab < <downloaded .pem file>", descs[3])
        self.assertIn("Delete the .pem file", descs[3])
        self.assertIn("--refresh-bus", acts[4]["cmd"])
        done = self.snapshot(apps={"colab-bus": {"exists": True, "id": 4242, "installed": True, "selection": "selected"}},
                             meta_secrets=["COLAB_APP_ID", "COLAB_APP_PRIVATE_KEY"], bus_workflow=True)
        self.assertEqual(colab.app_actions(acfg(), done), [])
        wrong = colab.app_actions(acfg(buses=[{**ACTIONS, "app_id": 1}]), done)
        self.assertEqual(len(wrong), 1)
        self.assertIn("is number 4242", wrong[0]["desc"])

    def test_build_plan_starts_the_workflow_and_leaves_the_branch_to_the_bus(self) -> None:
        snap = {**self.snapshot(state_branch=False), "repos": [{"name": "lab-colab", "visibility": "private"}],
                "issue_types": [], "projects": [], "project_fields": {}, "forms": {}, "rollout": {"spaces": {}}}
        acts = colab.build_plan(acfg(org={"name": "sensein", "meta_repo": "lab-colab", "project_title": "Lab portfolio"}), snap)
        start = next(a for a in acts if a["desc"].startswith("Start the bus"))
        self.assertEqual(start["cmd"][:3], ["gh", "workflow", "run"])
        self.assertTrue(colab.is_recurring(start))
        self.assertFalse(any(a.get("fn") == "state_init" for a in acts))
        self.assertTrue(any("created by the Actions bus on its first run" in a["desc"] for a in acts))

    def test_observe_apps_reads_names_only(self) -> None:
        calls = []

        def gh(args):
            calls.append(args[1])
            return {"orgs/sensein/installations?per_page=100": {"installations": [{"app_slug": "colab-bus", "app_id": 4242,
                                                                                  "repository_selection": "all"}]},
                    "apps/colab-bus": {"id": 4242, "slug": "colab-bus"},
                    "repos/sensein/lab-colab/actions/secrets?per_page=100": {"secrets": [{"name": "COLAB_APP_ID"}]},
                    "repos/sensein/lab-colab/contents/.github/workflows/colab-bus.yml": {"name": "colab-bus.yml"}}.get(args[1])
        out = colab.observe_apps(acfg(), gh)
        self.assertEqual(out["apps"]["colab-bus"], {"exists": True, "id": 4242, "installed": True, "selection": "all"})
        self.assertEqual((out["meta_secrets"], out["bus_workflow"]), (["COLAB_APP_ID"], True))
        self.assertFalse(any("/secrets/" in c for c in calls))  # never a secret by name
        self.assertEqual(colab.observe_apps(ops.cfg(), gh), {})  # no Actions bus: nothing read


class ManifestTests(unittest.TestCase):
    def test_manifest_is_private_with_no_webhook_events_or_admin(self) -> None:
        entry = colab.buses(acfg())[0]
        m = colab.app_manifest(acfg(), entry)
        self.assertEqual(m["name"], "colab-bus")
        self.assertIs(m["public"], False)
        self.assertEqual(m["hook_attributes"]["active"], False)
        self.assertEqual(m["default_events"], [])
        self.assertEqual(m["default_permissions"], {"metadata": "read", "issues": "write", "contents": "write",
                                                    "pull_requests": "read", "organization_projects": "write", "members": "read"})
        self.assertFalse(any("admin" in k or k in ("secrets", "workflows", "actions_variables") for k in m["default_permissions"]))

    def test_page_posts_exactly_the_manifest(self) -> None:
        entry = colab.buses(acfg())[0]
        m = colab.app_manifest(acfg(), {**entry, "app": "colab-bus"})
        page = colab.app_manifest_html(acfg(), {**m, "description": 'quote " and <tag> & amp'}, "st4te")

        class Form(html.parser.HTMLParser):
            found: dict = {}

            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                if tag == "form":
                    self.found["action"], self.found["method"] = a["action"], a["method"]
                if tag == "input" and a.get("name") == "manifest":
                    self.found["manifest"] = a["value"]
        f = Form()
        f.feed(page)
        self.assertEqual(f.found["action"], "https://github.com/organizations/sensein/settings/apps/new?state=st4te")
        self.assertEqual(f.found["method"], "post")
        self.assertEqual(json.loads(f.found["manifest"])["description"], 'quote " and <tag> & amp')
        self.assertNotIn("<script", page)

    def test_command_writes_the_page_and_prints_the_clicks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            self.assertEqual(colab.cmd_app_manifest(acfg(), Path(tmp), out=out), 0)
            page = Path(tmp) / "colab-bus-app.html"
            self.assertTrue(page.is_file())
            self.assertEqual(json.loads((Path(tmp) / "colab-bus-manifest.json").read_text())["name"], "colab-bus")
        text = out.getvalue()
        self.assertIn(page.resolve().as_uri(), text)
        self.assertIn("Create GitHub App for sensein", text)
        self.assertIn("gh secret set COLAB_APP_ID --repo sensein/lab-colab --body <App ID>", text)
        self.assertIn("rm <downloaded .pem file>", text)
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(colab.cmd_app_manifest(ops.cfg(), Path(tmp)), 2)


class AppSetupTests(unittest.TestCase):
    def runner(self, replies: dict):
        calls = []

        def run_(cmd, **kw):
            calls.append(cmd)
            path = cmd[2] if len(cmd) > 2 and cmd[:2] == ["gh", "api"] else ""
            data = replies.get(path)
            return subprocess.CompletedProcess(cmd, 0 if data is not None else 1, json.dumps(data) if data is not None else "", "")
        return run_, calls

    def checks(self, replies, config=None):
        found = []
        add = lambda name, status, say, details="", step="", link="", note="": found.append(  # noqa: E731
            {"name": name, "status": status, "say": say, "step": step, "details": details})
        run_, calls = self.runner(replies)
        colab._app_checks(add, config or acfg(), "sensein", "lab-colab", run_)
        return {c["name"]: c for c in found}, calls

    GOOD = {"apps/colab-bus": {"id": 4242},
            "orgs/sensein/installations?per_page=100": {"installations": [{"app_slug": "colab-bus", "id": 7, "repository_selection": "all"}]},
            "repos/sensein/lab-colab/actions/secrets?per_page=100": {"secrets": [{"name": "COLAB_APP_ID"}, {"name": "COLAB_APP_PRIVATE_KEY"}]},
            "repos/sensein/lab-colab/contents/.github/workflows/colab-bus.yml": {"name": "colab-bus.yml"},
            "repos/sensein/lab-colab/rulesets?includes_parents=false&per_page=100": [{"id": 11, "name": colab.RULESET_STATE}],
            "repos/sensein/lab-colab/rulesets/11": {"bypass_actors": [{"actor_type": "Integration", "actor_id": 4242}]}}

    def test_everything_in_place(self) -> None:
        found, calls = self.checks(self.GOOD)
        self.assertTrue(all(c["status"] != "fail" for c in found.values()), found)
        self.assertIn("only the workflow can read them", found["secrets"]["say"])
        self.assertFalse(any("/actions/secrets/" in " ".join(c) for c in calls))  # names only, never a secret's value
        self.assertIn("has not run yet", found["last run"]["say"])

    def test_each_missing_piece_has_one_plain_next_step(self) -> None:
        replies = {**self.GOOD, "apps/colab-bus": None, "repos/sensein/lab-colab/actions/secrets?per_page=100": {"secrets": []},
                   "orgs/sensein/installations?per_page=100": {"installations": []},
                   "repos/sensein/lab-colab/contents/.github/workflows/colab-bus.yml": None,
                   "repos/sensein/lab-colab/rulesets/11": {"bypass_actors": [{"actor_type": "Team", "actor_id": 99}]}}
        found, _ = self.checks({k: v for k, v in replies.items() if v is not None})
        for name in ("the App", "installed", "secrets", "workflow", "may write state"):
            self.assertEqual(found[name]["status"], "fail", name)
            self.assertTrue(found[name]["step"], name)
        self.assertIn("app-manifest", found["the App"]["step"])
        self.assertIn("gh secret set COLAB_APP_PRIVATE_KEY", found["secrets"]["step"])
        found, _ = self.checks(self.GOOD, ops.cfg())
        self.assertEqual(found["actions bus"]["status"], "fail")

    def test_not_an_owner_is_said_plainly_not_failed(self) -> None:
        replies = {k: v for k, v in self.GOOD.items() if "installations" not in k and "secrets" not in k and "rulesets" not in k}
        found, _ = self.checks(replies)
        for name in ("installed", "secrets", "may write state"):
            self.assertEqual(found[name]["status"], "info", name)


# --------------------------------------------------------------------------- the workflow and seeding


class WorkflowTemplateTests(unittest.TestCase):
    TEXT = (SKILL / "assets" / "templates" / "colab-bus.yml").read_text()

    def test_schedule_dispatch_concurrency_and_timeout(self) -> None:
        t = self.TEXT
        self.assertRegex(t, r'(?m)^  schedule:\n    - cron: "\*/10 \* \* \* \*"  # every 10 minutes')
        self.assertRegex(t, r"(?m)^  workflow_dispatch:")
        self.assertRegex(t, r"(?m)^concurrency:\n  group: colab-bus\n  cancel-in-progress: false")
        self.assertRegex(t, r"(?m)^permissions: \{\}")
        self.assertRegex(t, r"(?m)^    timeout-minutes: \d+")
        job_perms = re.search(r"(?m)^    permissions:\n((?:      .*\n)+)", t).group(1)
        self.assertEqual([line.split("#")[0].strip() for line in job_perms.splitlines()], ["contents: read"])

    def test_only_pinned_first_party_actions_and_no_secret_in_a_command(self) -> None:
        uses = re.findall(r"uses: (\S+)", self.TEXT)
        self.assertEqual([u.split("@")[0] for u in uses], ["actions/checkout", "actions/setup-python"])
        for u in uses:
            self.assertRegex(u, r"@[0-9a-f]{40}$")
        self.assertIn("persist-credentials: false", self.TEXT)
        self.assertIn('python-version: "3.11"', self.TEXT)
        run_lines = re.findall(r"(?m)^        run: (.*)$", self.TEXT)
        self.assertEqual(run_lines, ["python bus/colab_bus.py --once --app"])
        self.assertNotIn("echo", self.TEXT)
        for line in self.TEXT.splitlines():
            if "secrets." in line and not line.lstrip().startswith("#"):
                self.assertRegex(line, r"^          [A-Z_]+: \$\{\{ secrets\.[A-Z_]+ \}\}$")

    def test_parses_as_yaml_when_available(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")
        doc = yaml.safe_load(self.TEXT)
        self.assertEqual(doc["concurrency"], {"group": "colab-bus", "cancel-in-progress": False})
        self.assertEqual(doc["permissions"], {})
        self.assertEqual(doc["jobs"]["poll"]["steps"][-1]["env"]["COLAB_APP_ID"], "${{ secrets.COLAB_APP_ID }}")

    def test_seed_adds_it_and_refresh_replaces_only_the_bus_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            written = colab.cmd_seed(dest, None)
            self.assertIn(dest / ".github/workflows/colab-bus.yml", written)
            (dest / "bus" / "colab_bus.py").write_text("old\n")
            (dest / ".colab" / "config.toml").write_text("# the lab's own\n")
            self.assertEqual(colab.cmd_seed(dest, None), [])
            again = colab.cmd_seed(dest, None, refresh_bus=True)
            self.assertEqual(again, [dest / "bus" / "colab_bus.py"])
            self.assertEqual((dest / ".colab" / "config.toml").read_text(), "# the lab's own\n")

    def test_watchdog_points_at_the_workflow_not_at_a_bot(self) -> None:
        from datetime import datetime, timezone
        tests = ops.TemplateTests()
        config = ('[[buses]]\nid = "actions"\nkind = "actions"\napp = "colab-bus"\nrole = "primary"\n'
                  '[[buses]]\nid = "satra-orcd"\noperator = "satra"\n')
        stale = {"holder": "actions", "operator": BOT, "renewed_at": colab._iso(datetime.now(timezone.utc) - timedelta(hours=2)),
                 "ttl_minutes": 30}
        out, alert = tests.run_watchdog({"branch.txt": "colab-state\n", "config.toml": config, "lease.json": json.dumps(stale)})
        self.assertIn("stale=true", out)
        self.assertIn("Actions tab (App colab-bus)", alert)
        self.assertIn("@satra", alert)
        self.assertNotIn("[bot]", alert)


class MainAppTests(unittest.TestCase):
    def meta(self, tmp: str) -> Path:
        meta = Path(tmp) / "meta"
        (meta / ".colab").mkdir(parents=True)
        (meta / ".colab" / "config.toml").write_text(
            '[org]\nname = "sensein"\nmeta_repo = "lab-colab"\n[[buses]]\nid = "actions"\nkind = "actions"\napp = "colab-bus"\n'
            'app_id = 4242\nrole = "primary"\n')
        return meta

    def test_missing_secrets_fail_the_run_with_a_plain_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"COLAB_APP_ID": "", "COLAB_APP_PRIVATE_KEY": ""}):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = bus.main(["--meta", str(self.meta(tmp)), "--once", "--app"])
        self.assertEqual(rc, 1)
        self.assertIn("App sign-in failed: COLAB_APP_ID must be the App's number", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())

    def test_one_poll_as_the_app_with_its_state_on_colab_state(self) -> None:
        class Auth:
            login = BOT

            def token(self):
                return "ghs_x"

            def key_material(self):
                return b"m" * 32
        seen = {}

        def fake_cycle(gh, config, rollout, subs, state, state_dir, login, save, **kw):
            seen.update(login=login, remote=kw["remote"], state_dir=state_dir, gh_token=os.environ.get("GH_TOKEN"),
                        key=os.environ.get("COLAB_APP_PRIVATE_KEY"), headers=gh._headers())
            return {"active": True, "errors": 0}
        env = {"COLAB_APP_ID": "4242", "COLAB_APP_PRIVATE_KEY": "pem"}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env), \
                mock.patch.object(bus.AppAuth, "from_env", return_value=Auth()), mock.patch.object(bus, "cycle", fake_cycle), \
                mock.patch.object(bus.subprocess, "run") as git:
            with redirect_stdout(io.StringIO()) as out:
                rc = bus.main(["--meta", str(self.meta(tmp)), "--once", "--app"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["login"], BOT)
        self.assertEqual((seen["remote"].bus_id, seen["remote"].path), ("actions", "bus/actions/state.json"))
        self.assertEqual(seen["headers"]["Authorization"], "Bearer ghs_x")
        self.assertIsNone(seen["gh_token"])  # the token stays in this process: not exported to child processes
        self.assertIsNone(seen["key"])  # the private key is out of the environment before anything runs
        self.assertTrue(str(seen["state_dir"]).startswith(tempfile.gettempdir()))
        self.assertFalse(git.called)  # a fresh checkout: no git pull with any token
        self.assertIn('"audit": {}', out.getvalue())

    def test_an_operator_bus_still_needs_a_state_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                bus.main(["--meta", str(self.meta(tmp)), "--once"])


if __name__ == "__main__":
    unittest.main()
