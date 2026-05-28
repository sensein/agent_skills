#!/usr/bin/env python3
"""Validate that every skill in this repository is loadable by SKILL.md agents.

This is a deterministic, offline check of the cross-agent Agent Skills format
(https://developers.openai.com/codex/skills and the Claude Code skills format).
It is meant to run in CI without any human interaction, API keys, or network:

  - each skill is a flat directory with a top-level SKILL.md
  - SKILL.md has YAML frontmatter with non-empty ``name`` and ``description``
  - the frontmatter ``name`` matches the skill's directory name
  - no SKILL.md is nested inside another skill (agents like Claude Code that
    load a flat skills directory cannot install nested skills)
  - an optional ``agents/openai.yaml`` parses and uses only known top-level keys
  - script paths referenced from SKILL.md as ``skills/<name>/scripts/...`` exist

Exit code is non-zero if any skill fails, and every problem is printed.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"

KNOWN_OPENAI_KEYS = {"interface", "policy", "dependencies"}
SCRIPT_REF = re.compile(r"skills/[A-Za-z0-9_.-]+/scripts/[A-Za-z0-9_./-]+\.py")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Parse a minimal ``key: value`` YAML frontmatter block."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip("\n")
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line or line[0] in " \t":
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("'\"")
    return fields


def validate_skill(skill_dir: Path) -> list[str]:
    problems: list[str] = []
    name = skill_dir.name
    skill_md = skill_dir / "SKILL.md"

    if not skill_md.is_file():
        return [f"{name}: missing SKILL.md"]

    nested = [
        p for p in skill_dir.rglob("SKILL.md") if p != skill_md
    ]
    for path in nested:
        problems.append(f"{name}: nested skill at {path.relative_to(SKILLS_ROOT)}")

    text = skill_md.read_text()
    fields = parse_frontmatter(text)
    if not fields.get("name"):
        problems.append(f"{name}: SKILL.md frontmatter missing 'name'")
    elif fields["name"] != name:
        problems.append(
            f"{name}: frontmatter name '{fields['name']}' != directory '{name}'"
        )
    if not fields.get("description"):
        problems.append(f"{name}: SKILL.md frontmatter missing 'description'")

    openai_yaml = skill_dir / "agents" / "openai.yaml"
    if openai_yaml.is_file():
        problems.extend(_validate_openai_yaml(name, openai_yaml))

    for match in SCRIPT_REF.findall(text):
        if not (REPO_ROOT / match).is_file():
            problems.append(f"{name}: SKILL.md references missing script {match}")

    return problems


def _validate_openai_yaml(name: str, path: Path) -> list[str]:
    try:
        import yaml  # type: ignore
    except ImportError:
        # PyYAML may be absent; fall back to a top-level key sniff.
        top_keys = {
            line.split(":", 1)[0].strip()
            for line in path.read_text().splitlines()
            if line and line[0] not in " \t#" and ":" in line
        }
        unknown = top_keys - KNOWN_OPENAI_KEYS
        return (
            [f"{name}: agents/openai.yaml unknown top-level keys: {sorted(unknown)}"]
            if unknown
            else []
        )

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return [f"{name}: agents/openai.yaml does not parse: {exc}"]
    if not isinstance(data, dict):
        return [f"{name}: agents/openai.yaml must be a mapping"]
    unknown = set(data) - KNOWN_OPENAI_KEYS
    return (
        [f"{name}: agents/openai.yaml unknown top-level keys: {sorted(unknown)}"]
        if unknown
        else []
    )


def discover_skills(skills_root: Path = SKILLS_ROOT) -> list[Path]:
    return sorted(
        child
        for child in skills_root.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    )


def main() -> int:
    skills = discover_skills()
    if not skills:
        print("error: no skills found under skills/", file=sys.stderr)
        return 2

    all_problems: list[str] = []
    for skill_dir in skills:
        problems = validate_skill(skill_dir)
        status = "FAIL" if problems else "ok"
        print(f"[{status}] {skill_dir.name}")
        all_problems.extend(problems)

    if all_problems:
        print("\nProblems:", file=sys.stderr)
        for problem in all_problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"\nAll {len(skills)} skills valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
