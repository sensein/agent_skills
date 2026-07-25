---
name: orcd
description: Use MIT ORCD (Engaging) as a remote execution environment from Claude Code, Cowork, or Science - set up key-based SSH through the OnDemand portal, discover which Slurm partitions, GPU models, and storage tiers the current user is actually entitled to rather than assuming, place job IO on the fast bcs flash scratch, and submit and track work.
---

# MIT ORCD Remote Execution

Use this skill when work should run on MIT's ORCD cluster (`orcd-login.mit.edu`,
Slurm cluster `eofe7`, also called Engaging) instead of a laptop: GPU training,
large parallel CPU jobs, or anything reading datasets that already live on
cluster storage.

The skill exists because three things about ORCD are easy to get wrong and cost
hours each time:

1. **SSH auth is two-factor even with a key.** A misconfigured client fails with
   a message that points at the wrong cause.
2. **Entitlements are per-person and invisible.** Which partitions, GPU models,
   and storage a user can touch is set by Unix group membership. Two people in
   the same lab get different answers, so nothing can be hardcoded.
3. **The storage tiers differ by more than 10x in speed**, and the slowest one
   (`$HOME`) is the default working directory.

Everything below is discovered at runtime by the scripts. The concrete numbers
quoted in `references/` are illustrative snapshots from one account, not
constants -- always trust the script output over the docs.

## Start here, every session

```bash
cd skills/orcd/scripts
python3 orcd_doctor.py          # is access working? if not, exactly what to fix
```

`orcd_doctor.py` walks the preconditions in dependency order and stops at the
first broken one. When SSH is not yet set up it prints the full portal
walkthrough. Exit status is 0 only when the cluster is reachable, so it is safe
to gate on.

Then, once it passes:

```bash
python3 orcd_resources.py --gpus --idle   # what you can run on, and what is free
python3 orcd_storage.py                   # where to put data, and what is fast
```

## How the connection works

ORCD's sshd requires `publickey` **and** `keyboard-interactive`. The key alone
leaves the session in "partial success"; Duo then answers the second stage,
usually with **zero prompts**, because a recent sign-in at
<https://orcd-ood.mit.edu/> established device trust.

Two consequences matter:

- **Never set `BatchMode=yes`.** It disables keyboard-interactive on the client,
  so authentication always fails with `Permission denied (keyboard-interactive)`
  even though the key is fine. This is the single most common false alarm, and
  it looks exactly like a rejected key.
- **Use connection multiplexing.** The first connection is the expensive and
  occasionally interactive one; a `ControlMaster` socket with
  `ControlPersist 12h` makes every later command reuse it. One authentication
  covers a whole session, and `scp` rides the same socket.

`orcd_doctor.py --fix` writes a correct `~/.ssh/config` block. All scripts share
this plumbing through `orcd_common.py`, so use them (or `ssh orcd`) rather than
hand-rolling SSH flags.

If Duo trust has lapsed, a real prompt appears and non-interactive calls fail
fast. The fix is to sign in at the portal again, or run `ssh orcd` once by hand
and answer the prompt.

## First-time setup for a new group member

ORCD accepts no password over SSH, so a key has to be installed through the web
portal, which does support Duo. `orcd_doctor.py` prints this walkthrough when it
detects the key is missing; the full version with troubleshooting is in
[references/setup.md](references/setup.md).

The short form:

1. `ssh-keygen -t ed25519` if there is no key yet.
2. Sign in at <https://orcd-ood.mit.edu/> with MIT credentials plus Duo.
3. **Clusters -> Shell Access** gives an already-authenticated shell.
4. Append the laptop's `.pub` key to `~/.ssh/authorized_keys` there.
5. `python3 orcd_doctor.py --fix` locally to write the SSH config and connect.

A new account may reach step 5 and still have no Slurm association or storage
groups. The doctor reports both as warnings; the fix is an email to
orcd-help@mit.edu, not a config change.

## Discovering what a user can run on

Never assume a partition exists or is usable. Partition access is gated by
`AllowGroups`, and the per-user ceilings come from a QOS that each partition
attaches automatically. `orcd_resources.py` resolves both by asking the
scheduler:

- `scontrol show partition` lists every partition and its gates.
- `sbatch --test-only` is the authoritative yes/no. It queues nothing, and it
  also reports **when the request would start** -- the single most useful number
  for choosing where to send work.
- `sacctmgr show qos` gives the ceilings that will bind, distinguishing
  `MaxTRESPU` (yours alone) from `GrpTRES` (**one pool shared with the whole
  group**, so a colleague's job can block yours).
- GPU inventory is read from each partition's `TRES` string, which Slurm has
  already aggregated. Summing `sinfo` output instead undercounts badly, because
  `sinfo` collapses identically configured nodes onto one line.

Two rules that fall out of this, and that are easy to get wrong:

- **Do not pass `--qos`.** Each partition supplies its own QOS. Most users'
  associations do not permit naming those QOS explicitly, so adding the flag
  turns a working request into `Invalid qos specification`. Choose the
  partition; the QOS follows.
- **Request GPUs by model when it matters:** `--gres=gpu:h100:2`. Untyped
  `--gres=gpu:2` may land on anything from an L4 to an H200. A few partitions
  declare GPUs without a model and only accept the untyped form; the script
  labels those `untyped`.

See [references/slurm.md](references/slurm.md) for partition selection strategy,
priority weighting, and submission recipes.

## Storage: put job IO on flash, not in $HOME

ORCD encodes storage entitlement in group names of the form
`orcd_rg_<server>_<owner>`, and the server prefix names the hardware tier:

| Prefix | Tier | Use for |
| --- | --- | --- |
| `fstor*` | flash, NFS over RDMA | active job IO: checkpoints, intermediates, hot datasets |
| `hstor*` | capacity disk | datasets and results worth keeping |
| `core*` | archive | cold data; retrieval is slow |
| `nfs*` | shared `/home` | code and config only |

So a user's group list plus the mount table is enough to derive what is
reachable and what is worth using. `orcd_storage.py` does exactly that, and
`--setup` creates the per-user directories that do not exist yet.

The ordering that matters in practice, fastest first: node-local `/dev/shm`
(RAM, counts against the job's memory), node-local disk, **bcs flash scratch**,
capacity disk, `$HOME`. `$HOME` is both the default working directory and the
worst tier for many-small-file work -- 6-10x slower metadata than bcs flash
across two measurements. Clone code there; write output elsewhere.

Two traps worth knowing before writing any script:

- **Bare `df -h` can hang for minutes** on a login node when any network mount
  is unresponsive. Read `/proc/mounts` instead, which never blocks, and size
  individual paths under `timeout`.
- **`/orcd` is autofs.** A directory materialises only when something touches
  it, so listing a parent is not a reliable inventory. `orcd_storage.py`
  enumerates the LDAP automount maps to find project trees.

[references/storage.md](references/storage.md) has measured throughput per tier
and the stage-in/stage-out pattern for jobs that do heavy small-file IO.

## Submitting and tracking work

```bash
# Ask where a request would land and how soon, without queueing anything
python3 orcd_submit.py --plan --gpus 1 --gpu-type h100 --cpus 8 --mem 64G --time 2:00:00

# Submit, auto-selecting the soonest-starting partition
python3 orcd_submit.py --script train.sh --gpus 1 --gpu-type h100 --cpus 8 --mem 64G --time 4:00:00

python3 orcd_submit.py --queue
python3 orcd_submit.py --status <jobid>
```

`--plan` is worth running before any long job. Partition choice routinely
changes start time from minutes to days, and it is one flag.

Always set `--mem` explicitly. The cluster default is `DefMemPerCPU=1000`, so a
4-CPU job silently gets 4 GB and dies part-way through anything real.

For interactive work, ask for a shell on a compute node rather than working on
the login node:

```bash
ssh orcd -t 'srun -p mit_quicktest -t 15 -n 1 --mem=8G --pty bash'
```

## When something goes wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Permission denied (keyboard-interactive)` | `BatchMode=yes`, or Duo trust lapsed | Remove `BatchMode`; sign in at the portal again |
| `Invalid qos specification` | passed `--qos` | Drop the flag; pick the partition instead |
| `Requested node configuration is not available` | GPU model absent from that partition | Check `orcd_resources.py --gpus` for models per partition |
| Job killed part-way, no clear error | hit the 1 GB/CPU memory default | Set `--mem` explicitly |
| Job waits for days | shared partition is congested | `--plan` and pick a private or preemptable partition |
| Job vanished and requeued | ran in a `PreemptMode=REQUEUE` partition | Expected; checkpoint, or use a non-preemptable partition |
| A command hangs forever | bare `df -h`, or a stale mount | Use `/proc/mounts`; wrap probes in `timeout` |
| Everything is mysteriously slow | job IO is in `$HOME` | Move it to bcs flash scratch |

Preemptable partitions (`mit_preemptable`, `ou_bcs_low`) trade priority for
capacity. They are the right choice for checkpointed or idempotent work and the
wrong one for a long job that cannot resume.

## Scripts

| Script | Purpose |
| --- | --- |
| [`orcd_doctor.py`](scripts/orcd_doctor.py) | Verify access; print the exact remedy when it fails. `--fix` writes SSH config |
| [`orcd_resources.py`](scripts/orcd_resources.py) | Discover usable partitions, GPU models, QOS ceilings, live free capacity |
| [`orcd_storage.py`](scripts/orcd_storage.py) | Discover writable storage by tier and speed. `--setup` creates per-user dirs |
| [`orcd_submit.py`](scripts/orcd_submit.py) | Plan, submit, and track jobs; auto-select partition by start time |
| [`orcd_common.py`](scripts/orcd_common.py) | Shared SSH plumbing (multiplexing, base64 payloads, error mapping) |

All scripts are stdlib-only Python 3 and run locally, reaching the cluster over
one multiplexed SSH connection. Every one accepts `--host` to target a different
SSH alias, and the data-gathering ones accept `--json`.
