# Agent Skills

This repository is a home for many named agent skills.

Each skill lives in its own directory under `skills/` and should include a `SKILL.md` file plus any optional `agents/`, `scripts/`, `references/`, or `assets/` needed to make the skill reliable and reusable.

## Skills

The list below is kept in alphabetical order by skill name.

| Skill | Description |
| --- | --- |
| [`labnb`](./skills/labnb/SKILL.md) | Create and maintain a concurrency-safe global lab notebook outside project roots, with startup summaries of related prior work, first-class idea capture, isolated experiment workspaces, focused subskills, and append-only indexing across repos and tasks. |

## Utilities

- [`scripts/launch_agent_container.py`](./scripts/launch_agent_container.py): launch `codex` or `claude` inside a tightly-scoped Docker or Apptainer container using a reusable TOML config, including explicit auth mounts when credentials live outside the main agent state directory. See [docs/agent-container-launcher.md](./docs/agent-container-launcher.md).
