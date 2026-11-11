---
name: lab-colab
description: >-
  Bootstrap, roll out, and operate a Lab Colab Space: GitHub issues, Projects, and
  repos used as a typed work graph and a pub/sub bus for a research group, mixing
  public and private repos. Re-entrant: each run observes the org, diffs it against
  the config and the rollout phase, and proposes the next steps as a dry run.
  Also runs the day-to-day /colab commands (distill, next, claim, handoff, capture,
  brief, run, link, tag, credit, ingest, subscribe, publish, scan, improve). Use when the user
  wants to set up or advance the lab's GitHub-based coordination; turn discussions,
  meetings, or threads into typed work items; pick up, hand off, or run agent-ready work (a claimed Experiment issue in an isolated workspace under explicit budgets, with a metric/verify loop and immutable Observation records);
  capture session knowledge for the lab graph or BrainKB; check what is happening
  across projects; or scan before pushing to a public repo.
---

# Lab Colab Space

Use this skill to set up and run the lab's GitHub-based coordination layer. Work is a typed graph of issues (Idea, Question, Experiment, Review, Software, Model, Infrastructure, Dataset, Output, Task) linked by typed edges and grouped into spaces: named areas of work declared in the config, each with stewards and a list of existing repos. A space is not a repo or a folder, and nothing is created in member repos up front. A polling bus on ORCD moves events between repos, only ever toward stricter tiers.

Read [`references/design.md`](./references/design.md) once per session for the model, tiers, and bus rules, and [`references/commands.md`](./references/commands.md) before running a `/colab` command. [`references/runbook.md`](./references/runbook.md) has the operating steps: a new org from scratch, recovering from an accidental change, when re-applying loses data, and running the bus.

## Constitution

This skill obeys any higher-level instruction, project policy, or user constraint in scope. It never overrides user instructions, repo contribution rules, or rules about secrets and external writes. When a parent rule is stricter, follow it.

Hard rules, always:

1. **Dry run first.** Org-level writes (repos, issue types, Projects, fields) run only through `colab.py apply --yes`, after the user has seen the plan in this session and said yes. Approval for one plan does not carry over to the next.
2. **Never read restricted paths.** A Claude session sends what it reads to the model API, even on ORCD. Restricted data is processed only by ORCD jobs with open models, which return aggregates or pointers.
3. **Scan before any push or post** to a looser tier: `colab.py scan <files>` must exit 0. Treat any finding as a stop.
4. **Agents propose, humans accept.** Agent-made items get Status = Proposed until a steward or Lead accepts them.
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
   - `manual`: steps only a person can do (e.g. defining spaces, recording the Project number);
   - `gate`: a phase gate to verify before switching features on for a space.
4. **Confirm.** Ask the user to approve the `gh` actions. Then `apply --snapshot ... --yes`. Re-observe and re-plan, because new objects (e.g. the Project number) unlock later actions.
5. **Do the local steps**, each as a reviewed PR:
   - seed the meta repo: `colab.py seed --dest <meta clone>`;
   - the marketplace repo, when `[marketplace] repo` is set: `colab.py seed-marketplace --dest <clone of the marketplace repo>` (never overwrites; scan, PR; an owner then installs the Claude GitHub App and syncs it in claude.ai);
   - issue forms once, org-wide: `colab.py render-forms --dest <clone of the org's .github repo>/.github/ISSUE_TEMPLATE`. Repos with their own templates keep them; the plan lists those;
   - scan every changed file before pushing.
6. **Gates.** For a `gate` action, check the gate's criterion against real evidence (links to runs, PRs, scan output). Only then record it: `colab.py gate --rollout <meta clone>/.colab/rollout.json --space <s> --phase <n> --evidence <link>`. Commit the change by PR. Recording phase *n* switches on phase *n+1*'s features for that space only.
7. **Report** what changed, what is waiting on a person, and the next gate per space.

The bootstrap upgrades itself through the same loop. Its config, subscriptions, and the bus scripts live in the meta repo (seeded from this skill), so improving the rollout means a PR there, made with `/colab improve`.

### Rollout shape

Phase 0 reaches **every** space with low-risk parts only: types, org-wide forms, links, paste intake, dashboards, and the bus in **dry-run** (it logs what it would deliver and writes nothing but its heartbeat). Risky parts switch on per space, by gate: live bus writes (phase 1), then agent-initiated proposals, automatic intake, and the restricted tier (phase 2), then self-improvement (phase 3). Gates are in the config and in [`references/rollout.md`](./references/rollout.md).

### Starting the bus

Seeding puts `bus/colab-bus.sbatch.template` in the meta repo. To run it:

1. Copy the template **outside git**, into the bus state directory: private group storage, never scratch.
2. Fill the placeholders: partition, state directory, meta clone, a Python 3.11+ interpreter, and the token file.
3. Ask the user before running `sbatch`.

The job requeues itself near its time limit and after an unexpected exit. A space stays in dry-run until its phase-0 gate is recorded. The `colab-watchdog` workflow in the meta repo alerts the lab when the heartbeat goes stale. A one-shot check is `python3 colab_bus.py --meta <clone> --state-dir <dir> --token-file <file> --once`.

Give the bus its own fine-grained token (or a GitHub App installation token) in a `chmod 600` file. Grant only:
- Metadata, Issues, and Contents: read on the space and subscriber repos, including space repos in other orgs (without it those repos are skipped and count as restricted);
- Projects: read and write on the organization (Project sync);
- Issues: write on subscriber repos and the meta repo;
- Variables: write on the meta repo, for the heartbeat.

**Drift check.** With `[bus] drift_check_hours` above 0 (default 24), the bus runs `observe` and `build_plan` read-only with the config it loaded, drops the actions expected to recur (gates, "Start the bus", the own-templates and tracked read-only notes), and keeps one `Drift report` issue in the meta repo: edited in place while the plan is not clean, closed with a short comment when it is. It never applies anything; the issue lists the `plan` and `apply --yes` commands to review. `drift_actions` is in the heartbeat.

Org metadata (issue types, Projects) is changed only by `colab.py apply`, under the member's own `gh` login. That includes the Project's Spaces field and Iteration field and its views (`Space: <name>`, Agent queue, Proposals inbox, My work); the plan reads the views and their filters and fixes any that differ.

Once `[org] project_number` is set, the bus also keeps the Project's multi-select **Spaces** field current (`[bus] project_sync`): it adds newly opened items from space repos and sets their Spaces in the same pass, then re-syncs every item's Spaces every `project_sync_every` polls. `[bus] project_add` (default `["issue"]`) lists the kinds that are added, so pull requests stay off the board unless you add `"pull_request"`. This is bookkeeping on the private org Project, not a delivery, so it runs in dry-run spaces too. It needs the token to have Projects read and write on the organization.

## Session Heartbeats

A session that runs out of tokens just stops, with no handoff. So the heartbeat comes from activity: Claude Code hooks run `colab.py beat` after tool use, and a session that makes no tool calls sends no beats. Beats cost no model tokens. They are throttled locally, so a GitHub write happens at most every 10 minutes per session.

Each beat refreshes the session's comment in the meta repo's Session activity issue, and renews the session's lease on its claimed item. Leases default to 45 minutes, so a silent session's claim lapses within the hour and `next` offers the item again. The bus reports active, waiting, and stale sessions in its heartbeat.

Install the hooks once per member with `colab.py setup --install-hooks`. It first reports `gh` auth and scopes (`repo`, `read:org`, `project`; `admin:org` is for org owners only), a Python 3.11+ interpreter, whether `COLAB_CONFIG` resolves, and where the skill is installed, each with an exact fix. Then it backs up `~/.claude/settings.json`, merges the `PostToolUse` and `Stop` hooks, keeps every other key and hook, and adds nothing on a re-run. To do it by hand, use a Python 3.11+ interpreter and point `COLAB_CONFIG` at the meta clone's config:

```json
{
  "hooks": {
    "PostToolUse": [{ "matcher": "*", "hooks": [{ "type": "command",
      "command": "COLAB_CONFIG=<cfg> <python3.11+> <this-skill-dir>/scripts/colab.py beat --from-hook --background 2>/dev/null || true", "timeout": 30 }] }],
    "Stop": [{ "matcher": "*", "hooks": [{ "type": "command",
      "command": "<python3.11+> <this-skill-dir>/scripts/colab.py beat --from-hook --background" }] }]
  }
}
```

- **Claims.** On claim, record the item for the worktree with `echo owner/repo#n > "$(git rev-parse --git-dir)/colab-claim"`. Beats renew that item's lease.
- **Long waits.** Before waiting on a long Slurm job, run `colab.py beat --session <id> --job <jobid>`. The lease then runs to the job's scheduled end plus 15 minutes, and the bus counts the session as waiting, not stale.
- **Agents without hooks.** Call `beat` from their own loop. A timer loop keeps beating after the model has stopped, so use one only where hooks are unavailable.
- **Recovery.** Another session, or another agent such as Codex acting for the same member, resumes from the item's last handoff checkpoint. Frequent handoffs are what make an abrupt stop cheap.

## Day-to-day Commands

Each command is specified in [`references/commands.md`](./references/commands.md): what it reads, what it writes, and its contract. In short:

- **`distill`**: turn a thread, meeting transcript, or notes into proposed typed items and links.
- **`next`**: `colab.py next` lists Project items with Status = Ready and Agent-ready = yes that are unblocked and unleased, within the caller's access.
- **`claim`**: assign the item, set In progress, post a lease (`colab.py lease --user <me>`, 45 minutes), and write the worktree's `colab-claim` file so hooks renew it.
- **`handoff`**: post a ledger checkpoint. Hand off often: a session that runs out of tokens cannot write one.
- **`capture`**: turn session results into knowledge items, staged in the meta repo.
- **`brief`**: summarise what happened, challenges, and next steps for a space or the lab.
- **`run`**: `colab.py run start|confirm|step|loop|check|finish|stage|cleanup|status` (engine: `scripts/colab_run.py`) runs a claimed Experiment issue in an isolated workspace under budgets read from the issue. `start` prints the verify and iterate commands with their sha256, and who last edited the issue; nothing runs until you confirm that hash (`--confirm-verify`), and a later change to the command refuses again. Each iteration writes an immutable Observation, staged in the meta repo by PR. `scripts/monitor_slice.py` is the loop breaker. Runs that touch restricted data (tier computed as the bus does, failing closed) never execute in a session: they go to the batch job that `job.sbatch` describes.
- **`link`**: write a typed edge into the fenced `links` block. Validate it with `colab.py links --type <Type>`.
- **`tag`**: `colab.py tag <owner>/<repo>#<n> <label>... [--remove] [--dry-run]` adds or removes allow-listed labels only: `space:<name>` for a space in `[spaces]`, and the `[[labels]]` entries (today `tier:restricted`). It creates a label on first use, with your own `gh` login. In another org it works only where the space has `control = "owner"`.
- **`credit`**: propose or export CRediT roles.
- **`ingest`**: take a paste, upload, or connector pull, set its tier, and propose items.
- **`subscribe`**: edit `.colab/subscriptions.toml`.
- **`publish`**: emit an event; a looser tier becomes a promotion PR.
- **`scan`**: `colab.py scan`.
- **`improve`**: a PR to the skill, the config, or the infra.

Every session starts by reading the claimed issue and its ledger, and ends with `handoff` then `capture`.

## Migrating from labnb

The `labnb*` skills (a local, file-based lab notebook) are folded into this skill and removed. Notebook entries are now typed issues in the work graph; only the run engine was kept, as `/colab run`.

| labnb | lab-colab |
|---|---|
| `labnb-idea` and the idea index | An **Idea** issue (`distill` or the Idea form), found with `next` and `brief` |
| `labnb-promote` | A new **Experiment** issue with `derived-from: <owner>/<repo>#<n>` pointing at the Idea (`link`) |
| `labnb-resume` and the lab index | `brief` for the space (what happened, challenges, next steps) and `next` for what to pick up |
| `labnb-run`, `register_experiment.py` | `claim` the Experiment, then `colab.py run start`; budgets come from its form fields; confirm the printed command hash before the first step |
| `monitor_slice.py` (labnb) | `scripts/monitor_slice.py` here, same CLI; `colab.py run` calls it for you |
| `results.tsv`, `log.md`, `provenance.jsonl`, `metadata.json` | Same files in the run directory; plus `observations/*.json` and a staging PR |
| the global notebook under a lab root | The run directory under `${XDG_STATE_HOME:-~/.local/state}/lab-colab/runs/` for local state; the issue ledger and `.colab/staging/` for what the lab keeps |

Existing notebook directories are not read by the new code. Copy anything worth keeping into an Idea or Experiment issue, and delete the old directory yourself; nothing here deletes it.
