#!/usr/bin/env python3
"""Discover which ORCD filesystems this user can write to, and which are fast.

ORCD encodes storage entitlement in Unix group names of the form
``orcd_rg_<server>_<owner>``, and the server prefix tells you the hardware tier:

    fstor*   flash, exported over NFS-over-RDMA   -> fast, for active job IO
    hstor*   spinning disk, group capacity store  -> bulk, backed up
    core*    archive tier                         -> cold, retrieval is slow
    nfs*     the shared /home server              -> small, backed up, slow

So a user's group list plus the mount table is enough to derive both what is
reachable and what is worth using, with nothing hardcoded per person.

Two practical notes drive the implementation. Bare ``df -h`` can hang for
minutes on this cluster when any network mount is unresponsive, so paths are
read from ``/proc/mounts`` (never blocks) and sized individually under a
timeout. And ``/orcd`` is autofs, so a directory only materialises once
something touches it -- listing the parent is not a reliable inventory.

    python3 orcd_storage.py             # what you can write to, and how fast
    python3 orcd_storage.py --setup     # create your per-user dirs where missing
    python3 orcd_storage.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import orcd_common as oc

TIER_OF_SERVER = [
    (re.compile(r"^fstor"), "flash", "NFS over RDMA; best for active job IO"),
    (re.compile(r"^hstor"), "capacity", "spinning disk; bulk storage, backed up"),
    (re.compile(r"^core"), "archive", "archive tier; retrieval is slow"),
    (re.compile(r"^nfs"), "home", "shared home; small quota, backed up, slow metadata"),
    (re.compile(r"^eofe"), "capacity", "legacy capacity store"),
]

REMOTE = r'''
set +e
echo "@@IDENTITY"
printf "user|%s\n" "$(whoami)"
printf "home|%s\n" "$HOME"

echo "@@GROUPS"
id -Gn | tr ' ' '\n' | grep '^orcd_rg_' | sort

echo "@@MOUNTS"
# mountpoint|fstype|source  -- /proc/mounts never blocks, unlike df.
awk '$3 ~ /^(nfs|nfs4|lustre|gpfs|beegfs|xfs|ext4)$/ && $2 ~ /^\/(orcd|home|nese|pool)/ {print $2"|"$3"|"$1}' /proc/mounts | sort -u

echo "@@AUTOFS"
# Autofs roots that exist but are not yet mounted; probing these triggers them.
awk '$3=="autofs" && $2 ~ /^\/orcd/ {print $2}' /proc/mounts | sort

echo "@@PIDIRS"
# Per-PI and per-project trees are named after the PI/project, not the user, so
# enumerate the LDAP automount maps rather than guessing directory names.
if command -v ldapsearch >/dev/null 2>&1; then
  for tier in data compute scratch archive pool; do
    for key in $(ldapsearch -x -LLL -o ldif-wrap=no \
        -b "ou=auto.orcd.$tier,ou=automount,dc=cm,dc=cluster" \
        "(objectClass=automount)" cn 2>/dev/null | awk -F': ' '/^cn:/{print $2}'); do
      printf "%s|%s\n" "$tier" "$key"
    done
  done
fi
'''

# Sizes and writability are probed separately so one slow mount cannot stall the
# whole inventory: each path is timed out individually and all of them run
# concurrently, so total wall time is one timeout rather than the sum.
PROBE_TEMPLATE = r'''
set +e
probe() {{
  p="$1"
  if [ ! -e "$p" ]; then printf "%s|MISSING|||\n" "$p"; return; fi
  line=$(timeout 6 df -h --output=size,avail,pcent "$p" 2>/dev/null | tail -1)
  size=$(echo "$line" | awk '{{print $1}}'); avail=$(echo "$line" | awk '{{print $2}}')
  pct=$(echo "$line" | awk '{{print $3}}')
  w=$([ -w "$p" ] && echo yes || echo no)
  nb=$(timeout 6 test -e "$p/__STORAGE_WITHOUT_BACKUP__" && echo NO_BACKUP || echo -)
  printf "%s|%s|%s|%s|%s|%s\n" "$p" "$w" "${{size:-?}}" "${{avail:-?}}" "${{pct:-?}}" "$nb"
}}
for p in {paths}; do probe "$p" & done
wait
'''


TIER_OF_PATH = [
    (re.compile(r"^/orcd/archive/"), "archive", "archive tier; retrieval is slow"),
    (re.compile(r"^/orcd/scratch/"), "flash", "scratch tier; fast, purged, not backed up"),
    (re.compile(r"^/orcd/compute/"), "flash", "flash project tier"),
    (re.compile(r"^/orcd/(data|nese)/"), "capacity", "capacity disk tier"),
    (re.compile(r"^/orcd/datasets/"), "flash", "shared read-only datasets"),
    (re.compile(r"^/home"), "home", "shared home; small quota, slow metadata"),
]


def tier_for(source: str, path: str = "") -> tuple[str, str]:
    """Classify a mount by its server name, which encodes the hardware tier.

    Falls back to the path prefix, because an autofs tree that nothing has
    touched yet has no ``/proc/mounts`` entry and therefore no known server.
    """
    server = source.split(":")[0].split(".")[0]
    if server:
        for pattern, tier, note in TIER_OF_SERVER:
            if pattern.match(server):
                return tier, note
    for pattern, tier, note in TIER_OF_PATH:
        if pattern.match(path):
            return tier, note
    return "other", ""


def parse_pipe(lines: list[str], nfields: int) -> list[list[str]]:
    out = []
    for line in lines:
        if not line.strip():
            continue
        f = [x.strip() for x in line.split("|")]
        if len(f) >= nfields and f[0]:
            out.append(f)
    return out


def candidate_paths(groups: list[str], mounts: list[list[str]], pidirs: list[list[str]], home: str) -> list[str]:
    """Build the list of paths worth probing for this specific user.

    Sources, in order of reliability: mounts that already exist, the PI/project
    trees named in the LDAP automount maps (filtered to owners this user has a
    group for), and $HOME.
    """
    # orcd_rg_<server>_<owner> -- collect the owner tokens we are entitled to.
    # Partition groups look like orcd_rg_par_ou_bcs_high, whose owner is `bcs`,
    # so the trailing tier suffix is trimmed.
    owners: set[str] = set()
    for g in groups:
        m = re.match(r"^orcd_rg_[a-z0-9]+_(?:pi|pg|ou)_(.+)$", g)
        if not m:
            continue
        owner = m.group(1)
        owners.add(owner)
        owners.add(re.sub(r"_(high|low|normal|mgrs)$", "", owner))

    # Cluster infrastructure: real mounts, but never a place to put your data.
    infra = re.compile(r"^/orcd/(software|system|examples|home)(/|$)")

    def owner_of(path: str) -> str | None:
        m = re.match(r"^/orcd/(?:data|compute|scratch|archive|pool|nese)/([^/]+)/", path + "/")
        return m.group(1) if m else None

    paths: list[str] = [home]

    # Only probe mounts this user has a plausible claim to. The cluster exports
    # dozens of other groups' trees; touching each one costs a df and returns
    # nothing useful.
    for m in mounts:
        path = m[0]
        if infra.match(path):
            continue
        owner = owner_of(path)
        if owner is None or owner in owners:
            paths.append(path)

    for tier, key in pidirs:
        if key in owners:
            for n in ("001", "002", "003"):
                paths.append(f"/orcd/{tier}/{key}/{n}")

    # The general per-user scratch tier is open to everyone, so include it even
    # though no group name points at it.
    paths.append("/orcd/scratch/orcd/001")

    seen, ordered = set(), []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=oc.DEFAULT_HOST)
    ap.add_argument("--setup", action="store_true", help="create your per-user directory in each writable tier")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        raw = oc.run_remote(REMOTE, host=args.host, timeout=180)
    except oc.OrcdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("\nRun `python3 orcd_doctor.py` to diagnose access.", file=sys.stderr)
        return 1

    blocks = oc.parse_kv_blocks(raw)
    ident = {k: v for k, _, v in (l.partition("|") for l in blocks.get("IDENTITY", []) if "|" in l)}
    user = ident.get("user", "")
    home = ident.get("home", f"/home/{user}")
    groups = [g.strip() for g in blocks.get("GROUPS", []) if g.strip()]
    mounts = parse_pipe(blocks.get("MOUNTS", []), 3)
    pidirs = parse_pipe(blocks.get("PIDIRS", []), 2)

    paths = candidate_paths(groups, mounts, pidirs, home)
    probe = PROBE_TEMPLATE.format(paths=" ".join(f'"{p}"' for p in paths))
    try:
        praw = oc.run_remote(probe, host=args.host, timeout=240, check=False)
    except oc.OrcdError as exc:
        print(f"error probing paths: {exc}", file=sys.stderr)
        return 1

    source_of = {m[0]: m[2] for m in mounts}
    entries = []
    for f in parse_pipe(praw.splitlines(), 2):
        path = f[0]
        if f[1] == "MISSING":
            continue
        # A PI tree discovered via autofs has no /proc/mounts entry until touched,
        # so fall back to matching the longest known mount prefix.
        src = source_of.get(path, "")
        if not src:
            best = max((m for m in source_of if path.startswith(m)), key=len, default="")
            src = source_of.get(best, "")
        tier, note = tier_for(src, path)
        entries.append({
            "path": path,
            "writable": f[1] == "yes",
            "size": f[2] if len(f) > 2 else "?",
            "avail": f[3] if len(f) > 3 else "?",
            "used_pct": f[4] if len(f) > 4 else "?",
            # ORCD drops a __STORAGE_WITHOUT_BACKUP__ sentinel in unprotected
            # trees. Its presence is proof; its absence is only the absence of a
            # marker, so never report that as "backed up".
            "no_backup_marker": (f[5] if len(f) > 5 else "-") == "NO_BACKUP",
            "server": src.split(":")[0] if src else "?",
            "tier": tier,
            "note": note,
        })

    if args.setup:
        oc.heading("Creating your per-user directories")
        targets = [
            e["path"] for e in entries
            if e["tier"] == "flash"
            and e["writable"]
            and any(seg in e["path"] for seg in ("/scratch/", "/compute/"))
        ]
        if not targets:
            print("No writable flash tier found to set up.")
        else:
            script = "set +e\n" + "\n".join(
                f'd="{t}/{user}"; if [ -d "$d" ]; then echo "exists|$d"; '
                f'elif mkdir -p "$d" 2>/dev/null; then echo "created|$d"; '
                f'else echo "failed|$d"; fi'
                for t in targets
            )
            out = oc.run_remote(script, host=args.host, timeout=120, check=False)
            oc.table([[a, b] for a, b in (l.split("|", 1) for l in out.splitlines() if "|" in l)],
                     ["RESULT", "PATH"])
            print("\nThese directories are setgid, so files you create there stay group-readable.")

    if args.json:
        print(json.dumps({"user": user, "groups": groups, "storage": entries}, indent=2))
        return 0

    oc.heading(f"Storage you can reach as {user}")
    order = {"flash": 0, "capacity": 1, "home": 2, "archive": 3, "unknown": 4, "other": 5}
    entries.sort(key=lambda e: (order.get(e["tier"], 9), e["path"]))
    oc.table(
        [[
            e["path"], e["tier"], e["server"],
            "yes" if e["writable"] else "no",
            e["size"], e["avail"], e["used_pct"],
            "NO BACKUP" if e["no_backup_marker"] else "unmarked",
        ] for e in entries],
        ["PATH", "TIER", "SERVER", "WRITE", "SIZE", "AVAIL", "USED", "BACKUP"],
    )
    print(
        "\nBACKUP reads NO BACKUP where ORCD has flagged the tree as unprotected.\n"
        "'unmarked' means only that no such flag is present -- confirm with ORCD\n"
        "before trusting anything here to be backed up."
    )

    oc.heading("Your storage group memberships")
    print("\n".join(f"  {g}" for g in groups) or "  (none)")
    print(
        "\nThese follow the pattern orcd_rg_<server>_<owner>. The server prefix is the\n"
        "tier: fstor = flash, hstor = capacity disk, core = archive."
    )

    oc.heading("Where to put what")
    flash = [e for e in entries if e["tier"] == "flash" and e["writable"]]
    capacity = [e for e in entries if e["tier"] == "capacity" and e["writable"]]

    if flash:
        print("Active job IO -- checkpoints, intermediates, datasets being read hot:")
        for e in flash:
            flag = "NOT backed up" if e["no_backup_marker"] else "backup unconfirmed"
            print(f"  {e['path']}/{user:<28} {e['avail']:>6} free, {flag}")
    if capacity:
        print("\nDatasets and results worth keeping (backed up, slower):")
        for e in capacity:
            print(f"  {e['path']:<44} {e['avail']:>6} free")
    print(
        f"\nCode and small config:\n"
        f"  {home:<44} backed up, but the slowest metadata of\n"
        "  any tier. Clone repos here; write job output elsewhere.\n"
        "\nNode-local, fastest of all, wiped when the job ends:\n"
        "  $TMPDIR (/tmp) always exists. /scratch is large but present on some nodes\n"
        "  only, so probe it before use. /dev/shm is RAM and counts against the memory\n"
        "  you requested.\n"
        "\nSee references/storage.md for measured throughput and the staging pattern."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
