# Slurm on ORCD: choosing where to run

Cluster `eofe7`, Slurm 25.05, `select/cons_tres` with `CR_CORE_MEMORY`, backfill
scheduler. Login nodes run Rocky 8; most compute nodes advertise a `rocky8`
feature.

Run `python3 scripts/orcd_resources.py --gpus --idle` for the current, personal
answer. This document explains how to interpret it.

## Access is by Unix group, and it is per-person

Each partition declares `AllowGroups`. A partition is usable when the user is in
one of those groups, so two people in the same lab legitimately see different
answers, and no partition list can be hardcoded.

`scontrol show partition` shows all partitions regardless of access, because
`PrivateData=none` on this cluster. It therefore cannot be used as an
entitlement list. The authoritative check is:

```bash
sbatch --test-only -p <partition> -t 5 -n 1 --mem=1G --wrap=true
```

That queues nothing. Exit 0 means the request is permitted, and the message
reports **when it would start** and **on which node**:

```
sbatch: Job 18845606 to start at 2026-07-25T09:45:29 using 2 processors on nodes node1702 in partition ou_bcs_high
```

`orcd_submit.py --plan` runs this across every reachable partition for a real
request. Use it before any long job -- the same request can start in one minute
in a private partition and three days later in a shared one.

One blind spot, found the hard way: `--test-only` validates *scheduling*, not
QOS TRES ceilings. It happily reports an immediate start for 4x H100 in a
partition whose `GrpTRES` caps the whole group at 2 -- a request that would sit
in the queue forever. `--plan` therefore cross-checks each viable partition's
QOS ceilings (both `MaxTRESPU` and `GrpTRES`) and marks impossible rows
`EXCEEDS`, excluding them from auto-selection. When reading raw `--test-only`
output yourself, apply the same skepticism.

## Never pass `--qos`

Each partition attaches its own QOS automatically (`scontrol show partition`
field `QoS=`). That QOS is where the per-user ceilings live, and it applies
whether or not it is named.

A user's *association* usually permits only `normal`, so naming a partition's
QOS explicitly fails:

```
$ sbatch --test-only -p pi_satra --qos=pi_satra ...
allocation failure: Invalid qos specification
```

Choose the partition and let the QOS follow.

## Two different ceilings, and only one is yours

`sacctmgr show qos` exposes both, and conflating them causes confusing waits:

- **`MaxTRESPU`** -- per user. Your own cap.
- **`GrpTRES`** -- one pool shared by everyone in the group. A colleague's
  running job consumes it, so a request within your own limits can still queue.
- **`MaxSubmitPU`** -- queued plus running jobs. On ORCD these QOS set
  `MaxSubmitPU` and leave `MaxJobsPU` unset, so this is the count that bites.

`orcd_resources.py` prints them in separate columns for this reason.

## Job-count and array ceilings

Three limits stack, and the smallest wins:

| Limit | Where | Scope |
| --- | --- | --- |
| association `MaxSubmit` | `sacctmgr show assoc user=$USER` | one ceiling across **every** partition |
| QOS `MaxSubmitPU` | the partition's own QOS | per partition, queued + running |
| `MaxArraySize` | `scontrol show config` | highest task index in one array |

The association cap is the one people miss, because it is invisible in
`scontrol show partition` and is often *smaller* than the per-partition number.
`orcd_snapshot.py` prints all three together.

**Array tasks each count as a submitted job.** So an array larger than the target
partition's `MaxSubmitPU` is refused outright:

```
$ sbatch --test-only -p mit_preemptable -a 0-999 ...
QOSMaxSubmitJobPerUserLimit
```

**And `%K` throttling does not help.** `-a 0-999%50` is refused identically:
`%K` limits how many tasks run *concurrently*, not how many are submitted.

Measured by bisection, the largest accepted array is **`MaxSubmitPU` + 1** --
449 tasks where the cap is 448, 5 tasks where the cap is 4. So a sweep bigger
than that has to be split into consecutive arrays, or run as fewer tasks that
each loop over more work. The second option is usually better anyway: array
tasks have real scheduling overhead, and a task that processes 20 items amortises
it.

`orcd_snapshot.py` prints the derived per-partition array cap, so it stays
correct if the QOS is retuned.

## Priority

`priority/multifactor`, weighted so that the choice of partition and QOS
dominates everything a user controls at submit time:

| Factor | Weight |
| --- | --- |
| QOS | 2,000,000 |
| Partition | 600,000 |
| FairShare | 150,000 |
| Age | 20,000 |
| JobSize | 10,000 |

Practical reading: **which partition you pick matters far more than how long you
wait.** Partitions also carry a `PriorityTier`; a higher tier is considered
first regardless of the computed priority, which is why a private partition with
tier 100 starts immediately while a shared one at tier 25 backfills.

## Reading a partition

Fields worth checking in `scontrol show partition <name>`:

- `MaxTime` -- a walltime above it is refused outright (`EnforcePartLimits=ANY`).
- `PreemptMode=REQUEUE` -- jobs can be killed and requeued at any time.
- `PriorityTier` -- higher is considered first.
- `TRES` -- authoritative totals, including per-model GPU counts such as
  `gres/gpu:h200=104`. Prefer this over summing `sinfo`, which collapses
  identically configured nodes onto one line and undercounts.

Partitions on this cluster fall into recognisable classes. The names below are
illustrative of one account's view, not a fixed list:

- **Private lab partitions** (`pi_<name>`) -- highest tier, start immediately,
  small, often limited by a shared `GrpTRES`.
- **Group/department partitions** (`ou_<org>_{high,normal,low}`) -- `high` is
  short-walltime and tightly capped, good for interactive and debug work;
  `normal` is the workhorse; `low` is large but `REQUEUE`-preemptable.
- **Shared MIT partitions** (`mit_normal`, `mit_normal_gpu`) -- open to all,
  and therefore the most congested. `mit_normal_gpu` can be days deep.
- **`mit_quicktest`** -- 15-minute cap, very high tier. Ideal for smoke tests.
- **`mit_preemptable`** -- by far the largest pool of nodes and GPUs, lowest
  tier, `REQUEUE`. Excellent for checkpointed or idempotent work. See below.
- **`mit_data_transfer`** -- dedicated transfer nodes, long walltime, no GPUs.
  Use for staging large datasets, not for computation.

## Preemptable partitions as extra capacity

`mit_preemptable` spans essentially the whole cluster -- an order of magnitude
more nodes and GPUs than any single group partition, including GPU models that
appear nowhere else. Its own per-user ceiling is correspondingly generous. The
price is `PreemptMode=REQUEUE`: a higher-priority job can evict yours at any
moment, and Slurm puts it back in the queue.

So it is best understood not as a worse partition but as **a second, much larger
pool that is free to use if the work can be interrupted**. Group partitions like
`ou_bcs_low` are preemptable on the same terms.

Work that suits it: anything checkpointed, any idempotent array task, any
embarrassingly parallel sweep where losing one task costs a restart rather than
the run. Work that does not: a long single job with no checkpointing, or anything
holding a lock or an external session.

Making a job survive requeue:

```bash
#SBATCH -p mit_preemptable
#SBATCH --requeue
#SBATCH --signal=B:USR1@120       # USR1 to the batch shell, 120s before the kill

trap 'python save_checkpoint.py; exit 1' USR1   # exit non-zero so it requeues

python train.py --resume-if-exists "$CKPT"
```

Two details that decide whether this actually works:

- The handler must **exit non-zero**. A clean exit tells Slurm the job finished.
- `train.py` must resume from the checkpoint rather than restarting, so the same
  script is correct on both the first run and the fifth.

The complementary strategy is to run the same work in two partitions at once: a
small guaranteed allocation in a private partition for the part that must finish,
and a large preemptable array for the rest. `orcd_submit.py --plan` shows what
each would cost in start time.

## Requesting GPUs

Ask by model whenever the model matters:

```bash
--gres=gpu:h100:2        # this model only
--gres=gpu:2             # any model in the partition
```

Untyped requests may land on anything the partition has, which on a mixed
partition ranges from an L4 to an H200 -- a large difference in both memory and
throughput. A few partitions declare GPUs with no model at all and accept only
the untyped form; `orcd_resources.py` labels those `untyped`.

Verify a model is really requestable rather than merely present:

```bash
python3 scripts/orcd_resources.py --gpus
```

GPU nodes on this cluster are generally fat: commonly 120-256 CPUs and 1-2 TB
RAM per node with 4-8 GPUs. Request CPUs and memory in proportion to the GPUs
taken, or the node's remaining GPUs become unusable by anyone else.

### Multi-GPU: node topology matters

GPUs within one node communicate over NVLink/PCIe; GPUs on different nodes
communicate over the network fabric -- a large gap in both bandwidth and
latency, and cross-node work additionally needs a distributed launcher
(torchrun, srun-launched ranks) rather than plain data-parallel on one machine.

```bash
-N 1 --gres=gpu:h100:4     # 4 GPUs on ONE node: fastest interconnect
-N 2 --gres=gpu:h100:4     # 4 per node, 8 total: cross-node, fabric-bound
```

`--gres` counts GPUs **per node**, so the same `--gres` with a different `-N`
is a different total. Nodes here carry 4 or 8 GPUs (see the node shapes in
`orcd_resources.py`), which bounds what `-N 1` can ever provide.

Availability runs the other way: one node with 4 simultaneously-free GPUs is
much scarcer than 4 free GPUs scattered across a partition, so the tightly
packed shape can queue longer than the spread one. Plan both shapes and compare
start times before committing to either.

### Reading sinfo node states

`sinfo` appends single-character flags to states, and misreading them leads to
wrong conclusions about capacity (a `mixed-` node is not draining):

| Suffix | Meaning |
| --- | --- |
| `-` | PLANNED -- free resources already earmarked by the backfill scheduler |
| `*` | not responding |
| `~` | powered down |
| `#` | powering up |
| `%` | powering down |
| `$` | maintenance reservation |
| `@` | reboot pending |

A planned (`-`) node's idle GPUs are spoken for: counting them as available
overstates capacity, which is why `orcd_resources.py --idle` skips flagged
nodes entirely.

## Always set memory

`DefMemPerCPU=1000`, so an unspecified request gets 1 GB per CPU. A 4-CPU job
silently receives 4 GB and dies part-way through anything substantial, usually
with an unhelpful error.

```bash
--mem=64G            # per node
--mem-per-cpu=8G     # or per CPU
```

`ThreadsPerCore=2` on most nodes, so `-c 1` yields two visible CPUs. Inside a
job, size thread pools from `$SLURM_CPUS_PER_TASK` rather than `nproc`.

## Login nodes are for orchestration, not work

Login nodes are shared by everyone; anything that computes, compiles, resolves
a Python environment, unpacks a dataset, or checksums a tree belongs in a job.
Editing, `git`, scheduler queries, quota checks and short probes are fine.

This matters doubly for this skill: every `ssh orcd '<command>'` runs on the
login node, so the default for real work driven remotely is a one-shot `srun`
on the short partition, not a bare command:

```bash
ssh orcd 'srun -p mit_quicktest -t 15 -c 4 --mem=8G <command>'     # ≤15 min
ssh orcd 'srun -p ou_bcs_high -t 2:00:00 -c 8 --mem=32G <command>'  # longer
ssh orcd -t 'srun -p mit_quicktest -t 15 -n 1 --mem=8G --pty bash'  # interactive
```

`mit_quicktest` (15-minute cap, very high tier) starts near-instantly; when the
work will not fit, escalate to `ou_*_high` or `mit_normal` with a short `-t`.
Large copies go to `mit_data_transfer` ([storage.md](storage.md)).

## Submission recipes

Smoke test, near-instant:

```bash
sbatch -p mit_quicktest -t 10 -n 1 --mem=4G --wrap='hostname; echo ok'
```

Single-GPU training with explicit resources. Use an absolute `-o`/`-D`: sbatch
resolves relative paths against the *submission* cwd -- `$HOME` when submitting
through this skill -- and the directory must already exist or the job fails at
start with "Could not open stdout file":

```bash
#!/bin/bash
#SBATCH -J train
#SBATCH -p ou_bcs_high
#SBATCH -t 4:00:00
#SBATCH -c 8
#SBATCH --mem=64G
#SBATCH --gres=gpu:h100:1
#SBATCH -D /orcd/scratch/bcs/<NNN>/<user>/runs      # or orcd_submit.py --chdir
#SBATCH -o %x-%j.out                                 # relative to -D

module load cuda/12.9.1
srun python train.py
```

Array job. Each task counts as a submitted job, so the array must fit the
partition's `MaxSubmitPU + 1` (see above; `orcd_snapshot.py` prints it) --
`%K` throttles *concurrency* and does not raise that cap:

```bash
sbatch -p mit_preemptable -a 0-447%50 -t 2:00:00 -c 4 --mem=16G job.sh   # cap 448 here
```

Preemptable work must be able to resume. Trap the signal and checkpoint:

```bash
#SBATCH --requeue
#SBATCH --signal=B:USR1@120     # USR1 two minutes before the kill

trap 'python save_checkpoint.py; exit 1' USR1
```

Interactive GPU shell (see the login-node section for the short-partition form):

```bash
ssh orcd -t 'srun -p ou_bcs_high -t 2:00:00 -c 8 --mem=32G --gres=gpu:h100:1 --pty bash'
```

## Software

Lmod, with `module avail`. Available at the time of writing: `cuda/12.9.1`,
`13.0.1`, `13.1.0`; `gcc/12.2.0`, `14.3.0`; `openmpi/4.1.4`, `5.0.8`;
`miniforge/25.11.0-0`; `apptainer/1.4.2`. Check rather than assume.

`apptainer` (aliased `singularity`) is installed on login nodes with no module
needed. There is no Docker. Build or fetch a `.sif` and run:

```bash
apptainer exec --nv /path/to/image.sif python train.py    # --nv exposes GPUs
```

`uv` and `conda` are not installed system-wide. Either `module load miniforge`,
or install `uv` into `$HOME` -- `python3 scripts/orcd_uv.py --install` checks
for an existing `~/.local/bin/uv`, installs or upgrades it, and never touches
shell profiles (PATH edits go through `--add-to-path`, which requires the
user's explicit approval). Keep environments themselves off `$HOME`, since
resolving one is exactly the many-small-file workload that tier is worst at.
See [storage.md](storage.md) and [setup.md](setup.md).

## Inspecting jobs

```bash
squeue -u $USER -o "%.12i %.22j %.16P %.10T %.11M %.11l %.6D %R"
scontrol show job <id>                  # while queued or running
sacct -j <id> --format=JobID,State,Elapsed,ReqTRES%40,MaxRSS,ExitCode
sacct -u $USER -S today                 # after the fact
```

The `%R` column on a pending job gives the reason: `Priority` (waiting its
turn), `Resources` (waiting for hardware), `QOSGrpGpuLimit` or similar (a
ceiling from the section above is binding), `ReqNodeNotAvail` (asked for
something the partition does not have).

`MaxRSS` from `sacct` is the way to right-size `--mem` for the next run.
