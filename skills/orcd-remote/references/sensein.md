# Sensein group conventions on ORCD

This file is the group-specific layer on top of the generic ORCD skill: which
data trees belong to us, who grants access to them, and where things go so that
twenty people do not each keep a private copy of the same 15 GB model.

The machine-readable version is [assets/sensein.json](../assets/sensein.json);
`orcd_storage.py` loads it automatically and reports which project trees you can
already reach and which you would need access granted for. Other groups can
point `--group-config` (or `ORCD_GROUP_CONFIG`) at their own file.

## Use the symlinks, not resolved paths

Anything written down for the group -- job scripts, docs, shared code -- must
use the path forms that are valid for *every* member:

```bash
~/orcd/scratch                    # your 1 TB personal flash scratch
~/orcd/pool                       # your 1 TB personal capacity space
SCRATCH=$(readlink -f ~/orcd/scratch)   # resolve at runtime when a real path is needed
```

Never commit a resolved form like `/orcd/scratch/orcd/013/<user>`: the shard
number differs per person, so the path is only correct for whoever wrote it.
Group trees (`/orcd/data/satra/...`) are the same for everyone and safe to write
literally.

## Our data trees

| Tree | Project | Purpose |
| --- | --- | --- |
| `/orcd/data/satra/001` | lab-wide | per-user dirs under `users/<username>` |
| `/orcd/data/satra/002` | lab-wide | shared `models/`, `huggingface/`, `datasets/`, `projects/` |
| `/orcd/data/dandi/001`, `002` | DANDI | archive mirrors, `dandi-compute/`, `environments/` |
| `/orcd/data/linc/001` | LINC | per-member and shared imaging trees |

`abcd`, `sails`, and `kiva` have no dedicated `/orcd/data` tree; their groups
grant subtrees inside the shared capacity and bcs flash filesystems.

All trees are setgid (`drwxrws---`), so files created inside inherit the project
group and stay readable by collaborators -- use `rsync -a` and do not strip the
group bit.

## Consolidated storage: models, projects, users

The point of these conventions is deduplication: models and datasets are large,
quota is shared, and the capacity tier is already over 90% full.

**Models -- one shared cache, not one per person.** The lab Hugging Face cache
lives at `/orcd/data/satra/002/huggingface` (standard `HF_HOME` layout). Point
tools at it instead of letting them default to `~/.cache` -- which would also
eat your 200 GB home quota and its 1 M inode cap:

```bash
export HF_HOME=/orcd/data/satra/002/huggingface
```

**A shared cache can hold an incomplete snapshot, and nothing says so until the
load fails.** A partial download leaves the model directory present with its
`refs/` and most blobs in place, so every check short of loading it passes.
Observed: a `google/hear` snapshot missing `event_detector/.../saved_model.pb`
in one scratch cache while a complete copy sat in another; a cluster array
pointed at the first died on every recording. Before pointing a long array at a
cache you did not populate, load one model from it in a two-minute interactive
job. Failing in a `mit_quicktest` slot costs minutes; failing in task 7 of 10
costs the whole array.


Non-HF model weights go under `/orcd/data/satra/002/models/<name>`. Before
downloading anything large, check whether it is already there.

**Models are pinned by commit hash -- never a floating `main`.** The same
branch name serves different weights next month, and then nobody can say which
weights produced a result. Anything shared -- job scripts, configs, code, and
the cache itself -- must reference a model by its commit hash. Resolve the
hash first, then fetch and load with `revision=`:

```bash
# resolve without downloading (public repos, no token needed)
SHA=$(git ls-remote https://huggingface.co/<org>/<name> refs/heads/main | cut -f1)
```

```python
from huggingface_hub import HfApi, snapshot_download
sha = HfApi().model_info("org/name").sha        # or the git ls-remote value
snapshot_download("org/name", revision=sha)
model = AutoModel.from_pretrained("org/name", revision=sha)
```

Record the hash next to whatever the model produced (config, results file,
logs), so the run can be reproduced against exactly those weights.

**A fetch made via `main` (or any branch/tag -- tags move on the Hub too) is
redone with the committish.** A branch-name fetch leaves a `refs/main` file in
the cache, and that entry is how someone later loads "whatever `main` meant at
the time" while believing it is current. To fix one:

```bash
cd $HF_HOME/hub/models--<org>--<name>
cat refs/main                        # the commit you actually got
```

Re-run the fetch with `revision=<that sha>` to confirm the snapshot is
complete under the hash (cheap -- the blobs are already there), switch the
code or config that said `main` to the hash, and delete the `refs/main` file
so the floating name can no longer be resolved from the shared cache. To
audit the whole cache for branch/tag fetches that still need re-pinning:

```bash
find "$HF_HOME/hub" -path '*/refs/*' -type f
```

**Tool caches: `/orcd/data/satra/002/cache/<tool>`.** Same consolidation idea
for tools that keep their own download caches. senselab environments point
their cache at the group-writable:

```
/orcd/data/satra/002/cache/senselab
```

Create it once if missing -- the tree's setgid bit makes new files inherit the
project group, but group *write* needs the mode set explicitly:

```bash
mkdir -p /orcd/data/satra/002/cache/senselab
chmod g+ws /orcd/data/satra/002/cache/senselab
```

Models senselab pulls through Hugging Face still follow the rules above:
shared `HF_HOME`, pinned by commit hash.

**Projects.** Shared project work lives in `/orcd/data/satra/002/projects/<project>`,
or in the project's own tree (`/orcd/data/dandi/...`, `/orcd/data/linc/...`)
when one exists.

**Users.** Personal lab space is `/orcd/data/satra/001/users/<username>` --
create yours on first use. This is for work worth keeping; active job IO still
belongs on flash scratch (see [storage.md](storage.md)).

**Runs and environments are split across the two flash scratches.** Python
environments and `UV_CACHE_DIR` go on your personal `~/orcd/scratch` (private,
1 M inodes -- enough for a few envs); runs go on the group flash scratch under
`/orcd/scratch/bcs/<NNN>/<username>`. Group scratch directories are
**group-readable and closed to everyone outside the group**: `orcd_storage.py
--setup` creates yours with `chmod o-rwx`, and job scripts that write there
start with `umask 027`. Build environments inside a `mit_quicktest` job, never
on a login node.

**Datasets.** Shared datasets go under `/orcd/data/satra/002/datasets`, packed
as archives or tar/WebDataset shards rather than loose files.

## Getting access

Access is controlled through WebMoira group membership, managed by the sensein
admin team -- membership changes are an admin action, not something a script or
orcd-help can do for you. The `_mgrs` lists hold each project's managers.

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

The WebMoira `orcd_ug_(pg|pi)_<owner>_<role>` groups feed the on-cluster
`orcd_rg_<server>_(pg|pi)_<owner>` Unix groups that actually gate the mounts, so
`id -Gn` on a login node is the ground truth for what has propagated.
`orcd_storage.py` prints exactly this comparison: which configured projects you
can reach, and the WebMoira list to ask about for the ones you cannot.
