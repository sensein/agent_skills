# GitHub tools for Claude Code on ORCD

Claude Code sessions on ORCD can use GitHub's MCP tools: searching issues and PRs, reading files and Projects. This applies to sessions you start there, including ones you drive from the Claude app or browser through remote control.

The GitHub connector you add in claude.ai does not reach those sessions. Other claude.ai connectors (Slack, Hugging Face, …) do load, but the GitHub one did not as of 2026-11-19, even after a session restart. So add GitHub's official remote MCP server to Claude Code itself, signed in with your own `gh` login.

## Prerequisites

- `gh` is installed and signed in. `gh` is not installed system-wide on ORCD. Install your own copy into `~/.local/bin`:
  1. Download the `gh_<version>_linux_amd64.tar.gz` tarball and `gh_<version>_checksums.txt` from the latest release at https://github.com/cli/cli/releases.
  2. In the download directory, check them with `sha256sum -c --ignore-missing gh_<version>_checksums.txt`.
  3. Unpack the tarball and copy `bin/gh` into `~/.local/bin`. Make sure `~/.local/bin` is on your `PATH`; `command -v gh` should print it.
  4. Run `gh auth login --web`.
- Claude Code is installed on ORCD (`claude --version`).

## Add it (once per person)

```bash
GH="$(command -v gh)"   # use the absolute path; the helper may run without your PATH
claude mcp add-json -s user github "{\"type\":\"http\",\"url\":\"https://api.githubcopilot.com/mcp/\",\"headersHelper\":\"printf '{\\\"Authorization\\\":\\\"Bearer %s\\\",\\\"X-MCP-Readonly\\\":\\\"true\\\",\\\"X-MCP-Toolsets\\\":\\\"default,projects\\\"}' \\\"\$($GH auth token)\\\"\"}"
claude mcp get github     # Status: ✔ Connected
```

- **`-s user`** makes the server available in every Claude Code session you start on ORCD. The tools appear from the **next** session on.
- **`headersHelper`** runs `gh auth token` each time Claude Code connects. The token is never written into `~/.claude.json`: only the command is stored. If you rotate or refresh your `gh` login, nothing needs to change. `printf` is a shell builtin, so the token never appears in any process's arguments, where other users on a shared login node could see it with `ps`. Keep it a builtin: don't replace it with an external command such as `echo` from `/usr/bin` or `curl`.
- **`X-MCP-Readonly: true`** exposes read tools only: about 30, such as `search_issues`, `list_issues`, `issue_read`, `pull_request_read`, `get_file_contents`, `projects_list` and `projects_get`. There are no create, update, merge or push tools. Keep it read-only: make writes with `gh` or `git`, where Claude Code's permission prompts and your repo's PR rules apply.
- **`X-MCP-Toolsets: default,projects`** adds read access to GitHub Projects (org boards) on top of the default set.

To remove it:

```bash
claude mcp remove github -s user
```

## Notes

- **What the tools can see.** They see what your `gh` token can see. The token's scopes are listed by `gh auth status`. Read-only mode limits the tools, not the token.
- **Laptops too.** The same command works on a laptop.
- **Agents.** An agent must ask the user before running the `add-json` command for them. It changes the user's Claude Code config (`~/.claude.json`), so it needs an explicit yes in that session.
