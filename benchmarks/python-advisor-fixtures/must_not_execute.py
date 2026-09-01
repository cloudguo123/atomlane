"""Canary fixture: static analysis must never execute this module."""

from pathlib import Path

MARKER = Path(__file__).with_name("must-not-exist.marker")
MARKER.write_text("python advisor fixture was executed\n", encoding="utf-8")
