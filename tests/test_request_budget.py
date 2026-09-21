from __future__ import annotations

from dataclasses import replace
import json
import time
import unittest
from unittest import mock

from daemon import LiveJevService
from intent import TargetOrigin, build_request, interpret_response
from snapshot import Device, Param, Snapshot, Track
from tests.support import StatefulLive, choice, response, sample_snapshot


def large_snapshot(track_count: int = 90, params_per_track: int = 20) -> Snapshot:
    tracks = []
    for track_index in range(track_count):
        params = tuple(
            Param(
                index,
                f"Parameter {index}",
                0.5,
                0.0,
                1.0,
                "50%",
                f"live_set tracks {track_index} devices 0 parameters {index}",
            )
            for index in range(params_per_track)
        )
        device = Device(0, f"Device {track_index}", params, f"live_set tracks {track_index} devices 0")
        tracks.append(
            Track(
                track_index,
                f"Track {track_index + 1}",
                0.5,
                "-6.0 dB",
                0.0,
                "C",
                False,
                False,
                (device,),
                f"live_set tracks {track_index}",
            )
        )
    return Snapshot(tuple(tracks), 0.75, "-1.0 dB", 120.0, False, time.time())


class RequestBudgetTests(unittest.TestCase):
    def test_ninety_track_request_is_bounded_by_serialized_bytes(self) -> None:
        payload = build_request(large_snapshot(), "adjust this parameter", 44)
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(size, 64 * 1024)
        self.assertEqual(payload["state"]["detail_tracks"], [44])
        self.assertEqual(len(payload["questions"]), 10)

    def test_literal_alias_number_and_selected_tracks_form_the_shortlist(self) -> None:
        snapshot = large_snapshot(6, 1)
        with mock.patch("intent.load_aliases", return_value={"Track 5": ("Lead-Part",)}):
            payload = build_request(snapshot, "lead part and track 3", 1)
        self.assertEqual(payload["state"]["detail_tracks"], [2, 4, 1])
        self.assertIn("param_t4", payload["questions"])
        self.assertIn("param_t2", payload["questions"])
        self.assertIn("param_t1", payload["questions"])

    def test_selected_track_supplies_detail_when_no_name_is_present(self) -> None:
        payload = build_request(large_snapshot(4, 1), "turn the parameter down", 2)
        self.assertEqual(payload["state"]["detail_tracks"], [2])
        self.assertIn("param_t2", payload["questions"])

    def test_missing_detail_is_not_none_or_another_tracks_answer(self) -> None:
        snapshot = large_snapshot(2, 1)
        mocked = response("param", "t1", "up_small")
        mocked["answers"]["param_t0"] = choice("d0p0", 0.99)
        result = interpret_response(snapshot, "raise the lead control", mocked, (0,))
        self.assertEqual(result.intent.track, 1)
        self.assertIsNone(result.intent.param)
        self.assertEqual(result.intent.param_conf, 0.0)
        self.assertEqual(result.param_options, ())

    def test_unsolicited_detail_key_outside_request_is_ignored(self) -> None:
        snapshot = large_snapshot(2, 1)
        mocked = response("param", "t1", "up_small")
        mocked["answers"]["param_t1"] = choice("d0p0", 0.99)
        result = interpret_response(snapshot, "raise the lead control", mocked, (0,))
        self.assertIsNone(result.intent.param)
        self.assertEqual(result.evaluated_detail_tracks, (0,))

    def test_first_pass_omission_asks_and_clarified_omission_refuses(self) -> None:
        snapshot = large_snapshot(2, 1)
        mocked = response("param", "t1", "up_small")
        result = interpret_response(snapshot, "raise the lead control", mocked, (0,))
        service = LiveJevService(bridge=StatefulLive(names=("Track 1", "Track 2")), snapshot=snapshot, key="x")
        first = service._decision(result, "first")
        self.assertEqual(first["kind"], "ask")
        clarified = replace(result, intent=replace(result.intent, target_origin=TargetOrigin.CLARIFIED))
        second = service._decision(clarified, "second")
        self.assertEqual(second["kind"], "error")
        self.assertEqual(service.bridge.calls, [])

    def test_budget_omission_ask_skips_rewriter(self) -> None:
        snapshot = large_snapshot(2, 1)
        mocked = response("param", "t0", "up_small")
        mocked["answers"]["track_stated"] = {
            "type": "choice",
            "choice": "named",
            "confidence": 0.4,
            "probabilities": {"named": 0.4, "absent": 0.35, "reference": 0.25},
        }
        bridge = StatefulLive(names=("Track 1", "Track 2"), selected=1)
        service = LiveJevService(
            bridge=bridge,
            snapshot=snapshot,
            key="x",
            requester=lambda _payload, _key: mocked,
            llm_key="x",
            rewriter=lambda *_args: (_ for _ in ()).throw(AssertionError("rewriter called")),
        )
        answer = service.process({"id": "ask", "text": "modify kick control"})
        self.assertEqual(answer["kind"], "ask")
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_volume_outside_shortlist_executes(self) -> None:
        snapshot = large_snapshot(2, 1)
        mocked = response("volume", "t0", "down_small")
        mocked["answers"]["track_stated"] = {
            "type": "choice",
            "choice": "named",
            "confidence": 0.4,
            "probabilities": {"named": 0.4, "absent": 0.35, "reference": 0.25},
        }
        bridge = StatefulLive(names=("Track 1", "Track 2"), selected=1)
        service = LiveJevService(bridge=bridge, snapshot=snapshot, key="x", requester=lambda _payload, _key: mocked)
        answer = service.process({"id": "volume", "text": "attenuate kick"})
        self.assertEqual(answer["kind"], "result")
        self.assertTrue(any("live_set tracks 0 mixer_device volume" in call for call in bridge.calls))

    def test_stated_missing_track_is_still_rejected(self) -> None:
        snapshot = sample_snapshot()
        mocked = response("volume", "none", "down_small", track_conf=0.2)
        mocked["answers"]["track_stated"] = {
            "type": "choice",
            "choice": "named",
            "confidence": 0.9,
            "probabilities": {"named": 0.9, "absent": 0.05, "reference": 0.05},
        }
        result = interpret_response(snapshot, "lower Ghost", mocked, ())
        bridge = StatefulLive()
        service = LiveJevService(bridge=bridge, snapshot=snapshot, key="x")
        decision = service._decision(result, "missing")
        self.assertEqual(decision["kind"], "error")
        self.assertFalse(any("--write" in call for call in bridge.calls))


if __name__ == "__main__":
    unittest.main()
