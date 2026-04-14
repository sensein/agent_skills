#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_IMAGE = "node:24-bookworm-slim"
DEFAULT_APPTAINER_IMAGE = "docker://node:24-bookworm"
DEFAULT_CONFIG_NAME = ".agent-container.toml"
SCRIPT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AgentSpec:
    name: str
    command: str
    install_package: str
    install_mode: str
    agent_state_subdir: str
    skills_subdir: str
    default_env_vars: tuple[str, ...]


@dataclass(frozen=True)
class Mount:
    host_path: Path
    container_path: str
    mode: str


AGENT_SPECS = {
    "codex": AgentSpec(
        name="codex",
        command="codex",
        install_package="@openai/codex",
        install_mode="npm",
        agent_state_subdir=".codex",
        skills_subdir=".codex/skills",
        default_env_vars=("OPENAI_API_KEY",),
    ),
    "claude": AgentSpec(
        name="claude",
        command="claude",
        install_package="https://claude.ai/install.sh",
        install_mode="native",
        agent_state_subdir=".claude",
        skills_subdir=".claude/skills",
        default_env_vars=("ANTHROPIC_API_KEY",),
    ),
}


class SmartDefaultsFormatter(argparse.ArgumentDefaultsHelpFormatter):
    def _get_help_string(self, action: argparse.Action) -> str:
        if action.default in (None, argparse.SUPPRESS):
            return action.help or ""
        return super()._get_help_string(action)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a containerized Codex or Claude session with reusable config.",
        formatter_class=SmartDefaultsFormatter,
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_NAME,
        help="TOML config file to read or create.",
    )
    parser.add_argument(
        "--write-config",
        action="store_true",
        help="Persist the resolved settings back to the config file.",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the config file contents after resolution and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved command and mounts without launching the container.",
    )
    parser.add_argument(
        "--agent",
        choices=sorted(AGENT_SPECS),
        default=None,
        help="Agent CLI to launch. Defaults to the config value or 'codex'.",
    )
    parser.add_argument(
        "--engine",
        choices=("auto", "docker", "apptainer"),
        default=None,
        help="Container engine preference. Defaults to the config value or 'auto'.",
    )
    parser.add_argument(
        "--image",
        default=None,
        help=(
            "Container image to launch. Defaults to the config value or "
            f"'{DEFAULT_IMAGE}' ('{DEFAULT_APPTAINER_IMAGE}' for Apptainer)."
        ),
    )
    parser.add_argument(
        "--workspace-name",
        default=None,
        help="Label for the task workspace. Defaults to the config value or 'default'.",
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Optional external workspace root. Defaults to the config value or unset.",
    )
    parser.add_argument(
        "--agent-state-dir",
        default=None,
        help="Persistent host directory for the agent's own state and auth. Defaults to the config value or ~/.codex / ~/.claude.",
    )
    parser.add_argument(
        "--tool-state-dir",
        default=None,
        help="Persistent host directory for launcher-managed caches such as npm installs. Defaults to the config value or .agent-container-state/<agent> next to the config file.",
    )
    parser.add_argument(
        "--auth-path",
        action="append",
        default=None,
        help="Extra authentication file or directory to mount into the container. Repeat as needed.",
    )
    parser.add_argument(
        "--rw-dir",
        action="append",
        default=None,
        help="Host directory to mount read-write for the task. Repeat as needed.",
    )
    parser.add_argument(
        "--ro-dir",
        action="append",
        default=None,
        help="Host directory to mount read-only for the task. Repeat as needed.",
    )
    parser.add_argument(
        "--skill-dir",
        action="append",
        default=None,
        help="Skill directory or skills root to mount read-only. Defaults to the config value or this repo's skills/ directory when present.",
    )
    parser.add_argument(
        "--env-var",
        action="append",
        default=None,
        help="Environment variable name to pass through into the container. Defaults to agent-specific API key variables.",
    )
    parser.add_argument(
        "--cli-option",
        action="append",
        default=None,
        help="Option to pass through to the agent CLI before positional args. Repeat as needed.",
    )
    parser.add_argument(
        "--agent-arg",
        action="append",
        default=None,
        help="Extra argument to pass to the agent CLI inside the container. Repeat as needed.",
    )
    return parser.parse_args()


def default_agent_state_dir(agent: str) -> Path:
    return Path.home() / AGENT_SPECS[agent].agent_state_subdir


def default_tool_state_dir(config_path: Path, agent: str) -> Path:
    return config_path.parent / ".agent-container-state" / agent


def default_auth_paths(agent: str) -> list[Path]:
    if agent == "claude":
        candidate = Path.home() / ".claude.json"
        if candidate.exists():
            return [candidate]
    return []


def default_skill_dirs() -> list[Path]:
    skills_dir = SCRIPT_ROOT / "skills"
    if skills_dir.exists():
        return [skills_dir]
    return []


def load_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    return {
        "agent": raw.get("agent"),
        "engine": raw.get("engine"),
        "image": raw.get("image"),
        "workspace_name": raw.get("workspace_name"),
        "workspace_root": raw.get("workspace_root"),
        "agent_state_dir": raw.get("agent_state_dir"),
        "tool_state_dir": raw.get("tool_state_dir"),
        "auth_paths": raw.get("auth_paths", []),
        "rw_dirs": raw.get("rw_dirs", []),
        "ro_dirs": raw.get("ro_dirs", []),
        "skill_dirs": raw.get("skill_dirs", []),
        "env_vars": raw.get("env_vars", []),
        "cli_options": raw.get("cli_options", []),
        "agent_args": raw.get("agent_args", []),
    }


def resolved_config_text(settings: dict[str, object]) -> str:
    return "\n".join(
        [
            f'agent = {toml_string(str(settings["agent"]))}',
            f'engine = {toml_string(str(settings["engine"]))}',
            f'image = {toml_string(str(settings["image"]))}',
            f'workspace_name = {toml_string(str(settings["workspace_name"]))}',
            f'workspace_root = {toml_string(str(settings["workspace_root"]))}',
            f'agent_state_dir = {toml_string(str(settings["agent_state_dir"]))}',
            f'tool_state_dir = {toml_string(str(settings["tool_state_dir"]))}',
            f'auth_paths = {format_toml_list([str(value) for value in settings["auth_paths"]])}',
            f'rw_dirs = {format_toml_list([str(value) for value in settings["rw_dirs"]])}',
            f'ro_dirs = {format_toml_list([str(value) for value in settings["ro_dirs"]])}',
            f'skill_dirs = {format_toml_list([str(value) for value in settings["skill_dirs"]])}',
            f'env_vars = {format_toml_list([str(value) for value in settings["env_vars"]])}',
            f'cli_options = {format_toml_list([str(value) for value in settings["cli_options"]])}',
            f'agent_args = {format_toml_list([str(value) for value in settings["agent_args"]])}',
            "",
        ]
    )


def config_value(
    cli_value: object | None, loaded_value: object | None, fallback: object
) -> object:
    if cli_value is not None:
        return cli_value
    if loaded_value is not None:
        return loaded_value
    return fallback


def ensure_list(value: object | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def normalize_mount_path(path_str: str, label: str) -> Path:
    path = Path(path_str).expanduser().resolve()
    home = Path.home().resolve()
    if path == home:
        raise ValueError(f"Refusing to mount the full home directory for {label}: {path}")
    if not path.exists():
        raise ValueError(f"Mount path for {label} does not exist: {path}")
    return path


def maybe_agent_specific_string(
    *,
    cli_value: str | None,
    loaded_value: object | None,
    fallback: str,
    agent_overridden: bool,
) -> str:
    if cli_value is not None:
        return cli_value
    if agent_overridden:
        return fallback
    if loaded_value is None:
        return fallback
    return str(loaded_value)


def maybe_agent_specific_list(
    *,
    cli_value: list[str] | None,
    loaded_value: object | None,
    fallback: list[str],
    agent_overridden: bool,
) -> list[str]:
    if cli_value is not None:
        return cli_value
    if agent_overridden:
        return fallback
    if loaded_value is None:
        return fallback
    return ensure_list(loaded_value)


def resolve_settings(args: argparse.Namespace, config_path: Path) -> dict[str, object]:
    loaded = load_config(config_path)
    loaded_agent = str(loaded["agent"]) if loaded.get("agent") else None
    agent = str(config_value(args.agent, loaded.get("agent"), "codex"))
    agent_overridden = args.agent is not None and args.agent != loaded_agent
    workspace_name = str(
        config_value(args.workspace_name, loaded.get("workspace_name"), "default")
    )
    env_vars = maybe_agent_specific_list(
        cli_value=args.env_var,
        loaded_value=loaded.get("env_vars"),
        fallback=list(AGENT_SPECS[agent].default_env_vars),
        agent_overridden=agent_overridden,
    )
    settings = {
        "agent": agent,
        "engine": str(config_value(args.engine, loaded.get("engine"), "auto")),
        "image": str(config_value(args.image, loaded.get("image"), "")),
        "workspace_name": workspace_name,
        "workspace_root": str(config_value(args.workspace_root, loaded.get("workspace_root"), "")),
        "agent_state_dir": maybe_agent_specific_string(
            cli_value=args.agent_state_dir,
            loaded_value=loaded.get("agent_state_dir"),
            fallback=str(default_agent_state_dir(agent)),
            agent_overridden=agent_overridden,
        ),
        "tool_state_dir": maybe_agent_specific_string(
            cli_value=args.tool_state_dir,
            loaded_value=loaded.get("tool_state_dir"),
            fallback=str(default_tool_state_dir(config_path, agent)),
            agent_overridden=agent_overridden,
        ),
        "auth_paths": maybe_agent_specific_list(
            cli_value=args.auth_path,
            loaded_value=loaded.get("auth_paths"),
            fallback=[str(path) for path in default_auth_paths(agent)],
            agent_overridden=agent_overridden,
        ),
        "rw_dirs": ensure_list(config_value(args.rw_dir, loaded.get("rw_dirs"), [])),
        "ro_dirs": ensure_list(config_value(args.ro_dir, loaded.get("ro_dirs"), [])),
        "skill_dirs": ensure_list(
            config_value(args.skill_dir, loaded.get("skill_dirs"), [str(path) for path in default_skill_dirs()])
        ),
        "env_vars": env_vars,
        "cli_options": ensure_list(config_value(args.cli_option, loaded.get("cli_options"), [])),
        "agent_args": ensure_list(config_value(args.agent_arg, loaded.get("agent_args"), [])),
    }
    return settings


def toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_toml_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(toml_string(value) for value in values) + "]"


def write_config(config_path: Path, settings: dict[str, object]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(resolved_config_text(settings), encoding="utf-8")


def choose_engine(preferred: str) -> str:
    if preferred != "auto":
        if shutil.which(preferred) is None:
            raise RuntimeError(f"Requested container engine is unavailable: {preferred}")
        return preferred

    system = sys.platform
    candidates = ["docker", "apptainer"] if system.startswith(("darwin", "win")) else ["apptainer", "docker"]
    for candidate in candidates:
        if shutil.which(candidate):
            return candidate
    raise RuntimeError("Neither docker nor apptainer is available on PATH")


def default_image_for_engine(engine: str) -> str:
    if engine == "apptainer":
        return DEFAULT_APPTAINER_IMAGE
    return DEFAULT_IMAGE


def mount_name(path: Path, used: set[str]) -> str:
    base = path.name or "root"
    clean = "".join(char if char.isalnum() or char in ("-", "_") else "-" for char in base).strip("-")
    if not clean:
        clean = "mount"
    candidate = clean
    counter = 2
    while candidate in used:
        candidate = f"{clean}-{counter}"
        counter += 1
    used.add(candidate)
    return candidate


def build_mounts(settings: dict[str, object]) -> tuple[list[Mount], str]:
    used_names: set[str] = set()
    mounts: list[Mount] = []
    rw_dirs = [normalize_mount_path(path, "rw-dir") for path in settings["rw_dirs"]]
    ro_dirs = [normalize_mount_path(path, "ro-dir") for path in settings["ro_dirs"]]
    skill_dirs = [normalize_mount_path(path, "skill-dir") for path in settings["skill_dirs"]]
    auth_paths = [normalize_mount_path(path, "auth-path") for path in settings["auth_paths"]]
    agent_state_dir = Path(str(settings["agent_state_dir"])).expanduser().resolve()
    tool_state_dir = Path(str(settings["tool_state_dir"])).expanduser().resolve()
    agent = str(settings["agent"])
    spec = AGENT_SPECS[agent]
    home = Path.home().resolve()

    agent_state_dir.mkdir(parents=True, exist_ok=True)
    tool_state_dir.mkdir(parents=True, exist_ok=True)

    mounts.append(Mount(agent_state_dir, f"/home/agent/{spec.agent_state_subdir}", "rw"))
    mounts.append(Mount(tool_state_dir, "/home/agent/.container-agent", "rw"))
    for auth_path in auth_paths:
        if auth_path == agent_state_dir:
            continue
        try:
            relative = auth_path.relative_to(home)
            container_path = f"/home/agent/{relative.as_posix()}"
        except ValueError:
            name = mount_name(auth_path, used_names)
            container_path = f"/home/agent/.agent-auth/{name}"
        mounts.append(Mount(auth_path, container_path, "rw"))

    workdir = "/workspace"
    if rw_dirs:
        mounts.append(Mount(rw_dirs[0], "/workspace/task", "rw"))
        workdir = "/workspace/task"
        for path in rw_dirs[1:]:
            name = mount_name(path, used_names)
            mounts.append(Mount(path, f"/workspace/rw/{name}", "rw"))

    for path in ro_dirs:
        name = mount_name(path, used_names)
        mounts.append(Mount(path, f"/workspace/ro/{name}", "ro"))
        if workdir == "/workspace":
            workdir = f"/workspace/ro/{name}"

    for path in skill_dirs:
        name = mount_name(path, used_names)
        mounts.append(Mount(path, f"/opt/agent-skills/{name}", "ro"))

    return mounts, workdir


def shell_quote_args(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def install_block(spec: AgentSpec, engine: str) -> list[str]:
    if spec.install_mode == "npm":
        return [
            "if command -v npm >/dev/null 2>&1; then npm install -g npm@latest; fi",
            f'if ! command -v {spec.command} >/dev/null 2>&1; then npm install -g {shlex.quote(spec.install_package)}; fi'
        ]

    transport_check = [
        "if command -v curl >/dev/null 2>&1; then",
        f"  curl -fsSL {shlex.quote(spec.install_package)} | bash",
        "elif command -v wget >/dev/null 2>&1; then",
        f"  wget -qO- {shlex.quote(spec.install_package)} | bash",
    ]
    if engine == "docker":
        transport_check += [
            "elif command -v apt-get >/dev/null 2>&1; then",
            "  apt-get update",
            "  apt-get install -y curl ca-certificates",
            f"  curl -fsSL {shlex.quote(spec.install_package)} | bash",
            "elif command -v apk >/dev/null 2>&1; then",
            "  apk add --no-cache bash curl",
            f"  curl -fsSL {shlex.quote(spec.install_package)} | bash",
        ]
    transport_check += [
        "else",
        '  echo "Claude native installer requires curl or wget inside the container." >&2',
        "  exit 1",
        "fi",
    ]

    return [
        f"if ! command -v {spec.command} >/dev/null 2>&1; then",
        *["  " + line if line else line for line in transport_check],
        "fi",
    ]


def docker_system_package_block() -> list[str]:
    return [
        "if ! command -v git >/dev/null 2>&1; then",
        "  if command -v apt-get >/dev/null 2>&1; then",
        "    apt-get update",
        "    apt-get install -y git curl ca-certificates bash",
        "  elif command -v apk >/dev/null 2>&1; then",
        "    apk add --no-cache git curl ca-certificates bash",
        "  elif command -v dnf >/dev/null 2>&1; then",
        "    dnf install -y git curl ca-certificates bash",
        "  elif command -v microdnf >/dev/null 2>&1; then",
        "    microdnf install -y git curl ca-certificates bash",
        "  elif command -v yum >/dev/null 2>&1; then",
        "    yum install -y git curl ca-certificates bash",
        "  else",
        '    echo "git is required inside the container, but no supported package manager was found." >&2',
        "    exit 1",
        "  fi",
        "fi",
    ]


def apptainer_system_package_block(spec: AgentSpec) -> list[str]:
    lines = [
        'missing=""',
        'for tool in bash git; do',
        '  if ! command -v "$tool" >/dev/null 2>&1; then',
        '    missing="${missing} ${tool}"',
        "  fi",
        "done",
    ]
    if spec.install_mode == "native":
        lines += [
            "if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then",
            '  missing="${missing} curl-or-wget"',
            "fi",
        ]
    lines += [
        'if [ -n "${missing}" ]; then',
        '  echo "Apptainer images are treated as immutable. Please choose an image that already includes:${missing}" >&2',
        "  exit 1",
        "fi",
    ]
    return lines


def bootstrap_script(
    engine: str,
    agent: str,
    workdir: str,
    cli_options: list[str],
    agent_args: list[str],
) -> str:
    spec = AGENT_SPECS[agent]
    launcher_parts = [spec.command, *cli_options]
    if agent == "codex":
        launcher_parts += ["-C", workdir]
    launcher_parts += agent_args
    launch_cmd = shell_quote_args(launcher_parts)
    return "\n".join(
        [
            "set -euo pipefail",
            "mkdir -p /home/agent /home/agent/.container-agent/npm-global/bin /home/agent/.container-agent/npm-cache",
            "export HOME=/home/agent",
            "export NPM_CONFIG_PREFIX=/home/agent/.container-agent/npm-global",
            "export NPM_CONFIG_CACHE=/home/agent/.container-agent/npm-cache",
            "export NPM_CONFIG_UPDATE_NOTIFIER=false",
            'export PATH="$HOME/.local/bin:$NPM_CONFIG_PREFIX/bin:$PATH"',
            *(docker_system_package_block() if engine == "docker" else apptainer_system_package_block(spec)),
            "mkdir -p /opt/agent-skills",
            f'mkdir -p "$HOME/{spec.skills_subdir}"',
            "for mount_root in /opt/agent-skills/*; do",
            '  [ -e "$mount_root" ] || continue',
            '  if [ -f "$mount_root/SKILL.md" ]; then',
            '    skill_dirs="$mount_root"',
            "  else",
            '    skill_dirs="$mount_root"/*',
            "  fi",
            '  for skill_dir in $skill_dirs; do',
            '    [ -f "$skill_dir/SKILL.md" ] || continue',
            '    target="$HOME/' + spec.skills_subdir + '/$(basename "$skill_dir")"',
            '    if [ ! -e "$target" ]; then',
            '      ln -s "$skill_dir" "$target"',
            "    fi",
            "  done",
            "done",
            *install_block(spec, engine),
            f"cd {shlex.quote(workdir)}",
            f"exec {launch_cmd}",
        ]
    )


def build_docker_command(
    image: str, mounts: list[Mount], env_vars: list[str], script: str, workdir: str
) -> list[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "-it",
        "--workdir",
        workdir,
        "-e",
        "HOME=/home/agent",
        "-e",
        "NPM_CONFIG_PREFIX=/home/agent/.container-agent/npm-global",
        "-e",
        "NPM_CONFIG_CACHE=/home/agent/.container-agent/npm-cache",
        "-e",
        "AGENT_SKILLS_DIR=/opt/agent-skills",
        "-e",
        "AGENT_AUTH_ROOT=/home/agent/.agent-auth",
    ]
    for env_var in env_vars:
        if env_var in os.environ:
            command += ["-e", env_var]
    for mount in mounts:
        command += ["-v", f"{mount.host_path}:{mount.container_path}:{mount.mode}"]
    command += [image, "bash", "-lc", script]
    return command


def build_apptainer_command(
    image: str,
    mounts: list[Mount],
    env_vars: list[str],
    script: str,
    workdir: str,
    tool_state_dir: Path,
) -> list[str]:
    apptainer_home = tool_state_dir / "apptainer-home"
    apptainer_home.mkdir(parents=True, exist_ok=True)
    command = [
        "apptainer",
        "exec",
        "--cleanenv",
        "--containall",
        "--home",
        f"{apptainer_home}:/home/agent",
        "--pwd",
        workdir,
    ]
    for mount in mounts:
        command += ["--bind", f"{mount.host_path}:{mount.container_path}:{mount.mode}"]
    env_parts = [
        "NPM_CONFIG_PREFIX=/home/agent/.container-agent/npm-global",
        "NPM_CONFIG_CACHE=/home/agent/.container-agent/npm-cache",
        "AGENT_SKILLS_DIR=/opt/agent-skills",
        "AGENT_AUTH_ROOT=/home/agent/.agent-auth",
    ]
    for env_var in env_vars:
        if env_var in os.environ:
            env_parts.append(f"{env_var}={os.environ[env_var]}")
    command += ["--env", ",".join(env_parts), image, "bash", "-lc", script]
    return command


def build_command(settings: dict[str, object]) -> tuple[list[str], list[Mount], str, str]:
    engine = choose_engine(str(settings["engine"]))
    image = str(settings["image"]) or default_image_for_engine(engine)
    mounts, workdir = build_mounts(settings)
    script = bootstrap_script(
        engine=engine,
        agent=str(settings["agent"]),
        workdir=workdir,
        cli_options=[str(value) for value in settings["cli_options"]],
        agent_args=[str(value) for value in settings["agent_args"]],
    )
    env_vars = [str(value) for value in settings["env_vars"]]
    if engine == "docker":
        command = build_docker_command(image, mounts, env_vars, script, workdir)
    else:
        command = build_apptainer_command(
            image,
            mounts,
            env_vars,
            script,
            workdir,
            Path(str(settings["tool_state_dir"])).expanduser().resolve(),
        )
    return command, mounts, engine, image


def print_summary(
    command: list[str], mounts: list[Mount], engine: str, image: str, config_path: Path
) -> None:
    print(f"config: {config_path}")
    print(f"engine: {engine}")
    print(f"image: {image}")
    print("mounts:")
    for mount in mounts:
        print(f"  - {mount.mode}: {mount.host_path} -> {mount.container_path}")
    print("command:")
    print("  " + shell_quote_args(command))


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    settings = resolve_settings(args, config_path)

    if args.write_config or not config_path.exists():
        write_config(config_path, settings)
    if args.print_config:
        sys.stdout.write(resolved_config_text(settings))
        return 0

    command, mounts, engine, image = build_command(settings)
    print_summary(command, mounts, engine, image, config_path)
    if args.dry_run:
        return 0

    completed = subprocess.run(command)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
