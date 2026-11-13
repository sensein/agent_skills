# Runbook

Step-by-step operation of a Lab Colab Space. The plan is the source of truth: every step below is "run the plan, review it, apply it". Commands assume `COLAB=<this-skill-dir>/scripts` and a Python 3.11+ interpreter.

Everything core runs through each member's own credentials, used by that member's agent when they act: no service identity and nothing installed in any org. The minimum an org needs is its meta (config) repo and a Project. The bus (sections 4 and 5) is an optional relay for push-style deliveries, turned on with `[bus] enabled = true`; nothing depends on it.

## 1. A new org from scratch

Do these in order. Each step is safe to repeat.

1. **Member check.** Members who use the Claude app need nothing here ([getting-started.md](./getting-started.md)). Members with a terminal run `python3.11 "$COLAB/colab.py" setup`: one plain line per check (gh sign-in and scopes, org membership, the meta clone and `COLAB_CONFIG`, Python 3.11+, the skill, the heartbeat hooks), with one next step and link for each failure.
2. **Config.** Start from the bundled `assets/lab-colab.toml`. Set `[org] name`, `meta_repo`, `project_title`. Leave `[spaces]` empty for now.
3. **Observe and plan.** `colab.py --config <cfg> status > status.json`, then `colab.py --config <cfg> plan --snapshot status.json`. Read every action.
4. **Apply the `gh` actions.** `colab.py --config <cfg> apply --snapshot status.json --yes`. An org owner is needed for issue types. Re-run `status` and `plan`, because new objects (the Project number) unlock later actions.
5. **Meta repo.** Clone the new private meta repo and run `colab.py seed --dest <clone>`. Put the Project number in `.colab/config.toml` as `[org] project_number`, and the Session activity issue number as `[sessions] issue`. Scan, then open a PR. From now on the meta-repo copy of the config is the one to pass as `--config`.
6. **Spaces.** Declare `[spaces]` (stewards, repos, optional tier) in the meta-repo config, by PR. Re-plan: the Spaces field options and the `Space: <name>` views appear.
7. **Org-wide forms.** `colab.py render-forms --dest <clone of the forms repo>/.github/ISSUE_TEMPLATE`, scan, PR.
8. **Marketplace (optional).** Set `[marketplace] repo`, `source_repo`, `name` (and `exclude`). Re-plan: it creates the private repo, sets its Actions workflow permissions to read and write with PR approval allowed, and adds a `local` step `colab.py seed-marketplace --dest <clone>`. Seed, scan, PR. Then an org owner does the two `manual` steps: install the Claude GitHub App on the repo, and in claude.ai go to Organization settings > Plugins & skills > Marketplaces > Add plugins > Sync from GitHub. `check_marketplace.py` in the new repo enforces the claude.ai rules in CI (see section 6).
9. **The board.** Members' agents keep it current with their own logins: `colab.py sync` (and the cheap sync at the start of `next` and `inbox`) adds what each person can read and sets Spaces and their own Contributions. A first `colab.py sync --dry-run` by a steward shows what it would add. A member who wants the board fresher can run `colab.py sync` on a personal schedule, under their own login.
10. **Hooks.** Each member runs `colab.py setup --install-hooks`.
11. **Optional relay.** Only if the org wants push-style delivery issues: set `[bus] enabled = true` and follow section 4.
12. **Gates.** Record each space's gates with evidence, one PR each (`colab.py gate ...`).

## 2. Recovering from an accidental change

Someone deleted a view, renamed a field, or reset a setting. The plan describes exactly what differs.

1. An org admin runs `status` and `plan`. (With the optional relay running on a cluster and `[bus] drift_check_hours` above 0, it also keeps a **Drift report** issue in the meta repo.)
2. Check the change was not intentional. If it was, change the config by PR instead.
3. Re-run the plan, review it, then `colab.py --config <cfg> apply --snapshot <status.json> --yes`. `local` and `manual` actions are printed for you to do.
4. Re-run `plan`. A clean plan holds only the recurring items (gates, the own-templates note, and, with the relay on, "Start the bus").

The drift check never applies anything. Applying is always a person's `apply --yes` after reading the plan.

## 3. When re-applying loses data

Re-applying is not always harmless. Know these before you press `--yes`.

- **A deleted Project field comes back empty.** The plan recreates the field and its options, but every item's value for it is gone. Spaces and Contributions come back as members' agents sync (`colab.py sync --since 90d` refills a space at once). Status, Iteration, and any hand-set field do not.
- **Replacing a single-select field's options resets its values.** For Status the plan proposes a full replace of the options. That clears Status on every existing item. Do it only on a new board. On a live board, add the missing option by hand in the Project settings, and leave the plan's replace action unapplied.
- **View filters** are safe to re-apply: only the filter text changes.
- **Issue types, repos, and the marketplace permissions** are idempotent.

## 4. Retiring the bus, or running it as an optional relay

**Retiring it (the default since 2026-11-13).** With `[bus] enabled = false` (the bundled default), `colab.py plan` lists `manual` steps for whatever an earlier setup left, in this order, and apply never deletes anything:

1. Disable the workflow: Actions tab > colab-bus > **...** > **Disable workflow** (`gh workflow disable colab-bus.yml --repo <org>/<meta repo>`), then remove `.github/workflows/colab-bus.yml`, `.github/workflows/colab-watchdog.yml`, and `bus/` by a reviewed PR.
2. Stop any cluster standby: its operator runs `squeue -u $USER -n colab-bus` and `scancel --name colab-bus`, then deletes the token file in the bus state directory. Remove the `[[buses]]` entries by PR.
3. An org owner deletes the GitHub App (its settings page > Advanced > **Delete GitHub App**); that removes its installation and makes its keys useless.
4. Delete the `colab-bus` environment's secrets, then the environment: `gh secret delete COLAB_APP_ID --env colab-bus --repo <org>/<meta repo>`, the same for `COLAB_APP_PRIVATE_KEY` (and `COLAB_SIGNING_KEY` if set), then `gh api -X DELETE repos/<org>/<meta repo>/environments/colab-bus`.
5. Delete the `colab-state: operators only` ruleset (Settings > Rules > Rulesets), then the `colab-state` branch (`gh api -X DELETE repos/<org>/<meta repo>/git/refs/heads/colab-state`; fetch it first if you want to keep `audit/`), and the Actions variables `COLAB_SECURITY_STATE` and `COLAB_BUS_HEARTBEAT` if they exist. Keep the default-branch ruleset.
6. Optionally delete the `colab-bus-operators` team.

The Security gate never read any of these after the change: each member's CLI scans an item when it acts, and checks clears on the Security audit issue itself.

**Running it as an optional relay.** Set `[bus] enabled = true` by PR. The plan then proposes the parts below again. The bus is the workflow `.github/workflows/colab-bus.yml` in the meta repo, signed in as the org's GitHub App. Nobody runs it; an org owner sets it up once.

**Setting it up** (an org owner; `colab.py plan` lists these steps and `colab.py setup --app` checks each):

1. Declare it by PR: `[[buses]] id = "actions"`, `kind = "actions"`, `app = "colab-bus"` (or the lab's own name), `role = "primary"`.
2. `colab.py app-manifest --out <folder>`. Open the file link it prints in a browser signed in to GitHub as an org owner. Press **Create the App on GitHub**, then **Create GitHub App for `<org>`**.
3. You land on the App's settings page. Copy the **App ID** (a number near the top) into the entry as `app_id = <number>`, by PR.
4. On the same page, under **Private keys**, click **Generate a private key**. Your browser downloads `<app>.<date>.private-key.pem`.
5. Re-plan and apply: the `colab-state` ruleset gains the App (an `Integration` bypass, by `app_id`; nothing is removed), and apply creates the `colab-bus` environment with the default branch as its only deployment branch. By hand, the same is:

   ```bash
   gh api -X PUT repos/<org>/<meta repo>/environments/colab-bus --input - <<< '{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}'
   gh api -X POST repos/<org>/<meta repo>/environments/colab-bus/deployment-branch-policies -f name=<default branch> -f type=branch
   ```

   If the environment already has protection rules (a wait timer, reviewers, custom rules, self-review settings), apply leaves it alone and the plan prints these steps instead, so nothing is reset.

   Optional: in Settings > Environments > colab-bus, add required reviewers, so a run waits for one of them (scheduled runs wait too).
6. Store the secrets in that environment, never as repository secrets (a workflow on any branch can read repository secrets), then delete the file:

   ```bash
   gh secret set COLAB_APP_ID --env colab-bus --repo <org>/<meta repo> --body <App ID>
   gh secret set COLAB_APP_PRIVATE_KEY --env colab-bus --repo <org>/<meta repo> < ~/Downloads/<app>.<date>.private-key.pem
   rm ~/Downloads/<app>.<date>.private-key.pem
   ```

7. Install the App: open `https://github.com/apps/<app>/installations/new`, click **Install** next to the org, choose **Only select repositories**, and pick only what the plan lists: the meta repo, the space repos, and the subscriber repos in the org. Not **All repositories**: the App could then write issues and files in every repo. Add a repo there when a space or subscription gains one.
8. Seed the workflow by PR: `colab.py seed --dest <meta clone> --refresh-bus` (it also refreshes `bus/colab_bus.py`, `bus/colab_sync.py`, and `bus/colab.py`, and adds `.github/CODEOWNERS` lines for `/.github/workflows/` and `/bus/`, owned by `[bus] code_owners`, default `@<org>/colab-bus-operators`, which needs write access to the meta repo; the default-branch ruleset requires code owner review, so changes there need an owner's approval; existing CODEOWNERS lines are never changed), scan, open the PR, merge after review.
9. Start the first run: Actions tab > colab-bus > **Run workflow** (or `gh workflow run colab-bus.yml --repo <org>/<meta repo>`). If the meta repo has no `colab-state` branch yet, this run creates it (only once the ruleset is active). `colab.py bus status` then shows it.

**Checking it.** `colab.py bus status`; the workflow's runs in the Actions tab (each run prints one JSON line: its stats and counts of its audit lines, no item text); the watchdog. A red run means it could not sign in (secrets, installation) or could not run; the line says why in words.

**Rotating the key.** On the App's page, **Generate a private key** (a new `.pem`); `gh secret set COLAB_APP_PRIVATE_KEY --env colab-bus --repo <org>/<meta repo> < <new file>.pem`; delete the file; run the workflow once and check it is green; then **Delete** the old key on the App's page. **Rotate at once if the secrets were ever repository secrets** (not environment secrets), or ever readable from another branch: any member with write could have read them. Delete those repository secrets first (`gh secret delete COLAB_APP_PRIVATE_KEY --repo <org>/<meta repo>`, and the same for `COLAB_APP_ID` and `COLAB_SIGNING_KEY`); `colab.py setup --app` refuses them until they are gone.

**Disabled by GitHub.** GitHub turns a scheduled workflow off after 60 days without repository activity (the watchdog alerts: "The colab-bus workflow is disabled_inactivity"). Re-enable it: Actions tab > colab-bus > **Enable workflow**. The watchdog is a scheduled workflow too, so it can be turned off the same way; the rollout's regular PRs keep both on.

**Disabling it.** Actions tab > colab-bus > **...** > **Disable workflow**. A standby takes over once the lease is stale (45 minutes). To remove it for good: delete the `[[buses]]` entry and the workflow by PR, delete the two secrets, delete the `colab-bus` environment, and uninstall or delete the App (Organization settings > GitHub Apps); re-plan (the ruleset keeps its `Integration` bypass until an owner removes it: apply never removes one).

**Moving from an ORCD bus.** Run both for a while, with the ORCD bus as the standby, then retire it:

1. Set up the Actions bus (above) with `role = "primary"`, and change the ORCD entry to `role = "standby"`, `order = 1`, in the same PR.
2. Hand the lease over: `colab.py bus handback actions` (any operator). At its next run the Actions bus becomes active and starts from the ORCD bus's published flags, clears, and cursors (look-back `takeover_lookback_minutes`; delivery keys stop duplicates). The ORCD bus stands by.
3. Watch a few days: green runs, `bus status`, a quiet watchdog, deliveries in live spaces.
4. Keep the ORCD bus as a standby, or retire it: cancel the job, remove its entry by PR, and have an owner take the operator out of `colab-bus-operators`.

**Limits.** Scheduled runs can start late, so the lease lasts at least 45 minutes (a standby takes over after 45 minutes, not 15). The relay's published security state is display only: the CLI's gate does not read it. Each run is a deployment to the `colab-bus` environment, so the repo's Deployments list grows. An `[scan] injection_command` that needs ORCD does not run in Actions (the rules do). The drift check (section 2) does not run in Actions either: run `colab.py plan` yourself now and then. Audit lines go to `audit/<YYYY-MM>.jsonl` on `colab-state` (then `-2`, `-3`, ... past 900 KB) without spans or reasons; the full lines with spans stay in the run's temporary folder and are not kept.

## 5. Operating a standby bus on a cluster (optional relay)

A standby is a Slurm job that polls GitHub. Its job file is the filled copy of `bus/colab-bus.sbatch.template`, kept in the bus state directory outside git. Never work on it from a clone of the meta repo.

- **Check it.** `colab.py bus status` (which bus holds the lease, and each bus's last heartbeat); `squeue -u $USER -n colab-bus`; the newest `colab-bus-<jobid>.out` in the state directory. The `colab-watchdog` workflow alerts when no bus holds a fresh lease, naming the operators.
- **Hand over.** A standby takes over by itself when the primary's lease goes stale. When the primary is back, an operator (any operator can) runs `colab.py bus handback <primary id>`; the standby stands by at its next poll. To move the bus for good, change `role`/`order` in `[[buses]]` by PR and hand back.
- **Migrating a meta repo from the Actions variables.** In this order: the default-branch ruleset; the team (empty) and the `colab-state` ruleset (creation, update, deletion: team only); the `[[buses]]` entry by reviewed PR; team members added by an org owner by hand; the branch, created by an operator's `apply` (apply refuses until the ruleset is active and the caller is in the team); then signing. Restart the bus with `--bus-id` (the new sbatch template). The bus moves to the branch on its next poll and stops writing the variables; delete `COLAB_SECURITY_STATE` and `COLAB_BUS_HEARTBEAT` once the watchdog is quiet. `audit.jsonl` holds failures, drops, stale sessions, and drift-check failures.
- **Code changes need a restart.** The bus imports `colab_bus.py`, `colab_sync.py`, and `colab.py` once, at start. After the meta repo's `bus/` files change (they are pulled each poll but not reloaded), run `scontrol requeue <jobid>` or cancel and resubmit. Config, subscription, and rollout changes need nothing: they are re-read every poll.
- **Job-file changes need cancel and resubmit.** Slurm reuses the batch script exactly as it was first submitted when it requeues a job. Editing the job file and then requeuing runs the old script. After editing it, `scancel <jobid>`, then `sbatch` the edited file.
- **The drift check.** Every `drift_check_hours` the bus runs `observe` and `build_plan`, read-only, with the config it loaded. `observe` calls `gh`, which must be on the bus job's PATH and logged in as someone who can read the org. If it cannot, the failure goes to `audit.jsonl` as `drift_check_failed`, the poll goes on, and the check retries within the hour. The result is the `drift_actions` count in the heartbeat and in `state.json`. Set `drift_check_hours = 0` to turn it off.
- **Stop it.** Cancel the job. State stays in the state directory, so a restart resumes where it was.

## 6. The marketplace rules (claude.ai org sync)

`scripts/check_marketplace.py` in the marketplace repo encodes these. They were learned from claude.ai rejecting a sync, so keep them.

- Git sources need `https://` URLs, not `owner/repo` shorthand.
- `"strict": false` on every plugin without a `.claude-plugin/plugin.json`: the entry is the manifest.
- Under 200 MB per plugin, uncompressed.
- No top-level `bin/` directory in a plugin.
- A source under another owner must be a public repo.
- Source types are limited to `github`, `url`, `git-subdir`, or a `./` path.
- The marketplace repo itself must be private.
