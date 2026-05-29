from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = REPO_ROOT / "skills" / "labnb" / "scripts" / "monitor_slice.py"
REGISTER_SCRIPT = REPO_ROOT / "skills" / "labnb" / "scripts" / "register_experiment.py"

_spec = importlib.util.spec_from_file_location("monitor_slice", MONITOR_PATH)
monitor = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = monitor
_spec.loader.exec_module(monitor)

NOW = datetime(2026, 4, 14, 10, 30, 0, tzinfo=timezone.utc)


def make_args(**overrides) -> argparse.Namespace:
    defaults = dict(
        reserve_seconds=0,
        warn_fraction=monitor.DEFAULT_WARN_FRACTION,
        no_pace=False,
        stall_seconds=0,
        usage_file="",
        max_rss_bytes=0,
        max_pmem=0.0,
        max_failures=0,
        patience=0,
        metric_guardrail=None,
        direction="",
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def state_with(remaining_loop: int, remaining_overall: int, loop_budget: int, overall_budget: int) -> dict:
    return {
        "status": "running",
        "loop_budget_seconds": loop_budget,
        "overall_budget_seconds": overall_budget,
        "remaining_loop_seconds": remaining_loop,
        "remaining_overall_seconds": remaining_overall,
        "slice_elapsed_seconds": loop_budget - remaining_loop,
    }


def evaluate(state, args, *, direction="", metric=None, pace=None, usage=None):
    return monitor.evaluate_signals(
        state=state,
        args=args,
        direction=direction,
        metric=metric or monitor.metric_diagnostics([], direction),
        pace=pace or {"last_row_age_seconds": None, "avg_iter_seconds": None},
        usage=usage or {"peak_rss": None, "peak_pmem": None, "peak_pcpu": None},
    )


class BudgetSignalTests(unittest.TestCase):
    def test_reserve_breaks_before_hard_zero(self) -> None:
        state = state_with(remaining_loop=60, remaining_overall=600, loop_budget=300, overall_budget=1800)
        signals = evaluate(state, make_args(reserve_seconds=90))
        decision, primary = monitor.decide(signals)
        self.assertEqual(decision, "break")
        self.assertEqual(primary.category, "budget")

    def test_warn_fraction_emits_advisory_only(self) -> None:
        state = state_with(remaining_loop=50, remaining_overall=900, loop_budget=300, overall_budget=1800)
        signals = evaluate(state, make_args(warn_fraction=0.8))
        decision, primary = monitor.decide(signals)
        self.assertEqual(decision, "warn")
        self.assertIsNone(primary)

    def test_within_budget_continues(self) -> None:
        state = state_with(remaining_loop=200, remaining_overall=900, loop_budget=300, overall_budget=1800)
        signals = evaluate(state, make_args())
        self.assertEqual(monitor.decide(signals)[0], "continue")


class EngineeringSignalTests(unittest.TestCase):
    def test_pace_breaks_when_next_iteration_will_not_fit(self) -> None:
        state = state_with(remaining_loop=40, remaining_overall=900, loop_budget=300, overall_budget=1800)
        pace = {"last_row_age_seconds": 5, "avg_iter_seconds": 90}
        signals = evaluate(state, make_args(), pace=pace)
        decision, primary = monitor.decide(signals)
        self.assertEqual(decision, "break")
        self.assertEqual(primary.reason, "pace_insufficient_budget")

    def test_no_pace_flag_disables_projection(self) -> None:
        state = state_with(remaining_loop=40, remaining_overall=900, loop_budget=300, overall_budget=1800)
        pace = {"last_row_age_seconds": 5, "avg_iter_seconds": 90}
        signals = evaluate(state, make_args(no_pace=True), pace=pace)
        self.assertNotIn("engineering", {s.category for s in signals})

    def test_stall_breaks_on_old_last_row(self) -> None:
        state = state_with(remaining_loop=200, remaining_overall=900, loop_budget=300, overall_budget=1800)
        pace = {"last_row_age_seconds": 400, "avg_iter_seconds": 20}
        signals = evaluate(state, make_args(stall_seconds=120), pace=pace)
        decision, primary = monitor.decide(signals)
        self.assertEqual(decision, "break")
        self.assertEqual(primary.reason, "stalled")

    def test_resource_rss_ceiling(self) -> None:
        state = state_with(remaining_loop=200, remaining_overall=900, loop_budget=300, overall_budget=1800)
        usage = {"peak_rss": 2_000_000_000, "peak_pmem": 5.0, "peak_pcpu": 50.0}
        signals = evaluate(state, make_args(max_rss_bytes=1_000_000_000), usage=usage)
        decision, primary = monitor.decide(signals)
        self.assertEqual(decision, "break")
        self.assertEqual(primary.reason, "resource_rss")


class CorrectnessAndValidityTests(unittest.TestCase):
    def test_repeated_failures_break_correctness(self) -> None:
        rows = [
            {"iteration": "1", "timestamp_utc": "", "status": "failed", "metric": None},
            {"iteration": "2", "timestamp_utc": "", "status": "crashed", "metric": None},
        ]
        diag = monitor.metric_diagnostics(rows, "higher")
        self.assertEqual(diag["consecutive_failures"], 2)
        state = state_with(200, 900, 300, 1800)
        signals = evaluate(state, make_args(max_failures=2), direction="higher", metric=diag)
        decision, primary = monitor.decide(signals)
        self.assertEqual(decision, "break")
        self.assertEqual(primary.category, "correctness")

    def test_no_improvement_breaks_validity(self) -> None:
        rows = [
            {"iteration": "0", "timestamp_utc": "", "status": "baseline", "metric": 0.80},
            {"iteration": "1", "timestamp_utc": "", "status": "keep", "metric": 0.79},
            {"iteration": "2", "timestamp_utc": "", "status": "keep", "metric": 0.78},
            {"iteration": "3", "timestamp_utc": "", "status": "keep", "metric": 0.80},
        ]
        diag = monitor.metric_diagnostics(rows, "higher")
        self.assertEqual(diag["iterations_since_improvement"], 3)
        state = state_with(200, 900, 300, 1800)
        signals = evaluate(state, make_args(patience=3), direction="higher", metric=diag)
        decision, primary = monitor.decide(signals)
        self.assertEqual(decision, "break")
        self.assertEqual(primary.reason, "no_improvement")

    def test_guardrail_violation_breaks_validity(self) -> None:
        rows = [{"iteration": "1", "timestamp_utc": "", "status": "keep", "metric": 0.40}]
        diag = monitor.metric_diagnostics(rows, "higher")
        state = state_with(200, 900, 300, 1800)
        signals = evaluate(state, make_args(metric_guardrail=0.5), direction="higher", metric=diag)
        decision, primary = monitor.decide(signals)
        self.assertEqual(decision, "break")
        self.assertEqual(primary.reason, "guardrail_violation")

    def test_improvement_resets_no_improvement_counter(self) -> None:
        rows = [
            {"iteration": "0", "timestamp_utc": "", "status": "baseline", "metric": 0.5},
            {"iteration": "1", "timestamp_utc": "", "status": "keep", "metric": 0.4},
            {"iteration": "2", "timestamp_utc": "", "status": "keep", "metric": 0.9},
        ]
        diag = monitor.metric_diagnostics(rows, "higher")
        self.assertEqual(diag["best_metric"], 0.9)
        self.assertEqual(diag["iterations_since_improvement"], 0)


class SliceScopingTests(unittest.TestCase):
    def test_prior_slice_failures_not_counted(self) -> None:
        slice_started = datetime(2026, 4, 14, 10, 20, 0, tzinfo=timezone.utc)
        rows = [
            {"iteration": "1", "timestamp_utc": "2026-04-14T10:00:00Z", "ts": monitor.parse_iso("2026-04-14T10:00:00Z"), "status": "failed", "metric": None},
            {"iteration": "2", "timestamp_utc": "2026-04-14T10:05:00Z", "ts": monitor.parse_iso("2026-04-14T10:05:00Z"), "status": "crashed", "metric": None},
        ]
        # Cumulative (legacy) sees both failures...
        self.assertEqual(monitor.metric_diagnostics(rows, "higher")["consecutive_failures"], 2)
        # ...but a slice that starts after them counts none.
        scoped = monitor.metric_diagnostics(rows, "higher", slice_started)
        self.assertEqual(scoped["consecutive_failures"], 0)
        self.assertEqual(scoped["slice_iterations"], 0)

    def test_stall_age_measured_from_slice_start_when_no_in_slice_rows(self) -> None:
        slice_started = datetime(2026, 4, 14, 10, 20, 0, tzinfo=timezone.utc)
        rows = [
            {"iteration": "1", "timestamp_utc": "2026-04-14T10:00:00Z", "ts": monitor.parse_iso("2026-04-14T10:00:00Z"), "status": "keep", "metric": 0.1},
        ]
        pace = monitor.pace_diagnostics(rows, NOW, slice_started)
        # NOW is 10:30; age is measured from slice start (10:20), not the old row.
        self.assertEqual(pace["last_row_age_seconds"], 600)
        self.assertIsNone(pace["avg_iter_seconds"])

    def test_pace_uses_slice_start_as_t0(self) -> None:
        slice_started = datetime(2026, 4, 14, 10, 20, 0, tzinfo=timezone.utc)
        rows = [
            {"iteration": "1", "timestamp_utc": "2026-04-14T10:22:00Z", "ts": monitor.parse_iso("2026-04-14T10:22:00Z"), "status": "keep", "metric": 0.1},
        ]
        pace = monitor.pace_diagnostics(rows, NOW, slice_started)
        self.assertEqual(pace["avg_iter_seconds"], 120)


class PriorityTests(unittest.TestCase):
    def test_correctness_outranks_validity(self) -> None:
        rows = [
            {"iteration": "1", "timestamp_utc": "", "status": "failed", "metric": 0.1},
            {"iteration": "2", "timestamp_utc": "", "status": "failed", "metric": 0.1},
        ]
        diag = monitor.metric_diagnostics(rows, "higher")
        state = state_with(200, 900, 300, 1800)
        signals = evaluate(
            state,
            make_args(max_failures=2, patience=1, metric_guardrail=0.5),
            direction="higher",
            metric=diag,
        )
        decision, primary = monitor.decide(signals)
        self.assertEqual(decision, "break")
        self.assertEqual(primary.category, "correctness")


class PaceDiagnosticTests(unittest.TestCase):
    def test_pace_estimates_recent_gap_and_age(self) -> None:
        rows = [
            {"iteration": "1", "timestamp_utc": "2026-04-14T10:00:00Z", "status": "keep", "metric": 0.1},
            {"iteration": "2", "timestamp_utc": "2026-04-14T10:02:00Z", "status": "keep", "metric": 0.2},
        ]
        pace = monitor.pace_diagnostics(rows, NOW)
        self.assertEqual(pace["avg_iter_seconds"], 120)
        self.assertEqual(pace["last_row_age_seconds"], 28 * 60)


class UsageParsingTests(unittest.TestCase):
    def test_reads_info_json_execution_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "run_info.json"
            path.write_text(json.dumps({"execution_summary": {"peak_rss": 123, "peak_pmem": 4.5}}))
            peak = monitor.extract_peak_usage(path)
            self.assertEqual(peak["peak_rss"], 123)
            self.assertEqual(peak["peak_pmem"], 4.5)


class ExitCodeIntegrationTests(unittest.TestCase):
    def _register(self, lab_root: Path, project_root: Path) -> Path:
        result = subprocess.run(
            [
                sys.executable, str(REGISTER_SCRIPT),
                "--lab-root", str(lab_root),
                "--project-root", str(project_root),
                "--project-slug", "demo", "--experiment-slug", "exp",
                "--objective", "obj", "--entry-kind", "experiment",
                "--direction", "higher",
                "--overall-budget", "2 hours", "--loop-budget", "10 minutes",
            ],
            check=True, capture_output=True, text=True,
        )
        return Path(result.stdout.strip())

    def _monitor(self, *extra, check=False):
        return subprocess.run(
            [sys.executable, str(MONITOR_PATH), *extra],
            check=check, capture_output=True, text=True,
        )

    def test_stall_break_returns_nonzero_and_exit_zero_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            exp_dir = self._register(temp_path / "lab", temp_path / "project")
            self._monitor("start", "--experiment-dir", str(exp_dir),
                          "--now", "2026-04-14T10:00:00Z", check=True)
            # Log a stale baseline row, then check well after the stall window.
            results = exp_dir / "results.tsv"
            results.write_text(
                results.read_text()
                + "\t".join(["0", "2026-04-14T10:00:30Z", "baseline", "0.5", "30", "30", "", ""]) + "\n"
            )
            broke = self._monitor(
                "check", "--experiment-dir", str(exp_dir),
                "--stall-seconds", "120", "--now", "2026-04-14T10:05:00Z",
            )
            self.assertEqual(broke.returncode, 4)
            state = json.loads(broke.stdout)
            self.assertEqual(state["decision"], "break")
            self.assertEqual(state["break_category"], "engineering")
            metadata = json.loads((exp_dir / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "stopped")

            forced = self._monitor(
                "check", "--experiment-dir", str(exp_dir),
                "--stall-seconds", "120", "--exit-zero", "--now", "2026-04-14T10:06:00Z",
            )
            self.assertEqual(forced.returncode, 0)

    def test_continue_check_preserves_non_running_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            exp_dir = self._register(temp_path / "lab", temp_path / "project")
            self._monitor("start", "--experiment-dir", str(exp_dir),
                          "--now", "2026-04-14T10:00:00Z", check=True)
            self._monitor("finish", "--experiment-dir", str(exp_dir),
                          "--final-status", "completed", "--now", "2026-04-14T10:03:00Z", check=True)
            # A check on a finished slice must not flip status back to running.
            checked = self._monitor(
                "check", "--experiment-dir", str(exp_dir), "--now", "2026-04-14T10:04:00Z",
            )
            self.assertEqual(checked.returncode, 0)
            state = json.loads(checked.stdout)
            self.assertEqual(state["decision"], "continue")
            self.assertEqual(state["status"], "completed")
            metadata = json.loads((exp_dir / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "completed")


if __name__ == "__main__":
    unittest.main()
