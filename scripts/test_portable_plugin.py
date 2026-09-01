from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import mcp_server

ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


class PortableAgentPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.mcp = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))

    def test_manifest_uses_closed_agent_plugins_v1_shape(self) -> None:
        allowed = {
            "$schema",
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
            "keywords",
            "extensions",
        }
        self.assertEqual(self.manifest["$schema"], PLUGIN_SCHEMA)
        self.assertLessEqual(set(self.manifest), allowed)
        self.assertRegex(
            self.manifest["name"],
            re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$"),
        )
        self.assertNotIn("--", self.manifest["name"])
        self.assertNotIn("..", self.manifest["name"])

    def test_portable_and_codex_manifests_share_identity(self) -> None:
        codex = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        for field in (
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
        ):
            self.assertEqual(self.manifest[field], codex[field])
        for asset_field in ("composerIcon", "logo"):
            asset = codex["interface"][asset_field]
            self.assertTrue(asset.startswith("./"))
            self.assertTrue((ROOT / asset.removeprefix("./")).is_file())
        prompts = codex["interface"]["defaultPrompt"]
        self.assertLessEqual(len(prompts), 3)
        self.assertTrue(all(1 <= len(prompt) <= 128 for prompt in prompts))
        self.assertTrue(any("$accelerate-local-work" in prompt for prompt in prompts))
        self.assertTrue(any("$optimize-python-parallelism" in prompt for prompt in prompts))

    def test_release_version_is_consistent_across_every_published_surface(self) -> None:
        version = self.manifest["version"]
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        ui_source = (ROOT / "assets" / "parallel-indicator-host.js").read_text(
            encoding="utf-8"
        )
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertEqual(version, "0.12.0")
        self.assertEqual(package["version"], version)
        self.assertEqual(lock["version"], version)
        self.assertEqual(lock["packages"][""]["version"], version)
        self.assertEqual(mcp_server.SERVER_VERSION, version)
        self.assertIn(f'version: "{version}"', ui_source)
        self.assertRegex(citation, rf"(?m)^version: {re.escape(version)}$")
        first_release = re.search(r"(?m)^## ([0-9]+\.[0-9]+\.[0-9]+) -", changelog)
        self.assertIsNotNone(first_release)
        self.assertEqual(first_release.group(1), version)

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
        server = self.mcp["mcpServers"]["mac-parallel-accelerator"]
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

    def test_packaged_manifests_launch_from_a_unicode_space_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="AtomLane 包 release ") as directory:
            plugin_root = pathlib.Path(directory) / "plugin copy"
            plugin_root.mkdir()
            for source_name in ("scripts", "assets", "catalog", "third_party"):
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
                        "uri": "ui://widget/mac-parallel-indicator-0.12.0.html"
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 5,
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
                server = manifest["mcpServers"]["mac-parallel-accelerator"]
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
                self.assertEqual(set(responses), {1, 2, 3, 4, 5})
                self.assertIn("manifest-ok", responses[5]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
