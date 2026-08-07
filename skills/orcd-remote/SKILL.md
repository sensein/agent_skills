---
name: orcd-remote
description: Use MIT ORCD (Engaging, orcd-login.mit.edu, eofe7) as a remote execution environment, driven over SSH from your laptop or workstation - not from the cluster itself. Use this skill whenever the user mentions ORCD, Engaging, "the MIT cluster", Slurm, sbatch/srun/squeue, running or training on cluster GPUs (H100/H200/A100/L40S), cluster scratch or storage or quotas, ssh problems reaching orcd-login, or asks to run anything too big for a laptop - even if they never name ORCD explicitly. Sets up key-based SSH through the OnDemand portal, discovers which partitions, GPU models, and storage tiers this user is actually entitled to rather than assuming, checks for and installs/upgrades a per-user uv in the cluster home for modern Python (never editing shell profiles without the user's approval), places job IO on flash scratch, submits and tracks jobs, and diffs cluster config over time.
---

# MIT ORCD Remote Execution

Use this skill when work should run on MIT's ORCD cluster (`orcd-login.mit.edu`,
Slurm cluster `eofe7`, also called Engaging) instead of a laptop: GPU training,
large parallel CPU jobs, or anything reading datasets that already live on
cluster storage.

**Remote means remote.** Everything here runs on your own machine and reaches
the cluster over one multiplexed SSH connection -- that is the "-remote" in the
name. The scripts are not designed to run *on* a login node: there is no `orcd`
SSH alias there to loop back through, and the login nodes' system `python3` is
3.6, too old to even parse them. If a session is already on the cluster, use
`sinfo`/`sbatch`/`sacctmgr` and the reference docs directly instead of these
scripts.

The skill exists because three things about ORCD are easy to get wrong and cost
hours each time:

1. **SSH auth is two-factor even with a key.** A misconfigured client fails with
   a message that points at the wrong cause.
2. **Entitlements are per-person and invisible.** Which partitions, GPU models,
   and storage a user can touch is set by Unix group membership. Two people in
   the same lab get different answers, so nothing can be hardcoded.
3. **The storage tiers differ by more than 10x in speed**, and the slowest one
   (`$HOME`) is the default working directory.
4. **The configuration changes underneath you.** Partitions are retired, QOS
   ceilings retuned, GPU models swapped. `orcd_snapshot.py` captures the
   configuration and diffs it against a saved baseline, so drift is something
   you read rather than something that surprises a job script.

Everything below is discovered at runtime by the scripts. The concrete numbers
quoted in `references/` are illustrative snapshots from one account, not
constants -- always trust the script output over the docs.

When reporting results to a person, lead with the decision -- what to run,
where, and when it would start -- in a few sentences, and keep the full
partition tables and command evidence below that. The discovery scripts produce
a lot of detail; the reader asked a question, not for a survey.

## Start here, every session

```bash
cd "$(dirname "$(find ~/.claude/skills/orcd-remote ~/.agents/skills/orcd-remote . -name orcd_doctor.py 2>/dev/null | head -1)")"
python3 orcd_doctor.py          # is access working? if not, exactly what to fix
```

(Or simply `cd` to this skill's own `scripts/` directory, wherever it is
installed -- the paths above cover the common install locations and a checkout
of the skills repository.)

`orcd_doctor.py` walks the preconditions in dependency order and stops at the
first broken one. When SSH is not yet set up it prints the full portal
walkthrough. Exit status is 0 only when the cluster is reachable, so it is safe
to gate on.

Then, once it passes:

```bash
python3 orcd_resources.py --gpus --idle   # what you can run on, and what is free
python3 orcd_storage.py                   # where to put data, quotas, what is fast
python3 orcd_snapshot.py --save           # baseline the config so drift is visible
```

Later, when something that used to work stops working:

```bash
python3 orcd_snapshot.py --diff           # exit 2 if the cluster changed
```

## How the connection works

ORCD's sshd requires `publickey` **and** `keyboard-interactive`. The useful
mental model: **authenticate on the web first, and SSH behaves like plain
single-factor key auth.** A sign-in at <https://orcd-ood.mit.edu/> establishes
Duo device trust, and while that trust holds, the keyboard-interactive stage
answers itself with zero prompts. Only when the web authorization lapses does
SSH become true two-factor -- a real Duo prompt appears, and every
non-interactive call fails until someone answers one.

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

**Running from a cloud agent environment?** If this session is in a cloud or
remote container (Claude Code on the web, a CI runner) rather than on the
user's own machine, say so when asking for the key to be added: the key pair
lives in that ephemeral environment, installing it grants that environment
access to the ORCD account, and it should be a dedicated, identifiable key the
user approves and later removes. Details in
[references/setup.md](references/setup.md).

A new account may reach step 5 and still have no Slurm association or storage
groups. The doctor reports both as warnings; the fix is an email to
orcd-help@mit.edu, not a config change.

## Discovering what a user can run on

Never assume a partition exists or is usable. Partition access is gated by
`AllowGroups`, and the per-user ceilings come from a QOS that each partition
attaches automatically. `orcd_resources.py` resolves both by asking the
scheduler:

- `scontrol show partition` lists every partition and its gates.
- `sbatch --test-only` is the authoritative yes/no on *access*. It queues
  nothing, and it also reports **when the request would start** -- the single
  most useful number for choosing where to send work. One blind spot: it does
  **not** check QOS TRES ceilings, so it will report an immediate start for a
  request that a `GrpTRES` group pool can never admit. `orcd_submit.py --plan`
  cross-checks the ceilings and marks such rows `EXCEEDS`.
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

**Assume only `$HOME` is backed up.** It has snapshots; treat every other tier
-- scratch, pool, capacity, group trees -- as unprotected unless ORCD confirms
otherwise in writing. The `__STORAGE_WITHOUT_BACKUP__` sentinel marks some
unprotected trees, but its absence proves nothing. Anything irreplaceable needs
an explicit archive plan, not an assumption about the tier it sits on.

**Before recommending where data should go, ask what it looks like.** A "300 GB
dataset" does not determine an answer: 300 GB as three hundred 1 GB shards and
300 GB as a million 300 KB clips have opposite constraints (streaming throughput
vs the 1 M inode cap), and read-once staging differs from every-epoch random
access. When the file count and access pattern are not stated, ask -- or give a
short branch table rather than one answer built on silent assumptions.

**Every user also gets personal space inside their home directory**, separate
from any group allocation: `~/orcd/scratch` is 1 TB of flash and `~/orcd/pool` is
1 TB of capacity disk, neither backed up, alongside the 200 GB backed-up `~`
itself. These are **symlinks whose targets are sharded per user**
(`/orcd/scratch/orcd/<NNN>/<user>`, where `<NNN>` differs from person to
person), so resolve them rather than constructing paths:

```bash
SCRATCH=$(readlink -f ~/orcd/scratch)
```

ORCD regenerates a quota report at `~/orcd/.quota` roughly every 30 minutes, and it is the only place
these limits are visible -- `df` shows the whole shared filesystem, so it will
report hundreds of free TB in a space you can put 1 TB into. Read it via
`orcd_storage.py`.

**Watch the file counts, not just the gigabytes.** `~` and `~/orcd/scratch` each
carry a **1 M inode limit**, which one unpacked image dataset or a couple of
conda environments will reach at a few percent of the space quota. It surfaces as
a disk-full error against a quota that looks fine, and it is the strongest reason
to keep datasets as archives or container images rather than loose files.

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

## Group layer: sensein conventions

On top of the generic discovery sits a group-specific layer,
[references/sensein.md](references/sensein.md), with a machine-readable twin at
[assets/sensein.json](assets/sensein.json) that `orcd_storage.py` loads
automatically (another group can substitute its own via `--group-config` or
`ORCD_GROUP_CONFIG`). It covers three things the cluster cannot tell you:

- **Which data trees are ours and what they are for** -- `/orcd/data/satra`
  (lab-wide), `/orcd/data/dandi`, `/orcd/data/linc`, plus the projects that
  grant subtrees instead of whole filesystems (abcd, sails, kiva).
- **How access is actually granted**: WebMoira groups managed by the sensein
  admin team, not orcd-help. `orcd_storage.py` prints which projects your Unix
  groups already cover and the exact WebMoira list to ask about for the rest.
- **Consolidation conventions**, so twenty people do not hold twenty copies of
  the same model: one shared `HF_HOME` and `models/` tree, shared `datasets/`,
  per-project dirs, and per-user dirs under `users/`.

One rule from that file worth repeating here: **anything written down for the
group uses symlink forms** (`~/orcd/scratch`, resolved at runtime with
`readlink -f`), never a resolved `/orcd/scratch/orcd/<NNN>/<user>` path --
those shard numbers are per-person and wrong for everyone else.

## Python on the cluster: uv in `$HOME`

The login nodes' system `python3` is 3.6, and neither `uv` nor `conda` is
installed system-wide. The supported way to get modern Python for jobs is a
per-user `uv` in the cluster home directory, at `~/.local/bin/uv`:

```bash
python3 orcd_uv.py             # installed on the cluster? what version? on PATH?
python3 orcd_uv.py --install   # install it, or upgrade one already there
```

`orcd_doctor.py` also reports uv's presence as part of its cluster checks.
`--install` runs the official standalone installer with `UV_NO_MODIFY_PATH=1`
(and upgrades an existing install via `uv self update`), so it **never edits
shell startup files**. Nothing depends on the profile anyway: scripts and
sbatch job scripts should call uv by absolute path (`$HOME/.local/bin/uv`) or
export PATH themselves, which works in every shell.

**Any shell-profile change requires the user's explicit approval -- ask
first.** That covers `~/.bashrc`, `~/.bash_profile`, and `~/.profile`, on the
cluster or locally, whether edited directly or by an installer. When the user
wants `uv` on their interactive PATH, show them the exact line and file, get a
real yes, and only then run:

```bash
python3 orcd_uv.py --add-to-path --user-approved
```

The script enforces the rule: without `--user-approved` it asks for a typed
confirmation on a TTY and refuses outright in a non-interactive run. It backs
up the profile before appending, and afterwards verifies whether a fresh
non-interactive SSH actually sees uv (an interactivity guard at the top of
`~/.bashrc` can swallow the line for scripts -- it reports that honestly
rather than claiming success).

Keep environments and caches off `$HOME`: resolving an environment is exactly
the many-small-file workload that eats the 1 M inode quota. In job scripts,
put both on flash scratch:

```bash
export UV_CACHE_DIR="$(readlink -f ~/orcd/scratch)/uv-cache"
uv venv "$(readlink -f ~/orcd/scratch)/envs/myproj"      # not ~/envs
```

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

Point job output off `$HOME` too: `sbatch` runs the job with its working
directory set to the submission directory -- `$HOME` when submitting through
this skill -- so the default `slurm-<jobid>.out` lands on the slowest tier.
Harmless for a smoke test, wrong for anything that writes real output: pass
`--output '<flash-scratch-path>/%x-%j.out'` (the submit script prints this
reminder whenever `--output` is not set).

### Job-count and array limits

Three ceilings stack and the smallest wins: the **association `MaxSubmit`** (one
limit across every partition, and the one people miss because it does not appear
in `scontrol show partition`), the partition QOS **`MaxSubmitPU`**, and
**`MaxArraySize`**.

Array tasks each count as a submitted job, so an array larger than the target
partition's `MaxSubmitPU` is refused with `QOSMaxSubmitJobPerUserLimit` — and
**`-a 0-999%50` is refused identically**, because `%K` caps concurrent *running*
tasks, not submitted ones. Measured by bisection, the largest accepted array is
`MaxSubmitPU + 1`. Split bigger sweeps into consecutive arrays, or give each task
more work to do. `orcd_snapshot.py` prints the derived cap per partition.

### Multi-GPU: within one node or across nodes

The request shape matters as much as the count. GPUs on one node talk over
NVLink/PCIe; GPUs on different nodes talk over the network -- a large bandwidth
and latency gap, and cross-node training needs a distributed launcher besides.
Nodes here carry 4 or 8 GPUs, so up to 8 can be had within a single chassis:

```bash
-N 1 --gres=gpu:h100:4      # four GPUs on ONE node -- what most training wants
-N 2 --gres=gpu:h100:4      # four PER NODE, eight total, cross-node
```

Availability differs too: one node with 4 free GPUs is scarcer than 4 free GPUs
scattered across a partition, so a `-N 1` multi-GPU request can queue longer
than the same count spread out. `--plan` reflects this -- compare both shapes
before committing. See [references/slurm.md](references/slurm.md).

### Preemptable partitions are extra capacity

`mit_preemptable` spans nearly the whole cluster — an order of magnitude more
nodes and GPUs than any group partition, including models available nowhere else.
The price is `PreemptMode=REQUEUE`. Treat it as a second, much larger pool that is
free to use whenever the work can be interrupted: checkpointed training,
idempotent array tasks, any sweep where losing a task costs a restart. To survive
requeue, trap `--signal=B:USR1@120`, checkpoint, and **exit non-zero** — a clean
exit tells Slurm the job finished. Group partitions such as `ou_bcs_low` are
preemptable on the same terms. See [references/slurm.md](references/slurm.md).

For interactive work, ask for a shell on a compute node rather than working on
the login node:

```bash
ssh orcd -t 'srun -p mit_quicktest -t 15 -n 1 --mem=8G --pty bash'
```

## When something goes wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Permission denied (keyboard-interactive)` | `BatchMode=yes`, or Duo trust lapsed | Remove `BatchMode`; sign in at the portal again |
| Connection times out; doctor FAILs `tcp port 22` | network policy blocks SSH egress (common in cloud agent sessions) | Not a key/Duo problem; loosen the environment's network policy or run from a machine with SSH access |
| Duo suddenly prompts on every ssh | web authorization expired | Sign in at <https://orcd-ood.mit.edu/> once; ssh goes silent again |
| Locked out entirely | 10 failed Duo attempts locks the account for 90 min | Stop retrying -- close VS Code Remote-SSH, whose auto-reconnect resets the timer |
| `Invalid qos specification` | passed `--qos` | Drop the flag; pick the partition instead |
| `Requested node configuration is not available` | GPU model absent from that partition | Check `orcd_resources.py --gpus` for models per partition |
| Job killed part-way, no clear error | hit the 1 GB/CPU memory default | Set `--mem` explicitly |
| Job waits for days | shared partition is congested | `--plan` and pick a private or preemptable partition |
| Job vanished and requeued | ran in a `PreemptMode=REQUEUE` partition | Expected; checkpoint, or use a non-preemptable partition |
| A command hangs forever | bare `df -h`, or a stale mount | Use `/proc/mounts`; wrap probes in `timeout` |
| Everything is mysteriously slow | job IO is in `$HOME` | Move it to flash scratch |
| `QOSMaxSubmitJobPerUserLimit` | array bigger than `MaxSubmitPU`; `%K` does not help | Split into arrays of `MaxSubmitPU + 1` or fewer |
| Disk full, but quota looks fine | hit the 1 M inode limit | Check the file columns in `orcd_storage.py` |
| `uv: command not found` on the cluster | not installed, or `~/.local/bin` not on PATH | `orcd_uv.py --install`; call `$HOME/.local/bin/uv` by absolute path, or ask the user to approve a profile edit |
| Worked last month, fails now | cluster config changed | `orcd_snapshot.py --diff` |

## Scripts

| Script | Purpose |
| --- | --- |
| [`orcd_doctor.py`](scripts/orcd_doctor.py) | Verify access; print the exact remedy when it fails. `--fix` writes SSH config |
| [`orcd_resources.py`](scripts/orcd_resources.py) | Discover usable partitions, GPU models, QOS ceilings, live free capacity |
| [`orcd_storage.py`](scripts/orcd_storage.py) | Discover writable storage by tier and speed. `--setup` creates per-user dirs |
| [`orcd_uv.py`](scripts/orcd_uv.py) | Check/install/upgrade `uv` in the cluster `$HOME`; PATH profile edits only with explicit user approval |
| [`orcd_submit.py`](scripts/orcd_submit.py) | Plan, submit, and track jobs; auto-select partition by start time |
| [`orcd_snapshot.py`](scripts/orcd_snapshot.py) | Structured config summary; `--save` a baseline and `--diff` to see drift |
| [`orcd_common.py`](scripts/orcd_common.py) | Shared SSH plumbing (multiplexing, base64 payloads, error mapping) |

All scripts are stdlib-only Python 3 and run locally, reaching the cluster over
one multiplexed SSH connection. Every one accepts `--host` to target a different
SSH alias, and the data-gathering ones accept `--json`.
