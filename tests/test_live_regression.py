from __future__ import annotations

import copy
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from live_regression import Case, fingerprint, inspect_set, run_injected, run_regression


def make_snapshot(names=("LJ-TEST", "Keys", "Bass"), mute=False):
    tracks = []
    for index, name in enumerate(names):
        tracks.append({
            "index": index, "path": "live_set tracks {}".format(index), "name": name,
            "mute": mute, "solo": False, "arm": False,
            "mixer": {
                "volume": {"value": 0.85, "display": "0.00 dB"},
                "panning": {"value": 0.0, "display": "C"},
            },
            "sends": [0.0],
        })
    return {"tracks": tracks, "returns": [{"name": "A"}]}


class LiveRegressionTests(unittest.TestCase):
    def _assert_dispatch_exception_restores(self, exception):
        state = make_snapshot()
        context = inspect_set(lambda: state, lambda: "live_set tracks 1")
        sends = []

        def send(text):
            sends.append(text)
            if text == "change":
                state["tracks"][1]["mute"] = True
                raise exception
            state["tracks"][1]["mute"] = False
            return {"kind": "result", "ms": {"total": 1, "jev": 0}}

        with self.assertRaises(type(exception)):
            run_regression(
                [Case("change", "en", restore_utterances=("restore",))], context=context,
                read_snapshot=lambda: copy.deepcopy(state), read_selected=lambda: "live_set tracks 1",
                send=send, output=io.StringIO(), sleeper=lambda _seconds: None,
            )
        self.assertEqual(sends, ["change", "restore"])
        self.assertFalse(state["tracks"][1]["mute"])

    def test_timeout_after_dispatch_still_restores_then_reraises(self):
        self._assert_dispatch_exception_restores(TimeoutError("accepted but reply timed out"))

    def test_keyboard_interrupt_after_dispatch_still_restores_then_reraises(self):
        self._assert_dispatch_exception_restores(KeyboardInterrupt())

    def test_marker_missing_refuses_without_sending(self):
        sends = []
        report = run_injected(
            read_snapshot=lambda: make_snapshot(("Marker", "Keys", "Bass")),
            read_selected=lambda: "live_set tracks 1", send=lambda text: sends.append(text),
            cases=[], output=io.StringIO(),
        )
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(sends, [])

    def test_too_many_tracks_refuses_without_sending(self):
        snapshot = make_snapshot(tuple(["LJ-TEST"] + ["Track {}".format(i) for i in range(17)]))
        sends = []
        report = run_injected(
            read_snapshot=lambda: snapshot, read_selected=lambda: "live_set tracks 1",
            send=lambda text: sends.append(text), max_tracks=16, cases=[], output=io.StringIO(),
        )
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(sends, [])

    def test_selection_on_marker_refuses(self):
        report = run_injected(
            read_snapshot=lambda: make_snapshot(), read_selected=lambda: "live_set tracks 0",
            send=lambda _text: self.fail("send must not be called"), cases=[], output=io.StringIO(),
        )
        self.assertEqual(report.exit_code, 2)

    def test_marker_disappears_stops_with_exit_three(self):
        safe = make_snapshot()
        unsafe = make_snapshot(("Gone", "Keys", "Bass"))
        snapshots = iter([safe, safe, safe, unsafe, unsafe])
        sends = []
        context = inspect_set(lambda: safe, lambda: "live_set tracks 1")
        cases = [Case("mute it", "en"), Case("solo it", "en")]
        report = run_regression(
            cases, context=context, read_snapshot=lambda: next(snapshots, unsafe),
            read_selected=lambda: "live_set tracks 1",
            send=lambda text: sends.append(text) or {"kind": "result", "ms": {"total": 1, "jev": 0}},
            output=io.StringIO(), sleeper=lambda _seconds: None,
        )
        self.assertEqual(report.exit_code, 3)
        self.assertEqual(sends, ["mute it"])

    def test_failing_case_still_runs_restore(self):
        state = make_snapshot()
        context = inspect_set(lambda: state, lambda: "live_set tracks 1")
        sends = []

        def send(text):
            sends.append(text)
            if text == "restore":
                state["tracks"][1]["mute"] = False
            return {"kind": "result", "ms": {"total": 1, "jev": 0}}

        def fail(_before, _after, _selected, _reply):
            return "deliberate failure"

        report = run_regression(
            [Case("break", "en", assertion=fail, restore_utterances=("restore",))],
            context=context, read_snapshot=lambda: copy.deepcopy(state),
            read_selected=lambda: "live_set tracks 1", send=send,
            output=io.StringIO(), sleeper=lambda _seconds: None,
        )
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(sends, ["break", "restore"])

    def test_final_fingerprint_mismatch_is_nonzero(self):
        state = make_snapshot()
        context = inspect_set(lambda: state, lambda: "live_set tracks 1")

        def send(_text):
            state["tracks"][2]["pan"] = 0.5
            state["tracks"][2]["mixer"]["panning"]["value"] = 0.5
            return {"kind": "result", "ms": {"total": 1, "jev": 0}}

        report = run_regression(
            [Case("change", "en")], context=context,
            read_snapshot=lambda: copy.deepcopy(state), read_selected=lambda: "live_set tracks 1",
            send=send, output=io.StringIO(), sleeper=lambda _seconds: None,
        )
        self.assertNotEqual(report.exit_code, 0)
        self.assertNotEqual(report.after_fingerprint, fingerprint(make_snapshot()))


if __name__ == "__main__":
    unittest.main()
