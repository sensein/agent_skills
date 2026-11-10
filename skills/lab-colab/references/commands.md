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
| link | Issue body | No | None |
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
2. Choose the subscriber repo and, optionally, an `events` list.
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
