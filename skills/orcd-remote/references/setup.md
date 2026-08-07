# First-time ORCD setup

The goal is key-based SSH from a laptop to `orcd-login.mit.edu` that works
without a prompt, so agents can run commands non-interactively.

`python3 scripts/orcd_doctor.py` checks every step below and prints what is
missing. Read this document when a step needs explaining or when the doctor's
remedy did not work.

## Why the key has to go through a browser

ORCD's login nodes accept no password over SSH. The only ways in are a key that
is already installed, or the OnDemand web portal, which authenticates with MIT
credentials plus Duo. So the bootstrap is: use the portal once to install the
key, then use the key from then on.

The portal is at <https://orcd-ood.mit.edu/>.

## Steps

### 1. Have a key

```bash
ls ~/.ssh/id_ed25519 2>/dev/null || ssh-keygen -t ed25519 -C "$USER@mit.edu"
```

Prefer ed25519. ORCD's sshd advertises it, and it avoids the SHA-1 signature
problems that can make a very old RSA key fail in confusing ways. Set a
passphrase; the OS agent will cache it.

### 2. Copy the public key

```bash
pbcopy < ~/.ssh/id_ed25519.pub            # macOS
xclip -sel clip < ~/.ssh/id_ed25519.pub   # Linux
```

Copy the `.pub` file. If a private key ever leaves the machine, replace it.

**If the agent is running in a cloud environment** (Claude Code on the web, a
CI runner, a devcontainer) rather than on the user's own machine, the key pair
just generated lives in that environment -- and installing its public key on
ORCD gives that environment SSH access to the user's cluster account. Before
asking the user to add the key, say this plainly and:

- Get the account owner's explicit OK first.
- Use a dedicated key with an identifying comment, e.g.
  `ssh-keygen -t ed25519 -C "agent-cloud-$(date +%Y%m%d)"`, so it is easy to
  spot in `authorized_keys` later.
- Tell the user to remove that line from `~/.ssh/authorized_keys` on ORCD when
  the environment is retired or no longer trusted.
- Expect the container to be ephemeral: the private key may vanish when the
  session ends. That is normal and fine -- generate and install a fresh key
  next time. Never copy a private key out of the container to "save" it.

### 3. Get a shell through the portal

Sign in at <https://orcd-ood.mit.edu/>, then choose **Clusters -> Shell
Access** from the top menu. That is a shell on a login node, already
authenticated, with the same `$HOME` that SSH will land in.

### 4. Install the key

In that portal shell:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
cat >> ~/.ssh/authorized_keys      # paste the key, then press Ctrl-D
chmod 600 ~/.ssh/authorized_keys
```

`>>` appends. Using `>` would delete any key already there, including one a
collaborator or a cluster service depends on.

### 5. Configure and test locally

```bash
python3 scripts/orcd_doctor.py --fix
```

That appends a working block to `~/.ssh/config` and opens the connection. The
block it writes:

```
Host orcd orcd-login.mit.edu
    HostName orcd-login.mit.edu
    User <your-username>
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    PreferredAuthentications publickey,keyboard-interactive
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 12h
    ServerAliveInterval 60
```

Then `ssh orcd hostname` should print something like `login009`.

Keep the browser session signed in for that first SSH. The Duo device trust it
establishes is what lets the second authentication factor pass silently.

## The authentication flow, and the one trap

ORCD sets `AuthenticationMethods publickey,keyboard-interactive`. A successful
connection looks like this under `ssh -vv`:

```
debug1: Authentications that can continue: publickey,keyboard-interactive
Authenticated using "publickey" with partial success.
debug1: Authentications that can continue: keyboard-interactive
debug2: input_userauth_info_req: entering
debug2: input_userauth_info_req: num_prompts 0
Authenticated to orcd-login.mit.edu using "keyboard-interactive".
```

`num_prompts 0` is Duo waving the session through on established device trust.
Nothing is typed, but it is still a keyboard-interactive exchange.

The mental model worth keeping: **web first, then SSH is effectively
single-factor.** While a sign-in at the OnDemand portal holds, the second stage
answers itself and SSH feels like plain key auth. When that web authorization
expires, SSH reverts to true two-factor -- a real prompt appears, and anything
non-interactive fails until a human answers one. So the first move on any 2FA
symptom is a browser visit to <https://orcd-ood.mit.edu/>, not key surgery.

**And never set `BatchMode=yes` for this host.** BatchMode disables
keyboard-interactive on the client, so the second stage cannot happen and the
connection fails with:

```
Permission denied (keyboard-interactive).
```

That message reads like a rejected key, which sends people off replacing keys
that were never broken. The tell is `Server accepts key` followed by
`partial success` earlier in the `-vv` output: the key worked.

Distinguishing the two causes from `-vv` output, after the `partial success`
line (both print `Authentications that can continue: keyboard-interactive`, so
that line alone distinguishes nothing):

- **BatchMode (or `KbdInteractiveAuthentication no`)**: the client immediately
  reports `No more authentication methods to try` and fails. No prompt is ever
  attempted -- the client refused the method.
- **Lapsed Duo trust**: the keyboard-interactive exchange starts and a real
  prompt appears (or, in a non-interactive context, the session hangs until it
  times out).

Check the effective client config with `ssh -G orcd-login.mit.edu | grep -iE
'batchmode|kbdinteractive|preferredauthentications'` -- `-G` merges every config
source, which is exactly what eye-reading a config file misses.

## Duo lockout

Ten failed Duo attempts disable the account, and the lock clears automatically
after 90 minutes. The trap is software that retries on its own: a VS Code
Remote-SSH window left open keeps reconnecting in the background, each attempt
fails the second factor, and the lockout timer resets forever. If Duo prompts
have started failing repeatedly: stop, close anything that auto-reconnects,
sign in at the portal once, and only then try SSH again.

To keep automation non-interactive without BatchMode, open one master connection
and let everything else reuse it. That is what `orcd_common.py` does.

## Connection multiplexing

```bash
ssh -O check orcd     # is a master live? cheap, never authenticates
ssh orcd true         # open one (may prompt if Duo trust lapsed)
ssh -O exit orcd      # close it
```

With `ControlPersist 12h`, one authentication covers a working day, and `scp`
uses the same socket. If the socket goes stale after a laptop sleep or network
change, `ssh -O exit orcd` and reconnect.

## What "set up" does not include

Reaching a login node is necessary but not sufficient. A new account can log in
and still be unable to run anything:

- **No Slurm association.** `sacctmgr show assoc user=$USER` prints nothing, and
  every `sbatch` is refused. Ask orcd-help@mit.edu to add the account.
- **No `orcd_rg_*` groups.** Only `$HOME` is writable, and no private partition
  is reachable. Group membership is what grants both storage and partitions, so
  ask the PI or orcd-help@mit.edu to be added to the lab's groups.

`orcd_doctor.py` reports both as warnings rather than failures, because SSH
genuinely is working at that point. Neither is fixable from the client side.

## Python tooling: uv in the cluster home

The login nodes' system `python3` is 3.6, and `uv`/`conda` are not installed
system-wide, so a per-user `uv` at `~/.local/bin/uv` in the **cluster** home
directory is the supported way to get modern Python. `orcd_doctor.py` reports
whether it is present; `orcd_uv.py` manages it:

```bash
python3 scripts/orcd_uv.py             # installed? what version? on PATH?
python3 scripts/orcd_uv.py --install   # install, or upgrade if already there
```

`--install` uses the official standalone installer pinned to
`UV_INSTALL_DIR=$HOME/.local/bin` with `UV_NO_MODIFY_PATH=1`, so no shell
startup file is ever edited by the installer. Upgrades go through
`uv self update`.

### PATH, and the profile-approval rule

**No shell profile (`~/.bashrc`, `~/.bash_profile`, `~/.profile`) is modified
without the user's explicit approval.** `orcd_uv.py` enforces this: the
`--add-to-path` action shows the exact file and line it would append
(`export PATH="$HOME/.local/bin:$PATH"`), then proceeds only after a typed
`yes` on a TTY, or with `--user-approved` -- a flag an agent may pass only
after actually asking the user and getting a yes. In a non-interactive run
without that flag, it refuses. It also writes a `.orcd-uv.bak` backup before
appending.

The edit is optional. Scripts, agents, and sbatch job scripts should call
`$HOME/.local/bin/uv` by absolute path or export PATH themselves; the profile
line only exists for the user's interactive convenience. After an approved
edit the script re-checks over a fresh SSH connection and reports honestly if
the line is not reaching non-interactive shells (a common cause is an
interactivity guard near the top of `~/.bashrc` that `return`s before the
appended line runs).

One storage caveat: keep uv's cache and the environments it creates off
`$HOME` -- resolving an environment is exactly the many-small-file workload
that exhausts the 1 M inode quota. Set `UV_CACHE_DIR` and create venvs on
flash scratch (see [storage.md](storage.md)).

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Permission denied (keyboard-interactive)` | `BatchMode=yes` set, or Duo trust lapsed. Check `-vv` for `partial success` |
| `connect to host ... port 22: Connection timed out` | SSH egress is blocked -- cloud agent environments often allow only HTTPS. The doctor's `tcp port 22` check confirms it; keys and Duo are not the problem |
| Hangs, then times out | A Duo prompt is waiting. Run `ssh orcd` by hand and answer it |
| `Too many authentication failures` | The agent is offering many keys. Add `IdentitiesOnly yes` |
| Host key changed warning | Login nodes are behind round-robin DNS. Verify with ORCD before removing the old key |
| Works in a terminal, fails from an agent | The agent set `BatchMode`, or has no live master socket |
