from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "skills" / "labnb" / "scripts" / "register_experiment.py"
SUMMARY_SCRIPT = REPO_ROOT / "skills" / "labnb" / "scripts" / "summarize_index.py"


def register(lab_root: Path, project_root: Path, slug: str) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--lab-root",
            str(lab_root),
            "--project-root",
            str(project_root),
            "--project-slug",
            "demo-project",
            "--experiment-slug",
            slug,
            "--objective",
            f"Objective for {slug}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def register_with_workspace_root(
    lab_root: Path, project_root: Path, slug: str, workspace_root: Path
) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--lab-root",
            str(lab_root),
            "--project-root",
            str(project_root),
            "--project-slug",
            "demo-project",
            "--experiment-slug",
            slug,
            "--objective",
            f"Objective for {slug}",
            "--workspace-root",
            str(workspace_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def register_with_loop_budget(
    lab_root: Path, project_root: Path, slug: str, loop_budget: str
) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--lab-root",
            str(lab_root),
            "--project-root",
            str(project_root),
            "--project-slug",
            "demo-project",
            "--experiment-slug",
            slug,
            "--objective",
            f"Objective for {slug}",
            "--loop-budget",
            loop_budget,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def register_with_budgets(
    lab_root: Path,
    project_root: Path,
    slug: str,
    overall_budget: str,
    loop_budget: str,
) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--lab-root",
            str(lab_root),
            "--project-root",
            str(project_root),
            "--project-slug",
            "demo-project",
            "--experiment-slug",
            slug,
            "--objective",
            f"Objective for {slug}",
            "--overall-budget",
            overall_budget,
            "--loop-budget",
            loop_budget,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def register_idea(lab_root: Path, project_root: Path, slug: str) -> Path:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--lab-root",
            str(lab_root),
            "--project-root",
            str(project_root),
            "--project-slug",
            "demo-project",
            "--experiment-slug",
            slug,
            "--objective",
            f"Idea for {slug}",
            "--entry-kind",
            "idea",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


class LabNBTests(unittest.TestCase):
    def test_register_experiment_creates_layout_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            exp_dir = register(temp_path / "lab", temp_path / "project", "baseline")

            self.assertTrue(exp_dir.exists())
            self.assertTrue((exp_dir / "artifacts").exists())
            self.assertTrue((exp_dir / "plan.md").exists())
            self.assertTrue((exp_dir / "results.tsv").exists())
            metadata = json.loads((exp_dir / "metadata.json").read_text())
            self.assertEqual(metadata["entry_kind"], "experiment")
            self.assertEqual(metadata["project_slug"], "demo-project")
            self.assertEqual(metadata["entry_slug"], "baseline")
            self.assertTrue(Path(metadata["workspace_dir"]).exists())

            index_tsv = temp_path / "lab" / "index" / "experiments.tsv"
            index_md = temp_path / "lab" / "index" / "index.md"
            self.assertTrue(index_tsv.exists())
            self.assertTrue(index_md.exists())
            self.assertIn("baseline", index_tsv.read_text())
            self.assertIn("Total entries: 1", index_md.read_text())
            self.assertIn("Experiments: 1", index_md.read_text())
            self.assertIn("Goal: Objective for baseline", (exp_dir / "plan.md").read_text())
            self.assertIn("Workspace dir:", (exp_dir / "plan.md").read_text())
            self.assertIn("Overall budget: TBD", (exp_dir / "plan.md").read_text())
            self.assertIn("Loop budget: TBD", (exp_dir / "plan.md").read_text())
            self.assertIn("## Feasibility And First Slice", (exp_dir / "plan.md").read_text())
            self.assertIn("## Existing Context Summary", (exp_dir / "plan.md").read_text())
            self.assertIn("Smallest useful iteration:", (exp_dir / "plan.md").read_text())
            self.assertIn(
                "Parallel or downstream work outside this budget:",
                (exp_dir / "plan.md").read_text(),
            )

    def test_register_idea_creates_idea_directory_and_index_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            idea_dir = register_idea(temp_path / "lab", temp_path / "project", "future-run")

            self.assertTrue(idea_dir.exists())
            self.assertTrue((idea_dir / "idea.md").exists())
            self.assertFalse((idea_dir / "results.tsv").exists())
            metadata = json.loads((idea_dir / "metadata.json").read_text())
            self.assertEqual(metadata["entry_kind"], "idea")
            self.assertEqual(metadata["status"], "planned")
            self.assertEqual(metadata["workspace_dir"], "")

            index_md = (temp_path / "lab" / "index" / "index.md").read_text()
            self.assertIn("Ideas: 1", index_md)
            self.assertIn("future-run", index_md)

    def test_register_experiment_records_budgets_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            exp_dir = register_with_budgets(
                temp_path / "lab",
                temp_path / "project",
                "budgeted",
                "6 hours total",
                "2 hours",
            )

            metadata = json.loads((exp_dir / "metadata.json").read_text())
            self.assertEqual(metadata["overall_budget"], "6 hours total")
            self.assertEqual(metadata["loop_budget"], "2 hours")
            self.assertIn("Overall budget: 6 hours total", (exp_dir / "plan.md").read_text())
            self.assertIn("Loop budget: 2 hours", (exp_dir / "plan.md").read_text())

    def test_register_experiment_can_link_workspace_to_external_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            exp_dir = register_with_workspace_root(
                temp_path / "lab",
                temp_path / "project",
                "baseline",
                temp_path / "external-workspaces",
            )

            metadata = json.loads((exp_dir / "metadata.json").read_text())
            workspace_dir = Path(metadata["workspace_dir"])
            workspace_link = Path(metadata["workspace_link"])

            self.assertTrue(workspace_dir.exists())
            self.assertTrue(workspace_link.is_symlink())
            self.assertEqual(workspace_link.resolve(), workspace_dir)
            self.assertEqual(
                workspace_dir.parent.resolve(),
                (temp_path / "external-workspaces").resolve(),
            )

    def test_render_index_skips_malformed_rows(self) -> None:
        module_globals: dict[str, object] = {}
        exec(SCRIPT.read_text(encoding="utf-8"), module_globals)
        render_index = module_globals["render_index"]

        rendered = render_index(  # type: ignore[operator]
            [["too-short"], ["id", "time", "project", "exp", "goal", "/tmp/path", "/tmp/proj", ""]]
        )

        self.assertIn("Total entries: 1", rendered)
        self.assertIn("`id`", rendered)
        self.assertNotIn("too-short", rendered)

    def test_parallel_registrations_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            lab_root = temp_path / "lab"
            project_root = temp_path / "project"

            with ThreadPoolExecutor(max_workers=6) as executor:
                futures = [
                    executor.submit(register, lab_root, project_root, f"exp-{index}")
                    for index in range(6)
                ]
            exp_dirs = [future.result() for future in futures]

            self.assertEqual(len(exp_dirs), 6)
            self.assertEqual(len({path.name for path in exp_dirs}), 6)

            index_rows = (
                (lab_root / "index" / "experiments.tsv").read_text().strip().splitlines()
            )
            self.assertEqual(len(index_rows), 7)
            for exp_dir in exp_dirs:
                self.assertTrue(exp_dir.exists())
            workspace_dirs = [
                json.loads((exp_dir / "metadata.json").read_text())["workspace_dir"]
                for exp_dir in exp_dirs
            ]
            self.assertEqual(len(workspace_dirs), len(set(workspace_dirs)))

    def test_registration_sanitizes_all_tsv_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--lab-root",
                    str(temp_path / "lab"),
                    "--project-root",
                    str(temp_path / "project\troot"),
                    "--project-slug",
                    "demo\tproject",
                    "--experiment-slug",
                    "baseline\nrun",
                    "--objective",
                    "Objective\twith\nbreaks",
                    "--parent-id",
                    "parent\tid",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            lines = (temp_path / "lab" / "index" / "experiments.tsv").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[1].count("\t"), 9)
            self.assertNotIn("\n", lines[1])
            self.assertIn("demo project", lines[1])
            self.assertIn("baseline run", lines[1])
            self.assertIn("Objective with breaks", lines[1])

    def test_summary_reports_existing_ideas_and_experiments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            lab_root = temp_path / "lab"
            project_root = temp_path / "project"
            register(lab_root, project_root, "baseline")
            register_idea(lab_root, project_root, "future-run")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SUMMARY_SCRIPT),
                    "--lab-root",
                    str(lab_root),
                    "--project-slug",
                    "demo-project",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("Matches: 2", result.stdout)
            self.assertIn("Ideas: 1", result.stdout)
            self.assertIn("Experiments: 1", result.stdout)
            self.assertIn("future-run", result.stdout)
            self.assertIn("baseline", result.stdout)


if __name__ == "__main__":
    unittest.main()
