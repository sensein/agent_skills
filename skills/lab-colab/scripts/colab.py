#!/usr/bin/env python3
"""Bootstrap and operate a Lab Colab Space on GitHub (stdlib only, Python 3.11+).

The tool is re-entrant: every run observes the org, diffs it against the
desired config and the rollout state, and proposes the next actions.

Subcommands
  status        Observe the org with `gh` and print a JSON snapshot.
  plan          Diff config + snapshot + rollout state into ordered actions (dry run).
  apply         Run the plan's `gh` actions; needs --yes. File changes are never
                applied here: they are printed as local steps for a reviewed PR.
  seed          Write the meta-repo starter files into a local checkout.
  render-forms  Write one issue form per work type into a directory (org-wide: the .github repo).
  next          List Ready, agent-ready, unblocked, unleased items on the org Project.
  scan          Leak-scan files or stdin; exit 1 on any finding. --selftest checks
                that every seeded case is caught.
  links         Parse and validate the fenced ```links block of an issue body.
  lease         Compose a claim lease marker for an issue comment (default 45 minutes).
  beat          Session heartbeat: refresh this session's Session activity comment and renew
                its lease. Called by Claude Code hooks; throttled; costs no model tokens.
  seed-marketplace  Write the private plugin-marketplace repo's starter files (one plugin per skill).
  setup         Member onboarding check (gh scopes, Python, COLAB_CONFIG, hooks); --install-hooks
                merges the heartbeat hooks into ~/.claude/settings.json.
  gate          Record a passed gate for a space in a local rollout.json and
                switch on that phase's features for it.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_DIR / "assets" / "lab-colab.toml"
TEMPLATES = SKILL_DIR / "assets" / "templates"
TIERS = ("public", "lab", "restricted")
MARKETPLACE_TEMPLATES = TEMPLATES / "marketplace"
# Files `seed-marketplace` writes into the marketplace repo (published path -> template, None = generated).
MARKETPLACE_FILES = {
    ".claude-plugin/marketplace.json": None,
    "scripts/check_marketplace.py": "check_marketplace.py",
    ".github/workflows/validate.yml": "validate.yml",
    ".github/workflows/track-agent-skills.yml": "track-agent-skills.yml",
    "README.md": "README.md",
}


# --------------------------------------------------------------------------- config


def load_config(path: Path | None = None) -> dict:
    """Load the desired state; prefer a meta-repo copy when one is given."""
    with open(path or DEFAULT_CONFIG, "rb") as fh:
        return tomllib.load(fh)


def empty_rollout() -> dict:
    return {"schema": 1, "spaces": {}, "gates": []}


# --------------------------------------------------------------------------- observe


def _gh_json(args: list[str]) -> object | None:
    """Run gh and parse JSON; None when the object does not exist or gh fails."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return json.loads(out) if out else None


GQL_INFO = (
    "query($org: String!, $n: Int!) { organization(login: $org) { projectV2(number: $n) { id "
    "views(first: 100) { nodes { id name layout filter } } "
    "fields(first: 100) { nodes { ... on ProjectV2FieldCommon { id name dataType } "
    "... on ProjectV2SingleSelectField { options { name } } "
    "... on ProjectV2MultiSelectField { options: multiSelectOptions { name } } } } } } }")


def _project_info(org: str, number: int | str) -> dict | None:
    """One Project's id, views (with filters), and fields (with option names) through GraphQL; None on failure."""
    data = _gh_json(["api", "graphql", "-f", f"query={GQL_INFO}", "-f", f"org={org}", "-F", f"n={number}"])
    try:
        node = data["data"]["organization"]["projectV2"]
        return {"id": node["id"], "views": node["views"]["nodes"],
                "fields": [f for f in node["fields"]["nodes"] if f.get("name")]}
    except (TypeError, KeyError):
        return None


def observe_marketplace(config: dict, repo_names: set[str]) -> dict:
    """Marketplace repo state: Actions workflow permissions and which starter files exist. Read-only."""
    mp = config.get("marketplace") or {}
    repo = mp.get("repo") or ""
    if not repo or repo not in repo_names:
        return {}
    org = config["org"]["name"]
    perms = _gh_json(["api", f"repos/{org}/{repo}/actions/permissions/workflow"])
    tree = _gh_json(["api", f"repos/{org}/{repo}/git/trees/HEAD?recursive=1"])
    paths = [t["path"] for t in (tree or {}).get("tree", []) if isinstance(t, dict) and "path" in t]
    return {
        "workflow_permissions": (perms or {}).get("default_workflow_permissions"),
        "can_approve_pull_request_reviews": (perms or {}).get("can_approve_pull_request_reviews"),
        "files": sorted(p for p in paths if p in MARKETPLACE_FILES),
    }


def observe(config: dict) -> dict:
    """Collect what exists in the org. Read-only.

    Labels are not swept: none are created up front. Issue-template folders are
    read only for the forms repo and the space repos.
    """
    org, meta_repo = config["org"]["name"], config["org"]["meta_repo"]
    forms_repo = config["org"].get("forms_repo", ".github")
    repos = _gh_json(["repo", "list", org, "--limit", "1000", "--json", "name,visibility,isArchived"]) or []
    active = [r for r in repos if not r.get("isArchived")]
    types = _gh_json(["api", f"orgs/{org}/issue-types"]) or []
    projects = (_gh_json(["project", "list", "--owner", org, "--limit", "1000", "--format", "json"]) or {}).get("projects", [])
    snapshot: dict = {
        "org": org,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repos": [{"name": r["name"], "visibility": r["visibility"].lower()} for r in active],
        "meta_repo_exists": any(r["name"] == meta_repo for r in active),
        "issue_types": [t["name"] for t in types if isinstance(t, dict)],
        "projects": [{"number": p["number"], "title": p["title"]} for p in projects],
        "project_fields": {},
        "forms": {},
        "rollout": None,
    }
    snapshot["marketplace"] = observe_marketplace(config, {r["name"] for r in active})
    snapshot["project_views"] = {}
    for p in snapshot["projects"]:
        number = str(p["number"])
        info = _project_info(org, p["number"])
        if info is not None:
            snapshot["project_fields"][number] = [
                {"name": f["name"], "options": [o["name"] for o in f.get("options") or []]} for f in info["fields"]]
            snapshot["project_views"][number] = [
                {"name": v["name"], "layout": v.get("layout"), "filter": v.get("filter") or ""} for v in info["views"]]
            continue
        # GraphQL read failed: fall back to gh's field list; views stay unknown (the plan leaves them alone).
        fields = (_gh_json(["project", "field-list", number, "--owner", org, "--format", "json"]) or {}).get("fields", [])
        snapshot["project_fields"][number] = [
            {"name": f["name"], "options": [o["name"] for o in f.get("options", [])]} for f in fields]
    names = {r["name"] for r in snapshot["repos"]}
    for name in sorted(({forms_repo} | set(repo_spaces(config))) & names):
        forms = _gh_json(["api", f"repos/{org}/{name}/contents/.github/ISSUE_TEMPLATE"]) or []
        snapshot["forms"][name] = [f["name"] for f in forms if isinstance(f, dict)]
    if snapshot["meta_repo_exists"]:
        blob = _gh_json(["api", f"repos/{org}/{meta_repo}/contents/.colab/rollout.json"])
        if isinstance(blob, dict) and blob.get("content"):
            import base64

            snapshot["rollout"] = json.loads(base64.b64decode(blob["content"]))
    return snapshot


# --------------------------------------------------------------------------- plan


def space_repos(config: dict) -> dict[str, list[str]]:
    return {name: list(spec.get("repos", [])) for name, spec in (config.get("spaces") or {}).items()}


def repo_spaces(config: dict) -> dict[str, list[str]]:
    """repo -> the spaces it belongs to (membership is implied; no labels)."""
    out: dict[str, list[str]] = {}
    for name, spec in (config.get("spaces") or {}).items():
        for repo in spec.get("repos", []):
            out.setdefault(repo, []).append(name)
    return out


def form_filename(type_name: str) -> str:
    return f"colab-{type_name.lower()}.yml"


GQL_COLORS = ["GRAY", "BLUE", "GREEN", "YELLOW", "ORANGE", "RED", "PINK", "PURPLE"]


def _options_literal(options: list[str], described: bool = False) -> str:
    return ", ".join(
        f'{{name: {json.dumps(o)}, color: {GQL_COLORS[i % len(GQL_COLORS)]}, '
        f'description: {json.dumps(f"Space {o}" if described else "")}}}' for i, o in enumerate(options))


def _options_mutation(options: list[str], multi: bool = False) -> str:
    """GraphQL mutation that replaces a select field's options (single or multi)."""
    if multi:
        return ("mutation($id: ID!) { updateProjectV2Field(input: {fieldId: $id, multiSelectOptions: ["
                + _options_literal(options, True) + "]}) { projectV2Field { ... on ProjectV2FieldCommon { name } } } }")
    return ("mutation($id: ID!) { updateProjectV2Field(input: {fieldId: $id, singleSelectOptions: ["
            + _options_literal(options) + "]}) "
            "{ projectV2Field { ... on ProjectV2SingleSelectField { name } } } }")


def _create_multi_mutation(name: str, options: list[str]) -> str:
    return ("mutation($project: ID!) { createProjectV2Field(input: {projectId: $project, dataType: MULTI_SELECT, "
            f"name: {json.dumps(name)}, multiSelectOptions: [" + _options_literal(options, True) + "]}) "
            "{ projectV2Field { ... on ProjectV2FieldCommon { id name } } } }")


ITERATION_WEEKS = 2
ITERATION_COUNT = 4


def next_monday(today: date) -> date:
    """The first Monday strictly after today."""
    return today + timedelta(days=7 - today.weekday())


def _create_iteration_mutation(name: str, today: date) -> str:
    start = next_monday(today)
    days = 7 * ITERATION_WEEKS
    items = ", ".join(
        f'{{title: "Cycle {i + 1}", startDate: "{(start + timedelta(days=days * i)).isoformat()}", duration: {days}}}'
        for i in range(ITERATION_COUNT))
    return ("mutation($project: ID!) { createProjectV2Field(input: {projectId: $project, dataType: ITERATION, "
            f"name: {json.dumps(name)}, iterationConfiguration: {{startDate: \"{start.isoformat()}\", duration: {days}, "
            f"iterations: [{items}]}}}}) {{ projectV2Field {{ ... on ProjectV2FieldCommon {{ id name }} }} }} }}")


def space_filter(name: str) -> str:
    """Project view filter that shows one space (matches the multi-select Spaces field)."""
    return f'spaces:"{name}"' if re.search(r"\s", name) else f"spaces:{name}"


def desired_views(config: dict) -> list[dict]:
    """The views the Project should have: one per space, plus the standing queues."""
    views = [{"name": f"Space: {n}", "layout": "TABLE_LAYOUT", "filter": space_filter(n)} for n in space_repos(config)]
    views += [
        {"name": "Agent queue", "layout": "TABLE_LAYOUT", "filter": "status:Ready agent-ready:yes"},
        {"name": "Proposals inbox", "layout": "TABLE_LAYOUT", "filter": "status:Proposed"},
        {"name": "My work", "layout": "BOARD_LAYOUT", "filter": "assignee:@me"},
    ]
    return views


def _field_options(fields: list) -> dict[str, list[str]]:
    """Accept field lists as names or {name, options} dicts."""
    out: dict[str, list[str]] = {}
    for f in fields:
        if isinstance(f, str):
            out[f] = []
        else:
            out[f["name"]] = list(f.get("options", []))
    return out


def is_recurring(action: dict) -> bool:
    """Plan actions that are expected to stay in every plan; the bus's drift check ignores them."""
    desc = action.get("desc", "")
    return (action["kind"] == "gate"
            or desc.startswith("Start the bus")
            or (action["kind"] == "manual" and desc.startswith("These space repos have their own issue templates")))


def _action(kind: str, phase: int, desc: str, cmd: list[str] | None = None, **extra) -> dict:
    return {"kind": kind, "phase": phase, "desc": desc, "cmd": cmd, **extra}


def build_plan(config: dict, snapshot: dict, today: date | None = None) -> list[dict]:
    """Ordered actions that move the org toward the config. Pure: no I/O (today only seeds the Iteration dates)."""
    today = today or date.today()
    org = config["org"]["name"]
    meta = config["org"]["meta_repo"]
    actions: list[dict] = []
    repos = {r["name"]: r for r in snapshot.get("repos", [])}

    # Phase 0: foundation, reaching every space with low-risk parts only.
    if not snapshot.get("meta_repo_exists"):
        actions.append(_action("gh", 0, f"Create the private meta repo {org}/{meta}",
                               ["gh", "repo", "create", f"{org}/{meta}", "--private",
                                "--description", "Lab Colab Space: config, ledgers, event snapshots, knowledge staging"]))
        actions.append(_action("local", 0, "Seed the meta repo from a local clone, then open a PR",
                               ["python", "colab.py", "seed", "--dest", f"<clone of {org}/{meta}>"]))
    elif snapshot.get("rollout") is None:
        actions.append(_action("local", 0, "Meta repo has no .colab/rollout.json; seed it",
                               ["python", "colab.py", "seed", "--dest", f"<clone of {org}/{meta}>"]))

    have_types = set(snapshot.get("issue_types", []))
    for t in config.get("types", []):
        if t["name"] not in have_types:
            actions.append(_action("gh", 0, f"Create org issue type {t['name']}",
                                   ["gh", "api", "-X", "POST", f"orgs/{org}/issue-types",
                                    "-f", f"name={t['name']}", "-f", f"description={t['description']}",
                                    "-f", f"color={t['color']}", "-F", "is_enabled=true"]))

    title = config["org"]["project_title"]
    wanted_number = config["org"].get("project_number", 0)
    project = next((p for p in snapshot.get("projects", []) if wanted_number and p["number"] == wanted_number), None) \
        or next((p for p in snapshot.get("projects", []) if p["title"] == title), None)
    if project is None:
        actions.append(_action("gh", 0, f"Create the org Project '{title}'",
                               ["gh", "project", "create", "--owner", org, "--title", title]))
        have_fields: dict[str, list[str]] = {}
        number = "<new project number>"
    else:
        number = str(project["number"])
        have_fields = _field_options(snapshot.get("project_fields", {}).get(number, []))
    spaces = space_repos(config)
    pid = f"<project id of {number}>"
    for f in config.get("fields", []):
        multi = f["type"] == "MULTI_SELECT"
        want = list(spaces) if multi else list(f.get("options", []))  # Spaces options come from [spaces]
        if f["name"] in have_fields:
            missing = [o for o in want if o not in have_fields[f["name"]]]
            if missing and multi:
                actions.append(_action("gh", 0,
                                       f"Set the options of Project field {f['name']} to: {', '.join(want)}. "
                                       "GitHub may reset existing values on items; that is fine, "
                                       "because the bus recomputes them on every sync",
                                       ["gh", "api", "graphql", "-f", f"query={_options_mutation(want, multi=True)}",
                                        "-f", f"id=<field id of {f['name']} on project {number}>"]))
            elif missing:
                # e.g. every Project has a built-in Status. GraphQL can replace its options;
                # this resets values on existing items, so it is safe only on a new board.
                actions.append(_action("gh", 0,
                                       f"Set the options of Project field {f['name']} to: {', '.join(f['options'])} "
                                       "(resets existing values; run on a new board only)",
                                       ["gh", "api", "graphql", "-f", f"query={_options_mutation(f['options'])}",
                                        "-f", f"id=<field id of {f['name']} on project {number}>"]))
            continue
        if f["type"] == "ITERATION":
            days = 7 * ITERATION_WEEKS
            actions.append(_action("gh", 0,
                                   f"Create the '{f['name']}' iteration field: {ITERATION_WEEKS}-week cycles from "
                                   f"{next_monday(today).isoformat()}, {ITERATION_COUNT} iterations ({days} days each)",
                                   ["gh", "api", "graphql", "-f", f"query={_create_iteration_mutation(f['name'], today)}",
                                    "-f", f"project={pid}"]))
            continue
        if multi:
            if want:
                actions.append(_action("gh", 0, f"Create Project field {f['name']} (multi-select) with options: {', '.join(want)}",
                                       ["gh", "api", "graphql", "-f", f"query={_create_multi_mutation(f['name'], want)}",
                                        "-f", f"project={pid}"]))
            continue  # no spaces yet: the 'Define spaces' step below comes first
        cmd = ["gh", "project", "field-create", number, "--owner", org, "--name", f["name"], "--data-type", f["type"]]
        if f.get("options"):
            cmd += ["--single-select-options", ",".join(f["options"])]
        actions.append(_action("gh", 0, f"Create Project field {f['name']}", cmd))

    # Views: created empty, then given their filter (GitHub has no filter on create).
    views_known = number in snapshot.get("project_views", {}) or project is None
    have_views = {v["name"]: v for v in snapshot.get("project_views", {}).get(number, [])}
    if not views_known:
        actions.append(_action("manual", 0, f"Could not read the views of Project {number}; re-run status before planning views"))
    for v in desired_views(config) if views_known else []:
        have = have_views.get(v["name"])
        set_filter = ["gh", "api", "graphql", "-f",
                      "query=mutation($view: ID!) { updateProjectV2View(input: {viewId: $view, filter: "
                      + json.dumps(v["filter"]) + "}) { projectV2View { id filter } } }",
                      "-f", f"view=<view id of {v['name']} on project {number}>"]
        if have is None:
            actions.append(_action("gh", 0, f"Create Project view '{v['name']}' ({v['layout']})",
                                   ["gh", "api", "graphql", "-f",
                                    "query=mutation($project: ID!) { createProjectV2View(input: {projectId: $project, name: "
                                    + json.dumps(v["name"]) + f", layout: {v['layout']}" + "}) { projectV2View { id } } }",
                                    "-f", f"project={pid}"]))
            actions.append(_action("gh", 0, f"Set the filter of view '{v['name']}' to: {v['filter']}", set_filter))
        elif " ".join(have.get("filter", "").split()) != v["filter"]:
            actions.append(_action("gh", 0, f"Fix the filter of view '{v['name']}': '{have.get('filter', '')}' -> '{v['filter']}'", set_filter))

    if not spaces:
        candidates = sorted(n for n in repos if n != meta)
        actions.append(_action("manual", 0,
                               "Define spaces: edit [spaces] in .colab/config.toml (name, stewards, repos, optional tier). "
                               f"Candidate repos: {', '.join(candidates[:40]) or 'none found'}"))

    # No labels up front: tier comes from repo visibility and the space tier setting,
    # and space membership from the repo lists. Labels appear only on first use.
    for repo in sorted(repo_spaces(config)):
        if repo not in repos:
            actions.append(_action("manual", 0, f"Space repo {repo} not found in {org}; fix [spaces]"))

    # Issue forms once, org-wide, in the forms repo (GitHub's default templates).
    forms_repo = config["org"].get("forms_repo", ".github")
    if forms_repo not in repos:
        actions.append(_action("manual", 0, f"Create {org}/{forms_repo} (public) to hold org-wide default issue forms"))
    else:
        have = set(snapshot.get("forms", {}).get(forms_repo, []))
        missing = [t["name"] for t in config.get("types", []) if form_filename(t["name"]) not in have]
        if missing:
            actions.append(_action("local", 0, f"Open a PR adding {len(missing)} org-wide issue forms to {forms_repo}",
                                   ["python", "colab.py", "render-forms", "--dest", f"<clone of {org}/{forms_repo}>/.github/ISSUE_TEMPLATE"]))
    own = sorted(r for r in repo_spaces(config) if r != forms_repo and snapshot.get("forms", {}).get(r))
    if own:
        actions.append(_action("manual", 0,
                               "These space repos have their own issue templates, so the org-wide forms do not show there: "
                               f"{', '.join(own)}. Add colab forms to one only if its maintainers want them."))

    if snapshot.get("meta_repo_exists") and not config.get("sessions", {}).get("issue"):
        actions.append(_action("gh", 0, f"Create the 'Session activity' issue in {org}/{meta} for session heartbeats",
                               ["gh", "issue", "create", "--repo", f"{org}/{meta}", "--title", "Session activity",
                                "--body", "Each agent session keeps one comment here, refreshed by `colab.py beat`. "
                                          "The bus reads them to tell active, waiting, and stale sessions apart."]))
        actions.append(_action("manual", 0, "Put that issue's number in .colab/config.toml as [sessions] issue = N (by PR)"))

    rollout = snapshot.get("rollout") or empty_rollout()
    bus_seen = any(s.get("bus") for s in rollout.get("spaces", {}).values())
    if spaces and not bus_seen:
        actions.append(_action("local", 0, "Start the bus in dry-run on ORCD (self-requeuing Slurm job; it logs and never writes)",
                               ["sbatch", "<rendered colab-bus.sbatch>"]))

    actions += marketplace_actions(config, snapshot, repos)

    # Phases 1+: per-space switches, opened only after that space passes the previous gate.
    phases = {p["id"]: p for p in config.get("phases", [])}
    for space in spaces:
        reached = rollout.get("spaces", {}).get(space, {}).get("phase", -1)
        target = reached + 1
        if target in phases:
            opens = ", ".join(phases.get(target + 1, {}).get("switches", [])) or "nothing new"
            actions.append(_action("gate", target,
                                   f"Space {space}: phase {target} gate. When this holds, record it (opens: {opens}). "
                                   f"Gate: {phases[target]['gate']}",
                                   ["python", "colab.py", "gate", "--rollout", "<meta clone>/.colab/rollout.json",
                                    "--space", space, "--phase", str(target), "--evidence", "<link to the evidence>"]))
    return actions


def marketplace_actions(config: dict, snapshot: dict, repos: dict) -> list[dict]:
    """Plan steps for the optional private plugin marketplace repo ([marketplace] repo = "" turns it off)."""
    mp = config.get("marketplace") or {}
    repo = mp.get("repo") or ""
    if not repo:
        return []
    org = config["org"]["name"]
    have = snapshot.get("marketplace") or {}
    out: list[dict] = []
    exists = repo in repos
    if not exists:
        out.append(_action("gh", 0, f"Create the private marketplace repo {org}/{repo}",
                           ["gh", "repo", "create", f"{org}/{repo}", "--private",
                            "--description", "Private Claude plugin marketplace: one plugin per skill"]))
    elif repos[repo].get("visibility") != "private":
        out.append(_action("manual", 0, f"{org}/{repo} is {repos[repo].get('visibility')}; the marketplace repo must be private "
                                        "(claude.ai org sync reads it with the Claude GitHub App). Make it private in the repo settings"))
    if not exists or have.get("workflow_permissions") != "write" or have.get("can_approve_pull_request_reviews") is not True:
        out.append(_action("gh", 0, f"Set Actions workflow permissions on {org}/{repo}: read and write, and allow Actions to approve PRs "
                                    "(the tracking workflow opens and merges version-bump PRs)",
                           ["gh", "api", "-X", "PUT", f"repos/{org}/{repo}/actions/permissions/workflow",
                            "-f", "default_workflow_permissions=write", "-F", "can_approve_pull_request_reviews=true"]))
    missing = [p for p in MARKETPLACE_FILES if p not in set(have.get("files", []))]
    if missing:
        out.append(_action("local", 0, f"Seed the marketplace repo ({len(missing)} of {len(MARKETPLACE_FILES)} starter files missing), then open a PR",
                           ["python", "colab.py", "seed-marketplace", "--dest", f"<clone of {org}/{repo}>"]))
        out.append(_action("manual", 0, f"claude.ai step 1 (an org owner): install the Claude GitHub App on {org}/{repo}"))
        out.append(_action("manual", 0, "claude.ai step 2 (an org owner): Organization settings > Plugins & skills > Marketplaces > "
                                        f"Add plugins > Sync from GitHub, then pick {org}/{repo}"))
    return out


# --------------------------------------------------------------------------- apply


FIELD_ID = re.compile(r"<field id of (?P<name>.+?) on project (?P<number>\d+)>")
VIEW_ID = re.compile(r"<view id of (?P<name>.+?) on project (?P<number>\d+)>")
PROJECT_ID = re.compile(r"<project id of (?P<number>\d+)>")


def _resolve_field_id(part: str, org: str | None = None) -> str:
    """Replace a `<field|view|project id of ...>` placeholder with the real node id, looked up now."""
    for pattern, kind in ((FIELD_ID, "field"), (VIEW_ID, "view"), (PROJECT_ID, "project")):
        m = pattern.search(part)
        if not m:
            continue
        owner = org or load_config()["org"]["name"]
        if kind == "project":
            data = _gh_json(["project", "view", m["number"], "--owner", owner, "--format", "json"]) or {}
            found = data.get("id")
            label = f"project {m['number']}"
        else:
            info = _project_info(owner, m["number"])
            if info is not None:
                items = info["fields" if kind == "field" else "views"]
            else:  # fields can still come from gh; views cannot
                items = (_gh_json(["project", "field-list", m["number"], "--owner", owner, "--format", "json"]) or {}).get("fields", []) \
                    if kind == "field" else []
            found = next((i["id"] for i in items if i["name"] == m["name"]), None)
            label = f"{kind} {m['name']!r} on project {m['number']}"
        if found is None:
            raise SystemExit(f"{label} not found")
        return part[: m.start()] + found + part[m.end():]
    return part


def apply(actions: list[dict], yes: bool, throttle: float = 1.0, org: str | None = None) -> int:
    import time

    runnable = [a for a in actions if a["kind"] == "gh"]
    for a in actions:
        if a["kind"] != "gh":
            print(f"[{a['kind']}] {a['desc']}" + (f"\n    {' '.join(a['cmd'])}" if a.get("cmd") else ""))
    if not runnable:
        print("No gh actions to run.")
        return 0
    if not yes:
        print(f"\n{len(runnable)} gh actions would run. Re-run with --yes after review.")
        for a in runnable:
            print(f"  - {a['desc']}")
        return 0
    failures = 0
    for a in runnable:
        if any("<new project number>" in part for part in a["cmd"]):
            print(f"skip (re-plan after the Project exists): {a['desc']}")
            continue
        a = {**a, "cmd": [_resolve_field_id(part, org) for part in a["cmd"]]}
        proc = subprocess.run(a["cmd"], capture_output=True, text=True)
        status = "ok" if proc.returncode == 0 else f"FAILED: {proc.stderr.strip()[:200]}"
        failures += proc.returncode != 0
        print(f"{status:>6}  {a['desc']}")
        time.sleep(throttle)
    return 1 if failures else 0


# --------------------------------------------------------------------------- seed + forms


def render_form(type_spec: dict) -> str:
    """One GitHub issue form (YAML) for a work type. Hand-written YAML, no PyYAML."""
    def q(s: str) -> str:
        return json.dumps(s)  # JSON strings are valid YAML scalars

    lines = [
        f"name: {q(type_spec['name'])}",
        f"description: {q(type_spec['description'])}",
        f"type: {q(type_spec['name'])}",
        "body:",
        "  - type: markdown",
        "    attributes:",
        "      value: " + q("Never paste restricted content; link to it. Add typed links in a fenced links block, one per line, e.g. uses: sensein/senselab#12."),
    ]
    for field in type_spec.get("fields", []):
        lines += [
            f"  - type: {field['kind']}",
            f"    id: {field['id']}",
            "    attributes:",
            f"      label: {q(field['label'])}",
            "    validations:",
            f"      required: {'true' if field.get('required') else 'false'}",
        ]
    lines += [
        "  - type: textarea",
        "    id: links",
        "    attributes:",
        "      label: \"Links\"",
        "      description: \"One per line: <edge>: <owner>/<repo>#<n> or <edge>: kg:<id>\"",
        "      render: links",
        "    validations:",
        "      required: false",
    ]
    return "\n".join(lines) + "\n"


def cmd_render_forms(config: dict, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for t in config.get("types", []):
        path = dest / form_filename(t["name"])
        path.write_text(render_form(t))
        written.append(path)
    return written


def cmd_seed(dest: Path, config_path: Path | None) -> list[Path]:
    """Starter files for the meta repo checkout. Never overwrites existing files.

    The sbatch goes in as a template; the filled copy (real paths) lives in the
    bus state directory, outside git, so the meta repo keeps scanning clean.
    """
    colab = dest / ".colab"
    colab.mkdir(parents=True, exist_ok=True)
    plan = {
        colab / "config.toml": (config_path or DEFAULT_CONFIG).read_text(),
        colab / "rollout.json": json.dumps(empty_rollout(), indent=2) + "\n",
        colab / "subscriptions.toml": (TEMPLATES / "subscriptions.toml").read_text(),
        dest / ".github" / "workflows" / "colab-watchdog.yml": (TEMPLATES / "colab-watchdog.yml").read_text(),
        dest / "bus" / "colab-bus.sbatch.template": (TEMPLATES / "colab-bus.sbatch").read_text(),
        dest / ".gitignore": "bus/*.local.sbatch\n",
    }
    written = []
    for path, text in plan.items():
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)
    scripts_dir = dest / "bus"
    for name in ("colab_bus.py", "colab.py"):
        target = scripts_dir / name
        if not target.exists():
            shutil.copy2(Path(__file__).with_name(name), target)
            written.append(target)
    return written


# --------------------------------------------------------------------------- marketplace

DESC_LIMIT = 300


def skill_description(skill_md: str) -> str:
    """The `description` from a SKILL.md frontmatter, folded to one line and cut to 300 characters."""
    m = re.match(r"---\s*\n(.*?)\n---", skill_md, re.S)
    lines = m.group(1).splitlines() if m else []
    text = ""
    for i, line in enumerate(lines):
        if not line.startswith("description:"):
            continue
        value = line.split(":", 1)[1].strip()
        if value[:1] in (">", "|"):  # block scalar: the indented lines that follow
            block = []
            for nxt in lines[i + 1:]:
                if nxt and not nxt[0].isspace():
                    break
                block.append(nxt.strip())
            text = " ".join(b for b in block if b)
        else:
            text = value.strip("\"'")
        break
    text = " ".join(text.split())
    return text if len(text) <= DESC_LIMIT else text[: DESC_LIMIT - 3].rstrip() + "..."


class GhSource:
    """Reads the source repo's skills and main commit through `gh` (read-only)."""

    def __init__(self, org: str, repo: str):
        self.org, self.repo = org, repo

    def sha(self) -> str:
        out = _gh_json(["api", f"repos/{self.org}/{self.repo}/commits/main"])
        if not isinstance(out, dict) or not out.get("sha"):
            raise SystemExit(f"could not read main of {self.org}/{self.repo} with gh")
        return out["sha"]

    def skills(self) -> list[str]:
        items = _gh_json(["api", f"repos/{self.org}/{self.repo}/contents/skills"]) or []
        return sorted(i["name"] for i in items if isinstance(i, dict) and i.get("type") == "dir")

    def skill_md(self, name: str) -> str | None:
        blob = _gh_json(["api", f"repos/{self.org}/{self.repo}/contents/skills/{name}/SKILL.md"])
        if isinstance(blob, dict) and blob.get("content"):
            return base64.b64decode(blob["content"]).decode()
        return None


def marketplace_json(config: dict, source, today: date | None = None) -> dict:
    """One git-subdir plugin per skill folder in the source repo, pinned to its current main commit."""
    org, mp = config["org"]["name"], config.get("marketplace") or {}
    src_repo = mp.get("source_repo") or "agent_skills"
    sha = source.sha()
    version = f"{(today or datetime.now(timezone.utc).date()).strftime('%Y.%m.%d')}+{sha[:7]}"
    skip = set(mp.get("exclude") or [])
    plugins = []
    for name in source.skills():
        md = source.skill_md(name)
        if name in skip or md is None:
            continue
        plugins.append({
            "name": name,
            "description": skill_description(md),
            "version": version,
            "source": {"source": "git-subdir", "url": f"https://github.com/{org}/{src_repo}.git",
                       "path": f"skills/{name}", "sha": sha},
            "strict": False,  # no plugin.json: the entry is the manifest
        })
    return {"name": mp.get("name") or org,
            "description": f"Skills from {org}/{src_repo}, one plugin per skill, tracked hourly.",
            "owner": {"name": org, "url": f"https://github.com/{org}"},
            "plugins": plugins}


def render_marketplace_template(text: str, config: dict) -> str:
    mp = config.get("marketplace") or {}
    org = config["org"]["name"]
    for key, value in (("__ORG__", org), ("__SOURCE_REPO__", mp.get("source_repo") or "agent_skills"),
                       ("__NAME__", mp.get("name") or org), ("__REPO__", mp.get("repo") or "claude-plugins")):
        text = text.replace(key, value)
    return text


def cmd_seed_marketplace(config: dict, dest: Path, source=None, today: date | None = None) -> list[Path]:
    """Starter files for the marketplace repo checkout. Never overwrites existing files."""
    mp = config.get("marketplace") or {}
    if not mp.get("repo"):
        raise SystemExit("[marketplace] repo is empty in the config; nothing to seed")
    org = config["org"]["name"]
    plan: dict[str, str] = {}
    for rel, template in MARKETPLACE_FILES.items():
        if not (dest / rel).exists():
            if template is None:
                src = source or GhSource(org, mp.get("source_repo") or "agent_skills")
                plan[rel] = json.dumps(marketplace_json(config, src, today), indent=2) + "\n"
            else:
                plan[rel] = render_marketplace_template((MARKETPLACE_TEMPLATES / template).read_text(), config)
    written = []
    for rel, text in plan.items():
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if rel.endswith(".py"):
            path.chmod(0o755)
        written.append(path)
    return written


# --------------------------------------------------------------------------- member setup

HOOK_EVENTS = ("PostToolUse", "Stop")
NEEDED_SCOPES = ("repo", "read:org", "project")
BEAT = "colab.py beat"


def _find_python(runner=None, which=shutil.which) -> str | None:
    """A Python 3.11+ interpreter: this one if it qualifies, else the first on PATH that does."""
    runner = runner or subprocess.run
    if sys.version_info >= (3, 11) and sys.executable:
        return sys.executable
    for name in ("python3.14", "python3.13", "python3.12", "python3.11", "python3"):
        exe = which(name)
        if not exe:
            continue
        try:
            out = runner([exe, "-c", "import sys; print(int(sys.version_info >= (3, 11)))"],
                         capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if out == "1":
            return exe
    return None


def hook_command(config_path: Path | str, python: str, skill_dir: Path = SKILL_DIR) -> str:
    return f"COLAB_CONFIG={config_path} {python} {skill_dir}/scripts/colab.py beat --from-hook --background 2>/dev/null || true"


def _hook_commands(settings: dict, event: str) -> list[str]:
    return [h.get("command", "") for entry in (settings.get("hooks") or {}).get(event, []) if isinstance(entry, dict)
            for h in entry.get("hooks", []) if isinstance(h, dict)]


def merge_hooks(settings: dict, command: str) -> list[str]:
    """Add the beat hook to PostToolUse and Stop in place; returns the events changed. Idempotent.

    Any existing colab beat hook is left alone, so a re-run adds nothing and hand edits survive.
    """
    changed = []
    hooks = settings.setdefault("hooks", {})
    for event in HOOK_EVENTS:
        if any(BEAT in c for c in _hook_commands(settings, event)):
            continue
        hooks.setdefault(event, []).append({"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": 30}]})
        changed.append(event)
    return changed


def _scopes(auth_text: str) -> list[str] | None:
    m = re.search(r"Token scopes:\s*(.+)", auth_text)
    return re.findall(r"[\w:.-]+", m.group(1).replace("'", " ")) if m else None


def cmd_setup(config_path: Path | None, install_hooks: bool, home: Path | None = None, env=None,
              runner=None, which=shutil.which, skill_dir: Path = SKILL_DIR) -> tuple[list[dict], int]:
    """Member onboarding check. Returns (checks, exit code); each check has name, status, detail, fix."""
    env = os.environ if env is None else env
    runner = runner or subprocess.run
    home = home or Path.home()
    settings_path = home / ".claude" / "settings.json"
    checks: list[dict] = []

    def add(name: str, status: str, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    # gh auth and scopes
    if not which("gh"):
        add("gh", "fail", "gh is not on PATH", "install the GitHub CLI: https://cli.github.com, then `gh auth login`")
    else:
        proc = runner(["gh", "auth", "status"], capture_output=True, text=True)
        text = (proc.stdout or "") + (proc.stderr or "")
        scopes = _scopes(text)
        if proc.returncode != 0:
            add("gh auth", "fail", "gh is not logged in", "gh auth login --scopes repo,read:org,project")
        elif scopes is None:
            add("gh auth", "ok", "logged in; token scopes are not shown (fine-grained or app token), so they are not checked")
        else:
            missing = [x for x in NEEDED_SCOPES if x not in scopes]
            if missing:
                add("gh scopes", "fail", f"missing scopes: {', '.join(missing)}",
                    "gh auth refresh " + " ".join(f"-s {x}" for x in missing))
            else:
                add("gh scopes", "ok", "repo, read:org and project are present")
            add("gh admin:org", "info" if "admin:org" not in scopes else "ok",
                "present" if "admin:org" in scopes else "not present; only org owners need it (to create issue types)",
                "" if "admin:org" in scopes else "org owners only: gh auth refresh -s admin:org")

    # Python 3.11+
    python = _find_python(runner, which)
    if python:
        add("python", "ok", f"Python 3.11+ at {python}")
    else:
        add("python", "fail", "no Python 3.11+ found on PATH", "install one, e.g. `uv python install 3.12`, and put it on PATH")

    # settings.json (read once; an unreadable file blocks hook changes, never anything else)
    settings, settings_err = {}, None
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text() or "{}")
            if not isinstance(settings, dict):
                raise ValueError("top level is not an object")
        except (OSError, ValueError) as err:
            settings, settings_err = {}, str(err)
            add("settings.json", "fail", f"{settings_path} is not valid JSON ({err}); hooks left untouched",
                "fix the file by hand, then re-run setup")

    # COLAB_CONFIG
    cfg_value = str(config_path) if config_path else env.get("COLAB_CONFIG") or (settings.get("env") or {}).get("COLAB_CONFIG")
    cfg_source = "--config" if config_path else "environment" if env.get("COLAB_CONFIG") else "settings.json env"
    cfg_fix = "gh repo clone <org>/<meta repo>, then `export COLAB_CONFIG=<meta clone>/.colab/config.toml` (or pass --config)"
    cfg_abs = None
    if not cfg_value:
        add("COLAB_CONFIG", "fail", "not set", cfg_fix)
    else:
        path = Path(cfg_value).expanduser()
        try:
            load_config(path)
            cfg_abs = path.resolve()
            add("COLAB_CONFIG", "ok", f"{cfg_abs} (from {cfg_source})")
        except (OSError, tomllib.TOMLDecodeError) as err:
            add("COLAB_CONFIG", "fail", f"{cfg_value} does not load ({err.__class__.__name__})", cfg_fix)

    # where the skill is installed
    sd = str(skill_dir)
    kind = ("a user skills directory" if "/.claude/skills/" in sd or "/.agents/skills/" in sd
            else "a plugin install" if "/.claude/plugins/" in sd else "a repository checkout")
    add("skill location", "info", f"{sd} ({kind})")

    # heartbeat hooks
    present = {e: any(BEAT in c for c in _hook_commands(settings, e)) for e in HOOK_EVENTS}
    if install_hooks:
        if settings_err or cfg_abs is None or python is None:
            add("hooks", "fail", "not installed: " + ("settings.json is unreadable" if settings_err else
                "COLAB_CONFIG does not resolve" if cfg_abs is None else "no Python 3.11+"),
                "fix the item above, then re-run `colab.py setup --install-hooks`")
        else:
            changed = merge_hooks(settings, hook_command(cfg_abs, python, skill_dir))
            if changed:
                settings_path.parent.mkdir(parents=True, exist_ok=True)
                backup = None
                if settings_path.exists():
                    backup = settings_path.with_name(f"settings.json.colab-bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
                    shutil.copy2(settings_path, backup)
                tmp = settings_path.with_name("settings.json.colab-tmp")
                tmp.write_text(json.dumps(settings, indent=2) + "\n")
                tmp.replace(settings_path)
                add("hooks", "ok", f"added heartbeat hooks for {', '.join(changed)}" + (f"; backup at {backup}" if backup else ""))
            else:
                add("hooks", "ok", "heartbeat hooks already present; nothing changed")
    elif all(present.values()):
        add("hooks", "ok", f"heartbeat hooks present in {settings_path}")
    else:
        add("hooks", "fail", f"no heartbeat hook for {', '.join(e for e, v in present.items() if not v)} in {settings_path}",
            "python3.11 <skill>/scripts/colab.py setup --install-hooks")
    return checks, 1 if any(c["status"] == "fail" for c in checks) else 0


def print_setup(checks: list[dict]) -> None:
    tag = {"ok": "ok  ", "fail": "FAIL", "info": "info"}
    for c in checks:
        print(f"[{tag[c['status']]}] {c['name']}: {c['detail']}")
        if c["fix"] and c["status"] != "ok":
            print(f"        fix: {c['fix']}")


# --------------------------------------------------------------------------- scan

SELFTEST = {
    "secret": "token = gh" + "p_" + "a" * 36,
    "path": "data lives at /" + "orcd/scratch/orcd/999/someone/run1",
}


def scan_text(text: str, patterns: dict[str, list[str]]) -> dict[str, int]:
    counts = {cat: 0 for cat in patterns}
    for cat, regexes in patterns.items():
        for rx in regexes:
            counts[cat] += len(re.findall(rx, text))
    return counts


def cmd_scan(config: dict, paths: list[str], selftest: bool) -> int:
    patterns = config.get("scan", {}).get("patterns", {})
    if selftest:
        missed = [cat for cat, sample in SELFTEST.items() if scan_text(sample, patterns).get(cat, 0) == 0]
        print(json.dumps({"selftest": "fail" if missed else "pass", "missed": missed}))
        return 1 if missed else 0
    external = config.get("scan", {}).get("command")
    if external:
        proc = subprocess.run([*external.split(), *paths], capture_output=True, text=True)
        print(proc.stdout.strip())
        return proc.returncode
    total = {cat: 0 for cat in patterns}
    sources = paths or ["-"]
    for p in sources:
        text = sys.stdin.read() if p == "-" else Path(p).read_text(errors="replace")
        for cat, n in scan_text(text, patterns).items():
            total[cat] += n
    print(json.dumps(total))
    return 1 if any(total.values()) else 0


# --------------------------------------------------------------------------- links

LINKS_BLOCK = re.compile(r"```links\s*\n(.*?)```", re.S)
LINK_LINE = re.compile(r"^\s*(?P<edge>[a-z][a-z-]*)\s*:\s*(?P<target>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+|kg:[A-Za-z0-9_.:/-]+)\s*$")


def parse_links(body: str) -> tuple[list[dict], list[str]]:
    """Return (edges, errors) from every fenced ```links block in an issue body."""
    edges: list[dict] = []
    errors: list[str] = []
    for block in LINKS_BLOCK.findall(body or ""):
        for n, line in enumerate(block.splitlines(), 1):
            if not line.strip() or line.strip().startswith("#"):
                continue
            m = LINK_LINE.match(line)
            if not m:
                errors.append(f"line {n}: cannot parse {line.strip()!r}")
                continue
            edges.append({"edge": m["edge"], "target": m["target"]})
    return edges, errors


def validate_links(edges: list[dict], config: dict, source_type: str | None = None) -> list[str]:
    allowed = set(config["edges"]["body"])
    kg_only = set(config["edges"].get("kg_only", []))
    domain = config["edges"].get("domain", {})
    errors = []
    for e in edges:
        if e["edge"] in kg_only:
            errors.append(f"{e['edge']} is set in the knowledge merge step, not in issue bodies")
        elif e["edge"] in config["edges"]["native"]:
            errors.append(f"{e['edge']} is GitHub-native: use a sub-issue or blocked-by link instead")
        elif e["edge"] not in allowed:
            errors.append(f"unknown edge '{e['edge']}'")
        elif source_type and e["edge"] in domain and source_type not in domain[e["edge"]]:
            errors.append(f"{source_type} cannot '{e['edge']}' (allowed: {', '.join(domain[e['edge']])})")
    return errors


# --------------------------------------------------------------------------- leases

LEASE = re.compile(r"<!-- colab-lease user=(?P<user>[A-Za-z0-9-]+) until=(?P<until>[0-9T:+-]+Z?) -->")


def compose_lease(user: str, hours: float = 4, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    until = (now + timedelta(hours=hours)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return f"<!-- colab-lease user={user} until={until} -->"


def live_lease(comments: list[str], now: datetime | None = None) -> dict | None:
    """The newest unexpired lease among issue comments, if any."""
    now = now or datetime.now(timezone.utc)
    latest = None
    for text in comments:
        for m in LEASE.finditer(text or ""):
            until = datetime.fromisoformat(m["until"].replace("Z", "+00:00"))
            if until > now and (latest is None or until > latest["until"]):
                latest = {"user": m["user"], "until": until}
    return latest


# --------------------------------------------------------------------------- session heartbeats
#
# A session that runs out of tokens just stops: no handoff, no goodbye. So the
# heartbeat comes from activity, not a timer. Claude Code hooks call `beat` after
# tool use; no tool use means no beat. Beats are throttled locally (no network
# most of the time) and cost no model tokens. Each session keeps one comment in
# the meta repo's "Session activity" issue, edited in place, and a beat also
# renews the session's lease on its claimed item. A session waiting on a long
# Slurm job names it with --job; its lease then runs to the job's scheduled end.

SESSION = re.compile(r"<!-- colab-session id=(?P<id>[A-Za-z0-9._-]+) user=(?P<user>[A-Za-z0-9-]+) "
                     r"beat=(?P<beat>[0-9T:+-]+Z)(?: job=(?P<job>\d+))?(?: item=(?P<item>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+))? -->")
ITEM = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>\d+)$")


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def compose_session(session: str, user: str, beat: datetime, job: str | None = None, item: str | None = None) -> str:
    sid = re.sub(r"[^A-Za-z0-9._-]", "-", session)[:64]
    marker = f"<!-- colab-session id={sid} user={user} beat={_iso(beat)}"
    if job:
        marker += f" job={job}"
    if item:
        marker += f" item={item}"
    return marker + " -->"


def session_comment(session: str, user: str, beat: datetime, job: str | None, item: str | None) -> str:
    lines = [f"Session `{session[:12]}` of @{user}: last activity {_iso(beat)}."]
    if item:
        lines.append(f"Working on {item}.")
    if job:
        lines.append(f"Waiting on Slurm job {job}.")
    return "\n".join(lines) + "\n\n" + compose_session(session, user, beat, job, item)


def parse_sessions(comments: list[dict]) -> list[dict]:
    """Session markers from the Session activity issue's comments."""
    out = []
    for c in comments:
        m = SESSION.search(c.get("body") or "")
        if m:
            out.append({"comment_id": c.get("id"), "session": m["id"], "user": m["user"], "job": m["job"],
                        "item": m["item"], "beat": datetime.fromisoformat(m["beat"].replace("Z", "+00:00"))})
    return out


def session_state(s: dict, now: datetime, stale_minutes: int, running_jobs: set[str]) -> str:
    """active, waiting (its Slurm job still runs), or stale."""
    if now - s["beat"] <= timedelta(minutes=stale_minutes):
        return "active"
    if s.get("job") and s["job"] in running_jobs:
        return "waiting"
    return "stale"


def should_push(last: dict | None, now: datetime, min_interval: int, job: str | None, item: str | None) -> bool:
    """Write to GitHub only when the interval passed or what the session is doing changed."""
    if not last:
        return True
    if last.get("job") != job or last.get("item") != item:
        return True
    return (now - datetime.fromisoformat(last["at"])).total_seconds() >= min_interval


def slurm_end(job: str) -> datetime | None:
    """Scheduled end of a Slurm job, if this host can see it."""
    try:
        out = subprocess.run(["squeue", "-h", "-j", job, "-o", "%e"], capture_output=True, text=True, timeout=20).stdout.strip()
        return datetime.fromisoformat(out).astimezone(timezone.utc) if out and out[0].isdigit() else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def claimed_item(cwd: str | None) -> str | None:
    """The item this worktree has claimed: `<git dir>/colab-claim` holds `owner/repo#n`."""
    try:
        git_dir = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--absolute-git-dir"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        text = Path(git_dir, "colab-claim").read_text().strip() if git_dir else ""
    except (OSError, subprocess.TimeoutExpired):
        return None
    return text if ITEM.match(text) else None


def _gh_send(method: str, path: str, body: dict) -> dict | None:
    proc = subprocess.run(["gh", "api", "-X", method, path, "--input", "-"], input=json.dumps(body),
                          capture_output=True, text=True)
    return json.loads(proc.stdout) if proc.returncode == 0 and proc.stdout.strip() else None


def _own_comment(path: str, marker: re.Pattern, match) -> dict | None:
    for page in range(1, 11):
        batch = _gh_json(["api", f"{path}/comments?per_page=100&page={page}"]) or []
        for c in batch:
            m = marker.search(c.get("body") or "")
            if m and match(m):
                return c
        if len(batch) < 100:
            return None
    return None


def cmd_beat(config: dict, session: str | None, item: str | None, job: str | None,
             from_hook: bool, background: bool, now: datetime | None = None) -> int:
    cwd = None
    if from_hook:
        try:
            hook = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            hook = {}
        session = session or hook.get("session_id")
        cwd = hook.get("cwd")
    if not session:
        print("beat needs --session or hook input with session_id", file=sys.stderr)
        return 2
    item = item or claimed_item(cwd)
    now = now or datetime.now(timezone.utc)
    sess_cfg = config.get("sessions", {})
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "colab" / "beats"
    cache.mkdir(parents=True, exist_ok=True)
    state_file = cache / f"{re.sub(r'[^A-Za-z0-9._-]', '-', session)[:64]}.json"
    last = json.loads(state_file.read_text()) if state_file.exists() else None
    if not should_push(last, now, int(sess_cfg.get("beat_min_seconds", 600)), job, item):
        return 0  # throttled: no network
    if background and os.fork() > 0:
        return 0  # parent returns at once so the hook adds no latency
    org, meta = config["org"]["name"], config["org"]["meta_repo"]
    user = (last or {}).get("user") or (_gh_json(["api", "user"]) or {}).get("login", "")
    issue = sess_cfg.get("issue")
    if issue and user:
        path = f"repos/{org}/{meta}/issues/{issue}"
        body = session_comment(session, user, now, job, item)
        mine = _own_comment(path, SESSION, lambda m: m["id"] == re.sub(r"[^A-Za-z0-9._-]", "-", session)[:64])
        if mine:
            _gh_send("PATCH", f"repos/{org}/{meta}/issues/comments/{mine['id']}", {"body": body})
        else:
            _gh_send("POST", f"{path}/comments", {"body": body})
    m = ITEM.match(item or "")
    if m and user:
        # renew this member's lease on the claimed item; never create one (claim does that)
        path = f"repos/{m['repo']}/issues/{m['number']}"
        lease = _own_comment(path, LEASE, lambda x: x["user"] == user)
        if lease:
            minutes = float(sess_cfg.get("lease_minutes", 45))
            until = now + timedelta(minutes=minutes)
            end = slurm_end(job) if job else None
            if end and end + timedelta(minutes=15) > until:
                until = end + timedelta(minutes=15)
            text = LEASE.sub(f"<!-- colab-lease user={user} until={_iso(until)} -->", lease["body"])
            _gh_send("PATCH", f"repos/{m['repo']}/issues/comments/{lease['id']}", {"body": text})
    state_file.write_text(json.dumps({"at": now.isoformat(), "user": user, "job": job, "item": item}))
    return 0


# --------------------------------------------------------------------------- next


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def field_value(item: dict, name: str):
    """Read a Project field from a `gh project item-list` item, whatever its key casing."""
    want = _norm(name)
    for key, value in item.items():
        if _norm(key) == want:
            return value
    return None


def candidates(items: list[dict]) -> list[dict]:
    """Issues with Status = Ready and Agent-ready = yes. Visibility is GitHub's own check:
    the list only holds items the caller can see."""
    out = []
    for it in items:
        content = it.get("content") or {}
        if content.get("type") != "Issue":
            continue
        if str(field_value(it, "Status") or "") != "Ready" or str(field_value(it, "Agent-ready") or "").lower() != "yes":
            continue
        out.append({"repo": content.get("repository", ""), "number": content.get("number"),
                    "title": content.get("title", ""), "url": content.get("url", "")})
    return out


def pick_next(cands: list[dict], comments: dict[str, list[str]], blocked: dict[str, bool],
              now: datetime | None = None, limit: int = 10) -> list[dict]:
    """Drop items with a live lease or an open blocker."""
    out = []
    for c in cands:
        if blocked.get(c["url"]) or live_lease(comments.get(c["url"], []), now):
            continue
        out.append(c)
        if len(out) >= limit:
            break
    return out


def cmd_next(config: dict, items_path: Path | None, limit: int) -> int:
    org = config["org"]["name"]
    title = config["org"]["project_title"]
    if items_path:
        items = json.loads(items_path.read_text()).get("items", [])
    else:
        projects = (_gh_json(["project", "list", "--owner", org, "--limit", "1000", "--format", "json"]) or {}).get("projects", [])
        project = next((p for p in projects if p["title"] == title), None)
        if project is None:
            print(f"No Project titled {title!r}; run the bootstrap plan first.", file=sys.stderr)
            return 1
        items = (_gh_json(["project", "item-list", str(project["number"]), "--owner", org,
                           "--limit", "1000", "--format", "json"]) or {}).get("items", [])
    cands = candidates(items)
    comments: dict[str, list[str]] = {}
    blocked: dict[str, bool] = {}
    for c in cands[: limit * 3]:
        path = f"repos/{c['repo']}/issues/{c['number']}"
        comments[c["url"]] = [x.get("body", "") for x in (_gh_json(["api", f"{path}/comments?per_page=100"]) or [])]
        blockers = _gh_json(["api", f"{path}/dependencies/blocked_by"]) or []
        blocked[c["url"]] = any(b.get("state") == "open" for b in blockers if isinstance(b, dict))
    print(json.dumps(pick_next(cands, comments, blocked, limit=limit), indent=2))
    return 0


# --------------------------------------------------------------------------- gate


def record_gate(rollout: dict, config: dict, space: str, phase: int, evidence: str,
                today: str | None = None) -> dict:
    """Mark `phase` passed for `space` and switch on the next phase's features."""
    phases = {p["id"]: p for p in config.get("phases", [])}
    if phase not in phases:
        raise ValueError(f"unknown phase {phase}")
    state = rollout.setdefault("spaces", {}).setdefault(space, {"phase": -1, "switches": {}})
    if phase > state.get("phase", -1) + 1:
        raise ValueError(f"{space} is at phase {state.get('phase', -1)}; pass phase {state.get('phase', -1) + 1} first")
    state["phase"] = max(state.get("phase", -1), phase)
    if phase == 0:
        state["bus"] = "dry-run"
    nxt = phases.get(phase + 1)
    for sw in (nxt or {}).get("switches", []):
        state.setdefault("switches", {})[sw] = True
        if sw == "bus_live":
            state["bus"] = "live"
    rollout.setdefault("gates", []).append({
        "space": space, "phase": phase, "date": today or datetime.now(timezone.utc).date().isoformat(),
        "evidence": evidence,
    })
    return rollout


# --------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="config TOML (default: meta-repo copy if given, else bundled)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("plan", "apply"):
        p = sub.add_parser(name)
        p.add_argument("--snapshot", type=Path, help="use a saved status JSON instead of observing")
        if name == "apply":
            p.add_argument("--yes", action="store_true")
    p = sub.add_parser("seed"); p.add_argument("--dest", type=Path, required=True)
    p = sub.add_parser("render-forms"); p.add_argument("--dest", type=Path, required=True)
    p = sub.add_parser("seed-marketplace"); p.add_argument("--dest", type=Path, required=True)
    p = sub.add_parser("setup"); p.add_argument("--install-hooks", action="store_true")
    p = sub.add_parser("next"); p.add_argument("--items", type=Path, help="saved `gh project item-list` JSON")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("scan"); p.add_argument("paths", nargs="*"); p.add_argument("--selftest", action="store_true")
    p = sub.add_parser("links"); p.add_argument("--body", type=Path, help="file with the issue body (default stdin)")
    p.add_argument("--type", dest="source_type")
    p = sub.add_parser("lease"); p.add_argument("--user", required=True); p.add_argument("--hours", type=float)
    p = sub.add_parser("beat"); p.add_argument("--session"); p.add_argument("--item", help="owner/repo#n")
    p.add_argument("--job", help="Slurm job the session is waiting on"); p.add_argument("--from-hook", action="store_true")
    p.add_argument("--background", action="store_true", help="return at once; write in a forked child")
    p = sub.add_parser("gate")
    p.add_argument("--rollout", type=Path, required=True); p.add_argument("--space", required=True)
    p.add_argument("--phase", type=int, required=True); p.add_argument("--evidence", required=True)
    args = ap.parse_args(argv)
    env_cfg = os.environ.get("COLAB_CONFIG")
    if args.cmd == "setup":  # must work before a config resolves: that is one of the things it checks
        checks, rc = cmd_setup(args.config, args.install_hooks)
        print_setup(checks)
        return rc
    config = load_config(args.config or (Path(env_cfg) if env_cfg else None))
    org, meta = config["org"]["name"], config["org"]["meta_repo"]

    if args.cmd == "status":
        print(json.dumps(observe(config), indent=2))
        return 0
    if args.cmd in ("plan", "apply"):
        snapshot = json.loads(args.snapshot.read_text()) if args.snapshot else observe(config)
        actions = build_plan(config, snapshot)
        if args.cmd == "plan":
            print(json.dumps(actions, indent=2))
            return 0
        return apply(actions, args.yes, config.get("bus", {}).get("write_interval_seconds", 1.0), org)
    if args.cmd == "seed":
        for path in cmd_seed(args.dest, args.config):
            print(path)
        return 0
    if args.cmd == "seed-marketplace":
        for path in cmd_seed_marketplace(config, args.dest):
            print(path)
        return 0
    if args.cmd == "render-forms":
        for path in cmd_render_forms(config, args.dest):
            print(path)
        return 0
    if args.cmd == "scan":
        return cmd_scan(config, args.paths, args.selftest)
    if args.cmd == "links":
        body = args.body.read_text() if args.body else sys.stdin.read()
        edges, errors = parse_links(body)
        errors += validate_links(edges, config, args.source_type)
        print(json.dumps({"edges": edges, "errors": errors}, indent=2))
        return 1 if errors else 0
    if args.cmd == "next":
        return cmd_next(config, args.items, args.limit)
    if args.cmd == "lease":
        hours = args.hours if args.hours is not None else float(config.get("sessions", {}).get("lease_minutes", 45)) / 60
        print(compose_lease(args.user, hours))
        return 0
    if args.cmd == "beat":
        try:
            return cmd_beat(config, args.session, args.item, args.job, args.from_hook, args.background)
        except Exception as err:  # a heartbeat must never break the session that calls it
            print(f"beat skipped: {err!r}"[:300], file=sys.stderr)
            return 0
        return 0
    if args.cmd == "gate":
        rollout = json.loads(args.rollout.read_text()) if args.rollout.exists() else empty_rollout()
        record_gate(rollout, config, args.space, args.phase, args.evidence)
        args.rollout.write_text(json.dumps(rollout, indent=2) + "\n")
        print(json.dumps(rollout["spaces"][args.space], indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
