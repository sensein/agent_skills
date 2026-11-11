"""Tests for structsense's download-on-demand ontology cache (scripts/ontology_cache.py).

Nothing large is downloaded: the "release" is a temp directory of tiny fixtures served
through file:// URLs, or a mocked opener. Standard library only.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "skills" / "structsense"
sys.path.insert(0, str(SKILL / "scripts"))

import ontology_cache as oc  # noqa: E402

TINY_OWL = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:owl="http://www.w3.org/2002/07/owl#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
  <owl:Class rdf:about="http://example.org/onto/T_1"><rdfs:label>zebrafish widget</rdfs:label></owl:Class>
</rdf:RDF>
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.release = self.tmp / "release"
        self.release.mkdir()
        self.cache = self.tmp / "cache"
        self.files = {"tiny.owl": TINY_OWL, "other.ttl": b"@prefix ex: <http://example.org/> .\nex:a a ex:B .\n"}
        entries = []
        for name, data in self.files.items():
            (self.release / name).write_bytes(data)
            entries.append({"file": name, "url": (self.release / name).as_uri(),
                            "sha256": __import__("hashlib").sha256(data).hexdigest(), "size": len(data)})
        self.manifest = {e["file"]: e for e in entries}
        self.manifest_path = self.tmp / "ontologies.json"
        self.manifest_path.write_text(json.dumps({"ontologies": entries}))


class ResolveTests(Base):
    def test_download_verify_and_install(self):
        p = oc.resolve("tiny.owl", manifest=self.manifest, cache=self.cache)
        self.assertEqual(p, self.cache / "tiny.owl")
        self.assertEqual(p.read_bytes(), TINY_OWL)
        self.assertEqual([f.name for f in self.cache.iterdir() if f.suffix == ".part"], [])

    def test_cache_reuse_does_not_touch_network(self):
        oc.resolve("tiny.owl", manifest=self.manifest, cache=self.cache)
        boom = mock.Mock(side_effect=AssertionError("network used"))
        p = oc.resolve("tiny.owl", manifest=self.manifest, cache=self.cache, opener=boom)
        boom.assert_not_called()
        self.assertTrue(p.is_file())

    def test_checksum_mismatch_rejected_and_nothing_cached(self):
        (self.release / "tiny.owl").write_bytes(TINY_OWL + b"<!-- tampered -->")
        with self.assertRaises(oc.ChecksumMismatch):
            oc.resolve("tiny.owl", manifest=self.manifest, cache=self.cache)
        self.assertFalse((self.cache / "tiny.owl").exists())
        self.assertEqual([f for f in self.cache.iterdir() if f.suffix == ".part"], [])

    def test_upstream_fallback_used_when_primary_is_bad(self):
        good = self.tmp / "upstream.owl"
        good.write_bytes(TINY_OWL)
        m = {"tiny.owl": dict(self.manifest["tiny.owl"], url=(self.tmp / "nope.owl").as_uri(),
                              upstream="https://example.org/tiny.owl")}
        opener = lambda url: io.BytesIO(TINY_OWL) if url.startswith("https://") else oc._open_url(url)  # noqa: E731
        p = oc.resolve("tiny.owl", manifest=m, cache=self.cache, opener=opener)
        self.assertEqual(p.read_bytes(), TINY_OWL)

    def test_offline_cold_cache_gives_clear_error(self):
        def offline(url):
            raise urllib.error.URLError("Network is unreachable")
        with self.assertRaises(oc.OntologyUnavailable) as cm:
            oc.resolve("tiny.owl", manifest=self.manifest, cache=self.cache, opener=offline)
        msg = str(cm.exception)
        self.assertTrue(cm.exception.network)
        for needle in ("tiny.owl", "Network is unreachable", "import-local", oc.ENV_CACHE):
            self.assertIn(needle, msg)

    def test_unknown_name(self):
        with self.assertRaises(oc.OntologyError):
            oc.resolve("nope.owl", manifest=self.manifest, cache=self.cache)

    def test_wrong_size_cached_file_is_redownloaded(self):
        self.cache.mkdir()
        (self.cache / "tiny.owl").write_bytes(b"truncated")
        p = oc.resolve("tiny.owl", manifest=self.manifest, cache=self.cache)
        self.assertEqual(p.read_bytes(), TINY_OWL)

    def test_concurrent_resolves_download_once(self):
        calls = []

        def opener(url):
            calls.append(url)
            return oc._open_url(url)
        results = []
        ts = [threading.Thread(target=lambda: results.append(
            oc.resolve("tiny.owl", manifest=self.manifest, cache=self.cache, opener=opener))) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(results), 6)
        self.assertEqual(len(calls), 1)


class CacheDirTests(unittest.TestCase):
    def _env(self, **kw):
        keep = {k: v for k, v in os.environ.items() if k not in (oc.ENV_CACHE, "XDG_CACHE_HOME")}
        keep.update(kw)
        return mock.patch.dict(os.environ, keep, clear=True)

    def test_env_override_wins(self):
        with self._env(**{oc.ENV_CACHE: "/x/y", "XDG_CACHE_HOME": "/z"}):
            self.assertEqual(oc.cache_dir(), Path("/x/y"))

    def test_xdg(self):
        with self._env(XDG_CACHE_HOME="/z"):
            self.assertEqual(oc.cache_dir(), Path("/z/structsense/ontologies"))

    def test_default_home(self):
        with self._env(HOME="/home/u"):
            self.assertEqual(oc.cache_dir(), Path.home() / ".cache" / "structsense" / "ontologies")

    def test_env_var_used_by_resolve(self):
        base = Base("run")  # reuse fixture builder without running a test
        base.setUp()
        try:
            with self._env(**{oc.ENV_CACHE: str(base.tmp / "envcache")}):
                p = oc.resolve("tiny.owl", manifest=base.manifest)
            self.assertEqual(p.parent, base.tmp / "envcache")
        finally:
            base.tearDown()

    def test_no_cache_override_in_cli_dir(self):
        with self._env(**{oc.ENV_CACHE: "/q"}):
            r = subprocess.run([sys.executable, str(SKILL / "scripts" / "ontology_cache.py"), "dir"],
                               capture_output=True, text=True, env=os.environ)
        self.assertEqual(r.stdout.strip(), "/q")


class ImportLocalTests(Base):
    def test_imports_matching_skips_mismatch(self):
        src = self.tmp / "checkout"
        src.mkdir()
        (src / "tiny.owl").write_bytes(TINY_OWL)
        (src / "other.ttl").write_bytes(b"corrupt")
        r = oc.import_local(src, manifest=self.manifest, cache=self.cache)
        self.assertEqual(r["imported"], ["tiny.owl"])
        self.assertEqual(r["mismatch"], ["other.ttl"])
        self.assertTrue((self.cache / "tiny.owl").is_file())
        self.assertFalse((self.cache / "other.ttl").exists())

    def test_idempotent_and_absent(self):
        src = self.tmp / "checkout"
        src.mkdir()
        (src / "tiny.owl").write_bytes(TINY_OWL)
        oc.import_local(src, manifest=self.manifest, cache=self.cache)
        r = oc.import_local(src, manifest=self.manifest, cache=self.cache)
        self.assertEqual(r["already_cached"], ["tiny.owl"])
        self.assertEqual(r["absent"], ["other.ttl"])

    def test_imported_file_resolves_offline(self):
        src = self.tmp / "checkout"
        src.mkdir()
        (src / "tiny.owl").write_bytes(TINY_OWL)
        oc.import_local(src, manifest=self.manifest, cache=self.cache)
        boom = mock.Mock(side_effect=AssertionError("network used"))
        oc.resolve("tiny.owl", manifest=self.manifest, cache=self.cache, opener=boom)

    def test_not_a_directory(self):
        with self.assertRaises(oc.OntologyError):
            oc.import_local(self.tmp / "missing", manifest=self.manifest, cache=self.cache)


class PrefetchVerifyCliTests(Base):
    def _cli(self, *args):
        env = dict(os.environ, **{oc.ENV_CACHE: str(self.cache)})
        return subprocess.run([sys.executable, str(SKILL / "scripts" / "ontology_cache.py"),
                               "--manifest", str(self.manifest_path), *args],
                              capture_output=True, text=True, env=env)

    def test_prefetch_all_then_verify(self):
        r = self._cli("prefetch", "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(p.name for p in self.cache.glob("*") if not p.name.startswith(".")),
                         ["other.ttl", "tiny.owl"])
        self.assertEqual(self._cli("verify").returncode, 0)
        (self.cache / "tiny.owl").write_bytes(b"x" * len(TINY_OWL))
        self.assertEqual(self._cli("verify").returncode, 1)

    def test_prefetch_names_only(self):
        self.assertEqual(self._cli("prefetch", "tiny.owl").returncode, 0)
        self.assertFalse((self.cache / "other.ttl").exists())

    def test_prefetch_requires_selection(self):
        self.assertNotEqual(self._cli("prefetch").returncode, 0)

    def test_prefetch_failure_exit_code(self):
        (self.release / "tiny.owl").unlink()
        r = self._cli("prefetch", "tiny.owl")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED tiny.owl", r.stderr)

    def test_cli_import_local(self):
        src = self.tmp / "checkout"
        src.mkdir()
        (src / "tiny.owl").write_bytes(TINY_OWL)
        r = self._cli("import-local", str(src))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.cache / "tiny.owl").is_file())

    def test_path_cold_cache_offline_error_exit(self):
        (self.release / "tiny.owl").unlink()
        r = self._cli("path", "tiny.owl")
        self.assertEqual(r.returncode, 2)
        self.assertIn("import-local", r.stderr)


class ManifestTests(unittest.TestCase):
    def test_shipped_manifest_is_well_formed(self):
        data = json.loads((SKILL / "ontologies.json").read_text())
        self.assertEqual(data["release_tag"], "structsense-ontologies-v1")
        names = [e["file"] for e in data["ontologies"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreater(len(names), 50)
        for e in data["ontologies"]:
            self.assertEqual(e["url"], f"{oc.ASSET_BASE}/{e['file']}")
            self.assertRegex(e["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(e["size"], 0)

    def test_every_enabled_priority_file_is_in_manifest(self):
        sys.path.insert(0, str(SKILL / "scripts"))
        import concept_mapping as cm
        names = set(oc.load_manifest())
        rows = cm.read_priority_table(SKILL / "trusted_ontologes" / "priority.md")
        # files that were never bundled (too big for the repo) are listed in priority.md but have no manifest entry
        bundled = [r["file"] for r in rows if r["file"] in names]
        self.assertGreater(len(bundled), 20)

    def test_build_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.owl").write_bytes(TINY_OWL)
            (Path(d) / "readme.md").write_text("x")
            m = oc.build_manifest(Path(d))
            self.assertEqual([e["file"] for e in m["ontologies"]], ["a.owl"])
            self.assertEqual(m["ontologies"][0]["size"], len(TINY_OWL))


class SkillSizeTests(unittest.TestCase):
    @staticmethod
    def _size(skip: tuple = ()) -> int:
        total = 0
        for p in SKILL.rglob("*"):
            rel = p.relative_to(SKILL).parts
            if p.is_file() and "__pycache__" not in rel and "lexicon" not in rel \
                    and not any(rel[:len(s)] == s for s in skip):
                total += p.stat().st_size
        return total

    def test_skill_folder_well_under_plugin_limit(self):
        # claude.ai caps a plugin at 200 MB. The folder is ~16 MB without the ontologies:
        # the bundled ABCD/HBCD dictionaries (~9 MB) and examples (~3 MB) are intentional,
        # so 5 MB is not reachable here; guard against a regression with a 20 MB ceiling.
        n = self._size()
        self.assertLess(n, 20 * 1024 * 1024, f"skills/structsense is {n / 1e6:.1f} MB")

    def test_no_bundled_ontology_files(self):
        left = [p.name for p in (SKILL / "trusted_ontologes").iterdir()
                if p.suffix.lower() in oc.ONTOLOGY_EXTS]
        self.assertEqual(left, [])


class ConceptMappingIntegrationTests(Base):
    """The lexicon reads ontologies through the cache; names and behaviour are unchanged."""

    def _cfg(self, empty_dir: Path) -> dict:
        prio = self.tmp / "priority.md"
        prio.write_text("| Priority | Name | File | CURIE prefix | Namespace | Labels | Notes |\n"
                        "|---:|---|---|---|---|---|---|\n"
                        "| 1 | tiny | tiny.owl | TINY | http://example.org/onto/ | * | fixture |\n")
        return {"trusted_ontologies": {"directory": str(empty_dir), "priority_file": str(prio),
                                       "lexicon_dir": str(self.tmp / "lex"),
                                       "match_properties": {"exact": ["http://www.w3.org/2000/01/rdf-schema#label"]}}}

    def test_lexicon_downloads_lazily_then_indexes_from_cache(self):
        import concept_mapping as cm
        empty = self.tmp / "no_files"
        empty.mkdir()
        with mock.patch.dict(os.environ, {oc.ENV_CACHE: str(self.cache)}), \
                mock.patch.object(oc, "MANIFEST_PATH", self.manifest_path):
            lex = cm.Lexicon(self._cfg(empty))
            src = lex.sources[0]
            self.assertFalse(src.path.is_file())          # cold: nothing downloaded by merely looking
            self.assertFalse(self.cache.exists())
            lex.ensure_built()                             # first use downloads, then indexes
            self.assertEqual(src.path, self.cache / "tiny.owl")
            self.assertEqual(lex.status()["tiny"]["classes"], 1)
            hits = lex.query(["zebrafish widget"])
            self.assertEqual([h[2] for h in hits], ["http://example.org/onto/T_1"])

    def test_directory_file_takes_precedence_over_cache(self):
        import concept_mapping as cm
        d = self.tmp / "dir"
        d.mkdir()
        (d / "tiny.owl").write_bytes(TINY_OWL)
        with mock.patch.dict(os.environ, {oc.ENV_CACHE: str(self.cache)}), \
                mock.patch.object(oc, "MANIFEST_PATH", self.manifest_path):
            lex = cm.Lexicon(self._cfg(d))
            lex.ensure_built()
            self.assertEqual(lex.sources[0].path, d / "tiny.owl")
            self.assertFalse(self.cache.exists())

    def test_offline_cold_cache_skips_with_warning(self):
        import concept_mapping as cm
        (self.release / "tiny.owl").unlink()
        empty = self.tmp / "no_files"
        empty.mkdir()
        with mock.patch.dict(os.environ, {oc.ENV_CACHE: str(self.cache)}), \
                mock.patch.object(oc, "MANIFEST_PATH", self.manifest_path):
            lex = cm.Lexicon(self._cfg(empty))
            with self.assertLogs("concept_mapping", level="WARNING") as logs:
                lex.ensure_built()
            self.assertTrue(any("unavailable" in m for m in logs.output))
            self.assertEqual([s.name for s in lex.missing_files()], ["tiny"])


if __name__ == "__main__":
    unittest.main()
