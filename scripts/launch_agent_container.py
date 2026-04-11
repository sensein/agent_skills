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


DEFAULT_IMAGE = "node:22-bookworm-slim"
DEFAULT_APPTAINER_IMAGE = f"docker://{DEFAULT_IMAGE}"
DEFAULT_CONFIG_NAME = ".agent-container.toml"
SCRIPT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AgentSpec:
    name: str
    command: str
    install_package: str
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
        agent_state_subdir=".codex",
        skills_subdir=".codex/skills",
        default_env_vars=("OPENAI_API_KEY",),
    ),
    "claude": AgentSpec(
        name="claude",
        command="claude",
        install_package="@anthropic-ai/claude-code",
        agent_state_subdir=".claude",
        skills_subdir=".claude/skills",
        default_env_vars=("ANTHROPIC_API_KEY",),
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch a containerized Codex or Claude session with reusable config."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_NAME)
    parser.add_argument("--write-config", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--agent", choices=sorted(AGENT_SPECS), default=None)
    parser.add_argument("--engine", choices=("auto", "docker", "apptainer"), default=None)
    parser.add_argument("--image", default=None)
    parser.add_argument("--workspace-name", default=None)
    parser.add_argument("--workspace-root", default=None)
    parser.add_argument("--agent-state-dir", default=None)
    parser.add_argument("--tool-state-dir", default=None)
    parser.add_argument("--auth-path", action="append", default=None)
    parser.add_argument("--rw-dir", action="append", default=None)
    parser.add_argument("--ro-dir", action="append", default=None)
    parser.add_argument("--skill-dir", action="append", default=None)
    parser.add_argument("--env-var", action="append", default=None)
    parser.add_argument("--agent-arg", action="append", default=None)
    return parser.parse_args()


def default_agent_state_dir(agent: str) -> Path:
    return Path.home() / AGENT_SPECS[agent].agent_state_subdir


def default_tool_state_dir(config_path: Path, agent: str) -> Path:
    return config_path.parent / ".agent-container-state" / agent


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
        "agent_args": raw.get("agent_args", []),
    }


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


def resolve_settings(args: argparse.Namespace, config_path: Path) -> dict[str, object]:
    loaded = load_config(config_path)
    agent = str(config_value(args.agent, loaded.get("agent"), "codex"))
    workspace_name = str(
        config_value(args.workspace_name, loaded.get("workspace_name"), "default")
    )
    env_vars = ensure_list(
        config_value(args.env_var, loaded.get("env_vars"), list(AGENT_SPECS[agent].default_env_vars))
    )
    settings = {
        "agent": agent,
        "engine": str(config_value(args.engine, loaded.get("engine"), "auto")),
        "image": str(config_value(args.image, loaded.get("image"), "")),
        "workspace_name": workspace_name,
        "workspace_root": str(config_value(args.workspace_root, loaded.get("workspace_root"), "")),
        "agent_state_dir": str(
            config_value(
                args.agent_state_dir,
                loaded.get("agent_state_dir"),
                default_agent_state_dir(agent),
            )
        ),
        "tool_state_dir": str(
            config_value(
                args.tool_state_dir,
                loaded.get("tool_state_dir"),
                default_tool_state_dir(config_path, agent),
            )
        ),
        "auth_paths": ensure_list(config_value(args.auth_path, loaded.get("auth_paths"), [])),
        "rw_dirs": ensure_list(config_value(args.rw_dir, loaded.get("rw_dirs"), [])),
        "ro_dirs": ensure_list(config_value(args.ro_dir, loaded.get("ro_dirs"), [])),
        "skill_dirs": ensure_list(
            config_value(args.skill_dir, loaded.get("skill_dirs"), [str(path) for path in default_skill_dirs()])
        ),
        "env_vars": env_vars,
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
    content = "\n".join(
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
            f'agent_args = {format_toml_list([str(value) for value in settings["agent_args"]])}',
            "",
        ]
    )
    config_path.write_text(content, encoding="utf-8")


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


def bootstrap_script(agent: str, workdir: str, agent_args: list[str]) -> str:
    spec = AGENT_SPECS[agent]
    extra_dirs = []
    if workdir != "/workspace":
        extra_dirs.append(workdir)
    launcher_parts = [spec.command]
    if agent == "codex":
        launcher_parts += ["-C", workdir]
    launcher_parts += agent_args
    launch_cmd = shell_quote_args(launcher_parts)
    return "\n".join(
        [
            "set -euo pipefail",
            "mkdir -p /home/agent /home/agent/.container-agent/npm-global/bin",
            "export HOME=/home/agent",
            "export NPM_CONFIG_PREFIX=/home/agent/.container-agent/npm-global",
            'export PATH="$NPM_CONFIG_PREFIX/bin:$PATH"',
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
            f'if ! command -v {spec.command} >/dev/null 2>&1; then npm install -g {shlex.quote(spec.install_package)}; fi',
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
    image: str, mounts: list[Mount], env_vars: list[str], script: str, workdir: str
) -> list[str]:
    command = [
        "apptainer",
        "exec",
        "--cleanenv",
        "--containall",
        "--pwd",
        workdir,
    ]
    for mount in mounts:
        command += ["--bind", f"{mount.host_path}:{mount.container_path}:{mount.mode}"]
    env_parts = [
        "HOME=/home/agent",
        "NPM_CONFIG_PREFIX=/home/agent/.container-agent/npm-global",
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
        agent=str(settings["agent"]),
        workdir=workdir,
        agent_args=[str(value) for value in settings["agent_args"]],
    )
    env_vars = [str(value) for value in settings["env_vars"]]
    if engine == "docker":
        command = build_docker_command(image, mounts, env_vars, script, workdir)
    else:
        command = build_apptainer_command(image, mounts, env_vars, script, workdir)
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
        sys.stdout.write(config_path.read_text(encoding="utf-8"))
        return 0

    command, mounts, engine, image = build_command(settings)
    print_summary(command, mounts, engine, image, config_path)
    if args.dry_run:
        return 0

    completed = subprocess.run(command)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
