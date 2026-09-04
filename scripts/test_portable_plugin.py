from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import mcp_server

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


class CodexPluginPackagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.mcp = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))

    def test_codex_manifest_uses_a_closed_component_shape(self) -> None:
        allowed = {
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
            "keywords",
            "skills",
            "interface",
            "mcpServers",
        }
        self.assertLessEqual(set(self.manifest), allowed)
        self.assertRegex(
            self.manifest["name"],
            re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$"),
        )
        self.assertNotIn("--", self.manifest["name"])
        self.assertNotIn("..", self.manifest["name"])

    def test_codex_manifest_assets_and_prompts_are_valid(self) -> None:
        for asset_field in ("composerIcon", "logo"):
            asset = self.manifest["interface"][asset_field]
            self.assertTrue(asset.startswith("./"))
            self.assertTrue((ROOT / asset.removeprefix("./")).is_file())
        prompts = self.manifest["interface"]["defaultPrompt"]
        self.assertLessEqual(len(prompts), 3)
        self.assertTrue(all(1 <= len(prompt) <= 128 for prompt in prompts))
        self.assertTrue(any("$accelerate-local-work" in prompt for prompt in prompts))
        self.assertTrue(any("$optimize-python-parallelism" in prompt for prompt in prompts))

    def test_codex_plugin_bundles_a_non_blocking_task_assessment_hook(self) -> None:
        hook_path = ROOT / "hooks" / "hooks.json"
        config = json.loads(hook_path.read_text(encoding="utf-8"))
        self.assertEqual(set(config["hooks"]), {"UserPromptSubmit"})
        groups = config["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(groups), 1)
        self.assertNotIn("matcher", groups[0])
        handlers = groups[0]["hooks"]
        self.assertEqual(len(handlers), 1)
        handler = handlers[0]
        self.assertEqual(handler["type"], "command")
        self.assertIn("${PLUGIN_ROOT}/scripts/task_assessment_hook.py", handler["command"])
        self.assertIn("python3 -I -S", handler["command"])
        self.assertEqual(
            handler["commandWindows"],
            '"${PLUGIN_ROOT}\\scripts\\task-assessment-hook.cmd"',
        )
        self.assertEqual(handler["additionalContextLimit"], 512)
        self.assertNotIn("decision", hook_path.read_text(encoding="utf-8"))
        self.assertTrue((ROOT / "scripts" / "task_assessment_hook.py").is_file())
        windows_launcher = ROOT / "scripts" / "task-assessment-hook.cmd"
        self.assertTrue(windows_launcher.is_file())
        launcher_text = windows_launcher.read_text(encoding="utf-8")
        launcher_lines = launcher_text.splitlines()
        self.assertLess(
            launcher_lines.index("where python3 >nul 2>nul"),
            launcher_lines.index("where py >nul 2>nul"),
        )
        self.assertIn("python3 -I -S", launcher_text)
        self.assertIn("py -3 -I -S", launcher_text)
        self.assertIn("python -I -S", launcher_text)

    def test_release_version_is_consistent_across_every_published_surface(self) -> None:
        version = self.manifest["version"]
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        ui_source = (ROOT / "assets" / "parallel-indicator-host.js").read_text(
            encoding="utf-8"
        )
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(version, "0.16.0")
        self.assertEqual(package["version"], version)
        self.assertEqual(package["license"], "MPL-2.0")
        self.assertEqual(lock["version"], version)
        self.assertEqual(lock["packages"][""]["version"], version)
        self.assertEqual(lock["packages"][""]["license"], package["license"])
        self.assertEqual(mcp_server.SERVER_VERSION, version)
        self.assertIn(f'version: "{version}"', ui_source)
        self.assertRegex(citation, rf"(?m)^version: {re.escape(version)}$")
        first_release = re.search(r"(?m)^## ([0-9]+\.[0-9]+\.[0-9]+) -", changelog)
        self.assertIsNotNone(first_release)
        self.assertEqual(first_release.group(1), version)

    def test_indicator_resource_keeps_versioned_aliases_backward_compatible(self) -> None:
        for uri in (
            mcp_server.INDICATOR_RESOURCE_URI,
            "ui://widget/atomlane-indicator-0.13.0.html",
        ):
            with self.subTest(uri=uri):
                response = mcp_server.response_for(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "resources/read",
                        "params": {"uri": uri},
                    }
                )
                self.assertIsNotNone(response)
                self.assertNotIn("error", response)
                resource = response["result"]["contents"][0]
                self.assertEqual(resource["uri"], uri)
                self.assertEqual(resource["mimeType"], mcp_server.INDICATOR_MIME_TYPE)
                self.assertIn("AtomLane", resource["text"])

    def test_indicator_resource_rejects_non_atomlane_and_malformed_uris(self) -> None:
        for uri in (
            "ui://widget/another-app-0.13.0.html",
            "ui://widget/atomlane-indicator.html",
            "ui://widget/atomlane-indicator-0.13.html",
            "ui://widget/atomlane-indicator-00.13.0.html",
            "ui://widget/atomlane-indicator-0.13.0-beta.1.html",
            "ui://widget/atomlane-indicator-0.13.0+local.html",
            "ui://widget/atomlane-indicator-0.13.0.html?x=1",
            "ui://widget/atomlane-indicator-0.13.0.html#x",
            "ui://widget/atomlane-indicator-0.13.0.html/extra",
            "file://widget/atomlane-indicator-0.13.0.html",
            "ui://widget/atomlane-indicator-9999999.0.0.html",
            None,
            13,
        ):
            with self.subTest(uri=uri):
                response = mcp_server.response_for(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "resources/read",
                        "params": {"uri": uri},
                    }
                )
                self.assertIsNotNone(response)
                expected_code = -32602 if not isinstance(uri, str) else -32002
                self.assertEqual(response["error"]["code"], expected_code)
                self.assertNotIn("result", response)

    def test_resource_protocol_errors_never_use_tool_result_shape(self) -> None:
        for params in (None, [], {}, {"uri": None}):
            with self.subTest(params=params):
                response = mcp_server.response_for(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "resources/read",
                        "params": params,
                    }
                )
                self.assertEqual(response["error"]["code"], -32602)
                self.assertNotIn("result", response)

        with mock.patch.object(
            mcp_server,
            "_indicator_html",
            side_effect=OSError("private path must not leak"),
        ):
            response = mcp_server.response_for(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "resources/read",
                    "params": {"uri": mcp_server.INDICATOR_RESOURCE_URI},
                }
            )
        self.assertEqual(
            response,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32603, "message": "Internal error"},
            },
        )

    def test_non_object_jsonrpc_request_is_rejected_without_crashing(self) -> None:
        self.assertEqual(
            mcp_server.response_for([]),
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            },
        )

    def test_current_license_and_legacy_boundary_are_explicit(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        legacy_text = (ROOT / "LICENSES" / "MIT-legacy.txt").read_text(
            encoding="utf-8"
        )
        licensing = (ROOT / "LICENSING.md").read_text(encoding="utf-8")
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        report_generator = (ROOT / "scripts" / "generate_test_report.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(self.manifest["license"], "MPL-2.0")
        self.assertTrue(
            license_text.startswith("Mozilla Public License Version 2.0")
        )
        self.assertTrue(legacy_text.startswith("MIT License"))
        self.assertIn("0.13.0", licensing)
        self.assertIn("a25827da0844bb1f9d895c582f8bd741c37c953c", licensing)
        self.assertIn("Mozilla Public License", notice)
        self.assertRegex(citation, r"(?m)^license: MPL-2\.0$")
        self.assertIn('"https://www.mozilla.org/MPL/2.0/"', report_generator)
        for skill in (ROOT / "skills").glob("*/SKILL.md"):
            with self.subTest(skill=skill.parent.name):
                self.assertRegex(
                    skill.read_text(encoding="utf-8"),
                    r"(?m)^license: MPL-2\.0$",
                )

    def test_current_product_surfaces_do_not_publish_legacy_identity(self) -> None:
        # Changelog and retained source-bound evidence are historical records.
        historical_records = {
            "CHANGELOG.md",
            "docs/index.html",
            "docs/test-results.json",
            "docs/windows-preview-results.json",
        }
        ignored_roots = {".git", "node_modules", "__pycache__"}
        ignored_suffixes = {
            ".gif",
            ".ico",
            ".jpg",
            ".jpeg",
            ".png",
            ".pyc",
            ".webp",
            ".zip",
        }
        legacy_markers = (
            "mac" + "-parallel-accelerator",
            "mac" + "_parallel_accelerator",
            "Mac" + " Parallel Accelerator",
            "MAC" + "_PARALLEL_ACCELERATOR",
            "MPA" + "_TRAFFIC_TOKEN",
        )
        violations: list[str] = []

        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() in ignored_suffixes:
                continue
            relative = path.relative_to(ROOT)
            if relative.as_posix() in historical_records:
                continue
            if any(part in ignored_roots for part in relative.parts):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for marker in legacy_markers:
                if marker.casefold() in content.casefold():
                    violations.append(f"{relative.as_posix()}: {marker}")

        self.assertEqual(violations, [])

    def test_third_party_notices_match_direct_package_versions(self) -> None:
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        direct = {**package["dependencies"], **package["devDependencies"]}
        for dependency, version in direct.items():
            self.assertRegex(
                notices,
                rf"(?m)^- Package: `{re.escape(dependency)}` {re.escape(version)}$",
            )

    def test_browser_bundle_dependencies_have_exact_license_payloads(self) -> None:
        source = (ROOT / "assets" / "parallel-indicator-host.js").read_text(
            encoding="utf-8"
        )
        bundle = (ROOT / "assets" / "parallel-indicator-host.bundle.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('from "@modelcontextprotocol/ext-apps"', source)
        self.assertNotIn("app-with-deps", source)
        self.assertRegex(bundle, r"major:4,minor:4,patch:3")
        self.assertNotRegex(bundle, r"major:4,minor:3,patch:5")
        provenance = json.loads(
            (ROOT / "third_party" / "bundle-dependencies.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(provenance["schema_version"], "1.0")
        self.assertEqual(
            provenance["bundle"], "assets/parallel-indicator-host.bundle.js"
        )
        dependencies = provenance["dependencies"]
        expected_names = {
            "@modelcontextprotocol/ext-apps",
            "@modelcontextprotocol/sdk",
            "zod",
            "zod-to-json-schema",
        }
        self.assertEqual({item["name"] for item in dependencies}, expected_names)
        self.assertEqual(len(dependencies), len(expected_names))
        license_directory = ROOT / "third_party" / "licenses"
        self.assertEqual(
            {path.name for path in license_directory.iterdir() if path.is_file()},
            {pathlib.Path(item["license_file"]).name for item in dependencies},
        )
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        for dependency in dependencies:
            with self.subTest(dependency=dependency["name"]):
                package_root = ROOT / "node_modules" / dependency["name"]
                package = json.loads(
                    (package_root / "package.json").read_text(encoding="utf-8")
                )
                self.assertEqual(package["version"], dependency["version"])
                payload = ROOT / dependency["license_file"]
                self.assertEqual(
                    payload.read_bytes().rstrip(b"\n"),
                    (package_root / "LICENSE").read_bytes().rstrip(b"\n"),
                )
                self.assertRegex(
                    notices,
                    rf"(?m)^- Package: `{re.escape(dependency['name'])}` "
                    rf"{re.escape(dependency['version'])}$",
                )
                self.assertIn(dependency["license_file"], notices)

    def test_stdio_server_is_plugin_root_relative_and_closed(self) -> None:
        self.assertEqual(self.mcp["$schema"], MCP_SCHEMA)
        self.assertEqual(set(self.mcp), {"$schema", "mcpServers"})
        server = self.mcp["mcpServers"]["atomlane"]
        self.assertEqual(set(server), {"type", "command", "args", "cwd"})
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["command"], "python3")
        self.assertEqual(server["cwd"], "${PLUGIN_ROOT}")
        self.assertEqual(server["args"], ["${PLUGIN_ROOT}/scripts/mcp_server.py"])
        self.assertTrue((ROOT / "scripts" / "mcp_server.py").is_file())

    def test_portable_skill_discovery_is_unambiguous(self) -> None:
        skills = sorted(path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md"))
        self.assertEqual(skills, ["accelerate-local-work", "optimize-python-parallelism"])

    def test_codex_package_excludes_local_run_and_scratch_data(self) -> None:
        ignored = {
            line.strip()
            for line in (ROOT / ".codexignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertTrue(
            {
                ".coverage",
                "htmlcov/",
                "runs/",
                "work/",
                "*.tmp",
                "*.log",
            }.issubset(ignored)
        )
        self.assertTrue((ROOT / ".codex-plugin" / "plugin.json").is_file())
        self.assertTrue((ROOT / ".mcp.json").is_file())
        self.assertFalse(
            (ROOT / "plugin.json").exists(),
            "a root Agent Plugin manifest makes current Codex skip bundled hooks",
        )

    def test_packaged_manifests_launch_from_a_unicode_space_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="AtomLane 包 release ") as directory:
            plugin_root = pathlib.Path(directory) / "plugin copy"
            plugin_root.mkdir()
            for source_name in ("scripts", "assets", "catalog", "third_party", "hooks"):
                shutil.copytree(
                    ROOT / source_name,
                    plugin_root / source_name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            for source_name in ("mcp.json", ".mcp.json"):
                shutil.copy2(ROOT / source_name, plugin_root / source_name)

            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "packaged-smoke", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "resources/read",
                    "params": {
                        "uri": (
                            "ui://widget/atomlane-indicator-"
                            f"{self.manifest['version']}.html"
                        )
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "resources/read",
                    "params": {
                        "uri": "ui://widget/atomlane-indicator-0.13.0.html"
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "parallel_exec",
                        "arguments": {
                            "default_cwd": str(plugin_root),
                            "tasks": [
                                {
                                    "id": "manifest-smoke",
                                    "argv": [
                                        sys.executable,
                                        "-c",
                                        "print('manifest-ok')",
                                    ],
                                }
                            ],
                        },
                    },
                },
            ]
            encoded = "".join(json.dumps(item) + "\n" for item in requests)
            for manifest_name in ("mcp.json", ".mcp.json"):
                manifest = json.loads(
                    (plugin_root / manifest_name).read_text(encoding="utf-8")
                )
                server = manifest["mcpServers"]["atomlane"]
                args = [
                    item.replace("${PLUGIN_ROOT}", str(plugin_root))
                    for item in server["args"]
                ]
                cwd = server["cwd"].replace("${PLUGIN_ROOT}", str(plugin_root))
                if cwd == ".":
                    cwd = str(plugin_root)
                completed = subprocess.run(
                    [server["command"], *args],
                    cwd=cwd,
                    input=encoded,
                    text=True,
                    capture_output=True,
                    timeout=30,
                    check=False,
                    encoding="utf-8",
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                responses = {
                    item["id"]: item
                    for line in completed.stdout.splitlines()
                    if (item := json.loads(line)).get("id") is not None
                }
                self.assertEqual(set(responses), {1, 2, 3, 4, 5, 6})
                self.assertEqual(
                    responses[5]["result"]["contents"][0]["uri"],
                    "ui://widget/atomlane-indicator-0.13.0.html",
                )
                self.assertIn("manifest-ok", responses[6]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
