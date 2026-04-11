from __future__ import annotations

import sys
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


if __name__ == "__main__":
    unittest.main()
