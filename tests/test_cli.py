from __future__ import annotations

import contextlib
import io
import unittest

from cli import _print


class CliTests(unittest.TestCase):
    def test_gemini_marker_is_printed(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print({"kind": "ask", "line": "Try again", "options": [], "via": "gemini"})

        self.assertEqual(output.getvalue(), "Try again\nGemini\n\n")

    def test_non_gemini_response_has_no_marker(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            _print({"kind": "info", "line": "Ready"})

        self.assertEqual(output.getvalue(), "Ready\n")


if __name__ == "__main__":
    unittest.main()
