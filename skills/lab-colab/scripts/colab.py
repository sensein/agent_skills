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
  lease         Compose or read a claim lease marker for an issue comment.
  gate          Record a passed gate for a space in a local rollout.json and
                switch on that phase's features for it.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = SKILL_DIR / "assets" / "lab-colab.toml"
TEMPLATES = SKILL_DIR / "assets" / "templates"
TIERS = ("public", "lab", "restricted")


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
    for p in snapshot["projects"]:
        fields = (_gh_json(["project", "field-list", str(p["number"]), "--owner", org, "--format", "json"]) or {}).get("fields", [])
        snapshot["project_fields"][str(p["number"])] = [
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


def _options_mutation(options: list[str]) -> str:
    """GraphQL mutation that replaces a single-select field's options."""
    opts = ", ".join(f'{{name: {json.dumps(o)}, color: {GQL_COLORS[i % len(GQL_COLORS)]}, description: ""}}'
                     for i, o in enumerate(options))
    return ("mutation($id: ID!) { updateProjectV2Field(input: {fieldId: $id, singleSelectOptions: [" + opts + "]}) "
            "{ projectV2Field { ... on ProjectV2SingleSelectField { name } } } }")


def _field_options(fields: list) -> dict[str, list[str]]:
    """Accept field lists as names or {name, options} dicts."""
    out: dict[str, list[str]] = {}
    for f in fields:
        if isinstance(f, str):
            out[f] = []
        else:
            out[f["name"]] = list(f.get("options", []))
    return out


def _action(kind: str, phase: int, desc: str, cmd: list[str] | None = None, **extra) -> dict:
    return {"kind": kind, "phase": phase, "desc": desc, "cmd": cmd, **extra}


def build_plan(config: dict, snapshot: dict) -> list[dict]:
    """Ordered actions that move the org toward the config. Pure: no I/O."""
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
    project = next((p for p in snapshot.get("projects", []) if p["title"] == title), None)
    if project is None:
        actions.append(_action("gh", 0, f"Create the org Project '{title}'",
                               ["gh", "project", "create", "--owner", org, "--title", title]))
        have_fields: dict[str, list[str]] = {}
        number = "<new project number>"
    else:
        number = str(project["number"])
        have_fields = _field_options(snapshot.get("project_fields", {}).get(number, []))
    for f in config.get("fields", []):
        if f["name"] in have_fields:
            missing = [o for o in f.get("options", []) if o not in have_fields[f["name"]]]
            if missing:
                # e.g. every Project has a built-in Status. GraphQL can replace its options;
                # this resets values on existing items, so it is safe only on a new board.
                actions.append(_action("gh", 0,
                                       f"Set the options of Project field {f['name']} to: {', '.join(f['options'])} "
                                       "(resets existing values; run on a new board only)",
                                       ["gh", "api", "graphql", "-f", f"query={_options_mutation(f['options'])}",
                                        "-f", f"id=<field id of {f['name']} on project {number}>"]))
            continue
        if f["type"] == "ITERATION":
            actions.append(_action("manual", 0, f"Add the '{f['name']}' iteration field in the Project web UI (gh cannot create it)"))
            continue
        cmd = ["gh", "project", "field-create", number, "--owner", org, "--name", f["name"], "--data-type", f["type"]]
        if f.get("options"):
            cmd += ["--single-select-options", ",".join(f["options"])]
        actions.append(_action("gh", 0, f"Create Project field {f['name']}", cmd))

    spaces = space_repos(config)
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

    rollout = snapshot.get("rollout") or empty_rollout()
    bus_seen = any(s.get("bus") for s in rollout.get("spaces", {}).values())
    if spaces and not bus_seen:
        actions.append(_action("local", 0, "Start the bus in dry-run on ORCD (self-requeuing Slurm job; it logs and never writes)",
                               ["sbatch", "<rendered colab-bus.sbatch>"]))

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


# --------------------------------------------------------------------------- apply


FIELD_ID = re.compile(r"<field id of (?P<name>.+?) on project (?P<number>\d+)>")


def _resolve_field_id(part: str, org: str | None = None) -> str:
    """Replace a `<field id of NAME on project N>` placeholder with the real node id."""
    m = FIELD_ID.search(part)
    if not m:
        return part
    owner = org or load_config()["org"]["name"]
    fields = (_gh_json(["project", "field-list", m["number"], "--owner", owner, "--format", "json"]) or {}).get("fields", [])
    fid = next((f["id"] for f in fields if f["name"] == m["name"]), None)
    if fid is None:
        raise SystemExit(f"field {m['name']!r} not found on project {m['number']}")
    return part[: m.start()] + fid + part[m.end():]


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
    p = sub.add_parser("next"); p.add_argument("--items", type=Path, help="saved `gh project item-list` JSON")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("scan"); p.add_argument("paths", nargs="*"); p.add_argument("--selftest", action="store_true")
    p = sub.add_parser("links"); p.add_argument("--body", type=Path, help="file with the issue body (default stdin)")
    p.add_argument("--type", dest="source_type")
    p = sub.add_parser("lease"); p.add_argument("--user", required=True); p.add_argument("--hours", type=float, default=4)
    p = sub.add_parser("gate")
    p.add_argument("--rollout", type=Path, required=True); p.add_argument("--space", required=True)
    p.add_argument("--phase", type=int, required=True); p.add_argument("--evidence", required=True)
    args = ap.parse_args(argv)
    config = load_config(args.config)
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
        print(compose_lease(args.user, args.hours))
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
