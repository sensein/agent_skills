#!/usr/bin/env python3
"""Check that ORCD access works, and say exactly what to fix when it does not.

Run this first, every time. It walks the chain of preconditions in dependency
order and stops at the first broken link, because a later check cannot be
meaningfully interpreted while an earlier one is failing.

    python3 orcd_doctor.py                  # diagnose
    python3 orcd_doctor.py --fix            # also write ~/.ssh/config and open the master
    python3 orcd_doctor.py --sandbox-setup  # sandbox with internet: mint a key and
                                            # print the command that authorizes it

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
import time
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


def find_identity(explicit: str | None = None) -> tuple[Path | None, list[Path]]:
    """Pick the key to offer, preferring modern algorithms.

    Returns ``(chosen, all_found)``. ed25519 comes first because ORCD's sshd
    advertises it and it avoids the SHA-1 signature pitfalls of ancient RSA keys.
    ``explicit`` (``--identity``) overrides the search for users whose ORCD key
    is not the first one found.
    """
    if explicit:
        p = Path(explicit).expanduser()
        return (p if p.is_file() else None), ([p] if p.is_file() else [])
    candidates = ["id_ed25519", "id_ecdsa", "id_rsa"]
    found = [SSH_DIR / c for c in candidates if (SSH_DIR / c).is_file()]
    return (found[0] if found else None), found


def ssh_effective(alias: str, config: Path | None = None) -> dict[str, str]:
    """Effective client options for ``alias`` from ``ssh -G``.

    ``-G`` merges Include files, Match blocks and ``Host *`` wildcards, all of
    which a regex over one file misses. ``config`` pins a file (for tests);
    None uses ssh's normal search.
    """
    cmd = ["ssh", "-G", *(["-F", str(config)] if config else []), alias]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        k, _, v = line.partition(" ")
        out[k.lower()] = v.strip()
    return out


def config_has_host(alias: str, config: Path | None = None) -> bool:
    """``alias`` has a config block.

    A configured alias normally resolves to a different HostName. When the
    alias *is* the FQDN (the doctor's block registers both), fall back to the
    marker that block always sets, ``ControlMaster auto`` -- otherwise every
    --fix run would append another copy.
    """
    eff = ssh_effective(alias, config)
    hn = eff.get("hostname", "")
    if not hn:
        return False
    return hn.lower() != alias.lower() or eff.get("controlmaster") == "auto"


def config_has_batchmode(alias: str, config: Path | None = None) -> bool:
    """The single most common misconfiguration for this cluster, wherever it hides."""
    return ssh_effective(alias, config).get("batchmode") == "yes"


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
        f"No passwords over ssh: install the key through the portal (Duo works there).\n\n"
        f"  1. Copy {pub}  (pbcopy / xclip -sel clip)\n"
        f"  2. Sign in at {oc.OOD_URL} -> Clusters -> Shell Access\n"
        f"  3. In that shell:  mkdir -p ~/.ssh && chmod 700 ~/.ssh\n"
        f"                     cat >> ~/.ssh/authorized_keys   # paste, Ctrl-D\n"
        f"                     chmod 600 ~/.ssh/authorized_keys\n"
        f"  4. Here:           ssh {user}@{hostname} hostname\n\n"
        f"Keep the browser signed in for that first ssh; its Duo trust makes ssh silent.\n\n"
        "Cloud/sandbox session (Claude Code on the web, CI)? This key lives in an\n"
        "ephemeral container and authorizing it grants the container account access:\n"
        "owner's OK first, dedicated identifiable key, revoke when retired, never copy\n"
        "the private key out. With ssh egress, `orcd_doctor.py --sandbox-setup` mints\n"
        "the key and prints the exact authorize command for the owner.\n"
    )


def egress_blocked_message(hostname: str) -> None:
    oc.heading("SSH egress is blocked")
    print(
        f"`{hostname}` resolves but nothing answers on port 22: the network drops ssh.\n"
        "Keys and Duo are not the problem; installing a key will not help from here.\n"
        "Causes: a cloud sandbox whose policy allows only HTTPS egress (an HTTPS proxy\n"
        "may even answer 200 to CONNECT :22 yet never deliver an SSH banner) -- loosen\n"
        "the policy or run from a machine with ssh access; or a campus/corporate\n"
        "firewall -- try the MIT VPN.\n"
    )


def sandbox_setup(user: str, hostname: str, identity_path: str | None = None) -> int:
    """Prepare a sandbox that has internet access to reach ORCD.

    Verifies SSH egress first (no point minting a key the network can never
    present), ensures a dedicated identifiable key exists in this environment,
    and prints the exact command the ACCOUNT OWNER runs on ORCD to authorize
    it -- plus the command that revokes it later. Adding the key is the
    owner's action, never the agent's: handing over the command is the
    authorization step.
    """
    if not oc.ssh_available():
        print("no `ssh` on PATH; install OpenSSH first (e.g. apt-get install openssh-client)",
              file=sys.stderr)
        return 1
    try:
        socket.getaddrinfo(hostname, 22)
    except socket.gaierror as exc:
        print(f"{hostname} does not resolve: {exc.strerror or exc}", file=sys.stderr)
        return 1
    try:
        socket.create_connection((hostname, 22), timeout=10).close()
    except OSError:
        egress_blocked_message(hostname)
        blocked_key, _ = find_identity()
        if blocked_key is not None:
            print(
                "If you still want to pre-authorize this environment anyway, its public\n"
                f"key is at {blocked_key}.pub -- but note that a future sandbox will carry\n"
                "a different key, so this only helps if THIS session later gains egress.\n"
            )
        return 1

    identity, _ = find_identity(identity_path)
    created = False
    if identity is None:
        SSH_DIR.mkdir(mode=0o700, exist_ok=True)
        identity = SSH_DIR / "id_ed25519"
        comment = f"orcd-sandbox-{user or 'agent'}-{time.strftime('%Y%m%d')}"
        proc = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(identity), "-q"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(f"ssh-keygen failed: {(proc.stderr or '').strip()}", file=sys.stderr)
            return 1
        created = True

    pub_path = Path(f"{identity}.pub")
    if not pub_path.is_file():
        print(f"{identity} has no matching {pub_path}; regenerate the key pair", file=sys.stderr)
        return 1
    pubkey = pub_path.read_text().strip()
    if "'" in pubkey or "\n" in pubkey:
        print(f"{pub_path} does not look like a single-line public key; append it manually",
              file=sys.stderr)
        return 1

    oc.heading("Authorize this sandbox on ORCD")
    origin = "newly generated, no passphrase (headless)" if created else "already present"
    print(
        f"Port 22 to {hostname} is reachable; this sandbox connects once its key is\n"
        f"authorized. Key: {identity} ({origin}).\n\n"
        "ACCOUNT OWNER -- if you approve this environment's access, run in an ORCD\n"
        f"shell ({oc.OOD_URL} -> Clusters -> Shell Access, or any ssh session):\n\n"
        f"    mkdir -p ~/.ssh && chmod 700 ~/.ssh && printf '%s\\n' '{pubkey}' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys\n"
    )
    parts = pubkey.split()
    tag = parts[2] if len(parts) >= 3 else ""
    if re.fullmatch(r"[A-Za-z0-9@._-]+", tag or ""):
        print(f"Revoke later with:  sed -i '/{tag}/d' ~/.ssh/authorized_keys\n")
    else:
        print("Revoke later by deleting this key's line from ~/.ssh/authorized_keys.\n")
    print(
        f"Then, from here:  python3 orcd_doctor.py --fix --user {user or '<username>'}\n"
        "The private key dies with the container; mint a fresh one next time, never copy it out."
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=oc.DEFAULT_HOST, help="ssh alias to use (default: %(default)s)")
    ap.add_argument("--hostname", default=oc.DEFAULT_HOSTNAME, help="real login hostname")
    ap.add_argument("--user", default=None,
                    help="your MIT/ORCD username (default: $ORCD_USER, then local $USER)")
    ap.add_argument("--identity", help="private key to use (default: first of id_ed25519/id_ecdsa/id_rsa)")
    ap.add_argument("--fix", action="store_true", help="write ~/.ssh/config and open the master connection")
    ap.add_argument("--sandbox-setup", action="store_true",
                    help="sandbox with internet access: verify egress, mint a dedicated key "
                         "if none exists, and print the command the account owner runs on "
                         "ORCD to authorize this environment")
    args = ap.parse_args()

    # The local login name is a guess at the ORCD username -- wrong in most
    # sandboxes (root, runner) -- so say when it is being used.
    user_guessed = False
    if not args.user:
        args.user = os.environ.get("ORCD_USER", "")
        if not args.user:
            args.user = os.environ.get("USER", "")
            user_guessed = bool(args.user)

    if args.sandbox_setup:
        if not args.user or user_guessed:
            print("error: --sandbox-setup needs the ORCD username: pass --user <mit-username>",
                  file=sys.stderr)
            return 1
        return sandbox_setup(args.user, args.hostname, args.identity)
    if args.fix and not args.user:
        # An empty `User` line makes ssh reject its config for every host.
        print("error: --fix needs a username (--user or $ORCD_USER)", file=sys.stderr)
        return 1
    if user_guessed:
        print(f"note: using local login '{args.user}' as the ORCD username; pass --user if it differs.")

    rep = Report()
    failure_detail = ""

    # 1. ssh client present -- nothing else can be checked without it.
    if not oc.ssh_available():
        rep.add(BAD, "ssh client", "no `ssh` on PATH; install OpenSSH")
        rep.render()
        return 1
    rep.add(OK, "ssh client", "found")

    # 2. A key to offer.
    identity, all_keys = find_identity(args.identity)
    if identity is None:
        rep.add(BAD, "ssh key", f"{args.identity} not found" if args.identity
                else f"none of id_ed25519/id_ecdsa/id_rsa in {SSH_DIR}")
    else:
        others = [k.name for k in all_keys[1:]]
        extra = f" (also present: {', '.join(others)})" if others else ""
        rep.add(OK, "ssh key", f"{identity.name}{extra}")

    # 3. ~/.ssh/config entry, and the BatchMode trap (via `ssh -G`, so a
    # `Host *` block or an Include'd file is seen too).
    configured = config_has_host(args.host)
    if configured:
        rep.add(OK, f"ssh config [{args.host}]", "present")
    elif args.fix and identity is not None:
        block = write_config(args.host, args.hostname, args.user, identity)
        configured = True
        rep.add(OK, f"ssh config [{args.host}]", "written by --fix")
        print("Appended to ~/.ssh/config:" + block)
    else:
        rep.add(WARN, f"ssh config [{args.host}]", "missing; re-run with --fix to write it")
    # Regardless of the alias: a `Host *` block or an Include'd file can set
    # it, and `ssh -G` reports the effective value either way.
    if config_has_batchmode(args.host):
        rep.add(BAD, "ssh BatchMode",
                "effective `BatchMode yes` -- remove it; it breaks Duo keyboard-interactive")

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
    elif not configured and not args.user:
        rep.add(BAD, "login node reachable", "skipped: no username (pass --user or set ORCD_USER)")
        failure_detail = "no username"
    else:
        target = args.host if configured else f"{args.user}@{args.hostname}"
        if oc.master_is_live(target):
            rep.add(OK, "connection multiplexing", "master socket already live")
            reachable = True
        else:
            # Without a config block the key must be named explicitly, or ssh
            # offers only the default files and an --identity key never gets used.
            key = identity if (args.identity or not configured) else None
            ok, msg = oc.open_master(target, identity=key)
            if ok:
                rep.add(OK, "login node reachable", msg)
                reachable = True
            else:
                rep.add(BAD, "login node reachable", msg)
                failure_detail = msg

    # 6. Only if we got in: confirm the cluster side looks sane.
    if reachable:
        target = args.host if configured else f"{args.user}@{args.hostname}"
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
            egress_blocked_message(args.hostname)
        elif needs_key:
            print_key_instructions(identity, user, args.hostname)
        elif unreachable:
            # A local key exists, so lead with the cause the error points at
            # rather than dumping the whole walkthrough at someone whose only
            # problem is DNS or VPN.
            low = failure_detail.lower()
            if "no username" in low:
                oc.heading("No username")
                print(f"Pass --user <mit-username> (or set ORCD_USER); then --fix writes the alias.\n")
            elif "resolve" in low:
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
                    "Likeliest first: (1) key not installed on the cluster -- see below;\n"
                    f"(2) Duo trust lapsed -- sign in at {oc.OOD_URL} and retry;\n"
                    "(3) `BatchMode yes` somewhere in ssh config -- it always breaks Duo.\n"
                    f"`ssh -vv {args.host} true` showing `partial success` means the key is\n"
                    "fine and the problem is Duo.\n"
                )
                print_key_instructions(identity, user, args.hostname)
        return 1

    if not configured:
        # Reachable as user@host, but every other script defaults to the alias.
        print(
            f"\nAccess works, but the `{args.host}` alias is not configured, and the other\n"
            f"scripts default to it. Run `python3 orcd_doctor.py --fix --user {args.user}`\n"
            f"once, or pass --host {args.user}@{args.hostname} to each of them.\n"
        )
        return 0
    print(
        "\nAccess is working. Next:\n"
        "  python3 orcd_resources.py     # what you can actually run on\n"
        "  python3 orcd_storage.py       # where to put data, and what is fast\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
