# Slurm on ORCD: choosing where to run

Cluster `eofe7`, Slurm 25.05, `select/cons_tres` with `CR_CORE_MEMORY`,
backfill scheduler. Login nodes Rocky 8; most compute nodes advertise `rocky8`.
`python3 scripts/orcd_resources.py --gpus --idle` gives the current personal
answer; this file explains how to read it.

## Access is per person

Each partition declares `AllowGroups`; two people in one lab see different
partitions, so nothing can be hardcoded. `scontrol show partition` lists every
partition regardless of access (`PrivateData=none`), so it is not an
entitlement list. The oracle queues nothing and reports start time and node:

```bash
sbatch --test-only -p <partition> -t 5 -n 1 --mem=1G --wrap=true
# sbatch: Job 18845606 to start at 2026-07-25T09:45:29 using 2 processors on nodes node1702 in partition ou_bcs_high
```

`orcd_submit.py --plan` runs it across every reachable partition for a real
request; the same request can start in a minute in a private partition and
three days later in a shared one.

**Blind spot**: `--test-only` validates scheduling, not QOS TRES ceilings -- it
reports an immediate start for 4x H100 in a partition whose `GrpTRES` caps the
group at 2. `--plan` cross-checks the GPU ceilings (`gres/gpu` in `MaxTRESPU`
and `GrpTRES`; CPU/memory are not checked) and marks such rows `EXCEEDS`.

## Never pass `--qos`

Each partition attaches its own QOS (`QoS=` in `scontrol show partition`),
where the per-user ceilings live. Most associations permit only `normal`, so
naming a partition's QOS fails:

```
$ sbatch --test-only -p pi_satra --qos=pi_satra ...
allocation failure: Invalid qos specification
```

## Ceilings

`sacctmgr show qos`:

- `MaxTRESPU` -- yours alone.
- `GrpTRES` -- one pool for the whole group; a colleague's job can block yours.
- `MaxSubmitPU` -- queued + running. ORCD sets this and leaves `MaxJobsPU`
  unset, so it is the count that bites.

Job-count limits stack; the smallest wins:

| Limit | Where | Scope |
| --- | --- | --- |
| association `MaxSubmit` | `sacctmgr show assoc user=$USER` | one ceiling across **every** partition (often the smallest, and invisible in `scontrol show partition`) |
| QOS `MaxSubmitPU` | the partition's QOS | per partition, queued + running |
| `MaxArraySize` | `scontrol show config` | highest task index in one array |

**Array tasks each count as a submitted job.** `-a 0-999` on a partition with
`MaxSubmitPU` below 1000 is refused with `QOSMaxSubmitJobPerUserLimit`, and
`-a 0-999%50` identically: `%K` caps *concurrent* tasks, not submitted ones.
Measured by bisection, the largest accepted array is **`MaxSubmitPU + 1`** (449
at cap 448, 5 at cap 4). Split sweeps into consecutive arrays, or give each task
more items -- usually better anyway, since array tasks carry scheduling
overhead. `orcd_snapshot.py` prints all three limits and the derived cap.

## Priority

`priority/multifactor`; partition and QOS dominate everything a user controls:

| Factor | Weight |
| --- | --- |
| QOS | 2,000,000 |
| Partition | 600,000 |
| FairShare | 150,000 |
| Age | 20,000 |
| JobSize | 10,000 |

Partitions also carry a `PriorityTier`, considered before computed priority:
a tier-100 private partition starts at once while a tier-25 shared one
backfills. Which partition you pick matters more than how long you wait.

## Reading a partition

`scontrol show partition <name>`: `MaxTime` (walltime above it is refused,
`EnforcePartLimits=ANY`); `PreemptMode=REQUEUE` (killable at any time);
`PriorityTier`; `TRES` (authoritative totals incl. per-model GPUs such as
`gres/gpu:h200=104` -- summing `sinfo` undercounts because it collapses
identical nodes onto one line).

Classes seen from one account (illustrative, not a fixed list):

- `pi_<name>` -- private, highest tier, immediate, small, often bound by `GrpTRES`.
- `ou_<org>_{high,normal,low}` -- `high` short-walltime and tightly capped
  (interactive/debug); `normal` the workhorse; `low` large but preemptable.
- `mit_normal`, `mit_normal_gpu` -- open to all, most congested; the GPU one
  can be days deep.
- `mit_quicktest` -- 15-minute cap, very high tier; smoke tests and short work.
- `mit_preemptable` -- the largest pool by far, lowest tier, `REQUEUE`.
- `mit_data_transfer` -- transfer nodes, long walltime, no GPUs; staging, not compute.

## Preemptable partitions

`mit_preemptable` spans essentially the whole cluster -- an order of magnitude
more nodes and GPUs than any group partition, with models found nowhere else
and a generous per-user ceiling -- at the price of `PreemptMode=REQUEUE`.
`ou_*_low` is preemptable on the same terms. Use it for checkpointed work,
idempotent array tasks and sweeps where a lost task costs a restart; not for a
long unresumable job or anything holding a lock or external session.

```bash
#SBATCH -p mit_preemptable
#SBATCH --requeue
#SBATCH --signal=B:USR1@120       # USR1 to the batch shell 120 s before the kill
trap 'python save_checkpoint.py; exit 1' USR1   # exit NON-ZERO: a clean exit means "finished"
python train.py --resume-if-exists "$CKPT"      # must resume, not restart
```

Complementary pattern: a small guaranteed allocation in a private partition for
what must finish, plus a large preemptable array for the rest; `--plan` prices
both.

## GPUs

`--gres=gpu:h100:2` (this model) vs `--gres=gpu:2` (any model: L4 to H200 on a
mixed partition). Partitions that declare GPUs without a model accept only the
untyped form (`untyped` in `orcd_resources.py`); `--gpus` confirms a model is
requestable, not merely present. GPU nodes are fat (120-256 CPUs, 1-2 TB RAM,
4-8 GPUs): request CPUs and memory in proportion, or the remaining GPUs become
unusable for everyone.

**Topology.** GPUs on one node talk over NVLink/PCIe; across nodes over the
fabric, and cross-node work needs a distributed launcher (torchrun, srun ranks).

```bash
-N 1 --gres=gpu:h100:4     # 4 GPUs on ONE node
-N 2 --gres=gpu:h100:4     # 4 per node, 8 total, fabric-bound
```

`--gres` counts **per node**. Nodes carry 4 or 8 GPUs (`node_shapes` in
`orcd_snapshot.py --json`). One node with 4 free GPUs is scarcer than 4 free
GPUs spread out, so the packed shape can queue longer; `--plan` both.

**sinfo state flags** (a `mixed-` node is not draining):

| Suffix | Meaning |
| --- | --- |
| `-` | PLANNED: free resources already earmarked by backfill |
| `*` | not responding |
| `~` | powered down |
| `#` | powering up |
| `%` | powering down |
| `$` | maintenance reservation |
| `@` | reboot pending |
| `^` | reboot issued |
| `!` | pending power-down |

`orcd_resources.py --idle` skips every flagged node.

## Memory

`DefMemPerCPU=1000`: an unspecified request gets 1 GB per CPU and dies part-way
with an unhelpful error. Always `--mem=64G` (per node) or `--mem-per-cpu=8G`.
`ThreadsPerCore=2` on most nodes, so `-c 1` shows two CPUs; size thread pools
from `$SLURM_CPUS_PER_TASK`, not `nproc`. `MaxRSS` from `sacct` right-sizes
`--mem` for the next run.

## Login nodes are for orchestration, not work

Editing, `git`, scheduler queries, quota checks: fine. Anything that computes,
compiles, resolves an environment, unpacks a dataset or checksums a tree: a job.
Every `ssh orcd '<command>'` runs on the login node, so real work driven
remotely is a one-shot `srun` on the short partition:

```bash
ssh orcd 'srun -p mit_quicktest -t 15 -c 4 --mem=8G <command>'     # <=15 min, near-instant start
ssh orcd 'srun -p ou_bcs_high -t 2:00:00 -c 8 --mem=32G <command>'  # longer
ssh orcd -t 'srun -p mit_quicktest -t 15 -n 1 --mem=8G --pty bash'  # interactive
ssh orcd -t 'srun -p ou_bcs_high -t 2:00:00 -c 8 --mem=32G --gres=gpu:h100:1 --pty bash'
```

Large copies: `mit_data_transfer` ([storage.md](storage.md)).

## Recipes

```bash
sbatch -p mit_quicktest -t 10 -n 1 --mem=4G --wrap='hostname; echo ok'        # smoke test
sbatch -p mit_preemptable -a 0-447%50 -t 2:00:00 -c 4 --mem=16G job.sh        # array <= MaxSubmitPU+1 (448 here)
```

Use absolute `-D`/`-o`: sbatch resolves relative paths against the *submission*
cwd (`$HOME` via this skill), and the directory must exist or the job fails at
start with `Could not open stdout file`. Start job scripts with `set -eo pipefail`
so a failed step fails the job instead of a `COMPLETED` with no output.

```bash
#!/bin/bash
#SBATCH -J train -p ou_bcs_high -t 4:00:00 -c 8 --mem=64G --gres=gpu:h100:1
#SBATCH -D /orcd/scratch/bcs/<NNN>/<user>/runs      # or orcd_submit.py --chdir
#SBATCH -o %x-%j.out                                 # relative to -D
set -eo pipefail
module load cuda/12.9.1
srun python train.py
```

## Software

Lmod (`module avail`). Seen: `cuda/12.9.1`, `13.0.1`, `13.1.0`; `gcc/12.2.0`,
`14.3.0`; `openmpi/4.1.4`, `5.0.8`; `miniforge/25.11.0-0`; `apptainer/1.4.2`.
`apptainer` (alias `singularity`) works on login nodes without a module; no
Docker: `apptainer exec --nv image.sif python train.py`. No system `uv`/`conda`:
`module load miniforge`, or `orcd_uv.py --install` ([setup.md](setup.md)); keep
environments off `$HOME` ([storage.md](storage.md)).

## Inspecting jobs

```bash
squeue -u $USER -o "%.12i %.22j %.16P %.10T %.11M %.11l %.6D %R"
scontrol show job <id>                  # queued or running
sacct -j <id> --format=JobID,State,Elapsed,ReqTRES%40,MaxRSS,ExitCode
sacct -u $USER -S today
```

Pending `%R` reasons: `Priority` (waiting its turn), `Resources` (waiting for
hardware), `QOSGrp*Limit` (a ceiling above is binding -- an array then runs in
waves below its `%K`), `ReqNodeNotAvail` (the partition lacks what was asked).
