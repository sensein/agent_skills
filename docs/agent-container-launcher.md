# Agent Container Launcher

`scripts/launch_agent_container.py` creates or reuses a small TOML config and launches a containerized `codex` or `claude` session without mounting the full home directory.

## Defaults

- Engine: auto-select `apptainer` on Linux when available, otherwise `docker`
- Image: `node:24-bookworm-slim` for Docker, `docker://node:24-bookworm` for Apptainer
- Config: `./.agent-container.toml`
- Skills: mounts this repo's `skills/` directory read-only when present
- System tools: Docker bootstraps `git` in-container when needed; Apptainer treats the image as immutable and expects `bash`, `git`, and any installer transport like `curl` or `wget` to already be present

## Example

```bash
python scripts/launch_agent_container.py \
  --agent codex \
  --rw-dir /path/to/repo \
  --ro-dir /path/to/reference-data \
  --skill-dir /path/to/extra-skills \
  --write-config \
  --dry-run
```

On first run, the script writes the config file if it does not already exist. Later runs can reuse or edit that TOML file directly.

## Security Model

- mounts only the directories you list, plus a narrow agent state directory and a launcher-specific tool state directory
- does not mount the full home directory
- mounts skill directories read-only
- mounts task code or data directories read-only or read-write based on the provided flags

## Reusable Config

The generated config stores:

- selected agent
- container engine and image
- agent state and tool state directories
- extra authentication paths that should be mounted into the container
- read-only and read-write task mounts
- skill directories
- environment variables to pass through
- extra CLI options that should be passed to the agent before positional args
- extra agent CLI arguments

## CLI passthrough

Use `--cli-option` for agent flags that should appear before positional args or subcommands.

Examples:

```bash
python scripts/launch_agent_container.py \
  --agent codex \
  --cli-option --model \
  --cli-option gpt-5 \
  --cli-option --full-auto \
  --agent-arg login \
  --agent-arg status
```

```bash
python scripts/launch_agent_container.py \
  --agent claude \
  --cli-option --verbose \
  --agent-arg auth \
  --agent-arg status
```

Use `agent_args` for trailing subcommands or positional args.

## Authentication

The launcher already mounts the selected agent's main state directory, such as `~/.codex` or `~/.claude`, in read-write mode.

If credentials or helper auth files live elsewhere, add them with `--auth-path /path/to/auth-store`. Home-relative auth paths are mounted back into the same relative location under `/home/agent`, and non-home auth paths are mounted under `/home/agent/.agent-auth/`.
