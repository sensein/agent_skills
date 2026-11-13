# Running a standby bus (operators): optional relay

> **Optional relay.** Since 2026-11-13 the lab works through each member's own credentials: the board (`colab.py sync`), the news (`colab.py inbox`), the Security gate (each `claim` and `run` scans the item itself), contributions, and sessions need no bus. The bus is kept only as an optional relay for push-style delivery issues, turned on with `[bus] enabled = true`. If your org does not set that, you do not need this page; `colab.py plan` lists the steps to retire an existing bus.

The bus is a small job that polls GitHub every few minutes and files delivery issues in subscriber repos. It may also keep the board current and publish its own security state, but nothing reads that state any more.

When an org runs it, the main bus runs by itself on GitHub Actions, signed in as the lab's GitHub App, so nobody has to keep it running and nobody holds its key (an org owner sets it up: [runbook.md](./runbook.md), section 4). This page is for a **standby**: a second bus on a cluster such as ORCD that takes over if the Actions bus stops (or the only bus, in an org without an App). A standby runs under **your** GitHub login: you are its operator.

## Who can run one

- Any member of the org team `colab-bus-operators`.
- One bus is active per org, normally the Actions bus. Others stand by, possibly at other institutions, and take over in order if the active one stops (for the Actions bus: no run for 45 minutes).
- Your bus sees and does only what you can on GitHub. A standby's operator needs the same access as the primary's.

## Once: get set up

1. **Declare your bus.** Add an entry to the meta repo's `.colab/config.toml` by PR:

   ```toml
   [[buses]]
   id = "handle-orcd"         # short, unique in the org
   operator = "handle"        # your GitHub login
   host = "MIT ORCD"          # where it runs, in words
   role = "standby"           # "primary" only in an org without an Actions bus
   order = 1
   ```

2. **Ask an org owner to add you to the team.** Once your entry is merged by a reviewed PR, `colab.py plan` shows a manual step with the exact command to add you to `colab-bus-operators`; an owner runs it by hand (apply never adds members). The first time, the plan also creates, in order, the protected default branch, the team, and the `colab-state` ruleset; once you are in the team, you run the plan's last step yourself to create the branch (only operators may create it).

3. **Give your assistant a lesser token.** Your assistant's everyday GitHub calls should not use your full login, because your login can change the bus's state. Run:

   ```bash
   python3 <skill folder>/scripts/colab.py setup --operator
   ```

   - It opens with a link to GitHub's token page, already filled in: the name, the org, 90 days, and only these rights: read and comment on issues and pull requests, read files, use the lab board, see members. No pushing, no admin, no Actions variables.
   - Click **Generate token**, copy it, run `setup --operator` again and paste it when asked. It does not show on screen.
   - Setup checks the token without changing anything on GitHub. If it could push to `colab-state` or has admin rights, setup says so and does not use it.
   - It stores the token privately (`~/.config/lab-colab/assistant-token`, readable only by you) and points Claude Code at it (`GH_CONFIG_DIR` in `~/.claude/settings.json`, with a backup). Your own terminal keeps your full login.
   - If the link does not fill the form (GitHub changes things), the details line lists the same steps by hand.
   - If your org asks owners to approve tokens, an owner approves it once.

4. **Start the job.** `setup --operator` prints these steps with your paths filled in:
   1. Copy `bus/colab-bus.sbatch.template` from the meta repo into your private bus folder (group storage, never scratch, never git) as `colab-bus.local.sbatch`.
   2. Fill `PARTITION`, `STATE_DIR`, `META_CLONE`, `PYTHON` (3.11 or newer), and `BUS_ID`.
   3. `sbatch colab-bus.local.sbatch`. The job uses your own `gh` login on that host (it unsets the assistant's profile), and requeues itself.

## Day to day

- `colab.py bus status` says which bus is running and when each was last heard from.
- The `colab-watchdog` workflow alerts the lab when no bus holds a fresh lease, naming the operators to contact.
- When your standby took over because the Actions bus stopped, fix the workflow (the Actions tab shows why its runs fail), then hand the lease back: `colab.py bus handback actions` (the Actions bus's id). If your bus was the primary and a standby took over while yours was down, yours stands by when it comes back. Hand the lease back when you are ready: `colab.py bus handback <your bus id>`. Any operator can hand the lease to any bus, so agree on it among the operators.
- To stop for good: cancel the job, remove your `[[buses]]` entry by PR, and ask an owner to apply the plan (and to take you out of the team).

## What keeps this safe

The Security gate does not depend on any of this: every member's CLI scans an item when it acts and checks clears on the audit issue itself. What follows protects the relay's own state.

- Only the lab's App and `colab-bus-operators` can write the `colab-state` branch (a repository ruleset). Every member can read it.
- Each poll is one commit with compare-and-set on the branch head, so two buses cannot both be active.
- The bus checks that the branch's latest commit is its own, another operator's, or the App's; anything else is reported and overwritten.
- Every delivery carries a hidden key; a bus never delivers the same event twice, even across a handover, and only issues opened by operators count (a copied marker does nothing).
- How fresh the lease is comes from GitHub's own commit time, not your machine's clock.
- The lesser token limits your assistant's ordinary GitHub calls. It is **not** a boundary against a hijacked agent running as your user: such an agent could switch back to your full login (unset `GH_CONFIG_DIR`, or read your gh config) and push to `colab-state`. This is why the lab's main bus runs as the App on GitHub Actions instead. Keep the bus folder private, and turn on `[security] signing = "ssh"` with the key on the bus host for defence in depth.
