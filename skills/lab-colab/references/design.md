# Lab Colab design

This file defines the work graph, tiers, bus, knowledge flow, and intake for a Lab Colab Space. Read it once per session.

## 1. Work graph

### 1.1 Item types

| Type | Description | Done means | Required fields |
|---|---|---|---|
| Idea | A possibility worth triaging. | Promoted, parked, or dropped. | idea |
| Question | A research question with hypotheses and linked evidence. | Hypotheses recorded with linked evidence. | question |
| Experiment | Tests a hypothesis and records results. | Results recorded with provenance, including negative results. | hypothesis, data, metrics |
| Review | A literature review. | Query, sources, inclusion rule, and synthesis recorded. | query, sources, inclusion |
| Software | A software change or project. | Released and documented. | goal, repo |
| Model | Builds or adapts a model. | Evaluated against a baseline, with a model card. | task, baseline |
| Infrastructure | Containers, launchers, and runbooks. | A second person verifies the runbook. | need |
| Dataset | Curated data with a datasheet. | Datasheet written and access terms registered. | description, access |
| Output | A paper, preprint, talk, or grant section. | Not defined in config. Use Status. | venue |
| Task | A unit of work under any item. | Not defined in config. Use Status. | work |

### 1.2 Edges

Native edges use GitHub links. Body edges use a `links` block in the issue body. Knowledge-only edges are set in the knowledge merge step.

| Edge | Kind | Where it is set | Domain |
|---|---|---|---|
| part-of | native | GitHub sub-issue | any |
| depends-on | native | GitHub blocked-by | any |
| addresses | body | `links` block | source is Experiment or Review |
| uses | body | `links` block | any |
| produces | body | `links` block | any |
| informs | body | `links` block | any |
| contributes-to | body | `links` block | any |
| derived-from | body | `links` block | any |
| supersedes | kg-only | knowledge merge step | not in issue bodies |
| contradicts | kg-only | knowledge merge step | not in issue bodies |
| supports | kg-only | knowledge merge step | not in issue bodies |
| refines | kg-only | knowledge merge step | not in issue bodies |

Grammar for the `links` block. One edge per line:

```links
uses: example-org/example-project#12
derived-from: kg:example-node-1
```

- `<edge>: <owner>/<repo>#<n>` targets a work item.
- `<edge>: kg:<id>` targets a knowledge node.
- The parser rejects native and knowledge-only edges in issue bodies.

### 1.3 Node classes

| Class | Identified by | Lives in |
|---|---|---|
| Work item | `<owner>/<repo>#<n>` | Its member repo, as an issue |
| Knowledge node | `kg:<id>` IRI | BrainKB; staged in the meta repo first |

### 1.4 Spaces

- A space is a named area of work declared in `[spaces]` in the meta-repo config: one or more stewards, a list of existing repos, and an optional `tier = "restricted"`. It is not a repo or a folder.
- Items in a listed repo belong to that space. Spaces may share repos.
- An item joins a space its repo is not in through a `space:<name>` label, created on first use.
- An item belongs to its repo's spaces plus any space named by a `space:<name>` label on the item.
- Project view filters cannot OR "repo is X" with "label is Y", so membership is stored in the multi-select **Spaces** field (options = the names in `[spaces]`). Each space gets a view `Space: <name>` filtered on `spaces:<name>`.
- `colab.py plan` also creates the standing views, and fixes any existing view whose filter differs from the one below. A view's layout is set at creation and not changed afterwards.

| View | Filter | Layout |
|---|---|---|
| `Space: <name>` | `spaces:<name>` | table |
| Agent queue | `status:Ready agent-ready:yes` | table |
| Proposals inbox | `status:Proposed` | table |
| My work | `assignee:@me` | board |

## 2. Fields, and why there are almost no labels

| Name | Kind | Values | Purpose |
|---|---|---|---|
| Sensitivity | Project field, single select | public, lab, restricted | Shown on the board; mirrors the routing tier. |
| Agent-ready | Project field, single select | yes, no | An agent may claim the item without a briefing. |
| Status | Project field, single select | Proposed, Accepted, Ready, In progress, Blocked, Review, Done, Parked, Dropped | Workflow state. |
| Lead | Project field, text | none | Person accountable for the item. |
| Risk | Project field, single select | high, medium, low | Risk level. |
| Target date | Project field, date | none | Target date. |
| KG link | Project field, text | none | Knowledge node for the item. |
| Spaces | Project field, multi-select | one option per space in `[spaces]` | Space membership, kept by the bus; drives the `Space: <name>` views. Options are set by `colab.py plan`; values by the bus. |
| Iteration | Project field, iteration | 2-week cycles | Created by `colab.py plan` through GraphQL: 2-week cycles starting the next Monday, 4 iterations. |

The field list is the config's `[[fields]]`; Spaces has no options there, because they come from `[spaces]` at plan time. When options are missing, the plan proposes an update that sets the full list. GitHub may reset existing values when it does, which is fine: the bus recomputes every item's value on its next sync. The Project is found by `[org] project_number` (0 = not created yet; the meta repo sets it), or by title.

- Routing tier = the strictest of repo visibility, the space `tier` setting, and a `tier:restricted` label. That label exists only for an exception, a restricted item in a lab repo, and is created on first use.
- Agent-made items are marked by Status = Proposed alone.
- Nothing else is a label. Space membership comes from the config, and the bus marks its deliveries with a hidden body marker.

## 3. Tiers

| Tier | Visibility ceiling | Who may read | Claude session may read |
|---|---|---|---|
| public | Public | Anyone | Yes |
| lab | Lab members | Lab members | Yes |
| restricted | Named project members only | Named project members | No |

Flow rules:

1. An event moves only to an equal or stricter tier.
2. Lab to public happens only by a promotion PR with a clean scan.
3. Restricted events carry pointers only. Restricted summaries stay empty.
4. Restricted data never enters git.
5. Claude sessions never read restricted paths. Content goes to the model API.
6. ORCD open-model jobs process restricted data. They return aggregates or pointers.
7. A backlink is written only toward an equal or stricter tier.

## 4. Bus

### 4.1 Polling and state

- The bus polls GitHub. It does not use webhooks, because compute nodes accept no inbound connections.
- Default poll interval is 300 seconds.
- Requests use ETags. A 304 response means no change and costs no rate limit.
- Each repo keeps a cursor: the newest `updated_at` seen. Visibility is re-read every poll.
- The first poll of a repo is a baseline: it records the newest update and the current releases, and routes nothing. Events from before the baseline are never routed, so history cannot flood subscribers.
- State lives in a private group-storage directory, never scratch. It holds cursors, ETags, seen IDs, and logs.

### 4.2 Envelope

One envelope per item transition (opened, closed, published), not per edit.

| Field | Meaning |
|---|---|
| `id` | `<repo>:<kind>:<number>:<transition>`. The dedupe key. |
| `kind` | `issue`, `pull_request`, or `release`. |
| `transition` | `opened`, `closed`, or `published`. |
| `repo` | Source repo. |
| `tier` | Strictest of repo visibility, space `tier`, and a `tier:restricted` label. |
| `source` | HTML URL of the item. |
| `node_id` | GraphQL node id of the item (empty for releases); used by Project sync. |
| `summary` | Title or name. Empty when the tier is restricted. |
| `labels` | Label names. |
| `spaces` | Spaces from the repo's config membership, plus any `space:*` labels. |
| `sender` | Author login. |
| `at` | When the transition happened. |

A public repo sets the base tier to public. Any other repo sets it to lab.

### 4.3 Dedupe, self-filter, routing

- The bus skips any envelope ID it has seen. It keeps the newest 20,000 IDs, trimmed oldest first, and saves state after every envelope.
- The bus never routes its own deliveries: each carries a hidden HTML marker in its body. The bus adds no labels anywhere. It also ignores events from its own login, by default the token's login.
- A subscription matches on topic and on its optional `events` list.
- The bus validates each issue's fenced `links` block once per body version. It logs errors to `audit.jsonl`, and comments on the issue only when the repo's spaces are live. No per-repo workflow is needed.
- A repo shared by several spaces counts as live, or restricted-enabled, only when all of its spaces are.
- Destination tier = the stricter of the subscriber repo's real visibility and the subscription's `tier` override. An override can only tighten it. A repo whose visibility is unknown counts as public.
- A delivery needs a destination tier at or above the event tier. Otherwise the bus drops it and logs the drop to `audit.jsonl`.
- Restricted events move only for spaces whose `restricted` switch is on, and they carry no summary.

### 4.4 Modes

The mode is set per destination space in `.colab/rollout.json`. A dry-run space never receives a write.

| Mode | Behaviour |
|---|---|
| dry-run | Appends would-be deliveries to `would-deliver.jsonl`. Writes nothing to GitHub except the heartbeat and the Project sync (section 4.5b), which is board bookkeeping, not a delivery. |
| live | Opens an issue in the subscriber repo for each delivery. |

### 4.5 Heartbeat and failures

- After each poll, the bus writes repo variable `COLAB_BUS_HEARTBEAT` on the meta repo.
- The value is JSON with `at`, `envelopes`, `deliveries`, `drops`, `dry_run`, `errors`, and `project_writes`, plus `drift_actions` on polls that ran a drift check.
- A failed read or write is logged to `audit.jsonl` and the poll goes on. A failed poll backs off, from 1 minute up to 1 hour.
- The Slurm job requeues itself near its time limit and after an unexpected exit.
- The `colab-watchdog` workflow in the meta repo alerts the lab when the heartbeat is stale: 30 minutes by default, or the `COLAB_STALE_MINUTES` variable.
- Run one local poll with `colab_bus.py --meta <clone> --state-dir <dir> --token-file <file> --once`.

### 4.5a Session heartbeats

- Claude Code hooks call `colab.py beat` after tool use, so beats track real activity and stop when a session runs out of tokens.
- Each session keeps one comment in the meta repo's Session activity issue, edited in place, with marker `<!-- colab-session id= user= beat= [job=] [item=] -->`.
- A beat renews the member's lease on the claimed item. Leases default to 45 minutes; with `--job` they run to the Slurm job's scheduled end plus 15 minutes.
- Each poll, the bus classifies sessions as active (a beat within `stale_minutes`), waiting (its Slurm job is still queued or running), or stale. It adds `sessions_active`, `sessions_waiting`, and `sessions_stale` to its heartbeat, and logs each stale session that held an item once to `audit.jsonl`.

### 4.5b Project sync

Enabled by `[bus] project_sync = true` once `[org] project_number` is set. It is board bookkeeping on the private org Project, not a delivery: it runs in **dry-run spaces too** and does not wait for `bus_live`. It writes only to the Project, never to a repo.

- **Add on open.** Each newly *opened* issue or PR envelope from a space repo is added to the Project (`addProjectV2ItemById`, using the content `node_id` that the envelope carries). Items from before the baseline never produce an envelope, so they are never added. A restricted-tier item is added only for spaces with the `restricted` switch on, as for routing.
- **Periodic sweep.** On the first poll and then every `project_sync_every` polls (default 6), the bus pages through all Project items with GraphQL, computes each item's desired Spaces (repo membership plus `space:*` labels), and updates only items whose value differs. An empty desired set clears the field. A space named by a label but absent from the field's options is skipped and logged.
- **Throttle and failures.** Writes are spaced by `write_interval_seconds` and counted in the stats as `project_writes`. A failed read or write is logged to `audit.jsonl` (`project_write_failed`), counted in `errors`, and the poll goes on.
- **Token.** The bus token needs Projects read and write on the organization, in addition to the grants in SKILL.md.

### 4.5c Drift check

Enabled by `[bus] drift_check_hours` (default 24; 0 turns it off). It is read-only: the bus never applies a plan.

- At most that often, the bus runs `observe(config)` and `build_plan` with the config it already loads. `observe` shells out to `gh`, so `gh` must be on the job's PATH and authenticated.
- It drops the actions that always recur in a plan: `gate`, "Start the bus", and the informational own-templates `manual` note.
- If anything remains, it creates or updates one issue titled `Drift report` in the meta repo. The body lists each proposed action and the `colab.py plan` and `apply --yes` commands to review it. The issue is edited in place, never commented on again. The body carries the delivery marker, so the bus does not route it.
- When the plan is clean it closes the issue with a short comment.
- It records `drift_actions` in the heartbeat stats, and the last check time (`drift_checked_at`) in state. A failed check, including an observation with no repos (gh unavailable), is audited as `drift_check_failed`, counted in `errors`, and retried after an hour. It never stops the poll.

### 4.6 Throttle and digest

- Live writes and Project writes are throttled to one per second (`write_interval_seconds = 1.0`).
- Above 20 matching subscribers (`fanout_digest_threshold`), one digest issue in the meta repo replaces the fan-out. The tier rule still applies to it.

### 4.7 Subscriptions

File: `.colab/subscriptions.toml` in the meta repo.

| Key | Values |
|---|---|
| `topic` | `repo:<name>`, `label:<label>`, or `space:<space>` |
| `events` | Optional list: `issue`, `pull_request`, `release` |
| `subscriber` | Repo that receives the delivery |
| `deliver` | `issue` (default). The bus delivers only issues. |
| `tier` | Optional override of the subscriber's tier |

## 5. Knowledge and credit

- Captures are staged as JSON-LD-style files in `.colab/staging/` in the meta repo, through a PR.
- The item's Lead reviews each staged node.
- Approved nodes sync to BrainKB through its MCP server.

| Node tier | BrainKB destination |
|---|---|
| public | Shared spaces |
| lab | Private space |
| restricted | Not synced |

- CRediT roles are proposed by the bus and confirmed by people. A proposed role is not final until a person confirms it.
- Agent work is recorded as acting on behalf of the directing member.
- Profiles are visible to the member and the PI by default.

## 6. Intake

Material enters by paste, by upload, or by a connector pull. Connectors run under the member's own authorization: Slack, Zoom, Drive, and Box.

Flow:

1. Store raw material in private ORCD group storage first.
2. A local model on ORCD sets the tier.
3. Restricted material goes to ORCD-only processing. Claude sessions see only aggregates or pointers.
4. Proposed items follow the usual path: Status Proposed.

Consent:

- Recording a conversation in Massachusetts requires all-party consent.
- Off-record marks exclude the marked segment from processing.
- A member can withdraw material. Withdrawn material is removed from storage, and its derived items are reviewed.
