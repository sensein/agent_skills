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
import datetime as dt
import json
import re
import shlex
import sys
from pathlib import Path

import orcd_common as oc


def parse_walltime(spec: str) -> int | None:
    """Seconds for an sbatch ``-t`` value, or None if unbounded/unparsable.

    Slurm's forms: ``MM``, ``MM:SS``, ``HH:MM:SS``, ``DD-HH``, ``DD-HH:MM``,
    ``DD-HH:MM:SS``. Note ``30`` is thirty *minutes*, not seconds.
    """
    s = spec.strip()
    if not s or s.upper() in ("UNLIMITED", "INFINITE"):
        return None
    days = 0
    if "-" in s:
        d, _, s = s.partition("-")
        if not d.isdigit():
            return None
        days = int(d)
        parts = (s.split(":") if s else ["0"]) + ["0", "0"]
        h, m, sec = parts[:3]
    else:
        parts = s.split(":")
        if len(parts) == 1:
            h, m, sec = "0", parts[0], "0"
        elif len(parts) == 2:
            h, m, sec = "0", parts[0], parts[1]
        elif len(parts) == 3:
            h, m, sec = parts
        else:
            return None
    try:
        return days * 86400 + int(h) * 3600 + int(m) * 60 + int(sec)
    except ValueError:
        return None


def _slurm_time(text: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def fetch_reservations(host: str) -> tuple[dt.datetime | None, list[dict[str, str]]]:
    """Active and upcoming reservations, plus the *cluster's* current time.

    The cluster's clock is what Slurm compares against, and it need not match
    the caller's, so the comparison baseline is fetched with the reservations.
    """
    script = r'''
set +e
echo "@@NOW"
date +%Y-%m-%dT%H:%M:%S
echo "@@RES"
scontrol show reservation -o 2>/dev/null | while read -r line; do
  [ -n "$line" ] || continue
  get() { echo "$line" | grep -oE "(^| )$1=[^ ]*" | head -1 | cut -d= -f2-; }
  printf "%s|%s|%s|%s|%s|%s\n" "$(get ReservationName)" "$(get StartTime)" \
    "$(get EndTime)" "$(get NodeCnt)" "$(get PartitionName)" "$(get Flags)"
done
'''
    blocks = oc.parse_kv_blocks(oc.run_remote(script, host=host, timeout=60, check=False))
    now = _slurm_time(next((l for l in blocks.get("NOW", []) if l.strip()), ""))
    res = []
    for line in blocks.get("RES", []):
        f = [x.strip() for x in line.split("|")]
        if len(f) >= 6 and f[0]:
            res.append({"name": f[0], "start": f[1], "end": f[2],
                        "nodes": f[3], "partition": f[4], "flags": f[5]})
    return now, res


def reservation_conflict(reservations: list[dict[str, str]], walltime_s: int | None,
                         now: dt.datetime | None, partition: str | None = None) -> dict | None:
    """The reservation a job of this walltime would run into, if any.

    A job whose time limit crosses the start of a reservation covering its
    nodes is not scheduled before it: Slurm holds it (``ReqNodeNotAvail,
    Reserved for maintenance``) until the window ends, which looks exactly like
    ordinary queueing. Only cluster-wide reservations (``MAINT``/``ALL_NODES``)
    and ones naming this partition are reported; node-specific reservations
    cannot be evaluated from here and would only cry wolf.

    Returns the soonest offender with ``fits_seconds`` -- the largest ``-t``
    that still starts now.
    """
    if not walltime_s or now is None:
        return None
    deadline = now + dt.timedelta(seconds=walltime_s)
    best: dict | None = None
    for r in reservations:
        flags = r.get("flags", "").upper()
        rpart = r.get("partition", "")
        cluster_wide = "MAINT" in flags or "ALL_NODES" in flags
        names_partition = bool(partition) and rpart == partition
        if not (cluster_wide or names_partition):
            continue
        start = _slurm_time(r.get("start", ""))
        if start is None or start <= now or start > deadline:
            continue
        if best is None or start < _slurm_time(best["start"]):
            best = {**r, "fits_seconds": int((start - now).total_seconds())}
    return best


def reservation_warning(res: dict, walltime: str) -> str:
    """Explain a held-by-reservation request and the walltime that would run."""
    fits = res["fits_seconds"]
    hh, mm = divmod(max(fits // 60, 0), 60)
    return (
        f"\nWARNING: reservation {res['name']} starts {res['start']} "
        f"(ends {res['end']}, {res['nodes']} nodes, flags {res['flags']}).\n"
        f"A -t {walltime} request cannot finish before then, so Slurm holds it until the\n"
        f"window ends -- shown only as PENDING, reason `ReqNodeNotAvail, Reserved for\n"
        f"maintenance`, indistinguishable from ordinary queueing.\n"
        f"Largest walltime that starts now: -t {hh}:{mm:02d}:00 ({fits // 60} min).\n"
        f"Otherwise submit after {res['end']}."
    )


def pending_reservation_hint(queue_text: str) -> str:
    """Flag queue rows held by a reservation rather than waiting their turn."""
    if not re.search(r"ReqNodeNotAvail|Reserved for maintenance|ReqNodeNotAvail,\s*Reserv", queue_text):
        return ""
    return (
        "\nOne or more jobs are held by a reservation, not queueing: `ReqNodeNotAvail`\n"
        "with a reservation means the time limit crosses a maintenance window, so the\n"
        "job waits for the window to end. `scontrol show reservation` shows when;\n"
        "resubmit with a walltime that fits before it starts."
    )


def literal_path_problem(args: argparse.Namespace) -> str:
    """sbatch receives --chdir/--output/--remote-script exactly as typed.

    They are shlex-quoted on the way in, so `~` and `$VAR` arrive literally and
    fail at job start ("Unable to change directory", "Could not open stdout
    file") -- the same trap scp_to already refuses for. Absolute paths only.
    """
    for flag in ("chdir", "output", "remote_script"):
        val = getattr(args, flag, None)
        if val and (val.startswith("~") or "$" in val):
            return (f"--{flag.replace('_', '-')} {val!r}: sbatch gets this literally; "
                    "use an absolute path (no ~ or $VAR)")
    return ""


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
    if getattr(args, "exclude", ""):
        flags += ["--exclude", args.exclude]
    if args.name:
        flags += ["-J", args.name]
    if args.array:
        flags += ["-a", args.array]
    if args.output:
        flags += ["-o", args.output]
    if args.chdir:
        flags += ["-D", args.chdir]
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
                 for m in re.finditer(r"gres/gpu:([A-Za-z0-9_.-]+)=(\d+)", tres)}
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


def parse_mem_mb(spec: str) -> int:
    """Megabytes for an sbatch ``--mem`` value (bare number = MB)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*", spec.upper())
    if not m:
        return 0
    mult = {"K": 1 / 1024, "": 1, "M": 1, "G": 1024, "T": 1024 * 1024}[m.group(2)]
    return int(float(m.group(1)) * mult)


def _tres(text: str) -> dict[str, int]:
    """``cpu=64,mem=1000G,gres/gpu=4,gres/gpu:h100=4`` -> ints (mem in MB)."""
    out: dict[str, int] = {}
    for item in text.split(","):
        k, _, v = item.partition("=")
        if not k or not v or v == "N/A":
            continue
        out[k] = parse_mem_mb(v) if k == "mem" else int(re.sub(r"\D", "", v) or 0)
    return out


_BAD_STATE = ("DRAIN", "DOWN", "FAIL", "MAINT", "NOT_RESPONDING", "INVAL")
_NOT_NOW = ("POWER", "RESERVED")  # healthy but not placeable right now: not reported as skipped
_GPU_REASON = re.compile(r"gpu|gres|nvidia|nvml|xid|ecc|cuda", re.I)


def _gres_gpu_count(text: str) -> int | None:
    """Total GPUs in a node ``Gres``/``GresUsed`` value, e.g. ``gpu:h100:4(S:0-1)``; None if no gpu entry."""
    total, seen = 0, False
    for item in re.split(r",(?![^()]*\))", text or ""):
        m = re.match(r"gpu(?::[A-Za-z0-9_.-]+)?:(\d+)", item.strip())
        if m:
            total += int(m.group(1))
            seen = True
    return total if seen else None


def parse_node_line(line: str) -> dict | None:
    """One ``scontrol show node -o`` line -> state, reason, configured/used GRES and TRES."""
    name = re.search(r"NodeName=(\S+)", line)
    if not name:
        return None
    reason = re.search(r"\sReason=(.*?)(?:\s\[\S+@[^\]]*\]|\s\w+=|$)", line)
    return {
        "name": name.group(1),
        "state": (re.search(r"\sState=(\S+)", line) or [None, ""])[1],
        "reason": reason.group(1).strip() if reason else "",
        "gres": (re.search(r"\sGres=(\S*)", line) or [None, ""])[1],
        "gres_used": (re.search(r"\sGresUsed=(\S*)", line) or [None, ""])[1],
        "cfg": _tres((re.search(r"CfgTRES=(\S*)", line) or [None, ""])[1]),
        "alloc": _tres((re.search(r"AllocTRES=(\S*)", line) or [None, ""])[1]),
    }


def node_unhealthy(node: dict) -> str:
    """Why a node must not be used, or "" if it looks healthy.

    Down/drained/failing/unresponsive/invalid/maintenance states, a GPU count in
    ``Gres`` that disagrees with ``CfgTRES gres/gpu`` or with ``GresUsed``, and a
    GPU-related ``Reason`` on a node still marked usable.
    """
    state = node["state"].upper()
    bad = [b for b in _BAD_STATE if b in state]
    if bad:
        reason = node.get("reason", "")
        return f"state {node['state']}" + (f" ({reason})" if reason else "")
    if not (state.startswith("IDLE") or state.startswith("MIXED") or state.startswith("ALLOC")):
        return f"state {node['state'] or 'unknown'}"
    gres = _gres_gpu_count(node.get("gres", ""))
    cfg = node.get("cfg", {}).get("gres/gpu")
    if gres is not None and cfg is not None and gres != cfg:
        return f"GRES mismatch: Gres reports {gres} gpu, CfgTRES {cfg}"
    used = _gres_gpu_count(node.get("gres_used", ""))
    if gres is not None and used is not None and used > gres:
        return f"GRES mismatch: GresUsed {used} gpu exceeds Gres {gres}"
    if gres is None and cfg:
        return f"GRES mismatch: CfgTRES {cfg} gpu but no Gres gpu entry"
    if node.get("reason") and _GPU_REASON.search(node["reason"]):
        return f"reason: {node['reason']}"
    return ""


def fetch_live(host: str) -> dict:
    """Partition MaxTime, per-node free resources and pending counts, in one ssh call."""
    script = r'''
set +e
echo "@@PART"
scontrol show partition -o 2>/dev/null | sed -E 's/^PartitionName=([^ ]+).* MaxTime=([^ ]+).*/\1|\2/' | grep '|'
echo "@@MEMB"
sinfo -h -N -o "%R|%N" 2>/dev/null
echo "@@NODE"
scontrol show node -o 2>/dev/null
echo "@@PEND"
squeue -h -t PD -o "%P" 2>/dev/null | sort | uniq -c
'''
    blocks = oc.parse_kv_blocks(oc.run_remote(script, host=host, timeout=120, check=False))
    max_time = {}
    for line in blocks.get("PART", []):
        n, _, t = line.partition("|")
        if n.strip():
            max_time[n.strip()] = t.strip()
    members: dict[str, list[str]] = {}
    for line in blocks.get("MEMB", []):
        part, _, node = line.partition("|")
        if node.strip():
            members.setdefault(part.strip(), []).append(node.strip())
    nodes = {}
    for line in blocks.get("NODE", []):
        node = parse_node_line(line)
        if node:
            node["unhealthy"] = node_unhealthy(node)
            nodes[node["name"]] = node
    pending = {}
    for line in blocks.get("PEND", []):
        m = re.match(r"\s*(\d+)\s+(\S+)", line)
        if m:
            pending[m.group(2)] = int(m.group(1))
    return {"max_time": max_time, "members": members, "nodes": nodes, "pending": pending}


def live_fit(args: argparse.Namespace, live: dict, part: str, excluded: set[str]) -> list[str]:
    """Nodes of ``part`` (healthy, not excluded) with the requested cpus/mem/gres idle right now."""
    need_mem = parse_mem_mb(args.mem)
    fits = []
    for name in live["members"].get(part, []):
        node = live["nodes"].get(name)
        if node is None or name in excluded:
            continue
        state = node["state"].upper()
        if node.get("unhealthy") or any(b in state for b in _NOT_NOW) or not (
                state.startswith("IDLE") or state.startswith("MIXED")):
            continue
        cfg, alloc = node["cfg"], node["alloc"]
        if cfg.get("cpu", 0) - alloc.get("cpu", 0) < args.cpus:
            continue
        if cfg.get("mem") and need_mem and cfg["mem"] - alloc.get("mem", 0) < need_mem:
            continue
        if args.gpus:
            key = f"gres/gpu:{args.gpu_type}" if args.gpu_type else "gres/gpu"
            if key not in cfg or cfg[key] - alloc.get(key, 0) < args.gpus:
                continue
        fits.append(name)
    return fits


def skipped_nodes(live: dict, partitions: list[str]) -> list[dict]:
    """Unhealthy nodes in ``partitions`` with the reason each is skipped."""
    out = []
    for part in partitions:
        for name in live["members"].get(part, []):
            node = live["nodes"].get(name)
            if node and node.get("unhealthy"):
                out.append({"node": name, "partition": part, "reason": node["unhealthy"]})
    return sorted(out, key=lambda r: (r["partition"], r["node"]))


def plan(args: argparse.Namespace, partitions: list[str]) -> tuple[list[dict], list[dict]]:
    """Ask the scheduler what each partition would do with this exact request.

    Each row carries the ``sbatch --test-only`` estimate (which ignores
    backfill) and a live fit check against currently idle, healthy node
    resources. Returns ``(rows, skipped_nodes)``.
    """
    lines = ["set +e"]
    for p in partitions:
        flags = " ".join(shlex.quote(f) for f in build_flags(args, p))
        lines.append(
            f'out=$(sbatch --test-only {flags} --wrap=true 2>&1); '
            f'if [ $? -eq 0 ]; then echo "{p}|OK|$out"; else echo "{p}|NO|$(echo "$out" | head -1 | sed "s/.*error: //")"; fi'
        )
    out = oc.run_remote("\n".join(lines), host=args.host, timeout=240, check=False)
    ceilings = qos_gpu_ceilings(args) if args.gpus else {}
    live = fetch_live(args.host)
    excluded = {x for x in re.split(r"[,\s]+", args.exclude or "") if x}
    want_s = parse_walltime(args.time)

    rows = []
    for line in out.splitlines():
        f = line.split("|", 2)
        if len(f) < 3:
            continue
        part, ok, detail = f[0].strip(), f[1].strip(), f[2].strip()
        mt = live["max_time"].get(part, "")
        mt_s = parse_walltime(mt)
        exceeds = bool(want_s and mt_s and want_s > mt_s)
        fit_nodes = live_fit(args, live, part, excluded)
        row = {"partition": part, "allowed": ok == "OK", "fits_now": bool(fit_nodes) and not exceeds,
               "fit_nodes": fit_nodes[:5], "would_start": "-", "node": "-",
               "max_time": mt or "?", "exceeds_time": exceeds, "excluded": sorted(excluded),
               "pending_jobs": live["pending"].get(part, 0), "note": ""}
        if ok == "OK":
            when = re.search(r"\d{4}-\d{2}-\d{2}T[\d:]+", detail)
            node = re.search(r"on nodes? (\S+)", detail)
            row["would_start"] = when.group(0) if when else "?"
            row["node"] = node.group(1) if node else "?"
            row["note"] = gpu_ceiling_note(args, ceilings.get(part, {})) if args.gpus else ""
        else:
            row["note"] = detail[:58]
        if exceeds:
            row["note"] = f"EXCEEDS time limit (MaxTime {mt})"
        rows.append(row)
    return rows, skipped_nodes(live, partitions)


def _render(r: dict) -> list[str]:
    if r["exceeds_time"]:
        state = "EXCEEDS time limit"
    elif r["fits_now"]:
        state = "fits now"
    elif r["allowed"]:
        state = f"queued (estimate {r['would_start']})"
    else:
        state = "no"
    return [r["partition"], "yes" if r["allowed"] else "no", state, r["max_time"], r["would_start"],
            r["node"], ",".join(r["fit_nodes"][:2]) or "-", str(r["pending_jobs"]), r["note"]]


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
    ap.add_argument("--chdir", help="job working directory (sbatch -D); default is $HOME, the slowest tier")
    ap.add_argument("--exclude", metavar="NODES", default="",
                    help="optional comma-separated nodes to exclude (sbatch --exclude), for --plan and "
                         "submission; empty by default (unhealthy nodes are skipped automatically)")
    ap.add_argument("--json", action="store_true", help="with --plan, print machine-readable JSON only")
    ap.add_argument("--plan", action="store_true", help="only show where this would land, submit nothing")
    ap.add_argument("--queue", action="store_true", help="show your queue")
    ap.add_argument("--status", help="show details for a job id")
    args = ap.parse_args()

    problem = literal_path_problem(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 1

    try:
        if args.queue:
            oc.heading("Your jobs")
            out = oc.run_remote(
                'squeue -u "$USER" -o "%.12i %.22j %.16P %.10T %.11M %.11l %.6D %R" 2>&1',
                host=args.host, timeout=60,
            )
            print(out.rstrip() or "(no jobs)")
            print(pending_reservation_hint(out), end="")
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

        rows, skipped = plan(args, partitions)
        # A row the scheduler accepts but a QOS GPU ceiling can never admit, or a
        # -t above MaxTime, is not viable -- it would never run.
        viable = [r for r in rows if r["allowed"] and not r["exceeds_time"]
                  and not r["note"].startswith("EXCEEDS")]
        rows.sort(key=lambda r: (not r["allowed"], r["exceeds_time"], not r["fits_now"], r["would_start"]))
        viable.sort(key=lambda r: (not r["fits_now"], r["would_start"]))

        res_now, reservations = fetch_reservations(args.host)
        clash = reservation_conflict(reservations, parse_walltime(args.time), res_now,
                                     args.partition)
        best_row = viable[0] if viable else None

        if args.json and args.plan:
            print(json.dumps({
                "request": {"cpus": args.cpus, "mem": args.mem, "time": args.time, "gpus": args.gpus,
                            "gpu_type": args.gpu_type, "nodes": args.nodes},
                "excluded": sorted(x for x in re.split(r"[,\s]+", args.exclude or "") if x),
                "skipped_nodes": skipped,
                "rows": rows,
                "soonest": ({"partition": best_row["partition"], "fits_now": best_row["fits_now"],
                             "would_start": best_row["would_start"]} if best_row else None),
                "reservation_warning": reservation_warning(clash, args.time) if clash else "",
                "note": "sbatch --test-only start estimates ignore backfill; trust fits_now",
            }, indent=2))
            return 0 if best_row else 1

        oc.heading("Where this request would land")
        if args.exclude:
            print(f"Excluding nodes: {args.exclude}")
        if skipped:
            print("Skipped nodes (unhealthy):")
            for r in skipped:
                print(f"  {r['node']} ({r['partition']}): {r['reason']}")
        oc.table([_render(r) for r in rows],
                 ["PARTITION", "ALLOWED", "STATUS", "MAXTIME", "WOULD START", "NODE", "FREE NODES", "PEND", "NOTE"])
        print("\nSTATUS: `fits now` = an allowed, healthy, non-excluded node has the cpus/mem/gres idle right now "
              "(pending jobs ahead of you are not counted; PEND shows them).\n"
              "`queued (estimate ...)` comes from sbatch --test-only, which ignores backfill and can be "
              "badly pessimistic: trust `fits now` over it.")
        if any(r["note"].startswith("EXCEEDS") and not r["exceeds_time"] for r in rows):
            print("\nEXCEEDS: --test-only ignores QOS TRES ceilings; those rows would queue forever and are excluded.")

        # A reservation the walltime crosses holds the job with no distinct
        # state: WOULD START above already lands past the window, but the
        # reason is worth naming, along with the walltime that runs now.
        if clash:
            print(reservation_warning(clash, args.time))

        if not viable:
            print("\nNo partition accepts this request (usual causes: over the QOS limit, or -t above MaxTime).")
            return 1

        best = best_row["partition"]
        if best_row["fits_now"]:
            print(f"\nSoonest start: {best} (fits now on {', '.join(best_row['fit_nodes'][:2])})")
        else:
            print(f"\nSoonest start: {best} at {best_row['would_start']} (test-only estimate; ignores backfill)")

        if args.plan:
            print("\n--plan given, so nothing was submitted.")
            return 0

        if not (args.script or args.remote_script or args.wrap):
            print("\nNothing to submit. Pass --script, --remote-script, or --wrap.")
            return 0

        partition = args.partition or best
        # Unhealthy nodes of the chosen partition are excluded from the real
        # submission too, so the scheduler cannot place the job on one.
        bad = [r["node"] for r in skipped if r["partition"] == partition]
        if bad:
            args.exclude = ",".join(sorted({*(x for x in re.split(r"[,\s]+", args.exclude or "") if x), *bad}))
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
        if not (args.output or args.chdir):
            print(
                f"\nNote: cwd and stdout (slurm-{job.group(1)}.out) are in $HOME, the slowest\n"
                "tier. For real output pass --chdir <flash-scratch-path> and/or --output."
            )
        return 0

    except oc.OrcdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("\nRun `python3 orcd_doctor.py` to diagnose access.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
