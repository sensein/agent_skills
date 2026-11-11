Every session begins by reading the claimed issue and its ledger, and ends with `handoff` then `capture`. The ledger is the issue's comment thread: checkpoint comments and lease markers. Read [design.md](./design.md) for tiers, edges, and bus rules before running a command.

## Summary

| Command | Writes | Lease | Human step |
|---|---|---|---|
| distill | Proposed items and links | No | Accept or drop each item |
| next | Nothing | No | Choose an item |
| claim | Assignee, Status, lease comment | Creates one | None |
| handoff | Checkpoint comment, lease comment | Renews | None |
| capture | Staging PR in the meta repo | No | Lead reviews the PR |
| brief | Nothing by default | No | Read the output |
| run | Local run directory; progress comments; staging PR | Renewed by beats | Lead reviews the staging PR |
| link | Issue body | No | None |
| tag | Labels on one issue; the label itself on first use | No | A steward confirms a removed tier label |
| credit | Proposed roles | No | Person confirms roles |
| ingest | Private storage, proposed items | No | Member gives consent |
| subscribe | Subscriptions PR | No | Review the PR |
| publish | Item, or promotion PR | No | Review promotion PR |
| scan | Nothing | No | Fix findings |
| improve | Self-improve PR | No | Lead approves scanner, tier, or bus changes |

## distill

**Purpose.** Turn a thread, meeting transcript, or notes into proposed typed items and links.

**Reads.** The source material at its own tier. Open items in the target space, to find duplicates.

**Writes.** New issues in a repo of the target space, added to the org Project with Status Proposed. No labels, except `space:<name>` when an item joins a space its repo is not in, or `tier:restricted` for an exception. Each body has a `links` block.

**Steps.**

1. Check the source tier. If it is restricted, stop and send the material to `ingest`.
2. Pick one type per candidate from the table in `design.md`.
3. Link a candidate to an existing open item when one matches. Do not duplicate it.
4. Write each body with its required fields and a `links` block. Run `colab.py links --type <Type>` on each body. Fix every error.
5. Create the issues, add them to the Project, and set Status as described under Writes.

**Guardrails.**

- A person accepts or drops each item. One approval does not carry over.
- Restricted content never goes into a body. Link to it by pointer.
- Run `colab.py scan` before posting text to a looser tier.

## next

**Purpose.** List the items an agent may claim now.

**Reads.** Org Project items with Status Ready and Agent-ready yes. Native blocked-by links. Comments, for lease markers.

**Writes.** Nothing.

**Steps.**

1. Run `colab.py next`. It reads the org Project's items with Status Ready and Agent-ready yes.
2. Drop any item with an open blocked-by (`depends-on`) link.
3. Drop any item with a live lease. A live lease is the newest `colab-lease` marker whose `until` is later than now. The helper's `live_lease` check does this.
4. Drop any item above the tier the session may read. Restricted items are never listed to a Claude session.
5. Print the rest with number, title, space, type, Risk, and Target date.

**Guardrails.**

- Read only. `next` never claims an item.
- An expired lease does not block an item.

## claim

**Purpose.** Take an item and mark it in progress.

**Reads.** The item body, its comments (for a live lease check), and its Status.

**Writes.** The assignee, Status set to In progress, and one lease comment.

**Steps.**

1. Re-check the item. It must be Ready, have no open blocked-by link, and have no live lease held by another user. Stop if any check fails.
2. Run `colab.py lease --user <login>`. The default lease is 45 minutes. Session heartbeats renew it while the session is active, so it lapses soon after a session stops.
3. Post the marker as a comment. The marker has this form:

   ```
   <!-- colab-lease user=<login> until=2026-01-01T12:00:00Z -->
   ```

4. Assign the item to the user.
5. Set Status to In progress in the Project.
6. Record the claim for this worktree, so hooks renew the lease: `echo <owner>/<repo>#<n> > "$(git rev-parse --git-dir)/colab-claim"`.

**Guardrails.**

- Never take over a live lease. Ask the holder or wait for expiry.
- Lease `user` values use GitHub login characters only.

## handoff

**Purpose.** Leave a checkpoint so the next session, person, or agent can continue. A session that runs out of tokens cannot hand off, so hand off at every meaningful step, not only at the end.

**Reads.** Current session state, open PRs on the item, and run links.

**Writes.** One checkpoint comment on the item. The lease is renewed by session heartbeats, not here.

**Steps.**

1. Collect the current state, the next steps, the open questions, and links to PRs and runs.
2. Write a checkpoint comment with four headings: State, Next steps, Open questions, and Links.
3. Before a long wait on a Slurm job, run `colab.py beat --session <id> --job <jobid>`, so the lease covers the job.
4. Run `capture` next, to record knowledge from the session.

**Guardrails.**

- Links are pointers. Put no restricted content in the comment.
- The newest unexpired lease governs the item.

## capture

**Purpose.** Turn session results into knowledge items for the lab graph and BrainKB.

**Reads.** Session results, the claimed issue, the commits made, and the model used.

**Writes.** JSON-LD-style files in `.colab/staging/` in the meta repo, delivered as a PR.

**Steps.**

1. Pick the results worth keeping. Write one node per claim.
2. Add grounding. A Finding or Hypothesis must cite an observation, a dataset, or a parent node. A node without grounding is not staged.
3. Add provenance to each node: session, issue, commit, model, and date. Add the tier and a confidence value.
4. Leave out restricted content. Store pointers only.
5. Write the files to `.colab/staging/`. Run `colab.py scan` on them and open a PR.

**Guardrails.**

- The item's Lead reviews the PR. Approved nodes sync to BrainKB.
- Restricted nodes never sync to BrainKB.
- The agent records its work as acting on behalf of the directing member.

## brief

**Purpose.** Summarise what happened, the challenges, and the next steps for a space or the whole lab.

**Reads.** Items in the scope with their Status, Lead, and Iteration. Handoff checkpoints in the time window. Merged PRs and the runs they link to. Open blocked-by links.

**Writes.** Nothing by default. The brief is returned in the session. Publishing it is a separate `publish` step.

**Steps.**

1. Set the scope: one space or the whole lab, and a time window.
2. Collect items by Status, handoff checkpoints, and merged PRs in the window.
3. Group the findings under three headings: What happened, Challenges, and Next steps. Challenges include blocked items, open questions, and stale leases.
4. Remove content above the audience's tier. Use pointers for restricted items.
5. Return the brief. Publish it only when the user asks.

**Guardrails.**

- No restricted content in the brief.
- A lab-tier brief is not posted to a public repo.

## run

**Purpose.** Run a claimed Experiment issue: an isolated workspace, explicit budgets, a metric and verify loop, a loop breaker, and one immutable Observation per iteration.

**Reads.** The Experiment issue (body, labels, repo visibility, who last edited the body, the Project's Sensitivity) through `gh`. The workspace source repo. Never restricted inputs.

**Writes.** A local run directory, default `${XDG_STATE_HOME:-~/.local/state}/lab-colab/runs/<owner>-<repo>-<n>-<timestamp>` (override with `--root`, `COLAB_RUN_ROOT`, or `[run] root`). A compact progress comment on the issue at the `[run] comment_every` / `comment_every_seconds` cadence. Staged Observations in the meta repo, by PR.

**Steps.**

1. `claim` the issue first. Then `python3 "$COLAB/colab.py" run start <owner>/<repo>#<n> --source <repo dir>`. It prints the `run_dir`, the `workspace`, the tier, and the **verify and iterate commands with their sha256**. Nothing has run.
2. **Confirm the commands.** They come from issue text, which anyone with write access can edit. Read them. `start` also reports who last edited the issue body (the `userContentEdits` editor, or the author if never edited) and warns loudly when that is not the claimant (`gh api user`). Then confirm: `colab.py run confirm --run-dir <dir> --sha256 <hash>`, or pass `--confirm-verify <hash>` to `step`/`loop`. The confirmed hash is stored in `run.json`. `step` and `loop` refuse until it matches the current commands, and refuse again if the commands in `run.json` change or the issue's Verify command changes later (the issue is re-read each step; if it cannot be read, the step refuses). Confirmation is always required, including when the editor is the claimant.
3. Budgets come from the issue's **Run budget** field (`iterations=20, wall=2h, loop=30m, compute=4 GPU-hours, metric=bacc, direction=higher`), falling back to the Compute plan and Metrics fields, and the **Verify command** field. `--iterations`, `--wall`, `--loop`, `--compute`, `--metric`, `--direction`, `--verify` override them. Iterations and wall time must be explicit, or `start` refuses. The run works even when the meta config's Experiment form lacks these fields, using the flags instead.
4. The workspace is a git worktree on its own `colab-run/<id>` branch when `--source` is a git repo, otherwise a copy without `.git` (`--mode worktree|dir|auto`). Two runs never share a workspace, a branch, or a run directory.
5. Work the loop. Either call `colab.py run step --run-dir <dir>` after each change you make, or let `colab.py run loop --run-dir <dir>` repeat `--iterate-cmd` then the verify command. Each step runs the loop breaker (`monitor_slice.py check`) first. Exit 4 means stop: do not iterate again.
6. Commands run in the workspace, without a shell, in their own process group. At the budget (or `--step-timeout`) the group gets TERM, then KILL after a short grace; no command gets time beyond the budget. The verify command's last stdout line is the metric: a finite number, or JSON such as `{"metric": 0.8, "compute": 0.5, "notes": "..."}`. NaN, Infinity, booleans, a missing metric, or an invalid `compute` (not a finite number >= 0, or text starting with one) is recorded as a crash. The engine writes no NaN or Infinity anywhere.
7. Each step appends a row to `results.tsv` and a line to `log.md`, keeps stdout and stderr under `logs/`, appends a PROV-style line to `provenance.jsonl` (the engine also logs `start`, `confirm-verify`, `prepare-workspace`, `finish`, `stage`, `cleanup`), and writes `observations/<run-id>-<iteration>.json`. An Observation holds the parameters, git commit, container digest (`--container-digest` or `COLAB_CONTAINER_DIGEST`; null if unknown), Slurm job id (`--slurm-job` or `SLURM_JOB_ID`; null if none), metric value, compute, wall seconds, run-relative paths to the logs, and the provenance: `agent`, `acting_for` (the GitHub login), `verify_sha256`, `measured_by` (`verify`, or `manual` for `--metric-value`), and `prov:wasGeneratedBy` (this step's activity, informed by the monitored slice's start activity). It is created once, read-only, with a content hash. It is never rewritten.
8. Thresholds for the loop breaker (`--patience`, `--max-failures`, `--stall-seconds`, `--reserve-seconds`, `--usage-file`, `--governance-file`, `--min-trust-score`, `--break-on-drift`, `--metric-guardrail`) are given to `start` and stored in `run.json`. A `duct` `info.json` and a `kya` verdict plug in there.
9. `colab.py run finish --run-dir <dir>` closes the slice. `colab.py run stage --run-dir <dir> --meta <meta clone>` scans the Observations (the same logic as `colab.py scan`, including `[scan] command`), then copies them into `.colab/staging/observations/<run-id>/`. A scan finding stages nothing new and never touches files from an earlier clean stage. `--open-pr` branches from `origin/main`, commits, pushes, and opens the PR. Then `handoff` and `capture`, which cite the Observations as grounding.
10. `colab.py run cleanup <run-id> --yes` removes the run's workspace: `git worktree remove` and `prune`, and deletes the `colab-run/<id>` branch (a copied workspace is deleted). It keeps the run directory (logs, Observations) unless `--delete-run-dir`, and refuses while the run is active unless `--force`.

**Guardrails.**

- **Restricted data.** The tier is computed as the bus does: the strictest of the repo's visibility, the declared `tier` of its spaces (repo membership and `space:*` labels, strictest across spaces), a `tier:*` label, and the Project's Sensitivity field when readable. Labels are read from the issue even when `--body-file` is given, and if they cannot be read the run is treated as restricted. A restricted run (or `--restricted`, or a Data field that says restricted) never executes in a session, and `start` does not copy or read the source tree: it writes `job.sbatch` only, and the batch job prepares the workspace. After the owner agrees, submit it; the script embeds the confirmed hash, so submitting is the confirmation. `step`, `loop` and the workspace step refuse unless they run inside that batch job: `SLURM_JOB_NAME` is `colab-run-<id>` and `scontrol show job $SLURM_JOB_ID` reports `BatchFlag=1`, so an interactive allocation or `srun` shell never qualifies. No progress comments are posted for such runs.
- **Restricted staging.** A restricted run stages numbers and ids only: no `notes`, no log pointers, only numeric or boolean parameters. `--allow-restricted-aggregates` stages the full record, and only after the owner agrees.
- **Job interpreter.** `job.sbatch` calls the interpreter recorded at start (`--python`, `[run] python`, else the one that ran `start`), and checks it is Python 3.11 or newer before anything else. It never calls a bare `python3`.
- Local logs are append-only (one `O_APPEND` write per line). The directory lock carries an owner token: a stale lock (dead owner on this host, or older than the stale window by the filesystem's own clock) is broken by renaming it away, and put back if it turns out not to be the stale one; release removes only a lock with our token. Iteration numbers are allocated under that lock, so parallel steps never collide or exceed the iteration budget, and a duplicate observation id is a clean refusal.
- A progress comment and every staged file is scanned first. A finding withholds the comment or stages nothing.
- Observations record the member the agent acts for. They are proposals: the item's Lead reviews the staging PR.
- Never merge the staging PR from the run.

## link

**Purpose.** Write one typed edge into an issue body.

**Reads.** The issue body. The target item, to confirm it exists.

**Writes.** The issue body, inside its fenced `links` block.

**Steps.**

1. Pick the edge from the table in `design.md`. Use a sub-issue for `part-of` and a blocked-by link for `depends-on`. Do not write those in the block.
2. Add one line to the `links` block. Create the block if it is missing.
3. Run `colab.py links --type <Type>` on the body. Pass the body on stdin, or with `--body <file>`. Exit 0 is required.
4. Write the body back to the issue.
5. Add a backlink on the target only when the target's tier is equal or stricter.

**Guardrails.**

- Knowledge-only edges (`supersedes`, `contradicts`, `supports`, `refines`) are not written here. They go in the knowledge merge step.
- The domain rule applies. `addresses` is valid only on an Experiment or Review.

## tag

**Purpose.** Put an item into a space its repo is not in, or mark one restricted item, with a label.

**Reads.** `[spaces]`, `[[labels]]`, and `[labels_space]` in the config. Whether the label exists in the repo.

**Writes.** The label in the repo, on first use only (colour and description from the config). The label on the issue.

**Steps.**

1. Run `colab.py tag <owner>/<repo>#<n> <label> [<label>...]`. Add `--remove` to take labels off, and `--dry-run` to print the `gh` calls without running them.
2. For each label it checks the repo, creates the label if it is missing, then adds it to the issue. `--remove` deletes it from the issue and never touches the repo's label.
3. Writes run under your own `gh` login, at least one second apart. A failed write prints `FAILED` and exits 1.

**Guardrails.**

- Allowed labels only: `space:<name>` for a name in `[spaces]`, and each `[[labels]]` entry. Anything else exits 2 and lists the allowed set. To allow a new label, change the config by PR.
- Removing a `tier:*` label loosens the item's tier. It is allowed, with a warning: a steward of the item's space must confirm it.
- In another org, `tag` runs only when the lab controls that org (a space with `org = "<org>"` and `control = "owner"`, the default). It acts with your own access there, which may be lower than the space owner's; a 403 or 404 means ask them. Upstream orgs (`control = "contributor"`, or not named by any space) are read-only and refused with exit 2.

## credit

**Purpose.** Propose CRediT roles for an Output, or export confirmed roles.

**Reads.** The Output item's contributors field. Handoff checkpoints and merged PRs, as evidence of contribution.

**Writes.** Proposed roles in the Output item's contributors field. An export file, when asked.

**Steps.**

1. Pick the Output item.
2. Propose one or more CRediT roles for each contributor, citing the evidence. Mark each role as proposed.
3. A person confirms or changes each role.
4. Export only the confirmed roles, and only when asked.

**Guardrails.**

- A role stays proposed until a person confirms it. The bus may also propose roles.
- Agent work is recorded as acting on behalf of the directing member.
- Profiles are visible to the member and the PI by default.

## ingest

**Purpose.** Take raw material from a paste, an upload, or a connector pull, and propose items at the correct tier.

**Reads.** The pasted text, the uploaded file, or the connector content. Connectors run under the member's own authorization: Slack, Zoom, Drive, or Box.

**Writes.** Raw files in private ORCD group storage, never in git or scratch. Proposed items at the set tier.

**Steps.**

1. Confirm consent before recording or transcribing. Recording a conversation in Massachusetts requires all-party consent.
2. Skip any segment marked off-record.
3. Store the raw material in private ORCD group storage. Do not copy it into git or scratch.
4. A local model on ORCD sets the tier. Restricted material goes to ORCD-only processing, which returns aggregates or pointers.
5. Propose items from the processed output with `distill`, at the set tier.

**Guardrails.**

- A Claude session never reads restricted paths.
- On retraction, remove the raw material from storage. Review the items derived from it.

## subscribe

**Purpose.** Add, change, or remove a subscription.

**Reads.** `.colab/subscriptions.toml` in the meta repo. The subscriber's tier.

**Writes.** A PR that changes `.colab/subscriptions.toml`.

**Steps.**

1. Choose a topic: `repo:<name>`, `label:<label>`, or `space:<space>`.
2. Choose the subscriber repo and, optionally, an `events` list. The subscriber must be in `[org] name`; the bus drops and audits deliveries to any other org.
3. Check the subscriber's tier against the source tier. Events above the subscriber's tier are dropped and audited.
4. Add a `[[subscribe]]` block. Leave `deliver` at its default, `issue`.
5. Run `colab.py scan` on the file, and open a PR.

**Guardrails.**

- Above 20 subscribers, the config calls for one digest issue. The bus does not implement the digest yet.
- Edit the file by PR only. Do not change it on the default branch.

## publish

**Purpose.** Make an item, PR, or release visible to subscribers. The bus picks up issues, pull requests, and releases on its next poll.

**Reads.** The item, its current tier, and the target audience tier.

**Writes.** The item itself, through its normal GitHub action. For a looser tier, a promotion PR.

**Steps.**

1. Read the current tier: repo visibility, the space `tier`, and any `tier:restricted` label. Read the target audience tier.
2. If the target is equal or stricter, publish directly.
3. If the target is looser, open a promotion PR with the content.
4. Run `colab.py scan` on the content. Exit 0 is required.
5. A person reviews and merges the promotion PR.

**Guardrails.**

- Lab to public happens only through a promotion PR with a clean scan.
- Restricted content never goes to git. Restricted events carry pointers only.
- No bus write loosens a tier.

## scan

**Purpose.** Check files for leaks before a push or a post.

**Reads.** The files named on the command line, or stdin when no file is given or the name is `-`.

**Writes.** Nothing. The command prints JSON counts per category to stdout.

**Steps.**

1. Run `colab.py scan <files>`. Use stdin with `colab.py scan -`.
2. When the patterns change, run `colab.py scan --selftest` first. It prints `pass` or `fail`.
3. Exit 0 means clean. Any other exit code is a stop.
4. Fix each finding at its source. Do not repeat the matched text in a reply or a comment.
5. When `[scan].command` is set in the config, the external scanner runs in place of the built-in patterns. Its exit code decides.

Built-in categories: `secret`, `path`, and `participant_id`. The `participant_id` list is empty by default. Add the lab's formats in the meta-repo copy of the config.

**Guardrails.**

- Exit 0 is required before any push or post to a looser tier.
- A finding is a stop, not a warning.

## improve

**Purpose.** Propose a change to the skill, the config, or the infra.

**Reads.** The file to change. `.colab/rollout.json` when the change concerns a gate. Bus logs when the change concerns delivery.

**Writes.** A PR labelled `self-improve` in the target repo.

**Steps.**

1. State the change and the measured reason for it.
2. Make the change on a branch. Run `colab.py scan` on each changed file.
3. Open a PR labelled `self-improve`. Link the evidence.
4. If the change touches the scanner, tiers, or bus rules, request lead approval.
5. After the PR merges, record any gate with `colab.py gate --evidence <link>`.

**Guardrails.**

- Changes to the scanner, tiers, or bus rules need lead approval.
- A rollout gate is recorded only with evidence. Evidence is a link to a run, PR, or scan output.
- Phase 3 requires an improve PR merged with a measured gain.
