#!/usr/bin/env python3
"""Check that ORCD access works, and say exactly what to fix when it does not.

Run this first, every time. It walks the chain of preconditions in dependency
order and stops at the first broken link, because a later check cannot be
meaningfully interpreted while an earlier one is failing.

    python3 orcd_doctor.py            # diagnose
    python3 orcd_doctor.py --fix      # also write ~/.ssh/config and open the master

Exit status is 0 when the cluster is reachable and 1 otherwise, so callers can
gate on it.
"""
from __future__ import annotations

import argparse
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import orcd_common as oc

SSH_DIR = Path.home() / ".ssh"
SSH_CONFIG = SSH_DIR / "config"

CONFIG_BLOCK_TEMPLATE = """
Host {alias} {hostname}
    HostName {hostname}
    User {user}
    IdentityFile {identity}
    IdentitiesOnly yes
    # ORCD requires publickey AND keyboard-interactive (Duo). Never add
    # BatchMode -- it disables keyboard-interactive and auth always fails.
    PreferredAuthentications publickey,keyboard-interactive
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 12h
    ServerAliveInterval 60
"""

OK, WARN, BAD = "ok", "warn", "bad"
MARK = {OK: "PASS", WARN: "WARN", BAD: "FAIL"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.fatal = False

    def add(self, status: str, name: str, detail: str = "") -> None:
        self.rows.append((MARK[status], name, detail))
        if status == BAD:
            self.fatal = True

    def render(self) -> None:
        oc.heading("ORCD access check")
        oc.table([list(r) for r in self.rows], ["", "CHECK", "DETAIL"])


def find_identity() -> tuple[Path | None, list[Path]]:
    """Pick the key to offer, preferring modern algorithms.

    Returns ``(chosen, all_found)``. ed25519 comes first because ORCD's sshd
    advertises it and it avoids the SHA-1 signature pitfalls of ancient RSA keys.
    """
    candidates = ["id_ed25519", "id_ecdsa", "id_rsa"]
    found = [SSH_DIR / c for c in candidates if (SSH_DIR / c).is_file()]
    return (found[0] if found else None), found


def config_has_host(alias: str) -> bool:
    if not SSH_CONFIG.is_file():
        return False
    pattern = re.compile(rf"^\s*Host\s+.*\b{re.escape(alias)}\b", re.MULTILINE)
    return bool(pattern.search(SSH_CONFIG.read_text()))


def config_has_batchmode(alias: str) -> bool:
    """Detect the single most common misconfiguration for this cluster."""
    if not SSH_CONFIG.is_file():
        return False
    text = SSH_CONFIG.read_text()
    match = re.search(
        rf"^\s*Host\s+.*\b{re.escape(alias)}\b(.*?)(?=^\s*Host\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return False
    return re.search(r"^\s*BatchMode\s+yes", match.group(1), re.MULTILINE | re.IGNORECASE) is not None


def write_config(alias: str, hostname: str, user: str, identity: Path) -> str:
    SSH_DIR.mkdir(mode=0o700, exist_ok=True)
    block = CONFIG_BLOCK_TEMPLATE.format(
        alias=alias, hostname=hostname, user=user, identity=identity
    )
    with SSH_CONFIG.open("a") as fh:
        fh.write(block)
    SSH_CONFIG.chmod(0o600)
    return block


def print_key_instructions(identity: Path | None, user: str, hostname: str) -> None:
    oc.heading("Install your public key on ORCD")
    if identity is None:
        print(
            "You have no SSH key yet. Create one (accept the default path, and set\n"
            "a passphrase -- your agent will cache it):\n\n"
            "    ssh-keygen -t ed25519 -C \"$USER@mit.edu\"\n"
        )
        pub = "~/.ssh/id_ed25519.pub"
    else:
        pub = f"{identity}.pub"

    print(
        f"ORCD does not accept a password over SSH, so the key has to be installed\n"
        f"through the web portal, which does support Duo two-factor:\n\n"
        f"  1. Copy your public key to the clipboard:\n\n"
        f"         pbcopy < {pub}          # macOS\n"
        f"         xclip -sel clip < {pub} # Linux\n\n"
        f"  2. Open {oc.OOD_URL} and sign in with your MIT\n"
        f"     credentials plus Duo.\n\n"
        f"  3. In the top menu choose  Clusters -> Shell Access.  You now have a\n"
        f"     shell on a login node, already authenticated.\n\n"
        f"  4. In that shell, paste your key into authorized_keys:\n\n"
        f"         mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        f"         cat >> ~/.ssh/authorized_keys    # paste, then press Ctrl-D\n"
        f"         chmod 600 ~/.ssh/authorized_keys\n\n"
        f"  5. Back on your laptop, verify:\n\n"
        f"         ssh {user}@{hostname} hostname\n\n"
        f"Leave the browser session signed in for that first SSH. The Duo device\n"
        f"trust it establishes is what lets SSH finish without prompting you.\n"
    )
    print(
        "NOTE if this session runs in a cloud or remote agent environment (Claude\n"
        "Code on the web, a CI runner, a devcontainer) rather than on your own\n"
        "machine: the key pair above lives in that environment, and installing its\n"
        "public key gives that environment SSH access to your ORCD account. Get the\n"
        "account owner's explicit OK first, use a dedicated key with an identifying\n"
        "comment (e.g. ssh-keygen -C \"agent-cloud-$(date +%Y%m%d)\") so it is easy\n"
        "to spot, and remove that line from ~/.ssh/authorized_keys on ORCD when the\n"
        "environment is retired. Cloud containers are usually ephemeral: the private\n"
        "key may vanish when the session ends. That is normal -- generate and install\n"
        "a fresh key next time instead of copying private keys out of the container.\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=oc.DEFAULT_HOST, help="ssh alias to use (default: %(default)s)")
    ap.add_argument("--hostname", default=oc.DEFAULT_HOSTNAME, help="real login hostname")
    ap.add_argument("--user", default=os.environ.get("ORCD_USER") or os.environ.get("USER", ""),
                    help="your MIT/ORCD username (default: local $USER)")
    ap.add_argument("--fix", action="store_true", help="write ~/.ssh/config and open the master connection")
    args = ap.parse_args()

    rep = Report()
    failure_detail = ""

    # 1. ssh client present -- nothing else can be checked without it.
    if not oc.ssh_available():
        rep.add(BAD, "ssh client", "no `ssh` on PATH; install OpenSSH")
        rep.render()
        return 1
    rep.add(OK, "ssh client", "found")

    # 2. A key to offer.
    identity, all_keys = find_identity()
    if identity is None:
        rep.add(BAD, "ssh key", f"none of id_ed25519/id_ecdsa/id_rsa in {SSH_DIR}")
    else:
        others = [k.name for k in all_keys[1:]]
        extra = f" (also present: {', '.join(others)})" if others else ""
        rep.add(OK, "ssh key", f"{identity.name}{extra}")

    # 3. ~/.ssh/config entry, and the BatchMode trap.
    if config_has_host(args.host):
        if config_has_batchmode(args.host):
            rep.add(
                BAD, f"~/.ssh/config [{args.host}]",
                "has `BatchMode yes` -- remove it; it breaks Duo keyboard-interactive",
            )
        else:
            rep.add(OK, f"~/.ssh/config [{args.host}]", "present")
    elif args.fix and identity is not None:
        block = write_config(args.host, args.hostname, args.user, identity)
        rep.add(OK, f"~/.ssh/config [{args.host}]", "written by --fix")
        print("Appended to ~/.ssh/config:" + block)
    else:
        rep.add(WARN, f"~/.ssh/config [{args.host}]", "missing; re-run with --fix to write it")

    # 4. Name resolution, checked here rather than inferred from ssh's stderr:
    # open_master() inherits the terminal so Duo prompts stay visible, which
    # means its stderr is not available for classification.
    resolves = True
    try:
        socket.getaddrinfo(args.hostname, 22)
        rep.add(OK, "hostname resolves", args.hostname)
    except socket.gaierror as exc:
        resolves = False
        rep.add(BAD, "hostname resolves", f"{args.hostname}: {exc.strerror or exc}")

    # 4b. TCP to port 22, probed before any ssh attempt. Cloud agent
    # environments and locked-down networks often allow only HTTPS egress and
    # silently drop SSH; without this probe the eventual ssh failure is
    # indistinguishable from an auth problem and sends people chasing keys
    # and Duo that were never broken.
    port_blocked = False
    if resolves:
        try:
            socket.create_connection((args.hostname, 22), timeout=10).close()
            rep.add(OK, "tcp port 22", "reachable")
        except OSError as exc:
            port_blocked = True
            rep.add(BAD, "tcp port 22", f"cannot connect: {exc.strerror or exc}")

    # 5. Reachability. This is the check that actually matters.
    reachable = False
    if identity is None:
        rep.add(BAD, "login node reachable", "skipped: no key to offer")
    elif not resolves:
        rep.add(BAD, "login node reachable", "skipped: hostname does not resolve")
        failure_detail = "could not resolve"
    elif port_blocked:
        rep.add(BAD, "login node reachable", "skipped: port 22 is blocked")
        failure_detail = "port 22 blocked"
    else:
        target = args.host if config_has_host(args.host) else f"{args.user}@{args.hostname}"
        if oc.master_is_live(target):
            rep.add(OK, "connection multiplexing", "master socket already live")
            reachable = True
        else:
            ok, msg = oc.open_master(target)
            if ok:
                rep.add(OK, "login node reachable", msg)
                reachable = True
            else:
                rep.add(BAD, "login node reachable", msg)
                failure_detail = msg

    # 6. Only if we got in: confirm the cluster side looks sane.
    if reachable:
        target = args.host if config_has_host(args.host) else f"{args.user}@{args.hostname}"
        try:
            out = oc.run_remote(
                'echo "@@WHO"; hostname -s; whoami\n'
                'echo "@@SLURM"; command -v sinfo >/dev/null && sinfo --version || echo MISSING\n'
                'echo "@@ASSOC"; sacctmgr -nP show assoc user=$USER format=Account,QOS 2>/dev/null | head -5\n'
                'echo "@@GROUPS"; id -Gn | tr " " "\\n" | grep -c "^orcd_rg_" || true\n'
                'echo "@@UV"; if [ -x "$HOME/.local/bin/uv" ]; then "$HOME/.local/bin/uv" --version 2>/dev/null; '
                'elif command -v uv >/dev/null 2>&1; then uv --version 2>/dev/null; else echo MISSING; fi\n',
                host=target,
                timeout=60,
            )
        except oc.OrcdError as exc:
            rep.add(BAD, "cluster commands", str(exc).splitlines()[0])
        else:
            blocks = oc.parse_kv_blocks(out)
            who = [l for l in blocks.get("WHO", []) if l.strip()]
            if who:
                rep.add(OK, "logged in as", f"{who[-1]} on {who[0]}")
            slurm = [l for l in blocks.get("SLURM", []) if l.strip()]
            if slurm and slurm[0] != "MISSING":
                rep.add(OK, "slurm client", slurm[0])
            else:
                rep.add(BAD, "slurm client", "sinfo not found on the login node")
            assoc = [l for l in blocks.get("ASSOC", []) if l.strip()]
            if assoc:
                rep.add(OK, "slurm association", "; ".join(assoc))
            else:
                rep.add(
                    WARN, "slurm association",
                    "none found -- you may not be added to an account yet; "
                    "email orcd-help@mit.edu",
                )
            ngroups = [l for l in blocks.get("GROUPS", []) if l.strip()]
            if ngroups and ngroups[0].isdigit() and int(ngroups[0]) > 0:
                rep.add(OK, "storage groups", f"{ngroups[0]} orcd_rg_* group memberships")
            else:
                rep.add(WARN, "storage groups", "no orcd_rg_* groups; only $HOME will be writable")
            uv = [l for l in blocks.get("UV", []) if l.strip()]
            if uv and uv[0] != "MISSING":
                rep.add(OK, "uv (cluster $HOME)", uv[0])
            else:
                rep.add(
                    WARN, "uv (cluster $HOME)",
                    "not installed; `python3 orcd_uv.py --install` (never edits shell profiles)",
                )

    rep.render()

    if rep.fatal:
        needs_key = any(r[1] == "ssh key" and r[0] == MARK[BAD] for r in rep.rows)
        unreachable = any(r[1] == "login node reachable" and r[0] == MARK[BAD] for r in rep.rows)
        user = args.user or "<your-username>"

        if port_blocked:
            # Checked before the missing-key case on purpose: installing a key
            # cannot help until packets can reach the login node at all.
            oc.heading("SSH egress is blocked")
            print(
                f"`{args.hostname}` resolves, but nothing answers on port 22 -- the\n"
                "network between this machine and ORCD is dropping SSH. Keys and Duo\n"
                "are not the problem, and installing a key will not help from here.\n"
                "Common causes:\n\n"
                "  - A cloud agent environment (Claude Code on the web, a CI runner)\n"
                "    whose network policy allows only HTTP/HTTPS egress. Loosen the\n"
                "    environment's network policy, or drive ORCD from a machine with\n"
                "    direct SSH access instead. Tunneling through the environment's\n"
                "    HTTPS proxy usually fails the same way: the proxy may answer 200\n"
                "    to CONNECT host:22 yet never deliver an SSH banner, because the\n"
                "    policy is enforced on the proxy's upstream connection.\n"
                "  - A restrictive campus or corporate network; try the MIT VPN.\n"
            )
        elif needs_key:
            print_key_instructions(identity, user, args.hostname)
        elif unreachable:
            # A local key exists, so lead with the cause the error points at
            # rather than dumping the whole walkthrough at someone whose only
            # problem is DNS or VPN.
            low = failure_detail.lower()
            if "resolve" in low:
                oc.heading("Cannot resolve the login node")
                print(
                    f"`{args.hostname}` did not resolve. Check the spelling, and check\n"
                    "that you have network access to MIT (some paths require the VPN).\n"
                )
            elif "timed out" in low:
                oc.heading("Connection timed out")
                print(
                    "A Duo prompt may be waiting for an answer that a non-interactive\n"
                    f"run cannot give. Run this by hand once and respond to it:\n\n"
                    f"    ssh {args.host}\n"
                )
            else:
                oc.heading("Connected, but authentication failed")
                print(
                    "Your key was offered and refused, or the second factor did not\n"
                    "pass. In order of likelihood:\n\n"
                    f"  1. The key is not installed on the cluster yet -- see below.\n"
                    f"  2. Duo device trust has lapsed. Sign in at {oc.OOD_URL}\n"
                    "     and then retry.\n"
                    "  3. `BatchMode yes` is set for this host somewhere in your SSH\n"
                    "     config. It disables keyboard-interactive and auth always fails.\n\n"
                    "To see which stage failed, look for `partial success` in:\n\n"
                    f"    ssh -vv {args.host} true\n\n"
                    "If it appears, your key is fine and the problem is Duo.\n"
                )
                print_key_instructions(identity, user, args.hostname)
        return 1

    print(
        "\nAccess is working. Next:\n"
        "  python3 orcd_resources.py     # what you can actually run on\n"
        "  python3 orcd_storage.py       # where to put data, and what is fast\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
