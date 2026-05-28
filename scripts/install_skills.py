#!/usr/bin/env python3
"""Install the flat skills in this repository into an AI coding agent.

Each skill in ``skills/`` is a single flat directory containing a top-level
``SKILL.md`` (plus optional ``agents/``, ``scripts/``, ``references/``, and
``assets/``). Agents such as Claude Code load skills from a flat skills
directory and do not support a ``SKILL.md`` nested inside another skill, so
this installer copies whole skill directories into the target agent's skills
directory and refuses to install a skill that contains nested skills.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"

# Default per-agent skills directories, matching scripts/launch_agent_container.py.
AGENT_SKILL_DIRS = {
    "claude": Path.home() / ".claude" / "skills",
    "codex": Path.home() / ".codex" / "skills",
}


def discover_skills(skills_root: Path = SKILLS_ROOT) -> list[Path]:
    """Return skill directories: immediate children with a top-level SKILL.md."""
    return sorted(
        child
        for child in skills_root.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    )


def find_nested_skills(skill_dir: Path) -> list[Path]:
    """Return any SKILL.md files below the skill's own top-level SKILL.md."""
    return sorted(
        path
        for path in skill_dir.rglob("SKILL.md")
        if path != skill_dir / "SKILL.md"
    )


def resolve_dest(agent: str | None, dest: str | None) -> Path:
    if dest:
        return Path(dest).expanduser()
    if agent and agent in AGENT_SKILL_DIRS:
        return AGENT_SKILL_DIRS[agent]
    raise SystemExit(
        "error: specify --dest, or --agent from "
        f"{sorted(AGENT_SKILL_DIRS)}"
    )


def install(
    skills: list[Path],
    dest_root: Path,
    *,
    force: bool,
    dry_run: bool,
) -> int:
    dest_root.mkdir(parents=True, exist_ok=True)
    for skill_dir in skills:
        target = dest_root / skill_dir.name
        action = "would install" if dry_run else "installing"
        print(f"{action}: {skill_dir.name} -> {target}")
        if dry_run:
            continue
        if target.exists():
            if not force:
                print(
                    f"  skipped: {target} already exists (use --force to replace)",
                    file=sys.stderr,
                )
                continue
            shutil.rmtree(target)
        shutil.copytree(skill_dir, target)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_SKILL_DIRS),
        help="Target agent; selects a default skills directory.",
    )
    parser.add_argument(
        "--dest",
        help="Explicit skills directory to install into (overrides --agent).",
    )
    parser.add_argument(
        "--skills",
        nargs="+",
        metavar="NAME",
        help="Install only these skills by name (default: all).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List discoverable skills and exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace a skill directory that already exists at the target.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be installed without copying.",
    )
    args = parser.parse_args(argv)

    available = discover_skills()
    by_name = {skill.name: skill for skill in available}

    if args.list:
        for name in sorted(by_name):
            print(name)
        return 0

    if args.skills:
        missing = [name for name in args.skills if name not in by_name]
        if missing:
            print(f"error: unknown skills: {', '.join(missing)}", file=sys.stderr)
            return 2
        selected = [by_name[name] for name in args.skills]
    else:
        selected = available

    nested_problems = {
        skill.name: nested
        for skill in selected
        if (nested := find_nested_skills(skill))
    }
    if nested_problems:
        print(
            "error: these skills contain nested SKILL.md files and cannot be "
            "installed flatly (split them into top-level skills first):",
            file=sys.stderr,
        )
        for name, nested in nested_problems.items():
            for path in nested:
                print(f"  {name}: {path.relative_to(SKILLS_ROOT)}", file=sys.stderr)
        return 2

    dest_root = resolve_dest(args.agent, args.dest)
    return install(
        selected, dest_root, force=args.force, dry_run=args.dry_run
    )


if __name__ == "__main__":
    raise SystemExit(main())
