from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class UiBundleSecurityTests(unittest.TestCase):
    def test_bundle_contains_no_dynamic_code_evaluation(self) -> None:
        bundle = (ROOT / "assets" / "parallel-indicator-host.bundle.js").read_text(
            encoding="utf-8"
        )
        self.assertIsNone(re.search(r"\bnew\s+Function\s*\(", bundle))
        self.assertIsNone(re.search(r"\beval\s*\(", bundle))


if __name__ == "__main__":
    unittest.main()
