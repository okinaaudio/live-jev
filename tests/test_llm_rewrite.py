from __future__ import annotations

import io
import json
import urllib.error
import unittest
from unittest import mock
from dataclasses import replace

from llm_rewrite import GeminiRewriter, build_prompt
from snapshot import Device, Track
from tests.support import sample_snapshot


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


class GeminiRewriterTests(unittest.TestCase):
    def test_prompt_excludes_track_containing_bridge_device(self) -> None:
        snapshot = sample_snapshot()
        bridge_track = Track(
            3, "Codex Bridge", 0.5, "-9.0 dB", 0.0, "C", False, False,
            (Device(0, "LiveUdpBridge", (), "live_set tracks 3 devices 0"),),
            "live_set tracks 3",
        )
        prompt = build_prompt(replace(snapshot, tracks=(*snapshot.tracks, bridge_track)), "ぶりっじ")
        self.assertIn("Pad", prompt)
        self.assertNotIn("Codex Bridge", prompt)
        self.assertNotIn("LiveUdpBridge", prompt)

    def test_uses_35_flash_without_thinking(self) -> None:
        body = b'{"candidates":[{"content":{"parts":[{"text":"Drums\\u3092\\u30df\\u30e5\\u30fc\\u30c8"}]}}]}'
        with mock.patch("llm_rewrite._OPENER.open", return_value=_Response(body)) as opened:
            text = GeminiRewriter()(sample_snapshot(), "drums off", "top-secret-key")
        request = opened.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIn("/gemini-3.5-flash:generateContent", request.full_url)
        self.assertEqual(
            payload["generationConfig"],
            {"maxOutputTokens": 120, "thinkingConfig": {"thinkingBudget": 0}},
        )
        self.assertNotIn("thinkingLevel", json.dumps(payload))
        self.assertEqual(text, "Drumsをミュート")

    def test_http_statuses_have_fixed_redacted_messages(self) -> None:
        cases = {
            400: "Geminiが受け付けません（400）",
            401: "Geminiの鍵が拒否されました（401/403）",
            403: "Geminiの鍵が拒否されました（401/403）",
            429: "Geminiの回数制限（429）",
            500: "Gemini側の問題（5xx）",
            503: "Gemini側の問題（5xx）",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                error = urllib.error.HTTPError(
                    "https://example.invalid", status, "secret response", {}, io.BytesIO(b"secret body")
                )
                with mock.patch("llm_rewrite._OPENER.open", side_effect=error):
                    with self.assertRaisesRegex(RuntimeError, f"^{expected}$") as raised:
                        GeminiRewriter()(sample_snapshot(), "test", "top-secret-key")
                message = str(raised.exception)
                self.assertNotIn("secret", message)
                self.assertNotIn("top-secret-key", message)

    def test_network_failure_has_fixed_message(self) -> None:
        with mock.patch(
            "llm_rewrite._OPENER.open",
            side_effect=urllib.error.URLError("secret network detail"),
        ):
            with self.assertRaisesRegex(RuntimeError, "^Geminiに繋がりません（通信）$") as raised:
                GeminiRewriter()(sample_snapshot(), "test", "top-secret-key")
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
