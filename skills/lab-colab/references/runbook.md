# Runbook

Step-by-step operation of a Lab Colab Space. The plan is the source of truth: every step below is "run the plan, review it, apply it". Commands assume `COLAB=<this-skill-dir>/scripts` and a Python 3.11+ interpreter.

## 1. A new org from scratch

Do these in order. Each step is safe to repeat.

1. **Member check.** Every person who will run the skill runs `python3.11 "$COLAB/colab.py" setup`. It checks `gh` auth and scopes (`repo`, `read:org`, `project`; `admin:org` only for org owners), a Python 3.11+ interpreter, `COLAB_CONFIG`, where the skill is installed, and the heartbeat hooks, with an exact fix for each failure.
2. **Config.** Start from the bundled `assets/lab-colab.toml`. Set `[org] name`, `meta_repo`, `project_title`. Leave `[spaces]` empty for now.
3. **Observe and plan.** `colab.py --config <cfg> status > status.json`, then `colab.py --config <cfg> plan --snapshot status.json`. Read every action.
4. **Apply the `gh` actions.** `colab.py --config <cfg> apply --snapshot status.json --yes`. An org owner is needed for issue types. Re-run `status` and `plan`, because new objects (the Project number) unlock later actions.
5. **Meta repo.** Clone the new private meta repo and run `colab.py seed --dest <clone>`. Put the Project number in `.colab/config.toml` as `[org] project_number`, and the Session activity issue number as `[sessions] issue`. Scan, then open a PR. From now on the meta-repo copy of the config is the one to pass as `--config`.
6. **Spaces.** Declare `[spaces]` (stewards, repos, optional tier) in the meta-repo config, by PR. Re-plan: the Spaces field options and the `Space: <name>` views appear.
7. **Org-wide forms.** `colab.py render-forms --dest <clone of the forms repo>/.github/ISSUE_TEMPLATE`, scan, PR.
8. **Marketplace (optional).** Set `[marketplace] repo`, `source_repo`, `name` (and `exclude`). Re-plan: it creates the private repo, sets its Actions workflow permissions to read and write with PR approval allowed, and adds a `local` step `colab.py seed-marketplace --dest <clone>`. Seed, scan, PR. Then an org owner does the two `manual` steps: install the Claude GitHub App on the repo, and in claude.ai go to Organization settings > Plugins & skills > Marketplaces > Add plugins > Sync from GitHub. `check_marketplace.py` in the new repo enforces the claude.ai rules in CI (see section 5).
9. **Bus.** Follow "Starting the bus" in `SKILL.md`, then section 4 below. It starts in dry-run.
10. **Hooks.** Each member runs `colab.py setup --install-hooks`.
11. **Gates.** Record each space's gates with evidence, one PR each (`colab.py gate ...`).

## 2. Recovering from an accidental change

Someone deleted a view, renamed a field, or reset a setting. The plan describes exactly what differs.

1. If the bus is running with `[bus] drift_check_hours` above 0, it has already opened or updated the **Drift report** issue in the meta repo, listing each proposed action. Otherwise run `status` and `plan` yourself.
2. Check the change was not intentional. If it was, change the config by PR instead.
3. Re-run the plan, review it, then `colab.py --config <cfg> apply --snapshot <status.json> --yes`. `local` and `manual` actions are printed for you to do.
4. Re-run `plan`. A clean plan holds only the recurring items (gates, "Start the bus", the own-templates note). The bus then closes the Drift report on its next check.

The drift check never applies anything. Applying is always a person's `apply --yes` after reading the plan.

## 3. When re-applying loses data

Re-applying is not always harmless. Know these before you press `--yes`.

- **A deleted Project field comes back empty.** The plan recreates the field and its options, but every item's value for it is gone. Spaces recovers by itself: the bus recomputes it on its next sweep. Status, Iteration, and any hand-set field do not.
- **Replacing a single-select field's options resets its values.** For Status the plan proposes a full replace of the options. That clears Status on every existing item. Do it only on a new board. On a live board, add the missing option by hand in the Project settings, and leave the plan's replace action unapplied.
- **View filters** are safe to re-apply: only the filter text changes.
- **Issue types, repos, and the marketplace permissions** are idempotent.

## 4. Operating the bus

The bus is a Slurm job that polls GitHub. Its job file is the filled copy of `bus/colab-bus.sbatch.template`, kept in the bus state directory outside git. Never work on it from a clone of the meta repo.

- **Check it.** `squeue -u $USER -n colab-bus`; the newest `colab-bus-<jobid>.out` in the state directory; and the `COLAB_BUS_HEARTBEAT` variable on the meta repo. The `colab-watchdog` workflow alerts when the heartbeat is stale. `audit.jsonl` holds failures, drops, stale sessions, and drift-check failures.
- **Code changes need a restart.** The bus imports `colab_bus.py` and `colab.py` once, at start. After the meta repo's `bus/` files change (they are pulled each poll but not reloaded), run `scontrol requeue <jobid>` or cancel and resubmit. Config, subscription, and rollout changes need nothing: they are re-read every poll.
- **Job-file changes need cancel and resubmit.** Slurm reuses the batch script exactly as it was first submitted when it requeues a job. Editing the job file and then requeuing runs the old script. After editing it, `scancel <jobid>`, then `sbatch` the edited file.
- **The drift check.** Every `drift_check_hours` the bus runs `observe` and `build_plan`, read-only, with the config it loaded. `observe` calls `gh`, which must be on the bus job's PATH and logged in as someone who can read the org. If it cannot, the failure goes to `audit.jsonl` as `drift_check_failed`, the poll goes on, and the check retries within the hour. The result is the `drift_actions` count in the heartbeat and in `state.json`. Set `drift_check_hours = 0` to turn it off.
- **Stop it.** Cancel the job. State stays in the state directory, so a restart resumes where it was.

## 5. The marketplace rules (claude.ai org sync)

`scripts/check_marketplace.py` in the marketplace repo encodes these. They were learned from claude.ai rejecting a sync, so keep them.

- Git sources need `https://` URLs, not `owner/repo` shorthand.
- `"strict": false` on every plugin without a `.claude-plugin/plugin.json`: the entry is the manifest.
- Under 200 MB per plugin, uncompressed.
- No top-level `bin/` directory in a plugin.
- A source under another owner must be a public repo.
- Source types are limited to `github`, `url`, `git-subdir`, or a `./` path.
- The marketplace repo itself must be private.
