from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

import generate_test_report


class ReportRenderingTests(unittest.TestCase):
    def test_python_advisor_evidence_is_non_executing_and_hash_stable(self) -> None:
        check, evidence = generate_test_report.build_python_advisor_evidence()

        self.assertEqual(check["status"], "passed")
        self.assertTrue(evidence["available"])
        self.assertFalse(evidence["execution_performed"])
        self.assertFalse(evidence["files_modified"])
        self.assertFalse(evidence["target_code_executed"])
        self.assertFalse(evidence["target_files_modified"])
        self.assertTrue(evidence["fixture_hashes_unchanged"])
        self.assertEqual(evidence["fixture_sha256_before"], evidence["fixture_sha256_after"])
        self.assertTrue(evidence["execution_marker_absent"])
        self.assertEqual(evidence["benefit_kind"], "not_estimated")

    def test_python_advisor_evidence_fails_when_execution_marker_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture_root = pathlib.Path(directory) / "benchmarks" / "python-advisor-fixtures"
            fixture_root.mkdir(parents=True)
            source_root = (
                generate_test_report.ROOT / "benchmarks" / "python-advisor-fixtures"
            )
            for name in (
                "must_not_execute.py",
                "native.py",
                "pure_cpu.py",
                "read_io.py",
                "stateful.py",
            ):
                (fixture_root / name).write_bytes((source_root / name).read_bytes())
            (fixture_root / "must-not-exist.marker").write_text("executed\n", encoding="utf-8")

            with mock.patch.object(generate_test_report, "ROOT", pathlib.Path(directory)):
                check, evidence = generate_test_report.build_python_advisor_evidence()

        self.assertEqual(check["status"], "failed")
        self.assertFalse(evidence["execution_marker_absent"])

    def test_public_report_has_share_and_discovery_metadata(self) -> None:
        rendered = generate_test_report.render_html(
            {
                "overall": "passed",
                "generated_at": "2026-08-26T00:00:00+00:00",
                "version": "0.11.0",
                "summary": {"total": 74},
            }
        )
        self.assertIn('property="og:image"', rendered)
        self.assertIn("74 verified tests", rendered)
        self.assertIn('name="twitter:card" content="summary_large_image"', rendered)
        self.assertIn('rel="canonical"', rendered)
        self.assertIn('"@type":"SoftwareApplication"', rendered)
        self.assertIn("issues/new?template=first-run.yml", rendered)
        self.assertNotIn(".innerHTML", rendered)

    def test_public_report_renders_python_advisor_integrity_evidence(self) -> None:
        rendered = generate_test_report.render_html(
            {
                "overall": "passed",
                "generated_at": "2026-08-26T00:00:00+00:00",
                "version": "0.11.0",
                "summary": {"total": 1},
            }
        )

        self.assertIn("fixture SHA-256 unchanged", rendered)
        self.assertIn("execution marker absent", rendered)


if __name__ == "__main__":
    unittest.main()
