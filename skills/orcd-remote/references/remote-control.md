# Claude Code on ORCD with remote control

Goal: a Claude Code session that runs on ORCD for days, which you drive from the Claude app, claude.ai/code or your phone. The work, and any data it reads, stays on the cluster. It runs as a chain of Slurm jobs, not on a login node.

The scripts are in [`scripts/claude_rc/`](../scripts/claude_rc/): `claude-rc-submit.sh` (what you run) and `claude-rc.sbatch` (the job).

## Before you start

1. **An ORCD account and ssh access**: see [setup.md](setup.md). You need a key installed through the OnDemand portal.
2. **A partition you can use for days.** A group partition (for sensein, `pi_satra`, through `orcd_ug_pi_satra_all`; see [sensein.md](sensein.md)) is best. `mit_normal` (12 h) works too, with more hand-overs. `start` refuses partitions under 12 hours, because each successor starts 2 hours early and waits; `check` marks those and the preemptable or transfer-only ones to avoid.
3. **Claude Code on ORCD**, installed in your home without root:

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash      # official installer; puts `claude` in ~/.local/bin
   ```

4. **Sign in once, interactively**, in your ssh session: run `claude`, sign in with your lab Claude account (it prints a link to open on your laptop or phone), answer the folder-trust prompt, then `/exit`. Do this in the folder you will serve (default `~/orcd/scratch`).

## Start it

Run these on a login node:

```bash
RC=<orcd-remote skill dir>/scripts/claude_rc
bash $RC/claude-rc-submit.sh check                  # ✓/✗ per prerequisite, and the partitions you can use
bash $RC/claude-rc-submit.sh start -p <partition>   # default: serve ~/orcd/scratch for 14 days, 2 CPUs, 16G
bash $RC/claude-rc-submit.sh status                 # jobs and the latest log lines
```

`start` options:
- `-d "2026-12-20 09:00"` or `-d "+30 days"` sets the deadline;
- `-c` and `-m` set CPUs and memory;
- `-w <folder>` sets the folder served. Run `claude` in it once and trust it first, or the session waits at that prompt. `start` refuses your home folder, which holds your keys and sign-ins.

Once the job starts, the log shows "Connected", and the session is listed in your Claude app and at claude.ai/code, where you can start conversations in it.

## What the chain does

- Each run lasts up to the partition's MaxTime, capped at 2 days, the deadline, and the next maintenance window. The window comes from a scheduled `MAINT` reservation, or the third Tuesday of the month.
- About 90 seconds after `claude remote-control` starts, the run submits its successor to begin 2 hours before it ends. The successor waits for its predecessor to exit, then takes over, so the hand-over is a short reconnect.
- If no successor was submitted, the run's `USR1` warning, 10 minutes before the end, submits one to follow it.
- `claude update` runs at each start.
- Every leftover process in the job is reaped at the end.
- `start` copies the job script into `RC_DIR`, by default a `claude-rc` folder in your `~/orcd/scratch`, mode 700. Successors use that copy, so updating the skill never breaks a running chain. Logs are `RC_DIR/claude-rc-<jobid>.out`.

## Stop or change it

- Stop: `scancel -n claude-rc` cancels the running job and its queued successor.
- Change the deadline, partition or size: stop it, then `start` again.
- One chain per person. `start` refuses while a `claude-rc` job exists.

## Notes

- Remote control serves one folder. A second chain on the same folder fails with "already served"; the job retries for a while, then exits.
- Sessions on ORCD don't get the claude.ai GitHub connector. Add GitHub's MCP server per [github-mcp.md](github-mcp.md), and run `gh auth login` on ORCD for the `gh` CLI.
- Restricted data rules still apply: the session sends what it reads to the model API. Process restricted data only in jobs with open models.
