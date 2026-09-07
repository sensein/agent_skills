# Sensein group conventions on ORCD

Group-specific layer over the generic skill: which data trees are ours, who
grants access, and where things go so twenty people do not each hold a copy of
the same 15 GB model. Machine-readable twin: [assets/sensein.json](../assets/sensein.json),
loaded by `orcd_storage.py` (other groups: `--group-config` / `ORCD_GROUP_CONFIG`).

## Use the symlinks, not resolved paths

Anything written for the group uses forms valid for every member:

```bash
~/orcd/scratch                          # 1 TB personal flash
~/orcd/pool                             # 1 TB personal capacity
SCRATCH=$(readlink -f ~/orcd/scratch)   # resolve at runtime when a real path is needed
```

Never commit `/orcd/scratch/orcd/013/<user>`: the shard differs per person.
Group trees (`/orcd/data/satra/...`) are identical for everyone and safe to
write literally.

## Our data trees

| Tree | Project | Purpose |
| --- | --- | --- |
| `/orcd/data/satra/001` | lab-wide | per-user dirs under `users/<username>` |
| `/orcd/data/satra/002` | lab-wide | shared `models/`, `huggingface/`, `datasets/`, `projects/`, `cache/` |
| `/orcd/data/dandi/001`, `002` | DANDI | archive mirrors, `dandi-compute/`, `environments/` |
| `/orcd/data/linc/001` | LINC | per-member and shared imaging trees |

`abcd`, `sails`, `kiva` have no `/orcd/data` tree; their groups grant subtrees
inside the shared capacity and bcs flash filesystems.

All trees are setgid (`drwxrws---`): files inherit the project group. Use
`rsync -a`; never strip the group bit.

## Consolidated storage

Capacity is shared and over 90% full; deduplicate.

**Hugging Face cache**: `/orcd/data/satra/002/huggingface` (standard `HF_HOME`
layout). Never let tools default to `~/.cache` (200 GB home quota, 1 M inodes).

```bash
export HF_HOME=/orcd/data/satra/002/huggingface
```

**A shared cache can hold an incomplete snapshot**: `refs/` and most blobs
present, one weight file missing, every check short of loading passes. Before a
long array uses a cache you did not populate, load one model from it in a
`mit_quicktest` job.

**Pin models by commit hash, never `main`.** Resolve, then fetch and load with
`revision=`; record the hash next to the results.

```bash
SHA=$(git ls-remote https://huggingface.co/<org>/<name> refs/heads/main | cut -f1)   # no token needed for public repos
```

```python
from huggingface_hub import HfApi, snapshot_download
sha = HfApi().model_info("org/name").sha
snapshot_download("org/name", revision=sha)
model = AutoModel.from_pretrained("org/name", revision=sha)
```

**A fetch made via `main` (or any branch/tag) is redone with the committish.**
It leaves `refs/main` in the cache, which later resolves to whatever `main`
meant then. `cat $HF_HOME/hub/models--<org>--<name>/refs/main` gives the commit
actually received; re-fetch with `revision=<sha>` (blobs dedupe), switch the
caller to the sha, delete the `refs/main` file. Audit for leftovers:

```bash
find "$HF_HOME/hub" -path '*/refs/*' -type f
```

**Non-HF weights**: `/orcd/data/satra/002/models/<name>`. Check before
downloading anything large.

**Tool caches**: `/orcd/data/satra/002/cache/<tool>`. senselab uses the
group-writable `/orcd/data/satra/002/cache/senselab`; create once with
`mkdir -p` + `chmod g+ws` (setgid inherits the group, not group-write). Models
senselab pulls via HF follow the rules above.

**Projects**: `/orcd/data/satra/002/projects/<project>`, or the project's own
tree (`/orcd/data/dandi/...`, `/orcd/data/linc/...`).

**Users**: `/orcd/data/satra/001/users/<username>`, created on first use, for
work worth keeping. Active job IO belongs on flash scratch.

**Runs vs environments**: Python environments and `UV_CACHE_DIR` on personal
`~/orcd/scratch` (1 M inodes: a few envs); runs on group flash scratch
`/orcd/scratch/bcs/<NNN>/<username>`, group-readable and closed to others
(`orcd_storage.py --setup` applies `chmod o-rwx`; job scripts `umask 027`).
Build environments in a `mit_quicktest` job, never on a login node.

**Datasets**: `/orcd/data/satra/002/datasets`, as archives or tar/WebDataset
shards, not loose files.

## Getting access

WebMoira group membership, managed by the sensein admin team (not orcd-help).
`_mgrs` lists hold each project's managers.

| WebMoira group | Grants |
| --- | --- |
| [orcd_ug_pi_satra_all](https://groups.mit.edu/webmoira/list/orcd_ug_pi_satra_all) | lab-wide `/orcd/data/satra` trees |
| [orcd_ug_pg_dandi_all](https://groups.mit.edu/webmoira/list/orcd_ug_pg_dandi_all) | `/orcd/data/dandi` trees |
| [orcd_ug_pg_dandi_mgrs](https://groups.mit.edu/webmoira/list/orcd_ug_pg_dandi_mgrs) | DANDI managers |
| [orcd_ug_pg_linc_all](https://groups.mit.edu/webmoira/list/orcd_ug_pg_linc_all) | `/orcd/data/linc` tree |
| [orcd_ug_pg_linc_mgrs](https://groups.mit.edu/webmoira/list/orcd_ug_pg_linc_mgrs) | LINC managers |
| [orcd_ug_pg_abcd_all](https://groups.mit.edu/webmoira/list/orcd_ug_pg_abcd_all) | ABCD subtrees (capacity + fstor003 flash) |
| [orcd_ug_pg_sails_all](https://groups.mit.edu/webmoira/list/orcd_ug_pg_sails_all) | SAILS subtrees (capacity + fstor002 flash) |
| [orcd_ug_pg_kiva_all](https://groups.mit.edu/webmoira/list/orcd_ug_pg_kiva_all) | KIVA subtrees (capacity) |

WebMoira `orcd_ug_(pg|pi)_<owner>_<role>` feeds the on-cluster
`orcd_rg_<server>_(pg|pi)_<owner>` Unix groups that gate the mounts; `id -Gn`
on a login node is the ground truth for what has propagated. `orcd_storage.py`
prints which projects you can reach and the WebMoira list to ask about for the
rest.
