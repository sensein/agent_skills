#!/usr/bin/env python3
"""Capture ORCD's Slurm and storage configuration, and diff it against a baseline.

Cluster configuration is not stable. Partitions are added and retired, QOS
ceilings are retuned, nodes gain and lose GPUs, and group membership changes when
someone joins or leaves a project. Any of those silently invalidates a job script
that worked last month, usually presenting as an unexplained queue or a refusal.

So: snapshot the configuration, keep it, and diff it later.

    python3 orcd_snapshot.py                 # human-readable summary
    python3 orcd_snapshot.py --save          # write a JSON baseline
    python3 orcd_snapshot.py --diff          # compare now against the baseline
    python3 orcd_snapshot.py --diff --save   # compare, then update the baseline
    python3 orcd_snapshot.py --json          # emit the snapshot on stdout

Baselines live in ``~/.orcd/snapshots/``: ``<cluster>-latest.json`` plus a
timestamped copy per save, so history is kept without extra bookkeeping.

Exit status from ``--diff`` is 0 when nothing changed and 2 when something did,
which makes it usable from a cron job or a CI step.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import orcd_common as oc

SNAP_DIR = Path.home() / ".orcd" / "snapshots"

# Config keys worth watching. Scheduling weights are included because they
# determine whether picking a different partition still helps.
CONFIG_KEYS = [
    "ClusterName", "SlurmctldHost", "SchedulerType", "SelectType",
    "SelectTypeParameters", "DefMemPerCPU", "DefMemPerNode", "MaxMemPerNode",
    "MaxJobCount", "MaxArraySize", "MaxTasksPerNode", "EnforcePartLimits",
    "AccountingStorageEnforce", "GresTypes", "PriorityType",
    "PriorityWeightAge", "PriorityWeightFairShare", "PriorityWeightJobSize",
    "PriorityWeightPartition", "PriorityWeightQOS", "PrologFlags", "TmpFS",
    "JobRequeue", "KillWait",
]

REMOTE = r'''
set +e

echo "@@META"
printf "user|%s\n" "$(whoami)"
printf "login_node|%s\n" "$(hostname -s)"
printf "slurm_version|%s\n" "$(sinfo --version 2>/dev/null)"

echo "@@CONFIG"
scontrol show config 2>/dev/null | awk -F'=' '
  /^[A-Za-z]/ { k=$1; sub(/[ \t]+$/,"",k); v=substr($0, index($0,"=")+1);
                gsub(/^[ \t]+|[ \t]+$/,"",v); print k"|"v }'

echo "@@ASSOC"
# Association-level caps. MaxSubmitJobs here is a single ceiling across every
# partition, and is easy to miss because the per-QOS number is usually larger.
sacctmgr -nP show assoc user="$USER" \
  format=Cluster,Account,Partition,QOS,DefaultQOS,GrpTRES,MaxJobs,MaxSubmit,MaxWall 2>/dev/null

echo "@@PARTITIONS"
scontrol show partition -o 2>/dev/null | while read -r line; do
  get() { echo "$line" | grep -oE "(^| )$1=[^ ]*" | head -1 | cut -d= -f2-; }
  printf "%s|%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n" \
    "$(get PartitionName)" "$(get AllowGroups)" "$(get AllowAccounts)" \
    "$(get AllowQos)" "$(get QoS)" "$(get MaxTime)" "$(get MaxNodes)" \
    "$(get TotalNodes)" "$(get PreemptMode)" "$(get PriorityTier)" "$(get TRES)"
done

echo "@@QOS"
sacctmgr -nP show qos \
  format=Name,Priority,MaxWall,MaxTRESPU,MaxTRESPJ,GrpTRES,MaxJobsPU,MaxSubmitPU,Flags,Preempt 2>/dev/null

echo "@@ACCESS"
# The authoritative entitlement check: ask the scheduler, do not infer.
for P in $(scontrol show partition -o 2>/dev/null | sed -E 's/^PartitionName=([^ ]+).*/\1/'); do
  sbatch --test-only -p "$P" -t 5 -n 1 --mem=1G --wrap=true >/dev/null 2>&1 \
    && echo "$P|yes" || echo "$P|no"
done

echo "@@GROUPS"
id -Gn | tr ' ' '\n' | grep '^orcd_rg_' | sort

echo "@@NODESHAPES"
# Distinct hardware configurations, so a fleet change shows up as a diff.
sinfo -h -N -o "%c|%m|%G" 2>/dev/null | sort | uniq -c | awk '{print $2"|"$1}'

echo "@@QUOTA"
# ORCD's daily per-user quota report. The only place the per-user scratch and
# pool limits appear -- df reports the whole filesystem, not the quota.
[ -r "$HOME/orcd/.quota" ] && cat "$HOME/orcd/.quota"

echo "@@HOMELINKS"
# $HOME/orcd holds root-managed symlinks to this user's personal spaces,
# including a per-user scratch whose /orcd/scratch/orcd/<NNN> shard differs
# per user and cannot be guessed.
if [ -d "$HOME/orcd" ]; then
  for l in "$HOME/orcd"/*; do
    [ -e "$l" ] || continue
    printf "%s|%s\n" "$(basename "$l")" "$(readlink -f "$l" 2>/dev/null)"
  done
fi

echo "@@STORAGE"
awk '$3 ~ /^(nfs|nfs4|lustre|gpfs|beegfs)$/ && $2 ~ /^\/(orcd|home)/ {print $2"|"$1}' \
  /proc/mounts | sort -u
'''


def rows(lines: list[str], n: int) -> list[list[str]]:
    out = []
    for line in lines:
        if not line.strip():
            continue
        f = [x.strip() for x in line.split("|")]
        if f[0]:
            out.append(f + [""] * (n - len(f)) if len(f) < n else f)
    return out


def build_snapshot(host: str) -> dict:
    raw = oc.run_remote(REMOTE, host=host, timeout=300)
    b = oc.parse_kv_blocks(raw)

    meta = {k: v for k, _, v in (l.partition("|") for l in b.get("META", []) if "|" in l)}
    allcfg = {k: v for k, _, v in (l.partition("|") for l in b.get("CONFIG", []) if "|" in l)}
    config = {k: allcfg[k] for k in CONFIG_KEYS if k in allcfg}

    partitions = {}
    for f in rows(b.get("PARTITIONS", []), 11):
        partitions[f[0]] = {
            "allow_groups": f[1], "allow_accounts": f[2], "allow_qos": f[3],
            "partition_qos": f[4], "max_time": f[5], "max_nodes": f[6],
            "total_nodes": f[7], "preempt_mode": f[8], "priority_tier": f[9],
            "tres": f[10],
            "gpus": {m.group(1): int(m.group(2))
                     for m in re.finditer(r"gres/gpu:([a-z0-9_]+)=(\d+)", f[10])},
        }

    qos = {}
    for f in rows(b.get("QOS", []), 10):
        qos[f[0]] = {
            "priority": f[1], "max_wall": f[2], "max_tres_pu": f[3],
            "max_tres_pj": f[4], "grp_tres": f[5], "max_jobs_pu": f[6],
            "max_submit_pu": f[7], "flags": f[8], "preempt": f[9],
        }

    assoc = []
    for f in rows(b.get("ASSOC", []), 9):
        assoc.append({
            "cluster": f[0], "account": f[1], "partition": f[2], "qos": f[3],
            "default_qos": f[4], "grp_tres": f[5],
            "max_jobs": f[6], "max_submit": f[7], "max_wall": f[8],
        })

    access = {f[0]: f[1] == "yes" for f in rows(b.get("ACCESS", []), 2)}

    quota = []
    for line in b.get("QUOTA", []):
        f = [x.strip() for x in line.split("|")]
        if len(f) < 7 or not f[0] or f[0] == "Space" or f[0].startswith("-"):
            continue
        if not re.match(r"^[A-Z][A-Z0-9 ]*$", f[0]):
            continue
        quota.append({
            "space": f[0], "used_gb": f[1], "limit_gb": f[2],
            "files": f[4], "file_limit": f[5],
        })

    personal = {f[0]: f[1] for f in rows(b.get("HOMELINKS", []), 2)}

    shapes = {}
    for f in rows(b.get("NODESHAPES", []), 2):
        cpu_mem_gres = f[0]
        shapes[cpu_mem_gres] = int(f[1]) if f[1].isdigit() else f[1]

    return {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "meta": meta,
        "config": config,
        "association": assoc,
        "partitions": partitions,
        "qos": qos,
        "partition_access": access,
        "storage_groups": sorted(g for g in b.get("GROUPS", []) if g.strip()),
        "quota": quota,
        "personal_spaces": personal,
        "storage_mounts": {f[0]: f[1] for f in rows(b.get("STORAGE", []), 2)},
        "node_shapes": shapes,
    }


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

# Fields that change on their own and would drown out real signal.
IGNORED = {"captured_at", "meta.login_node"}


def flatten(obj, prefix: str = "") -> dict[str, str]:
    """Flatten nested structures to dotted paths so diffs are precise."""
    flat: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            flat.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        # Lists are keyed by content rather than index, so reordering is not
        # reported as a change. Scalars become membership entries, which is what
        # makes a group being added or removed show up as exactly one line.
        for item in obj:
            if isinstance(item, dict):
                ident = item.get("account", "") or item.get("space", "")
                part = item.get("partition", "")
                flat.update(flatten(item, f"{prefix}[{ident}/{part}]" if part else f"{prefix}[{ident}]"))
            else:
                flat[f"{prefix}[{item}]"] = "present"
    else:
        flat[prefix] = "" if obj is None else str(obj)
    return flat


def diff_snapshots(old: dict, new: dict) -> dict[str, list[tuple[str, str, str]]]:
    a, b = flatten(old), flatten(new)
    keys = (set(a) | set(b)) - IGNORED
    added, removed, changed = [], [], []
    for k in sorted(keys):
        if k not in a:
            added.append((k, "", b[k]))
        elif k not in b:
            removed.append((k, a[k], ""))
        elif a[k] != b[k]:
            changed.append((k, a[k], b[k]))
    return {"added": added, "removed": removed, "changed": changed}


def significance(path: str) -> str | None:
    """Explain why a given change is likely to matter, when it clearly does."""
    if path.startswith("partition_access."):
        return "partition access changed -- job scripts targeting it may now fail"
    if ".max_submit" in path or ".max_jobs" in path:
        return "job-count ceiling changed"
    if "MaxArraySize" in path or "MaxJobCount" in path:
        return "array/job-count ceiling changed"
    if ".gpus." in path:
        return "GPU inventory changed -- a --gres model may no longer exist"
    if ".max_tres" in path or ".grp_tres" in path:
        return "resource ceiling changed"
    if ".max_time" in path:
        return "walltime ceiling changed"
    if ".preempt_mode" in path:
        return "preemption behaviour changed"
    if path.startswith("storage_groups"):
        return "storage entitlement changed"
    if "DefMemPerCPU" in path:
        return "default memory changed -- unspecified --mem gets a different value"
    if path.startswith("config.PriorityWeight"):
        return "scheduling weights changed"
    return None


def print_summary(snap: dict) -> None:
    meta, cfg = snap["meta"], snap["config"]
    oc.heading("Cluster")
    oc.table(
        [[k, str(v)] for k, v in [
            ("cluster", cfg.get("ClusterName", "?")),
            ("slurm", meta.get("slurm_version", "?")),
            ("user", meta.get("user", "?")),
            ("captured", snap["captured_at"]),
            ("default mem/cpu", cfg.get("DefMemPerCPU", "?") + " MB"),
            ("max array size", cfg.get("MaxArraySize", "?")),
            ("max jobs (cluster)", cfg.get("MaxJobCount", "?")),
        ]],
        ["FIELD", "VALUE"],
    )

    oc.heading("Your association limits (apply across all partitions)")
    oc.table(
        [[a["account"], a["qos"] or "-", a["max_jobs"] or "-",
          a["max_submit"] or "-", a["grp_tres"] or "-"] for a in snap["association"]],
        ["ACCOUNT", "QOS ALLOWED", "MAXJOBS", "MAXSUBMIT", "GRPTRES"],
    )

    usable = [p for p, ok in snap["partition_access"].items() if ok]
    oc.heading(f"Partitions you can submit to ({len(usable)} of {len(snap['partition_access'])})")
    rowsout = []
    for p in sorted(usable):
        meta_p = snap["partitions"].get(p, {})
        q = snap["qos"].get(meta_p.get("partition_qos", ""), {})
        gpus = ", ".join(f"{t}:{n}" for t, n in sorted(meta_p.get("gpus", {}).items())) or "-"
        rowsout.append([
            p, meta_p.get("max_time", "?"), meta_p.get("total_nodes", "?"), gpus,
            meta_p.get("preempt_mode", "?"), meta_p.get("priority_tier", "?"),
            q.get("max_submit_pu", "") or "-",
        ])
    oc.table(rowsout, ["PARTITION", "MAXTIME", "NODES", "GPUS", "PREEMPT", "TIER", "MAXSUBMIT"])

    if snap.get("quota"):
        oc.heading("Your quotas (ORCD refreshes ~/orcd/.quota daily)")
        oc.table(
            [[q["space"], f"{q['used_gb']} / {q['limit_gb']} GB",
              f"{q['files']} / {q['file_limit']}"] for q in snap["quota"]],
            ["SPACE", "USED / LIMIT", "FILES USED / LIMIT"],
        )
        print("\nFile counts bind independently of gigabytes; a 1M inode cap is easy to hit.")

    if snap.get("personal_spaces"):
        oc.heading("Your personal spaces (~/orcd symlinks)")
        oc.table([[k, v] for k, v in sorted(snap["personal_spaces"].items())],
                 ["NAME", "RESOLVES TO"])

    oc.heading("Job-count and array ceilings")
    caps = [int(a["max_submit"]) for a in snap["association"] if a["max_submit"].isdigit()]
    assoc_cap = min(caps) if caps else None
    limit_rows = []
    for p in sorted(usable):
        pq = snap["partitions"].get(p, {}).get("partition_qos", "")
        sub = snap["qos"].get(pq, {}).get("max_submit_pu", "")
        # Empirically the largest accepted array is MaxSubmitPU + 1 on this
        # cluster (verified by bisection in two partitions with very different
        # caps). `%K` throttling does not raise it -- it limits concurrent
        # running tasks, not the submitted count.
        arr = str(int(sub) + 1) if sub.isdigit() else "-"
        limit_rows.append([p, sub or "-", arr])
    oc.table(limit_rows, ["PARTITION", "MAXSUBMIT (queued+running)", "MAX ARRAY TASKS"])
    print(
        f"\nAssociation MaxSubmit: {assoc_cap if assoc_cap is not None else 'unset'} "
        "(a single ceiling across every partition)\n"
        f"Cluster MaxArraySize:  {snap['config'].get('MaxArraySize', '?')} "
        f"   MaxJobCount: {snap['config'].get('MaxJobCount', '?')}\n"
        "\nThe binding limit is the smallest that applies. Array tasks each count as\n"
        "a submitted job, so `-a 0-999` is refused with QOSMaxSubmitJobPerUserLimit\n"
        "wherever MaxSubmitPU is below 1000 -- and adding `%50` does not help. Split\n"
        "large sweeps into consecutive arrays instead."
    )


def print_diff(d: dict, old_at: str, new_at: str) -> None:
    total = sum(len(v) for v in d.values())
    oc.heading(f"Configuration diff ({old_at} -> {new_at})")
    if not total:
        print("No changes.")
        return

    for label, items in (("CHANGED", d["changed"]), ("ADDED", d["added"]), ("REMOVED", d["removed"])):
        if not items:
            continue
        print(f"\n{label} ({len(items)})")
        for path, before, after in items:
            if label == "CHANGED":
                print(f"  {path}\n      before: {before}\n      after:  {after}")
            elif label == "ADDED":
                print(f"  {path} = {after}")
            else:
                print(f"  {path} (was {before})")

    notes = {}
    for path, _, _ in d["changed"] + d["added"] + d["removed"]:
        why = significance(path)
        if why:
            notes.setdefault(why, []).append(path)
    if notes:
        oc.heading("Why these matter")
        for why, paths in notes.items():
            print(f"  {why}")
            for p in paths[:6]:
                print(f"      {p}")
            if len(paths) > 6:
                print(f"      ... and {len(paths) - 6} more")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=oc.DEFAULT_HOST)
    ap.add_argument("--save", action="store_true", help="write/update the JSON baseline")
    ap.add_argument("--diff", action="store_true", help="compare against the saved baseline")
    ap.add_argument("--baseline", help="explicit baseline path (default ~/.orcd/snapshots/<cluster>-latest.json)")
    ap.add_argument("--json", action="store_true", help="print the snapshot as JSON")
    args = ap.parse_args()

    try:
        snap = build_snapshot(args.host)
    except oc.OrcdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("\nRun `python3 orcd_doctor.py` to diagnose access.", file=sys.stderr)
        return 1

    cluster = snap["config"].get("ClusterName") or "orcd"
    baseline = Path(args.baseline) if args.baseline else SNAP_DIR / f"{cluster}-latest.json"

    exit_code = 0
    if args.diff:
        if not baseline.is_file():
            print(f"No baseline at {baseline}. Create one with --save.", file=sys.stderr)
            exit_code = 1
        else:
            old = json.loads(baseline.read_text())
            d = diff_snapshots(old, snap)
            print_diff(d, old.get("captured_at", "?"), snap["captured_at"])
            if sum(len(v) for v in d.values()):
                exit_code = 2

    if args.save:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = snap["captured_at"].replace(":", "").replace("-", "")
        payload = json.dumps(snap, indent=2, sort_keys=True)
        (SNAP_DIR / f"{cluster}-{stamp}.json").write_text(payload)
        baseline.write_text(payload)
        print(f"\nBaseline written: {baseline}")

    if args.json:
        print(json.dumps(snap, indent=2, sort_keys=True))
    elif not args.diff:
        print_summary(snap)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
