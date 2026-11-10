---
name: lab-colab
description: >-
  Bootstrap, roll out, and operate a Lab Colab Space: GitHub issues, Projects, and
  repos used as a typed work graph and a pub/sub bus for a research group, mixing
  public and private repos. Re-entrant: each run observes the org, diffs it against
  the config and the rollout phase, and proposes the next steps as a dry run.
  Also runs the day-to-day /colab commands (distill, next, claim, handoff, capture,
  brief, link, credit, ingest, subscribe, publish, scan, improve). Use when the user
  wants to set up or advance the lab's GitHub-based coordination; turn discussions,
  meetings, or threads into typed work items; pick up or hand off agent-ready work;
  capture session knowledge for the lab graph or BrainKB; check what is happening
  across projects; or scan before pushing to a public repo.
---

# Lab Colab Space

Use this skill to set up and run the lab's GitHub-based coordination layer. Work is a typed graph of issues (Idea, Question, Experiment, Review, Software, Model, Infrastructure, Dataset, Output, Task) linked by typed edges and grouped by `space:*` labels. A polling bus on ORCD moves events between repos, only ever toward stricter tiers.

Read [`references/design.md`](./references/design.md) once per session for the model, tiers, and bus rules, and [`references/commands.md`](./references/commands.md) before running a `/colab` command.

## Constitution

This skill obeys any higher-level instruction, project policy, or user constraint in scope. It never overrides user instructions, repo contribution rules, or rules about secrets and external writes. When a parent rule is stricter, follow it.

Hard rules, always:

1. **Dry run first.** Org-level writes (repos, issue types, labels, Projects, fields) run only through `colab.py apply --yes`, after the user has seen the plan in this session and said yes. Approval for one plan does not carry over to the next.
2. **Never read restricted paths.** A Claude session sends what it reads to the model API, even on ORCD. Restricted data is processed only by ORCD jobs with open models, which return aggregates or pointers.
3. **Scan before any push or post** to a looser tier: `colab.py scan <files>` must exit 0. Treat any finding as a stop.
4. **Agents propose, humans accept.** Agent-made items get `agent-proposed` and Status = Proposed until a person accepts them.
5. **PRs, not direct writes**, for every file change, to the repo's integration branch when it has one.
6. **Stay under GitHub's content limits**: one write per second, and a digest issue instead of a fan-out above 20 targets.

## Helper Script Location

Commands below use `skills/lab-colab/scripts/colab.py`, the path inside this repository. Once installed, the helpers sit next to this file under `<this-skill-dir>/scripts/`. Resolve paths from where this `SKILL.md` lives:

```bash
COLAB="<this-skill-dir>/scripts"
python3 "$COLAB/colab.py" --help
```

The scripts need Python 3.11+ (for `tomllib`) and an authenticated `gh` (`gh auth status`). Creating issue types needs an org owner.

## The Bootstrap Loop

Run this loop whenever the user asks to set up, continue, or check the rollout. It is safe to re-run: every step observes first.

1. **Find the config.** If a clone of the meta repo exists (default `sensein/lab-colab`), pass `--config <clone>/.colab/config.toml`; that copy is the source of truth. Otherwise use the bundled [`assets/lab-colab.toml`](./assets/lab-colab.toml) and tell the user that bootstrap starts from defaults.
2. **Observe.** `python3 "$COLAB/colab.py" --config <cfg> status > /tmp/colab-status.json` (read-only).
3. **Plan.** `python3 "$COLAB/colab.py" --config <cfg> plan --snapshot /tmp/colab-status.json`. Show the user the actions grouped by kind:
   - `gh`: org metadata, run by `apply`;
   - `local`: file changes you make in a clone and send as a PR;
   - `manual`: steps only a person can do (e.g. the Iteration field, defining spaces);
   - `gate`: a phase gate to verify before switching features on for a space.
4. **Confirm.** Ask the user to approve the `gh` actions. Then `apply --snapshot ... --yes`. Re-observe and re-plan, because new objects (e.g. the Project number) unlock later actions.
5. **Do the local steps**, each as a reviewed PR:
   - seed the meta repo: `colab.py seed --dest <meta clone>`;
   - per space repo: `colab.py render-forms --dest <clone>/.github/ISSUE_TEMPLATE --workflows <clone>/.github/workflows`;
   - scan every changed file before pushing.
6. **Gates.** For a `gate` action, check the gate's criterion against real evidence (links to runs, PRs, scan output). Only then record it: `colab.py gate --rollout <meta clone>/.colab/rollout.json --space <s> --phase <n> --evidence <link>`. Commit the change by PR. Recording phase *n* switches on phase *n+1*'s features for that space only.
7. **Report** what changed, what is waiting on a person, and the next gate per space.

The bootstrap upgrades itself through the same loop. Its config, subscriptions, and the bus scripts live in the meta repo (seeded from this skill), so improving the rollout means a PR there, made with `/colab improve`.

### Rollout shape

Phase 0 reaches **every** space with low-risk parts only: types, labels, forms, links, paste intake, dashboards, and the bus in **dry-run** (it logs what it would deliver and writes nothing but its heartbeat). Risky parts switch on per space, by gate: live bus writes (phase 1), then agent-initiated proposals, automatic intake, and the restricted tier (phase 2), then self-improvement (phase 3). Gates are in the config and in [`references/rollout.md`](./references/rollout.md).

### Starting the bus

Seeding puts `bus/colab-bus.sbatch.template` in the meta repo. To run it:

1. Copy the template **outside git**, into the bus state directory: private group storage, never scratch.
2. Fill the placeholders: partition, state directory, meta clone, a Python 3.11+ interpreter, and the token file.
3. Ask the user before running `sbatch`.

The job requeues itself near its time limit and after an unexpected exit. A space stays in dry-run until its phase-0 gate is recorded. The `colab-watchdog` workflow in the meta repo alerts the lab when the heartbeat goes stale. A one-shot check is `python3 colab_bus.py --meta <clone> --state-dir <dir> --token-file <file> --once`.

Give the bus its own fine-grained token (or a GitHub App installation token) in a `chmod 600` file. Grant only:
- Metadata, Issues, and Contents: read on the space and subscriber repos;
- Issues: write on subscriber repos and the meta repo;
- Variables: write on the meta repo, for the heartbeat.

Org metadata (issue types, Projects) is changed only by `colab.py apply`, under the member's own `gh` login.

## Day-to-day Commands

Each command is specified in [`references/commands.md`](./references/commands.md): what it reads, what it writes, and its contract. In short:

- **`distill`**: turn a thread, meeting transcript, or notes into proposed typed items and links.
- **`next`**: list Ready, unblocked, unleased `agent-ready` items within the caller's access.
- **`claim`**: assign the item, set In progress, and post a lease (`colab.py lease --user <me>`).
- **`handoff`**: post a ledger checkpoint and renew the lease.
- **`capture`**: turn session results into knowledge items, staged in the meta repo.
- **`brief`**: summarise what happened, challenges, and next steps for a space or the lab.
- **`link`**: write a typed edge into the fenced `links` block. Validate it with `colab.py links --type <Type>`.
- **`credit`**: propose or export CRediT roles.
- **`ingest`**: take a paste, upload, or connector pull, set its tier, and propose items.
- **`subscribe`**: edit `.colab/subscriptions.toml`.
- **`publish`**: emit an event; a looser tier becomes a promotion PR.
- **`scan`**: `colab.py scan`.
- **`improve`**: a PR to the skill, the config, or the infra.

Every session starts by reading the claimed issue and its ledger, and ends with `handoff` then `capture`.
