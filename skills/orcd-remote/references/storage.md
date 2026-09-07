# ORCD storage: tiers, speed, and where to put job IO

`python3 scripts/orcd_storage.py` lists this user's writable paths; this file
explains the tiers.

## Quotas: `~/orcd/.quota`

ORCD regenerates a per-user report roughly every 30 minutes. It is the only
place the personal scratch and pool limits appear; `df` shows the whole shared
filesystem (270 TB free in a space capped at 1 TB).

```
 Space   | Usage (GB) | Limit (GB) | % Used |  Files | Limit | % Used
---------+------------+------------+--------+--------+-------+--------
 HOME    |       71.0 |      200.0 |  35.48 | 277.2K |  1.0M |  27.72
 SCRATCH |      220.9 |     1024.0 |  21.57 |  73.0K |  1.0M |   7.30
 POOL    |        0.0 |     1024.0 |   0.00 |      8 |  2.1B |   0.00
```

| Space | Quota | Files | Backed up | Purpose |
| --- | --- | --- | --- | --- |
| `~` (HOME) | 200 GB | 1 M | yes, snapshots | code, config, small inputs |
| `~/orcd/scratch` | **1 TB** | **1 M** | no | environments, private staging |
| `~/orcd/pool` | 1 TB | ~2 B | no | larger datasets |

The file column binds on its own: one unpacked image dataset or two conda
environments reach 1 M inodes at a few percent of the byte quota, and it fails
as "disk full". Keep datasets as archives or container images.

## Personal spaces are sharded symlinks

```
~/orcd/scratch  -> /orcd/scratch/orcd/<NNN>/<user>   # flash
~/orcd/pool     -> /orcd/pool/<NNN>/<user>           # capacity
~/orcd/datasets -> /orcd/datasets/001                # shared, read-only
~/orcd/examples -> /orcd/examples
```

`<NNN>` differs per person, so a resolved path is correct only for whoever
resolved it. Write the symlink; resolve at runtime: `SCRATCH=$(readlink -f ~/orcd/scratch)`.

## Group storage: the group name encodes tier and owner

```
orcd_rg_<server>_<owner>      # server prefix = hardware tier; owner = pi_<name>, pg_<project>, ou_<org>
```

| Server prefix | Hardware | Mounted under | Use for |
| --- | --- | --- | --- |
| `fstor*` | flash, NFS over RDMA (`proto=rdma,port=20049`) | `/orcd/scratch/...`, `/orcd/compute/...` | active job IO |
| `hstor*` | spinning disk | `/orcd/data/...`, `/orcd/pool/...` | datasets and results to keep |
| `core*` | archive | `/orcd/archive/...` | cold data |
| `nfs*` | shared home server | `/home/<user>` | code and config only |

Group allocations (hundreds of TB) have group quotas, not the personal ones
above (a group pool may show in `~/orcd/.quota` as `POOL 2`).

## Measured throughput

One sample on an H100 node (`node1702`): 1 GiB sequential `dd` (`O_DIRECT`
where supported) and wall time to create 500 small files. Indicative only; the
small-file column moves with load.

| Tier | Path | Write MB/s | Read MB/s | 500 files (s) |
| --- | --- | --- | --- | --- |
| RAM | `/dev/shm` | 3500 | 6300 | 0.02 |
| node-local disk | `/tmp` | 202 | 1400 | 0.03 |
| bcs flash scratch | `/orcd/scratch/bcs/001` | 1000 | 2800 | 0.13 |
| bcs flash scratch | `/orcd/scratch/bcs/002` | 1000 | 3000 | 0.14 |
| bcs flash project | `/orcd/compute/bcs/001` | 232 | 3300 | 3.19 |
| capacity disk | `/orcd/data/<pi>/002` | 1100 | 1500 | 0.16 |
| shared home | `/home/<user>` | 222 | 1300 | 0.78 |

- `$HOME` is the slowest tier for small files (6x here, 10x in an earlier run)
  and the default working directory: the most common avoidable slow job.
- Flash scratch and capacity disk measured alike; the tiers to avoid writing
  many small files to are `$HOME` and the flash *project* tier (metadata ~25x
  slower under contention; read shared datasets from it, do not write there).
- `/dev/shm` is RAM and counts against `--mem`.

## Node-local scratch varies

`$TMPDIR` (`/tmp`) always exists; `/scratch` is large (3.5 TB seen) but absent
on some nodes, GPU nodes included; Slurm reports `TmpDisk=0` everywhere, so it
cannot be asked; nothing is cleaned beyond the job's `/tmp`.

```bash
for d in /scratch "$TMPDIR" /dev/shm; do [ -d "$d" ] && [ -w "$d" ] && { LOCAL="$d"; break; }; done
WORK="$LOCAL/$SLURM_JOB_ID"; mkdir -p "$WORK"; trap 'rm -rf "$WORK"' EXIT
```

## Backup

`__STORAGE_WITHOUT_BACKUP__` at a tree's root proves no backup (both bcs flash
scratch filesystems carry it); its absence proves nothing, so `orcd_storage.py`
says `unmarked`, never "backed up". Working assumption: **only `$HOME` is backed
up**. Anything irreplaceable needs an explicit archive plan (archive tier or an
external copy).

## The staging pattern

Many-small-file work (unpacking archives, resolving environments, per-step
checkpoints): copy in, work on node-local disk, copy out -- thousands of network
operations become two sequential ones.

```bash
#!/bin/bash
#SBATCH -p ou_bcs_high -t 4:00:00 -c 8 --mem=64G --gres=gpu:h100:1
set -eo pipefail
umask 027                                    # group-readable, nothing for others
ENVS=$(readlink -f ~/orcd/scratch)/envs      # personal flash: environments
RUNS=/orcd/scratch/bcs/<NNN>/$USER/runs      # group flash: runs (orcd_storage.py lists it)
WORK=${TMPDIR:-/tmp}/$SLURM_JOB_ID
mkdir -p "$WORK" "$RUNS"; trap 'rm -rf "$WORK"' EXIT
tar -C "$WORK" -xf "$RUNS/../dataset.tar"                          # stage in
"$ENVS/myproj/bin/python" train.py --data "$WORK" --out "$WORK/out"
rsync -a "$WORK/out/" "$RUNS/$SLURM_JOB_ID/"                       # stage out what is worth keeping
```

Before placing a dataset, ask for file count and access pattern: 300 GB as
three hundred 1 GB shards wants streaming; as a million 300 KB clips it hits
the inode cap first; read-once and every-epoch random access want different
tiers. By workload:

- Many small files (image sets, venvs): stage in, or better keep one archive /
  WebDataset shard and read sequentially.
- Few large files (checkpoints, video, HDF5, Zarr): read and write flash
  scratch directly.
- Group-shared datasets: read from the project tier in place; do not copy per
  user.
- Python environments: never in `$HOME`, never built on a login node. Personal
  `~/orcd/scratch`, built in a `mit_quicktest` job, or a container image.

## Two traps

Bare `df -h` hangs for minutes on a login node when any network mount is
unresponsive: read `/proc/mounts` (never blocks) and size paths under `timeout`:

```bash
awk '$3 ~ /^(nfs|nfs4)$/ {print $2, $1}' /proc/mounts   # inventory
timeout 6 df -h /orcd/scratch/bcs/001                   # sizing
```

`/orcd` is autofs: a tree appears only once touched, so listing a parent is not
an inventory. The maps are in LDAP, which is how `orcd_storage.py` finds
project trees:

```bash
ldapsearch -x -LLL -b "ou=auto.orcd.data,ou=automount,dc=cm,dc=cluster" "(objectClass=automount)" cn automountInformation
```

## Conventions

Group directories are setgid: files inherit the group. Use `rsync -a`; never
strip the group bit. Observed layouts:

```
/orcd/data/<pi>/001/users/<username>/     per-person space in the lab store
/orcd/data/<pi>/002/{datasets,models,projects,cache}/
/orcd/scratch/bcs/<NNN>/<username>/       per-person group flash scratch
```

**Two flash scratches, two jobs.** Personal `~/orcd/scratch` (1 TB, 1 M inodes;
a torch-sized venv is 50-100 k files) holds Python environments, `UV_CACHE_DIR`
and private staging. Group flash `/orcd/scratch/bcs/<NNN>/<user>` holds runs:
checkpoints, outputs, shared intermediates, readable by the lab.

**Group scratch dirs are group-readable and closed to others.** Setgid keeps
files group-owned but leaves the "other" bits alone, so
`python3 scripts/orcd_storage.py --setup` creates the per-user dir on each
group flash tier with `chmod o-rwx` (existing dirs too, mode reported), and
every job script that writes there starts with `umask 027`. `--setup` skips
`~/orcd/scratch` and `~/orcd/pool`: ORCD provisions those; a missing symlink is
a request for orcd-help@mit.edu.

## Moving data

Anything large goes through the transfer partition, not a login node:

```bash
sbatch -p mit_data_transfer -t 12:00:00 -c 8 --mem=32G \
  --wrap='rsync -a --info=progress2 /orcd/scratch/bcs/001/$USER/run/ /orcd/data/<pi>/002/results/'
```

From a laptop, over the skill's multiplexed connection:

```bash
rsync -a -e "ssh -o ControlPath=~/.ssh/cm-%r@%h:%p" ./local/ orcd:/orcd/scratch/bcs/001/$USER/
```

A remote path with `(` or `)` needs quoting for the remote shell:
`orcd:"'/path/with (x)/'"`. Recurring or very large external transfers: Globus
(ask orcd-help@mit.edu for the endpoint).
