import json
import tempfile
import unittest
from pathlib import Path

from scripts.render_challenge import (
    RUNTIME_SANITIZER,
    VERSO_RUNTIME,
    VerificationError,
    artifact_manifest,
    extract_module_doc,
    merge_renderer_manifest,
    parsed_challenge_metadata,
    sanitize_bundle,
    static_html_sanitize,
    toolchain_verso_commit,
    trusted_lakefile,
)


class RenderChallengeTests(unittest.TestCase):
    def test_toolchain_mapping_is_exact(self):
        self.assertEqual(
            toolchain_verso_commit("leanprover/lean4:v4.31.0-rc2"),
            "916bb962ceb8b88e6a731db6d28e862f99e834c4",
        )
        with self.assertRaisesRegex(VerificationError, "no probed Verso"):
            toolchain_verso_commit("leanprover/lean4:v4.29.1")

    def test_every_accepted_toolchain_has_an_exact_renderer(self):
        mapping = json.loads((Path(__file__).resolve().parents[1] / "toolchains.json").read_text())
        self.assertEqual(set(mapping["lean4export"]), set(mapping["verso"]))

    def test_manifest_merge_reuses_identical_dependency(self):
        shared = {
            "name": "plausible",
            "type": "git",
            "url": "https://github.com/leanprover-community/plausible",
            "rev": "1" * 40,
            "inherited": True,
        }
        merged = merge_renderer_manifest(
            {"version": "1.2.0", "packages": [shared]},
            {"version": "1.2.0", "packages": [shared]},
            "2" * 40,
        )
        names = [package["name"] for package in merged["packages"]]
        self.assertEqual(names.count("plausible"), 1)
        self.assertIn("verso", names)

    def test_manifest_merge_rejects_dependency_substitution(self):
        source = {
            "packages": [
                {
                    "name": "shared",
                    "type": "git",
                    "url": "https://github.com/example/shared",
                    "rev": "1" * 40,
                }
            ]
        }
        verso = {
            "packages": [
                {
                    "name": "shared",
                    "type": "git",
                    "url": "https://github.com/example/shared",
                    "rev": "2" * 40,
                }
            ]
        }
        with self.assertRaisesRegex(VerificationError, "conflicts"):
            merge_renderer_manifest(source, verso, "3" * 40)

    def test_trusted_lakefile_uses_only_direct_exact_dependencies(self):
        manifest = {
            "packages": [
                {
                    "name": "direct",
                    "type": "git",
                    "url": "https://github.com/example/direct",
                    "rev": "1" * 40,
                    "inherited": False,
                },
                {
                    "name": "transitive",
                    "type": "git",
                    "url": "https://github.com/example/transitive",
                    "rev": "2" * 40,
                    "inherited": True,
                },
            ]
        }
        lakefile = trusted_lakefile(manifest, "3" * 40)
        self.assertIn('name = "direct"', lakefile)
        self.assertNotIn('name = "transitive"', lakefile)
        self.assertIn('name = "verso"', lakefile)
        self.assertIn('name = "Challenge"', lakefile)

    def test_static_html_gets_csp_and_runtime_sanitizer(self):
        html = """<!doctype html><html><head><base href="../">
<meta http-equiv="refresh" content="0;url=https://attacker.example/refresh">
<script src="marked.js"></script><script>window.safe = true;</script>
</head><body><a href="https://attacker.example/">leave</a><a href="data:text/html,bad">data</a>
<img src="javascript:bad" onerror="steal()"></body></html>"""
        sanitized = static_html_sanitize(
            html, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        self.assertIn("Content-Security-Policy", sanitized)
        self.assertIn("../palomar-sanitize.js", sanitized)
        self.assertIn("../palomar-verso.js", sanitized)
        self.assertNotIn("window.safe", sanitized)
        self.assertNotIn("marked.js", sanitized)
        self.assertNotIn("onerror", sanitized)
        self.assertNotIn("javascript:bad", sanitized)
        self.assertNotIn("attacker.example", sanitized)
        self.assertNotIn("http-equiv=\"refresh\"", sanitized.lower())
        self.assertNotIn("data:text/html", sanitized)

    def test_static_html_removes_unclosed_scripts_and_slash_separated_handlers(self):
        html = """<html><head><base href="../"></head><body>
<a href="#"/onclick="alert(1)">safe</a>
<script src="https://attacker.invalid/payload.js">
</body></html>"""
        sanitized = static_html_sanitize(
            html, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        self.assertNotIn("onclick", sanitized.lower())
        self.assertNotIn("attacker.invalid", sanitized)
        self.assertEqual(sanitized.lower().count("<script"), 2)

    def test_static_html_rewrites_unquoted_urls(self):
        html = """<html><head><base href="../"></head><body>
<a href=javascript:alert(1)>bad</a><img src=asset.png>
</body></html>"""
        sanitized = static_html_sanitize(
            html, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        self.assertIn('href="#"', sanitized)
        self.assertIn('src="../asset.png"', sanitized)
        self.assertNotIn("javascript:", sanitized)

    def test_static_html_places_csp_before_scripts(self):
        html = '<html><head><base href="../"></head><body></body></html>'
        sanitized = static_html_sanitize(
            html, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        head = sanitized[sanitized.index("<head>") : sanitized.index("</head>")]
        self.assertLess(head.index("Content-Security-Policy"), head.index("<script"))

    def test_module_doc_and_surface_metadata_are_parsed_from_lean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            challenge = root / "Challenge.lean"
            solution = root / "Solution.lean"
            comparator = root / "comparator.json"
            challenge.write_text(
                """-- /-! not a module doc -/
import Mathlib
import Batteries

/-!
# Module title

Metadata body.
-/
namespace Example
/-- Headline. -/
theorem headline : True := trivial
end Example
""",
                encoding="utf-8",
            )
            solution.write_text("import ErdosUnitDistance\n", encoding="utf-8")
            comparator.write_text(
                json.dumps(
                    {
                        "challenge_module": "Challenge",
                        "solution_module": "Solution",
                        "theorem_names": ["Example.headline"],
                        "definition_names": [],
                        "permitted_axioms": [],
                        "enable_nanoda": False,
                    }
                ),
                encoding="utf-8",
            )
            metadata = parsed_challenge_metadata(challenge, solution, comparator)
            self.assertEqual(metadata["schema_version"], 2)
            self.assertEqual(metadata["imports"], ["Batteries", "Mathlib"])
            self.assertEqual(metadata["module_doc"], "# Module title\n\nMetadata body.")
            self.assertEqual(metadata["declarations"], ["Example.headline"])
            self.assertEqual(metadata["solution_imports"], ["ErdosUnitDistance"])

    def test_module_doc_parser_skips_strings_and_nested_regular_comments(self):
        source = '''def fake := "/-! nope -/"\n/- outer /-! nested -/ -/\n/-! real doc -/'''
        self.assertEqual(extract_module_doc(source), "real doc")

    def test_static_html_binds_the_compared_declaration_and_scroll_surface(self):
        html_text = '''<!doctype html><html><head><base href="../"></head><body>
<section class="code-content"><div class="md-text"><p>Headline.</p></div>
<code class="hl lean block"><span class="const token"
data-binding="const-Example.headline" id="Example___headline">headline</span></code>
</section></body></html>'''
        sanitized = static_html_sanitize(
            html_text,
            "../palomar-sanitize.js",
            "../palomar-verso.js",
            ["Example.headline"],
        )
        self.assertIn("data-palomar-declarations=\"[&quot;Example.headline&quot;]\"", sanitized)
        self.assertIn("html { height: auto !important", sanitized)
        self.assertIn("overflow: visible !important", sanitized)
        self.assertIn("white-space: nowrap", sanitized)
        self.assertIn("background: #fff !important", sanitized)
        self.assertIn("palomar-declaration-style", sanitized)

    def test_static_html_rejects_a_missing_compared_declaration(self):
        html_text = "<!doctype html><html><head><base href=\"../\"></head><body></body></html>"
        with self.assertRaisesRegex(VerificationError, "does not contain compared declaration"):
            static_html_sanitize(
                html_text,
                "../palomar-sanitize.js",
                "../palomar-verso.js",
                ["Example.missing"],
            )

    def test_raw_svg_is_not_an_accepted_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "attack.svg").write_text("<svg><script>attack()</script></svg>")
            with self.assertRaisesRegex(VerificationError, "unexpected file type"):
                artifact_manifest(root)

    def test_bundle_is_bounded_sanitized_and_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            output = root / "output"
            (raw / "Challenge").mkdir(parents=True)
            output.mkdir()
            (raw / "Challenge" / "index.html").write_text(
                """<!doctype html><html><head><base href="../"><script src="marked.js"></script>
<script>window.safe = true;</script></head><body><p>Challenge</p></body></html>""",
                encoding="utf-8",
            )
            (raw / "marked.js").write_text("window.marked = {parse: x => x};", encoding="utf-8")
            (raw / "code.css").write_text("code { color: black; }", encoding="utf-8")
            tree_hash = sanitize_bundle(raw, output)
            manifest = json.loads((output / "artifact-manifest.json").read_text())
            self.assertEqual(manifest["artifact_tree_sha256"], tree_hash)
            self.assertEqual(artifact_manifest(output)[1], tree_hash)
            self.assertEqual(len(tree_hash), 64)
            self.assertEqual((output / "palomar-sanitize.js").read_text(), RUNTIME_SANITIZER)
            self.assertEqual((output / "palomar-verso.js").read_text(), VERSO_RUNTIME)
            metadata = json.loads((output / "challenge-metadata.json").read_text())
            self.assertEqual(metadata["declarations"], [])
            self.assertFalse((output / "marked.js").exists())

    def test_artifact_manifest_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.js"
            target.write_text("safe", encoding="utf-8")
            (root / "linked.js").symlink_to(target)
            with self.assertRaisesRegex(VerificationError, "symbolic link"):
                artifact_manifest(root)

    def test_only_root_artifact_manifest_is_excluded_from_content_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifact-manifest.json").write_text("root")
            nested = root / "nested" / "artifact-manifest.json"
            nested.parent.mkdir()
            nested.write_text("nested")
            files, _tree_hash = artifact_manifest(root)
            self.assertEqual([item["path"] for item in files], ["nested/artifact-manifest.json"])


if __name__ == "__main__":
    unittest.main()
