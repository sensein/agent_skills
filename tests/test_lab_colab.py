from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import tomllib
import unittest
from datetime import date, datetime, timedelta, timezone
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
        "forms": {}, "rollout": colab.empty_rollout(),
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
        for name in ("colab-bus.sbatch", "colab-watchdog.yml", "subscriptions.toml"):
            self.assertTrue((SKILL / "assets" / "templates" / name).is_file(), name)
        tomllib.loads((SKILL / "assets" / "templates" / "subscriptions.toml").read_text())

    def test_no_labels_are_created_up_front(self) -> None:
        self.assertTrue(all(lab.get("on_demand") for lab in CONFIG["labels"]))


class PlanTests(unittest.TestCase):
    def test_empty_org_creates_meta_repo_and_asks_for_spaces(self) -> None:
        actions = colab.build_plan(CONFIG, snapshot(meta_repo_exists=False, rollout=None))
        descs = [a["desc"] for a in actions]
        self.assertTrue(any("Create the private meta repo" in d for d in descs))
        self.assertTrue(any(a["kind"] == "manual" and "Define spaces" in a["desc"] for a in actions))
        self.assertEqual(sum("Create org issue type" in d for d in descs), 10)
        self.assertEqual(CONFIG["org"]["project_number"], 0)
        self.assertTrue(CONFIG["bus"]["project_sync"])
        self.assertEqual(CONFIG["bus"]["project_sync_every"], 6)

    def test_iteration_field_is_created_through_graphql(self) -> None:
        actions = colab.build_plan(CONFIG, snapshot(projects=[{"number": 7, "title": "Lab portfolio"}]),
                                   today=date(2026, 11, 10))  # a Tuesday
        self.assertFalse(any(a["kind"] == "manual" and "Iteration" in a["desc"] for a in actions))
        it = [a for a in actions if a["kind"] == "gh" and "iteration field" in a["desc"]]
        self.assertEqual(len(it), 1)
        query = next(p for p in it[0]["cmd"] if p.startswith("query="))
        self.assertIn("dataType: ITERATION", query)
        self.assertIn('startDate: "2026-11-16"', query)  # next Monday
        self.assertIn("duration: 14", query)
        self.assertEqual(query.count('title: "Cycle'), 4)
        self.assertIn('startDate: "2026-12-28"', query)  # fourth cycle
        self.assertIn("project=<project id of 7>", it[0]["cmd"])
        creates = [a for a in actions if a["kind"] == "gh" and "field-create" in a["cmd"]]
        self.assertTrue(all("7" in a["cmd"] for a in creates))

    def test_next_monday_is_strictly_after_today(self) -> None:
        self.assertEqual(colab.next_monday(date(2026, 11, 9)), date(2026, 11, 16))  # Monday -> a week on
        self.assertEqual(colab.next_monday(date(2026, 11, 15)), date(2026, 11, 16))  # Sunday

    def test_plan_creates_spaces_field_views_when_missing(self) -> None:
        cfg = config_with_spaces()
        cfg["spaces"]["other"] = {"repos": ["senselab"]}
        snap = snapshot(projects=[{"number": 7, "title": "Lab portfolio"}], project_views={"7": []})
        actions = colab.build_plan(cfg, snap)
        spaces = [a for a in actions if "Spaces" in a["desc"] and a["kind"] == "gh" and "Create Project field" in a["desc"]]
        self.assertEqual(len(spaces), 1)
        query = next(p for p in spaces[0]["cmd"] if p.startswith("query="))
        self.assertIn("dataType: MULTI_SELECT", query)
        self.assertIn('name: "proj"', query)
        self.assertIn('name: "other"', query)
        created = [a["desc"] for a in actions if a["desc"].startswith("Create Project view")]
        self.assertEqual(created, ["Create Project view 'Space: proj' (TABLE_LAYOUT)",
                                   "Create Project view 'Space: other' (TABLE_LAYOUT)",
                                   "Create Project view 'Agent queue' (TABLE_LAYOUT)",
                                   "Create Project view 'Proposals inbox' (TABLE_LAYOUT)",
                                   "Create Project view 'My work' (BOARD_LAYOUT)"])
        filters = {a["desc"].split("'")[1]: a["desc"].split("to: ", 1)[1] for a in actions if a["desc"].startswith("Set the filter")}
        self.assertEqual(filters["Space: proj"], "spaces:proj")
        self.assertEqual(filters["Agent queue"], "status:Ready agent-ready:yes")
        self.assertEqual(filters["Proposals inbox"], "status:Proposed")
        self.assertEqual(filters["My work"], "assignee:@me")
        # the filter step addresses the view by a placeholder resolved at apply time
        step = next(a for a in actions if a["desc"].startswith("Set the filter of view 'My work'"))
        self.assertIn("view=<view id of My work on project 7>", step["cmd"])
        self.assertTrue(any(a["desc"].startswith("Create the 'Iteration' iteration field") for a in actions))

    def test_plan_without_spaces_does_not_create_empty_spaces_field(self) -> None:
        actions = colab.build_plan(CONFIG, snapshot(projects=[{"number": 7, "title": "Lab portfolio"}], project_views={"7": []}))
        self.assertFalse(any("Spaces" in a["desc"] for a in actions if a["kind"] == "gh"))

    def test_plan_fixes_wrong_view_filter_and_adds_missing_spaces_options(self) -> None:
        cfg = config_with_spaces()
        cfg["spaces"]["other"] = {"repos": ["senselab"]}
        views = [{"name": v["name"], "layout": v["layout"], "filter": v["filter"]} for v in colab.desired_views(cfg)]
        views[0]["filter"] = 'repo:sensein/proj label:"space:proj"'
        fields = [{"name": f["name"], "options": f.get("options", [])} for f in cfg["fields"] if f["name"] != "Spaces"]
        fields.append({"name": "Spaces", "options": ["proj"]})
        snap = snapshot(projects=[{"number": 7, "title": "Lab portfolio"}], project_fields={"7": fields},
                        project_views={"7": views})
        actions = colab.build_plan(cfg, snap)
        fixes = [a for a in actions if a["desc"].startswith("Fix the filter")]
        self.assertEqual(len(fixes), 1)
        self.assertIn("'Space: proj'", fixes[0]["desc"])
        self.assertIn("view=<view id of Space: proj on project 7>", fixes[0]["cmd"])
        self.assertFalse(any(a["desc"].startswith("Create Project view") for a in actions))
        opts = [a for a in actions if a["desc"].startswith("Set the options of Project field Spaces")]
        self.assertEqual(len(opts), 1)
        self.assertIn("recomputes", opts[0]["desc"])
        self.assertIn("reset existing values", opts[0]["desc"])
        self.assertIn("multiSelectOptions", next(p for p in opts[0]["cmd"] if p.startswith("query=")))

    def test_unreadable_views_are_left_alone(self) -> None:
        actions = colab.build_plan(config_with_spaces(), snapshot(projects=[{"number": 7, "title": "Lab portfolio"}]))
        self.assertFalse(any(("Project view" in a["desc"] or "of view" in a["desc"]) and a["kind"] == "gh" for a in actions))
        self.assertTrue(any(a["kind"] == "manual" and "views of Project 7" in a["desc"] for a in actions))

    def test_space_filter_quotes_names_with_spaces(self) -> None:
        self.assertEqual(colab.space_filter("a-b"), "spaces:a-b")
        self.assertEqual(colab.space_filter("a b"), 'spaces:"a b"')

    def test_placeholders_resolve_at_apply_time(self) -> None:
        info = {"id": "PVT_1", "views": [{"id": "V1", "name": "My work"}], "fields": [{"id": "F1", "name": "Spaces"}]}
        orig_info, orig_json = colab._project_info, colab._gh_json
        colab._project_info = lambda org, n: info
        colab._gh_json = lambda args: {"id": "PVT_1"}
        try:
            self.assertEqual(colab._resolve_field_id("view=<view id of My work on project 7>", "o"), "view=V1")
            self.assertEqual(colab._resolve_field_id("id=<field id of Spaces on project 7>", "o"), "id=F1")
            self.assertEqual(colab._resolve_field_id("project=<project id of 7>", "o"), "project=PVT_1")
            with self.assertRaises(SystemExit):
                colab._resolve_field_id("view=<view id of Nope on project 7>", "o")
        finally:
            colab._project_info, colab._gh_json = orig_info, orig_json

    def test_converged_org_only_asks_for_gate(self) -> None:
        cfg = config_with_spaces()
        cfg["sessions"]["issue"] = 5
        snap = snapshot(
            issue_types=[t["name"] for t in cfg["types"]],
            projects=[{"number": 7, "title": "Lab portfolio"}],
            project_fields={"7": [{"name": f["name"], "options": list(cfg["spaces"]) if f["type"] == "MULTI_SELECT" else f.get("options", [])}
                                  for f in cfg["fields"]]},
            project_views={"7": colab.desired_views(cfg)},
            repos=[{"name": "lab-colab", "visibility": "private"}, {"name": "proj", "visibility": "private"},
                   {"name": ".github", "visibility": "public"}],
            forms={".github": [colab.form_filename(t["name"]) for t in cfg["types"]]},
            rollout={"spaces": {"proj": {"phase": -1, "bus": "dry-run"}}, "gates": []},
        )
        actions = colab.build_plan(cfg, snap)
        self.assertEqual([a["kind"] for a in actions], ["gate"])
        self.assertIn("--phase", actions[0]["cmd"])
        self.assertEqual(actions[0]["cmd"][actions[0]["cmd"].index("--phase") + 1], "0")

    def test_builtin_status_options_become_manual_step(self) -> None:
        snap = snapshot(projects=[{"number": 7, "title": "Lab portfolio"}],
                        project_fields={"7": [{"name": "Status", "options": ["Todo", "In Progress", "Done"]}]})
        steps = [a for a in colab.build_plan(CONFIG, snap) if "field Status" in a["desc"]]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["kind"], "gh")
        self.assertIn("updateProjectV2Field", " ".join(steps[0]["cmd"]))
        self.assertIn("id=<field id of Status on project 7>", steps[0]["cmd"])
        self.assertEqual(colab._resolve_field_id("id=plain"), "id=plain")

    def test_forms_go_org_wide_and_no_labels_are_planned(self) -> None:
        snap = snapshot(repos=snapshot()["repos"] + [{"name": ".github", "visibility": "public"}],
                        forms={"proj": ["bug.yml"]})
        actions = colab.build_plan(config_with_spaces(), snap)
        self.assertFalse(any("label" in (a.get("cmd") or []) for a in actions))
        forms = [a for a in actions if "org-wide issue forms" in a["desc"]]
        self.assertEqual(len(forms), 1)
        self.assertIn("<clone of sensein/.github>/.github/ISSUE_TEMPLATE", forms[0]["cmd"])
        self.assertTrue(any(a["kind"] == "manual" and "own issue templates" in a["desc"] and "proj" in a["desc"] for a in actions))
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
            written = colab.cmd_render_forms(CONFIG, tmp / "ISSUE_TEMPLATE")
            self.assertEqual(len(written), 10)
            self.assertNotIn("labels:", written[0].read_text())
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


def watched() -> dict:
    """State for a repo the bus already baselined before these test events."""
    return {"cursors": {"proj": "2026-11-08T00:00:00Z"}, "baseline_at": {"proj": "2026-11-08T00:00:00Z"}}


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
        env = {"kind": "release", "repo": "x", "labels": ["topic:vllm"], "spaces": ["proj"]}
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
            fake, state = FakeGH(), watched()
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
            bus.poll_once(fake, bus_config(), src_live, subs, watched(), Path(tmp), "me")
            self.assertEqual(fake.sent, [])  # destination still dry-run
            dest_live = {"spaces": {"dest": {"bus": "live"}}}
            bus.poll_once(fake, bus_config(), dest_live, subs, watched(), Path(tmp), "me")
            self.assertEqual(len(fake.sent), 1)
            body = fake.sent[0][2]
            self.assertNotIn("labels", body)
            self.assertIn(bus.DELIVERY_MARKER, body["body"])

    def test_own_deliveries_are_never_rerouted(self) -> None:
        delivered = [{"number": 9, "id": 9, "title": "[colab] x", "labels": [], "body": bus.DELIVERY_MARKER,
                      "state": "open", "created_at": "2026-11-09T02:00:00Z", "updated_at": "2026-11-09T02:00:00Z",
                      "html_url": "u", "user": {"login": "a-human"}}]
        with tempfile.TemporaryDirectory() as tmp:
            stats = bus.poll_once(FakeGH(issues=delivered), bus_config(), {"spaces": {}},
                                  [{"topic": "repo:proj", "subscriber": "dest"}], watched(), Path(tmp), "me")
            self.assertEqual(stats["envelopes"], 0)

    def test_public_destination_never_gets_lab_content(self) -> None:
        subs = [{"topic": "repo:proj", "subscriber": "dest"}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeGH(visibility={"proj": "private", "dest": "public", "lab-colab": "private"})
            stats = bus.poll_once(fake, bus_config(), {"spaces": {"dest": {"bus": "live"}}}, subs, watched(), Path(tmp), "me")
            self.assertEqual(fake.sent, [])
            self.assertEqual(stats["drops"], 1)

    def test_digest_above_threshold(self) -> None:
        subs = [{"topic": "repo:proj", "subscriber": f"r{i}"} for i in range(3)]
        vis = {"proj": "private", "lab-colab": "private", **{f"r{i}": "private" for i in range(3)}}
        cfg = bus_config(fanout_digest_threshold=2)
        cfg["spaces"]["meta"] = {"repos": ["lab-colab"]}
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeGH(visibility=vis)
            stats = bus.poll_once(fake, cfg, {"spaces": {"meta": {"bus": "live"}}}, subs, watched(), Path(tmp), "me")
            self.assertEqual(len(fake.sent), 1)
            self.assertIn("/lab-colab/issues", fake.sent[0][1])
            self.assertEqual(stats["deliveries"], 1)


class ConfigDrivenBusTests(unittest.TestCase):
    CFG = {"spaces": {"sails": {"repos": ["a", "shared"], "tier": "restricted"}, "speech": {"repos": ["shared", "b"]}}}

    def test_space_tier_and_membership_come_from_config(self) -> None:
        self.assertEqual(bus.repo_tiers(self.CFG), {"a": "restricted", "shared": "restricted"})
        self.assertEqual(bus.repo_spaces(self.CFG)["shared"], ["sails", "speech"])
        item = {"number": 1, "title": "t", "labels": [], "state": "open", "created_at": "x", "html_url": "u", "user": {"login": "a"}}
        env = bus.envelopes_for("issue", "shared", "private", item, "restricted", ["sails", "speech"])[0]
        self.assertEqual(env["tier"], "restricted")
        self.assertEqual(env["summary"], "")
        self.assertEqual(env["spaces"], ["sails", "speech"])
        self.assertEqual(bus.dest_tier("private", None, "restricted"), "restricted")

    def test_shared_repo_is_live_only_when_all_its_spaces_are(self) -> None:
        cfg = {"org": {"name": "sensein", "meta_repo": "lab-colab"},
               "spaces": {"src": {"repos": ["proj"]}, "x": {"repos": ["dest"]}, "y": {"repos": ["dest"]}},
               "bus": {"write_interval_seconds": 0}}
        subs = [{"topic": "repo:proj", "subscriber": "dest"}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeGH()
            bus.poll_once(fake, cfg, {"spaces": {"x": {"bus": "live"}}}, subs, watched(), Path(tmp), "me")
            self.assertEqual(fake.sent, [])

    def test_links_block_checked_once_per_body_and_commented_when_live(self) -> None:
        bad = [{"number": 7, "id": 7, "title": "t", "labels": [], "state": "open", "created_at": "2026-11-09T01:00:00Z",
                "updated_at": "2026-11-09T01:00:00Z", "html_url": "u", "user": {"login": "a"},
                "body": "```links\nfrobs: sensein/x#1\n```"}]
        cfg = {"org": {"name": "sensein", "meta_repo": "lab-colab"}, "spaces": {"proj": {"repos": ["proj"]}},
               "edges": CONFIG["edges"], "bus": {"write_interval_seconds": 0}}
        with tempfile.TemporaryDirectory() as tmp:
            fake, state = FakeGH(issues=bad), watched()
            live = {"spaces": {"proj": {"bus": "live"}}}
            bus.poll_once(fake, cfg, live, [], state, Path(tmp), "me")
            comments = [x for x in fake.sent if x[1].endswith("/issues/7/comments")]
            self.assertEqual(len(comments), 1)
            self.assertIn("unknown edge", comments[0][2]["body"])
            self.assertIn(bus.DELIVERY_MARKER, comments[0][2]["body"])
            bus.poll_once(fake, cfg, live, [], state, Path(tmp), "me")
            self.assertEqual(len([x for x in fake.sent if x[1].endswith("/comments")]), 1)


class BaselineTests(unittest.TestCase):
    def test_first_poll_baselines_and_routes_nothing(self) -> None:
        subs = [{"topic": "repo:proj", "subscriber": "dest"}]
        with tempfile.TemporaryDirectory() as tmp:
            fake, state = FakeGH(), {}
            live = {"spaces": {"dest": {"bus": "live"}}}
            stats = bus.poll_once(fake, bus_config(), live, subs, state, Path(tmp), "me")
            self.assertEqual(stats.get("baselined"), 2)  # proj and dest
            self.assertEqual(stats["envelopes"], 0)
            self.assertEqual(fake.sent, [])
            self.assertEqual(state["cursors"]["proj"], "2026-11-09T01:00:00Z")
            # the same old issue seen again after the baseline is still not routed
            bus.poll_once(fake, bus_config(), live, subs, state, Path(tmp), "me")
            self.assertEqual(fake.sent, [])

    def test_old_item_updated_after_baseline_is_not_reported_as_opened(self) -> None:
        old = [{"number": 2, "id": 2, "title": "old", "labels": [], "state": "open", "created_at": "2025-01-01T00:00:00Z",
                "updated_at": "2026-11-09T05:00:00Z", "html_url": "u", "user": {"login": "a"}}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeGH(issues=old)
            stats = bus.poll_once(fake, bus_config(), {"spaces": {"dest": {"bus": "live"}}},
                                  [{"topic": "repo:proj", "subscriber": "dest"}], watched(), Path(tmp), "me")
            self.assertEqual(fake.sent, [])
            self.assertEqual(stats["envelopes"], 0)  # remembered, never routed


class FakeProjectGH(FakeGH):
    """FakeGH plus a GraphQL endpoint serving a Project with a Spaces field; records mutations."""

    def __init__(self, items=None, fail_writes=False, **kw):
        super().__init__(**kw)
        self.items = items or []
        self.fail_writes = fail_writes
        self.mutations = []

    def graphql(self, query, variables=None):
        if query.lstrip().startswith("mutation"):
            if self.fail_writes:
                raise bus.GraphQLError("boom")
            self.mutations.append((query.split("{", 2)[1].split("(")[0].strip(), variables))
            return {}
        if "projectV2(number" in query:
            return {"organization": {"projectV2": {"id": "PVT", "field": {"id": "FLD", "options": [
                {"id": "o-proj", "name": "proj"}, {"id": "o-dest", "name": "dest"}]}}}}
        return {"node": {"items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.items}}}


def project_item(item_id, repo, labels=(), spaces=(), typename="Issue", owner="sensein"):
    return {"id": item_id, "content": {"__typename": typename, "repository": {"name": repo, "owner": {"login": owner}},
                                       "labels": {"nodes": [{"name": n} for n in labels]}},
            "fieldValueByName": {"options": [{"name": n} for n in spaces]} if spaces else None}


def project_config(**bus_opts) -> dict:
    cfg = bus_config(project_sync=True, project_sync_every=6, **bus_opts)
    cfg["org"]["project_number"] = 67
    return cfg


class ProjectSyncTests(unittest.TestCase):
    def test_desired_spaces_are_repo_membership_plus_labels(self) -> None:
        member = {"proj": ["proj", "dest"], "other": ["dest"]}
        self.assertEqual(bus.desired_spaces("proj", [], member), ["dest", "proj"])
        self.assertEqual(bus.desired_spaces("other", ["bug", "space:proj", "space:extra"], member), ["dest", "extra", "proj"])
        self.assertEqual(bus.desired_spaces("unlisted", ["bug"], member), [])

    def test_bus_adds_opened_item_and_skips_pre_baseline(self) -> None:
        issues = [
            {"number": 5, "id": 5, "node_id": "I_new", "title": "t", "labels": [], "state": "open",
             "created_at": "2026-11-09T01:00:00Z", "updated_at": "2026-11-09T01:00:00Z", "html_url": "u", "user": {"login": "a"}},
            {"number": 2, "id": 2, "node_id": "I_old", "title": "old", "labels": [], "state": "open",
             "created_at": "2025-01-01T00:00:00Z", "updated_at": "2026-11-09T05:00:00Z", "html_url": "u", "user": {"login": "a"}}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeProjectGH(issues=issues)
            stats = bus.poll_once(fake, project_config(), {"spaces": {}}, [], watched(), Path(tmp), "me")  # dry-run space
            adds = [v for name, v in fake.mutations if name == "addProjectV2ItemById"]
            self.assertEqual(adds, [{"project": "PVT", "content": "I_new"}])
            self.assertEqual(stats["project_writes"], 1)
            self.assertEqual(fake.sent, [])  # nothing delivered or written over REST

    def test_pre_baseline_poll_adds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeProjectGH()
            bus.poll_once(fake, project_config(), {"spaces": {}}, [], {}, Path(tmp), "me")
            self.assertEqual([n for n, _ in fake.mutations if n == "addProjectV2ItemById"], [])

    def test_restricted_item_is_not_added_unless_switch_on(self) -> None:
        issues = [{"number": 5, "id": 5, "node_id": "I_r", "title": "t", "labels": [{"name": "tier:restricted"}], "state": "open",
                   "created_at": "2026-11-09T01:00:00Z", "updated_at": "2026-11-09T01:00:00Z", "html_url": "u", "user": {"login": "a"}}]
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeProjectGH(issues=issues)
            bus.poll_once(fake, project_config(), {"spaces": {}}, [], watched(), Path(tmp), "me")
            self.assertEqual([n for n, _ in fake.mutations if n == "addProjectV2ItemById"], [])

    def test_disabled_or_unnumbered_does_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for cfg in (bus_config(project_sync=True), {**project_config(), "bus": {"write_interval_seconds": 0}}):
                fake = FakeProjectGH()
                stats = bus.poll_once(fake, cfg, {"spaces": {}}, [], watched(), Path(tmp), "me")
                self.assertEqual(fake.mutations, [])
                self.assertEqual(stats["project_writes"], 0)

    def test_periodic_sync_updates_only_items_that_differ(self) -> None:
        items = [
            project_item("same", "proj", spaces=["proj"]),
            project_item("stale", "proj", spaces=["dest"]),
            project_item("new", "dest"),
            project_item("labelled", "elsewhere", labels=["space:proj"]),
            project_item("cleared", "elsewhere", spaces=["proj"]),
            project_item("foreign", "proj", owner="someone-else"),
            {"id": "draft", "content": {"__typename": "DraftIssue"}, "fieldValueByName": None},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake, state = FakeProjectGH(items=items), watched()
            stats = bus.poll_once(fake, project_config(), {"spaces": {}}, [], state, Path(tmp), "me")
            done = {v["item"]: (n, v.get("options")) for n, v in fake.mutations}
            self.assertEqual(done, {
                "stale": ("updateProjectV2ItemFieldValue", ["o-proj"]),
                "new": ("updateProjectV2ItemFieldValue", ["o-dest"]),
                "labelled": ("updateProjectV2ItemFieldValue", ["o-proj"]),
                "cleared": ("clearProjectV2ItemFieldValue", None),
            })
            self.assertEqual(stats["project_writes"], 4)
            # the next polls skip the sweep until project_sync_every polls have passed
            fake.mutations.clear()
            for _ in range(5):
                bus.poll_once(fake, project_config(), {"spaces": {}}, [], state, Path(tmp), "me")
            self.assertEqual(fake.mutations, [])
            bus.poll_once(fake, project_config(), {"spaces": {}}, [], state, Path(tmp), "me")
            self.assertEqual(len(fake.mutations), 4)  # the fake does not apply writes, so the same four differ

    def test_write_failure_is_audited_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeProjectGH(items=[project_item("stale", "proj")], fail_writes=True)
            stats = bus.poll_once(fake, project_config(), {"spaces": {}}, [], watched(), Path(tmp), "me")
            self.assertEqual(stats["project_writes"], 0)
            self.assertGreaterEqual(stats["errors"], 1)
            audit = [json.loads(line) for line in (Path(tmp) / "audit.jsonl").read_text().splitlines()]
            self.assertTrue(any("project_write_failed" in a for a in audit))

    def test_graphql_method_posts_with_auth_and_raises_on_errors(self) -> None:
        import io
        import urllib.request

        seen = {}

        def fake_urlopen(req, timeout=0):
            seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
            seen["body"] = json.loads(req.data)
            return io.BytesIO(json.dumps(seen.get("reply")).encode())

        orig = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            gh = bus.GitHub("tok", {})
            seen["reply"] = {"data": {"x": 1}}
            self.assertEqual(gh.graphql("query { x }", {"a": 1}), {"x": 1})
            self.assertEqual(seen["url"], "https://api.github.com/graphql")
            self.assertEqual(seen["auth"], "Bearer tok")
            self.assertEqual(seen["body"], {"query": "query { x }", "variables": {"a": 1}})
            seen["reply"] = {"errors": [{"message": "no"}]}
            with self.assertRaises(bus.GraphQLError):
                gh.graphql("query { x }")
        finally:
            urllib.request.urlopen = orig


class SessionHeartbeatTests(unittest.TestCase):
    T0 = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)

    def test_marker_roundtrip(self) -> None:
        body = colab.session_comment("abc-123", "someone", self.T0, "28624405", "sensein/p#4")
        [s] = colab.parse_sessions([{"id": 9, "body": body}])
        self.assertEqual((s["session"], s["user"], s["job"], s["item"], s["comment_id"]),
                         ("abc-123", "someone", "28624405", "sensein/p#4", 9))
        self.assertEqual(s["beat"], self.T0)

    def test_states(self) -> None:
        s = {"beat": self.T0, "job": "77"}
        self.assertEqual(colab.session_state(s, self.T0 + timedelta(minutes=30), 60, set()), "active")
        self.assertEqual(colab.session_state(s, self.T0 + timedelta(hours=3), 60, {"77"}), "waiting")
        self.assertEqual(colab.session_state(s, self.T0 + timedelta(hours=3), 60, set()), "stale")

    def test_throttle(self) -> None:
        last = {"at": self.T0.isoformat(), "job": None, "item": "a/b#1"}
        self.assertFalse(colab.should_push(last, self.T0 + timedelta(minutes=5), 600, None, "a/b#1"))
        self.assertTrue(colab.should_push(last, self.T0 + timedelta(minutes=11), 600, None, "a/b#1"))
        self.assertTrue(colab.should_push(last, self.T0 + timedelta(minutes=1), 600, "5", "a/b#1"))
        self.assertTrue(colab.should_push(None, self.T0, 600, None, None))

    def test_throttled_beat_makes_no_network_call(self) -> None:
        import io, os
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}):
            state = Path(tmp) / "colab" / "beats"
            state.mkdir(parents=True)
            (state / "s1.json").write_text(json.dumps({"at": self.T0.isoformat(), "user": "u", "job": None, "item": None}))
            with mock.patch.object(colab, "_gh_json", side_effect=AssertionError("network")), \
                    mock.patch.object(colab, "claimed_item", return_value=None), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": tmp}))):
                rc = colab.cmd_beat(CONFIG, None, None, None, True, False, now=self.T0 + timedelta(minutes=2))
            self.assertEqual(rc, 0)

    def test_claim_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import subprocess
            subprocess.run(["git", "init", "-q", tmp], check=True)
            (Path(tmp) / ".git" / "colab-claim").write_text("sensein/p#12\n")
            self.assertEqual(colab.claimed_item(tmp), "sensein/p#12")
            (Path(tmp) / ".git" / "colab-claim").write_text("not an item")
            self.assertIsNone(colab.claimed_item(tmp))

    def test_default_lease_is_short_and_plan_creates_sessions_issue(self) -> None:
        self.assertEqual(CONFIG["sessions"]["lease_minutes"], 45)
        actions = colab.build_plan(CONFIG, snapshot())
        self.assertTrue(any("Session activity" in a["desc"] for a in actions))
        cfg = json.loads(json.dumps(CONFIG)); cfg["sessions"]["issue"] = 5
        self.assertFalse(any("Session activity" in a["desc"] for a in colab.build_plan(cfg, snapshot())))

    def test_bus_counts_sessions_and_audits_stale_claims_once(self) -> None:
        now_t = datetime.now(timezone.utc)
        comments = [{"id": 1, "body": colab.session_comment("a", "u1", now_t, None, None)},
                    {"id": 2, "body": colab.session_comment("b", "u2", now_t - timedelta(hours=5), "88", "sensein/p#1")},
                    {"id": 3, "body": colab.session_comment("c", "u3", now_t - timedelta(hours=5), None, "sensein/p#2")}]

        class GH:
            def get(self, path, cache=False):
                return comments if "/issues/5/comments" in path else []

        cfg = {"org": {"name": "sensein", "meta_repo": "lab-colab"}, "sessions": {"issue": 5, "stale_minutes": 60}}
        with tempfile.TemporaryDirectory() as tmp:
            state = {}
            stats = bus.session_stats(GH(), cfg, state, Path(tmp), jobs=lambda ids: {"88"} & ids)
            self.assertEqual(stats, {"sessions_active": 1, "sessions_waiting": 1, "sessions_stale": 1})
            bus.session_stats(GH(), cfg, state, Path(tmp), jobs=lambda ids: {"88"} & ids)
            audit = (Path(tmp) / "audit.jsonl").read_text().splitlines()
            self.assertEqual(len(audit), 1)
            self.assertIn("sensein/p#2", audit[0])


class NextTests(unittest.TestCase):
    def test_candidates_and_pick_next(self) -> None:
        def item(n, status="Ready", ready="yes", kind="Issue"):
            return {"status": status, "agent-ready": ready,
                    "content": {"type": kind, "number": n, "repository": "sensein/p", "title": f"t{n}", "url": f"u{n}"}}
        items = [item(1), item(2, status="Todo"), item(3, ready="no"), item(4, kind="PullRequest"), item(5), item(6)]
        cands = colab.candidates(items)
        self.assertEqual([c["number"] for c in cands], [1, 5, 6])
        t0 = datetime(2026, 11, 10, tzinfo=timezone.utc)
        lease = colab.compose_lease("someone", now=t0)
        picked = colab.pick_next(cands, {"u1": [lease]}, {"u5": True}, now=t0 + timedelta(hours=1))
        self.assertEqual([c["number"] for c in picked], [6])


if __name__ == "__main__":
    unittest.main()
