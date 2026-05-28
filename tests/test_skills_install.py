from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / "skills"


def _load(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


install_skills = _load("install_skills", "scripts/install_skills.py")
summarize_run = _load("summarize_run", "skills/duct/scripts/summarize_run.py")


class FlatStructureTests(unittest.TestCase):
    def test_no_skill_contains_a_nested_skill(self) -> None:
        for skill_dir in install_skills.discover_skills():
            self.assertEqual(
                install_skills.find_nested_skills(skill_dir),
                [],
                f"{skill_dir.name} must not contain nested SKILL.md files",
            )

    def test_duct_skill_is_present_for_claude_and_codex(self) -> None:
        duct = SKILLS_ROOT / "duct"
        self.assertTrue((duct / "SKILL.md").exists())
        self.assertTrue((duct / "agents" / "openai.yaml").exists())


class InstallSkillsTests(unittest.TestCase):
    def test_discover_finds_known_skills(self) -> None:
        names = {p.name for p in install_skills.discover_skills()}
        self.assertIn("duct", names)
        self.assertIn("labnb", names)

    def test_install_copies_selected_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dest = Path(temp_dir) / "skills"
            code = install_skills.main(["--dest", str(dest), "--skills", "duct"])
            self.assertEqual(code, 0)
            self.assertTrue((dest / "duct" / "SKILL.md").exists())
            self.assertFalse((dest / "labnb").exists())

    def test_install_rejects_nested_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            parent = root / "skills" / "parent"
            (parent / "sub").mkdir(parents=True)
            (parent / "SKILL.md").write_text("---\nname: parent\n---\n")
            (parent / "sub" / "SKILL.md").write_text("---\nname: sub\n---\n")
            nested = install_skills.find_nested_skills(parent)
            self.assertEqual([p.name for p in nested], ["SKILL.md"])


class SummarizeRunTests(unittest.TestCase):
    def _write_info(self, directory: Path, summary: dict) -> Path:
        info_path = directory / "run_info.json"
        info_path.write_text(json.dumps({"execution_summary": summary}))
        return info_path

    def test_summary_extracts_headline_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            info_path = self._write_info(
                Path(temp_dir),
                {
                    "command": "pytest -q",
                    "exit_code": 0,
                    "wall_clock_time": 1.5,
                    "peak_rss": 24 * 1024 * 1024,
                    "average_rss": 22 * 1024 * 1024,
                    "peak_pcpu": 90.0,
                    "average_pcpu": 45.0,
                    "peak_pmem": 0.1,
                    "num_samples": 15,
                    "working_directory": temp_dir,
                },
            )
            info = json.loads(info_path.read_text())
            summary = summarize_run.build_summary(info)
            self.assertEqual(summary["exit_code"], 0)
            self.assertEqual(summary["num_samples"], 15)
            rendered = summarize_run.render(summary, info_path)
            self.assertIn("24.0 MiB", rendered)
            self.assertIn("90.0%", rendered)

    def test_resolve_info_path_from_directory_and_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            info_path = self._write_info(directory, {"exit_code": 0, "num_samples": 1})
            self.assertEqual(
                summarize_run.resolve_info_path(directory), info_path
            )
            self.assertEqual(
                summarize_run.resolve_info_path(directory / "run_"), info_path
            )

    def test_no_samples_adds_note(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            info_path = self._write_info(
                Path(temp_dir), {"exit_code": 0, "num_samples": 0}
            )
            info = json.loads(info_path.read_text())
            rendered = summarize_run.render(
                summarize_run.build_summary(info), info_path
            )
            self.assertIn("no resource samples", rendered)


if __name__ == "__main__":
    unittest.main()
