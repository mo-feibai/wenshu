"""Unit tests for the isolated build CLI; no npm or real document data."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from tests.baseline import build_site
from tests.baseline.tempdirs import TemporaryDirectory


class BuildSiteTests(unittest.TestCase):
    def test_existing_report_is_preserved_and_blocks_before_external_calls(self):
        target = self.root / "existing manual report.json"
        target.write_text("KEEP synthetic manual content", encoding="utf-8")
        calls = []

        def execute(argv, *, cwd, env):
            calls.append(list(argv))
            return self.successful_executor(argv, cwd=cwd, env=env)

        report = build_site.run_validation(repo=self.repo, node=self.node, npm_cli=self.npm_cli,
                                           report_path=target, executor=execute)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(target.read_text(encoding="utf-8"), "KEEP synthetic manual content")
        self.assertTrue(any(f["stage"] == "report" for f in report["failures"]))
        self.assertEqual(calls, [], "Invalid reports must block before runtime/install/build")

    def test_non_temporary_report_is_rejected_without_creating_files(self):
        areas = {name: self.root / name for name in ("system temp", "runner temp", "test temp")}
        for path in areas.values():
            path.mkdir()
        target = self.root / "not temporary" / "manual.json"
        calls = []

        def execute(argv, *, cwd, env):
            calls.append(list(argv))
            return self.successful_executor(argv, cwd=cwd, env=env)

        with patch("tempfile.gettempdir", return_value=str(areas["system temp"])):
            with patch.dict(os.environ, {"RUNNER_TEMP": str(areas["runner temp"]),
                                         "WENSHU_TEST_TMPDIR": str(areas["test temp"])}):
                report = build_site.run_validation(repo=self.repo, node=self.node, npm_cli=self.npm_cli,
                                                   report_path=target, executor=execute)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(target.parent.exists(), "No arbitrary out-of-repo directory may be created")
        self.assertTrue(any(f["stage"] == "report" for f in report["failures"]))
        self.assertEqual(calls, [])

    def setUp(self):
        self.temp = TemporaryDirectory(
            prefix="wenshu-build-unit-", dir=os.environ.get("WENSHU_TEST_TMPDIR")
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source repository"
        self.app = self.repo / "custom"
        source_files = {
            "package.json": json.dumps({"scripts": {"build": "python3 scripts/sync_files.py && python3 scripts/gen_site_content.py && astro build && pagefind --site dist", "build:win": "python scripts/sync_files.py && python scripts/gen_site_content.py && astro build && pagefind --site dist"}}),
            "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {"node_modules/astro": {"engines": {"node": ">=22.12.0", "npm": ">=9.6.5"}}}}),
            "astro.config.mjs": "export default {};\n",
            "scripts/sync_files.py": "# synthetic producer\n",
            "scripts/gen_site_content.py": "# synthetic producer\n",
            "src/content.config.ts": "export const collections = {};\n",
            "src/pages/index.astro": "<h1>Synthetic source</h1>\n",
            "src/styles/global.css": "/* existing custom modification */\n",
            "public/favicon.svg": "<svg/>\n",
            "public/logo.svg": "<svg/>\n",
            "public/swagger/index.html": "<html>Synthetic Swagger</html>\n",
            "public/vendor/swagger-ui/swagger-ui.css": "/* synthetic vendor */\n",
            "public/vendor/swagger-ui/swagger-ui-bundle.js": "/* synthetic vendor */\n",
            "public/fonts/MapleMono-CN-Regular.woff2": "synthetic-font\n",
            "public/fonts/MapleMono-CN-SemiBold.woff2": "synthetic-font\n",
            "public/fonts/MapleMono-CN-Bold.woff2": "synthetic-font\n",
            "server_config.json": "PRIVATE-CANARY",
            "sessions.json": "PRIVATE-CANARY",
            "shares.json": "PRIVATE-CANARY",
            ".npmrc": "PRIVATE-CANARY",
            "scripts/file_keys.json": "PRIVATE-CANARY",
            "src/data/assets.json": "PRIVATE-CANARY",
            "src/content/docs/private/document.md": "PRIVATE-CANARY",
            "public/files/private.sql": "PRIVATE-CANARY",
            "public/openapi/spec.yml": "PRIVATE-CANARY",
            "node_modules/private.txt": "PRIVATE-CANARY",
            "dist/private.html": "PRIVATE-CANARY",
            ".astro/private.json": "PRIVATE-CANARY",
            "logs/private.log": "PRIVATE-CANARY",
            "backups/private.sql": "PRIVATE-CANARY",
        }
        source_files["public/swagger/index.html"] = (
            '<link rel="stylesheet" href="/vendor/swagger-ui/swagger-ui.css">'
            '<style>/* /fonts/MapleMono-CN-Regular.woff2 '
            '/fonts/MapleMono-CN-SemiBold.woff2 /fonts/MapleMono-CN-Bold.woff2 */</style>'
            '<script src="/vendor/swagger-ui/swagger-ui-bundle.js"></script>'
            '<script>const syntheticSpec = "/openapi/spec.yml";</script>'
        )
        for relative, content in source_files.items():
            path = self.app / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.node = self.root / "runtime with spaces" / "node.exe"
        self.node.parent.mkdir(parents=True)
        self.node.write_text("fake runtime", encoding="utf-8")
        self.npm_cli = self.node.parent / "node_modules/npm/bin/npm-cli.js"
        self.npm_cli.parent.mkdir(parents=True)
        self.npm_cli.write_text("fake npm cli", encoding="utf-8")

    def successful_executor(self, argv, *, cwd, env):
        if "-e" in argv:
            Path(env["WENSHU_BUILD_RUNTIME_REPORT"]).write_text(
                '{"node":"v24.11.0","npm":"11.6.1"}', encoding="utf-8"
            )
        elif "run" in argv:
            shutil.copytree(
                Path(__file__).parent / "fixtures/build-artifacts", Path(cwd) / "dist"
            )
            shutil.copytree(Path(cwd) / "public", Path(cwd) / "dist", dirs_exist_ok=True)
            fixture_root = Path(__file__).parent / "fixtures/build"
            # Literal external-output examples: deliberately independent of the
            # harness expectations file and of both production producer parsers.
            downloads = (
                ("docs/nebula-test/api/dispatch-guide-v1.md", "files/dispatch-guide-v1.md"),
                ("docs/nebula-test/api/dispatch-guide-v2.md", "files/dispatch-guide-v2.md"),
                ("docs/nebula-test/api/dispatch-openapi.md", "files/api__dispatch-openapi.md"),
                ("docs/nebula-test/api/dispatch-openapi.yml", "files/api__dispatch-openapi.yml"),
                ("docs/nebula-test/sql/migration-ledger.sql", "files/migration-ledger.sql"),
                ("docs/nebula-test/assets/dispatch-diagram.svg", "files/dispatch-diagram.svg"),
                ("public/openapi/spec.yml", "openapi/spec.yml"),
            )
            for source, target in downloads:
                path = Path(cwd) / "dist" / target
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(fixture_root / source, path)
        return 0

    def test_successful_pipeline_checks_rendered_pages_and_cleans_owned_tree(self):
        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli,
            executor=self.successful_executor,
        )
        self.assertEqual(report["status"], "passed", report["failures"])
        rendered = [check for check in report["checks"] if check["name"].startswith("html:")]
        self.assertEqual(len(rendered), 8)
        self.assertTrue(all(check["status"] == "passed" for check in rendered))
        self.assertTrue(report["temporary_isolation"]["cleaned"])
        self.assertFalse(Path(report["temporary_isolation"]["root"]).exists())

    def _run_real_fixture_producers(self):
        root = self.root / "real fixture producers"
        app, docs = root / "app", root / "docs"
        scripts = app / "scripts"
        scripts.mkdir(parents=True)
        before = build_site._source_state(build_site.REPO / "custom")
        for name in ("sync_files.py", "gen_site_content.py"):
            shutil.copyfile(build_site.REPO / "custom" / "scripts" / name, scripts / name)
        build_site._copy_fixture_tree(build_site.FIXTURES / "docs", docs)
        env = build_site._child_environment(root, app, docs, self.node)
        self.assertEqual(env["WENSHU_DOCS"], str(docs))
        self.assertEqual(env["WENSHU_APP"], str(app))
        for name in ("sync_files.py", "gen_site_content.py"):
            log = root / (name + ".log")
            with log.open("w", encoding="utf-8") as handle:
                code = subprocess.run([sys.executable, "-B", str(scripts / name)],
                                      cwd=app, env=env, stdout=handle,
                                      stderr=subprocess.STDOUT, check=False).returncode
            self.assertEqual(code, 0, log.read_text(encoding="utf-8"))
        self.assertEqual(before, build_site._source_state(build_site.REPO / "custom"))
        return app

    def test_real_fixture_producers_preserve_explicit_openapi_attachment_urls(self):
        app = self._run_real_fixture_producers()
        for name in ("dispatch-guide-v2", "dispatch-openapi"):
            with self.subTest(document=name):
                text = (app / "src/content/docs/nebula-test/api" / (name + ".md")).read_text(encoding="utf-8")
                self.assertIn('  - "/files/api__dispatch-openapi.yml"', text)
                self.assertNotIn('  - "nebula-test/api/dispatch-openapi.yml"', text)
        guide = (app / "src/content/docs/nebula-test/api/dispatch-guide-v2.md").read_text(encoding="utf-8")
        self.assertIn('  - "/files/dispatch-diagram.svg"', guide)
        self.assertIn('  - "nebula-test/api/dispatch-openapi"', guide)
        self.assertEqual((app / "public/files/api__dispatch-openapi.yml").read_bytes(),
                         (build_site.FIXTURES / "docs/nebula-test/api/dispatch-openapi.yml").read_bytes())

    def test_real_sql_projection_and_independent_index_pin_legacy_empty_summary(self):
        declared = json.loads((build_site.FIXTURES / "expectations.json").read_text(encoding="utf-8"))
        example = json.loads((Path(__file__).parent / "fixtures/build-artifacts/search-index.json").read_text(encoding="utf-8"))
        sql_id = "nebula-test/sql/migration-ledger"
        self.assertEqual(next(row for row in declared["search_entries"] if row["id"] == sql_id)["summary"], "")
        self.assertEqual(next(row for row in example if row["id"] == sql_id)["summary"], "")
        app = self._run_real_fixture_producers()
        generated = (app / "src/content/docs/nebula-test/sql/migration-ledger.md").read_text(encoding="utf-8")
        self.assertTrue(generated.startswith("---\n"))
        self.assertNotIn("\nsummary:", generated.split("---", 2)[1])
        raw = app / "public/files/migration-ledger.sql"
        fixture = build_site.FIXTURES / "docs/nebula-test/sql/migration-ledger.sql"
        self.assertEqual(raw.read_bytes(), fixture.read_bytes())
        self.assertIn("-- summary: A harmless SQL download for a fictional ledger.", raw.read_text(encoding="utf-8"))

    def test_missing_spec_anchor_fails_with_declared_download_bytes_intact(self):
        def executor(argv, *, cwd, env):
            code = self.successful_executor(argv, cwd=cwd, env=env)
            if "run" in argv:
                page = Path(cwd) / "dist/docs/nebula-test/api/dispatch-guide-v2/index.html"
                text = page.read_text(encoding="utf-8")
                self.assertIn('href="/files/api__dispatch-openapi.yml"', text)
                page.write_text(text.replace('href="/files/api__dispatch-openapi.yml"',
                                             'href="/files/not-the-declared-spec.yml"'), encoding="utf-8")
            return code

        report = build_site.run_validation(repo=self.repo, node=self.node, npm_cli=self.npm_cli, executor=executor)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(f.get("check") == "html:docs/nebula-test/api/dispatch-guide-v2/index.html" for f in report["failures"]))
        self.assertTrue(any(c["name"] == "download:files/api__dispatch-openapi.yml" and c["status"] == "passed" for c in report["checks"]))
        self.assertTrue(report["temporary_isolation"]["cleaned"])

    def test_changed_sql_search_summary_fails_with_html_and_downloads_intact(self):
        def executor(argv, *, cwd, env):
            code = self.successful_executor(argv, cwd=cwd, env=env)
            if "run" in argv:
                target = Path(cwd) / "dist/search-index.json"
                items = json.loads(target.read_text(encoding="utf-8"))
                row = next(item for item in items if item["id"] == "nebula-test/sql/migration-ledger")
                self.assertEqual(row["summary"], "")
                row["summary"] = "UNDECLARED synthetic SQL summary"
                target.write_text(json.dumps(items), encoding="utf-8")
            return code

        report = build_site.run_validation(repo=self.repo, node=self.node, npm_cli=self.npm_cli, executor=executor)
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(f.get("check") == "search-index" for f in report["failures"]))
        self.assertTrue(all(c["status"] == "passed" for c in report["checks"] if c["name"].startswith("html:")))
        self.assertTrue(any(c["name"] == "download:files/migration-ledger.sql" and c["status"] == "passed" for c in report["checks"]))
        self.assertTrue(report["temporary_isolation"]["cleaned"])

    def test_changed_raw_sql_download_fails_without_weakening_html_checks(self):
        def executor(argv, *, cwd, env):
            code = self.successful_executor(argv, cwd=cwd, env=env)
            if "run" in argv:
                (Path(cwd) / "dist/files/migration-ledger.sql").write_text(
                    "NOT THE DECLARED SYNTHETIC SQL", encoding="utf-8"
                )
            return code

        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli, executor=executor,
        )
        self.assertEqual(report["status"], "failed")
        failure = next(item for item in report["failures"] if item.get("check") == "download:files/migration-ledger.sql")
        self.assertIn("raw fixture bytes", failure["message"])
        self.assertTrue(all(check["status"] == "passed" for check in report["checks"] if check["name"].startswith("html:")))
        self.assertTrue(report["temporary_isolation"]["cleaned"])

    def test_missing_pagefind_index_is_failure_with_html_and_downloads_intact(self):
        def executor(argv, *, cwd, env):
            code = self.successful_executor(argv, cwd=cwd, env=env)
            if "run" in argv:
                (Path(cwd) / "dist/pagefind/index/fixture.pf_index").unlink()
            return code

        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli, executor=executor,
        )
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(item.get("check") == "pagefind-index" for item in report["failures"]))
        self.assertIn("not browser search", report["boundary"])
        self.assertTrue(report["temporary_isolation"]["cleaned"])

    def test_missing_local_swagger_vendor_is_a_smoke_failure(self):
        def executor(argv, *, cwd, env):
            code = self.successful_executor(argv, cwd=cwd, env=env)
            if "run" in argv:
                (Path(cwd) / "dist/vendor/swagger-ui/swagger-ui-bundle.js").unlink()
            return code

        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli, executor=executor,
        )
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(item.get("check") == "static:vendor/swagger-ui/swagger-ui-bundle.js" for item in report["failures"]))
        self.assertTrue(report["temporary_isolation"]["cleaned"])

    def test_install_failure_is_reported_and_only_temporary_app_is_used(self):
        observed_roots = []

        def executor(argv, *, cwd, env):
            cwd = Path(cwd)
            isolated = cwd.parent
            observed_roots.append(isolated)
            self.assertNotEqual(cwd, self.app)
            self.assertEqual(env["WENSHU_APP"], str(cwd))
            self.assertTrue(Path(env["WENSHU_DOCS"]).is_relative_to(isolated))
            self.assertFalse((cwd / "server_config.json").exists())
            self.assertFalse((cwd / "scripts/file_keys.json").exists())
            self.assertFalse((cwd / "src/content/docs/private/document.md").exists())
            self.assertFalse((cwd / "node_modules/private.txt").exists())
            if "ci" in argv:
                return 17
            Path(env["WENSHU_BUILD_RUNTIME_REPORT"]).write_text(
                json.dumps({"node": "v24.11.0", "npm": "11.6.1"}), encoding="utf-8"
            )
            return 0

        report_path = self.root / "report.json"
        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli,
            report_path=report_path, executor=executor,
        )
        self.assertEqual(report["status"], "failed")
        self.assertIn("17", report["failures"][0]["message"])
        self.assertEqual(report["source_unchanged"]["status"], "passed")
        self.assertTrue(report["temporary_isolation"]["cleaned"])
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)
        self.assertTrue(observed_roots)
        self.assertTrue(all(not root.exists() for root in observed_roots))
        self.assertEqual((self.app / "src/styles/global.css").read_text(encoding="utf-8"), "/* existing custom modification */\n")

    def test_explicit_scratch_inside_test_namespace_supports_report_and_cleanup(self):
        scratch = self.repo / "tests/.baseline-tmp"
        scratch.mkdir(parents=True)
        report_path = scratch / "runner report.json"
        with patch.dict(os.environ, {"WENSHU_TEST_TMPDIR": str(scratch)}):
            report = build_site.run_validation(
                repo=self.repo, node=self.node, npm_cli=self.npm_cli,
                report_path=report_path, executor=self.successful_executor,
            )
        self.assertEqual(report["status"], "passed", report["failures"])
        self.assertTrue(report["temporary_isolation"]["cleaned"])
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)
        self.assertEqual(list(scratch.iterdir()), [report_path])

    def test_unsupported_node_is_failure_and_observed_versions_remain_in_report(self):
        calls = []
        def old_runtime(argv, *, cwd, env):
            calls.append(list(argv))
            self.assertIn("-e", argv, "npm install/build must not run after failed preflight")
            Path(env["WENSHU_BUILD_RUNTIME_REPORT"]).write_text(
                '{"node":"v22.11.0","npm":"11.6.1"}', encoding="utf-8"
            )
            return 0
        report_path = self.root / "unsupported runtime.json"
        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli,
            report_path=report_path, executor=old_runtime,
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["node"].get("version"), "v22.11.0")
        self.assertEqual(report["npm"].get("version"), "11.6.1")
        self.assertIn(">= 22.12.0", report["failures"][0]["message"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(report["temporary_isolation"]["cleaned"])
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)

    def test_cli_from_unrelated_cwd_returns_nonzero_and_writes_build_failure_report(self):
        report_path = self.root / "CLI failure report.json"
        def broken_build(argv, *, cwd, env):
            if "run" in argv:
                return 23
            return self.successful_executor(argv, cwd=cwd, env=env)
        previous = Path.cwd()
        try:
            os.chdir(self.node.parent)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = build_site.main(
                    ["--node", str(self.node), "--npm-cli", str(self.npm_cli),
                     "--report", str(report_path)],
                    repo=self.repo, executor=broken_build,
                )
        finally:
            os.chdir(previous)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"][0]["stage"], "build")
        self.assertIn("exited 23", report["failures"][0]["message"])
        self.assertTrue(report["temporary_isolation"]["cleaned"])

    def test_missing_required_lockfile_fails_before_any_external_process(self):
        (self.app / "package-lock.json").unlink()
        calls = []
        report_path = self.root / "missing input.json"
        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli,
            report_path=report_path, executor=lambda argv, **kw: calls.append(argv),
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"][0]["stage"], "inputs")
        self.assertIn("package-lock.json", report["failures"][0]["message"])
        self.assertFalse(calls)
        self.assertTrue(report["temporary_isolation"]["cleaned"])
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)

    def test_missing_node_or_npm_cli_is_always_an_error(self):
        cases = ((self.root / "missing-node.exe", self.npm_cli),
                 (self.node, self.root / "missing-npm-cli.js"))
        for node, npm_cli in cases:
            with self.subTest(node=node, npm_cli=npm_cli):
                calls = []
                report = build_site.run_validation(
                    repo=self.repo, node=node, npm_cli=npm_cli,
                    executor=lambda argv, **kw: calls.append(argv),
                )
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["failures"][0]["stage"], "runtime")
                self.assertFalse(calls)
                self.assertTrue(report["temporary_isolation"]["cleaned"])

    def test_npm_cmd_path_with_spaces_resolves_js_and_keeps_process_environment_isolated(self):
        wrapper = self.node.parent / "npm.cmd"
        wrapper.write_text("DO NOT EXECUTE THIS WRAPPER", encoding="utf-8")
        calls = []
        canaries = {"NPM_TOKEN": "SYNTHETIC-NOT-A-SECRET", "NODE_OPTIONS": "--require FAKE-CANARY",
                    "PYTHONPATH": "FAKE-CANARY", "WENSHU_DOCS": "FAKE-CANARY",
                    "NPM_CONFIG_USERCONFIG": "FAKE-CANARY", "NPM_CONFIG_REGISTRY": "FAKE-CANARY"}
        def isolated(argv, *, cwd, env):
            root = Path(cwd).parent
            calls.append(list(argv))
            self.assertEqual(argv[0], str(self.node))
            if "-e" not in argv:
                self.assertEqual(argv[1], str(self.npm_cli))
            self.assertNotIn(str(wrapper), argv)
            for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA",
                         "NPM_CONFIG_CACHE", "NPM_CONFIG_PREFIX", "TEMP", "TMP", "TMPDIR"):
                self.assertTrue(Path(env[name]).is_relative_to(root), name)
            for name in ("NPM_CONFIG_USERCONFIG", "NPM_CONFIG_GLOBALCONFIG"):
                self.assertEqual(Path(env[name]).read_text(encoding="utf-8"), "")
                self.assertTrue(Path(env[name]).is_relative_to(root))
            self.assertNotIn("NPM_TOKEN", env)
            self.assertNotIn("NODE_OPTIONS", env)
            self.assertNotIn("PYTHONPATH", env)
            self.assertNotIn("NPM_CONFIG_REGISTRY", env)
            self.assertEqual(env["WENSHU_APP"], str(cwd))
            self.assertTrue(Path(env["WENSHU_DOCS"]).is_relative_to(root))
            self.assertIn(str(self.node.parent), env["PATH"].split(os.pathsep))
            python_name = "python.cmd" if os.name == "nt" else "python"
            python_wrapper = root / "bin" / python_name
            self.assertIn(build_site.sys.executable, python_wrapper.read_text(encoding="utf-8"))
            self.assertIn("-B", python_wrapper.read_text(encoding="utf-8"))
            return self.successful_executor(argv, cwd=cwd, env=env)
        with patch.dict(os.environ, canaries):
            report = build_site.run_validation(
                repo=self.repo, node=self.node, npm_cli=wrapper, executor=isolated,
            )
            for name, value in canaries.items():
                self.assertEqual(os.environ[name], value)
        self.assertEqual(report["status"], "passed", report["failures"])
        self.assertEqual(len(calls), 3)
        self.assertTrue(report["temporary_isolation"]["cleaned"])

    def test_source_inputs_and_generated_output_metadata_changes_never_pass(self):
        for relative in ("src/styles/global.css", "dist/new-generated-output.html"):
            with self.subTest(relative=relative):
                def changes_source(argv, *, cwd, env):
                    code = self.successful_executor(argv, cwd=cwd, env=env)
                    if "run" in argv:
                        (self.app / relative).write_text("FAKE source changed", encoding="utf-8")
                    return code
                report = build_site.run_validation(
                    repo=self.repo, node=self.node, npm_cli=self.npm_cli,
                    executor=changes_source,
                )
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["source_unchanged"]["status"], "failed")
                self.assertTrue(any(f["stage"] == "integrity" for f in report["failures"]))
                self.assertTrue(report["temporary_isolation"]["cleaned"])

    def test_copy_rejects_lexical_overlap_and_existing_destinations(self):
        overlapping = self.root / "cancelled/../source repository/custom/new-copy"
        with self.assertRaises(build_site.BuildFailure):
            build_site.copy_app(self.app, overlapping)
        self.assertFalse((self.app / "new-copy").exists())
        destination = self.root / "existing copy target"
        destination.mkdir()
        marker = destination / "preserved.txt"
        marker.write_text("KEEP", encoding="utf-8")
        with self.assertRaises(build_site.BuildFailure):
            build_site.copy_app(self.app, destination)
        self.assertEqual(marker.read_text(encoding="utf-8"), "KEEP")


    def make_owned_directory_link(self, link, target):
        # NTFS junctions need no elevated symlink privilege. All paths are newly
        # owned unit trees; never repair an existing denied path or existing asset.
        if os.name == "nt":
            comspec = os.environ.get("COMSPEC", "C:/Windows/System32/cmd.exe")
            subprocess.run([comspec, "/d", "/c", "mklink", "/J", str(link), str(target)],
                           check=True, stdout=sys.stderr, stderr=sys.stderr)  # inherited handles, no pipe capture
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_redirected_temporary_root_reports_cleanup_refusal_without_uncaught_error(self):
        links = []
        def removes_owned_links():
            for link in links:
                if os.name == "nt":
                    link.rmdir()  # remove only the newly-created junction itself
                else:
                    link.unlink()
        self.addCleanup(removes_owned_links)
        def redirects_root(argv, *, cwd, env):
            code = self.successful_executor(argv, cwd=cwd, env=env)
            if "run" in argv:
                root = Path(cwd).parent
                moved = self.root / "owned redirected build"
                root.rename(moved)
                self.make_owned_directory_link(root, moved)
                links.append(root)
            return code
        report_path = self.root / "cleanup failure.json"
        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli,
            report_path=report_path, executor=redirects_root,
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["temporary_isolation"]["status"], "failed")
        self.assertFalse(report["temporary_isolation"]["cleaned"])
        self.assertTrue(any(f["stage"] == "cleanup" for f in report["failures"]))
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)


    def test_copy_rejects_links_and_reparse_points_without_reading_target(self):
        external = self.root / "synthetic external target"
        external.mkdir()
        (external / "canary.txt").write_text("FAKE EXTERNAL DATA", encoding="utf-8")
        link = self.app / "src/pages/linked-input"
        self.make_owned_directory_link(link, external)
        destination = self.root / "rejected copy"
        with self.assertRaisesRegex(build_site.BuildFailure, "Links/reparse points"):
            build_site.copy_app(self.app, destination)
        self.assertFalse(destination.exists())
        self.assertEqual((external / "canary.txt").read_text(encoding="utf-8"), "FAKE EXTERNAL DATA")


    def test_runtime_and_process_protocol_failures_are_errors_with_failure_json(self):
        cases = ("bad-runtime-object", "old-npm", "launch-error", "missing-exit-code")
        for mode in cases:
            with self.subTest(mode=mode):
                def broken(argv, *, cwd, env):
                    if "-e" in argv and mode == "bad-runtime-object":
                        Path(env["WENSHU_BUILD_RUNTIME_REPORT"]).write_text("[]", encoding="utf-8")
                        return 0
                    if "-e" in argv and mode == "old-npm":
                        Path(env["WENSHU_BUILD_RUNTIME_REPORT"]).write_text(
                            '{"node":"v24.11.0","npm":"9.6.4"}', encoding="utf-8")
                        return 0
                    if "ci" in argv and mode == "launch-error":
                        raise OSError("Synthetic external process launch failed")
                    if "ci" in argv and mode == "missing-exit-code":
                        return None
                    self.assertNotIn("run", argv)
                    return self.successful_executor(argv, cwd=cwd, env=env)
                report_path = self.root / (mode + ".json")
                report = build_site.run_validation(
                    repo=self.repo, node=self.node, npm_cli=self.npm_cli,
                    report_path=report_path, executor=broken,
                )
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["failures"])
                self.assertTrue(report["temporary_isolation"]["cleaned"])
                self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)

    def test_report_cannot_overwrite_source_inputs_through_lexical_traversal(self):
        report_path = self.root / "cancelled/../source repository/custom/src/styles/global.css"
        original = (self.app / "src/styles/global.css").read_bytes()
        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli,
            report_path=report_path, executor=self.successful_executor,
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"][-1]["stage"], "report")
        self.assertEqual((self.app / "src/styles/global.css").read_bytes(), original)
        self.assertTrue(report["temporary_isolation"]["cleaned"])


    def test_malformed_generated_pagefind_metadata_is_structured_smoke_failure(self):
        def malformed(argv, *, cwd, env):
            code = self.successful_executor(argv, cwd=cwd, env=env)
            if "run" in argv:
                (Path(cwd) / "dist/pagefind/pagefind-entry.json").write_text("[]", encoding="utf-8")
            return code
        report_path = self.root / "malformed pagefind.json"
        report = build_site.run_validation(
            repo=self.repo, node=self.node, npm_cli=self.npm_cli,
            report_path=report_path, executor=malformed,
        )
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(f.get("check") == "pagefind-index" for f in report["failures"]))
        self.assertTrue(report["temporary_isolation"]["cleaned"])
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)


if __name__ == "__main__":
    unittest.main()
