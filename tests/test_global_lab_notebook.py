from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "skills" / "global-lab-notebook" / "scripts" / "register_experiment.py"


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


class GlobalLabNotebookTests(unittest.TestCase):
    def test_register_experiment_creates_layout_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            exp_dir = register(temp_path / "lab", temp_path / "project", "baseline")

            self.assertTrue(exp_dir.exists())
            self.assertTrue((exp_dir / "artifacts").exists())
            self.assertTrue((exp_dir / "plan.md").exists())
            self.assertTrue((exp_dir / "results.tsv").exists())
            metadata = json.loads((exp_dir / "metadata.json").read_text())
            self.assertEqual(metadata["project_slug"], "demo-project")
            self.assertEqual(metadata["experiment_slug"], "baseline")

            index_tsv = temp_path / "lab" / "index" / "experiments.tsv"
            index_md = temp_path / "lab" / "index" / "index.md"
            self.assertTrue(index_tsv.exists())
            self.assertTrue(index_md.exists())
            self.assertIn("baseline", index_tsv.read_text())
            self.assertIn("Total experiments: 1", index_md.read_text())
            self.assertIn("Goal: Objective for baseline", (exp_dir / "plan.md").read_text())

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


if __name__ == "__main__":
    unittest.main()
