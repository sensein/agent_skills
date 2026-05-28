# Agent Skills

This repository is a home for many named agent skills.

Each skill lives in its own flat directory under `skills/` and should include a `SKILL.md` file plus any optional `agents/`, `scripts/`, `references/`, or `assets/` needed to make the skill reliable and reusable.

Skills are kept **flat**: a skill never contains another skill's `SKILL.md` nested inside it. Agents such as Claude Code load skills from a flat skills directory and do not support nested skills, so a related set of actions (for example the `labnb-*` family) ships as several top-level skill directories rather than one skill with sub-skill folders. This keeps the whole `skills/` tree installable into Claude Code, Codex, and other agents.

## Skills

The list below is kept in alphabetical order by skill name.

| Skill | Description |
| --- | --- |
| [`duct`](./skills/duct/SKILL.md) | Wrap any command with [con/duct](https://github.com/con/duct) to capture wall-clock time, CPU, and memory usage as structured logs, so agents and reviewers can inspect what a run actually consumed. |
| [`labnb`](./skills/labnb/SKILL.md) | Create and maintain a concurrency-safe global lab notebook outside project roots, with startup summaries of related prior work, first-class idea capture and promotion, isolated experiment workspaces, focused companion skills, and append-only indexing across projects, investigations, and tasks. |
| [`labnb-idea`](./skills/labnb-idea/SKILL.md) | Record a promising but not-yet-implemented experiment idea in the shared lab notebook index. |
| [`labnb-promote`](./skills/labnb-promote/SKILL.md) | Promote a lab notebook idea into a concrete experiment with explicit budgets, source links, and provenance. |
| [`labnb-resume`](./skills/labnb-resume/SKILL.md) | Summarize prior ideas and experiments for a project slug, then choose whether to resume, promote, branch, or start new work. |
| [`labnb-run`](./skills/labnb-run/SKILL.md) | Create and run a concrete lab notebook experiment with isolated workspace, explicit budgets, and iterative logging. |

## Installing Into An Agent

Use [`scripts/install_skills.py`](./scripts/install_skills.py) to copy the flat skill directories into an agent's skills directory:

```bash
python scripts/install_skills.py --list                 # show available skills
python scripts/install_skills.py --agent claude          # install all into ~/.claude/skills
python scripts/install_skills.py --agent codex --skills labnb duct
python scripts/install_skills.py --dest /path/to/skills  # any other agent
```

The installer refuses to install a skill that contains a nested `SKILL.md`, which guards against re-introducing the structure that some agents cannot load.

## Utilities

- [`scripts/install_skills.py`](./scripts/install_skills.py): install the flat skills in this repository into an AI coding agent's skills directory (`--agent claude`/`codex` or an explicit `--dest`), validating that no skill contains nested skills.
- [`scripts/launch_agent_container.py`](./scripts/launch_agent_container.py): launch `codex` or `claude` inside a tightly-scoped Docker or Apptainer container using a reusable TOML config, including explicit auth mounts when credentials live outside the main agent state directory. See [docs/agent-container-launcher.md](./docs/agent-container-launcher.md).
