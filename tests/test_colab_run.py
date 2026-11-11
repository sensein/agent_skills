"""Tests for `/colab run` (skills/lab-colab/scripts/colab_run.py): the run engine ported from labnb-run.

No network: every start uses --body-file/--offline, or patches the gh calls.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "lab-colab" / "scripts"
RUN = SCRIPTS / "colab_run.py"
COLAB = SCRIPTS / "colab.py"

sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("colab_run", RUN)
run = importlib.util.module_from_spec(_spec)
sys.modules["colab_run"] = run
_spec.loader.exec_module(run)

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.org",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.org")
ISSUE = """### Hypothesis

Larger batch is faster.

### Data (pointers only, never restricted content)

dataset sensein/data#3

### Metrics, including imbalance-aware ones

balanced accuracy

### Compute plan (partition, GPUs, budget)

_No response_

### Run budget (iterations=N, wall=2h, loop=30m, compute=GPU-hours, metric=name, direction=higher|lower)

iterations=4, wall=2h, loop=30m, compute=2 GPU-hours, metric=bacc, direction=higher

### Verify command (its last stdout line is the metric)

`python3 verify.py`
"""
VERIFY = "import json,os\nprint('noise')\nprint(json.dumps({'metric': 0.5 + 0.1*int(os.environ['COLAB_ITERATION']), 'compute': 0.5}))\n"


def cli(*args: str, check: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(RUN), *args], capture_output=True, text=True, check=check, env=env)


def make_source(root: Path, git: bool = False) -> Path:
    src = root / "src"
    src.mkdir()
    (src / "verify.py").write_text(VERIFY)
    (src / "code.txt").write_text("v1\n")
    if git:
        for cmd in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-q", "-m", "init"]):
            subprocess.run(["git", *cmd], cwd=src, check=True, env=GIT_ENV, capture_output=True)
    return src


def start(root: Path, src: Path, *extra: str, body: str = ISSUE, expect: int = 0) -> dict:
    body_file = root / "issue.md"
    body_file.write_text(body)
    proc = cli("start", "sensein/lab#7", "--offline", "--body-file", str(body_file), "--root", str(root / "runs"),
               "--source", str(src), "--comment-every", "0", *extra)
    assert proc.returncode == expect, proc.stderr + proc.stdout
    return json.loads(proc.stdout) if expect == 0 else {"stderr": proc.stderr}


class SpecParsingTests(unittest.TestCase):
    def test_durations(self) -> None:
        self.assertEqual(run.parse_duration_seconds("2h"), 7200)
        self.assertEqual(run.parse_duration_seconds("1 hour 30 min"), 5400)
        self.assertEqual(run.parse_duration_seconds("90"), 90)
        for bad in ("", "soon", "3 fortnights"):
            with self.assertRaises(ValueError):
                run.parse_duration_seconds(bad)

    def test_budgets_and_verify_come_from_the_form_fields(self) -> None:
        spec = run.spec_from_issue(ISSUE, [])
        self.assertEqual(spec["tokens"]["iterations"], "4")
        self.assertEqual(spec["tokens"]["wall"], "2h")
        self.assertEqual(spec["tokens"]["metric"], "bacc")
        self.assertEqual(spec["verify"], "python3 verify.py")
        self.assertFalse(spec["restricted"])

    def test_cli_overrides_form_and_missing_budget_is_refused(self) -> None:
        ns = argparse.Namespace(iterations=9, wall=None, loop=None, compute=None)
        b = run.resolve_budgets(run.spec_from_issue(ISSUE, [])["tokens"], ns)
        self.assertEqual((b["iterations"], b["wall_seconds"], b["loop_seconds"], b["compute"]), (9, 7200, 1800, 2.0))
        empty = argparse.Namespace(iterations=None, wall=None, loop=None, compute=None)
        with self.assertRaises(run.RunError):
            run.resolve_budgets({}, empty)

    def test_restricted_by_label_or_data_field_fails_closed(self) -> None:
        self.assertTrue(run.spec_from_issue(ISSUE, ["tier:restricted"])["restricted"])
        body = ISSUE.replace("dataset sensein/data#3", "restricted cohort, pointer only")
        self.assertTrue(run.spec_from_issue(body, [])["restricted"])

    def test_verify_output_forms(self) -> None:
        self.assertEqual(run.parse_verify_output("x\n0.25\n"), {"metric": 0.25})
        self.assertEqual(run.parse_verify_output('{"metric": 1, "compute": 2}\n')["compute"], 2)
        self.assertEqual(run.parse_verify_output("no metric here\n"), {})
        self.assertEqual(run.parse_verify_output("true\n"), {})


class WorkspaceIsolationTests(unittest.TestCase):
    def test_git_source_gets_its_own_worktree_and_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = make_source(root, git=True)
            a, b = start(root, src), start(root, src)
            self.assertEqual(a["mode"], "worktree")
            self.assertNotEqual(a["workspace"], b["workspace"])
            self.assertNotEqual(a["run_id"], b["run_id"])
            for r in (a, b):
                ws = Path(r["workspace"])
                self.assertTrue((ws / "code.txt").exists())
                self.assertNotEqual(ws.resolve(), src.resolve())
            (Path(a["workspace"]) / "code.txt").write_text("edited in run A\n")
            self.assertEqual((src / "code.txt").read_text(), "v1\n")  # the shared source is untouched
            self.assertEqual((Path(b["workspace"]) / "code.txt").read_text(), "v1\n")
            branches = subprocess.run(["git", "branch", "--list", "colab-run/*"], cwd=src, capture_output=True, text=True).stdout
            self.assertIn(a["run_id"], branches)
            self.assertIn(b["run_id"], branches)

    def test_non_git_source_is_copied_without_dot_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = make_source(root)
            (src / ".git").mkdir()
            (src / ".git" / "HEAD").write_text("x")
            r = start(root, src)
            self.assertEqual(r["mode"], "dir")
            self.assertTrue((Path(r["workspace"]) / "code.txt").exists())
            self.assertFalse((Path(r["workspace"]) / ".git").exists())

    def test_default_root_is_xdg_state_and_named_by_issue_and_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_STATE_HOME=tmp)
            env.pop("COLAB_RUN_ROOT", None)
            body = Path(tmp) / "i.md"
            body.write_text(ISSUE)
            proc = cli("start", "sensein/lab#7", "--offline", "--body-file", str(body), "--mode", "dir", "--comment-every", "0",
                       check=True, env=env)
            run_dir = Path(json.loads(proc.stdout)["run_dir"])
            self.assertEqual(run_dir.parent, (Path(tmp) / "lab-colab" / "runs").resolve())
            self.assertRegex(run_dir.name, r"^sensein-lab-7-\d{8}T\d{6}Z(-\d+)?$")

    def test_root_may_come_from_config_and_cli_wins(self) -> None:
        self.assertEqual(run.default_root({"run": {"root": "/x/y"}}, None), Path("/x/y"))
        self.assertEqual(run.default_root({"run": {"root": "/x/y"}}, "/z"), Path("/z"))

    def test_worktree_mode_without_a_repo_is_refused_and_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = make_source(root)
            res = start(root, src, "--mode", "worktree", expect=2)
            self.assertIn("git repository", res["stderr"])
            self.assertEqual(list((root / "runs").iterdir()), [])


class BudgetAndLoopTests(unittest.TestCase):
    def test_start_requires_explicit_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = ISSUE.replace("iterations=4, wall=2h, loop=30m, compute=2 GPU-hours, metric=bacc, direction=higher", "")
            res = start(root, make_source(root), body=body, expect=2)
            self.assertIn("budgets must be explicit", res["stderr"])

    def test_loop_logs_each_iteration_and_stops_on_the_iteration_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root, git=True), "--compute", "100")
            proc = cli("loop", "--run-dir", r["run_dir"])
            self.assertEqual(proc.returncode, 4, proc.stderr)
            run_dir = Path(r["run_dir"])
            rows = (run_dir / "results.tsv").read_text().splitlines()
            self.assertEqual(rows[0].split("\t"), run.RESULTS_HEADER)
            self.assertEqual(len(rows) - 1, 4)  # iterations=4 from the issue form
            self.assertEqual([x.split("\t")[0] for x in rows[1:]], ["1", "2", "3", "4"])
            self.assertEqual(json.loads((run_dir / "metadata.json").read_text())["status"], "budget_exhausted")
            self.assertIn("iterations budget reached", (run_dir / "log.md").read_text())
            self.assertTrue((run_dir / "summary.md").exists())
            status = json.loads(cli("status", "--run-dir", r["run_dir"], check=True).stdout)
            self.assertEqual(status["observations"], 4)
            self.assertEqual(status["best"]["iteration"], 4)

    def test_cli_iteration_override_beats_the_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--iterations", "2", "--compute", "100")
            cli("loop", "--run-dir", r["run_dir"])
            self.assertEqual(len((Path(r["run_dir"]) / "results.tsv").read_text().splitlines()), 3)

    def test_compute_budget_stops_the_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--iterations", "50", "--compute", "1.2")  # each step reports 0.5
            proc = cli("loop", "--run-dir", r["run_dir"])
            self.assertEqual(proc.returncode, 4)
            self.assertEqual(len(list((Path(r["run_dir"]) / "observations").glob("*.json"))), 3)
            self.assertIn("compute budget", (Path(r["run_dir"]) / "log.md").read_text())

    def test_wall_budget_stops_through_the_loop_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--wall", "10 minutes", "--now", "2026-04-14T10:00:00Z")
            proc = cli("step", "--run-dir", r["run_dir"], "--now", "2026-04-14T10:10:00Z")
            self.assertEqual(proc.returncode, 4)
            stop = json.loads(proc.stdout)["stop"]
            self.assertEqual(stop["category"], "budget")
            self.assertEqual(json.loads((Path(r["run_dir"]) / "metadata.json").read_text())["status"], "budget_exhausted")

    def test_repeated_crashes_break_on_correctness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--verify", "false", "--max-failures", "2", "--iterations", "20")
            proc = cli("loop", "--run-dir", r["run_dir"])
            self.assertEqual(proc.returncode, 4)
            rows = (Path(r["run_dir"]) / "results.tsv").read_text().splitlines()[1:]
            self.assertEqual([x.split("\t")[2] for x in rows], ["crash", "crash"])
            self.assertEqual(json.loads((Path(r["run_dir"]) / "metadata.json").read_text())["status"], "crashed")

    def test_patience_breaks_on_validity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--verify", "echo 0.5", "--iterations", "20", "--patience", "2",
                      "--direction", "higher")
            self.assertEqual(cli("loop", "--run-dir", r["run_dir"]).returncode, 4)
            self.assertEqual(len(list((Path(r["run_dir"]) / "observations").glob("*.json"))), 3)

    def test_finish_after_a_step_loop_records_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root))
            self.assertEqual(cli("step", "--run-dir", r["run_dir"]).returncode, 0)
            fin = cli("finish", "--run-dir", r["run_dir"], "--no-comment", check=True)
            self.assertEqual(json.loads(fin.stdout)["final_status"], "completed")
            again = cli("step", "--run-dir", r["run_dir"])
            self.assertEqual(again.returncode, 2)
            self.assertIn("not active", again.stderr)


class ObservationTests(unittest.TestCase):
    def test_observation_has_the_required_fields_and_relative_log_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = make_source(root, git=True)
            r = start(root, src, "--param", "lr=0.1", "--container-digest", "sha256:abc")
            out = json.loads(cli("step", "--run-dir", r["run_dir"], "--param", "seed=3", "--slurm-job", "4242", check=True).stdout)
            obs = json.loads((Path(r["run_dir"]) / out["observation"]).read_text())
            self.assertEqual(obs["schema"], "colab-observation/1")
            self.assertEqual(obs["parameters"], {"lr": 0.1, "seed": 3})
            self.assertRegex(obs["git_commit"], r"^[0-9a-f]{40}$")
            self.assertEqual(obs["container_digest"], "sha256:abc")
            self.assertEqual(obs["slurm_job_id"], "4242")
            self.assertEqual(obs["metric"], {"name": "bacc", "direction": "higher", "value": 0.6})
            self.assertEqual(obs["status"], "ok")
            self.assertIsInstance(obs["wall_seconds"], float)
            self.assertEqual(obs["issue"], "sensein/lab#7")
            for rel in obs["logs"].values():
                self.assertFalse(Path(rel).is_absolute())
                self.assertTrue((Path(r["run_dir"]) / rel).exists())
            self.assertIn("noise", (Path(r["run_dir"]) / obs["logs"]["stdout"]).read_text())
            self.assertTrue(run.verify_observation(Path(r["run_dir"]) / out["observation"]))

    def test_unknown_commit_and_digest_are_null_not_invented(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root))
            env = {k: v for k, v in os.environ.items() if k not in ("SLURM_JOB_ID", "COLAB_CONTAINER_DIGEST")}
            out = json.loads(cli("step", "--run-dir", r["run_dir"], check=True, env=env).stdout)
            obs = json.loads((Path(r["run_dir"]) / out["observation"]).read_text())
            self.assertIsNone(obs["git_commit"])
            self.assertIsNone(obs["container_digest"])
            self.assertIsNone(obs["slurm_job_id"])

    def test_observations_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root))
            out = json.loads(cli("step", "--run-dir", r["run_dir"], check=True).stdout)
            path = Path(r["run_dir"]) / out["observation"]
            self.assertEqual(path.stat().st_mode & 0o222, 0)  # read-only
            with self.assertRaises(FileExistsError):
                run.write_observation(Path(r["run_dir"]), json.loads(path.read_text()))
            os.chmod(path, 0o644)
            path.write_text(path.read_text().replace('"ok"', '"edited"'))
            self.assertFalse(run.verify_observation(path))

    def test_metric_value_records_a_measurement_taken_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--verify", "false")
            out = json.loads(cli("step", "--run-dir", r["run_dir"], "--metric-value", "0.91", check=True).stdout)
            self.assertEqual((out["status"], out["metric"]), ("ok", 0.91))

    def test_a_verify_without_a_metric_is_a_crash_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--verify", "echo hello")
            out = json.loads(cli("step", "--run-dir", r["run_dir"], check=True).stdout)
            self.assertEqual(out["status"], "crash")
            self.assertIsNone(out["metric"])

    def test_iterate_command_runs_in_the_workspace_before_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = make_source(root)
            (src / "bump.py").write_text("import os\nopen('marker.txt','a').write(os.environ['COLAB_ITERATION'])\n")
            r = start(root, src, "--iterate-cmd", "python3 bump.py", "--iterations", "2")
            cli("step", "--run-dir", r["run_dir"], check=True)
            self.assertEqual((Path(r["workspace"]) / "marker.txt").read_text(), "1")
            self.assertFalse((src / "marker.txt").exists())


class ConcurrencyAndAppendOnlyTests(unittest.TestCase):
    def test_parallel_steps_get_unique_iterations_and_clean_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--iterations", "50", "--compute", "100")
            with ThreadPoolExecutor(max_workers=6) as pool:
                procs = list(pool.map(lambda _: cli("step", "--run-dir", r["run_dir"]), range(6)))
            self.assertTrue(all(p.returncode == 0 for p in procs), [p.stderr for p in procs])
            run_dir = Path(r["run_dir"])
            rows = (run_dir / "results.tsv").read_text().splitlines()
            self.assertEqual(sorted(int(x.split("\t")[0]) for x in rows[1:]), [1, 2, 3, 4, 5, 6])
            for row in rows[1:]:
                self.assertEqual(row.count("\t"), len(run.RESULTS_HEADER) - 1)  # no interleaved or torn lines
            self.assertEqual(len(list((run_dir / "observations").glob("*.json"))), 6)
            self.assertEqual(len({p.name for p in (run_dir / "logs").iterdir()}), 12)
            self.assertFalse((run_dir / ".lock").exists())

    def test_parallel_steps_cannot_exceed_the_iteration_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--iterations", "3", "--compute", "100")
            with ThreadPoolExecutor(max_workers=6) as pool:
                procs = list(pool.map(lambda _: cli("step", "--run-dir", r["run_dir"]), range(6)))
            self.assertEqual(sorted(p.returncode for p in procs), [0, 0, 0, 4, 4, 4])
            self.assertEqual(len(list((Path(r["run_dir"]) / "observations").glob("*.json"))), 3)

    def test_parallel_starts_never_share_a_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = make_source(root, git=True)
            body = root / "issue.md"
            body.write_text(ISSUE)

            def go(_):
                return json.loads(cli("start", "sensein/lab#7", "--offline", "--body-file", str(body), "--root", str(root / "runs"),
                                      "--source", str(src), "--comment-every", "0", check=True).stdout)

            with ThreadPoolExecutor(max_workers=5) as pool:
                runs = list(pool.map(go, range(5)))
            self.assertEqual(len({x["run_dir"] for x in runs}), 5)
            self.assertEqual(len({x["workspace"] for x in runs}), 5)

    def test_results_and_log_are_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root), "--iterations", "9", "--compute", "100")
            run_dir = Path(r["run_dir"])
            before_rows = (run_dir / "results.tsv").read_text()
            before_log = (run_dir / "log.md").read_text()
            cli("step", "--run-dir", r["run_dir"], check=True)
            cli("step", "--run-dir", r["run_dir"], check=True)
            self.assertTrue((run_dir / "results.tsv").read_text().startswith(before_rows))
            self.assertTrue((run_dir / "log.md").read_text().startswith(before_log))
            first = (run_dir / "results.tsv").read_text().splitlines()[1]
            cli("step", "--run-dir", r["run_dir"], check=True)
            self.assertEqual((run_dir / "results.tsv").read_text().splitlines()[1], first)

    def test_append_line_is_a_single_whole_line_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.log"
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda i: run.append_line(path, f"line-{i}-" + "x" * 500), range(80)))
            lines = path.read_text().splitlines()
            self.assertEqual(len(lines), 80)
            self.assertTrue(all(len(x) == len("line-00-") + 500 or x.startswith("line-") for x in lines))

    def test_lock_excludes_and_breaks_a_dead_owners_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            with run.run_lock(d):
                with self.assertRaises(TimeoutError):
                    with run.run_lock(d, timeout_s=0.2):
                        pass
            self.assertFalse((d / ".lock").exists())
            (d / ".lock").mkdir()
            (d / ".lock" / "owner.json").write_text(json.dumps({"pid": 2**22 + 12345, "host": run.socket.gethostname(), "at": 0}))
            with run.run_lock(d, timeout_s=2):  # the owner is gone: the lock is broken, not waited on
                pass


class ProgressCommentTests(unittest.TestCase):
    def _args(self, run_dir: str) -> argparse.Namespace:
        return argparse.Namespace(run_dir=run_dir, param=None, metric_value=None, slurm_job=None, container_digest=None,
                                  step_timeout=0, in_job=False, now=None)

    def _config(self) -> tuple:
        import colab
        return colab, colab.load_config()

    def _step(self, run_dir, config, colab_mod) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return run.cmd_step(self._args(run_dir), config, colab_mod)

    def test_comment_cadence_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = root / "i.md"
            body.write_text(ISSUE)
            r = json.loads(cli("start", "sensein/lab#7", "--body-file", str(body), "--root", str(root / "runs"),
                               "--source", str(make_source(root)), "--mode", "dir", "--comment-every", "2", "--iterations", "9",
                               "--compute", "100", check=True).stdout)
            colab_mod, config = self._config()
            posted = []
            with mock.patch.object(run, "post_issue_comment", lambda ref, text: posted.append((ref, text)) or True):
                for _ in range(5):
                    self._step(r["run_dir"], config, colab_mod)
            self.assertEqual(len(posted), 2)  # after iterations 2 and 4
            ref, text = posted[0]
            self.assertEqual(ref, {"owner": "sensein", "repo": "lab", "n": 7})
            self.assertIn("iteration 2 of 9", text)
            self.assertIn("best bacc: 0.7", text)
            self.assertLess(len(text.splitlines()), 6)  # compact

    def test_comment_with_a_scan_finding_is_withheld(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = root / "i.md"
            body.write_text(ISSUE)
            leak = "/" + "orcd/scratch/orcd/999/someone/x"
            r = json.loads(cli("start", "sensein/lab#7", "--body-file", str(body), "--root", str(root / "runs"),
                               "--source", str(make_source(root)), "--mode", "dir", "--comment-every", "1",
                               "--metric", leak, check=True).stdout)
            colab_mod, config = self._config()
            posted = []
            with mock.patch.object(run, "post_issue_comment", lambda ref, text: posted.append(text) or True):
                self._step(r["run_dir"], config, colab_mod)
            self.assertEqual(posted, [])
            self.assertIn("withheld", (Path(r["run_dir"]) / "log.md").read_text())

    def test_no_comments_when_offline_or_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = start(root, make_source(root))  # --offline and --comment-every 0
            colab_mod, config = self._config()
            with mock.patch.object(run, "post_issue_comment", side_effect=AssertionError("no network")):
                self._step(r["run_dir"], config, colab_mod)


class RestrictedDataTests(unittest.TestCase):
    def _restricted_start(self, root: Path, src: Path) -> dict:
        marker = root / "ran.txt"
        (src / "touch.py").write_text(f"open({str(marker)!r}, 'a').write('x')\nprint(0.5)\n")
        return start(root, src, "--label", "tier:restricted", "--verify", "python3 touch.py", "--partition", "pi_x")

    def test_start_writes_a_job_script_and_runs_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = self._restricted_start(root, make_source(root))
            self.assertTrue(r["restricted"])
            script = Path(r["job_script"]).read_text()
            self.assertIn("loop --run-dir", script)
            self.assertIn("--in-job", script)
            self.assertIn("#SBATCH --partition=pi_x", script)
            self.assertIn("sbatch", r["next"])
            self.assertFalse((root / "ran.txt").exists())

    def test_session_cannot_step_or_loop_a_restricted_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = self._restricted_start(root, make_source(root))
            env = {k: v for k, v in os.environ.items() if k != "SLURM_JOB_ID"}
            for action in ("step", "loop"):
                for extra in ([], ["--in-job"]):  # the flag alone is not enough: there must be a Slurm job
                    proc = cli(action, "--run-dir", r["run_dir"], *extra, env=env)
                    self.assertEqual(proc.returncode, 2)
                    self.assertIn("ORCD job", proc.stderr)
            self.assertFalse((root / "ran.txt").exists())

    def test_inside_a_job_the_run_executes_and_records_the_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = self._restricted_start(root, make_source(root))
            env = dict(os.environ, SLURM_JOB_ID="777")
            proc = cli("step", "--run-dir", r["run_dir"], "--in-job", env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((root / "ran.txt").exists())
            out = json.loads(proc.stdout)
            obs = json.loads((Path(r["run_dir"]) / out["observation"]).read_text())
            self.assertEqual((obs["slurm_job_id"], obs["tier"]), ("777", "restricted"))
            self.assertNotIn("stdout", out)  # no log content is echoed back to the session

    def test_restricted_runs_never_post_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {"offline": False, "restricted": True, "comments": {"every_iterations": 1, "every_seconds": 0}}
            with mock.patch.object(run, "post_issue_comment", side_effect=AssertionError("posted")):
                run.maybe_comment(root, spec, 1, None, {}, run.utcnow())


class StageTests(unittest.TestCase):
    def _meta(self, root: Path) -> Path:
        meta = root / "meta"
        (meta / ".colab").mkdir(parents=True)
        return meta

    def _config(self):
        import colab
        return colab, colab.load_config()

    def _ran(self, root: Path, *extra: str) -> dict:
        r = start(root, make_source(root), "--iterations", "2", "--compute", "100", *extra)
        cli("loop", "--run-dir", r["run_dir"])
        return r

    def test_stage_copies_observations_under_colab_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r, meta = self._ran(root), self._meta(root)
            proc = cli("stage", "--run-dir", r["run_dir"], "--meta", str(meta))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            staged = sorted((meta / ".colab" / "staging" / "observations" / r["run_id"]).glob("*.json"))
            self.assertEqual(len(staged), 2)
            self.assertEqual(json.loads(staged[0].read_text())["@type"], "colab:Observation")
            again = cli("stage", "--run-dir", r["run_dir"], "--meta", str(meta))  # idempotent
            self.assertEqual(again.returncode, 0)

    def test_stage_refuses_a_scan_finding_and_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leak = "/" + "orcd/scratch/orcd/999/someone/x"
            r, meta = self._ran(root, "--param", f"data={leak}"), self._meta(root)
            proc = cli("stage", "--run-dir", r["run_dir"], "--meta", str(meta))
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(list((meta / ".colab" / "staging" / "observations" / r["run_id"]).glob("*.json")), [])

    def test_stage_refuses_a_tampered_observation_and_a_non_meta_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r, meta = self._ran(root), self._meta(root)
            obs = sorted((Path(r["run_dir"]) / "observations").glob("*.json"))[0]
            os.chmod(obs, 0o644)
            obs.write_text(obs.read_text().replace('"ok"', '"edited"'))
            self.assertEqual(cli("stage", "--run-dir", r["run_dir"], "--meta", str(meta)).returncode, 2)
            self.assertEqual(cli("stage", "--run-dir", r["run_dir"], "--meta", str(root)).returncode, 2)

    def test_open_pr_runs_branch_commit_push_and_pr_but_never_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r, meta = self._ran(root), self._meta(root)
            calls = []

            def fake(cmd, **kw):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            colab_mod, config = self._config()
            args = argparse.Namespace(run_dir=r["run_dir"], meta=str(meta), open_pr=True)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run.cmd_stage(args, config, colab_mod, runner=fake), 0)
            verbs = [c[3] if c[0] == "git" else c[1] for c in calls]
            self.assertEqual(verbs, ["switch", "add", "commit", "push", "pr"])
            self.assertFalse(any("merge" in " ".join(c) for c in calls))


class ColabPyDelegationTests(unittest.TestCase):
    def test_colab_py_run_delegates_to_the_engine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = root / "i.md"
            body.write_text(ISSUE)
            proc = subprocess.run([sys.executable, str(COLAB), "run", "start", "sensein/lab#7", "--offline", "--body-file", str(body),
                                   "--root", str(root / "runs"), "--source", str(make_source(root)), "--mode", "dir",
                                   "--comment-every", "0"], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["mode"], "dir")
            bad = subprocess.run([sys.executable, str(COLAB), "run", "status", "--run-dir", str(root / "nope")],
                                 capture_output=True, text=True)
            self.assertEqual(bad.returncode, 2)


if __name__ == "__main__":
    unittest.main()
