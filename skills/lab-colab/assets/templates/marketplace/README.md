# __REPO__

Private Claude plugin marketplace for the __ORG__ organization. It has one plugin per skill in the public [__ORG__/__SOURCE_REPO__](https://github.com/__ORG__/__SOURCE_REPO__) repo, and each plugin fetches only its own skill folder.

- **Updates:** the `track-agent-skills` workflow runs hourly. When `__SOURCE_REPO__` main moves, it pins every plugin to the new commit, bumps their versions, and merges that as a PR. A merged version bump is what triggers the claude.ai marketplace sync. The repo's Actions workflow permissions must be read and write, with Actions allowed to approve PRs.
- **Checks:** `scripts/check_marketplace.py` runs in CI (`validate`) and before each bump. It enforces what claude.ai org sync enforces: https git URLs, source types `github`, `url`, `git-subdir` or `./`, `"strict": false` when a plugin has no `plugin.json`, under 200 MB per plugin, no top-level `bin/`, a public repo for any source under another owner, and this repo private.
- **claude.ai (org-wide):** an owner installs the Claude GitHub App on this repo, then adds it under Organization settings > Plugins & skills > Marketplaces > Add plugins > Sync from GitHub.
- **Claude Code (per person):** `claude plugin marketplace add __ORG__/__REPO__`, then e.g. `claude plugin install <skill>@__NAME__`.
