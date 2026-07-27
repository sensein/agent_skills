#!/usr/bin/env python3
"""Submit work to ORCD, choosing the partition by asking the scheduler.

The useful trick here is that ``sbatch --test-only`` reports *when* a request
would start without queueing anything. Running it against every partition the
user can reach turns partition choice into a measurement instead of a guess --
which matters on ORCD, where an idle private partition and a six-hour queue on
the shared GPU partition are both one flag apart.

    # See where a request would land, and how soon, in each partition
    python3 orcd_submit.py --plan --gpus 1 --gpu-type h100 --cpus 8 --mem 64G --time 2:00:00

    # Submit a script, auto-selecting the soonest-starting partition
    python3 orcd_submit.py --script train.sh --gpus 1 --cpus 8 --mem 64G --time 4:00:00

    # Pin the partition yourself
    python3 orcd_submit.py --script train.sh --partition pi_satra --gpus 1

    # Check on things
    python3 orcd_submit.py --queue
    python3 orcd_submit.py --status 12345678
"""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path

import orcd_common as oc


def usable_partitions(host: str) -> list[str]:
    """Partitions this user may submit to, per the scheduler itself."""
    script = r'''
set +e
for P in $(scontrol show partition -o 2>/dev/null | sed -E 's/^PartitionName=([^ ]+).*/\1/'); do
  sbatch --test-only -p "$P" -t 5 -n 1 --mem=1G --wrap=true >/dev/null 2>&1 && echo "$P"
done
'''
    return [l.strip() for l in oc.run_remote(script, host=host, timeout=180).splitlines() if l.strip()]


def build_flags(args: argparse.Namespace, partition: str) -> list[str]:
    """Assemble sbatch flags.

    Deliberately never emits --qos: on ORCD each partition attaches its own QOS
    automatically, and most users' associations do not permit naming those QOS
    explicitly, so passing one turns a working request into
    "Invalid qos specification".
    """
    flags = ["-p", partition, "-t", args.time, "-c", str(args.cpus), "--mem", args.mem]
    if args.gpus:
        gres = f"gpu:{args.gpu_type}:{args.gpus}" if args.gpu_type else f"gpu:{args.gpus}"
        flags += ["--gres", gres]
    if args.nodes != 1:
        flags += ["-N", str(args.nodes)]
    if args.name:
        flags += ["-J", args.name]
    if args.array:
        flags += ["-a", args.array]
    if args.output:
        flags += ["-o", args.output]
    return flags


def qos_gpu_ceilings(args: argparse.Namespace) -> dict[str, dict[str, dict[str, int]]]:
    """Map partition -> {'user': {...}, 'group': {...}} GPU ceilings from its QOS.

    ``sbatch --test-only`` validates scheduling feasibility but does NOT check
    QOS TRES ceilings -- it happily reports an immediate start for a request the
    GrpTRES group pool can never admit. Cross-checking here is what turns the
    plan from plausible into honest.
    """
    script = r'''
set +e
echo "@@QOSMAP"
scontrol show partition -o 2>/dev/null | while read -r line; do
  name=$(echo "$line" | sed -E 's/^PartitionName=([^ ]+).*/\1/')
  qos=$(echo "$line" | grep -oE '(^| )QoS=[^ ]*' | head -1 | cut -d= -f2)
  printf "%s|%s\n" "$name" "$qos"
done
echo "@@QOS"
sacctmgr -nP show qos format=Name,MaxTRESPU,GrpTRES 2>/dev/null
'''
    out = oc.run_remote(script, host=args.host, timeout=60, check=False)
    blocks = oc.parse_kv_blocks(out)
    qos_of = {}
    for line in blocks.get("QOSMAP", []):
        f = line.split("|")
        if len(f) == 2 and f[0].strip():
            qos_of[f[0].strip()] = f[1].strip()

    def gpus_in(tres: str) -> dict[str, int]:
        found = {m.group(1): int(m.group(2))
                 for m in re.finditer(r"gres/gpu:([a-z0-9_]+)=(\d+)", tres)}
        bare = re.search(r"gres/gpu=(\d+)", tres)
        if bare:
            found["_any"] = int(bare.group(1))
        return found

    limits = {}
    for line in blocks.get("QOS", []):
        f = line.split("|")
        if len(f) >= 3 and f[0].strip():
            limits[f[0].strip()] = {"user": gpus_in(f[1]), "group": gpus_in(f[2])}
    return {p: limits.get(q, {"user": {}, "group": {}}) for p, q in qos_of.items()}


def gpu_ceiling_note(args: argparse.Namespace, ceilings: dict[str, dict[str, int]]) -> str:
    """One-line warning when the request exceeds a QOS GPU ceiling."""
    total = args.gpus * max(args.nodes, 1)
    for scope, label in (("group", "GROUP pool (GrpTRES"), ("user", "your per-user cap (MaxTRESPU")):
        lim = ceilings.get(scope, {})
        for key in ((args.gpu_type,) if args.gpu_type else ()) + ("_any",):
            cap = lim.get(key)
            if cap is not None and total > cap:
                shown = "gpu" if key == "_any" else f"gpu:{key}"
                return f"EXCEEDS {label} {shown}={cap}) -- would queue forever"
    return ""


def plan(args: argparse.Namespace, partitions: list[str]) -> list[list[str]]:
    """Ask the scheduler what each partition would do with this exact request."""
    lines = ["set +e"]
    for p in partitions:
        flags = " ".join(shlex.quote(f) for f in build_flags(args, p))
        lines.append(
            f'out=$(sbatch --test-only {flags} --wrap=true 2>&1); '
            f'if [ $? -eq 0 ]; then echo "{p}|OK|$out"; else echo "{p}|NO|$(echo "$out" | head -1 | sed "s/.*error: //")"; fi'
        )
    out = oc.run_remote("\n".join(lines), host=args.host, timeout=240, check=False)
    ceilings = qos_gpu_ceilings(args) if args.gpus else {}

    rows = []
    for line in out.splitlines():
        f = line.split("|", 2)
        if len(f) < 3:
            continue
        part, ok, detail = f[0].strip(), f[1].strip(), f[2].strip()
        if ok == "OK":
            when = re.search(r"\d{4}-\d{2}-\d{2}T[\d:]+", detail)
            node = re.search(r"on nodes? (\S+)", detail)
            note = gpu_ceiling_note(args, ceilings.get(part, {})) if args.gpus else ""
            rows.append([part, "yes", when.group(0) if when else "?", node.group(1) if node else "?", note])
        else:
            rows.append([part, "no", "-", "-", detail[:58]])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=oc.DEFAULT_HOST)
    ap.add_argument("--script", help="local path to a job script to copy up and submit")
    ap.add_argument("--remote-script", help="path to a script that already exists on the cluster")
    ap.add_argument("--wrap", help="a single command to run, instead of a script")
    ap.add_argument("--partition", help="pin a partition instead of auto-selecting")
    ap.add_argument("--time", default="1:00:00", help="walltime (default %(default)s)")
    ap.add_argument("--cpus", type=int, default=4, help="CPUs per task (default %(default)s)")
    ap.add_argument("--mem", default="16G", help="memory (default %(default)s)")
    ap.add_argument("--gpus", type=int, default=0, help="GPUs to request")
    ap.add_argument("--gpu-type", help="GPU model, e.g. h100, h200, a100, l40s")
    ap.add_argument("--nodes", type=int, default=1)
    ap.add_argument("--name", help="job name")
    ap.add_argument("--array", help="array spec, e.g. 0-99")
    ap.add_argument("--output", help="stdout path pattern, e.g. logs/%%x-%%j.out")
    ap.add_argument("--plan", action="store_true", help="only show where this would land, submit nothing")
    ap.add_argument("--queue", action="store_true", help="show your queue")
    ap.add_argument("--status", help="show details for a job id")
    args = ap.parse_args()

    try:
        if args.queue:
            oc.heading("Your jobs")
            out = oc.run_remote(
                'squeue -u "$USER" -o "%.12i %.22j %.16P %.10T %.11M %.11l %.6D %R" 2>&1',
                host=args.host, timeout=60,
            )
            print(out.rstrip() or "(no jobs)")
            return 0

        if args.status:
            oc.heading(f"Job {args.status}")
            out = oc.run_remote(
                f'scontrol show job {shlex.quote(args.status)} 2>&1 || '
                f'sacct -j {shlex.quote(args.status)} '
                f'--format=JobID,JobName%22,Partition,State,Elapsed,ReqTRES%40,MaxRSS,ExitCode 2>&1',
                host=args.host, timeout=60,
            )
            print(out.rstrip())
            return 0

        # Everything below needs to know which partitions are open to this user.
        if args.partition:
            partitions = [args.partition]
        else:
            partitions = usable_partitions(args.host)
            if not partitions:
                print("error: no partitions available to you", file=sys.stderr)
                return 1
            if args.gpus:
                # Only partitions that actually have GPUs can satisfy this.
                gpu_parts = oc.run_remote(
                    r'''scontrol show partition -o 2>/dev/null | grep 'gres/gpu=' '''
                    r'''| sed -E 's/^PartitionName=([^ ]+).*/\1/' ''',
                    host=args.host, timeout=60, check=False,
                ).split()
                filtered = [p for p in partitions if p in gpu_parts]
                if filtered:
                    partitions = filtered

        rows = plan(args, partitions)
        # A row the scheduler accepts but a QOS GPU ceiling can never admit is
        # not viable -- it would sit in the queue indefinitely.
        viable = [r for r in rows if r[1] == "yes" and not r[4].startswith("EXCEEDS")]
        rows.sort(key=lambda r: (r[1] != "yes", r[2]))

        oc.heading("Where this request would land")
        oc.table(rows, ["PARTITION", "ALLOWED", "WOULD START", "NODE", "NOTE"])
        if any(r[4].startswith("EXCEEDS") for r in rows):
            print(
                "\nEXCEEDS rows: sbatch --test-only validates scheduling, not QOS TRES\n"
                "ceilings, so it reports a start time for requests the partition's QOS\n"
                "can never admit. Those partitions are excluded from auto-selection."
            )

        if not viable:
            print(
                "\nNo partition would accept this request. The reasons above are the\n"
                "scheduler's own; the usual causes are asking for more than the\n"
                "partition's QOS allows you, or a walltime above its MaxTime."
            )
            return 1

        viable.sort(key=lambda r: r[2])
        best = viable[0][0]
        print(f"\nSoonest start: {best} at {viable[0][2]}")

        if args.plan:
            print("\n--plan given, so nothing was submitted.")
            return 0

        if not (args.script or args.remote_script or args.wrap):
            print("\nNothing to submit. Pass --script, --remote-script, or --wrap.")
            return 0

        partition = args.partition or best
        flags = build_flags(args, partition)

        if args.script:
            local = Path(args.script)
            if not local.is_file():
                print(f"error: {local} not found", file=sys.stderr)
                return 1
            # Relative on purpose, for both hops: scp's SFTP mode resolves it
            # against $HOME without any shell expansion (a $HOME or quoted ~
            # arrives literal and fails), and the sbatch below runs via ssh
            # with cwd=$HOME, so the same relative path is valid there too.
            remote_path = f".orcd-jobs/{local.name}"
            oc.run_remote("mkdir -p ~/.orcd-jobs", host=args.host, timeout=60)
            oc.scp_to(str(local), remote_path, host=args.host)
            target = remote_path
        elif args.remote_script:
            target = args.remote_script
        else:
            target = None

        quoted = " ".join(shlex.quote(f) for f in flags)
        if target:
            cmd = f"sbatch {quoted} {shlex.quote(target)} 2>&1"
        else:
            cmd = f"sbatch {quoted} --wrap={shlex.quote(args.wrap)} 2>&1"

        out = oc.run_remote(cmd, host=args.host, timeout=120, check=False).strip()
        print(f"\n{out}")
        job = re.search(r"Submitted batch job (\d+)", out)
        if not job:
            return 1
        print(
            f"\nTrack it with:\n"
            f"  python3 orcd_submit.py --status {job.group(1)}\n"
            f"  python3 orcd_submit.py --queue"
        )
        if not args.output:
            print(
                f"\nNote: job stdout will land in $HOME (slurm-{job.group(1)}.out) --\n"
                "sbatch's default cwd. For jobs that write real output, pass\n"
                "--output '<flash-scratch-path>/%x-%j.out' to keep IO off home."
            )
        return 0

    except oc.OrcdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("\nRun `python3 orcd_doctor.py` to diagnose access.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
