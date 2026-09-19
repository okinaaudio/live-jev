from __future__ import annotations

import unittest
from unittest import mock
import json
from pathlib import Path
import tempfile
from dataclasses import replace

from actions import ACTIONS
from bridge_client import Ack, BridgeResult
from daemon import LiveJevService
from intent import ACTION_CRITERIA, Action, Step, build_request, candidate_params, interpret_response, parse_local, parse_number
from tests.support import response, sample_snapshot
from snapshot import Device, Track


class IntentAndActionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = sample_snapshot()

    def test_request_contains_all_speculative_questions_in_one_payload(self) -> None:
        payload = build_request(self.snapshot, "パッドを少し下げて")
        self.assertEqual(payload["model"], "jev-latest")
        self.assertIn("param_t0", payload["questions"])
        self.assertIn("action", payload["questions"])
        self.assertIn("compound", payload["questions"])
        self.assertIn("refers_previous", payload["questions"])
        self.assertNotIn("devices", payload["state"])

    def test_track_criteria_contains_position_devices_and_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aliases = Path(directory) / "aliases.json"
            aliases.write_text('{"Pad": ["パッド", "うわもの"]}', encoding="utf-8")
            with mock.patch("intent.ALIASES_PATH", aliases):
                criteria = build_request(self.snapshot, "パッド上げて")["questions"]["track"]["criteria"]
        self.assertEqual(criteria["t0"], "Pad（1番目）。デバイス: Reverb。別名: パッド, うわもの")
        self.assertEqual(criteria["t1"], "Bass（2番目）。デバイス: 。別名: ")

    def test_action_criteria_contains_spoken_variants(self) -> None:
        variants = {
            "solo": ("ソロ", "そろ", "だけ聞かせて", "だけ鳴らして"),
            "mute": ("消して", "鳴らさないで"),
            "volume": ("おんりょう", "ボリューム", "上げて", "下げて"),
            "tempo": ("てんぽ", "BPM"),
            "play": ("再生", "スタート", "流して"),
            "stop": ("止めて", "ストップ"),
            "param": ("つまみ", "ノブ", "パラメータ"),
        }
        for action, words in variants.items():
            with self.subTest(action=action):
                self.assertTrue(all(word in ACTION_CRITERIA[action] for word in words))

    def test_twelve_utterance_table_maps_to_intent_and_arguments(self) -> None:
        def db_arguments(path, target, probes, best=None):
            arguments = []
            for value in probes:
                arguments.extend([
                    ["--write", "--api-parameter-set", path, json.dumps(value), "set"],
                    ["--write", "--api-call", path, "str_for_value", json.dumps([value]), "display"],
                ])
            if best is not None:
                arguments.append(["--write", "--api-parameter-set", path, json.dumps(best), "set"])
            arguments.append(["--api-mixer-status", target, "mixer"])
            return arguments

        master_path = "live_set master_track mixer_device volume"
        cases = [
            ("ベース下げて", response("volume", "t1", "down_small"), Action.VOLUME, [
                ["--write", "--api-parameter-set", "live_set tracks 1 mixer_device volume", "0.52", "set"],
                ["--api-mixer-status", "1", "read"],
                ["--write", "--api-call", "live_set tracks 1 mixer_device volume", "str_for_value", "[0.52]", "display"],
            ]),
            ("パッドもうちょい上", response("volume", "t0", "up_small"), Action.VOLUME, [
                ["--write", "--api-parameter-set", "live_set tracks 0 mixer_device volume", "0.63", "set"],
                ["--api-mixer-status", "0", "read"],
                ["--write", "--api-call", "live_set tracks 0 mixer_device volume", "str_for_value", "[0.63]", "display"],
            ]),
            ("ドラムだけ聞かせて", response("solo", "t2"), Action.SOLO, [
                ["--write", "--api-set", "live_set tracks 2", "solo", "1", "set"],
                ["--api-get", "live_set tracks 2", "solo", "read"],
            ]),
            ("ドラムのソロ外して", response("unsolo", "t2"), Action.UNSOLO, [
                ["--write", "--api-set", "live_set tracks 2", "solo", "0", "set"],
                ["--api-get", "live_set tracks 2", "solo", "read"],
            ]),
            ("テンポ90", response("tempo", step="set"), Action.TEMPO, [
                ["--write", "--tempo", "90"],
                ["--api-get", "live_set", "tempo", "tempo"],
            ]),
            ("テンポちょい上げ", response("tempo", step="up_small"), Action.TEMPO, [
                ["--write", "--tempo", "122"],
                ["--api-get", "live_set", "tempo", "tempo"],
            ]),
            ("止めて", response("stop"), Action.STOP, [
                ["--write", "--api-call", "live_set", "stop_playing", "[]", "transport"],
                ["--api-get", "live_set", "is_playing", "playing"],
            ]),
            ("再生", response("play"), Action.PLAY, [
                ["--write", "--api-call", "live_set", "start_playing", "[]", "transport"],
                ["--api-get", "live_set", "is_playing", "playing"],
            ]),
            ("マスター-3dB", response("volume", "master", "set"), Action.VOLUME, db_arguments(
                master_path, "master", [0.5, 0.75, 0.875, 0.9375, 0.96875, 0.953125, 0.9453125, 0.94921875],
            )),
            ("パン左30", response("pan", "t0", "set"), Action.PAN, [
                ["--write", "--api-parameter-set", "live_set tracks 0 mixer_device panning", "-0.6", "set"],
                ["--api-mixer-status", "0", "read"],
                ["--write", "--api-call", "live_set tracks 0 mixer_device panning", "str_for_value", "[-0.6]", "display"],
            ]),
            ("リバーブのDryWet上げて", response("param", "t0", "up_small", param="d0p0"), Action.PARAM, [
                ["--write", "--api-parameter-set", "live_set tracks 0 devices 0 parameters 0", "0.3", "set"],
                ["--api-device-parameters", "live_set tracks 0 devices 0", "read"],
            ]),
            ("もっとエモくして", response("none", generation=0.95), Action.NONE, []),
        ]
        for utterance, mocked, expected_action, expected_arguments in cases:
            with self.subTest(utterance=utterance):
                result = interpret_response(self.snapshot, utterance, mocked)
                self.assertIs(result.intent.action, expected_action)
                if utterance == "マスター-3dB":
                    self.assertEqual(result.intent.number.value, -3.0)
                    class DbBridge:
                        def __init__(self):
                            self.calls = []
                            self.value = 0.75

                        def run(inner_self, arguments):
                            arguments = list(arguments)
                            inner_self.calls.append(arguments)
                            if "--api-parameter-set" in arguments:
                                at = arguments.index("--api-parameter-set")
                                inner_self.value = float(arguments[at + 2])
                                return BridgeResult((), 1, 0, False)
                            if "--api-call" in arguments:
                                at = arguments.index("--api-call")
                                value = json.loads(arguments[at + 3])[0]
                                shown = f"{-60 + value * 60:.1f} dB"
                                return BridgeResult((Ack("api_call", arguments[at + 4], shown, arguments[at + 1], "str_for_value"),), 1, 0, False)
                            at = arguments.index("--api-mixer-status")
                            payload = {"parameters": {"volume": {"path": master_path, "value": inner_self.value, "min": 0, "max": 1}}}
                            return BridgeResult((Ack("api_mixer_status", arguments[at + 2], payload, "live_set master_track"),), 1, 0, False)

                    bridge = DbBridge()
                    service = LiveJevService(bridge=bridge, snapshot=self.snapshot, key="x", requester=lambda _p, _k: mocked)
                    with mock.patch("daemon.request_id", side_effect=lambda kind: kind):
                        self.assertEqual(service.process({"text": utterance})["kind"], "result")
                    self.assertEqual(bridge.calls, expected_arguments)
                    continue
                if expected_action is Action.NONE:
                    self.assertGreater(result.intent.needs_generation, 0.5)
                    class NoBridge:
                        def __init__(self):
                            self.calls = []

                        def run(inner_self, arguments):
                            inner_self.calls.append(list(arguments))
                            raise AssertionError("info must not call live.py")

                    bridge = NoBridge()
                    service = LiveJevService(bridge=bridge, snapshot=self.snapshot, key="x", requester=lambda _p, _k: mocked)
                    self.assertEqual(service.process({"text": utterance})["kind"], "info")
                    self.assertEqual(bridge.calls, expected_arguments)
                    continue
                with mock.patch("actions.request_id", side_effect=lambda kind: kind):
                    self.assertEqual(ACTIONS[expected_action].apply(self.snapshot, result.intent), expected_arguments)

    def test_number_parser_supports_units_destinations_and_pan_words(self) -> None:
        self.assertEqual(parse_number("-6 dBに", Action.VOLUME).unit, "db")
        self.assertEqual(parse_number("テンポを90へ", Action.TEMPO).value, 90)
        self.assertEqual(parse_number("右30", Action.PAN).value, 30)
        self.assertEqual(parse_number("左30", Action.PAN).value, -30)
        self.assertEqual(parse_number("20L", Action.PAN).value, -20)
        self.assertEqual(parse_number("L20", Action.PAN).unit, "pan")
        self.assertEqual(parse_number("パン20", Action.PAN).unit, "pan")
        self.assertEqual(parse_number("左20%", Action.PAN).unit, "percent")
        self.assertEqual(parse_number("センター", Action.PAN).value, 0)
        self.assertEqual(parse_number("−6dB", Action.VOLUME).value, -6)
        self.assertEqual(parse_number("－6dB", Action.VOLUME).value, -6)
        self.assertEqual(parse_number("ー6dB", Action.VOLUME).value, -6)
        self.assertEqual(parse_number("マスター6dB", Action.VOLUME).value, 6)
        self.assertEqual(parse_number("マイナス6dB", Action.VOLUME).value, -6)
        self.assertIsNone(parse_number(".5", Action.VOLUME))
        self.assertIsNone(parse_number("1e2", Action.TEMPO))
        self.assertIsNone(parse_number("abc12", Action.TEMPO))
        self.assertIsNone(parse_number("12abc", Action.TEMPO))
        self.assertIsNone(parse_number("90dB", Action.TEMPO))
        self.assertIsNone(parse_number("80%", Action.VOLUME))
        self.assertIsNone(parse_number("左30 dB", Action.PAN))
        self.assertIsNone(parse_number("右30 bpm", Action.PAN))
        self.assertIsNone(parse_number("xL30", Action.PAN))

    def test_pan_live_units_and_explicit_percent_use_different_scales(self) -> None:
        for utterance, expected in (("左20", -0.4), ("右20", 0.4), ("20L", -0.4), ("L20", -0.4), ("左20%", -0.2)):
            with self.subTest(utterance=utterance):
                intent = interpret_response(self.snapshot, utterance, response("pan", "t0", "set")).intent
                with mock.patch("actions.request_id", side_effect=lambda kind: kind):
                    arguments = ACTIONS[Action.PAN].apply(self.snapshot, intent)
                self.assertEqual(float(arguments[0][3]), expected)

    def test_bridge_track_stays_in_snapshot_but_is_not_a_candidate_or_local_name(self) -> None:
        bridge_track = Track(
            3, "Codex Bridge", 0.5, "-9.0 dB", 0.0, "C", False, False,
            (Device(0, "My LiveUdpBridge Device", (), "live_set tracks 3 devices 0"),),
            "live_set tracks 3",
        )
        snapshot = replace(self.snapshot, tracks=(*self.snapshot.tracks, bridge_track))
        with tempfile.TemporaryDirectory() as directory:
            aliases = Path(directory) / "aliases.json"
            aliases.write_text('{"Codex Bridge": ["ぶりっじ"]}', encoding="utf-8")
            with mock.patch("intent.ALIASES_PATH", aliases):
                payload = build_request(snapshot, "ぶりっじをミュート")
        self.assertEqual(len(payload["state"]["tracks"]), 4)
        self.assertNotIn("t3", payload["questions"]["track"]["criteria"])
        self.assertNotIn("ぶりっじ", str(payload["questions"]["track"]["criteria"]))
        self.assertIsNone(parse_local("Codex Bridgeをミュート", snapshot))

    def test_param_candidates_are_capped_at_250(self) -> None:
        from dataclasses import replace
        from snapshot import Device, Param

        params = tuple(Param(i, str(i), 0, 0, 1, "0", f"p{i}") for i in range(300))
        track = replace(self.snapshot.tracks[0], devices=(Device(0, "Huge", params, "d"),))
        payload = build_request(replace(self.snapshot, tracks=(track,)), "つまみ")
        self.assertEqual(len(payload["questions"]["param_t0"]["criteria"]), 251)
        self.assertEqual(len(candidate_params(replace(self.snapshot, tracks=(track,)))[0]), 250)

    def test_track_and_parameter_names_do_not_become_numbers(self) -> None:
        from dataclasses import replace

        named = replace(self.snapshot.tracks[0], name="808")
        snapshot = replace(self.snapshot, tracks=(named, *self.snapshot.tracks[1:]))
        result = interpret_response(snapshot, "808を下げて", response("volume", "t0", "down_small"))
        self.assertIsNone(result.intent.number)

    def test_local_parser_resolves_numbered_tracks_to_zero_based_index(self) -> None:
        for target in ("トラック3", "3番目", "3番目のトラック", "3番トラック"):
            with self.subTest(target=target):
                intent = parse_local(f"{target}の音量を-3dBに", self.snapshot)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.track, 2)
                self.assertEqual(intent.number.value, -3)

    def test_local_parser_requires_exact_track_name_case_insensitively(self) -> None:
        exact = parse_local("bassをミュート", self.snapshot)
        self.assertIsNotNone(exact)
        self.assertEqual(exact.track, 1)
        self.assertIsNone(parse_local("Basをミュート", self.snapshot))

    def test_local_parser_accepts_exact_named_restore_with_optional_particle(self) -> None:
        for utterance in ("Bass 戻して", "bassを戻して"):
            with self.subTest(utterance=utterance):
                intent = parse_local(utterance, self.snapshot)
                self.assertIsNotNone(intent)
                self.assertIs(intent.action, Action.NONE)
                self.assertEqual(intent.track, 1)
                self.assertGreater(intent.refers_previous, 0.6)
        self.assertIsNone(parse_local("Basを戻して", self.snapshot))

    def test_local_parser_removes_digit_bearing_track_name_before_value(self) -> None:
        from dataclasses import replace

        named = replace(self.snapshot.tracks[1], name="Bass 2")
        snapshot = replace(self.snapshot, tracks=(self.snapshot.tracks[0], named, self.snapshot.tracks[2]))
        intent = parse_local("Bass 2 の音量を -8dB に", snapshot)
        self.assertIsNotNone(intent)
        self.assertEqual(intent.track, 1)
        self.assertEqual(intent.number.value, -8)

    def test_parameter_answer_can_select_the_track(self) -> None:
        mocked = response("param", "none", "up_small", param="d0p0", track_conf=0.2)
        result = interpret_response(self.snapshot, "リバーブのDryWet上げて", mocked)
        self.assertEqual(result.intent.track, 0)
        self.assertEqual(result.intent.param.name, "Dry/Wet")

    def test_parameter_answer_uses_highest_confidence_track(self) -> None:
        from dataclasses import replace
        from snapshot import Device, Param

        second_param = Param(0, "Dry/Wet", 0.5, 0, 1, "50%", "live_set tracks 1 devices 0 parameters 0")
        second_device = Device(0, "Reverb", (second_param,), "live_set tracks 1 devices 0")
        second_track = replace(self.snapshot.tracks[1], devices=(second_device,))
        snapshot = replace(self.snapshot, tracks=(self.snapshot.tracks[0], second_track, self.snapshot.tracks[2]))
        mocked = response("param", "none", "up_small", param="d0p0", track_conf=0.2, param_conf=0.7)
        mocked["answers"]["param_t1"] = {
            "type": "choice", "choice": "d0p0", "confidence": 0.9, "probabilities": {"d0p0": 0.9, "none": 0.1},
        }
        result = interpret_response(snapshot, "リバーブのDryWet上げて", mocked)
        self.assertEqual(result.intent.track, 1)
        self.assertEqual(result.intent.param.path, second_param.path)

    def test_registry_covers_every_action(self) -> None:
        self.assertEqual(set(ACTIONS), set(Action))

    def test_get_that_build_commands_would_send_first_is_a_separate_call(self) -> None:
        mute = interpret_response(self.snapshot, "ドラムをミュート", response("mute", "t2")).intent
        tempo = interpret_response(self.snapshot, "テンポ90", response("tempo", step="set")).intent
        mute_calls = ACTIONS[Action.MUTE].apply(self.snapshot, mute)
        tempo_calls = ACTIONS[Action.TEMPO].apply(self.snapshot, tempo)
        self.assertEqual(len(mute_calls), 2)
        self.assertIn("--api-set", mute_calls[0])
        self.assertIn("--api-get", mute_calls[1])
        self.assertEqual(len(tempo_calls), 2)
        self.assertIn("--tempo", tempo_calls[0])
        self.assertIn("--api-get", tempo_calls[1])


if __name__ == "__main__":
    unittest.main()
