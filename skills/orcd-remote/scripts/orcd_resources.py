#!/usr/bin/env python3
"""Discover what *this* user can actually run on ORCD. Nothing is hardcoded.

Partition access on ORCD is gated by Unix group membership (``AllowGroups``),
and the per-user ceilings come from the QOS that each partition attaches
automatically. Neither is guessable from a hostname, and both differ per person,
so this script asks the scheduler instead of assuming:

  * ``scontrol show partition``  -- every partition and its gates
  * ``sbatch --test-only``       -- the authoritative yes/no, per partition and
                                    per GPU type, plus an estimated start time
  * ``sacctmgr show qos``        -- the ceilings that will actually bind you
  * ``sinfo``                    -- GPU inventory and what is idle right now

``--test-only`` never queues anything; it asks the scheduler to evaluate the
request and report where it *would* land.

    python3 orcd_resources.py              # partitions you can use
    python3 orcd_resources.py --gpus       # add per-GPU-type access probing
    python3 orcd_resources.py --idle       # add what is free right now
    python3 orcd_resources.py --json       # machine-readable
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import orcd_common as oc

# One round trip does all the discovery. Sections are marked with @@ so the
# local side never has to guess where one command's output ends.
REMOTE = r'''
set +e
echo "@@IDENTITY"
printf "user|%s\n" "$(whoami)"
printf "cluster|%s\n" "$(scontrol show config 2>/dev/null | awk -F= '/^ClusterName/{gsub(/ /,"",$2);print $2}')"
printf "groups|%s\n" "$(id -Gn | tr ' ' ',')"
printf "assoc_qos|%s\n" "$(sacctmgr -nP show assoc user="$USER" format=QOS | head -1)"
printf "accounts|%s\n" "$(sacctmgr -nP show assoc user="$USER" format=Account | sort -u | paste -sd, -)"
printf "defmempercpu|%s\n" "$(scontrol show config 2>/dev/null | awk -F= '/^DefMemPerCPU/{gsub(/ /,"",$2);print $2}')"

echo "@@PARTITIONS"
# name|allowgroups|allowqos|partition_qos|maxtime|totalnodes|tres
scontrol show partition -o 2>/dev/null | while read -r line; do
  get() { echo "$line" | grep -oE "(^| )$1=[^ ]*" | head -1 | cut -d= -f2-; }
  printf "%s|%s|%s|%s|%s|%s|%s\n" \
    "$(get PartitionName)" "$(get AllowGroups)" "$(get AllowQos)" \
    "$(get QoS)" "$(get MaxTime)" "$(get TotalNodes)" "$(get TRES)"
done

echo "@@TESTONLY"
# Ask the scheduler whether a minimal job in each partition is permitted.
for P in $(scontrol show partition -o 2>/dev/null | sed -E 's/^PartitionName=([^ ]+).*/\1/'); do
  out=$(sbatch --test-only -p "$P" -t 5 -n 1 --mem=1G --wrap=true 2>&1)
  if [ $? -eq 0 ]; then
    printf "%s|OK|%s\n" "$P" "$(echo "$out" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]+' | head -1)"
  else
    printf "%s|DENIED|%s\n" "$P" "$(echo "$out" | head -1 | sed 's/.*error: //')"
  fi
done

echo "@@QOS"
# name|maxwall|maxtres_per_user|grptres_shared|maxsubmit_per_user
# Slurm exposes two different ceilings and conflating them is misleading:
# MaxTRESPU is yours alone, GrpTRES is shared by everyone in the group.
sacctmgr -nP show qos format=Name,MaxWall,MaxTRESPU,GrpTRES,MaxSubmitPU 2>/dev/null

echo "@@IDLE"
# partition|node|state|gres|gresused
sinfo -h -N -O "Partition:40,NodeHost:24,StateLong:20,Gres:60,GresUsed:60" 2>/dev/null \
  | awk '{p=$1; n=$2; s=$3; g=$4; u=$5; print p"|"n"|"s"|"g"|"u}'
'''


def parse_identity(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        if "|" in line:
            k, _, v = line.partition("|")
            out[k.strip()] = v.strip()
    return out


def parse_partitions(lines: list[str]) -> dict[str, dict]:
    parts: dict[str, dict] = {}
    for line in lines:
        f = line.split("|")
        if len(f) < 7 or not f[0].strip():
            continue
        parts[f[0].strip()] = {
            "name": f[0].strip(),
            "allow_groups": f[1].strip(),
            "allow_qos": f[2].strip(),
            "partition_qos": f[3].strip(),
            "max_time": f[4].strip(),
            "total_nodes": f[5].strip(),
            "tres": f[6].strip(),
        }
    return parts


def parse_testonly(lines: list[str]) -> dict[str, tuple[bool, str]]:
    out: dict[str, tuple[bool, str]] = {}
    for line in lines:
        f = line.split("|")
        if len(f) < 2 or not f[0].strip():
            continue
        out[f[0].strip()] = (f[1].strip() == "OK", f[2].strip() if len(f) > 2 else "")
    return out


def gputypes_from_tres(parts: dict[str, dict]) -> dict[str, dict[str, int]]:
    """Read GPU inventory out of each partition's TRES string.

    Slurm has already aggregated totals there (``gres/gpu:h200=104``). Summing
    ``sinfo`` output instead would undercount badly, because sinfo collapses
    nodes with identical configurations onto one line.
    """
    out: dict[str, dict[str, int]] = {}
    typed = re.compile(r"gres/gpu:([a-z0-9_]+)=(\d+)")
    untyped = re.compile(r"gres/gpu=(\d+)")
    for name, meta in parts.items():
        tres = meta.get("tres", "")
        found = {m.group(1): int(m.group(2)) for m in typed.finditer(tres)}
        if not found:
            # Some partitions declare GPUs without a model, e.g. `gpu:1`. They are
            # still requestable, just only as `--gres=gpu:N`.
            bare = untyped.search(tres)
            if bare and int(bare.group(1)) > 0:
                found = {"untyped": int(bare.group(1))}
        if found:
            out[name] = found
    return out


def parse_qos(lines: list[str]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for line in lines:
        f = line.split("|")
        if len(f) < 5 or not f[0].strip():
            continue
        out[f[0].strip()] = {
            "max_wall": f[1].strip(),
            "max_tres_pu": f[2].strip(),
            "grp_tres": f[3].strip(),
            "max_submit_pu": f[4].strip(),
        }
    return out


def gpu_counts(gres: str) -> dict[str, int]:
    """Parse a sinfo Gres/GresUsed string into {type: count}.

    Tokens look like ``gpu:h100:4(S:0-1)``, ``gpu:h100:2(IDX:0-1)`` or, on
    partitions that declare no model, ``gpu:4``; the last is keyed ``untyped``
    to match ``gputypes_from_tres``.
    """
    out: dict[str, int] = {}
    for tok in gres.split(","):
        m = re.match(r"gpu:(?:([a-z0-9_]+):)?(\d+)", tok.strip())
        if m:
            out[m.group(1) or "untyped"] = out.get(m.group(1) or "untyped", 0) + int(m.group(2))
    return out


def parse_idle(lines: list[str]) -> dict[str, dict[str, int]]:
    """Free GPUs per partition/type = configured minus in use, on healthy nodes."""
    free: dict[str, dict[str, int]] = {}
    for line in lines:
        f = line.split("|")
        if len(f) < 5:
            continue
        part, _node, state, gres, used = (x.strip() for x in f[:5])
        # sinfo marks the default partition with a trailing `*`; scontrol does
        # not, and the two must key identically.
        part = part.rstrip("*")
        if not part:
            continue
        # Only nodes that can actually accept work. The suffix flags matter as
        # much as the base state: `mixed-` is PLANNED, meaning the backfill
        # scheduler has already earmarked the node's free GPUs for a queued job,
        # so counting them overstates availability (observed as 8 "free" vs 7
        # genuinely usable). * unresponsive, ~ powered down, # powering up,
        # % powering down, $ maintenance, @ reboot pending.
        if not any(s in state for s in ("idle", "mixed", "allocated")):
            continue
        if any(c in state for c in "*~#%-$@"):
            continue
        conf = gpu_counts(gres)
        inuse = gpu_counts(used)
        for gtype, n in conf.items():
            avail = max(0, n - inuse.get(gtype, 0))
            if avail:
                free.setdefault(part, {})[gtype] = free.setdefault(part, {}).get(gtype, 0) + avail
    return free


def probe_gpu_access(usable: list[str], gputypes: dict[str, dict[str, int]], host: str) -> dict[str, list[str]]:
    """Confirm each (partition, GPU type) pair is really requestable.

    A GPU type appearing in ``sinfo`` does not by itself mean this user may ask
    for it, so each pair gets its own ``--test-only``.
    """
    combos = [(p, t) for p in usable for t in sorted(gputypes.get(p, {}))]
    if not combos:
        return {}
    lines = ["set +e"]
    for part, gtype in combos:
        # An untyped GPU partition can only be asked for as `gpu:N`.
        gres = "gpu:1" if gtype == "untyped" else f"gpu:{gtype}:1"
        lines.append(
            f'out=$(sbatch --test-only -p {part} --gres={gres} -t 5 -n 1 --mem=1G --wrap=true 2>&1); '
            f'[ $? -eq 0 ] && echo "{part}|{gtype}|OK" || echo "{part}|{gtype}|NO"'
        )
    out = oc.run_remote("\n".join(lines), host=host, timeout=180, check=False)
    ok: dict[str, list[str]] = {}
    for line in out.splitlines():
        f = line.split("|")
        if len(f) == 3 and f[2].strip() == "OK":
            ok.setdefault(f[0], []).append(f[1])
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=oc.DEFAULT_HOST)
    ap.add_argument("--gpus", action="store_true", help="probe each GPU type for real access")
    ap.add_argument("--idle", action="store_true", help="show GPUs free right now")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    args = ap.parse_args()

    try:
        raw = oc.run_remote(REMOTE, host=args.host, timeout=240)
    except oc.OrcdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("\nRun `python3 orcd_doctor.py` to diagnose access.", file=sys.stderr)
        return 1

    blocks = oc.parse_kv_blocks(raw)
    ident = parse_identity(blocks.get("IDENTITY", []))
    parts = parse_partitions(blocks.get("PARTITIONS", []))
    tested = parse_testonly(blocks.get("TESTONLY", []))
    gputypes = gputypes_from_tres(parts)
    qos = parse_qos(blocks.get("QOS", []))
    idle = parse_idle(blocks.get("IDLE", [])) if args.idle else {}

    usable = [p for p, (ok, _) in sorted(tested.items()) if ok]
    denied = {p: msg for p, (ok, msg) in sorted(tested.items()) if not ok}

    gpu_ok: dict[str, list[str]] = {}
    if args.gpus and usable:
        try:
            gpu_ok = probe_gpu_access(usable, gputypes, args.host)
        except oc.OrcdError as exc:
            print(f"warning: GPU probe failed: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "identity": ident,
            "usable_partitions": usable,
            "denied_partitions": denied,
            "partitions": parts,
            "start_estimate": {p: t for p, (ok, t) in tested.items() if ok},
            "gpu_types": gputypes,
            "gpu_access_confirmed": gpu_ok,
            "qos": qos,
            "idle_gpus": idle,
        }, indent=2))
        return 0

    oc.heading("Who you are on ORCD")
    oc.table(
        [
            ["user", ident.get("user", "?")],
            ["cluster", ident.get("cluster", "?")],
            ["slurm accounts", ident.get("accounts", "?")],
            ["QOS you may request", ident.get("assoc_qos", "?") or "(none beyond default)"],
            ["default mem per cpu", f"{ident.get('defmempercpu', '?')} MB"],
        ],
        ["FIELD", "VALUE"],
    )

    oc.heading(f"Partitions you can submit to ({len(usable)} of {len(parts)})")
    rows = []
    for p in usable:
        meta = parts.get(p, {})
        gtypes = gputypes.get(p, {})
        if args.gpus and p in gpu_ok:
            gstr = ", ".join(f"{t}:{gtypes.get(t, '?')}" for t in sorted(gpu_ok[p]))
        elif gtypes:
            gstr = ", ".join(f"{t}:{n}" for t, n in sorted(gtypes.items()))
        else:
            gstr = "-"
        pqos = meta.get("partition_qos", "")
        lim = qos.get(pqos, {})
        per_user = lim.get("max_tres_pu", "") or "-"
        shared = lim.get("grp_tres", "") or "-"
        rows.append([
            p,
            meta.get("max_time", "?"),
            meta.get("total_nodes", "?"),
            gstr,
            per_user,
            shared,
            lim.get("max_submit_pu", "") or "-",
            tested[p][1] or "-",
        ])
    oc.table(
        rows,
        ["PARTITION", "MAXTIME", "NODES", "GPUS (configured)",
         "LIMIT: YOU", "LIMIT: GROUP (shared)", "MAXSUBMIT", "WOULD START"],
    )

    print(
        "\nBoth limits come from the partition's own QOS and apply whether or not you\n"
        "ask for them. LIMIT: YOU is yours alone. LIMIT: GROUP is a single pool shared\n"
        "with everyone else in the group, so a colleague's running job can block yours.\n"
        "MAXSUBMIT caps queued plus running jobs. WOULD START is this moment's estimate\n"
        "from the backfill scheduler -- a far-future value means a long queue."
    )

    if denied:
        oc.heading(f"Partitions closed to you ({len(denied)})")
        oc.table(
            [[p, (parts.get(p, {}).get("allow_groups", "") or "-")[:44], msg[:60]] for p, msg in denied.items()],
            ["PARTITION", "REQUIRES GROUP", "REASON"],
        )
        print("\nAccess is by Unix group. To request one, email orcd-help@mit.edu.")

    if args.idle and idle:
        oc.heading("GPUs free right now")
        rows = []
        for p in usable:
            if p in idle:
                for gtype, n in sorted(idle[p].items()):
                    rows.append([p, gtype, str(n)])
        oc.table(rows, ["PARTITION", "GPU TYPE", "FREE"])
        print("\nA snapshot, not a reservation -- it can change between now and submit.")

    if not args.gpus:
        print("\nRe-run with --gpus to verify each GPU type is really requestable, or --idle for live capacity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
