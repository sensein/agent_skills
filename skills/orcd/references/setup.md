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

**Therefore: never set `BatchMode=yes` for this host.** BatchMode disables
keyboard-interactive on the client, so the second stage cannot happen and the
connection fails with:

```
Permission denied (keyboard-interactive).
```

That message reads like a rejected key, which sends people off replacing keys
that were never broken. The tell is `Server accepts key` followed by
`partial success` earlier in the `-vv` output: the key worked.

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

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `Permission denied (keyboard-interactive)` | `BatchMode=yes` set, or Duo trust lapsed. Check `-vv` for `partial success` |
| Hangs, then times out | A Duo prompt is waiting. Run `ssh orcd` by hand and answer it |
| `Too many authentication failures` | The agent is offering many keys. Add `IdentitiesOnly yes` |
| Host key changed warning | Login nodes are behind round-robin DNS. Verify with ORCD before removing the old key |
| Works in a terminal, fails from an agent | The agent set `BatchMode`, or has no live master socket |
