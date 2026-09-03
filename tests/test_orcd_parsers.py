"""Unit tests for the pure parsing and decision logic in skills/orcd-remote.

The cluster is never contacted: remote calls are stubbed with recorded-style
output, so these run anywhere and guard the parsers against the failure modes
found in review (mis-split node shapes, over-tolerant diffs, doubled per-user
paths, `*`-suffixed default partitions, regex config detection).
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "orcd-remote" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import orcd_common as oc  # noqa: E402
import orcd_doctor as doctor  # noqa: E402
import orcd_resources as resources  # noqa: E402
import orcd_snapshot as snapshot  # noqa: E402
import orcd_storage as storage  # noqa: E402
import orcd_submit as submit  # noqa: E402


class ResourcesParserTests(unittest.TestCase):
    def test_gpu_counts_handles_typed_untyped_and_index_suffixes(self) -> None:
        self.assertEqual(resources.gpu_counts("gpu:h100:4(S:0-1)"), {"h100": 4})
        self.assertEqual(resources.gpu_counts("gpu:h100:2(IDX:0-1)"), {"h100": 2})
        self.assertEqual(resources.gpu_counts("gpu:4"), {"untyped": 4})
        self.assertEqual(resources.gpu_counts("(null)"), {})

    def test_parse_idle_strips_default_partition_marker_and_counts_untyped(self) -> None:
        idle = resources.parse_idle([
            "mit_normal_gpu*|node1|mixed|gpu:h100:4|gpu:h100:1",
            "pi_x|node2|idle|gpu:4|gpu:0",
            "pi_x|node3|mixed-|gpu:4|gpu:0",      # PLANNED: earmarked, must not count
            "pi_x|node4|drained|gpu:4|gpu:0",     # cannot accept work
        ])
        self.assertEqual(idle, {"mit_normal_gpu": {"h100": 3}, "pi_x": {"untyped": 4}})

    def test_gputypes_from_tres_falls_back_to_untyped(self) -> None:
        parts = {
            "a": {"tres": "cpu=64,mem=500000M,node=2,gres/gpu=8,gres/gpu:h100=8"},
            "b": {"tres": "cpu=64,mem=500000M,node=1,gres/gpu=4"},
            "c": {"tres": "cpu=64,mem=500000M,node=1"},
        }
        self.assertEqual(resources.gputypes_from_tres(parts),
                         {"a": {"h100": 8}, "b": {"untyped": 4}})


SNAPSHOT_REMOTE = """@@META
user|satra
login_node|login009
slurm_version|slurm 25.05.1
@@CONFIG
ClusterName|eofe7
DefMemPerCPU|1000
MaxArraySize|25000
@@ASSOC
eofe7|satra_lab||normal|normal||500|1000|
@@PARTITIONS
pi_satra|orcd_rg_par_pi_satra|ALL|ALL|pi_satra|7-00:00:00|UNLIMITED|4|OFF|100|cpu=512,mem=4000000M,node=4,gres/gpu=16,gres/gpu:h100=16
mit_quicktest|ALL|ALL|ALL|mit_quicktest|00:15:00|UNLIMITED|30|OFF|90|cpu=3840,mem=30000000M,node=30
@@QOS
pi_satra|100|7-00:00:00|gres/gpu=8||gres/gpu=16||448||
mit_quicktest|90|00:15:00|||||10||
@@ACCESS
pi_satra|yes
mit_quicktest|yes
mit_normal_gpu|no
@@GROUPS
orcd_rg_hstor006_pi_satra
@@NODESHAPES
64|515000|gpu:h100:4|12
128|1031000|gpu:h200:8|3
64|257000|(null)|40
@@QUOTA
                               QUOTA REPORT
 Space   | Usage (GB) | Limit (GB) | % Used |  Files | Limit | % Used
---------+------------+------------+--------+--------+-------+--------
 HOME    |       71.0 |      200.0 |  35.48 | 277.2K |  1.0M |  27.72
 SCRATCH |      220.9 |     1024.0 |  21.57 |  73.0K |  1.0M |   7.30
@@HOMELINKS
scratch|/orcd/scratch/orcd/013/satra
pool|/orcd/pool/007/satra
@@STORAGE
/home|nfs001:/home
"""


class SnapshotTests(unittest.TestCase):
    def build(self) -> dict:
        with mock.patch.object(oc, "run_remote", return_value=SNAPSHOT_REMOTE):
            return snapshot.build_snapshot("orcd")

    def test_node_shapes_keep_the_whole_shape_and_the_count(self) -> None:
        shapes = self.build()["node_shapes"]
        self.assertEqual(shapes, {
            "64|515000|gpu:h100:4": 12,
            "128|1031000|gpu:h200:8": 3,
            "64|257000|(null)": 40,
        })

    def test_partitions_qos_access_and_quota_parse(self) -> None:
        snap = self.build()
        self.assertEqual(snap["partitions"]["pi_satra"]["gpus"], {"h100": 16})
        self.assertEqual(snap["qos"]["pi_satra"]["max_submit_pu"], "448")
        self.assertEqual(snap["partition_access"], {"pi_satra": True, "mit_quicktest": True,
                                                    "mit_normal_gpu": False})
        self.assertEqual([q["space"] for q in snap["quota"]], ["HOME", "SCRATCH"])
        self.assertEqual(snap["personal_spaces"]["pool"], "/orcd/pool/007/satra")

    def test_integer_changes_are_never_hidden_by_tolerance(self) -> None:
        for before, after in (("100", "101"), ("448", "452"), ("104", "105")):
            self.assertFalse(snapshot._same_value(before, after), f"{before}->{after}")

    def test_rendered_size_jitter_is_still_tolerated(self) -> None:
        self.assertTrue(snapshot._same_value("81.7B", "81.6B"))
        self.assertTrue(snapshot._same_value("1024.0", "1024.0"))
        self.assertFalse(snapshot._same_value("1.0M", "2.0M"))

    def test_diff_reports_node_count_and_gpu_changes(self) -> None:
        old = self.build()
        new = self.build()
        new["partitions"]["pi_satra"]["total_nodes"] = "5"
        new["partitions"]["pi_satra"]["gpus"]["h100"] = 20
        new["node_shapes"]["64|515000|gpu:h100:4"] = 11
        d = snapshot.diff_snapshots(old, new)
        changed = {k for k, _, _ in d["changed"]}
        self.assertIn("partitions.pi_satra.total_nodes", changed)
        self.assertIn("partitions.pi_satra.gpus.h100", changed)
        self.assertIn("node_shapes.64|515000|gpu:h100:4", changed)


STORAGE_REMOTE = """@@IDENTITY
user|satra
home|/home/satra
@@QUOTA
 Space   | Usage (GB) | Limit (GB) | % Used |  Files | Limit | % Used
---------+------------+------------+--------+--------+-------+--------
 HOME    |       71.0 |      200.0 |  35.48 | 277.2K |  1.0M |  27.72
 SCRATCH |      220.9 |      512.0 |  43.14 |  73.0K |  1.0M |   7.30
 POOL    |        0.0 |     1024.0 |   0.00 |      8 |  2.1B |   0.00
@@HOMELINKS
scratch|/home/satra/orcd/scratch|/orcd/scratch/orcd/013/satra
pool|/home/satra/orcd/pool|/orcd/pool/007/satra
@@GROUPS
orcd_rg_fstor001_ou_bcs
orcd_rg_hstor006_pi_satra
@@MOUNTS
/home|nfs|nfs001.cm.cluster:/home
/orcd/scratch/bcs/001|nfs|fstor001.cm.cluster:/bcs001
/orcd/scratch/orcd/013|nfs|fstor002.cm.cluster:/orcd013
@@AUTOFS
/orcd/data
@@PIDIRS
data|satra
"""


def storage_probe_output(script: str) -> str:
    """Answer the concurrent probe with one line per quoted path in the script."""
    paths = [p.strip('"') for p in script.split("for p in ", 1)[1].split(";", 1)[0].split()]
    lines = []
    for p in paths:
        if p.startswith("/orcd/data/satra/00"):
            lines.append(f"{p}|MISSING|||") if p.endswith("3") else lines.append(f"{p}|yes|500T|100T|80%|-")
        elif p == "/orcd/scratch/bcs/001":
            lines.append(f"{p}|yes|100T|40T|60%|NO_BACKUP")
        else:
            lines.append(f"{p}|yes|294T|100T|66%|-")
    return "\n".join(lines) + "\n"


class StorageTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, list[str]]:
        sent: list[str] = []

        def fake_run_remote(script, host="orcd", timeout=180, check=True):
            sent.append(script)
            if "@@IDENTITY" in script:
                return STORAGE_REMOTE
            if "probe()" in script:
                return storage_probe_output(script)
            return "\n".join(
                f"created|{line.split('=', 1)[1].split(';', 1)[0].strip(chr(34))}|drwxr-s---"
                for line in script.splitlines() if line.startswith('d=')
            )

        buf = io.StringIO()
        with mock.patch.object(oc, "run_remote", side_effect=fake_run_remote), \
                mock.patch.object(sys, "argv", ["orcd_storage.py", *argv]), \
                redirect_stdout(buf):
            rc = storage.main()
        return rc, buf.getvalue(), sent

    def test_pool_path_is_classified_capacity_before_it_is_mounted(self) -> None:
        self.assertEqual(storage.tier_for("", "/orcd/pool/007/satra")[0], "capacity")
        self.assertEqual(storage.tier_for("fstor001.cm.cluster:/x", "/anything")[0], "flash")

    def test_setup_never_doubles_the_username_and_locks_out_others(self) -> None:
        rc, out, sent = self.run_main(["--setup"])
        self.assertEqual(rc, 0)
        setup_scripts = [s for s in sent if "chmod o-rwx" in s]
        self.assertEqual(len(setup_scripts), 1, "exactly one setup script should be sent")
        script = setup_scripts[0]
        self.assertNotIn("/satra/satra", script)
        self.assertNotIn("/orcd/scratch/orcd/013", script, "personal scratch is ORCD-provisioned")
        self.assertIn('d="/orcd/scratch/bcs/001/satra"', script)

    def test_quota_label_uses_gigabytes_below_a_terabyte(self) -> None:
        _rc, out, _sent = self.run_main([])
        self.assertIn("291G of 512G quota", out)
        self.assertNotIn("0T quota", out)

    def test_probe_marks_stale_mounts_without_aborting(self) -> None:
        self.assertIn('timeout 6 test -e "$p"', storage.PROBE_TEMPLATE)
        self.assertIn('timeout 6 test -w "$p"', storage.PROBE_TEMPLATE)
        self.assertIn("STALE", storage.PROBE_TEMPLATE)


@unittest.skipIf(shutil.which("ssh") is None, "needs an ssh client for `ssh -G`")
class DoctorConfigTests(unittest.TestCase):
    def config(self, text: str) -> Path:
        p = Path(tempfile.mkdtemp()) / "config"
        p.write_text(text)
        return p

    def test_alias_is_detected_only_when_it_is_actually_defined(self) -> None:
        self.assertTrue(doctor.config_has_host("orcd", self.config(
            "Host orcd\n    HostName orcd-login.mit.edu\n")))
        self.assertFalse(doctor.config_has_host("orcd", self.config(
            "Host orcd-login.mit.edu\n    User satra\n")))
        self.assertFalse(doctor.config_has_host("orcd", self.config(
            "Host orcd-old\n    HostName elsewhere\n")))

    def test_batchmode_is_caught_even_from_a_global_block(self) -> None:
        cfg = self.config("Host *\n    BatchMode yes\nHost orcd\n    HostName orcd-login.mit.edu\n")
        self.assertTrue(doctor.config_has_batchmode("orcd", cfg))
        clean = self.config("Host orcd\n    HostName orcd-login.mit.edu\n")
        self.assertFalse(doctor.config_has_batchmode("orcd", clean))

    def test_fix_refuses_an_empty_username(self) -> None:
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["orcd_doctor.py", "--fix", "--user", ""]), \
                mock.patch("sys.stderr", err):
            self.assertEqual(doctor.main(), 1)
        self.assertIn("needs a username", err.getvalue())

    def test_explicit_identity_overrides_the_search(self) -> None:
        key = Path(tempfile.mkdtemp()) / "id_rsa_orcd"
        key.write_text("x")
        chosen, found = doctor.find_identity(str(key))
        self.assertEqual((chosen, found), (key, [key]))
        self.assertEqual(doctor.find_identity(str(key) + ".missing"), (None, []))


class CommonTransportTests(unittest.TestCase):
    def fake_proc(self, rc: int, out: str = "", err: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)

    def test_ssh_exit_255_is_fatal_even_when_check_is_false(self) -> None:
        with mock.patch.object(oc, "ssh_available", return_value=True), \
                mock.patch.object(oc.subprocess, "run",
                                  return_value=self.fake_proc(255, err="Connection closed")):
            with self.assertRaises(oc.OrcdError) as cm:
                oc.run_remote("true", check=False)
        self.assertIn("exit 255", str(cm.exception))

    def test_remote_error_text_survives_a_2_and_1_redirect(self) -> None:
        with mock.patch.object(oc, "ssh_available", return_value=True), \
                mock.patch.object(oc.subprocess, "run",
                                  return_value=self.fake_proc(1, out="sacct: fatal: Bad job/step specified")):
            with self.assertRaises(oc.OrcdError) as cm:
                oc.run_remote("sacct -j abc 2>&1")
        self.assertIn("Bad job/step specified", str(cm.exception))


class SubmitTests(unittest.TestCase):
    def args(self, **over) -> argparse.Namespace:
        base = dict(time="1:00:00", cpus=4, mem="16G", gpus=0, gpu_type=None, nodes=1,
                    name=None, array=None, output=None, chdir=None)
        base.update(over)
        return argparse.Namespace(**base)

    def test_chdir_becomes_sbatch_D_and_qos_is_never_emitted(self) -> None:
        flags = submit.build_flags(self.args(chdir="/orcd/scratch/bcs/001/satra", gpus=2,
                                             gpu_type="h100"), "pi_satra")
        self.assertIn("-D", flags)
        self.assertEqual(flags[flags.index("-D") + 1], "/orcd/scratch/bcs/001/satra")
        self.assertIn("gpu:h100:2", flags)
        self.assertNotIn("--qos", " ".join(flags))

    def test_ceiling_note_multiplies_gpus_by_nodes(self) -> None:
        ceilings = {"user": {"h100": 8}, "group": {"h100": 16}}
        self.assertEqual(submit.gpu_ceiling_note(self.args(gpus=4, nodes=2, gpu_type="h100"), ceilings), "")
        note = submit.gpu_ceiling_note(self.args(gpus=4, nodes=5, gpu_type="h100"), ceilings)
        self.assertTrue(note.startswith("EXCEEDS GROUP pool"))


if __name__ == "__main__":
    unittest.main()
