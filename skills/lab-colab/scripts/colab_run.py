#!/usr/bin/env python3
"""colab-run: run a claimed Experiment issue in an isolated workspace, under explicit budgets.

Stdlib only, Python 3.11+. Also reachable as `colab.py run <action> ...`.

Actions
  start    Read the Experiment issue (or --body-file), work out the tier, resolve budgets and the metric, make an
           isolated workspace (git worktree, or a copied run directory), write the run spec, start the monitored
           slice. It prints the verify and iterate commands with their sha256 and who last edited the issue body.
           Nothing has run yet.
  confirm  Confirm those exact commands: `confirm --run-dir D --sha256 H`. (`step` and `loop` take --confirm-verify H.)
  step     One iteration: monitor check, optional iteration command, verify command, then log the result:
           results.tsv row, an immutable Observation JSON, log.md, provenance.jsonl, and a progress comment when due.
  loop     Repeat `step` until a stop condition (monitor break, iterations, compute, or --max-steps).
  check    Run the loop breaker (monitor_slice.py check) with the run's stored thresholds.
  finish   Close the slice and set the final status.
  stage    Copy the run's Observations into <meta clone>/.colab/staging/observations/ (scanned with `colab.py scan`'s
           logic); --open-pr makes the branch from origin/main, commit, push and PR in the meta repo. It never merges.
  cleanup  Remove a run's workspace: `git worktree remove` and `prune`, delete the colab-run/<id> branch (--yes).
  status   Print the run spec, the confirmation state and the observation count.

Run directory (default ${XDG_STATE_HOME:-~/.local/state}/lab-colab/runs/<owner>-<repo>-<n>-<timestamp>)
  run.json  metadata.json  provenance.jsonl  results.tsv  log.md  observations/  logs/  workspace/
All local run logs are append-only (single O_APPEND writes under a token-owned directory lock); observations are written
once, exclusively, and made read-only. Iteration numbers are allocated under the lock, so parallel steps never collide.

Commands come from issue text, so they never run unconfirmed: step and loop refuse until the sha256 of the verify and
iterate commands has been confirmed, and refuse again if the issue's command changes.

Restricted data: the tier is the bus's rule (repo visibility, the spaces' declared tier, tier:* labels, the Project's
Sensitivity), failing closed when the issue cannot be read. A restricted run never executes in a session, and its
source tree is not copied or read there. `start` writes job.sbatch; only the batch job it creates (job name
colab-run-<id>, BatchFlag=1) can step or loop it.

Exit codes: 0 ok, 2 usage or refusal, 4 the run must stop (monitor break, iterations, compute).
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import random
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
MONITOR = HERE / "monitor_slice.py"
STOP_CODE = 4
KILL_GRACE_SECONDS = 3.0
PROV_CONTEXT = {"prov": "http://www.w3.org/ns/prov#", "colab": "urn:colab:"}
SCHEMA = "colab-observation/1"
RESULTS_HEADER = ["iteration", "timestamp_utc", "status", "metric_value", "slice_elapsed_seconds",
                  "overall_elapsed_seconds", "commit", "notes"]
UNIT_SECONDS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60,
                "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
                "d": 86400, "day": 86400, "days": 86400}
BUDGET_KEYS = ("iterations", "wall", "loop", "compute", "metric", "direction")
RESTRICTED_WORD = re.compile(r"\brestricted\b", re.I)
ISSUE_REF = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<n>\d+)$")


class RunError(Exception):
    """A refusal or a bad input; main() prints it and exits 2."""


# --------------------------------------------------------------------------- small helpers


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_now(text: str | None) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc) if text else utcnow()


def parse_duration_seconds(value: str) -> int:
    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty duration")
    if text.isdigit():
        return int(text)
    pairs = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", text)
    if not pairs or re.sub(r"[\s,]+", "", text) != "".join(f"{a}{u}" for a, u in pairs):
        raise ValueError(f"unrecognized duration: {value}")
    total = 0.0
    for amount, unit in pairs:
        if unit not in UNIT_SECONDS:
            raise ValueError(f"unsupported duration unit: {unit}")
        total += float(amount) * UNIT_SECONDS[unit]
    return int(total)


def jdumps(obj, **kw) -> str:
    """All JSON this engine writes refuses NaN and Infinity."""
    return json.dumps(obj, allow_nan=False, **kw)


def finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON constant {name}")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
def append_line(path: Path, text: str) -> None:
    """Append-only: one O_APPEND write of whole lines, never a rewrite."""
    data = (text if text.endswith("\n") else text + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def tsv(value: object) -> str:
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def fs_now(directory: Path) -> float:
    """The filesystem's own clock: the mtime of a file just created there (NFS and the local clock can differ)."""
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".clock-")
        os.close(fd)
        try:
            return Path(tmp).stat().st_mtime
        finally:
            Path(tmp).unlink(missing_ok=True)
    except OSError:
        return time.time()


def _read_owner(lock: Path) -> dict:
    try:
        return json.loads((lock / "owner.json").read_text())
    except (OSError, ValueError):
        return {}


@contextmanager
def run_lock(run_dir: Path, timeout_s: float = 60.0, poll_s: float = 0.02, stale_s: float = 300.0):
    """Directory lock (mkdir is atomic) with an owner token. A lock whose owner died on this host, or that is older
    than stale_s by the filesystem's clock, is broken by renaming it to a grave. The grave's token must be the one
    we judged stale; if it is not (someone took the lock in between), the lock is renamed back. Release only
    removes a lock that still carries our token."""
    lock = run_dir / ".lock"
    token = secrets.token_hex(12)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lock.mkdir()
            (lock / "owner.json").write_text(jdumps({"pid": os.getpid(), "host": socket.gethostname(), "token": token}))
            break
        except FileExistsError:
            owner = _read_owner(lock)
            try:
                age = fs_now(run_dir) - lock.stat().st_mtime
            except OSError:
                continue  # released between the mkdir and the stat
            dead = bool(owner) and owner.get("host") == socket.gethostname() and not _pid_alive(int(owner.get("pid", 0) or 0))
            if dead or age > stale_s:
                grave = run_dir / f".lock.stale-{os.getpid()}-{random.randrange(10**9)}"
                try:
                    lock.rename(grave)
                except OSError:
                    continue
                if _read_owner(grave).get("token") == owner.get("token"):
                    shutil.rmtree(grave, ignore_errors=True)
                else:  # not the lock we judged stale: put it back
                    try:
                        grave.rename(lock)
                    except OSError:
                        shutil.rmtree(grave, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {lock}")
            time.sleep(poll_s)
    try:
        yield
    finally:
        if _read_owner(lock).get("token") == token:
            (lock / "owner.json").unlink(missing_ok=True)
            try:
                lock.rmdir()
            except OSError:
                pass
# --------------------------------------------------------------------------- issue form -> run spec


def parse_form(body: str) -> dict[str, str]:
    """GitHub issue-form bodies are `### Label` sections. Returns lower-cased label -> text."""
    sections: dict[str, str] = {}
    label, lines = None, []
    for line in (body or "").splitlines():
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            if label is not None:
                sections[label] = "\n".join(lines).strip()
            label, lines = m.group(1).strip().lower(), []
        elif label is not None:
            lines.append(line)
    if label is not None:
        sections[label] = "\n".join(lines).strip()
    return {k: ("" if v == "_No response_" else v) for k, v in sections.items()}


def section(form: dict[str, str], *starts: str) -> str:
    for key, value in form.items():
        if any(key.startswith(s) for s in starts):
            return value
    return ""


def budget_tokens(text: str) -> dict[str, str]:
    """`iterations=20, wall=2h; compute=4 gpu-hours` -> {'iterations': '20', 'wall': '2h', 'compute': '4'}."""
    found: dict[str, str] = {}
    for m in re.finditer(r"\b(iterations|iters|wall|loop|compute|metric|direction)\s*[=:]\s*([^,;\n]+)", text or "", re.I):
        key = "iterations" if m.group(1).lower() == "iters" else m.group(1).lower()
        found.setdefault(key, m.group(2).strip().strip("`"))
    return found


def spec_from_issue(body: str, labels: list[str]) -> dict:
    form = parse_form(body)
    tokens: dict[str, str] = {}
    for text in (section(form, "run budget", "budget"), section(form, "compute plan"), section(form, "metrics")):
        for k, v in budget_tokens(text).items():
            tokens.setdefault(k, v)
    verify = section(form, "verify command", "verify").strip().strip("`").strip()
    data = section(form, "data")
    return {
        "tokens": tokens,
        "verify": verify.splitlines()[0].strip().strip("`") if verify else "",
        "restricted": ("tier:restricted" in labels) or bool(RESTRICTED_WORD.search(data)),
        "hypothesis": section(form, "hypothesis"),
    }


def leading_number(text: str) -> float:
    m = re.match(r"\s*(\d+(?:\.\d+)?)", text or "")
    if not m:
        raise RunError(f"not a number: {text!r}")
    return float(m.group(1))


def resolve_budgets(tokens: dict[str, str], args: argparse.Namespace) -> dict:
    """CLI overrides beat the issue form. Iterations and wall time must be explicit; compute is optional."""
    pick = lambda cli, key: cli if cli not in (None, "") else tokens.get(key, "")  # noqa: E731
    iterations, wall = pick(args.iterations, "iterations"), pick(args.wall, "wall")
    if not str(iterations) or not wall:
        raise RunError("budgets must be explicit: set iterations and wall time in the issue's Budget field "
                       "(iterations=N, wall=2h) or pass --iterations and --wall")
    try:
        wall_s = parse_duration_seconds(wall)
        loop_s = parse_duration_seconds(pick(args.loop, "loop") or wall)
        n_iter = int(float(iterations))
        compute = pick(args.compute, "compute")
        compute_budget = leading_number(str(compute)) if str(compute) else 0.0
    except (ValueError, RunError) as err:
        raise RunError(f"bad budget: {err}")
    if n_iter < 1 or wall_s < 1 or loop_s < 1:
        raise RunError("budgets must be positive")
    return {"iterations": n_iter, "wall": str(wall), "wall_seconds": wall_s, "loop": str(pick(args.loop, "loop") or wall),
            "loop_seconds": min(loop_s, wall_s), "compute": compute_budget, "compute_text": str(compute or "")}


# --------------------------------------------------------------------------- issue + gh (the only network)

Q_ISSUE = ("query($o: String!, $r: String!, $n: Int!) { repository(owner: $o, name: $r) { visibility "
           "issue(number: $n) { title body author { login } labels(first: 100) { nodes { name } } "
           "userContentEdits(first: 1) { nodes { editor { login } } } } } }")
Q_SENSITIVITY = ("query($o: String!, $r: String!, $n: Int!) { repository(owner: $o, name: $r) { issue(number: $n) { "
                 "projectItems(first: 20) { nodes { project { number } fieldValueByName(name: \"Sensitivity\") { "
                 "... on ProjectV2ItemFieldSingleSelectValue { name } } } } } } }")


def _gh_graphql(query: str, ref: dict) -> dict:
    proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}", "-f", f"o={ref['owner']}", "-f", f"r={ref['repo']}",
                           "-F", f"n={ref['n']}"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RunError(f"gh graphql failed: {proc.stderr.strip()[:200]}")
    try:
        return json.loads(proc.stdout)["data"]["repository"]
    except (ValueError, KeyError, TypeError):
        raise RunError("gh graphql returned no repository data")


def fetch_issue(ref: dict) -> dict:
    """Body, labels, repo visibility, the author, and who last edited the body (the author if never edited)."""
    repo = _gh_graphql(Q_ISSUE, ref)
    issue = repo.get("issue") or {}
    if not issue:
        raise RunError(f"could not read {ref['owner']}/{ref['repo']}#{ref['n']}")
    edits = (issue.get("userContentEdits") or {}).get("nodes") or []
    author = (issue.get("author") or {}).get("login", "")
    editor = ((edits[0].get("editor") or {}).get("login") if edits else "") or author
    return {"body": issue.get("body") or "", "title": issue.get("title", ""),
            "labels": [x["name"] for x in (issue.get("labels") or {}).get("nodes", [])],
            "visibility": str(repo.get("visibility", "")).lower(), "author": author, "editor": editor}


def fetch_sensitivity(ref: dict, project_number: int) -> str | None:
    """The Project's Sensitivity value for the issue, when readable; None otherwise."""
    try:
        nodes = ((_gh_graphql(Q_SENSITIVITY, ref).get("issue") or {}).get("projectItems") or {}).get("nodes") or []
    except RunError:
        return None
    for node in nodes:
        if (not project_number or (node.get("project") or {}).get("number") == project_number):
            name = ((node.get("fieldValueByName") or {}).get("name") or "").lower()
            if name:
                return name
    return None


def gh_login() -> str:
    proc = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def post_issue_comment(ref: dict, body: str) -> bool:
    proc = subprocess.run(["gh", "api", "-X", "POST", f"repos/{ref['owner']}/{ref['repo']}/issues/{ref['n']}/comments",
                           "--input", "-"], input=jdumps({"body": body}), capture_output=True, text=True)
    return proc.returncode == 0


def load_colab(config_path: str | None):
    import colab  # noqa: PLC0415 (path set above)
    env = os.environ.get("COLAB_CONFIG")
    return colab, colab.load_config(Path(config_path) if config_path else (Path(env) if env else None))


def compute_tier(config: dict, ref: dict, labels: list[str], visibility: str | None, sensitivity: str | None = None) -> str:
    """The bus's rule: the strictest of repo visibility, the declared tier of the repo's spaces (membership plus
    space:* labels), a tier:* label, and the Project's Sensitivity. Unknown visibility fails closed."""
    import colab_bus as bus  # noqa: PLC0415
    if visibility not in ("public", "private", "internal"):
        return "restricted"
    declared = [bus.repo_tiers(config).get(bus.repo_key(f"{ref['owner']}/{ref['repo']}", config.get("org", {}).get("name")))]
    for name in {lab.split(":", 1)[1] for lab in labels if lab.startswith("space:")}:
        declared.append(((config.get("spaces") or {}).get(name) or {}).get("tier"))
    return bus._strictest(bus.tier_of(visibility, labels, None), sensitivity, *declared)


def scan_ok(colab_mod, config: dict, paths: list[Path]) -> bool:
    """Exactly `colab.py scan`: the external [scan].command when set, else the built-in patterns. Exit 0 = clean."""
    with contextlib.redirect_stdout(io.StringIO()):
        return colab_mod.cmd_scan(config, [str(p) for p in paths], False) == 0


# --------------------------------------------------------------------------- workspace


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def is_git(path: Path) -> bool:
    return path.is_dir() and _git(["rev-parse", "--is-inside-work-tree"], path).returncode == 0


def make_workspace(run_dir: Path, run_id: str, source: Path | None, mode: str, base: str) -> dict:
    """An isolated workspace nobody else writes to: a new git worktree on its own branch, or a copied directory."""
    workspace = run_dir / "workspace"
    if workspace.exists():
        raise RunError(f"workspace already exists: {workspace}")
    if mode == "auto":
        mode = "worktree" if source and is_git(source) else "dir"
    if mode == "worktree":
        if not source or not is_git(source):
            raise RunError("--mode worktree needs --source inside a git repository")
        branch = f"colab-run/{run_id}"
        proc = _git(["worktree", "add", "-b", branch, str(workspace), base or "HEAD"], source)
        if proc.returncode != 0:
            raise RunError(f"git worktree add failed: {proc.stderr.strip()[:300]}")
        return {"mode": "worktree", "path": "workspace", "branch": branch, "source": str(source)}
    if source:
        shutil.copytree(source, workspace, ignore=shutil.ignore_patterns(".git"), symlinks=True)
    else:
        workspace.mkdir()
    return {"mode": "dir", "path": "workspace", "branch": "", "source": str(source) if source else ""}


def ensure_workspace(run_dir: Path, spec: dict) -> dict:
    """Restricted runs defer the workspace into the job, so a session never copies or reads the source tree."""
    ws = spec["workspace"]
    if not ws.get("deferred"):
        return spec
    with run_lock(run_dir):
        spec = load_run(run_dir)
        ws = spec["workspace"]
        if ws.get("deferred"):
            source = Path(ws["source"]) if ws.get("source") else None
            spec["workspace"] = make_workspace(run_dir, spec["run_id"], source, ws.get("requested_mode", "auto"), ws.get("base", ""))
            atomic_write(run_dir / "run.json", jdumps(spec, indent=2) + "\n")
            prov_log(run_dir, "prepare-workspace", {"mode": spec["workspace"]["mode"]})
    return spec


def git_state(workspace: Path) -> dict:
    if not is_git(workspace):
        return {"commit": None, "dirty": None}
    head = _git(["rev-parse", "HEAD"], workspace)
    dirty = _git(["status", "--porcelain"], workspace)
    return {"commit": head.stdout.strip() if head.returncode == 0 else None, "dirty": bool(dirty.stdout.strip())}


# --------------------------------------------------------------------------- run directory


def default_root(config: dict, cli_root: str | None) -> Path:
    if cli_root:
        return Path(cli_root).expanduser()
    if os.environ.get("COLAB_RUN_ROOT"):
        return Path(os.environ["COLAB_RUN_ROOT"]).expanduser()
    if config.get("run", {}).get("root"):
        return Path(config["run"]["root"]).expanduser()
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "lab-colab" / "runs"


def load_run(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        raise RunError(f"not a run directory (no readable run.json): {run_dir}")


def monitor(run_dir: Path, action: str, extra: list[str], now: str | None = None) -> tuple[int, dict]:
    cmd = [sys.executable, str(MONITOR), action, "--experiment-dir", str(run_dir), *extra]
    if now:
        cmd += ["--now", now]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        state = json.loads(proc.stdout)
    except ValueError:
        raise RunError(f"monitor_slice {action} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.returncode, state


def monitor_flags(spec: dict) -> list[str]:
    m, out = spec.get("monitor", {}), []
    for key, flag in (("patience", "--patience"), ("max_failures", "--max-failures"), ("stall_seconds", "--stall-seconds"),
                      ("reserve_seconds", "--reserve-seconds"), ("usage_file", "--usage-file"),
                      ("governance_file", "--governance-file"), ("min_trust_score", "--min-trust-score"),
                      ("metric_guardrail", "--metric-guardrail"), ("max_rss_bytes", "--max-rss-bytes")):
        if m.get(key) not in (None, "", 0):
            out += [flag, str(m[key])]
    if m.get("break_on_drift"):
        out.append("--break-on-drift")
    return out


def log_md(run_dir: Path, text: str) -> None:
    append_line(run_dir / "log.md", f"- {iso(utcnow())} {text}")


def prov_log(run_dir: Path, action: str, detail: dict | None = None, now: datetime | None = None) -> None:
    """Every action of the engine is one PROV-style line in provenance.jsonl (best effort, append-only). The monitor
    reads only lines that carry a labnb:stateSnapshot, so these never disturb its state."""
    spec = {}
    try:
        spec = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        pass
    t = iso(now or utcnow())
    append_line(run_dir / "provenance.jsonl", jdumps({
        "@context": PROV_CONTEXT, "prov:type": ["prov:Activity", f"colab:Run{action.title().replace('-', '')}"],
        "prov:id": f"urn:colab:activity:{action}:{run_dir.name}:{t}", "prov:startedAtTime": t,
        "prov:wasAssociatedWith": f"urn:colab:agent:{spec.get('agent', 'unspecified')}",
        "prov:actedOnBehalfOf": spec.get("acting_for", ""), "colab:action": action, "colab:detail": detail or {}}, sort_keys=True))


def parse_params(pairs: list[str]) -> dict:
    out: dict = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise RunError(f"--param needs key=value, got {pair!r}")
        try:
            out[key] = json.loads(value, parse_constant=_reject_constant)
        except ValueError:
            out[key] = value
    return out


def command_hash(verify: str, iterate: str) -> str:
    return hashlib.sha256(jdumps({"verify": verify, "iterate": iterate}, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- start


def cmd_start(args, config: dict) -> int:
    m = ISSUE_REF.match(args.issue)
    if not m:
        raise RunError("issue must look like owner/repo#n")
    ref = {"owner": m["owner"], "repo": m["repo"], "n": int(m["n"])}
    notes: list[str] = []
    fetched: dict | None = None
    if not args.offline:
        try:
            fetched = fetch_issue(ref)
        except RunError as err:
            if not args.body_file:
                raise
            notes.append(f"could not read the issue's labels ({err}); treating the run as restricted")
    elif not args.body_file:
        raise RunError("--offline needs --body-file (the issue body)")
    body = Path(args.body_file).read_text() if args.body_file else fetched["body"]
    labels = list((fetched or {}).get("labels", [])) + list(args.label or [])
    visibility = (fetched or {}).get("visibility") if fetched else (args.visibility if args.offline else None)
    sensitivity = None
    if fetched:
        sensitivity = fetch_sensitivity(ref, int(config.get("org", {}).get("project_number") or 0))
    tier = compute_tier(config, ref, labels, visibility, sensitivity)
    form = spec_from_issue(body, labels)
    budgets = resolve_budgets(form["tokens"], args)
    verify = args.verify or form["verify"]
    if not verify:
        raise RunError("no verify command: add a Verify command field to the issue or pass --verify")
    iterate = args.iterate_cmd or ""
    direction = (args.direction or form["tokens"].get("direction", "")).strip().lower()
    if direction not in ("", "higher", "lower"):
        raise RunError("direction must be higher or lower")
    metric = args.metric or form["tokens"].get("metric", "")
    restricted = bool(args.restricted or form["restricted"] or tier == "restricted")
    claimant = args.claimant or ("" if args.offline else gh_login())
    editor = args.editor or (fetched or {}).get("editor") or ""
    if not claimant or not editor or editor.lower() != claimant.lower():
        notes.append(f"WARNING: the issue body was last edited by {editor or 'an unknown person'}, not the claimant "
                     f"{claimant or 'unknown'}. The verify command below came from that text. Read it before confirming.")
    root = default_root(config, args.root)
    root.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    base_id = f"{ref['owner']}-{ref['repo']}-{ref['n']}-{stamp}"
    run_id, suffix = base_id, 1
    while True:
        try:
            (root / run_id).mkdir()
            break
        except FileExistsError:
            suffix += 1
            run_id = f"{base_id}-{suffix}"
    run_dir = (root / run_id).resolve()
    run_cfg = config.get("run", {})
    requested_mode = args.mode or run_cfg.get("mode", "auto")
    source = Path(args.source).expanduser().resolve() if args.source else None
    if restricted:  # nothing reads or copies the source in this session; the job does it
        workspace = {"deferred": True, "mode": requested_mode, "requested_mode": requested_mode, "path": "workspace",
                     "branch": f"colab-run/{run_id}", "source": str(source) if source else "", "base": args.base}
    else:
        try:
            workspace = make_workspace(run_dir, run_id, source, requested_mode, args.base)
        except RunError:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
    (run_dir / "observations").mkdir()
    (run_dir / "logs").mkdir()
    created = iso(parse_now(args.now))
    comment_every = args.comment_every if args.comment_every is not None else int(run_cfg.get("comment_every", 5))
    comment_secs = args.comment_every_seconds if args.comment_every_seconds is not None else int(run_cfg.get("comment_every_seconds", 0))
    digest = command_hash(verify, iterate)
    python = args.python or run_cfg.get("python") or sys.executable
    spec = {
        "schema": "colab-run/1", "run_id": run_id, "created_at_utc": created,
        "issue": f"{ref['owner']}/{ref['repo']}#{ref['n']}", "issue_ref": ref, "title": (fetched or {}).get("title", ""),
        "hypothesis": form["hypothesis"], "restricted": restricted, "tier": "restricted" if restricted else tier,
        "offline": bool(args.offline), "budgets": budgets, "metric": {"name": metric, "direction": direction},
        "verify": verify, "iterate": iterate, "verify_sha256": digest, "confirmed_sha256": "",
        "verify_source": "cli" if args.verify else "issue", "issue_editor": editor, "acting_for": claimant,
        "agent": args.agent or os.environ.get("COLAB_AGENT") or ("claude-code" if os.environ.get("CLAUDECODE") else "unspecified"),
        "step_timeout": args.step_timeout, "python": python,
        "params": parse_params(args.param), "workspace": workspace,
        "container_digest": args.container_digest or os.environ.get("COLAB_CONTAINER_DIGEST", ""),
        "comments": {"every_iterations": max(0, comment_every), "every_seconds": max(0, comment_secs)},
        "monitor": {"patience": args.patience, "max_failures": args.max_failures, "stall_seconds": args.stall_seconds,
                    "reserve_seconds": args.reserve_seconds, "usage_file": args.usage_file or "",
                    "governance_file": args.governance_file or "", "min_trust_score": args.min_trust_score,
                    "metric_guardrail": args.metric_guardrail, "break_on_drift": args.break_on_drift},
    }
    atomic_write(run_dir / "run.json", jdumps(spec, indent=2) + "\n")
    atomic_write(run_dir / "metadata.json", jdumps({
        "entry_id": run_id, "entry_kind": "experiment", "status": "planned", "created_at_utc": created,
        "entry_dir": str(run_dir), "experiment_dir": str(run_dir), "objective": spec["title"] or spec["issue"],
        "metric_name": metric, "direction": direction, "verify_command": verify,
        "overall_budget": budgets["wall"], "overall_budget_seconds": budgets["wall_seconds"],
        "loop_budget": budgets["loop"], "loop_budget_seconds": budgets["loop_seconds"],
        "workspace_dir": str(run_dir / "workspace"), "issue": spec["issue"], "restricted": restricted,
    }, indent=2) + "\n")
    append_line(run_dir / "results.tsv", "\t".join(RESULTS_HEADER))
    append_line(run_dir / "log.md", "# Run log\n")
    atomic_write(run_dir / "counter", "0")
    code, state = monitor(run_dir, "start", [], args.now)
    prov_log(run_dir, "start", {"issue": spec["issue"], "tier": spec["tier"], "verify_sha256": digest,
                                "issue_editor": editor, "restricted": restricted})
    log_md(run_dir, f"started {spec['issue']} budgets iterations={budgets['iterations']} wall={budgets['wall']} "
                    f"compute={budgets['compute'] or 'none'}; workspace {workspace['mode']}; tier {spec['tier']}")
    out = {"run_dir": str(run_dir), "run_id": run_id, "workspace": str(run_dir / "workspace"), "mode": workspace["mode"],
           "restricted": restricted, "tier": spec["tier"], "budgets": budgets, "monitor": state.get("status"),
           "commands": {"verify": verify, "iterate": iterate, "sha256": digest},
           "confirm": f"read the commands above, then pass --confirm-verify {digest} to step or loop (nothing has run)"}
    if notes:
        out["warnings"] = notes
        for note in notes:
            print(f"colab run: {note}", file=sys.stderr)
    if restricted:
        out["job_script"] = str(write_job_script(run_dir, spec, args, digest))
        out["next"] = ("restricted: nothing runs in this session. Review job.sbatch (submitting it confirms the commands), "
                       f"then submit it after the owner agrees: sbatch {out['job_script']}")
    print(jdumps(out, indent=2))
    return 0


def write_job_script(run_dir: Path, spec: dict, args, digest: str) -> Path:
    partition = args.partition or os.environ.get("COLAB_RUN_PARTITION", "")
    py = shlex.quote(spec["python"])
    lines = ["#!/bin/bash", f"#SBATCH --job-name=colab-run-{spec['run_id']}",
             f"#SBATCH --time={max(1, spec['budgets']['wall_seconds'] // 60 + 5)}",
             f"#SBATCH --output={run_dir}/logs/job-%j.out"]
    if partition:
        lines.append(f"#SBATCH --partition={partition}")
    lines += ["set -euo pipefail",
              "# Restricted data: this batch job, not a Claude session, reads it. Only aggregates and run-relative pointers leave the run dir.",
              f"{py} -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || "
              f"{{ echo 'colab run needs Python 3.11 or newer: {spec['python']}' >&2; exit 1; }}",
              f"{py} {shlex.quote(str(HERE / 'colab_run.py'))} loop --run-dir {shlex.quote(str(run_dir))} --in-job "
              f"--confirm-verify {digest}"]
    path = run_dir / "job.sbatch"
    atomic_write(path, "\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------- guards


def is_batch_job(run_id: str, env=None, runner=subprocess.run) -> bool:
    """True only inside the batch job this run's job.sbatch created: the job's name is colab-run-<id> and
    `scontrol show job` says BatchFlag=1 (an salloc or srun session has BatchFlag=0)."""
    env = os.environ if env is None else env
    job = env.get("SLURM_JOB_ID") or ""
    if not job or env.get("SLURM_JOB_NAME") != f"colab-run-{run_id}":
        return False
    try:
        proc = runner(["scontrol", "show", "job", job], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and re.search(r"\bBatchFlag=1\b", proc.stdout or "") is not None


def guard_restricted(spec: dict, in_job: bool) -> None:
    if spec["restricted"] and not (in_job and is_batch_job(spec["run_id"])):
        raise RunError("this run touches restricted data: it must run inside the batch job made from its job.sbatch "
                       "(job name colab-run-<id>, BatchFlag=1), never in a session or an interactive allocation")


def confirm_commands(run_dir: Path, spec: dict, confirm: str | None, config: dict | None = None) -> dict:
    """Refuse unless the exact verify and iterate commands were confirmed by their sha256. The confirmed hash is stored
    in run.json; a later change to the command (including a changed issue) refuses again."""
    digest = command_hash(spec["verify"], spec["iterate"])
    if digest != spec["verify_sha256"]:
        raise RunError("run.json's commands no longer match the hash recorded at start; start a new run")
    if confirm and confirm != digest:
        raise RunError(f"--confirm-verify does not match the current commands (sha256 {digest})")
    if confirm and spec.get("confirmed_sha256") != digest:
        with run_lock(run_dir):
            spec = load_run(run_dir)
            spec["confirmed_sha256"] = digest
            atomic_write(run_dir / "run.json", jdumps(spec, indent=2) + "\n")
            prov_log(run_dir, "confirm-verify", {"verify_sha256": digest})
    if spec.get("confirmed_sha256") != digest:
        raise RunError("the verify/iterate commands have not been confirmed.\n"
                       f"  verify:  {spec['verify']}\n  iterate: {spec['iterate'] or '(none)'}\n  sha256:  {digest}\n"
                       f"  issue body last edited by: {spec.get('issue_editor') or 'unknown'}; acting for: {spec.get('acting_for') or 'unknown'}\n"
                       f"Read them, then re-run with --confirm-verify {digest}")
    if spec.get("verify_source") == "issue" and not spec["offline"] and not spec["restricted"]:
        try:
            now_issue = fetch_issue(spec["issue_ref"])
        except RunError as err:
            raise RunError(f"could not re-read the issue to check its commands are unchanged: {err}")
        current = spec_from_issue(now_issue["body"], now_issue["labels"])["verify"]
        if current != spec["verify"]:
            raise RunError("the issue's verify command changed since it was confirmed; start a new run and confirm the new one")
    return spec


# --------------------------------------------------------------------------- step


def stop_reason(run_dir: Path, spec: dict, rows_done: int, used: float) -> tuple[str, str] | None:
    b = spec["budgets"]
    if rows_done >= b["iterations"]:
        return "budget", f"iterations budget reached ({b['iterations']})"
    if b["compute"] and used >= b["compute"]:
        return "budget", f"compute budget reached ({used:g} of {b['compute']:g})"
    return None


def compute_value(raw):
    """A compute figure from verify: a finite number >= 0, or text starting with one ('0.5 gpu-hours').
    Raises RunError otherwise."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = leading_number(raw)
    if not finite_number(raw) or raw < 0:
        raise RunError(f"invalid compute value: {raw!r}")
    return float(raw)


def compute_used(run_dir: Path) -> float:
    total = 0.0
    for path in (run_dir / "observations").glob("*.json"):
        try:
            total += compute_value(json.loads(path.read_text()).get("compute")) or 0.0
        except (OSError, ValueError, TypeError, RunError):
            continue
    return total


def parse_verify_output(stdout: str) -> dict:
    """The last non-empty stdout line: a bare finite number, or JSON with a finite metric (and optional status,
    compute, notes). NaN, Infinity and booleans are not metrics; they return {}."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return {}
    try:
        value = json.loads(lines[-1].strip(), parse_constant=_reject_constant)
    except ValueError:
        return {}
    if finite_number(value):
        return {"metric": float(value)}
    if not isinstance(value, dict):
        return {}
    if "metric" in value and not finite_number(value["metric"]):
        return {"invalid": "metric is not a finite number"}
    return value


def obs_core(obs: dict) -> dict:
    return {k: v for k, v in obs.items() if k != "content_sha256"}


def content_hash(obj: dict) -> str:
    return hashlib.sha256(jdumps(obj, sort_keys=True).encode()).hexdigest()


def write_observation(run_dir: Path, obs: dict) -> Path:
    """Immutable: exclusive create, then read-only. A second write of the same id refuses."""
    path = run_dir / "observations" / f"{obs['id']}.json"
    body = obs_core(obs)
    body["content_sha256"] = content_hash(body)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(jdumps(body, indent=2, sort_keys=True) + "\n")
    return path


def verify_observation(path: Path) -> bool:
    data = json.loads(path.read_text())
    want = data.pop("content_sha256", "")
    return content_hash(data) == want


def run_command(cmd: str, cwd: Path, env: dict, timeout: float, out: Path, err: Path) -> tuple[int | None, str, float]:
    """No shell. Runs in its own process group; on timeout the group gets TERM, then KILL after a short grace.
    Returns (exit code or None on timeout, stdout, wall seconds). Output goes to the log files."""
    started = time.monotonic()
    if timeout <= 0:
        out.write_text(""); err.write_text("no time left in the budget")
        return None, "", 0.0
    try:
        proc = subprocess.Popen(shlex.split(cmd), cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except (OSError, ValueError) as err_:
        out.write_text(""); err.write_text(f"could not start command: {err_}")
        return 127, "", 0.0
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        for sig, grace in ((signal.SIGTERM, KILL_GRACE_SECONDS), (signal.SIGKILL, 10.0)):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, sig)
            try:
                stdout, stderr = proc.communicate(timeout=grace)
                break
            except subprocess.TimeoutExpired:
                continue
        else:
            stdout, stderr = "", ""
        code = None
    out.write_text(stdout or "")
    err.write_text(stderr or "")
    return code, stdout or "", round(time.monotonic() - started, 3)


def comment_text(spec: dict, rows: list[dict], best: dict | None, iteration: int, stop: str = "") -> str:
    last = rows[-1] if rows else {}
    name = spec["metric"]["name"] or "metric"
    parts = [f"colab run `{spec['run_id']}`: iteration {iteration} of {spec['budgets']['iterations']}",
             f"- latest {name}: {last.get('metric')} ({last.get('status')}), wall {last.get('wall_seconds')} s"]
    if best:
        parts.append(f"- best {name}: {best['metric']} at iteration {best['iteration']}")
    if stop:
        parts.append(f"- stopped: {stop}")
    parts.append(f"<!-- colab-run id={spec['run_id']} iter={iteration} -->")
    return "\n".join(parts)


def recent_rows(run_dir: Path) -> list[dict]:
    rows = []
    for path in sorted((run_dir / "observations").glob("*.json")):
        try:
            d = json.loads(path.read_text())
            rows.append({"iteration": d["iteration"], "metric": d["metric"]["value"], "status": d["status"],
                         "wall_seconds": d["wall_seconds"]})
        except (OSError, ValueError, KeyError):
            continue
    return rows


def best_row(rows: list[dict], direction: str) -> dict | None:
    scored = [r for r in rows if finite_number(r["metric"]) and r["status"] == "ok"]
    if not scored or direction not in ("higher", "lower"):
        return None
    return (max if direction == "higher" else min)(scored, key=lambda r: r["metric"])


def maybe_comment(run_dir: Path, spec: dict, iteration: int, colab_mod, config: dict, now: datetime, stop: str = "") -> None:
    """Compact progress comment on the issue at the configured cadence. Scanned with `colab.py scan`'s logic
    (external scanner included) first; never for restricted runs."""
    c = spec["comments"]
    if spec["offline"] or spec["restricted"] or not (c["every_iterations"] or c["every_seconds"]):
        return
    with run_lock(run_dir):
        state_path = run_dir / "comment_state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"iteration": 0, "at": 0.0}
        due = bool(stop) or (c["every_iterations"] and iteration - state["iteration"] >= c["every_iterations"]) \
            or (c["every_seconds"] and now.timestamp() - state["at"] >= c["every_seconds"] and iteration > state["iteration"])
        if not due:
            return
        atomic_write(state_path, jdumps({"iteration": iteration, "at": now.timestamp()}))
    rows = recent_rows(run_dir)
    text = comment_text(spec, rows, best_row(rows, spec["metric"]["direction"]), iteration, stop)
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "comment.txt"
        probe.write_text(text)
        clean = scan_ok(colab_mod, config, [probe])
    if not clean:
        log_md(run_dir, "progress comment withheld: scan finding")
        return
    ok = post_issue_comment(spec["issue_ref"], text)
    log_md(run_dir, f"progress comment {'posted' if ok else 'FAILED'} at iteration {iteration}")


def cmd_step(args, config: dict, colab_mod=None) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    spec = load_run(run_dir)
    guard_restricted(spec, args.in_job)
    spec = confirm_commands(run_dir, spec, args.confirm_verify)
    spec = ensure_workspace(run_dir, spec)
    if args.metric_value is not None and not finite_number(args.metric_value):
        raise RunError("--metric-value must be a finite number")
    workspace = run_dir / spec["workspace"]["path"]
    mflags = monitor_flags(spec) + (["--direction", spec["metric"]["direction"]] if spec["metric"]["direction"] else [])
    now_text = args.now
    with run_lock(run_dir):
        code, state = monitor(run_dir, "check", mflags, now_text)
        if code == 0 and state.get("status") != "running":
            raise RunError(f"run is not active (status {state.get('status')}); start a new run")
        n = int((run_dir / "counter").read_text() or 0)
        stop = None
        if code != 0:
            stop = (state.get("break_category", "break"), state.get("break_reason", "loop breaker"))
        else:
            stop = stop_reason(run_dir, spec, n, compute_used(run_dir))
        if stop:
            if code == 0:  # the engine's own budgets (iterations, compute); the monitor records its own breaks
                meta = json.loads((run_dir / "metadata.json").read_text())
                meta["status"] = "budget_exhausted"
                atomic_write(run_dir / "metadata.json", jdumps(meta, indent=2) + "\n")
            log_md(run_dir, f"stop: {stop[0]}: {stop[1]}")
            print(jdumps({"stop": {"category": stop[0], "reason": stop[1]}, "iterations_done": n}))
            return STOP_CODE
        iteration = n + 1
        atomic_write(run_dir / "counter", str(iteration))
        remaining = min(state.get("remaining_loop_seconds", 1), state.get("remaining_overall_seconds", 1))
        slice_started = state.get("slice_started_at_utc", "")
    # ---- unlocked: the long part
    params = {**spec["params"], **parse_params(args.param)}
    env = dict(os.environ, COLAB_RUN_DIR=str(run_dir), COLAB_WORKSPACE=str(workspace), COLAB_ITERATION=str(iteration),
               COLAB_ISSUE=spec["issue"], COLAB_PARAMS=jdumps(params, sort_keys=True))
    for key, value in params.items():
        env["COLAB_PARAM_" + re.sub(r"[^A-Za-z0-9]", "_", key).upper()] = str(value)
    tag = f"iter-{iteration:04d}"
    out_log, err_log = run_dir / "logs" / f"{tag}.stdout.log", run_dir / "logs" / f"{tag}.stderr.log"
    budget_left = float(remaining)
    timeout = min(float(args.step_timeout or spec["step_timeout"] or budget_left), budget_left)
    started = time.monotonic()
    code_v, stdout, note = None, "", ""
    measured_by = "verify"
    result: dict = {}
    if args.metric_value is not None:  # the caller measured it already (e.g. a Slurm job it waited on)
        result, status, measured_by = {"metric": args.metric_value}, "ok", "manual"
        out_log.write_text(""); err_log.write_text("")
    else:
        if spec["iterate"]:
            ic, _, _ = run_command(spec["iterate"], workspace, env, timeout, run_dir / "logs" / f"{tag}.iterate.stdout.log",
                                   run_dir / "logs" / f"{tag}.iterate.stderr.log")
            if ic != 0:
                note = f"iterate command exit {ic}"
        code_v, stdout, _ = run_command(spec["verify"], workspace, env, timeout - (time.monotonic() - started), out_log, err_log)
        result = parse_verify_output(stdout)
        status = "timeout" if code_v is None else ("crash" if code_v != 0 else "ok")
        if status == "ok" and not finite_number(result.get("metric")):
            status, note = "crash", (note + "; " if note else "") + (result.get("invalid") or "verify printed no finite metric on its last line")
        if status == "ok" and note.startswith("iterate"):
            status = "crash"
    wall = round(time.monotonic() - started, 3)
    compute = None
    if status == "ok":
        try:
            compute = compute_value(result.get("compute"))
        except RunError as err:
            status, note = "crash", (note + "; " if note else "") + str(err)
    metric = result.get("metric") if status == "ok" else None
    if isinstance(result.get("status"), str) and status == "ok":
        status = result["status"].lower()
    free_note = result.get("notes", "")
    note = "; ".join(x for x in (note, str(free_note) if isinstance(free_note, (str, int, float)) and not isinstance(free_note, bool) else "") if x)
    git = git_state(workspace)
    done_at = parse_now(now_text) if now_text else utcnow()
    slice_epoch = int(parse_now(slice_started).timestamp()) if slice_started else 0
    obs = {
        "schema": SCHEMA, "@type": "colab:Observation", "@context": PROV_CONTEXT, "id": f"{spec['run_id']}-{iteration:04d}",
        "run_id": spec["run_id"], "issue": spec["issue"], "iteration": iteration, "timestamp_utc": iso(done_at),
        "tier": spec["tier"], "status": status,
        "agent": spec["agent"], "acting_for": spec["acting_for"] or None,
        "verify_sha256": spec["verify_sha256"], "measured_by": measured_by,
        "prov:wasGeneratedBy": {"prov:activity": f"urn:colab:activity:step:{spec['run_id']}:{iteration}",
                                "prov:wasInformedBy": f"urn:labnb:activity:start:{spec['run_id']}:{slice_epoch}",
                                "prov:wasAssociatedWith": f"urn:colab:agent:{spec['agent']}",
                                "prov:actedOnBehalfOf": spec["acting_for"] or None},
        "parameters": params, "git_commit": git["commit"], "git_dirty": git["dirty"],
        "container_digest": args.container_digest or spec["container_digest"] or None,
        "slurm_job_id": args.slurm_job or os.environ.get("SLURM_JOB_ID") or None,
        "metric": {"name": spec["metric"]["name"], "direction": spec["metric"]["direction"], "value": metric},
        "compute": compute, "wall_seconds": wall,
        "logs": {"stdout": f"logs/{tag}.stdout.log", "stderr": f"logs/{tag}.stderr.log"},
        "exit_code": code_v, "notes": note,
    }
    with run_lock(run_dir):
        try:
            path = write_observation(run_dir, obs)
        except FileExistsError:
            raise RunError(f"observation {obs['id']} already exists: the iteration counter is out of sync; "
                           "inspect the run directory before going on")
        _, st = monitor(run_dir, "check", ["--exit-zero"] + mflags, now_text)  # fresh elapsed values for the row
        append_line(run_dir / "results.tsv", "\t".join(tsv(x) for x in [
            iteration, obs["timestamp_utc"], status, "" if metric is None else metric,
            st.get("slice_elapsed_seconds", ""), st.get("overall_elapsed_seconds", ""), git["commit"] or "", note]))
        log_md(run_dir, f"iteration {iteration}: {status}, {spec['metric']['name'] or 'metric'}={metric}, wall {wall}s")
        prov_log(run_dir, "step", {"iteration": iteration, "status": status, "observation": path.name,
                                   "measured_by": measured_by}, done_at)
    if colab_mod is not None:
        maybe_comment(run_dir, spec, iteration, colab_mod, config, done_at)
    print(jdumps({"iteration": iteration, "status": status, "metric": metric, "wall_seconds": wall,
                  "observation": str(path.relative_to(run_dir))}))
    return 0


# --------------------------------------------------------------------------- loop, check, finish


def final_status_after_stop(run_dir: Path, fallback: str) -> str:
    try:
        status = json.loads((run_dir / "metadata.json").read_text()).get("status", "")
    except (OSError, ValueError):
        status = ""
    return status if status in ("budget_exhausted", "stopped", "blocked", "crashed") else fallback


def cmd_finish(args, config: dict, colab_mod=None, stopped: str = "") -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    spec = load_run(run_dir)
    final = args.final_status
    with run_lock(run_dir):
        if final == "auto":
            final = final_status_after_stop(run_dir, "completed")
        code, state = monitor(run_dir, "finish", ["--final-status", final], args.now)
        n = int((run_dir / "counter").read_text() or 0)
        rows = recent_rows(run_dir)
        best = best_row(rows, spec["metric"]["direction"])
        summary = [f"# Summary: {spec['issue']}", "", f"- Run: {spec['run_id']}", f"- Final status: {final}",
                   f"- Iterations: {len(rows)}", f"- Best {spec['metric']['name'] or 'metric'}: "
                   f"{best['metric'] if best else 'n/a'}" + (f" (iteration {best['iteration']})" if best else "")]
        atomic_write(run_dir / "summary.md", "\n".join(summary) + "\n")
        log_md(run_dir, f"finished: {final}")
        prov_log(run_dir, "finish", {"final_status": final, "iterations": len(rows)})
    if colab_mod is not None and not args.no_comment:
        spec_c = {**spec, "comments": {"every_iterations": 1, "every_seconds": 0}}
        maybe_comment(run_dir, spec_c, n, colab_mod, config, utcnow(), stop=stopped or f"finished ({final})")
    print(jdumps({"final_status": final, "iterations": len(rows), "best": best}))
    return 0


def cmd_loop(args, config: dict, colab_mod=None) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    spec = load_run(run_dir)
    guard_restricted(spec, args.in_job)
    confirm_commands(run_dir, spec, args.confirm_verify)
    steps, stop_msg = 0, ""
    while not args.max_steps or steps < args.max_steps:
        code = cmd_step(args, config, colab_mod)
        if code == STOP_CODE:
            stop_msg = "stop condition"
            break
        steps += 1
    final = argparse.Namespace(run_dir=str(run_dir), final_status="auto" if stop_msg else "completed",
                               now=None, no_comment=False)
    cmd_finish(final, config, colab_mod, stopped=stop_msg)
    return STOP_CODE if stop_msg else 0


def cmd_check(args, config: dict) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    spec = load_run(run_dir)
    mflags = monitor_flags(spec) + (["--direction", spec["metric"]["direction"]] if spec["metric"]["direction"] else [])
    with run_lock(run_dir):
        code, state = monitor(run_dir, "check", mflags, args.now)
    print(jdumps(state, indent=2))
    return code


def cmd_status(args, config: dict) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    spec = load_run(run_dir)
    rows = recent_rows(run_dir)
    print(jdumps({"run_id": spec["run_id"], "issue": spec["issue"], "restricted": spec["restricted"], "tier": spec["tier"],
                  "budgets": spec["budgets"], "observations": len(rows),
                  "confirmed": spec.get("confirmed_sha256") == spec["verify_sha256"],
                  "best": best_row(rows, spec["metric"]["direction"]),
                  "compute_used": compute_used(run_dir),
                  "status": json.loads((run_dir / "metadata.json").read_text()).get("status")}, indent=2))
    return 0


# --------------------------------------------------------------------------- stage


RESTRICTED_KEEP = ("schema", "@type", "@context", "id", "run_id", "issue", "iteration", "timestamp_utc", "tier", "status",
                   "agent", "acting_for", "verify_sha256", "measured_by", "prov:wasGeneratedBy", "git_commit",
                   "container_digest", "slurm_job_id", "compute", "wall_seconds", "exit_code")
SAFE_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def stage_projection(obs: dict, allow_aggregates: bool) -> dict:
    """What is written to the meta repo. A restricted run stages numbers and ids only: no notes, no log pointers, and
    only numeric or boolean parameters, unless --allow-restricted-aggregates."""
    if obs.get("tier") != "restricted" or allow_aggregates:
        return obs
    out = {k: obs[k] for k in RESTRICTED_KEEP if k in obs}
    out["metric"] = {"name": obs["metric"]["name"], "direction": obs["metric"]["direction"], "value": obs["metric"]["value"]}
    out["parameters"] = {k: v for k, v in (obs.get("parameters") or {}).items()
                         if SAFE_KEY.match(k) and (finite_number(v) or isinstance(v, bool))}
    out["source_content_sha256"] = obs.get("content_sha256")
    out.pop("content_sha256", None)
    out["content_sha256"] = content_hash(out)
    return out


def cmd_stage(args, config: dict, colab_mod, runner=subprocess.run) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    spec = load_run(run_dir)
    meta = Path(args.meta).expanduser().resolve()
    if not (meta / ".colab").is_dir():
        raise RunError(f"{meta} does not look like a clone of the meta repo (no .colab/)")
    dest = meta / ".colab" / "staging" / "observations" / spec["run_id"]
    files = sorted((run_dir / "observations").glob("*.json"))
    if not files:
        raise RunError("no observations to stage")
    for f in files:
        if not verify_observation(f):
            raise RunError(f"observation changed after it was written: {f.name}")
    allow = bool(getattr(args, "allow_restricted_aggregates", False))
    with tempfile.TemporaryDirectory() as tmp:  # scan first, in a scratch dir: a finding touches nothing already staged
        pending: list[Path] = []
        for f in files:
            text = jdumps(stage_projection(json.loads(f.read_text()), allow), indent=2, sort_keys=True) + "\n"
            target = dest / f.name
            if target.exists() and target.read_text() != text:
                raise RunError(f"{target.name} already staged with different content; observations are immutable")
            probe = Path(tmp) / f.name
            probe.write_text(text)
            pending.append(probe)
        clean = scan_ok(colab_mod, config, pending)
        print(jdumps({"scan": "clean" if clean else "finding"}))
        if not clean:
            print("scan findings: nothing new was staged", file=sys.stderr)
            return 1
        dest.mkdir(parents=True, exist_ok=True)
        staged = []
        for probe in pending:
            target = dest / probe.name
            shutil.copyfile(probe, target)
            staged.append(target)
    rel = [str(t.relative_to(meta)) for t in staged]
    print(jdumps({"staged": rel}))
    prov_log(run_dir, "stage", {"files": len(rel), "open_pr": bool(args.open_pr)})
    if args.open_pr:
        branch = f"colab-run/observations-{spec['run_id']}"
        title = f"Stage observations: {spec['issue']} ({spec['run_id']})"
        steps = [["git", "-C", str(meta), "fetch", "origin", "main"],
                 ["git", "-C", str(meta), "switch", "-c", branch, "origin/main"],
                 ["git", "-C", str(meta), "add", *rel],
                 ["git", "-C", str(meta), "commit", "-m", title], ["git", "-C", str(meta), "push", "-u", "origin", branch],
                 ["gh", "pr", "create", "--title", title, "--head", branch, "--base", "main",
                  "--body", f"Immutable Observation records from run `{spec['run_id']}` on {spec['issue']}. Scanned clean. Lead review requested."]]
        for step in steps:
            if runner(step, capture_output=True, text=True, cwd=meta).returncode != 0:
                raise RunError(f"open-pr failed at: {' '.join(step[:4])}")
    return 0


# --------------------------------------------------------------------------- cleanup


def cmd_cleanup(args, config: dict) -> int:
    run_dir = default_root(config, args.root) / args.run_id
    spec = load_run(run_dir)
    if not args.yes:
        raise RunError(f"cleanup removes the workspace and branch of {spec['run_id']}; re-run with --yes to confirm")
    status = json.loads((run_dir / "metadata.json").read_text()).get("status", "")
    if status in ("started", "planned") and not args.force:
        raise RunError(f"the run is still {status}; finish it first (or pass --force)")
    ws, workspace = spec["workspace"], run_dir / spec["workspace"]["path"]
    done = []
    if ws.get("mode") == "worktree" and ws.get("source"):
        src = Path(ws["source"])
        if workspace.exists():
            _git(["worktree", "remove", "--force", str(workspace)], src)
            done.append("worktree removed")
        _git(["worktree", "prune"], src)
        if ws.get("branch") and _git(["branch", "-D", ws["branch"]], src).returncode == 0:
            done.append(f"branch {ws['branch']} deleted")
    elif workspace.exists():
        shutil.rmtree(workspace)
        done.append("workspace removed")
    prov_log(run_dir, "cleanup", {"done": done})
    if args.delete_run_dir:
        shutil.rmtree(run_dir)
        done.append("run directory deleted")
    print(jdumps({"run_id": spec["run_id"], "done": done}))
    return 0


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="lab-colab config TOML (default: COLAB_CONFIG, else the bundled one)")
    sub = ap.add_subparsers(dest="action", required=True)

    p = sub.add_parser("start", help="make the workspace and run spec for a claimed Experiment issue")
    p.add_argument("issue", help="owner/repo#n")
    p.add_argument("--body-file", help="issue body from a file instead of the issue (labels and visibility are still read)")
    p.add_argument("--label", action="append", help="extra issue label (repeatable), e.g. tier:restricted")
    p.add_argument("--offline", action="store_true",
                   help="no gh at all (tests, air-gapped): you assert labels (--label) and visibility (--visibility)")
    p.add_argument("--visibility", default="private", choices=("public", "private", "internal"), help="with --offline only")
    p.add_argument("--claimant", help="GitHub login acting for (default: gh api user)")
    p.add_argument("--editor", help="who last edited the issue body (default: read from GitHub)")
    p.add_argument("--agent", help="agent name recorded in Observations (default COLAB_AGENT)")
    p.add_argument("--python", help="interpreter written into job.sbatch (default [run] python, else this one; needs 3.11+)")
    p.add_argument("--root", help="runs root (default COLAB_RUN_ROOT, [run].root, else ${XDG_STATE_HOME:-~/.local/state}/lab-colab/runs)")
    p.add_argument("--source", help="repo or directory the workspace is made from")
    p.add_argument("--mode", choices=("auto", "worktree", "dir"))
    p.add_argument("--base", default="", help="git ref for the worktree (default HEAD)")
    p.add_argument("--iterations", type=int); p.add_argument("--wall"); p.add_argument("--loop"); p.add_argument("--compute")
    p.add_argument("--metric"); p.add_argument("--direction", choices=("higher", "lower"))
    p.add_argument("--verify", help="verify command; its last stdout line is the metric (number, or JSON with metric)")
    p.add_argument("--iterate-cmd", help="optional command run in the workspace before each verify")
    p.add_argument("--step-timeout", type=float, default=0, help="seconds per command (default: what the budget leaves)")
    p.add_argument("--param", action="append", help="key=value, repeatable; env COLAB_PARAM_<KEY> for the commands")
    p.add_argument("--container-digest"); p.add_argument("--restricted", action="store_true"); p.add_argument("--partition")
    p.add_argument("--comment-every", type=int, help="progress comment every N iterations (0 = off; default [run].comment_every)")
    p.add_argument("--comment-every-seconds", type=int)
    p.add_argument("--patience", type=int, default=0); p.add_argument("--max-failures", type=int, default=0)
    p.add_argument("--stall-seconds", type=int, default=0); p.add_argument("--reserve-seconds", type=int, default=0)
    p.add_argument("--usage-file"); p.add_argument("--governance-file")
    p.add_argument("--min-trust-score", type=float); p.add_argument("--metric-guardrail", type=float)
    p.add_argument("--break-on-drift", action="store_true"); p.add_argument("--now")

    for name, help_ in (("step", "one iteration"), ("loop", "iterate until a stop condition")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--run-dir", required=True); p.add_argument("--param", action="append")
        p.add_argument("--confirm-verify", metavar="SHA256", help="the sha256 start printed for the verify/iterate commands")
        p.add_argument("--metric-value", type=float, help="record this metric instead of running verify")
        p.add_argument("--slurm-job"); p.add_argument("--container-digest"); p.add_argument("--step-timeout", type=float, default=0)
        p.add_argument("--in-job", action="store_true", help="inside the batch job from job.sbatch (required for restricted runs)")
        p.add_argument("--now")
        if name == "loop":
            p.add_argument("--max-steps", type=int, default=0)
    p = sub.add_parser("confirm", help="confirm the verify/iterate commands start printed, by their sha256")
    p.add_argument("--run-dir", required=True); p.add_argument("--sha256", required=True)
    p = sub.add_parser("check"); p.add_argument("--run-dir", required=True); p.add_argument("--now")
    p = sub.add_parser("finish"); p.add_argument("--run-dir", required=True)
    p.add_argument("--final-status", default="auto"); p.add_argument("--no-comment", action="store_true"); p.add_argument("--now")
    p = sub.add_parser("status"); p.add_argument("--run-dir", required=True); p.add_argument("--now")
    p = sub.add_parser("stage"); p.add_argument("--run-dir", required=True)
    p.add_argument("--meta", required=True, help="clone of the meta repo"); p.add_argument("--open-pr", action="store_true")
    p.add_argument("--allow-restricted-aggregates", action="store_true",
                   help="stage a restricted run's full record (notes, free-text parameters, log pointers); default stages numbers and ids only")
    p = sub.add_parser("cleanup", help="remove a run's workspace: git worktree remove and prune, delete the colab-run/<id> branch")
    p.add_argument("run_id"); p.add_argument("--root"); p.add_argument("--yes", action="store_true", help="confirm the removal")
    p.add_argument("--force", action="store_true", help="even if the run is still active")
    p.add_argument("--delete-run-dir", action="store_true", help="also delete logs and observations")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        colab_mod, config = load_colab(args.config)
        if args.action == "start":
            return cmd_start(args, config)
        if args.action == "step":
            return cmd_step(args, config, colab_mod)
        if args.action == "loop":
            return cmd_loop(args, config, colab_mod)
        if args.action == "confirm":
            run_dir = Path(args.run_dir).expanduser().resolve()
            spec = confirm_commands(run_dir, load_run(run_dir), args.sha256)
            print(jdumps({"confirmed": spec["verify_sha256"]}))
            return 0
        if args.action == "check":
            return cmd_check(args, config)
        if args.action == "finish":
            return cmd_finish(args, config, colab_mod)
        if args.action == "status":
            return cmd_status(args, config)
        if args.action == "stage":
            return cmd_stage(args, config, colab_mod)
        if args.action == "cleanup":
            return cmd_cleanup(args, config)
    except RunError as err:
        print(f"colab run: {err}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
