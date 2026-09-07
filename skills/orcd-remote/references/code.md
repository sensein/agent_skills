# Getting code onto the cluster

Login nodes have no GitHub credentials, no agent forwarding, and no accepted
GitHub host key, so `git fetch` against GitHub fails there by default. Two
working paths.

## Check first

`orcd_doctor.py` reports `git over ssh (cluster)`. By hand:

```bash
ssh orcd 'ssh -o BatchMode=yes -T git@github.com'   # want: Hi <user>! You've successfully authenticated
```

`BatchMode=yes` is correct here: the cluster is talking to GitHub (publickey
only). It is wrong only for ORCD's own sshd, where it breaks Duo.

## Path 1: a deploy key on the cluster

Prefer a per-repository, read-only **deploy key** over an account key: the
private key sits passphrase-less in a shared home directory, and a deploy key
exposes one repository, not everything the account can reach. Use a dedicated
filename so an existing `id_ed25519` is never overwritten (`ssh-keygen` would
prompt, and under `ssh orcd '...'` that prompt reads EOF).

```bash
ssh orcd 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519_github -C orcd-github && cat ~/.ssh/id_ed25519_github.pub'
# add the printed key at https://github.com/<org>/<repo>/settings/keys  (Deploy keys, read-only)
ssh orcd 'printf "\nHost github.com\n    IdentityFile ~/.ssh/id_ed25519_github\n    IdentitiesOnly yes\n    CheckHostIP no\n" >> ~/.ssh/config'
ssh orcd 'ssh -o StrictHostKeyChecking=accept-new -T git@github.com'   # accept GitHub's host key once
```

`CheckHostIP no` is OpenSSH's default since 8.5; the Rocky 8 login nodes run
8.0, where it is `yes` and causes the trap below.

### The stale IP host-key trap

GitHub rotated its RSA host key (2023) and serves several IPs. A `known_hosts`
line holding an old key **for an IP** fails strict checking: interactive git
warns and continues, every `BatchMode` caller (agent, cron, sbatch) gets
`Host key verification failed`, and it is intermittent because it depends
which IP DNS returned. The doctor names the offending IP.

```bash
ssh orcd 'ssh-keygen -R <ip from the warning>'   # works on hashed known_hosts too
```

`CheckHostIP no` under `Host github.com` (above) prevents recurrence.

## Path 2: `git bundle` (no key on the cluster)

One credential-free file carrying exactly the commits you name. Resolve the
cluster path locally first: `$SCRATCH` is not set on your machine, and scp's
SFTP mode never expands variables.

```bash
S=$(ssh orcd 'readlink -f ~/orcd/scratch')
git bundle create /tmp/work.bundle <base>..<branch>      # first time: git bundle create /tmp/work.bundle <branch>
scp /tmp/work.bundle "orcd:$S/"
ssh orcd "cd '$S/<checkout>' && git fetch '$S/work.bundle' <branch> && git checkout --detach FETCH_HEAD"
# first time instead:  ssh orcd "git clone '$S/work.bundle' '$S/<checkout>'"
```

`<base>..<branch>` requires the checkout to already contain `<base>`.
`git checkout --detach` refuses to clobber local edits; use `git reset --hard`
only when discarding them is intended.

## Always

- Echo `git rev-parse HEAD` into every job's output; an artifact whose code
  cannot be identified afterwards is worth much less.
- Reuse an existing checkout and environment. A fresh `uv sync --all-extras`
  costs tens of thousands of inodes, and the inode cap bites long before bytes
  ([storage.md](storage.md)).
