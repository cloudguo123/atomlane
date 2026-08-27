from __future__ import annotations

import unittest

import generate_test_report


class ReportRenderingTests(unittest.TestCase):
    def test_public_report_has_share_and_discovery_metadata(self) -> None:
        rendered = generate_test_report.render_html(
            {
                "overall": "passed",
                "generated_at": "2026-08-26T00:00:00+00:00",
                "version": "0.10.0",
            }
        )
        self.assertIn('property="og:image"', rendered)
        self.assertIn("44 verified tests", rendered)
        self.assertIn('name="twitter:card" content="summary_large_image"', rendered)
        self.assertIn('rel="canonical"', rendered)
        self.assertIn('"@type":"SoftwareApplication"', rendered)
        self.assertIn("issues/new?template=first-run.yml", rendered)
        self.assertNotIn(".innerHTML", rendered)


if __name__ == "__main__":
    unittest.main()
