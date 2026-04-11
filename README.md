# Agent Skills

This repository is a home for many named agent skills.

Each skill lives in its own directory under `skills/` and should include a `SKILL.md` file plus any optional `agents/`, `scripts/`, `references/`, or `assets/` needed to make the skill reliable and reusable.

## Skills

The list below is kept in alphabetical order by skill name.

| Skill | Description |
| --- | --- |
| [`global-lab-notebook`](./skills/global-lab-notebook/SKILL.md) | Create and maintain a concurrency-safe global lab notebook outside project roots, with an autoresearch-style improvement loop, unique experiment directories, append-only indexing, and per-experiment logs for work that spans multiple repos or tasks. |

## Utilities

- [`scripts/launch_agent_container.py`](./scripts/launch_agent_container.py): launch `codex` or `claude` inside a tightly-scoped Docker or Apptainer container using a reusable TOML config, including explicit auth mounts when credentials live outside the main agent state directory. See [docs/agent-container-launcher.md](./docs/agent-container-launcher.md).
