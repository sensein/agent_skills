from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills" / "lab-colab"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, SKILL / rel)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


colab = _load("colab", "scripts/colab.py")
bus = _load("colab_bus", "scripts/colab_bus.py")
CONFIG = colab.load_config()


def snapshot(**overrides) -> dict:
    base = {
        "org": "sensein", "repos": [{"name": "lab-colab", "visibility": "private"},
                                    {"name": "proj", "visibility": "private"},
                                    {"name": "senselab", "visibility": "public"}],
        "meta_repo_exists": True, "issue_types": [], "projects": [], "project_fields": {},
        "labels": {}, "forms": {}, "rollout": colab.empty_rollout(),
    }
    base.update(overrides)
    return base


def config_with_spaces() -> dict:
    cfg = json.loads(json.dumps(CONFIG))
    cfg["spaces"] = {"proj": {"steward": "someone", "repos": ["proj"]}}
    return cfg


class ConfigTests(unittest.TestCase):
    def test_bundled_config_parses_and_is_complete(self) -> None:
        self.assertEqual(len(CONFIG["types"]), 10)
        colors = {"gray", "blue", "green", "yellow", "orange", "red", "pink", "purple"}
        for t in CONFIG["types"]:
            self.assertIn(t["color"], colors, t["name"])
            self.assertTrue(t["fields"], t["name"])
        self.assertEqual([p["id"] for p in CONFIG["phases"]], [0, 1, 2, 3])

    def test_templates_exist(self) -> None:
        for name in ("colab-bus.sbatch", "colab-watchdog.yml", "colab-links-check.yml", "subscriptions.toml"):
            self.assertTrue((SKILL / "assets" / "templates" / name).is_file(), name)
        tomllib.loads((SKILL / "assets" / "templates" / "subscriptions.toml").read_text())

    def test_rendered_links_check_matches_config_and_script(self) -> None:
        text = colab.render_links_check(CONFIG)
        self.assertIn(colab.LINK_LINE.pattern, text)
        self.assertNotIn("_EDGES}}", text)
        allowed = json.loads(re.search(r"ALLOWED = set\((.*?)\)", text).group(1))
        self.assertEqual(set(allowed), set(CONFIG["edges"]["body"]))


class PlanTests(unittest.TestCase):
    def test_empty_org_creates_meta_repo_and_asks_for_spaces(self) -> None:
        actions = colab.build_plan(CONFIG, snapshot(meta_repo_exists=False, rollout=None))
        descs = [a["desc"] for a in actions]
        self.assertTrue(any("Create the private meta repo" in d for d in descs))
        self.assertTrue(any(a["kind"] == "manual" and "Define spaces" in a["desc"] for a in actions))
        self.assertEqual(sum("Create org issue type" in d for d in descs), 10)

    def test_iteration_field_is_manual(self) -> None:
        actions = colab.build_plan(CONFIG, snapshot(projects=[{"number": 7, "title": "Lab portfolio"}]))
        manual = [a for a in actions if a["kind"] == "manual" and "Iteration" in a["desc"]]
        self.assertEqual(len(manual), 1)
        creates = [a for a in actions if a["kind"] == "gh" and "field-create" in a["cmd"]]
        self.assertTrue(all("7" in a["cmd"] for a in creates))

    def test_converged_org_only_asks_for_gate(self) -> None:
        cfg = config_with_spaces()
        labels = [lab["name"] for lab in cfg["labels"]]
        snap = snapshot(
            issue_types=[t["name"] for t in cfg["types"]],
            projects=[{"number": 7, "title": "Lab portfolio"}],
            project_fields={"7": [{"name": f["name"], "options": f.get("options", [])} for f in cfg["fields"]]},
            labels={"proj": labels + ["space:proj"], "lab-colab": labels},
            forms={"proj": [colab.form_filename(t["name"]) for t in cfg["types"]]},
            rollout={"spaces": {"proj": {"phase": -1, "bus": "dry-run"}}, "gates": []},
        )
        actions = colab.build_plan(cfg, snap)
        self.assertEqual([a["kind"] for a in actions], ["gate"])
        self.assertIn("--phase", actions[0]["cmd"])
        self.assertEqual(actions[0]["cmd"][actions[0]["cmd"].index("--phase") + 1], "0")

    def test_builtin_status_options_become_manual_step(self) -> None:
        snap = snapshot(projects=[{"number": 7, "title": "Lab portfolio"}],
                        project_fields={"7": [{"name": "Status", "options": ["Todo", "In Progress", "Done"]}]})
        steps = [a for a in colab.build_plan(CONFIG, snap) if a["kind"] == "manual" and "Status" in a["desc"]]
        self.assertEqual(len(steps), 1)
        self.assertIn("Proposed", steps[0]["desc"])

    def test_space_labels_and_forms_planned(self) -> None:
        actions = colab.build_plan(config_with_spaces(), snapshot())
        self.assertTrue(any("space:proj" in a["desc"] for a in actions))
        self.assertTrue(any("issue forms to proj" in a["desc"] for a in actions))
        self.assertTrue(any("dry-run" in a["desc"] for a in actions))

    def test_apply_without_yes_runs_nothing(self) -> None:
        actions = [{"kind": "gh", "phase": 0, "desc": "x", "cmd": ["false"]}]
        self.assertEqual(colab.apply(actions, yes=False, throttle=0), 0)


class GateTests(unittest.TestCase):
    def test_gates_open_switches_in_order(self) -> None:
        r = colab.empty_rollout()
        colab.record_gate(r, CONFIG, "proj", 0, "https://example/pr/1", today="2026-11-09")
        self.assertEqual(r["spaces"]["proj"]["bus"], "live")
        self.assertTrue(r["spaces"]["proj"]["switches"]["bus_live"])
        colab.record_gate(r, CONFIG, "proj", 1, "https://example/pr/2", today="2026-11-10")
        self.assertTrue(r["spaces"]["proj"]["switches"]["restricted"])
        self.assertEqual(len(r["gates"]), 2)

    def test_cannot_skip_a_phase(self) -> None:
        with self.assertRaises(ValueError):
            colab.record_gate(colab.empty_rollout(), CONFIG, "proj", 2, "x")


class ScanTests(unittest.TestCase):
    def test_selftest_passes_with_bundled_patterns(self) -> None:
        self.assertEqual(colab.cmd_scan(CONFIG, [], selftest=True), 0)

    def test_clean_text_has_no_findings(self) -> None:
        counts = colab.scan_text("A public note about senselab releases.", CONFIG["scan"]["patterns"])
        self.assertFalse(any(counts.values()))

    def test_scan_flags_paths_and_tokens(self) -> None:
        text = "see /" + "orcd/data/x/y and gh" + "p_" + "b" * 36
        counts = colab.scan_text(text, CONFIG["scan"]["patterns"])
        self.assertGreaterEqual(counts["path"], 1)
        self.assertGreaterEqual(counts["secret"], 1)


class LinksTests(unittest.TestCase):
    BODY = "Intro\n\n```links\nuses: sensein/senselab#412\naddresses: kg:q/child-asr\n# a comment\nbogus line\n```\n"

    def test_parse_reports_bad_lines(self) -> None:
        edges, errors = colab.parse_links(self.BODY)
        self.assertEqual([e["edge"] for e in edges], ["uses", "addresses"])
        self.assertEqual(len(errors), 1)

    def test_validate_domain_native_and_kg_only(self) -> None:
        edges = [{"edge": "addresses", "target": "kg:x"}, {"edge": "part-of", "target": "a/b#1"},
                 {"edge": "contradicts", "target": "kg:y"}, {"edge": "frobs", "target": "a/b#2"}]
        errors = colab.validate_links(edges, CONFIG, source_type="Software")
        self.assertEqual(len(errors), 4)
        self.assertEqual(colab.validate_links(edges[:1], CONFIG, source_type="Experiment"), [])


class LeaseTests(unittest.TestCase):
    def test_lease_roundtrip_and_expiry(self) -> None:
        t0 = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        marker = colab.compose_lease("someone", hours=4, now=t0)
        self.assertEqual(colab.live_lease([marker], now=t0 + timedelta(hours=1))["user"], "someone")
        self.assertIsNone(colab.live_lease([marker], now=t0 + timedelta(hours=5)))


class FormsAndSeedTests(unittest.TestCase):
    def test_render_forms_and_seed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            written = colab.cmd_render_forms(CONFIG, tmp / "ISSUE_TEMPLATE", tmp / "workflows")
            self.assertEqual(len(written), 11)
            exp = (tmp / "ISSUE_TEMPLATE" / "colab-experiment.yml").read_text()
            self.assertIn('type: "Experiment"', exp)
            self.assertIn("id: hypothesis", exp)
            seeded = colab.cmd_seed(tmp / "meta", None)
            names = {p.name for p in seeded}
            self.assertTrue({"config.toml", "rollout.json", "colab_bus.py", "colab-bus.sbatch.template", ".gitignore"} <= names)
            # the seeded meta repo must pass its own phase-0 scan
            files = [str(x) for x in (tmp / "meta").rglob("*") if x.is_file()]
            self.assertEqual(colab.cmd_scan(CONFIG, files, selftest=False), 0)
            self.assertEqual(colab.cmd_seed(tmp / "meta", None), [])  # never overwrites

    def test_forms_are_valid_yaml_when_pyyaml_present(self) -> None:
        try:
            import yaml  # type: ignore
        except ImportError:
            self.skipTest("PyYAML not installed")
        for t in CONFIG["types"]:
            data = yaml.safe_load(colab.render_form(t))
            self.assertEqual(data["type"], t["name"])


class FakeGH:
    """Serves one private space repo with one issue; records writes."""

    def __init__(self, visibility=None, issues=None):
        self.visibility = visibility or {"proj": "private", "dest": "private", "lab-colab": "private"}
        self.issues = issues if issues is not None else [
            {"number": 5, "id": 5, "title": "t", "labels": [], "state": "open", "created_at": "2026-11-09T01:00:00Z",
             "updated_at": "2026-11-09T01:00:00Z", "html_url": "u", "user": {"login": "a"}}]
        self.sent = []

    def get(self, path, cache=False):
        name = path.rsplit("/", 1)[-1]
        if path.count("/") == 3 and name in self.visibility:
            return {"visibility": self.visibility[name]}
        if "/issues?" in path and "/proj/" in path:
            return self.issues
        return []

    def send(self, method, path, body):
        self.sent.append((method, path, body))
        return {}


def bus_config(**bus) -> dict:
    return {"org": {"name": "sensein", "meta_repo": "lab-colab"},
            "spaces": {"proj": {"repos": ["proj"]}, "dest": {"repos": ["dest"]}},
            "bus": {"write_interval_seconds": 0, **bus}}


class BusTests(unittest.TestCase):
    def test_tiers(self) -> None:
        self.assertEqual(bus.tier_of("public", []), "public")
        self.assertEqual(bus.tier_of("private", []), "lab")
        self.assertEqual(bus.tier_of("public", ["tier:restricted"]), "restricted")
        self.assertTrue(bus.may_deliver("public", "lab"))
        self.assertFalse(bus.may_deliver("lab", "public"))

    def test_dest_tier_fails_closed_and_override_only_tightens(self) -> None:
        self.assertEqual(bus.dest_tier(None), "public")
        self.assertEqual(bus.dest_tier("public", "restricted"), "restricted")
        self.assertEqual(bus.dest_tier("private", "public"), "lab")

    def test_envelopes_are_per_transition(self) -> None:
        item = {"number": 3, "title": "secret title", "labels": [{"name": "tier:restricted"}], "state": "closed",
                "created_at": "2026-11-09T00:00:00Z", "closed_at": "2026-11-10T00:00:00Z", "html_url": "u", "user": {"login": "a"}}
        envs = bus.envelopes_for("issue", "proj", "private", item)
        self.assertEqual([e["transition"] for e in envs], ["opened", "closed"])
        self.assertEqual(envs[0]["id"], "proj:issue:3:opened")
        self.assertTrue(all(e["summary"] == "" and e["tier"] == "restricted" for e in envs))

    def test_route_drops_looser_unknown_and_restricted(self) -> None:
        subs = [{"topic": "repo:proj", "subscriber": "pub"}, {"topic": "repo:proj", "subscriber": "other"},
                {"topic": "repo:proj", "subscriber": "unknown"}]
        env = {"id": "e", "kind": "issue", "repo": "proj", "tier": "lab", "labels": [], "sender": "a"}
        deliveries, drops = bus.route(env, subs, {"pub": "public", "other": "lab"}, bot_login="me")
        self.assertEqual([d["to"] for d in deliveries], ["other"])
        self.assertEqual(sorted(d["to"] for d in drops), ["pub", "unknown"])
        self.assertEqual(bus.route({**env, "sender": "me"}, subs, {}, "me"), ([], []))
        restricted = {**env, "tier": "restricted"}
        self.assertEqual(bus.route(restricted, subs[1:2], {"other": "restricted"}, "me")[0], [])
        self.assertEqual(len(bus.route(restricted, subs[1:2], {"other": "restricted"}, "me", restricted_ok=True)[0]), 1)

    def test_label_and_space_topics(self) -> None:
        env = {"kind": "release", "repo": "x", "labels": ["topic:vllm", "space:proj"]}
        self.assertTrue(bus.matches({"topic": "label:topic:vllm"}, env))
        self.assertTrue(bus.matches({"topic": "space:proj"}, env))
        self.assertFalse(bus.matches({"topic": "space:proj", "events": ["issue"]}, env))

    def test_seen_trims_oldest_first(self) -> None:
        state: dict = {}
        old_cap = bus.SEEN_CAP
        bus.SEEN_CAP = 3
        try:
            for i in ["z1", "a2", "m3", "b4"]:
                self.assertTrue(bus.remember(state, i))
            self.assertEqual(state["seen"], ["a2", "m3", "b4"])
            self.assertTrue(bus.remember(state, "z1"))  # forgotten, so new again
        finally:
            bus.SEEN_CAP = old_cap

    def test_dry_run_writes_nothing_and_dedupes(self) -> None:
        subs = [{"topic": "repo:proj", "subscriber": "dest"}]
        with tempfile.TemporaryDirectory() as tmp:
            fake, state = FakeGH(), {}
            stats = bus.poll_once(fake, bus_config(), {"spaces": {}}, subs, state, Path(tmp), "colab-bus[bot]")
            self.assertEqual(stats["dry_run"], 1)
            self.assertEqual(fake.sent, [])
            self.assertEqual(state["cursors"]["proj"], "2026-11-09T01:00:00Z")
            again = bus.poll_once(fake, bus_config(), {"spaces": {}}, subs, state, Path(tmp), "colab-bus[bot]")
            self.assertEqual(again["envelopes"], 0)

    def test_live_is_decided_by_destination_and_marks_deliveries(self) -> None:
        subs = [{"topic": "repo:proj", "subscriber": "dest"}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeGH()
            src_live = {"spaces": {"proj": {"bus": "live"}}}
            bus.poll_once(fake, bus_config(), src_live, subs, {}, Path(tmp), "me")
            self.assertEqual(fake.sent, [])  # destination still dry-run
            dest_live = {"spaces": {"dest": {"bus": "live"}}}
            bus.poll_once(fake, bus_config(), dest_live, subs, {}, Path(tmp), "me")
            self.assertEqual(len(fake.sent), 1)
            body = fake.sent[0][2]
            self.assertIn(bus.DELIVERY_LABEL, body["labels"])
            self.assertIn(bus.DELIVERY_MARKER, body["body"])

    def test_own_deliveries_are_never_rerouted(self) -> None:
        delivered = [{"number": 9, "id": 9, "title": "[colab] x", "labels": [{"name": bus.DELIVERY_LABEL}],
                      "state": "open", "created_at": "2026-11-09T02:00:00Z", "updated_at": "2026-11-09T02:00:00Z",
                      "html_url": "u", "user": {"login": "a-human"}}]
        with tempfile.TemporaryDirectory() as tmp:
            stats = bus.poll_once(FakeGH(issues=delivered), bus_config(), {"spaces": {}},
                                  [{"topic": "repo:proj", "subscriber": "dest"}], {}, Path(tmp), "me")
            self.assertEqual(stats["envelopes"], 0)

    def test_public_destination_never_gets_lab_content(self) -> None:
        subs = [{"topic": "repo:proj", "subscriber": "dest"}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeGH(visibility={"proj": "private", "dest": "public", "lab-colab": "private"})
            stats = bus.poll_once(fake, bus_config(), {"spaces": {"dest": {"bus": "live"}}}, subs, {}, Path(tmp), "me")
            self.assertEqual(fake.sent, [])
            self.assertEqual(stats["drops"], 1)

    def test_digest_above_threshold(self) -> None:
        subs = [{"topic": "repo:proj", "subscriber": f"r{i}"} for i in range(3)]
        vis = {"proj": "private", "lab-colab": "private", **{f"r{i}": "private" for i in range(3)}}
        cfg = bus_config(fanout_digest_threshold=2)
        cfg["spaces"]["meta"] = {"repos": ["lab-colab"]}
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeGH(visibility=vis)
            stats = bus.poll_once(fake, cfg, {"spaces": {"meta": {"bus": "live"}}}, subs, {}, Path(tmp), "me")
            self.assertEqual(len(fake.sent), 1)
            self.assertIn("/lab-colab/issues", fake.sent[0][1])
            self.assertEqual(stats["deliveries"], 1)


if __name__ == "__main__":
    unittest.main()
