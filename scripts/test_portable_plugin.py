from __future__ import annotations

import json
import pathlib
import re
import unittest

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


if __name__ == "__main__":
    unittest.main()
