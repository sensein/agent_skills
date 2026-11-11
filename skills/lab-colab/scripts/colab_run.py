#!/usr/bin/env python3
"""colab-run: run a claimed Experiment issue in an isolated workspace, under explicit budgets.

Stdlib only, Python 3.11+. Also reachable as `colab.py run <action> ...`.

Actions
  start    Read the Experiment issue (or --body-file), resolve budgets and the metric, make an isolated
           workspace (git worktree, or a copied run directory), write the run spec, start the monitored slice.
  step     One iteration: monitor check, optional iteration command, verify command, then log the result:
           results.tsv row, an immutable Observation JSON, log.md, and a progress comment when due.
  loop     Repeat `step` until a stop condition (monitor break, iterations, compute, or --max-steps).
  check    Run the loop breaker (monitor_slice.py check) with the run's stored thresholds.
  finish   Close the slice and set the final status.
  stage    Copy the run's Observations into <meta clone>/.colab/staging/observations/ (scanned); --open-pr
           makes the branch, commit, push and PR in the meta repo. It never merges.
  status   Print the run spec, the monitor state and the observation count.

Run directory (default ${XDG_STATE_HOME:-~/.local/state}/lab-colab/runs/<owner>-<repo>-<n>-<timestamp>)
  run.json  metadata.json  provenance.jsonl  results.tsv  log.md  observations/  logs/  workspace/
All local run logs are append-only (single O_APPEND writes under a directory lock); observations are written
once, exclusively, and made read-only. Iteration numbers are allocated under the lock, so parallel steps never collide.

Restricted data: a run whose issue carries tier:restricted (or whose Data field says restricted, or --restricted)
never executes in a session. `start` writes job.sbatch and stops; an ORCD job runs `loop --in-job`.

Exit codes: 0 ok, 2 usage or refusal, 4 the run must stop (monitor break, iterations, compute).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import socket
import string
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


@contextmanager
def run_lock(run_dir: Path, timeout_s: float = 60.0, poll_s: float = 0.02, stale_s: float = 300.0):
    """Directory lock (mkdir is atomic). A lock whose owner died on this host, or that is very old, is broken
    by renaming it away first, so only one waiter can break it and nobody deletes a fresh lock."""
    lock = run_dir / ".lock"
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lock.mkdir()
            (lock / "owner.json").write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                                                         "at": time.time()}))
            break
        except FileExistsError:
            try:
                owner = json.loads((lock / "owner.json").read_text())
            except (OSError, ValueError):
                owner = {}
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                continue  # released between the mkdir and the stat
            dead = owner.get("host") == socket.gethostname() and not _pid_alive(int(owner.get("pid", 0) or 0))
            if (dead and owner) or age > stale_s:
                grave = run_dir / f".lock.stale-{os.getpid()}-{random.randrange(10**9)}"
                try:
                    lock.rename(grave)
                    shutil.rmtree(grave, ignore_errors=True)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {lock}")
            time.sleep(poll_s)
    try:
        yield
    finally:
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


def fetch_issue(ref: dict) -> dict:
    proc = subprocess.run(["gh", "api", f"repos/{ref['owner']}/{ref['repo']}/issues/{ref['n']}"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RunError(f"could not read {ref['owner']}/{ref['repo']}#{ref['n']}: {proc.stderr.strip()[:200]}")
    data = json.loads(proc.stdout)
    return {"body": data.get("body") or "", "labels": [x["name"] for x in data.get("labels", [])],
            "title": data.get("title", "")}


def post_issue_comment(ref: dict, body: str) -> bool:
    proc = subprocess.run(["gh", "api", "-X", "POST", f"repos/{ref['owner']}/{ref['repo']}/issues/{ref['n']}/comments",
                           "--input", "-"], input=json.dumps({"body": body}), capture_output=True, text=True)
    return proc.returncode == 0


def load_colab(config_path: str | None):
    import colab  # noqa: PLC0415 (path set above)
    env = os.environ.get("COLAB_CONFIG")
    return colab, colab.load_config(Path(config_path) if config_path else (Path(env) if env else None))


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


# --------------------------------------------------------------------------- start


def parse_params(pairs: list[str]) -> dict:
    out: dict = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise RunError(f"--param needs key=value, got {pair!r}")
        try:
            out[key] = json.loads(value)
        except ValueError:
            out[key] = value
    return out


def cmd_start(args, config: dict) -> int:
    m = ISSUE_REF.match(args.issue)
    if not m:
        raise RunError("issue must look like owner/repo#n")
    ref = {"owner": m["owner"], "repo": m["repo"], "n": int(m["n"])}
    if args.body_file:
        issue = {"body": Path(args.body_file).read_text(), "labels": list(args.label or []), "title": ""}
    elif args.offline:
        raise RunError("--offline needs --body-file (the issue body) or budgets on the command line")
    else:
        issue = fetch_issue(ref)
        issue["labels"] += list(args.label or [])
    form = spec_from_issue(issue["body"], issue["labels"])
    budgets = resolve_budgets(form["tokens"], args)
    verify = args.verify or form["verify"]
    if not verify:
        raise RunError("no verify command: add a Verify command field to the issue or pass --verify")
    direction = (args.direction or form["tokens"].get("direction", "")).strip().lower()
    if direction not in ("", "higher", "lower"):
        raise RunError("direction must be higher or lower")
    metric = args.metric or form["tokens"].get("metric", "")
    restricted = bool(args.restricted or form["restricted"])
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
    try:
        workspace = make_workspace(run_dir, run_id, Path(args.source).expanduser().resolve() if args.source else None,
                                   args.mode or run_cfg.get("mode", "auto"), args.base)
    except RunError:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    (run_dir / "observations").mkdir()
    (run_dir / "logs").mkdir()
    created = iso(parse_now(args.now))
    comment_every = args.comment_every if args.comment_every is not None else int(run_cfg.get("comment_every", 5))
    comment_secs = args.comment_every_seconds if args.comment_every_seconds is not None else int(run_cfg.get("comment_every_seconds", 0))
    spec = {
        "schema": "colab-run/1", "run_id": run_id, "created_at_utc": created,
        "issue": f"{ref['owner']}/{ref['repo']}#{ref['n']}", "issue_ref": ref, "title": issue.get("title", ""),
        "hypothesis": form["hypothesis"], "restricted": restricted, "offline": bool(args.offline),
        "budgets": budgets, "metric": {"name": metric, "direction": direction},
        "verify": verify, "iterate": args.iterate_cmd or "", "step_timeout": args.step_timeout,
        "params": parse_params(args.param), "workspace": workspace,
        "container_digest": args.container_digest or os.environ.get("COLAB_CONTAINER_DIGEST", ""),
        "comments": {"every_iterations": max(0, comment_every), "every_seconds": max(0, comment_secs)},
        "monitor": {"patience": args.patience, "max_failures": args.max_failures, "stall_seconds": args.stall_seconds,
                    "reserve_seconds": args.reserve_seconds, "usage_file": args.usage_file or "",
                    "governance_file": args.governance_file or "", "min_trust_score": args.min_trust_score,
                    "metric_guardrail": args.metric_guardrail, "break_on_drift": args.break_on_drift},
    }
    atomic_write(run_dir / "run.json", json.dumps(spec, indent=2) + "\n")
    atomic_write(run_dir / "metadata.json", json.dumps({
        "entry_id": run_id, "entry_kind": "experiment", "status": "planned", "created_at_utc": created,
        "entry_dir": str(run_dir), "experiment_dir": str(run_dir), "objective": issue.get("title", "") or spec["issue"],
        "metric_name": metric, "direction": direction, "verify_command": verify,
        "overall_budget": budgets["wall"], "overall_budget_seconds": budgets["wall_seconds"],
        "loop_budget": budgets["loop"], "loop_budget_seconds": budgets["loop_seconds"],
        "workspace_dir": str(run_dir / "workspace"), "issue": spec["issue"], "restricted": restricted,
    }, indent=2) + "\n")
    append_line(run_dir / "results.tsv", "\t".join(RESULTS_HEADER))
    append_line(run_dir / "log.md", "# Run log\n")
    atomic_write(run_dir / "counter", "0")
    code, state = monitor(run_dir, "start", [], args.now)
    log_md(run_dir, f"started {spec['issue']} budgets iterations={budgets['iterations']} wall={budgets['wall']} "
                    f"compute={budgets['compute'] or 'none'}; workspace {workspace['mode']}")
    out = {"run_dir": str(run_dir), "run_id": run_id, "workspace": str(run_dir / "workspace"), "mode": workspace["mode"],
           "restricted": restricted, "budgets": budgets, "monitor": state.get("status")}
    if restricted:
        out["job_script"] = str(write_job_script(run_dir, spec, args))
        out["next"] = ("restricted: nothing runs in this session. Review job.sbatch, then submit it (ask the owner first): "
                       f"sbatch {out['job_script']}")
    print(json.dumps(out, indent=2))
    return 0


def write_job_script(run_dir: Path, spec: dict, args) -> Path:
    partition = args.partition or os.environ.get("COLAB_RUN_PARTITION", "")
    lines = ["#!/bin/bash", f"#SBATCH --job-name=colab-run-{spec['run_id']}"[:60],
             f"#SBATCH --time={max(1, spec['budgets']['wall_seconds'] // 60 + 5)}",
             f"#SBATCH --output={run_dir}/logs/job-%j.out"]
    if partition:
        lines.append(f"#SBATCH --partition={partition}")
    lines += ["set -euo pipefail",
              "# Restricted data: this job, not a Claude session, reads it. Aggregates and pointers only leave the run dir.",
              f"python3 {shlex.quote(str(HERE / 'colab_run.py'))} loop --run-dir {shlex.quote(str(run_dir))} --in-job"]
    path = run_dir / "job.sbatch"
    atomic_write(path, "\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------- step


def stop_reason(run_dir: Path, spec: dict, rows_done: int, compute_used: float) -> tuple[str, str] | None:
    b = spec["budgets"]
    if rows_done >= b["iterations"]:
        return "budget", f"iterations budget reached ({b['iterations']})"
    if b["compute"] and compute_used >= b["compute"]:
        return "budget", f"compute budget reached ({compute_used:g} of {b['compute']:g})"
    return None


def compute_used(run_dir: Path) -> float:
    total = 0.0
    for path in (run_dir / "observations").glob("*.json"):
        try:
            total += float(json.loads(path.read_text()).get("compute") or 0)
        except (OSError, ValueError, TypeError):
            continue
    return total


def parse_verify_output(stdout: str) -> dict:
    """The last non-empty stdout line: a bare number, or JSON with metric (and optional status, compute, notes)."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return {}
    last = lines[-1].strip()
    try:
        value = json.loads(last)
    except ValueError:
        return {}
    if isinstance(value, bool):
        return {}
    if isinstance(value, (int, float)):
        return {"metric": float(value)}
    return value if isinstance(value, dict) else {}


def write_observation(run_dir: Path, obs: dict) -> Path:
    """Immutable: exclusive create, then read-only. A second write of the same id refuses."""
    path = run_dir / "observations" / f"{obs['id']}.json"
    body = {k: v for k, v in obs.items() if k != "content_sha256"}
    body["content_sha256"] = hashlib.sha256(json.dumps(obs_core(obs), sort_keys=True).encode()).hexdigest()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return path


def obs_core(obs: dict) -> dict:
    return {k: v for k, v in obs.items() if k != "content_sha256"}


def verify_observation(path: Path) -> bool:
    data = json.loads(path.read_text())
    want = data.pop("content_sha256", "")
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest() == want


def run_command(cmd: str, cwd: Path, env: dict, timeout: float, out: Path, err: Path) -> tuple[int | None, str, float]:
    """No shell. Returns (exit code or None on timeout, stdout, wall seconds). Output goes to the log files."""
    started = time.monotonic()
    try:
        proc = subprocess.run(shlex.split(cmd), cwd=cwd, env=env, capture_output=True, text=True, timeout=max(1.0, timeout))
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        code, stdout, stderr = None, (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""), \
            (exc.stderr or b"").decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    except (OSError, ValueError) as err_:
        code, stdout, stderr = 127, "", f"could not start command: {err_}"
    out.write_text(stdout)
    err.write_text(stderr)
    return code, stdout, round(time.monotonic() - started, 3)


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
    scored = [r for r in rows if isinstance(r["metric"], (int, float)) and r["status"] == "ok"]
    if not scored or direction not in ("higher", "lower"):
        return None
    return (max if direction == "higher" else min)(scored, key=lambda r: r["metric"])


def maybe_comment(run_dir: Path, spec: dict, iteration: int, colab_mod, config: dict, now: datetime, stop: str = "") -> None:
    """Compact progress comment on the issue at the configured cadence. Scanned first; never in restricted runs."""
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
        atomic_write(state_path, json.dumps({"iteration": iteration, "at": now.timestamp()}))
    rows = recent_rows(run_dir)
    text = comment_text(spec, rows, best_row(rows, spec["metric"]["direction"]), iteration, stop)
    patterns = config.get("scan", {}).get("patterns", {})
    if any(colab_mod.scan_text(text, patterns).values()):
        log_md(run_dir, "progress comment withheld: scan finding")
        return
    ok = post_issue_comment(spec["issue_ref"], text)
    log_md(run_dir, f"progress comment {'posted' if ok else 'FAILED'} at iteration {iteration}")


def guard_restricted(spec: dict, in_job: bool) -> None:
    if spec["restricted"] and not (in_job and os.environ.get("SLURM_JOB_ID")):
        raise RunError("this run touches restricted data: it must run inside an ORCD job (submit job.sbatch), never in a session")


def cmd_step(args, config: dict, colab_mod=None) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    spec = load_run(run_dir)
    guard_restricted(spec, args.in_job)
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
                atomic_write(run_dir / "metadata.json", json.dumps(meta, indent=2) + "\n")
            log_md(run_dir, f"stop: {stop[0]}: {stop[1]}")
            print(json.dumps({"stop": {"category": stop[0], "reason": stop[1]}, "iterations_done": n}))
            return STOP_CODE
        iteration = n + 1
        atomic_write(run_dir / "counter", str(iteration))
        remaining = min(state.get("remaining_loop_seconds", 1), state.get("remaining_overall_seconds", 1))
    # ---- unlocked: the long part
    params = {**spec["params"], **parse_params(args.param)}
    env = dict(os.environ, COLAB_RUN_DIR=str(run_dir), COLAB_WORKSPACE=str(workspace), COLAB_ITERATION=str(iteration),
               COLAB_ISSUE=spec["issue"], COLAB_PARAMS=json.dumps(params, sort_keys=True))
    for key, value in params.items():
        env["COLAB_PARAM_" + re.sub(r"[^A-Za-z0-9]", "_", key).upper()] = str(value)
    tag = f"iter-{iteration:04d}"
    out_log, err_log = run_dir / "logs" / f"{tag}.stdout.log", run_dir / "logs" / f"{tag}.stderr.log"
    timeout = min(float(args.step_timeout or spec["step_timeout"] or remaining), float(remaining))
    started = time.monotonic()
    code_v, stdout, note = None, "", ""
    if args.metric_value is not None:  # the caller measured it already (e.g. a Slurm job it waited on)
        result, status = {"metric": args.metric_value}, "ok"
        out_log.write_text(""); err_log.write_text("")
    else:
        if spec["iterate"]:
            ic, _, _ = run_command(spec["iterate"], workspace, env, timeout, run_dir / "logs" / f"{tag}.iterate.stdout.log",
                                   run_dir / "logs" / f"{tag}.iterate.stderr.log")
            if ic != 0:
                note = f"iterate command exit {ic}"
        code_v, stdout, _ = run_command(spec["verify"], workspace, env, max(1.0, timeout - (time.monotonic() - started)),
                                        out_log, err_log)
        result = parse_verify_output(stdout)
        status = "timeout" if code_v is None else ("crash" if code_v != 0 else "ok")
        if status == "ok" and not isinstance(result.get("metric"), (int, float)):
            status, note = "crash", (note + "; " if note else "") + "verify printed no metric on its last line"
        if status == "ok" and note.startswith("iterate"):
            status = "crash"
    wall = round(time.monotonic() - started, 3)
    metric = result.get("metric") if status == "ok" else None
    if isinstance(result.get("status"), str) and status == "ok":
        status = result["status"].lower()
    note = "; ".join(x for x in (note, str(result.get("notes", ""))) if x)
    git = git_state(workspace)
    done_at = parse_now(now_text) if now_text else utcnow()
    obs = {
        "schema": SCHEMA, "@type": "colab:Observation", "id": f"{spec['run_id']}-{iteration:04d}",
        "run_id": spec["run_id"], "issue": spec["issue"], "iteration": iteration, "timestamp_utc": iso(done_at),
        "tier": "restricted" if spec["restricted"] else "lab", "status": status,
        "parameters": params, "git_commit": git["commit"], "git_dirty": git["dirty"],
        "container_digest": args.container_digest or spec["container_digest"] or None,
        "slurm_job_id": args.slurm_job or os.environ.get("SLURM_JOB_ID") or None,
        "metric": {"name": spec["metric"]["name"], "direction": spec["metric"]["direction"], "value": metric},
        "compute": result.get("compute"), "wall_seconds": wall,
        "logs": {"stdout": f"logs/{tag}.stdout.log", "stderr": f"logs/{tag}.stderr.log"},
        "exit_code": code_v, "notes": note,
    }
    with run_lock(run_dir):
        path = write_observation(run_dir, obs)
        _, st = monitor(run_dir, "check", ["--exit-zero"] + mflags, now_text)  # fresh elapsed values for the row
        append_line(run_dir / "results.tsv", "\t".join(tsv(x) for x in [
            iteration, obs["timestamp_utc"], status, "" if metric is None else metric,
            st.get("slice_elapsed_seconds", ""), st.get("overall_elapsed_seconds", ""), git["commit"] or "", note]))
        log_md(run_dir, f"iteration {iteration}: {status}, {spec['metric']['name'] or 'metric'}={metric}, wall {wall}s")
    if colab_mod is not None:
        maybe_comment(run_dir, spec, iteration, colab_mod, config, done_at)
    print(json.dumps({"iteration": iteration, "status": status, "metric": metric, "wall_seconds": wall,
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
    if colab_mod is not None and not args.no_comment:
        spec_c = {**spec, "comments": {"every_iterations": 1, "every_seconds": 0}}
        maybe_comment(run_dir, spec_c, n, colab_mod, config, utcnow(), stop=stopped or f"finished ({final})")
    print(json.dumps({"final_status": final, "iterations": len(rows), "best": best}))
    return 0


def cmd_loop(args, config: dict, colab_mod=None) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    spec = load_run(run_dir)
    guard_restricted(spec, args.in_job)
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
    print(json.dumps(state, indent=2))
    return code


def cmd_status(args, config: dict) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve()
    spec = load_run(run_dir)
    rows = recent_rows(run_dir)
    print(json.dumps({"run_id": spec["run_id"], "issue": spec["issue"], "restricted": spec["restricted"],
                      "budgets": spec["budgets"], "observations": len(rows),
                      "best": best_row(rows, spec["metric"]["direction"]),
                      "compute_used": compute_used(run_dir),
                      "status": json.loads((run_dir / "metadata.json").read_text()).get("status")}, indent=2))
    return 0


# --------------------------------------------------------------------------- stage


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
    dest.mkdir(parents=True, exist_ok=True)
    staged = []
    for f in files:
        target = dest / f.name
        if target.exists() and target.read_bytes() != f.read_bytes():
            raise RunError(f"{target.name} already staged with different content; observations are immutable")
        shutil.copyfile(f, target)
        staged.append(target)
    patterns = config.get("scan", {}).get("patterns", {})
    total = {cat: 0 for cat in patterns}
    for target in staged:
        for cat, count in colab_mod.scan_text(target.read_text(), patterns).items():
            total[cat] += count
    print(json.dumps({"scan": total}))
    if any(total.values()):
        for target in staged:
            target.unlink(missing_ok=True)
        print("scan findings: nothing staged", file=sys.stderr)
        return 1
    rel = [str(t.relative_to(meta)) for t in staged]
    print(json.dumps({"staged": rel}))
    if args.open_pr:
        branch = f"colab-run/observations-{spec['run_id']}"
        title = f"Stage observations: {spec['issue']} ({spec['run_id']})"
        steps = [["git", "-C", str(meta), "switch", "-c", branch], ["git", "-C", str(meta), "add", *rel],
                 ["git", "-C", str(meta), "commit", "-m", title], ["git", "-C", str(meta), "push", "-u", "origin", branch],
                 ["gh", "pr", "create", "--title", title, "--head", branch,
                  "--body", f"Immutable Observation records from run `{spec['run_id']}` on {spec['issue']}. Scanned clean. Lead review requested."]]
        for step in steps:
            if runner(step, capture_output=True, text=True, cwd=meta).returncode != 0:
                raise RunError(f"open-pr failed at: {' '.join(step[:4])}")
    return 0


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="lab-colab config TOML (default: COLAB_CONFIG, else the bundled one)")
    sub = ap.add_subparsers(dest="action", required=True)

    p = sub.add_parser("start", help="make the workspace and run spec for a claimed Experiment issue")
    p.add_argument("issue", help="owner/repo#n")
    p.add_argument("--body-file", help="issue body from a file instead of gh")
    p.add_argument("--label", action="append", help="extra issue label (repeatable), e.g. tier:restricted")
    p.add_argument("--offline", action="store_true", help="no gh at all: no issue read, no comments")
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
        p.add_argument("--metric-value", type=float, help="record this metric instead of running verify")
        p.add_argument("--slurm-job"); p.add_argument("--container-digest"); p.add_argument("--step-timeout", type=float, default=0)
        p.add_argument("--in-job", action="store_true", help="running inside an ORCD job (required for restricted runs)")
        p.add_argument("--now")
        if name == "loop":
            p.add_argument("--max-steps", type=int, default=0)
    p = sub.add_parser("check"); p.add_argument("--run-dir", required=True); p.add_argument("--now")
    p = sub.add_parser("finish"); p.add_argument("--run-dir", required=True)
    p.add_argument("--final-status", default="auto"); p.add_argument("--no-comment", action="store_true"); p.add_argument("--now")
    p = sub.add_parser("status"); p.add_argument("--run-dir", required=True); p.add_argument("--now")
    p = sub.add_parser("stage"); p.add_argument("--run-dir", required=True)
    p.add_argument("--meta", required=True, help="clone of the meta repo"); p.add_argument("--open-pr", action="store_true")
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
        if args.action == "check":
            return cmd_check(args, config)
        if args.action == "finish":
            return cmd_finish(args, config, colab_mod)
        if args.action == "status":
            return cmd_status(args, config)
        if args.action == "stage":
            return cmd_stage(args, config, colab_mod)
    except RunError as err:
        print(f"colab run: {err}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
