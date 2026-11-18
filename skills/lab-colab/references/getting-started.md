# Getting started with the Lab Colab Space

The lab keeps its work on GitHub: ideas, questions, experiments, and tasks, linked together, with a board that shows who is doing what. You do not need to know GitHub to take part. Your AI assistant does the bookkeeping; you say what you want.

Everything works through **your own GitHub sign-in**, used by your assistant when you ask it to do something. There is no lab robot, no shared account, and nothing to install in any organisation. You see exactly what GitHub lets you see; items from repos you cannot read show up on the board as hidden.

## The Claude app alone is not ready yet

Every lab-colab function today runs `colab.py`, which needs the GitHub tool (`gh`) signed in as you and Python 3.11 or newer. The Claude app on its own (a GitHub connector, no terminal) has neither, so it cannot sync the board, read your inbox, claim, or run the safety check. An app-only mode is future work.

Until then, use **Claude Code**, either on your laptop or on ORCD through remote control (you can drive an ORCD session from the Claude app or the browser; the work runs on the cluster). On ORCD, `python3.11` is installed system-wide (`/usr/bin/python3.11`), but `gh` is not installed system-wide. Install your own without root: download the `linux_amd64` tarball and the `checksums.txt` file from https://github.com/cli/cli/releases, check the tarball with `sha256sum -c --ignore-missing checksums.txt`, unpack it, and copy `bin/gh` into `~/.local/bin` (which should be on your `PATH`).

What working this way looks like, once set up:

- Ask in plain words: "What can I help with this week?", "File this idea: ...", "Turn these meeting notes into items.", "Who is working on the senselab release?", "Hand off what I did today on the speech experiment."
- Your assistant proposes new items; a steward of the area accepts them. Anything that needs a steward or an admin goes to them as a request with a link.
- When you ask "what's new?" or "what can I pick up?", your assistant first brings the board up to date with what you can see (a few seconds), then reads what changed since you last looked. Nobody sends you notification issues; you get the news when you ask.
- Before an assistant claims or runs an item, it reads the item's text and comments for anything that looks like instructions aimed at an assistant. If it says an item "has text that looks like instructions to an assistant", a steward of that area needs to read the item and clear it before any assistant works on it: that is a safety check, not an error. Stewards clear an item themselves, in their own terminal (`colab.py security clear <item> --reason "..."`, then type the item back when asked); an assistant cannot do it for them. The clear counts until someone changes the item again.

## If you write code (Claude Code and a terminal)

1. Accept the org invitation: GitHub sends an email inviting you to the lab's organisation; click **Join**. (Lost it? Open `https://github.com/orgs/<org>/invitation` while signed in.)
2. Sign in once in a browser, nothing to copy:

   ```bash
   gh auth login --web -s repo,read:org,project
   ```

3. Run the check with Python 3.11 or newer. On ORCD, plain `python3` is 3.6 and will not work; use `python3.11` (elsewhere, any Python 3.11+). It tells you, line by line, what is ready and the one next step for anything that is not:

   ```bash
   python3.11 <skill folder>/scripts/colab.py setup --clone --install-hooks
   ```

   - `--clone` puts the lab's settings (the meta repo) in a default place and prints a line to add to your shell profile.
   - `--install-hooks` lets your assistant tell the lab when it is working on an item it claimed, so its claims do not lapse unnoticed (sessions that claimed nothing post nothing). It backs up `~/.claude/settings.json` first. Re-run it after a plugin update: it repoints a hook whose script no longer exists.
   - `--json` prints the same checks for an agent.

4. Run it again until every line has a ✓.
5. Day to day, your assistant runs these for you (you can too):
   - `colab.py next` lists what you can pick up; it starts with a quick `sync` and ends with a note on active sessions and your inbox.
   - `colab.py inbox` shows what is new since you last looked, for the spaces you steward and your own subscriptions (`~/.config/lab-colab/subscriptions.toml`, same format as the lab's).
   - `colab.py sync` puts what you can see onto the lab's board, with your login; add `--dry-run` to see what it would change.

Lines with ✗ always say what to do next ("Next:") and, where useful, a page to open ("Link:"). The indented "details" line is for when you want the GitHub specifics.

## The bus (optional)

The lab no longer needs a background service: the board, the news, the safety check, and credit all work through each member's own sign-in. An org may still run the bus as an optional relay that files delivery issues in subscriber repos; most labs do not. See [runbook.md](./runbook.md), section 4, and [operators.md](./operators.md) if yours does.
