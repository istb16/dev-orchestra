"""Workspace helpers that are not about the ``.ai/`` layout."""

from __future__ import annotations

import re
import unittest

from helpers import IsolatedCase

from orchestrator import providers
from orchestrator import workspace as ws
from orchestrator.providers import base


class TestRedaction(IsolatedCase):
    def test_credential_shaped_strings_are_scrubbed(self):
        samples = [
            "key sk-abcdefghijklmnop123",
            "ANTHROPIC_API_KEY=sk-ant-abcdefghijklmnopqrs",
            "token: ghp_abcdefghijklmnopqrstuvwxyz01",
            "Authorization: Bearer abcdefghijklmnopqrstuv",
            "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NX0.dBjftJeZ4CVPmB92K27u",
        ]
        for sample in samples:
            cleaned = ws.redact(sample)
            self.assertIn("[redacted]", cleaned, sample)

    def test_ordinary_text_is_untouched(self):
        text = "implementer finished in 12 seconds using opus"
        self.assertEqual(ws.redact(text), text)

    def test_the_adapter_interface_hands_out_the_same_function(self):
        """User adapters import it from ``providers.base``; it has one home."""
        self.assertIs(base.redact, ws.redact)
        self.assertIs(providers.redact, ws.redact)
        self.assertEqual(ws.redact.__module__, "orchestrator.workspace")
        self.assertFalse(hasattr(base, "_SECRET_PATTERNS"))

    def test_the_patterns_are_the_ones_that_moved(self):
        self.assertEqual(
            [(pattern.pattern, pattern.flags) for pattern in ws._SECRET_PATTERNS],
            [
                (r"\b(sk-[A-Za-z0-9_\-]{12,})", re.UNICODE),
                (r"\b(sk-ant-[A-Za-z0-9_\-]{12,})", re.UNICODE),
                (r"\b(gh[pousr]_[A-Za-z0-9]{16,})", re.UNICODE),
                (r"\b(ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})", re.UNICODE),
                (r"(?i)\bbearer\s+([A-Za-z0-9_\-\.]{12,})", re.IGNORECASE | re.UNICODE),
                (
                    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|secret)"
                    r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.]{12,})",
                    re.IGNORECASE | re.UNICODE,
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
