from __future__ import annotations

import sys
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "launch_agent_container.py"

SPEC = importlib.util.spec_from_file_location("launch_agent_container", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class LaunchAgentContainerTests(unittest.TestCase):
    def test_choose_engine_prefers_apptainer_on_linux(self) -> None:
        with mock.patch.object(MODULE.sys, "platform", "linux"), mock.patch.object(
            MODULE.shutil,
            "which",
            side_effect=lambda name: f"/usr/bin/{name}" if name in {"apptainer", "docker"} else None,
        ):
            self.assertEqual(MODULE.choose_engine("auto"), "apptainer")

    def test_choose_engine_prefers_docker_on_macos(self) -> None:
        with mock.patch.object(MODULE.sys, "platform", "darwin"), mock.patch.object(
            MODULE.shutil,
            "which",
            side_effect=lambda name: f"/usr/bin/{name}" if name == "docker" else None,
        ):
            self.assertEqual(MODULE.choose_engine("auto"), "docker")

    def test_write_and_load_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / ".agent-container.toml"
            settings = {
                "agent": "codex",
                "engine": "auto",
                "image": "",
                "workspace_name": "demo",
                "workspace_root": "",
                "agent_state_dir": str(Path(temp_dir) / ".codex"),
                "tool_state_dir": str(Path(temp_dir) / ".state"),
                "auth_paths": [str(Path(temp_dir) / ".config" / "codex-auth")],
                "rw_dirs": [str(Path(temp_dir) / "repo")],
                "ro_dirs": [str(Path(temp_dir) / "reference")],
                "skill_dirs": [str(REPO_ROOT / "skills")],
                "env_vars": ["OPENAI_API_KEY"],
                "agent_args": ["--full-auto"],
            }
            MODULE.write_config(config_path, settings)
            loaded = MODULE.load_config(config_path)
            self.assertEqual(loaded["agent"], "codex")
            self.assertEqual(loaded["workspace_name"], "demo")
            self.assertEqual(loaded["agent_args"], ["--full-auto"])
            self.assertEqual(
                loaded["auth_paths"], [str(Path(temp_dir) / ".config" / "codex-auth")]
            )

    def test_agent_override_recomputes_agent_specific_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / ".agent-container.toml"
            MODULE.write_config(
                config_path,
                {
                    "agent": "codex",
                    "engine": "auto",
                    "image": "",
                    "workspace_name": "default",
                    "workspace_root": "",
                    "agent_state_dir": str(temp_path / ".codex"),
                    "tool_state_dir": str(temp_path / ".agent-container-state" / "codex"),
                    "auth_paths": [],
                    "rw_dirs": [],
                    "ro_dirs": [],
                    "skill_dirs": [str(REPO_ROOT / "skills")],
                    "env_vars": ["OPENAI_API_KEY"],
                    "agent_args": [],
                },
            )
            args = MODULE.parse_args.__wrapped__(["--config", str(config_path), "--agent", "claude"]) if hasattr(MODULE.parse_args, "__wrapped__") else None
            if args is None:
                import argparse as _argparse
                args = _argparse.Namespace(
                    config=str(config_path),
                    write_config=False,
                    print_config=False,
                    dry_run=False,
                    agent="claude",
                    engine=None,
                    image=None,
                    workspace_name=None,
                    workspace_root=None,
                    agent_state_dir=None,
                    tool_state_dir=None,
                    auth_path=None,
                    rw_dir=None,
                    ro_dir=None,
                    skill_dir=None,
                    env_var=None,
                    agent_arg=None,
                )
            settings = MODULE.resolve_settings(args, config_path)
            self.assertEqual(settings["agent"], "claude")
            self.assertEqual(settings["env_vars"], ["ANTHROPIC_API_KEY"])
            self.assertTrue(str(settings["agent_state_dir"]).endswith(".claude"))
            self.assertTrue(str(settings["tool_state_dir"]).endswith("/claude"))

    def test_claude_default_auth_path_includes_claude_json_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / ".claude.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(MODULE.Path, "home", return_value=temp_path):
                self.assertEqual(
                    MODULE.default_auth_paths("claude"),
                    [temp_path / ".claude.json"],
                )

    def test_print_config_outputs_resolved_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            config_path = temp_path / ".agent-container.toml"
            MODULE.write_config(
                config_path,
                {
                    "agent": "codex",
                    "engine": "auto",
                    "image": "",
                    "workspace_name": "default",
                    "workspace_root": "",
                    "agent_state_dir": str(temp_path / ".codex"),
                    "tool_state_dir": str(temp_path / ".agent-container-state" / "codex"),
                    "auth_paths": [],
                    "rw_dirs": [],
                    "ro_dirs": [],
                    "skill_dirs": [str(REPO_ROOT / "skills")],
                    "env_vars": ["OPENAI_API_KEY"],
                    "agent_args": [],
                },
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--config", str(config_path), "--print-config", "--agent", "claude"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn('agent = "claude"', result.stdout)
            self.assertIn('env_vars = ["ANTHROPIC_API_KEY"]', result.stdout)
            self.assertIn('.claude', result.stdout)

    def test_build_mounts_avoids_home_directory_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            (temp_path / "repo").mkdir()
            settings = {
                "agent": "codex",
                "rw_dirs": [str(temp_path / "repo")],
                "ro_dirs": [],
                "skill_dirs": [],
                "auth_paths": [],
                "agent_state_dir": str(temp_path / ".codex"),
                "tool_state_dir": str(temp_path / ".tool-state"),
            }
            mounts, workdir = MODULE.build_mounts(settings)
            self.assertEqual(workdir, "/workspace/task")
            self.assertTrue(any(mount.container_path == "/workspace/task" for mount in mounts))
            self.assertFalse(any(mount.host_path == Path.home().resolve() for mount in mounts))

    def test_build_mounts_maps_extra_auth_paths_inside_container(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            auth_dir = temp_path / ".config" / "claude"
            auth_dir.mkdir(parents=True)
            settings = {
                "agent": "claude",
                "rw_dirs": [],
                "ro_dirs": [],
                "skill_dirs": [],
                "auth_paths": [str(auth_dir)],
                "agent_state_dir": str(temp_path / ".claude"),
                "tool_state_dir": str(temp_path / ".tool-state"),
            }
            with mock.patch.object(MODULE.Path, "home", return_value=temp_path):
                mounts, _ = MODULE.build_mounts(settings)
            self.assertTrue(
                any(
                    mount.host_path == auth_dir.resolve()
                    and mount.container_path == "/home/agent/.config/claude"
                    for mount in mounts
                )
            )

    def test_build_command_uses_default_skill_repo_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_dir = temp_path / "repo"
            repo_dir.mkdir()
            settings = {
                "agent": "codex",
                "engine": "docker",
                "image": "",
                "workspace_name": "default",
                "workspace_root": "",
                "agent_state_dir": str(temp_path / ".codex"),
                "tool_state_dir": str(temp_path / ".tool-state"),
                "auth_paths": [],
                "rw_dirs": [str(repo_dir)],
                "ro_dirs": [],
                "skill_dirs": [str(REPO_ROOT / "skills")],
                "env_vars": [],
                "agent_args": ["--full-auto"],
            }
            with mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/docker"):
                command, mounts, engine, image = MODULE.build_command(settings)
            self.assertEqual(engine, "docker")
            self.assertEqual(image, MODULE.DEFAULT_IMAGE)
            self.assertTrue(any(mount.container_path.startswith("/opt/agent-skills/") for mount in mounts))
            self.assertIn("docker", command[0])

    def test_build_apptainer_command_uses_dedicated_home_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            repo_dir = temp_path / "repo"
            repo_dir.mkdir()
            settings = {
                "agent": "claude",
                "engine": "apptainer",
                "image": "",
                "workspace_name": "default",
                "workspace_root": "",
                "agent_state_dir": str(temp_path / ".claude"),
                "tool_state_dir": str(temp_path / ".tool-state"),
                "auth_paths": [],
                "rw_dirs": [str(repo_dir)],
                "ro_dirs": [],
                "skill_dirs": [str(REPO_ROOT / "skills")],
                "env_vars": [],
                "agent_args": ["auth", "status"],
            }
            with mock.patch.object(MODULE.shutil, "which", return_value="/usr/bin/apptainer"):
                command, _, engine, image = MODULE.build_command(settings)
            self.assertEqual(engine, "apptainer")
            self.assertEqual(image, MODULE.DEFAULT_APPTAINER_IMAGE)
            self.assertIn("--home", command)
            self.assertTrue(
                any(
                    "NPM_CONFIG_CACHE=/home/agent/.container-agent/npm-cache" in part
                    for part in command
                )
            )
            self.assertNotIn("HOME=/home/agent", command)

    def test_codex_bootstrap_updates_npm_before_install(self) -> None:
        script = MODULE.bootstrap_script("codex", "/workspace/task", ["login", "status"])
        self.assertIn("npm install -g npm@latest", script)
        self.assertIn("npm install -g @openai/codex", script)

    def test_help_includes_key_defaults(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "-h"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("container.toml", result.stdout)
        self.assertIn(MODULE.DEFAULT_IMAGE, result.stdout)
        self.assertIn("'codex'.", result.stdout)


if __name__ == "__main__":
    unittest.main()
