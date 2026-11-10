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

- A space is a label `space:<name>`. It is created in each member repo of that space.
- An item can carry several space labels.
- Projects v2 filters by label but cannot group by label. Each space gets a saved view.
- Spaces are defined in `[spaces]` in the meta-repo config. Each names a steward and its repos.

## 2. Fields and labels

| Name | Kind | Values | Purpose |
|---|---|---|---|
| `tier:public`, `tier:lab`, `tier:restricted` | label | none | Canonical routing key. Visible to every API. |
| Sensitivity | Project field, single select | public, lab, restricted | Mirrors the tier label. |
| `agent-ready` | label | none | An agent may claim the item without a briefing. |
| `agent-proposed` | label | none | Created by an agent. Awaits a human accept. |
| Status | Project field, single select | Proposed, Accepted, Ready, In progress, Blocked, Review, Done, Parked, Dropped | Workflow state. |
| Lead | Project field, text | none | Person accountable for the item. |
| Risk | Project field, single select | high, medium, low | Risk level. |
| Target date | Project field, date | none | Target date. |
| KG link | Project field, text | none | Knowledge node for the item. |
| Iteration | Project field, iteration | none | Added once in the Project web UI. |

- The default issue form sets `tier:lab`.
- Routing reads the label. If the label and the Sensitivity field disagree, fix the field.

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
- State lives in a private group-storage directory, never scratch. It holds cursors, ETags, seen IDs, and logs.

### 4.2 Envelope

One envelope per item transition (opened, closed, published), not per edit.

| Field | Meaning |
|---|---|
| `id` | `<repo>:<kind>:<number>:<transition>`. The dedupe key. |
| `kind` | `issue`, `pull_request`, or `release`. |
| `transition` | `opened`, `closed`, or `published`. |
| `repo` | Source repo. |
| `tier` | Stricter of the repo tier and the item's `tier:*` label. |
| `source` | HTML URL of the item. |
| `summary` | Title or name. Empty when the tier is restricted. |
| `labels` | Label names. |
| `sender` | Author login. |
| `at` | When the transition happened. |

A public repo sets the base tier to public. Any other repo sets it to lab.

### 4.3 Dedupe, self-filter, routing

- The bus skips any envelope ID it has seen. It keeps the newest 20,000 IDs, trimmed oldest first, and saves state after every envelope.
- The bus never routes its own deliveries: each carries the `colab-delivery` label and an HTML marker. It also ignores events from its own login, by default the token's login.
- A subscription matches on topic and on its optional `events` list.
- Destination tier = the stricter of the subscriber repo's real visibility and the subscription's `tier` override. An override can only tighten it. A repo whose visibility is unknown counts as public.
- A delivery needs a destination tier at or above the event tier. Otherwise the bus drops it and logs the drop to `audit.jsonl`.
- Restricted events move only for spaces whose `restricted` switch is on, and they carry no summary.

### 4.4 Modes

The mode is set per destination space in `.colab/rollout.json`. A dry-run space never receives a write.

| Mode | Behaviour |
|---|---|
| dry-run | Appends would-be deliveries to `would-deliver.jsonl`. Writes nothing to GitHub except the heartbeat. |
| live | Opens an issue in the subscriber repo for each delivery. |

### 4.5 Heartbeat and failures

- After each poll, the bus writes repo variable `COLAB_BUS_HEARTBEAT` on the meta repo.
- The value is JSON with `at`, `envelopes`, `deliveries`, `drops`, `dry_run`, and `errors`.
- A failed read or write is logged to `audit.jsonl` and the poll goes on. A failed poll backs off, from 1 minute up to 1 hour.
- The Slurm job requeues itself near its time limit and after an unexpected exit.
- The `colab-watchdog` workflow in the meta repo alerts the lab when the heartbeat is stale: 30 minutes by default, or the `COLAB_STALE_MINUTES` variable.
- Run one local poll with `colab_bus.py --meta <clone> --state-dir <dir> --token-file <file> --once`.

### 4.6 Throttle and digest

- Live writes are throttled to one per second (`write_interval_seconds = 1.0`).
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
4. Proposed items follow the usual path: `agent-proposed` label, Status Proposed.

Consent:

- Recording a conversation in Massachusetts requires all-party consent.
- Off-record marks exclude the marked segment from processing.
- A member can withdraw material. Withdrawn material is removed from storage, and its derived items are reviewed.
