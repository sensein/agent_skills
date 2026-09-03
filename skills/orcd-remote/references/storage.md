# ORCD storage: tiers, speed, and where to put job IO

Run `python3 scripts/orcd_storage.py` for the current user's writable paths.
This document explains the tiers and how to use them well.

## Start with your own quota report

ORCD writes a per-user quota report to `~/orcd/.quota`, regenerated roughly every 30 minutes (observed).
It is the authoritative source, and the only place the per-user scratch and pool
limits appear at all -- `df` reports the size of the whole shared filesystem, not
your quota, so it will happily tell you there are 270 TB free in a space you can
only put 1 TB into.

```
                               QUOTA REPORT
 Space   | Usage (GB) | Limit (GB) | % Used |  Files | Limit | % Used
---------+------------+------------+--------+--------+-------+--------
 HOME    |       71.0 |      200.0 |  35.48 | 277.2K |  1.0M |  27.72
 SCRATCH |      220.9 |     1024.0 |  21.57 |  73.0K |  1.0M |   7.30
 POOL    |        0.0 |     1024.0 |   0.00 |      8 |  2.1B |   0.00
```

Every user gets, inside their home directory:

| Space | Quota | Files | Backed up | Purpose |
| --- | --- | --- | --- | --- |
| `~` (HOME) | 200 GB | 1 M | yes, snapshots | code, config, small inputs |
| `~/orcd/scratch` | **1 TB** | **1 M** | no | active job IO |
| `~/orcd/pool` | 1 TB | ~2 B | no | larger datasets |

**The file-count column binds independently of the space column.** A 1 M inode
limit is reached by one unpacked image dataset or a couple of conda
environments, at a few percent of the gigabyte quota. It fails as a disk-full
error, which sends people looking at the wrong number entirely. This is the
strongest practical argument for the staging pattern below: keep datasets as
archives or container images, not as loose files.

## Your personal spaces are symlinks, and the target is not guessable

`~/orcd/` holds root-managed symlinks to this user's own storage:

```
~/orcd/scratch  -> /orcd/scratch/orcd/<NNN>/<user>   # flash, 1 TB quota
~/orcd/pool     -> /orcd/pool/<NNN>/<user>           # capacity disk, 1 TB
~/orcd/datasets -> /orcd/datasets/001                # shared, read-only
~/orcd/examples -> /orcd/examples
```

The per-user tiers are **sharded**, and `<NNN>` differs from person to person,
so a resolved path is only correct for whoever resolved it. Never write one
down for the group; use the symlink, and resolve it at runtime when a real path
is needed:

```bash
SCRATCH=$(readlink -f ~/orcd/scratch)
```

`orcd_storage.py` reports the resolved targets for exactly this reason.

## Entitlement and tier are both encoded in group names

Group membership grants storage, and the group name says which hardware:

```
orcd_rg_<server>_<owner>
        ^^^^^^^  ^^^^^^^
        tier     PI, project, or org unit
```

| Server prefix | Hardware | Mounted under | Use for |
| --- | --- | --- | --- |
| `fstor*` | flash, NFS over RDMA | `/orcd/scratch/...`, `/orcd/compute/...` | active job IO |
| `hstor*` | spinning disk | `/orcd/data/...`, `/orcd/pool/...` | datasets and results to keep |
| `core*` | archive | `/orcd/archive/...` | cold data |
| `nfs*` | shared home server | `/home/<user>` | code and config only |

Group storage sits alongside the per-user spaces above and is usually much
larger; a lab or project allocation of several hundred TB is normal. It is
governed by group quotas rather than the personal ones in `~/orcd/.quota`
(though a group pool may appear there as `POOL 2`).

The flash tier is exported over **NFS over RDMA** (`proto=rdma,port=20049` in
`/proc/mounts`), which is why it outperforms ordinary NFS by a wide margin.

Owner tokens distinguish scope: `pi_<name>` is a lab's storage, `pg_<name>` a
project's, `ou_<org>` an organisational unit's (for example a department-wide
`bcs` allocation).

## Measured throughput

One run on an H100 node (`node1702`), 1 GiB sequential `dd` with `O_DIRECT`
where supported, plus the wall time to create 500 small files. **Indicative
only** -- this was a single sample on a busy shared cluster, and the small-file
column in particular moves with load. Re-measure before optimising against it.

| Tier | Path | Write MB/s | Read MB/s | 500 files (s) |
| --- | --- | --- | --- | --- |
| RAM | `/dev/shm` | 3500 | 6300 | 0.02 |
| node-local disk | `/tmp` | 202 | 1400 | 0.03 |
| **bcs flash scratch** | `/orcd/scratch/bcs/001` | **1000** | **2800** | **0.13** |
| **bcs flash scratch** | `/orcd/scratch/bcs/002` | **1000** | **3000** | **0.14** |
| bcs flash project | `/orcd/compute/bcs/001` | 232 | 3300 | 3.19 |
| capacity disk | `/orcd/data/<pi>/002` | 1100 | 1500 | 0.16 |
| shared home | `/home/<user>` | 222 | 1300 | 0.78 |

What actually follows from this:

- **`$HOME` is the worst tier and the default working directory.** Its
  small-file handling measured 6x slower than flash scratch here, and 10x in an
  earlier run the same morning -- it moves with load, and always in the same
  direction. This is the most common avoidable cause of a slow job.
- **Flash scratch and capacity disk measured alike in this sample** -- both
  ~1 GB/s write, 1.5-3 GB/s read, ~0.15 s for 500 files. The write-here tiers
  to avoid are `$HOME` and the flash *project* tier, not capacity as such;
  capacity is simply the larger, longer-lived tier for results.
- **The flash project tier is not interchangeable with flash scratch.** Reads
  are excellent, but metadata was ~25x slower than scratch in this sample -- it
  holds large shared trees under contention. Read datasets from it; do not write
  thousands of small files to it.
- **`/dev/shm` is RAM.** It counts against the job's `--mem`. Requesting 64 GB
  and writing 40 GB there will get the job killed.

## Node-local scratch is not uniform

Compute nodes vary, so probe rather than assume:

- `$TMPDIR` is set to `/tmp` and always exists.
- `/scratch` is large where present (3.5 TB observed) but **absent on some
  nodes**, including some GPU nodes.
- Slurm reports `TmpDisk=0` on every node, so it is not tracking local disk and
  cannot be asked. There is also no cleanup guarantee beyond the job's own
  `/tmp`.

```bash
for d in /scratch "$TMPDIR" /dev/shm; do
  [ -d "$d" ] && [ -w "$d" ] && { LOCAL="$d"; break; }
done
WORK="$LOCAL/$SLURM_JOB_ID"; mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT
```

## No backup, and how to tell

ORCD places a `__STORAGE_WITHOUT_BACKUP__` sentinel at the root of unprotected
trees. Both bcs flash *scratch* filesystems carry it.

Its presence is proof there is no backup. Its **absence proves nothing** -- it
may simply be unmarked. `orcd_storage.py` reports `unmarked` rather than
claiming a tree is backed up. Confirm with ORCD before trusting anything here to
be recoverable.

The working assumption for the group: **only `$HOME` is backed up** (snapshots).
Scratch, pool, capacity, and group trees should all be treated as unprotected --
the capacity tier is larger and longer-lived, not safer. Anything irreplaceable
needs an explicit archive plan (the archive tier, or an external copy), not an
assumption about the tier it happens to sit on.

## The staging pattern

For jobs that touch many small files -- unpacking archives, resolving Python
environments, writing per-step checkpoints -- copy in, work locally, copy out.
This turns thousands of small network operations into two large sequential ones.

```bash
#!/bin/bash
#SBATCH -p ou_bcs_high -t 4:00:00 -c 8 --mem=64G --gres=gpu:h100:1
umask 027                                    # group-readable, nothing for others

ENVS=$(readlink -f ~/orcd/scratch)/envs      # personal flash: environments
RUNS=/orcd/scratch/bcs/<NNN>/$USER/runs      # group flash: runs (orcd_storage.py lists it)
WORK=${TMPDIR:-/tmp}/$SLURM_JOB_ID
mkdir -p "$WORK" "$RUNS"
trap 'rm -rf "$WORK"' EXIT                   # node-local is not auto-cleaned

# Stage in: one sequential read, then local access
tar -C "$WORK" -xf "$RUNS/../dataset.tar"

"$ENVS/myproj/bin/python" train.py --data "$WORK" --out "$WORK/out"

# Stage out only what is worth keeping
rsync -a "$WORK/out/" "$RUNS/$SLURM_JOB_ID/"
```

What to ask before placing a dataset -- size alone decides nothing. 300 GB as
three hundred 1 GB shards wants streaming throughput; 300 GB as a million
300 KB clips collides with the 1 M inode cap long before the byte quota; a
read-once corpus and an every-epoch random-access set want different tiers
entirely. When the file count and access pattern are unknown, ask the person
(or branch the answer explicitly) rather than building one recommendation on
silent assumptions.

Guidance by workload:

- **Many small files** (image datasets, conda/venv trees): stage in. Better
  still, keep them as a single archive or a webdataset/tar shard and read
  sequentially.
- **Few large files** (checkpoints, video, HDF5, Zarr with large chunks): read
  and write flash scratch directly. Staging adds nothing.
- **Datasets shared across the group**: read from the project tier in place.
  Copying a large shared dataset per user wastes both space and cache.
- **Python environments**: never resolve one in `$HOME`, and never on the login
  node. Put them on your personal `~/orcd/scratch` (see below), built inside a
  `mit_quicktest` job, or ship a container image instead -- one file rather
  than tens of thousands.

## Two failure modes to code around

**`df -h` with no argument can hang for minutes** on a login node whenever any
network mount is unresponsive, and it takes the whole script with it. Read
`/proc/mounts`, which never blocks, and size individual paths under a timeout:

```bash
awk '$3 ~ /^(nfs|nfs4)$/ {print $2, $1}' /proc/mounts   # safe inventory
timeout 6 df -h /orcd/scratch/bcs/001                   # safe sizing
```

**`/orcd` is autofs.** A path materialises only when something touches it, so
listing a parent directory is not an inventory -- a tree that is genuinely
accessible may simply not appear yet. The mount maps live in LDAP and can be
enumerated directly, which is how `orcd_storage.py` finds project trees:

```bash
ldapsearch -x -LLL -b "ou=auto.orcd.data,ou=automount,dc=cm,dc=cluster" \
  "(objectClass=automount)" cn automountInformation
```

## Conventions worth following

Group directories are **setgid**, so files created inside inherit the group and
stay readable by collaborators. Preserve that: use `rsync -a`, and avoid
`chmod`-ing the group bit away.

Observed layouts, useful as defaults when creating new areas:

```
/orcd/data/<pi>/001/users/<username>/     per-person space in the lab store
/orcd/data/<pi>/002/{datasets,models,projects}/
/orcd/scratch/bcs/<NNN>/<username>/       per-person flash scratch
```

### Two flash scratches, two jobs

| Tier | Path | Put here |
| --- | --- | --- |
| personal flash | `~/orcd/scratch` (1 TB, **1 M inodes**) | Python environments, `UV_CACHE_DIR`, private staging |
| group flash | `/orcd/scratch/bcs/<NNN>/<user>` | runs: checkpoints, outputs, shared intermediates |

Environments are the inode-hungry thing (a torch-sized venv is 50-100 k files),
so the personal tier holds a handful of them and nobody else needs them there.
Runs go on the group tier, where the allocation is larger and results are
readable by the lab.

**Group scratch dirs are group-readable and closed to others.** The tree's
setgid bit keeps files group-owned but does not set the "other" bits, so:

```bash
python3 scripts/orcd_storage.py --setup     # mkdir + chmod o-rwx on each group flash tier
umask 027                                   # in every job script that writes there
```

`--setup` applies `o-rwx` to existing per-user dirs as well as new ones and
reports each mode. It skips the personal `~/orcd/scratch` and `~/orcd/pool`:
ORCD provisions those, and a missing symlink is a request for orcd-help@mit.edu.

## Moving data in and out

Use the dedicated transfer partition for anything large, not a login node:

```bash
sbatch -p mit_data_transfer -t 12:00:00 -c 8 --mem=32G \
  --wrap='rsync -a --info=progress2 /orcd/scratch/bcs/001/$USER/run/ /orcd/data/<pi>/002/results/'
```

From a laptop, `scp`/`rsync` over the same multiplexed connection the skill
already maintains:

```bash
rsync -a -e "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p" ./local/ orcd:/orcd/scratch/bcs/001/$USER/
```

For very large or recurring external transfers, ORCD supports Globus; ask
orcd-help@mit.edu for the endpoint name.
