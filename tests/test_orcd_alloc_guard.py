"""Tests for the ORCD allocation guard and node-health parsing (no GPU or cluster needed)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "orcd-remote" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import orcd_alloc_guard as guard  # noqa: E402
import orcd_submit as submit  # noqa: E402

SMI_2 = (
    "GPU 0: NVIDIA H100 80GB HBM3 (UUID: GPU-aaaa1111-2222-3333-4444-555566667777)\n"
    "GPU 1: NVIDIA H100 80GB HBM3 (UUID: GPU-bbbb1111-2222-3333-4444-555566667777)\n"
)


def run_check(**over) -> list[str]:
    base = dict(alloc=2, alloc_src="SLURM_GPUS_ON_NODE", visible_env=2, smi=guard.parse_smi_list(SMI_2),
                apps=[], foreign_pids=set(), cgroup_path="/system.slice/slurmstepd.scope/job_77/step_batch",
                job_id="77", devices_list=None)
    base.update(over)
    return guard.check(**base)


class GuardParsingTests(unittest.TestCase):
    def test_smi_list_counts_devices_and_extracts_uuids(self) -> None:
        self.assertEqual(len(guard.parse_smi_list(SMI_2)), 2)
        self.assertTrue(guard.parse_smi_list(SMI_2)[0].startswith("GPU-aaaa"))
        self.assertEqual(guard.parse_smi_list("No devices were found\n"), [])

    def test_compute_apps(self) -> None:
        apps = guard.parse_compute_apps("GPU-aaaa, 4242, python\nGPU-bbbb, 99, /usr/bin/x y\n")
        self.assertEqual(apps[0], {"gpu": "GPU-aaaa", "pid": 4242, "name": "python"})
        self.assertEqual(len(apps), 2)
        self.assertEqual(guard.parse_compute_apps(""), [])

    def test_alloc_sources(self) -> None:
        self.assertEqual(guard.parse_alloc({"SLURM_GPUS_ON_NODE": "2"}), (2, "SLURM_GPUS_ON_NODE"))
        self.assertEqual(guard.parse_alloc({"SLURM_GPUS_ON_NODE": "gpu:h100:4"})[0], 4)
        self.assertEqual(guard.parse_alloc({"SLURM_JOB_GPUS": "0,3"}), (2, "SLURM_JOB_GPUS"))
        self.assertEqual(guard.parse_alloc({}), (None, ""))

    def test_visible(self) -> None:
        self.assertEqual(guard.parse_visible({"CUDA_VISIBLE_DEVICES": "0,1"}), 2)
        self.assertEqual(guard.parse_visible({"CUDA_VISIBLE_DEVICES": "GPU-a,GPU-b,GPU-c"}), 3)
        self.assertEqual(guard.parse_visible({"CUDA_VISIBLE_DEVICES": "NoDevFiles"}), 0)
        self.assertIsNone(guard.parse_visible({}))

    def test_cgroup_path_v2_and_v1(self) -> None:
        self.assertEqual(guard.parse_cgroup_path("0::/a/job_5/step_0\n"), "/a/job_5/step_0")
        v1 = "12:devices:/slurm/uid_1/job_5/step_0\n1:name=systemd:/x\n"
        self.assertEqual(guard.parse_cgroup_path(v1), "/slurm/uid_1/job_5/step_0")

    def test_devices_list_open(self) -> None:
        self.assertTrue(guard.devices_list_open("a *:* rwm\n"))
        self.assertFalse(guard.devices_list_open("c 195:0 rwm\nc 195:255 rwm\n"))

    def test_pid_in_job_does_not_match_job_id_prefix(self) -> None:
        self.assertTrue(guard.pid_in_job("0::/s/job_77/step_0", "77"))
        self.assertFalse(guard.pid_in_job("0::/s/job_770/step_0", "77"))
        self.assertFalse(guard.pid_in_job("0::/user.slice/x", "77"))


class GuardCheckTests(unittest.TestCase):
    def test_clean_allocation_passes(self) -> None:
        self.assertEqual(run_check(), [])

    def test_sees_more_gpus_than_allocated(self) -> None:
        smi = guard.parse_smi_list(SMI_2 + "GPU 2: NVIDIA H100 (UUID: GPU-cccc-1)\nGPU 3: NVIDIA H100 (UUID: GPU-dddd-1)\n")
        problems = run_check(smi=smi)
        self.assertEqual(len(problems), 1)
        self.assertIn("shows 4 GPU(s) but SLURM_GPUS_ON_NODE allocates 2", problems[0])

    def test_visible_env_disagrees(self) -> None:
        self.assertIn("CUDA_VISIBLE_DEVICES names 1", run_check(visible_env=1)[0])

    def test_foreign_process_on_gpu(self) -> None:
        apps = guard.parse_compute_apps("GPU-aaaa, 4242, python\n")
        problems = run_check(apps=apps, foreign_pids={4242})
        self.assertIn("pid 4242", problems[0])
        self.assertEqual(run_check(apps=apps, foreign_pids=set()), [])

    def test_open_devices_list_and_wrong_cgroup(self) -> None:
        self.assertIn("not enforced", run_check(devices_list="a *:* rwm\n")[0])
        self.assertIn("job_77", run_check(cgroup_path="/user.slice/session-1.scope")[0])

    def test_no_slurm_or_no_allocation(self) -> None:
        problems = run_check(job_id="", alloc=None, alloc_src="", visible_env=None, cgroup_path="")
        self.assertEqual(len(problems), 2)

    def test_nvidia_smi_missing_with_gpus_allocated(self) -> None:
        self.assertIn("unavailable", run_check(smi=None)[0])


NODE_OK = ("NodeName=n1 Arch=x86_64 CPUAlloc=4 Gres=gpu:h100:4(S:0-1) GresUsed=gpu:h100:1(IDX:0) "
           "State=MIXED CfgTRES=cpu=64,mem=500G,gres/gpu=4,gres/gpu:h100=4 AllocTRES=cpu=4 Reason=(null)")


class NodeHealthTests(unittest.TestCase):
    def health(self, line: str) -> str:
        return submit.node_unhealthy(submit.parse_node_line(line))

    def test_healthy(self) -> None:
        self.assertEqual(self.health(NODE_OK), "")

    def test_bad_states(self) -> None:
        for st in ("DOWN*", "IDLE+DRAIN", "DRAINING", "DRAINED", "MIXED+FAIL", "IDLE+NOT_RESPONDING",
                   "DOWN+INVALID_REG", "MAINT"):
            self.assertIn("state", self.health(NODE_OK.replace("State=MIXED", f"State={st}")), st)

    def test_reason_attached_to_state(self) -> None:
        line = NODE_OK.replace("State=MIXED", "State=DRAINED").replace(
            "Reason=(null)", "Reason=gpu fell off bus [root@2026-11-01T10:00:00]")
        self.assertIn("gpu fell off bus", self.health(line))

    def test_gres_count_mismatch(self) -> None:
        line = NODE_OK.replace("Gres=gpu:h100:4", "Gres=gpu:h100:3")
        self.assertIn("GRES mismatch", self.health(line))

    def test_gres_used_exceeds_configured(self) -> None:
        line = NODE_OK.replace("GresUsed=gpu:h100:1", "GresUsed=gpu:h100:5")
        self.assertIn("exceeds", self.health(line))

    def test_gpu_reason_on_usable_node(self) -> None:
        line = NODE_OK.replace("Reason=(null)", "Reason=XID 79 reported [root@2026-11-01T10:00:00]")
        self.assertIn("reason", self.health(line))

    def test_cpu_node_without_gres_is_healthy(self) -> None:
        line = "NodeName=c1 Gres=(null) State=IDLE CfgTRES=cpu=64,mem=500G AllocTRES= Reason=(null)"
        self.assertEqual(self.health(line), "")

    def test_skipped_nodes_lists_reasons_per_partition(self) -> None:
        bad = submit.parse_node_line(NODE_OK.replace("NodeName=n1", "NodeName=n2").replace("State=MIXED", "State=DOWN*"))
        bad["unhealthy"] = submit.node_unhealthy(bad)
        good = submit.parse_node_line(NODE_OK)
        good["unhealthy"] = ""
        live = {"members": {"p": ["n1", "n2"]}, "nodes": {"n1": good, "n2": bad}}
        out = submit.skipped_nodes(live, ["p"])
        self.assertEqual([(r["node"], r["partition"]) for r in out], [("n2", "p")])
        self.assertIn("DOWN", out[0]["reason"])


if __name__ == "__main__":
    unittest.main()
