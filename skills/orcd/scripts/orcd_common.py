#!/usr/bin/env python3
"""Shared SSH plumbing for the ORCD skill.

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
    if not ssh_available():
        return False
    proc = subprocess.run(
        ["ssh", *SSH_OPTS, "-O", "check", host],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def open_master(host: str = DEFAULT_HOST, timeout: int = 90) -> tuple[bool, str]:
    """Establish the shared master connection, inheriting the terminal.

    The terminal is inherited on purpose: if Duo device trust has lapsed the user
    gets a real prompt here and can answer it. Returns ``(ok, message)``.
    """
    if not ssh_available():
        return False, "no `ssh` binary found on PATH"
    if master_is_live(host):
        return True, "master connection already live"
    try:
        proc = subprocess.run(
            [*_base_cmd(host), "true"],
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

    if proc.returncode != 0 and check:
        stderr = (proc.stderr or "").strip()
        if "keyboard-interactive" in stderr or "Permission denied" in stderr:
            raise OrcdError(
                "SSH authentication failed. Your key was offered but the second "
                "factor was not satisfied.\n"
                f"  Fix: sign in at {OOD_URL} (Duo), then retry.\n"
                "  Details: " + stderr
            )
        raise OrcdError(
            f"remote command failed (exit {proc.returncode}) on {host}: {stderr}"
        )
    return proc.stdout


def scp_to(local: str, remote_path: str, host: str = DEFAULT_HOST, timeout: int = 120) -> None:
    """Copy a local file to the cluster over the same multiplexed connection."""
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
