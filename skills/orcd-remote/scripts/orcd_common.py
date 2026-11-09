#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Shared SSH plumbing for the ORCD skill; runs commands locally when already on ORCD.

Every other script in this skill runs *locally* and reaches the cluster through
one multiplexed SSH connection. Two facts about ORCD's login nodes drive the
design here:

1. Auth is ``publickey`` **plus** ``keyboard-interactive``. The key alone leaves
   the session in "partial success". Duo then usually answers with zero prompts
   (because a recent OOD login established device trust), so the whole exchange
   is silent -- but it is still a keyboard-interactive round trip. Passing
   ``BatchMode=yes`` disables keyboard-interactive on the client and therefore
   *always* fails with ``Permission denied (keyboard-interactive)``. Never set it.

2. Because of (1), the first connection is the expensive and occasionally
   interactive one. A ``ControlMaster`` socket makes every later call reuse it,
   so a whole session costs one authentication.

Import this module from a sibling script; Python puts the script's directory on
``sys.path``, so ``import orcd_common`` just works.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys

DEFAULT_HOST = os.environ.get("ORCD_HOST", "orcd")
DEFAULT_HOSTNAME = os.environ.get("ORCD_HOSTNAME", "orcd-login.mit.edu")
OOD_URL = "https://orcd-ood.mit.edu/"

# Options applied to every call. Deliberately no BatchMode -- see module docstring.
SSH_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", "ControlPath=~/.ssh/cm-%r@%h:%p",
    "-o", "ControlPersist=12h",
    "-o", "ConnectTimeout=15",
    "-o", "PreferredAuthentications=publickey,keyboard-interactive",
    "-o", "NumberOfPasswordPrompts=1",
    "-o", "LogLevel=ERROR",
]


def on_cluster() -> bool:
    """True when this process is already on ORCD, so commands run locally and ssh is not used.

    Rule: ``sbatch``, ``sinfo`` and ``squeue`` are all on PATH (the Slurm client
    is installed only on the cluster's login and compute nodes). Override with
    ``ORCD_MODE=local`` or ``ORCD_MODE=remote``.
    """
    mode = os.environ.get("ORCD_MODE", "").strip().lower()
    if mode in ("local", "remote"):
        return mode == "local"
    return all(shutil.which(c) for c in ("sbatch", "sinfo", "squeue"))


def cluster_uv() -> str:
    """Path of uv on the cluster: ``$HOME/.local/bin/uv`` if executable, else uv on PATH, else ""."""
    p = os.path.join(os.path.expanduser("~"), ".local", "bin", "uv")
    if os.access(p, os.X_OK):
        return p
    return shutil.which("uv") or ""


def require_uv() -> None:
    """On the cluster, stop with the install step unless uv exists and the interpreter is uv's.

    Never falls back to the system python3 (3.6 on the login nodes). Off the
    cluster this does nothing.
    """
    if not on_cluster():
        return
    if not cluster_uv():
        print("error: uv is not installed on this cluster account. Install it (writes only to "
              "$HOME/.local/bin, edits no shell profile):\n"
              "  python3 orcd_uv.py --install   (from a laptop, over ssh)\n"
              "  or: curl -LsSf https://astral.sh/uv/install.sh | sh\n"
              "then run scripts as: $HOME/.local/bin/uv run <script>", file=sys.stderr)
        sys.exit(1)
    if sys.version_info < (3, 10):
        print("error: run this script with uv, not the system python3 (%d.%d):\n"
              "  $HOME/.local/bin/uv run %s" % (sys.version_info[0], sys.version_info[1], sys.argv[0]),
              file=sys.stderr)
        sys.exit(1)


class OrcdError(RuntimeError):
    """A cluster call could not be completed."""


def ssh_available() -> bool:
    return shutil.which("ssh") is not None


def _base_cmd(host: str) -> list[str]:
    return ["ssh", *SSH_OPTS, host]


def master_is_live(host: str = DEFAULT_HOST) -> bool:
    """True when a multiplexed master socket is already usable.

    ``-O check`` asks the running master to confirm itself and never
    authenticates, so this is a cheap, side-effect-free probe.
    """
    if on_cluster():
        return True
    if not ssh_available():
        return False
    proc = subprocess.run(
        ["ssh", *SSH_OPTS, "-O", "check", host],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def open_master(host: str = DEFAULT_HOST, timeout: int = 90, identity=None) -> tuple[bool, str]:
    """Establish the shared master connection, inheriting the terminal.

    The terminal is inherited on purpose: if Duo device trust has lapsed the user
    gets a real prompt here and can answer it. ``identity`` pins a key file for
    targets that have no config block yet. Returns ``(ok, message)``.
    """
    if on_cluster():
        return True, "running on the cluster; no ssh needed"
    if not ssh_available():
        return False, "no `ssh` binary found on PATH"
    if master_is_live(host):
        return True, "master connection already live"
    key_opts = ["-i", str(identity), "-o", "IdentitiesOnly=yes"] if identity else []
    try:
        proc = subprocess.run(
            [*_base_cmd(host), *key_opts, "true"],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"timed out after {timeout}s opening a connection to {host}. "
            "A Duo prompt may be waiting -- run `ssh " + host + "` by hand."
        )
    if proc.returncode != 0:
        return False, f"ssh to {host} exited {proc.returncode}"
    return True, "master connection established"


def run_remote(
    script: str,
    host: str = DEFAULT_HOST,
    timeout: int = 180,
    check: bool = True,
) -> str:
    """Run a bash snippet on the login node and return its stdout.

    The snippet is shipped base64-encoded so that quoting, newlines, and ``$``
    survive intact regardless of what the caller wrote. stdin is closed so a
    stalled Duo prompt fails fast with a clear error instead of hanging forever.
    """
    if on_cluster():
        try:
            proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=timeout, cwd=os.path.expanduser("~"))
        except subprocess.TimeoutExpired as exc:
            raise OrcdError(f"local command timed out after {timeout}s") from exc
        if proc.returncode != 0 and check:
            detail = (proc.stderr or "").strip() or "\n".join((proc.stdout or "").strip().splitlines()[-3:])
            raise OrcdError(f"command failed (exit {proc.returncode}): {detail}")
        return proc.stdout
    if not ssh_available():
        raise OrcdError("no `ssh` binary found on PATH")

    payload = base64.b64encode(script.encode()).decode()
    remote = f"echo {payload} | base64 -d | bash"
    try:
        proc = subprocess.run(
            [*_base_cmd(host), remote],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OrcdError(
            f"remote command timed out after {timeout}s on {host}"
        ) from exc

    stderr = (proc.stderr or "").strip()
    if proc.returncode == 255:
        # 255 is ssh's own status, never the remote script's: auth failed or
        # the connection died. Fatal even for check=False callers, otherwise a
        # dropped master socket is indistinguishable from "no output".
        if "keyboard-interactive" in stderr or "Permission denied" in stderr:
            raise OrcdError(
                "SSH authentication failed. Your key was offered but the second "
                "factor was not satisfied.\n"
                f"  Fix: sign in at {OOD_URL} (Duo), then retry.\n"
                "  Details: " + stderr
            )
        raise OrcdError(f"ssh to {host} failed (exit 255): {stderr or 'connection lost'}")
    if proc.returncode != 0 and check:
        # Commands that redirect 2>&1 put their real error on stdout.
        detail = stderr or "\n".join((proc.stdout or "").strip().splitlines()[-3:])
        raise OrcdError(
            f"remote command failed (exit {proc.returncode}) on {host}: {detail}"
        )
    return proc.stdout


def scp_to(local: str, remote_path: str, host: str = DEFAULT_HOST, timeout: int = 120) -> None:
    """Copy a local file to the cluster over the same multiplexed connection.

    Prefer a *relative* ``remote_path`` (``.orcd-jobs/foo.sh``): modern scp
    speaks SFTP, which never passes the remote path through a shell, so
    ``$HOME/...`` arrives as a literal directory named ``$HOME`` and fails.
    Relative paths need no expansion at all -- both SFTP and legacy scp resolve
    them against the login home directory. A leading ``~/`` is normalised here
    to relative for the same reason, rather than trusting the expand-path
    extension to exist on both ends.
    """
    if on_cluster():
        rel = remote_path[2:] if remote_path.startswith("~/") else remote_path
        dest = os.path.join(os.path.expanduser("~"), rel)
        if os.path.abspath(local) != os.path.abspath(dest):
            shutil.copyfile(local, dest)
        return
    if remote_path.startswith("~/"):
        remote_path = remote_path[2:]
    if "$" in remote_path:
        raise OrcdError(
            f"refusing remote path {remote_path!r}: scp's SFTP mode does not "
            "expand shell variables; use a relative path instead"
        )
    proc = subprocess.run(
        ["scp", *SSH_OPTS, local, f"{host}:{remote_path}"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise OrcdError(f"scp failed: {(proc.stderr or '').strip()}")


# ---------------------------------------------------------------------------
# Small output helpers, so every script in the skill prints the same shapes.
# ---------------------------------------------------------------------------

def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m" if sys.stdout.isatty() else f"\n{text}")
    print("-" * len(text))


def table(rows: list[list[str]], headers: list[str]) -> None:
    """Print a left-aligned table sized to its contents."""
    if not rows:
        print("(nothing to show)")
        return
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        print(fmt.format(*(str(c) for c in padded[: len(headers)])))


def parse_kv_blocks(text: str, sentinel: str = "@@") -> dict[str, list[str]]:
    """Split remote output into named sections.

    Remote scripts emit ``@@SECTION`` markers; this turns the stream into
    ``{"SECTION": [lines...]}`` so callers never re-parse ad hoc.
    """
    blocks: dict[str, list[str]] = {}
    current = "_"
    for line in text.splitlines():
        if line.startswith(sentinel):
            current = line[len(sentinel):].strip()
            blocks.setdefault(current, [])
            continue
        blocks.setdefault(current, []).append(line)
    return blocks
