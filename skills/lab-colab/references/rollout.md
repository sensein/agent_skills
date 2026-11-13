# Rollout

Every space joins in phase 0 with the low-risk parts. Members' `colab.py sync` (Spaces field, new items on the board, their own Contributions) is board bookkeeping under each member's own login and runs from phase 0. The `bus_live` switch matters only to the optional relay ([bus] enabled = true). Risky parts switch on per space, and only after that space passes the previous phase's gate. `colab.py gate` records a pass in `.colab/rollout.json` and turns on the next phase's switches for that one space.

| Phase | Reaches | Switches it opens for the space | Gate to pass before the next phase |
|---|---|---|---|
| 0 Foundation | Every space at once | (the optional relay, if on, runs in dry-run) | `colab.py scan --selftest` passes and the meta repo scans clean; the org-wide forms exist; members' sync keeps the space's Spaces view current |
| 1 Distill + sessions | Per space | `bus_live` (relay deliveries only) | One full cycle: distill, claim, PR, handoff, with the lease honoured |
| 2 Knowledge + routing | Per space | `agent_proposals`, `auto_intake`, `restricted` | 50 accepted knowledge nodes; BrainKB sandbox round trip; catch-up under 2 minutes |
| 3 Self-evolution | Per space | `self_improve` | An improve PR merged with a measured gain; cost per task against a Claude-only baseline |

The config's `[[phases]]` table is authoritative; this page explains it.

## What each kind of plan action means

| Kind | Who acts | How |
|---|---|---|
| `gh` | `colab.py apply --yes` | Org metadata: repo creation (the meta repo, and the marketplace repo with its Actions workflow permissions), issue types, the Project, its fields (Spaces, Iteration included) and views. No labels are created up front. Throttled to 1 write per second |
| `local` | The agent, in a clone | A file change sent as a reviewed PR, scanned before push |
| `manual` | A person | Steps the API cannot do, or choices that belong to people (defining spaces, setting `[org] project_number` after the Project exists) |
| `gate` | The agent with a person | Check the criterion against evidence, then `colab.py gate ... --evidence <link>`, committed by PR |

The marketplace steps appear only when `[marketplace] repo` is set. Once the repo exists with its permissions and starter files, the plan drops them, including the claude.ai `manual` steps (which the API cannot observe).

An org admin checks for drift by running `plan` (the optional relay, on a cluster, can also report it in one `Drift report` issue); see [`runbook.md`](./runbook.md), and for the cases where re-applying loses data.

## Recording evidence

A gate entry is `{space, phase, date, evidence}`. Evidence is a link to something durable: a PR, a workflow run, a scan output committed to the meta repo, or a dashboard snapshot. Never record a gate on a claim without a link.

## Rolling back

Reverting the PR that changed `.colab/rollout.json` closes the switches again. Members' commands read it from the meta repo when they act (and the optional relay on every poll), so a revert takes effect at once.

## Federation (later)

Other orgs the group runs can adopt the same config; each needs only its config repo and a Project, and nothing is installed there. Members sync the external repos they can read onto their own org's Project; cross-org dependencies use a mirror issue in each org, linked by URL.
