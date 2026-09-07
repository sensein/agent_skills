# First-time ORCD setup

Goal: key-based, prompt-free ssh from your machine to `orcd-login.mit.edu`.
`orcd_doctor.py` checks every step and prints the remedy; this file is the
long form.

Login nodes accept no password over ssh. The only ways in are an installed key
or the OnDemand portal (<https://orcd-ood.mit.edu/>, MIT credentials + Duo), so
the portal installs the key once.

## Steps

1. **Key.** `ls ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -C "$USER@mit.edu"`.
   ed25519 (advertised by ORCD's sshd; avoids old-RSA SHA-1 failures). Set a
   passphrase and `ssh-add` it: only the doctor's first connection can answer a
   prompt; every other scripted call closes stdin. If the ORCD key is not the
   first of `id_ed25519`/`id_ecdsa`/`id_rsa`, pass `--identity <path>`.
2. **Copy the public key**: `pbcopy < ~/.ssh/id_ed25519.pub` (macOS) or
   `xclip -sel clip < ~/.ssh/id_ed25519.pub`. Only the `.pub`; a private key
   that leaves the machine is replaced.
3. **Portal shell**: sign in, **Clusters -> Shell Access** (a login-node shell,
   same `$HOME` ssh lands in).
4. **Install** there -- `>>`, never `>` (that deletes existing keys):

   ```bash
   mkdir -p ~/.ssh && chmod 700 ~/.ssh
   cat >> ~/.ssh/authorized_keys      # paste, Ctrl-D
   chmod 600 ~/.ssh/authorized_keys
   ```

5. **Configure and test locally**: `python3 scripts/orcd_doctor.py --fix --user <mit-username>`
   appends this block to `~/.ssh/config` and connects; then `ssh orcd hostname`
   prints a login node. Keep the browser signed in for that first ssh.

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

### Cloud or sandbox environments

The key pair lives in an ephemeral container, and authorizing it grants that
container access to the account. Say so, get the owner's explicit OK, use a
dedicated identifiable key, have it revoked when the environment is retired,
and never copy the private key out. Losing the key with the container is
normal; mint a fresh one next time.

When the sandbox has ssh egress (the doctor's `tcp port 22` check passes),
`python3 scripts/orcd_doctor.py --sandbox-setup --user <mit-username>` verifies
egress, mints an `orcd-sandbox-<user>-<date>` ed25519 key (no passphrase:
headless, ephemeral, revocable) if none exists, and prints two commands for the
**account owner**: the `authorized_keys` append and its revocation. The owner
running the append is the authorization; an agent never adds the key. Then
`--fix --user <mit-username>` connects. Blocked egress: no key helps; the
environment's network policy must change.

## Authentication: what `ssh -vv` shows

`AuthenticationMethods publickey,keyboard-interactive`. A good connection:

```
Authenticated using "publickey" with partial success.
debug1: Authentications that can continue: keyboard-interactive
debug2: input_userauth_info_req: num_prompts 0
Authenticated to orcd-login.mit.edu using "keyboard-interactive".
```

`num_prompts 0` is Duo passing on portal-established device trust. When that
lapses, a real prompt appears and non-interactive calls fail: sign in at the
portal, not key surgery.

`BatchMode=yes` disables keyboard-interactive on the client, so it always fails
with `Permission denied (keyboard-interactive)` -- which reads like a bad key.
Telling them apart after the `partial success` line: BatchMode (or
`KbdInteractiveAuthentication no`) reports `No more authentication methods to
try` at once; lapsed Duo starts the exchange and prompts (or hangs to timeout
non-interactively). `ssh -G orcd-login.mit.edu | grep -iE
'batchmode|kbdinteractive|preferredauthentications'` shows the effective
merged config.

**Lockout**: ten failed Duo attempts disable the account for 90 minutes, and
auto-reconnecting software (VS Code Remote-SSH) keeps resetting the timer.
Close it, sign in at the portal, then retry.

**Multiplexing**: one master carries the session (`ControlPersist 12h`; scp
rides it). `ssh -O check orcd` (is a master live; never authenticates),
`ssh orcd true` (open one), `ssh -O exit orcd` (close, e.g. after a laptop
sleep).

## Reachable is not enabled

A new account can log in yet have no **Slurm association** (`sacctmgr show
assoc user=$USER` empty; every `sbatch` refused) or no **`orcd_rg_*` groups**
(only `$HOME` writable, no private partitions). Both are WARNs in the doctor
and fixed by orcd-help@mit.edu or the PI, not from the client.

## uv in the cluster home

Login-node `python3` is 3.6; no system uv or conda. `python3 scripts/orcd_uv.py`
reports `~/.local/bin/uv`, its version and whether it is on PATH;
`--install` runs the standalone installer with `UV_INSTALL_DIR=$HOME/.local/bin`
and `UV_NO_MODIFY_PATH=1` (never edits startup files) or `uv self update`.

**No shell profile is modified without the user's explicit approval.**
`--add-to-path` shows the file and the line (`export PATH="$HOME/.local/bin:$PATH"`),
proceeds only after a typed `yes` on a TTY or `--user-approved` (pass only after
asking), backs the file up to `.orcd-uv.bak`, refuses to create a missing
profile (a new `~/.bash_profile` silences `~/.profile`), and re-checks whether a
fresh non-interactive ssh sees uv (an interactivity guard atop `~/.bashrc` can
swallow the line). Scripts and jobs call `$HOME/.local/bin/uv` by absolute path
regardless. Environments and `UV_CACHE_DIR` go on `~/orcd/scratch`, never
`$HOME` ([storage.md](storage.md)).

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Permission denied (keyboard-interactive)` | `BatchMode=yes`, or Duo trust lapsed; `-vv` shows `partial success` |
| `connect to host ... port 22: Connection timed out` | ssh egress blocked (cloud sandboxes allow only HTTPS); the doctor's `tcp port 22` check confirms; not a key problem |
| `kex_exchange_identification: Connection closed` via an HTTP proxy | the proxy answered `CONNECT :22` with 200 but its upstream was denied; same blocked egress |
| Hangs, then times out | a Duo prompt is waiting; run `ssh orcd` by hand |
| `Too many authentication failures` | the agent offers many keys; `IdentitiesOnly yes` |
| Host key changed warning | login nodes sit behind round-robin DNS; verify with ORCD before removing the old key |
| Works in a terminal, fails from an agent | the agent set `BatchMode`, or has no live master socket |
