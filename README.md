# Agent Skills

This repository is a home for many named agent skills.

Each skill lives in its own flat directory under `skills/` and should include a `SKILL.md` file plus any optional `agents/`, `scripts/`, `references/`, or `assets/` needed to make the skill reliable and reusable.

Skills are kept **flat**: a skill never contains another skill's `SKILL.md` nested inside it. Agents such as Claude Code load skills from a flat skills directory and do not support nested skills, so a related set of actions (for example the `labnb-*` family) ships as several top-level skill directories rather than one skill with sub-skill folders. This keeps the whole `skills/` tree installable into Claude Code, Codex, and other agents.

## Skills

The list below is kept in alphabetical order by skill name.

| Skill                                                        | Description |
|--------------------------------------------------------------| --- |
| [`duct`](./skills/duct/SKILL.md)                             | Wrap any command with [con/duct](https://github.com/con/duct) to capture wall-clock time, CPU, and memory usage as structured logs, so agents and reviewers can inspect what a run actually consumed. |
| [`kya`](./skills/kya/SKILL.md)                               | Govern and review agents with [veldt-kya](https://github.com/veldtlabs/veldt-kya) (Know Your Agents) — risk-score, consensus-judge, and drift-check an agent, emit compliance evidence, and write a governance verdict that the `labnb` loop can break on. |
| [`labnb`](./skills/labnb/SKILL.md)                           | Create and maintain a concurrency-safe global lab notebook outside project roots, with startup summaries of related prior work, first-class idea capture and promotion, isolated experiment workspaces, focused companion skills, and append-only indexing across projects, investigations, and tasks. |
| [`labnb-idea`](./skills/labnb-idea/SKILL.md)                 | Record a promising but not-yet-implemented experiment idea in the shared lab notebook index. |
| [`labnb-promote`](./skills/labnb-promote/SKILL.md)           | Promote a lab notebook idea into a concrete experiment with explicit budgets, source links, and provenance. |
| [`labnb-resume`](./skills/labnb-resume/SKILL.md)             | Summarize prior ideas and experiments for a project slug, then choose whether to resume, promote, branch, or start new work. |
| [`labnb-run`](./skills/labnb-run/SKILL.md)                   | Create and run a concrete lab notebook experiment with isolated workspace, explicit budgets, and iterative logging. |
| [`orcd-remote`](./skills/orcd-remote/SKILL.md)               | Use MIT ORCD (Engaging) as a remote execution environment, driven over SSH from your own machine: set up key-based SSH through the OnDemand portal, discover the Slurm partitions, GPU models, and storage tiers the current user is actually entitled to, place job IO on flash scratch, and submit and track work. |
| [`structsense`](./skills/structsense/SKILL.md)               | StructSense Skills transforms unstructured text and PDFs into validated, ontology-grounded structured JSON using a model-agnostic extraction pipeline. |
| [`synthscholar`](./skills/synthscholar/SKILL.md)             | Set up, run, and query SynthScholar / PRISMA systematic reviews: guided protocol intake, provenance queries over a finished review (full-text vs abstract-only inclusions, retrieval routes, screening audit trail), and bring-your-own-corpus reviews over PDFs the user supplies — with missing papers retrieved from a DOI open access first (Unpaywall/OpenAlex/Semantic Scholar) and through the user's own institutional EZproxy session for what stays paywalled — exported as Markdown and SLR-ontology Turtle. |

## Installing Into An Agent

Use [`scripts/install_skills.py`](./scripts/install_skills.py) to copy the flat skill directories into an agent's skills directory:

```bash
python scripts/install_skills.py --list                  # show available skills
python scripts/install_skills.py --agent claude           # -> ~/.claude/skills
python scripts/install_skills.py --agent codex            # -> ~/.agents/skills
python scripts/install_skills.py --agent codex --scope project   # -> ./.agents/skills
python scripts/install_skills.py --agent codex --skills labnb duct
python scripts/install_skills.py --dest /path/to/skills   # any other agent
```

Skill directories follow the cross-agent [Agent Skills](https://developers.openai.com/codex/skills) format: a flat directory with a `SKILL.md` (with `name` and `description` frontmatter). The optional `agents/openai.yaml` adds Codex-specific UI metadata and invocation policy and is ignored by agents that do not use it. Default install locations:

- **Claude Code:** `~/.claude/skills/` (user) or `<project>/.claude/skills/` (project).
- **Codex:** `~/.agents/skills/` (user) or `<repo>/.agents/skills/` (project), the Agent Skills open-standard location.

The installer refuses to install a skill that contains a nested `SKILL.md`, which guards against re-introducing the structure that some agents cannot load.

## Continuous Integration

[`.github/workflows/ci.yml`](./.github/workflows/ci.yml) runs on every push and pull request, with no human interaction or API keys required:

- **Unit tests:** `uv run python -m unittest discover -s tests`.
- **Validate skill format:** [`scripts/validate_skills.py`](./scripts/validate_skills.py) checks every skill against the Agent Skills format (flat directory, required `name`/`description` frontmatter, matching name, no nested skills, parseable `agents/openai.yaml`, and that referenced scripts exist) and dry-runs the installer.
- **`duct` skill smoke test:** installs `con-duct`, captures a real command with `duct`, and summarizes the run with the skill's helper.

## Utilities

- [`scripts/install_skills.py`](./scripts/install_skills.py): install the flat skills in this repository into an AI coding agent's skills directory (`--agent claude`/`codex`, `--scope user`/`project`, or an explicit `--dest`), validating that no skill contains nested skills.
- [`scripts/validate_skills.py`](./scripts/validate_skills.py): deterministically validate that every skill is loadable by SKILL.md-based agents; used by CI.
- [`scripts/launch_agent_container.py`](./scripts/launch_agent_container.py): launch `codex` or `claude` inside a tightly-scoped Docker or Apptainer container using a reusable TOML config, including explicit auth mounts when credentials live outside the main agent state directory. See [docs/agent-container-launcher.md](./docs/agent-container-launcher.md).
