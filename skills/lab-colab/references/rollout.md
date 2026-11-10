# Rollout

Every space joins in phase 0 with the low-risk parts. Risky parts switch on per space, and only after that space passes the previous phase's gate. `colab.py gate` records a pass in `.colab/rollout.json` and turns on the next phase's switches for that one space.

| Phase | Reaches | Switches it opens for the space | Gate to pass before the next phase |
|---|---|---|---|
| 0 Foundation | Every space at once | (bus runs in dry-run) | `colab.py scan --selftest` passes and the meta repo scans clean; the org-wide forms exist; a fresh bus heartbeat |
| 1 Distill + sessions | Per space | `bus_live` | One full cycle: distill, claim, PR, handoff, with the lease honoured |
| 2 Knowledge + routing | Per space | `agent_proposals`, `auto_intake`, `restricted` | 50 accepted knowledge nodes; BrainKB sandbox round trip; catch-up under 2 minutes |
| 3 Self-evolution | Per space | `self_improve` | An improve PR merged with a measured gain; cost per task against a Claude-only baseline |

The config's `[[phases]]` table is authoritative; this page explains it.

## What each kind of plan action means

| Kind | Who acts | How |
|---|---|---|
| `gh` | `colab.py apply --yes` | Org metadata: repo creation, issue types, Project and fields. No labels are created up front. Throttled to 1 write per second |
| `local` | The agent, in a clone | A file change sent as a reviewed PR, scanned before push |
| `manual` | A person | Steps the API cannot do, or choices that belong to people (defining spaces, the Iteration field) |
| `gate` | The agent with a person | Check the criterion against evidence, then `colab.py gate ... --evidence <link>`, committed by PR |

## Recording evidence

A gate entry is `{space, phase, date, evidence}`. Evidence is a link to something durable: a PR, a workflow run, a scan output committed to the meta repo, or a dashboard snapshot. Never record a gate on a claim without a link.

## Rolling back

Reverting the PR that changed `.colab/rollout.json` closes the switches again. The bus pulls the meta repo and re-reads the file on every poll, so a revert takes effect on the next poll.

## Federation (later)

Other orgs the group runs can adopt the same config and bus. Buses exchange only public-tier events, and cross-org dependencies use a mirror issue in each org, linked by URL.
