# Getting started with the Lab Colab Space

The lab keeps its work on GitHub: ideas, questions, experiments, and tasks, linked together, with a board that shows who is doing what. You do not need to know GitHub to take part. Your AI assistant does the bookkeeping; you say what you want.

## If you use the Claude app (no terminal)

Most members start here. There is nothing to install and no token to make.

1. **Accept the invitation.** GitHub sends an email inviting you to the lab's organisation. Click **Join**. (Lost it? Open `https://github.com/orgs/<org>/invitation` while signed in.)
2. **Connect GitHub.** In the Claude app, open Settings, then Connectors, and click **Connect** next to GitHub. Sign in when GitHub asks. That is the only sign-in.
3. **Turn on the lab's plugin.** In Settings, under the lab's plugins, enable `lab-colab`.
4. **Ask.** For example:
   - "What can I help with this week?"
   - "File this idea: ..." or "Turn these meeting notes into items."
   - "Who is working on the senselab release?"
   - "Hand off what I did today on the speech experiment."

Your assistant proposes new items; a steward of the area accepts them. Anything that needs a steward or an admin goes to them as a request with a link. If the assistant says an item "is waiting for a steward to check it", someone needs to look at that item's text before any assistant works on it: that is a safety check, not an error.

## If you write code (Claude Code and a terminal)

1. Accept the org invitation (as above).
2. Sign in once in a browser, nothing to copy:

   ```bash
   gh auth login --web -s repo,read:org,project
   ```

3. Run the check. It tells you, line by line, what is ready and the one next step for anything that is not:

   ```bash
   python3 <skill folder>/scripts/colab.py setup --clone --install-hooks
   ```

   - `--clone` puts the lab's settings (the meta repo) in a default place and prints a line to add to your shell profile.
   - `--install-hooks` lets your assistant tell the lab when it is working, so its claims do not lapse unnoticed. It backs up `~/.claude/settings.json` first.
   - `--json` prints the same checks for an agent.

4. Run it again until every line has a ✓.

Lines with ✗ always say what to do next ("Next:") and, where useful, a page to open ("Link:"). The indented "details" line is for when you want the GitHub specifics.

## Running a bus

A few members run the lab's bus, the small background job that moves news between projects. That needs one more step: see [operators.md](./operators.md).
