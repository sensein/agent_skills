"""Tests for the claude-rc chain's run planning (no cluster needed: scontrol is stubbed)."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

RC = Path(__file__).resolve().parents[1] / "skills" / "orcd-remote" / "scripts" / "claude_rc"
SBATCH = RC / "claude-rc.sbatch"
SUBMIT = RC / "claude-rc-submit.sh"


def epoch(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


@unittest.skipUnless(shutil.which("bash") and shutil.which("date"), "needs bash and GNU date")
class ClaudeRcPlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bin_ = Path(self.tmp.name) / "bin"
        bin_.mkdir()
        self.reservation = Path(self.tmp.name) / "reservation"
        self.reservation.write_text("")
        stub = bin_ / "scontrol"   # `scontrol show reservation -o` prints the file's content
        stub.write_text(f"#!/bin/sh\ncat '{self.reservation}'\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "TZ": "UTC",
                    "RC_DIR": self.tmp.name, "RC_WORKDIR": self.tmp.name}

    def run_rc(self, *args, now, deadline, max_run=172800):
        env = {**self.env, "RC_NOW": str(now), "RC_DEADLINE": str(deadline), "RC_MAX_RUN": str(max_run)}
        out = subprocess.run(["bash", str(SBATCH), *args], env=env, capture_output=True, text=True, check=True)
        return out.stdout.split()

    def test_syntax(self):
        for f in (SBATCH, SUBMIT):
            subprocess.run(["bash", "-n", str(f)], check=True)

    def test_limit_capped_by_max_run(self):
        now = epoch("2026-12-01 00:00")   # third Tuesday of Dec 2026 is the 15th
        self.assertEqual(self.run_rc("limit", now=now, deadline=now + 30 * 86400), [str(172800 // 60)])

    def test_limit_capped_by_deadline(self):
        now = epoch("2026-12-01 00:00")
        self.assertEqual(self.run_rc("limit", now=now, deadline=now + 3600), ["60"])

    def test_limit_stops_before_default_maintenance_day(self):
        now = epoch("2026-12-14 00:00")
        ms = epoch("2026-12-15 00:00")
        self.assertEqual(self.run_rc("limit", now=now, deadline=now + 30 * 86400), [str((ms - 300 - now) // 60)])

    def test_limit_uses_scheduled_maint_reservation(self):
        self.reservation.write_text(
            "ReservationName=maint StartTime=2026-12-02T06:00:00 EndTime=2026-12-02T18:00:00 "
            "Duration=12:00:00 Nodes=ALL Flags=MAINT,IGNORE_JOBS,ALL_NODES\n")
        now = epoch("2026-12-01 00:00")
        ms = epoch("2026-12-02 06:00")
        self.assertEqual(self.run_rc("limit", now=now, deadline=now + 30 * 86400), [str((ms - 300 - now) // 60)])

    def test_plan_successor_starts_two_hours_early(self):
        now = epoch("2026-12-01 00:00")
        end = epoch("2026-12-03 00:00")
        begin, mins = self.run_rc("plan", str(end), now=now, deadline=now + 30 * 86400)
        self.assertEqual(int(begin), end - 7200)
        self.assertEqual(int(mins), 172800 // 60)

    def test_plan_successor_waits_out_maintenance(self):
        now = epoch("2026-12-14 00:00")
        end = epoch("2026-12-14 23:30")   # ends within an hour of the maintenance day
        begin, _ = self.run_rc("plan", str(end), now=now, deadline=now + 30 * 86400)
        self.assertEqual(int(begin), epoch("2026-12-16 00:00"))

    def test_submit_without_args_prints_usage(self):
        out = subprocess.run(["bash", str(SUBMIT)], env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("claude-rc-submit.sh check", out.stdout)

    def _stub(self, name, body):
        f = Path(self.tmp.name) / "bin" / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    def _start_env(self):
        home = Path(self.tmp.name) / "home"
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        (home / ".claude" / ".credentials.json").write_text("{}")
        self._stub("sbatch", '[ "$1" = "--test-only" ] && exit 0; echo SBATCH "$@"')
        self._stub("squeue", "exit 0")
        self._stub("claude", "echo 2.0.0")
        self._stub("scontrol", 'case "$*" in *reservation*) exit 0 ;; *) echo "PartitionName=$3 MaxTime=$MAXTIME" ;; esac')
        env = {**self.env, "HOME": str(home), "RC_DIR": str(Path(self.tmp.name) / "state"), "MAXTIME": "2-00:00:00"}
        env.pop("ANTHROPIC_API_KEY", None)
        return env, home

    def submit(self, env, *args, cwd=None):
        return subprocess.run(["bash", str(SUBMIT), *args], env=env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, cwd=cwd)

    def test_status_with_no_logs_succeeds(self):
        self._stub("squeue", "exit 0")
        out = self.submit(self.env, "status")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_start_refuses_home_missing_and_comma_folders(self):
        env, home = self._start_env()
        bad = Path(self.tmp.name) / "a,b"
        bad.mkdir()
        cases = ((str(home), "home folder"), ("/", "home folder"), ("/nonexistent", "No such folder"),
                 ("/nonexistent/sub", "No such folder"), (str(bad), "commas"))
        for folder, msg in cases:
            out = self.submit(env, "start", "-p", "x", "-w", folder)
            self.assertEqual(out.returncode, 2, folder)
            self.assertIn(msg, out.stdout)

    def test_start_refuses_short_partition(self):
        env, _ = self._start_env()
        env["MAXTIME"] = "04:00:00"
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        out = self.submit(env, "start", "-p", "short", "-w", str(work))
        self.assertEqual(out.returncode, 2)
        self.assertIn("at least 12 hours", out.stdout)

    def test_start_passes_absolute_paths_to_the_chain(self):
        env, _ = self._start_env()
        work = Path(self.tmp.name) / "work"
        work.mkdir()
        out = self.submit(env, "start", "-p", "long", "-w", "work", "-d", "+3 days", cwd=self.tmp.name)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(f"RC_WORKDIR={work.resolve()}", out.stdout)
        self.assertIn("-p long -t 2880 -c 2 --mem=16G", out.stdout)


if __name__ == "__main__":
    unittest.main()
