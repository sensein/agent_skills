#!/usr/bin/env python3
"""Check, install, or upgrade `uv` in the cluster $HOME -- never touching a
shell profile unless the user has explicitly approved that edit.

The login nodes' system ``python3`` is 3.6 and neither ``uv`` nor ``conda`` is
installed system-wide, so a per-user ``uv`` at ``~/.local/bin/uv`` in the
cluster home directory is the supported way to get modern Python for jobs.

    python3 orcd_uv.py                 # is uv installed on the cluster? on PATH?
    python3 orcd_uv.py --install       # install it, or upgrade an existing one
    python3 orcd_uv.py --add-to-path   # append a PATH line to a shell profile

The profile rule, which this script enforces rather than merely documents:
**no shell profile is modified without the user's explicit approval.**

- ``--install`` runs the official standalone installer with
  ``UV_NO_MODIFY_PATH=1``, so the installer cannot edit ``.bashrc``/``.profile``
  behind anyone's back. Upgrades use ``uv self update``. No profile is touched.
- ``--add-to-path`` prints the exact file and line first, then proceeds only
  after a typed ``yes`` on a TTY -- or with ``--user-approved``, which asserts
  the user already said yes. An agent must ask the user and get a real answer
  before passing that flag; in a non-interactive run without it, the script
  refuses and exits non-zero.

Scripts and sbatch job scripts do not need the profile edit at all: call uv by
absolute path (``$HOME/.local/bin/uv``) or export PATH inside the job script.
The profile line is for the user's interactive convenience.

Exit status: 0 when uv is usable at the end of the run, 2 when uv is not
installed (check mode), 1 on errors or a refused/failed action.
"""
from __future__ import annotations

import argparse
import json
import sys

import orcd_common as oc

PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'
MARKER = "# orcd-remote: uv on PATH (user-approved)"
PROFILES = (".bashrc", ".bash_profile", ".profile")

CHECK_SCRIPT = r'''
echo "@@HOMEBIN"
if [ -x "$HOME/.local/bin/uv" ]; then
    "$HOME/.local/bin/uv" --version 2>/dev/null || echo BROKEN
else
    echo MISSING
fi
echo "@@ONPATH"
# What a fresh non-interactive SSH command sees -- the PATH that this skill's
# own remote calls, and anything an agent runs over ssh, will actually get.
command -v uv 2>/dev/null || echo NOT_ON_PATH
echo "@@PROFILES"
for f in .bashrc .bash_profile .profile; do
    p="$HOME/$f"
    if [ -f "$p" ] && grep -qF '.local/bin' "$p"; then echo "$f"; fi
done
true
'''

INSTALL_SCRIPT = r'''
if [ -x "$HOME/.local/bin/uv" ]; then
    echo "@@MODE"; echo upgrade
    echo "@@OUT"
    "$HOME/.local/bin/uv" self update 2>&1 || echo "@@SELFUPDATEFAILED"
else
    echo "@@MODE"; echo install
    echo "@@OUT"
    # UV_NO_MODIFY_PATH=1 is the profile-approval rule in action: the installer
    # is forbidden from editing .bashrc/.profile. PATH is handled separately,
    # and only with the user's explicit approval (--add-to-path).
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh \
            | env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh \
            | env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh 2>&1
    else
        echo "neither curl nor wget found on the login node"
    fi
fi
echo "@@VERSION"
"$HOME/.local/bin/uv" --version 2>/dev/null || echo MISSING
'''


def check(host: str) -> dict:
    raw = oc.run_remote(CHECK_SCRIPT, host=host, timeout=60)
    blocks = oc.parse_kv_blocks(raw)
    first = lambda key: next((l.strip() for l in blocks.get(key, []) if l.strip()), "")
    home_bin = first("HOMEBIN")
    on_path = first("ONPATH")
    return {
        "home_bin": None if home_bin in ("MISSING", "BROKEN", "") else home_bin,
        "home_bin_state": home_bin or "MISSING",
        "on_path": None if on_path in ("NOT_ON_PATH", "") else on_path,
        "profiles_referencing_local_bin": [l.strip() for l in blocks.get("PROFILES", []) if l.strip()],
    }


def render(status: dict) -> None:
    oc.heading("uv on the cluster")
    rows = [
        ["~/.local/bin/uv", status["home_bin"] or status["home_bin_state"]],
        ["on PATH (non-interactive ssh)", status["on_path"] or "no"],
        ["profiles mentioning .local/bin", ", ".join(status["profiles_referencing_local_bin"]) or "none"],
    ]
    oc.table(rows, ["WHAT", "STATE"])


def install(host: str) -> bool:
    raw = oc.run_remote(INSTALL_SCRIPT, host=host, timeout=300)
    blocks = oc.parse_kv_blocks(raw)
    mode = next((l for l in blocks.get("MODE", []) if l.strip()), "?")
    version = next((l for l in blocks.get("VERSION", []) if l.strip()), "MISSING")
    oc.heading(f"uv {mode}")
    out = [l for l in blocks.get("OUT", []) if l.strip()]
    if out:
        print("\n".join(out[-6:]))
    if "SELFUPDATEFAILED" in blocks:
        print(
            "\n`uv self update` failed -- this uv was probably not installed by the\n"
            "standalone installer. Remove it and re-run --install, or upgrade it the\n"
            "way it was installed (pip, conda, module)."
        )
        return False
    if version == "MISSING":
        print("\nInstall did not produce a working ~/.local/bin/uv. Output above should say why.")
        return False
    print(f"\n{version} at ~/.local/bin/uv (no shell profile was modified)")
    return True


def confirm_profile_edit(profile: str, user_approved: bool) -> bool:
    """The approval gate. A profile edit happens only past this function."""
    print(
        f"\nProposed change to the CLUSTER file ~/{profile} (a one-time append):\n\n"
        f"    {MARKER}\n"
        f"    {PATH_LINE}\n\n"
        f"A backup is written to ~/{profile}.orcd-uv.bak first."
    )
    if user_approved:
        print("Proceeding: --user-approved was passed, asserting the user already agreed.")
        return True
    if sys.stdin.isatty():
        answer = input("Append this line? Only 'yes' proceeds: ")
        return answer.strip().lower() == "yes"
    print(
        "\nRefusing: shell profiles are only modified with the user's explicit\n"
        "approval, and this run is non-interactive. Ask the user, then re-run\n"
        "with --user-approved. Until then, use the absolute path:\n"
        "    $HOME/.local/bin/uv"
    )
    return False


def add_to_path(host: str, profile: str) -> dict:
    """Append the PATH line (approval already granted) and return a fresh check()."""
    edit = f'''
p="$HOME/{profile}"
if [ ! -f "$p" ]; then
    echo "@@RESULT"; echo missing
elif grep -qF '.local/bin' "$p"; then
    echo "@@RESULT"; echo already
else
    cp -p "$p" "$p.orcd-uv.bak"
    printf '\\n%s\\n%s\\n' '{MARKER}' '{PATH_LINE}' >> "$p"
    echo "@@RESULT"; echo added
fi
'''
    blocks = oc.parse_kv_blocks(oc.run_remote(edit, host=host, timeout=60))
    result = next((l for l in blocks.get("RESULT", []) if l.strip()), "?")
    if result == "missing":
        # Creating ~/.bash_profile makes bash stop reading ~/.profile at login,
        # silently dropping whatever lived there. Never create a profile.
        print(f"~/{profile} does not exist; refusing to create it (a new file changes which\n"
              "startup files bash reads). Pick an existing one with --profile.")
        return check(host)
    if result == "already":
        print(f"~/{profile} already references .local/bin; nothing appended.")
    else:
        print(f"Appended to ~/{profile} (backup at ~/{profile}.orcd-uv.bak).")

    status = check(host)
    if status["on_path"]:
        print(f"Verified: a fresh ssh command now resolves uv at {status['on_path']}.")
    else:
        print(
            f"\nLine added, but a fresh non-interactive ssh still lacks uv on PATH --\n"
            f"usually an interactivity guard near the top of ~/{profile} returns first.\n"
            "Interactive logins get it; scripts should use $HOME/.local/bin/uv."
        )
    return status


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=oc.DEFAULT_HOST, help="ssh alias to use (default: %(default)s)")
    ap.add_argument("--install", action="store_true",
                    help="install uv into the cluster ~/.local/bin, or upgrade an existing install")
    ap.add_argument("--add-to-path", action="store_true",
                    help="append the ~/.local/bin PATH line to a cluster shell profile (needs approval)")
    ap.add_argument("--profile", default=".bashrc", choices=PROFILES,
                    help="which profile --add-to-path edits (default: %(default)s)")
    ap.add_argument("--user-approved", action="store_true",
                    help="assert the user explicitly approved the profile edit; only pass after asking them")
    ap.add_argument("--json", action="store_true", help="print the check result as JSON")
    args = ap.parse_args()

    try:
        if args.install:
            if not install(args.host):
                return 1

        status = check(args.host)

        if args.add_to_path:
            if status["home_bin_state"] == "MISSING":
                print("uv is not installed yet; run --install first (they compose: --install --add-to-path).",
                      file=sys.stderr)
                return 1
            if not confirm_profile_edit(args.profile, args.user_approved):
                return 1
            status = add_to_path(args.host, args.profile)
    except oc.OrcdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("\nRun `python3 orcd_doctor.py` to diagnose access.", file=sys.stderr)
        return 1

    usable = bool(status["home_bin"] or status["on_path"])
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        render(status)
        if not usable:
            print("\nuv is not installed. Install (or later upgrade) it with:\n"
                  "    python3 orcd_uv.py --install")
        elif not status["on_path"]:
            print("\nuv works by absolute path ($HOME/.local/bin/uv). To put it on the\n"
                  "interactive PATH, ask the user first, then:\n"
                  "    python3 orcd_uv.py --add-to-path --user-approved")

    return 0 if usable else 2


if __name__ == "__main__":
    sys.exit(main())
