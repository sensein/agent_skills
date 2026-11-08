#!/usr/bin/env python3
"""Allocation guard: run at the top of every GPU job script.

    python3 /path/to/orcd_alloc_guard.py || exit 1

Verifies, from inside the job, that it is confined to the GPUs Slurm allocated:

1. the GPUs visible to ``nvidia-smi -L`` (and ``CUDA_VISIBLE_DEVICES``) number
   exactly the allocated count (``SLURM_GPUS_ON_NODE`` / ``SLURM_JOB_GPUS``);
2. no process outside this job is using a visible GPU;
3. where cgroup information exists, device confinement is in effect.

On any mismatch it prints what it saw and exits 1, so the job stops instead of
running on GPUs it was not granted. Stdlib only; no GPU needed to import it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

UUID_RE = re.compile(r"UUID:\s*((?:GPU|MIG)-[0-9a-fA-F-]+)")


def parse_smi_list(text: str) -> list[str]:
    """``nvidia-smi -L`` output -> one entry per device (its UUID when present)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if re.match(r"GPU\s+\d+:", line):
            m = UUID_RE.search(line)
            out.append(m.group(1) if m else line)
    return out


def parse_compute_apps(text: str) -> list[dict]:
    """``--query-compute-apps=gpu_uuid,pid,process_name --format=csv,noheader`` -> dicts."""
    apps = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",", 2)]
        if len(parts) >= 2 and parts[1].isdigit():
            apps.append({"gpu": parts[0], "pid": int(parts[1]), "name": parts[2] if len(parts) > 2 else ""})
    return apps


def parse_alloc(env: dict[str, str]) -> tuple[int | None, str]:
    """Allocated GPU count and where it came from, from the Slurm environment."""
    v = env.get("SLURM_GPUS_ON_NODE", "").strip()
    if v:
        m = re.search(r"(\d+)\s*$", v)  # "2" or "gpu:h100:2"
        if m:
            return int(m.group(1)), "SLURM_GPUS_ON_NODE"
    v = env.get("SLURM_JOB_GPUS", env.get("GPU_DEVICE_ORDINAL", "")).strip()
    if v:
        return len([x for x in v.split(",") if x.strip()]), "SLURM_JOB_GPUS"
    return None, ""


def parse_visible(env: dict[str, str]) -> int | None:
    """Device count named by ``CUDA_VISIBLE_DEVICES``; None when unset, 0 when empty/NoDevFiles."""
    v = env.get("CUDA_VISIBLE_DEVICES")
    if v is None:
        return None
    v = v.strip()
    if v in ("", "NoDevFiles", "-1"):
        return 0
    return len([x for x in v.split(",") if x.strip()])


def parse_cgroup_path(text: str) -> str:
    """Unified (v2) or devices-controller (v1) path from ``/proc/<pid>/cgroup``."""
    v1 = ""
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        if parts[0] == "0" and parts[1] == "":
            return parts[2]
        if "devices" in parts[1].split(","):
            v1 = parts[2]
    return v1


def devices_list_open(text: str) -> bool:
    """True when a cgroup v1 ``devices.list`` allows every device (``a *:* rwm``)."""
    return any(re.fullmatch(r"a\s+\*:\*\s+rwm", ln.strip()) for ln in text.splitlines())


def pid_in_job(cgroup_text: str, job_id: str) -> bool:
    """Whether a process's ``/proc/<pid>/cgroup`` places it in this Slurm job."""
    return bool(job_id) and re.search(rf"job_{re.escape(job_id)}(?!\d)", cgroup_text) is not None


def check(*, alloc: int | None, alloc_src: str, visible_env: int | None, smi: list[str] | None,
          apps: list[dict], foreign_pids: set[int], cgroup_path: str, job_id: str,
          devices_list: str | None) -> list[str]:
    """Return a list of problems; empty means the job sees only its allocation."""
    problems = []
    if not job_id:
        problems.append("SLURM_JOB_ID is not set: not running inside a Slurm job")
    if alloc is None:
        problems.append("no GPU allocation in the environment (SLURM_GPUS_ON_NODE / SLURM_JOB_GPUS unset)")
    if smi is not None and alloc is not None and len(smi) != alloc:
        problems.append(f"nvidia-smi -L shows {len(smi)} GPU(s) but {alloc_src} allocates {alloc}")
    if smi is None and alloc:
        problems.append(f"nvidia-smi unavailable but {alloc} GPU(s) allocated")
    if visible_env is not None and alloc is not None and visible_env != alloc:
        problems.append(f"CUDA_VISIBLE_DEVICES names {visible_env} device(s) but {alloc_src} allocates {alloc}")
    for a in apps:
        if a["pid"] in foreign_pids:
            problems.append(f"GPU {a['gpu']} is in use by pid {a['pid']} ({a['name']}), not part of this job")
    if job_id and cgroup_path and f"job_{job_id}" not in cgroup_path:
        problems.append(f"process cgroup {cgroup_path!r} does not contain job_{job_id}")
    if devices_list is not None and devices_list_open(devices_list):
        problems.append("cgroup devices.list allows all devices (a *:* rwm): GPU confinement is not enforced")
    return problems


def _run(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def main() -> int:
    env = dict(os.environ)
    job_id = env.get("SLURM_JOB_ID", "")
    alloc, src = parse_alloc(env)
    smi_text = _run(["nvidia-smi", "-L"])
    smi = parse_smi_list(smi_text) if smi_text is not None else None
    apps_text = _run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name", "--format=csv,noheader"])
    apps = parse_compute_apps(apps_text or "")
    foreign = set()
    for a in apps:
        cg = _read(f"/proc/{a['pid']}/cgroup")
        if cg is None or not pid_in_job(cg, job_id):
            foreign.add(a["pid"])
    cg_self = _read("/proc/self/cgroup") or ""
    cg_path = parse_cgroup_path(cg_self)
    devices = None
    m = re.search(r"^\d+:[^:]*devices[^:]*:(.*)$", cg_self, re.M)
    if m:
        devices = _read(f"/sys/fs/cgroup/devices{m.group(1)}/devices.list")
    problems = check(alloc=alloc, alloc_src=src, visible_env=parse_visible(env), smi=smi, apps=apps,
                     foreign_pids=foreign, cgroup_path=cg_path, job_id=job_id, devices_list=devices)
    node = env.get("SLURMD_NODENAME", os.uname().nodename)
    if problems:
        print(f"orcd_alloc_guard: FAIL on {node} job {job_id or '?'}", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(f"  saw: nvidia-smi -L = {smi}; CUDA_VISIBLE_DEVICES={env.get('CUDA_VISIBLE_DEVICES')!r}; "
              f"SLURM_GPUS_ON_NODE={env.get('SLURM_GPUS_ON_NODE')!r}; SLURM_JOB_GPUS={env.get('SLURM_JOB_GPUS')!r}; "
              f"cgroup={cg_path!r}", file=sys.stderr)
        print("orcd_alloc_guard: refusing to run outside the allocation", file=sys.stderr)
        return 1
    print(f"orcd_alloc_guard: ok on {node} job {job_id}: {alloc} GPU(s), confined")
    return 0


if __name__ == "__main__":
    sys.exit(main())
