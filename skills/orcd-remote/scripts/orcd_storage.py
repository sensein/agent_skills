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
import os
import re
import sys
import textwrap
from pathlib import Path

import orcd_common as oc

TIER_OF_SERVER = [
    (re.compile(r"^fstor"), "flash", "NFS over RDMA; best for active job IO"),
    (re.compile(r"^hstor"), "capacity", "spinning disk; bulk storage; assume NOT backed up"),
    (re.compile(r"^core"), "archive", "archive tier; retrieval is slow"),
    (re.compile(r"^nfs"), "home", "shared home; small quota, backed up, slow metadata"),
    (re.compile(r"^eofe"), "capacity", "legacy capacity store"),
]

REMOTE = r'''
set +e
echo "@@IDENTITY"
printf "user|%s\n" "$(whoami)"
printf "home|%s\n" "$HOME"

echo "@@QUOTA"
# ORCD regenerates a per-user quota report roughly every 30 minutes. It is the
# only place the per-user scratch and pool limits appear -- df shows the whole
# filesystem, not the quota, so it is useless for those.
[ -r "$HOME/orcd/.quota" ] && cat "$HOME/orcd/.quota"

echo "@@HOMELINKS"
# $HOME/orcd holds root-managed symlinks to this user's personal spaces. The
# per-user scratch tier is sharded across /orcd/scratch/orcd/<NNN>, and the
# shard differs per user, so these links are the only reliable way to find it.
if [ -d "$HOME/orcd" ]; then
  for l in "$HOME/orcd"/*; do
    [ -e "$l" ] || continue
    printf "%s|%s|%s\n" "$(basename "$l")" "$l" "$(readlink -f "$l" 2>/dev/null)"
  done
fi

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
  # Even test -e blocks in D state on a hung mount, so every touch is bounded.
  timeout 6 test -e "$p"; rc=$?
  if [ $rc -eq 124 ]; then printf "%s|STALE|||\n" "$p"; return; fi
  if [ $rc -ne 0 ]; then printf "%s|MISSING|||\n" "$p"; return; fi
  line=$(timeout 6 df -h --output=size,avail,pcent "$p" 2>/dev/null | tail -1)
  size=$(echo "$line" | awk '{{print $1}}'); avail=$(echo "$line" | awk '{{print $2}}')
  pct=$(echo "$line" | awk '{{print $3}}')
  w=$(timeout 6 test -w "$p" && echo yes || echo no)
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
    (re.compile(r"^/orcd/pool/"), "capacity", "personal/group capacity pool"),
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


def parse_quota(lines: list[str]) -> list[dict[str, str]]:
    """Parse ORCD's per-user quota report.

    Format is a fixed-width table:
        HOME    |       71.0 |      200.0 |  35.48 | 277.2K |  1.0M |  27.72
    The file-count columns matter as much as the space columns: HOME and SCRATCH
    both carry a 1M inode limit, which many-small-file workloads hit long before
    they run out of gigabytes.
    """
    out = []
    for line in lines:
        f = [x.strip() for x in line.split("|")]
        if len(f) < 7 or not f[0] or f[0].startswith("-") or f[0] == "Space":
            continue
        if not re.match(r"^[A-Z][A-Z0-9 ]*$", f[0]):
            continue
        out.append({
            "space": f[0], "used_gb": f[1], "limit_gb": f[2], "pct_space": f[3],
            "files": f[4], "file_limit": f[5], "pct_files": f[6],
        })
    return out


def load_group_config(explicit: str | None) -> dict | None:
    """Load the optional group-conventions file.

    Precedence: --group-config flag, then $ORCD_GROUP_CONFIG, then the file
    shipped with this skill (assets/sensein.json). The script is fully
    functional without one -- the config only adds group annotations, so other
    groups can drop in their own file without touching the code.
    """
    candidates = [
        explicit,
        os.environ.get("ORCD_GROUP_CONFIG"),
        str(Path(__file__).resolve().parent.parent / "assets" / "sensein.json"),
    ]
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand)
        if p.is_file():
            try:
                return json.loads(p.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                print(f"warning: could not read group config {p}: {exc}", file=sys.stderr)
                return None
    return None


def report_group_access(cfg: dict, groups: list[str]) -> None:
    """Compare configured project entitlements against the user's real groups.

    Membership is managed in WebMoira by the group's admins -- it is an admin
    action, not something orcd-help or a script can grant -- so the actionable
    output for a missing project is the WebMoira list to ask about.
    """
    have_rows, missing_rows = [], []
    gset = set(groups)
    base = cfg.get("webmoira_base", "https://groups.mit.edu/webmoira/list/")
    for proj in cfg.get("projects", []):
        member = any(g in gset for g in proj.get("unix_groups", []))
        data = ", ".join(proj.get("data", [])) or "(subtrees in shared filesystems)"
        if member:
            have_rows.append([proj["name"], data])
        else:
            wm = ", ".join(f"{base}{w}" for w in proj.get("webmoira", []))
            missing_rows.append([proj["name"], wm])

    oc.heading(f"Project access ({cfg.get('group', 'group')} config)")
    if have_rows:
        print("You are in the groups for:")
        oc.table(have_rows, ["PROJECT", "DATA"])
    if missing_rows:
        print("\nNot yet granted -- membership is a WebMoira admin action:")
        oc.table(missing_rows, ["PROJECT", "ASK ABOUT (WebMoira)"])
        print(f"\n{cfg.get('contact', 'ask your group admin')}")


def print_conventions(cfg: dict) -> None:
    conv = cfg.get("conventions") or {}
    if not conv:
        return
    print(
        "\nGroup conventions (consolidate -- check before downloading anything large):"
    )
    labels = {
        "hf_home": "Hugging Face cache (export HF_HOME=...)",
        "senselab_cache": "senselab shared cache (group-writable)",
        "models": "shared model weights",
        "datasets": "shared datasets",
        "projects": "project trees",
        "user_dirs": "personal lab space",
        "personal_scratch": "personal flash scratch (1 TB)",
        "personal_pool": "personal capacity space (1 TB)",
    }
    for key, label in labels.items():
        if key in conv:
            print(f"  {conv[key]:<44} {label}")
    if "hf_revision_policy" in conv:
        print(textwrap.fill("HF models: " + conv["hf_revision_policy"], width=78,
                            initial_indent="  ", subsequent_indent="    "))
    print("  Use symlink forms (~/orcd/...) in anything shared -- resolved shard")
    print("  paths are only correct for the person who resolved them.")


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
    ap.add_argument("--group-config", help="group conventions JSON (default: $ORCD_GROUP_CONFIG, then the skill's assets/sensein.json)")
    args = ap.parse_args()
    group_cfg = load_group_config(args.group_config)

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
    quota = parse_quota(blocks.get("QUOTA", []))
    homelinks = parse_pipe(blocks.get("HOMELINKS", []), 3)

    # The personal spaces reached through ~/orcd are authoritative: they point at
    # this user's own shard, which no amount of path guessing would find.
    paths = candidate_paths(groups, mounts, pidirs, home)
    for _name, _link, target in homelinks:
        if target and target not in paths:
            paths.append(target)
    probe = PROBE_TEMPLATE.format(paths=" ".join(f'"{p}"' for p in paths))
    try:
        praw = oc.run_remote(probe, host=args.host, timeout=240, check=False)
    except oc.OrcdError as exc:
        print(f"error probing paths: {exc}", file=sys.stderr)
        return 1

    source_of = {m[0]: m[2] for m in mounts}
    entries = []
    stale = []
    for f in parse_pipe(praw.splitlines(), 2):
        path = f[0]
        if f[1] == "MISSING":
            continue
        if f[1] == "STALE":
            stale.append(path)
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

    # Personal spaces render as their ~/orcd symlink everywhere below: that form
    # is valid for every group member, whereas a resolved shard target is only
    # correct for this user. Their size/avail come from the quota report, not
    # df -- df reports the whole shard (294T) for a space capped at 1T.
    symlink_of = {t: n for n, _l, t in homelinks if t}
    quota_by_name: dict[str, dict[str, str]] = {}
    for q in quota:
        try:
            limit_gb = float(q["limit_gb"])
            free_gb = limit_gb - float(q["used_gb"])
        except ValueError:
            continue
        avail = f"{free_gb / 1024:.1f}T" if free_gb >= 1024 else f"{free_gb:.0f}G"
        size = f"{limit_gb / 1024:.0f}T" if limit_gb >= 1024 else f"{limit_gb:.0f}G"
        quota_by_name[q["space"].lower()] = {
            "size": size,
            "avail": avail,
            "used_pct": q["pct_space"] + "%",
            "free_label": f"{avail} of {size} quota",
        }

    if args.setup:
        oc.heading("Creating your per-user directories")
        targets = [
            e["path"] for e in entries
            if e["tier"] == "flash"
            and e["writable"]
            and any(seg in e["path"] for seg in ("/scratch/", "/compute/"))
            # ~/orcd/* targets are ORCD-provisioned and already per-user;
            # appending the username again would create <user>/<user>.
            and e["path"] not in symlink_of
            and not e["path"].rstrip("/").endswith(f"/{user}")
        ]
        if not targets:
            print("No writable group flash tier found to set up.")
        else:
            # Group convention: the tree's setgid bit keeps files group-owned,
            # but "others" must be locked out explicitly (o-rwx) -- a default
            # umask leaves a freshly created directory world-readable.
            script = "set +e\n" + "\n".join(
                f'd="{t}/{user}"; if [ -d "$d" ]; then r=exists; '
                f'elif mkdir -p "$d" 2>/dev/null; then r=created; else r=failed; fi; '
                f'[ "$r" != failed ] && chmod o-rwx "$d" 2>/dev/null; '
                f'echo "$r|$d|$(stat -c %A "$d" 2>/dev/null)"'
                for t in targets
            )
            out = oc.run_remote(script, host=args.host, timeout=120, check=False)
            oc.table([l.split("|", 2) for l in out.splitlines() if "|" in l],
                     ["RESULT", "PATH", "MODE"])
            print("\nMode: group access via setgid, none for others (chmod o-rwx). Use umask 027 in jobs.")

    if args.json:
        payload = {
            "user": user, "groups": groups, "storage": entries,
            "quota": quota,
            "personal_spaces": [{"name": n, "link": l, "target": t} for n, l, t in homelinks],
        }
        if group_cfg:
            gset = set(groups)
            payload["project_access"] = {
                p["name"]: any(g in gset for g in p.get("unix_groups", []))
                for p in group_cfg.get("projects", [])
            }
            payload["group_conventions"] = group_cfg.get("conventions", {})
        print(json.dumps(payload, indent=2))
        return 0

    if quota:
        oc.heading("Your quotas (from ~/orcd/.quota, regenerated roughly every 30 min)")
        oc.table(
            [[q["space"], f"{q['used_gb']} / {q['limit_gb']}", q["pct_space"] + "%",
              f"{q['files']} / {q['file_limit']}", q["pct_files"] + "%"] for q in quota],
            ["SPACE", "GB USED / LIMIT", "%", "FILES USED / LIMIT", "%"],
        )
        print(
            "\nThe file columns bind independently of the space columns. A 1M inode\n"
            "limit is reached by an unpacked image dataset or a conda env long before\n"
            "the gigabytes run out, and the failure looks like a disk-full error."
        )

    if homelinks:
        oc.heading("Your personal spaces (~/orcd symlinks)")
        oc.table([[f"~/orcd/{n}", t] for n, _l, t in homelinks], ["SYMLINK", "RESOLVES TO"])
        print(
            "\nThese are root-managed links to your own storage. The per-user scratch\n"
            "tier is sharded across /orcd/scratch/orcd/<NNN> and your shard is not\n"
            "predictable, so always resolve ~/orcd/scratch rather than assuming a path."
        )

    oc.heading(f"Storage you can reach as {user}")
    order = {"flash": 0, "capacity": 1, "home": 2, "archive": 3, "unknown": 4, "other": 5}
    entries.sort(key=lambda e: (order.get(e["tier"], 9), e["path"]))
    rows_out = []
    for e in entries:
        path = e["path"]
        if path in symlink_of:
            name = symlink_of[path]
            q = quota_by_name.get(name, {})
            shown = f"~/orcd/{name}"
            size = q.get("size", e["size"])
            avail = q.get("avail", e["avail"])
            used = q.get("used_pct", e["used_pct"])
        else:
            shown, size, avail, used = path, e["size"], e["avail"], e["used_pct"]
        rows_out.append([
            shown, e["tier"], e["server"],
            "yes" if e["writable"] else "no",
            size, avail, used,
            "NO BACKUP" if e["no_backup_marker"] else "unmarked",
        ])
    oc.table(rows_out, ["PATH", "TIER", "SERVER", "WRITE", "SIZE", "AVAIL", "USED", "BACKUP"])
    print("\nPersonal rows (~/orcd/...) show quota-based size/avail, not the shard's df.")
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

    if group_cfg:
        report_group_access(group_cfg, groups)

    oc.heading("Where to put what")
    flash = [e for e in entries if e["tier"] == "flash" and e["writable"]]
    capacity = [e for e in entries if e["tier"] == "capacity" and e["writable"]]

    # Blindly appending the username here once produced a doubled
    # ".../satra/satra" path, hence the endswith guard for non-symlink targets.
    def display(e: dict) -> tuple[str, str]:
        path = e["path"]
        if path in symlink_of:
            name = symlink_of[path]
            q = quota_by_name.get(name)
            return f"~/orcd/{name}", q["free_label"] if q else e["avail"] + " on shard"
        if path.rstrip("/").endswith(f"/{user}"):
            return path, e["avail"]
        return f"{path}/{user}", e["avail"]

    if flash:
        print("Active job IO -- checkpoints, intermediates, datasets being read hot:")
        for e in flash:
            flag = "NOT backed up" if e["no_backup_marker"] else "backup unconfirmed"
            shown, avail = display(e)
            print(f"  {shown:<44} {avail:>18} free, {flag}")
    if capacity:
        print("\nDatasets and results worth keeping (capacity tier -- larger, slower,")
        print("and like everything except $HOME: assume NOT backed up):")
        for e in capacity:
            path = e["path"]
            if path in symlink_of:
                name = symlink_of[path]
                q = quota_by_name.get(name)
                shown = f"~/orcd/{name}"
                avail = q["free_label"] if q else e["avail"]
            else:
                shown, avail = path, e["avail"]
            print(f"  {shown:<44} {avail:>18} free")
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
    if group_cfg:
        print_conventions(group_cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
