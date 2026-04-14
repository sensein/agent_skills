# Agent Skills

This repository is a home for many named agent skills.

Each skill lives in its own directory under `skills/` and should include a `SKILL.md` file plus any optional `agents/`, `scripts/`, `references/`, or `assets/` needed to make the skill reliable and reusable.

## Skills

The list below is kept in alphabetical order by skill name.

| Skill | Description |
| --- | --- |
| [`labnb`](./skills/labnb/SKILL.md) | Create and maintain a concurrency-safe global lab notebook outside project roots, with startup summaries of related prior work, first-class idea capture and promotion, isolated experiment workspaces, focused subskills, and append-only indexing across projects, investigations, and tasks. |
| [`labnb-idea`](./skills/labnb-idea/SKILL.md) | Record a promising but not-yet-implemented experiment idea in the shared lab notebook index. |
| [`labnb-promote`](./skills/labnb-promote/SKILL.md) | Promote a lab notebook idea into a concrete experiment with explicit budgets, source links, and provenance. |
| [`labnb-resume`](./skills/labnb-resume/SKILL.md) | Summarize prior ideas and experiments for a project slug, then choose whether to resume, promote, branch, or start new work. |
| [`labnb-run`](./skills/labnb-run/SKILL.md) | Create and run a concrete lab notebook experiment with isolated workspace, explicit budgets, and iterative logging. |

## Utilities

- [`scripts/launch_agent_container.py`](./scripts/launch_agent_container.py): launch `codex` or `claude` inside a tightly-scoped Docker or Apptainer container using a reusable TOML config, including explicit auth mounts when credentials live outside the main agent state directory. See [docs/agent-container-launcher.md](./docs/agent-container-launcher.md).
