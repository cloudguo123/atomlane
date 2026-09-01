from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import generate_growth_assets

ROOT = pathlib.Path(__file__).resolve().parents[1]


class GrowthAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.data = {
            "latest": {
                "run_id": "test-run",
                "generated_at": "2026-08-26T00:00:00+00:00",
                "parallel": {"wall_time_seconds": 310.0},
                "serial_equivalent": {
                    "seconds": 1240.0,
                    "method": "sum of observed task runtimes",
                },
                "savings": {"seconds": 930.0, "speedup_multiplier": 4.0},
            },
            "cumulative": {"saved_seconds": 930.0},
        }

    def test_social_preview_has_exact_dimensions_and_metrics(self) -> None:
        rendered = generate_growth_assets.social_svg(self.data)
        self.assertIn('width="1280" height="640"', rendered)
        self.assertIn("20m 40s", rendered)
        self.assertIn("5m 10s", rendered)
        self.assertIn("15m 30s", rendered)
        self.assertIn("4.00×", rendered)

    def test_listing_logo_is_square_and_product_specific(self) -> None:
        rendered = generate_growth_assets.listing_logo_svg()
        self.assertIn('width="1024" height="1024"', rendered)
        self.assertIn("ATOMLANE", rendered)
        self.assertIn("PROVEN-SAFE PARALLELISM", rendered)

    def test_share_outputs_are_machine_readable_and_honestly_labeled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = pathlib.Path(directory)
            generate_growth_assets.write_share_outputs(self.data, target)
            payload = json.loads((target / "latest.json").read_text(encoding="utf-8"))
            markdown = (target / "latest.md").read_text(encoding="utf-8")
            self.assertEqual(payload["time_saved_seconds"], 930.0)
            self.assertEqual(payload["source"], "controlled-duration low-load benchmark")
            self.assertIn("serial equivalent", markdown)
            self.assertIn("not rerun serially", markdown)
            self.assertTrue((target / "social-preview.svg").exists())

    def test_showcase_payload_keeps_safe_form_length_margins(self) -> None:
        source = (ROOT / "docs" / "launch" / "OPENAI_SHOWCASE.md").read_text(
            encoding="utf-8"
        )

        def paragraph_after(heading: str) -> str:
            return source.split(heading, 1)[1].lstrip("\n").split("\n\n", 1)[0]

        one_line = paragraph_after("## One-line description")
        building_process = paragraph_after("### Building process")
        project_description = paragraph_after("### Project description")
        setup = next(line for line in source.splitlines() if line.startswith("- Setup:"))
        self.assertLessEqual(len(one_line), 240)
        self.assertLessEqual(len(building_process), 480)
        self.assertLessEqual(len(setup.removeprefix("- Setup: ")), 480)
        self.assertLessEqual(len(project_description), 950)


if __name__ == "__main__":
    unittest.main()
