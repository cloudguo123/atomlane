#!/usr/bin/env python3

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pages_evidence_selector


class PagesEvidenceSelectorTests(unittest.TestCase):
    repository = "cloudguo123/atomlane"

    @classmethod
    def valid_payload(cls) -> dict[str, object]:
        return {
            "workflow_run": {
                "name": "CI",
                "id": 123456,
                "head_sha": "a" * 40,
                "head_branch": "main",
                "head_repository": {"full_name": cls.repository},
                "event": "push",
                "conclusion": "success",
            }
        }

    def test_valid_selector_is_narrow_and_nul_delimited(self) -> None:
        self.assertEqual(
            pages_evidence_selector.validate_workflow_run_selector(
                self.valid_payload(), self.repository
            ),
            ("CI", "123456", "a" * 40),
        )
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_text(json.dumps(self.valid_payload()), encoding="utf-8")
            output = io.BytesIO()
            stdout = mock.Mock(buffer=output)
            with mock.patch.object(pages_evidence_selector.sys, "stdout", stdout):
                self.assertEqual(
                    pages_evidence_selector.main([str(event), self.repository]), 0
                )
        self.assertEqual(output.getvalue(), b"CI\0" + b"123456\0" + b"a" * 40 + b"\0")

    def test_every_trust_field_fails_closed(self) -> None:
        mutations = {
            "fork repository": ("head_repository", {"full_name": "fork/atomlane"}),
            "pull request event": ("event", "pull_request"),
            "non-main branch": ("head_branch", "feature"),
            "failed conclusion": ("conclusion", "failure"),
            "malformed sha": ("head_sha", "a" * 39),
            "unknown workflow": ("name", "Release"),
            "boolean id": ("id", True),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label):
                payload = copy.deepcopy(self.valid_payload())
                payload["workflow_run"][field] = value
                with self.assertRaises(pages_evidence_selector.SelectorError):
                    pages_evidence_selector.validate_workflow_run_selector(
                        payload, self.repository
                    )

    def test_missing_or_oversized_payload_fails_closed(self) -> None:
        with self.assertRaises(pages_evidence_selector.SelectorError):
            pages_evidence_selector.validate_workflow_run_selector({}, self.repository)
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_bytes(b" " * (pages_evidence_selector.MAX_EVENT_BYTES + 1))
            with self.assertRaisesRegex(
                pages_evidence_selector.SelectorError, "size bound"
            ):
                pages_evidence_selector.load_selector(event, self.repository)


if __name__ == "__main__":
    unittest.main()
