from __future__ import annotations

from dataclasses import replace
import json
import time
import unittest
from unittest import mock

import daemon as D
from daemon import LiveJevService, Pending
from intent import Action, IntentResult, Number, Step, TargetOrigin, _local_intent, parse_local, split_compound, interpret_response
from messages import MESSAGES, render
from snapshot import MasterTrack, ReturnTrack
from tests.support import StatefulLive, response, sample_snapshot
from tests.test_first_class_targets import TypedTargetBridge, detailed_target_snapshot


class FinalReviewPendingTests(unittest.TestCase):
    def _service_with_pending(self, result: IntentResult, field: str) -> tuple[StatefulLive, LiveJevService]:
        bridge = StatefulLive(selected=1)
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda *_: self.fail("Jev should not be called"),
        )
        service.pending = Pending(result, field, request_id="question")
        return bridge, service

    def test_action_clarification_restarts_named_command_instead_of_using_selected_track(self) -> None:
        unresolved = interpret_response(sample_snapshot(), "何かして", response("none", action_conf=0.2), ())
        bridge, service = self._service_with_pending(unresolved, "action")

        answer = service.process({"id": "answer", "answering": "question", "text": "Drumsをミュートして"})

        self.assertEqual(answer["kind"], "result")
        self.assertEqual(bridge.flags("mute"), {"Pad": False, "Bass": False, "Drums": True})
        self.assertIsNone(service.pending)

    def test_action_clarification_does_not_keep_old_named_track_for_new_command(self) -> None:
        pending = interpret_response(sample_snapshot(), "FX Busを何かして", response("none", "t0", action_conf=0.2), ())
        bridge, service = self._service_with_pending(pending, "action")

        answer = service.process({"id": "answer", "answering": "question", "text": "mute Drums"})

        self.assertEqual(answer["kind"], "result")
        self.assertEqual(bridge.flags("mute"), {"Pad": False, "Bass": False, "Drums": True})

    def test_track_clarification_restarts_complete_tempo_command(self) -> None:
        unresolved = interpret_response(sample_snapshot(), "mute", response("mute", "none"), ())
        bridge, service = self._service_with_pending(unresolved, "track")

        answer = service.process({"id": "answer", "answering": "question", "text": "set tempo to 130"})

        self.assertEqual(answer["kind"], "result")
        self.assertEqual(bridge.tempo, 130.0)
        self.assertIsNone(service.pending)

    def test_compound_command_clears_stale_clarification(self) -> None:
        unresolved = interpret_response(sample_snapshot(), "何かして", response("none", action_conf=0.2), ())
        bridge, service = self._service_with_pending(unresolved, "action")

        answer = service.process({"id": "chain", "text": "mute Pad and solo Drums"})

        self.assertEqual(answer["kind"], "result")
        self.assertTrue(bridge.flags("mute")["Pad"])
        self.assertTrue(bridge.flags("solo")["Drums"])
        self.assertIsNone(service.pending)


class FinalReviewMultipleTargetTests(unittest.TestCase):
    def test_english_toggle_with_two_literal_targets_never_returns_one_target(self) -> None:
        snapshot = sample_snapshot()

        for text in ("mute Pad and Bass", "mute Pad, Bass", "solo Drums and Bass"):
            with self.subTest(text=text):
                intent = parse_local(text, snapshot)
                self.assertIsNotNone(intent)
                self.assertIsNone(intent.track)
                self.assertFalse(intent.tracks)


class FinalReviewCompoundTests(unittest.TestCase):
    def test_value_setting_operation_is_split_as_its_own_clause(self) -> None:
        clauses = split_compound(
            "Kickをミュートして、Bassをソロにして、Padを-6dBにして",
            sample_snapshot(),
        )

        self.assertEqual(clauses, ["Kickをミュート", "Bassをソロに", "Padを-6dBにして"])

    def test_unsplit_connector_followed_by_different_target_refuses_everything(self) -> None:
        bridge = StatefulLive(selected=1)
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda *_: self.fail("unsafe compound must not reach Jev"),
        )

        answer = service.process({"text": "mute Pad and Bass"})

        self.assertEqual(answer["kind"], "info")
        self.assertEqual(answer["line"], render("info.one_at_a_time", lang="ja"))
        self.assertFalse(any(bridge.flags("mute").values()))

    def test_english_toggle_with_two_literal_targets_writes_nothing(self) -> None:
        for text in ("mute Pad and Bass", "mute Pad, Bass", "solo Drums and Bass"):
            with self.subTest(text=text):
                bridge = StatefulLive(selected=1)
                service = LiveJevService(
                    bridge=bridge,
                    snapshot=sample_snapshot(),
                    key="x",
                    requester=lambda *_: self.fail("ambiguous local command must not reach Jev"),
                )

                answer = service.process({"text": text})

                self.assertNotEqual(answer["kind"], "result")
                self.assertFalse(any(bridge.flags(prop)[name] for prop in ("mute", "solo") for name in bridge.names.values()))

    def test_japanese_toggle_with_two_literal_targets_keeps_refusing(self) -> None:
        snapshot = sample_snapshot()

        for text in ("PadとBassをミュート", "Pad、Bassをソロ"):
            with self.subTest(text=text):
                intent = parse_local(text, snapshot)
                self.assertIsNotNone(intent)
                self.assertIsNone(intent.track)
                self.assertFalse(intent.tracks)


class FinalReviewEnglishTargetTests(unittest.TestCase):
    @staticmethod
    def _snapshot_with_a_track():
        base = sample_snapshot()
        tracks = list(base.tracks)
        tracks[0] = replace(tracks[0], name="A", sends=(0.0,))
        tracks[1] = replace(tracks[1], sends=(0.0,))
        tracks[2] = replace(tracks[2], sends=(0.0,))
        returned = ReturnTrack(0, "A-Reverb", 0.5, "-6.0 dB", 0.0, "C", False, False, (), "live_set return_tracks 0")
        return replace(base, tracks=tuple(tracks), returns=(returned,))

    def test_send_letter_and_article_do_not_override_explicit_on_target(self) -> None:
        snapshot = self._snapshot_with_a_track()

        for text in ("send a up on bass", "send A to 50% on bass"):
            with self.subTest(text=text):
                intent = parse_local(text, snapshot)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.track, 1)

                bridge = StatefulLive(names=("A", "Bass", "Drums"), selected=0)
                service = LiveJevService(
                    bridge=bridge,
                    snapshot=snapshot,
                    key="x",
                    requester=lambda *_: self.fail("deterministic send must not reach Jev"),
                )
                answer = service.process({"text": text})

                self.assertEqual(answer["kind"], "result")
                self.assertGreater(bridge.sends[(1, 0)], 0.0)
                self.assertEqual(bridge.sends[(0, 0)], 0.0)

    def test_article_a_in_a_little_is_not_the_one_letter_track(self) -> None:
        intent = parse_local("lower a little", self._snapshot_with_a_track())

        self.assertIsNotNone(intent)
        self.assertIsNone(intent.track)


class FinalReviewAuthorizationTests(unittest.TestCase):
    @staticmethod
    def _target_snapshot():
        base = sample_snapshot()
        tracks = tuple(replace(track, sends=(0.0,)) for track in base.tracks)
        returned = ReturnTrack(0, "Hall", 0.5, "-6.0 dB", 0.0, "C", False, False, (), "live_set return_tracks 0")
        master = MasterTrack("Master", base.master_volume, base.master_display, ())
        return replace(base, tracks=tracks, returns=(returned,), master=master)

    def test_send_destination_literal_is_not_mistaken_for_source_track(self) -> None:
        service = LiveJevService(bridge=StatefulLive(), snapshot=self._target_snapshot())
        intent = _local_intent(
            Action.SEND,
            track=1,
            send=0,
            step=Step.UP_SMALL,
            target_origin=TargetOrigin.SELECTED,
            utterance="Hallへのセンドを上げて",
        )

        self.assertIsNone(service._authorize_target(intent))

    def test_resolved_target_must_be_one_of_all_literal_targets(self) -> None:
        service = LiveJevService(bridge=StatefulLive(), snapshot=self._target_snapshot())
        intent = _local_intent(
            Action.MUTE,
            track=1,
            target_origin=TargetOrigin.NAMED,
            track_stated=1.0,
            utterance="Master bus の Pad をミュート",
        )

        refusal = service._authorize_target(intent)

        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["line"], render("error.named_track_missing", lang="ja"))

    def test_master_device_name_change_blocks_parameter_write(self) -> None:
        snapshot = detailed_target_snapshot()
        bridge = TypedTargetBridge(snapshot)
        bridge.device_names["live_set master_track devices 0"] = "Utility"
        service = LiveJevService(
            bridge=bridge,
            snapshot=snapshot,
            key="x",
            requester=lambda *_: response("device_off", "none"),
        )
        service.reader = mock.Mock()
        service.reader.read.return_value = (snapshot, 1)

        answer = service.process({"text": "Limiterをオフ"})

        self.assertEqual(answer["line"], render("error.stale", lang="ja"))
        self.assertEqual(bridge.params["live_set master_track devices 0 parameters 0"], 1.0)
        self.assertFalse(any("--api-parameter-set" in call for call in bridge.calls))

    def test_return_device_name_change_blocks_parameter_write(self) -> None:
        snapshot = detailed_target_snapshot()
        bridge = TypedTargetBridge(snapshot)
        bridge.device_names["live_set return_tracks 0 devices 0"] = "Utility"
        service = LiveJevService(
            bridge=bridge,
            snapshot=snapshot,
            key="x",
            requester=lambda *_: response("device_off", "none"),
        )
        service.reader = mock.Mock()
        service.reader.read.return_value = (snapshot, 1)

        answer = service.process({"text": "Echoをオフ"})

        self.assertEqual(answer["line"], render("error.stale", lang="ja"))
        self.assertEqual(bridge.params["live_set return_tracks 0 devices 0 parameters 0"], 1.0)
        self.assertFalse(any("--api-parameter-set" in call for call in bridge.calls))

    def test_ordinary_track_rename_rechecks_fresh_name_before_write(self) -> None:
        class RenamedBridge(StatefulLive):
            def __init__(self):
                super().__init__()
                self.name_reads = 0

            def run(self, arguments):
                if "--api-get" in arguments and arguments[arguments.index("--api-get") + 2] == "name":
                    self.name_reads += 1
                    if self.name_reads % 2 == 0:
                        self.names[1] = "Other"
                return super().run(arguments)

        snapshot = sample_snapshot()
        bridge = RenamedBridge()
        service = LiveJevService(bridge=bridge, snapshot=snapshot)
        service.reader = mock.Mock()
        service.reader.read.return_value = (snapshot, 1)

        answer = service.process({"text": "rename Bass to Low End"})

        self.assertEqual(answer["line"], render("error.stale", lang="ja"))
        self.assertFalse(any("--rename-track-index" in call for call in bridge.calls))

    def test_return_rename_rechecks_fresh_name_before_write(self) -> None:
        class RenamedReturnBridge(TypedTargetBridge):
            def __init__(self, snapshot):
                super().__init__(snapshot)
                self.name_reads = 0

            def run(self, arguments):
                if "--api-get" in arguments and arguments[arguments.index("--api-get") + 1] == "live_set return_tracks 0":
                    self.name_reads += 1
                    if self.name_reads % 2 == 0:
                        self.names["live_set return_tracks 0"] = "Other"
                return super().run(arguments)

        snapshot = detailed_target_snapshot()
        bridge = RenamedReturnBridge(snapshot)
        service = LiveJevService(bridge=bridge, snapshot=snapshot)
        service.reader = mock.Mock()
        service.reader.read.return_value = (snapshot, 1)

        answer = service.process({"text": "Hallの名前をPlateにして"})

        self.assertEqual(answer["line"], render("error.stale", lang="ja"))
        self.assertFalse(any("--api-set" in call for call in bridge.calls))

    def test_rename_destination_is_not_treated_as_a_literal_target(self) -> None:
        service = LiveJevService(bridge=StatefulLive(), snapshot=sample_snapshot())
        for utterance in ("rename track 2 to Drums", "2番トラックの名前をDrumsにして"):
            with self.subTest(utterance=utterance):
                intent = _local_intent(
                    Action.RENAME,
                    track=1,
                    text="Drums",
                    target_origin=TargetOrigin.NAMED,
                    track_stated=1.0,
                    utterance=utterance,
                )
                self.assertIsNone(service._authorize_target(intent))

    def test_inserted_device_name_is_not_treated_as_a_literal_target(self) -> None:
        service = LiveJevService(bridge=StatefulLive(), snapshot=sample_snapshot())
        cases = (
            _local_intent(
                Action.INSERT_PLUGIN,
                track=1,
                plugin="Drums",
                target_origin=TargetOrigin.NAMED,
                track_stated=1.0,
                utterance="insert Drums on track 2",
            ),
            _local_intent(
                Action.INSERT_PLUGIN,
                track=1,
                native_device="Drums",
                target_origin=TargetOrigin.NAMED,
                track_stated=1.0,
                utterance="2番トラックにDrums入れて",
            ),
        )
        for intent in cases:
            with self.subTest(utterance=intent.utterance):
                self.assertIsNone(service._authorize_target(intent))

    def test_non_destination_literal_track_still_refuses_wrong_resolved_target(self) -> None:
        service = LiveJevService(bridge=StatefulLive(), snapshot=sample_snapshot())
        intent = _local_intent(
            Action.RENAME,
            track=0,
            text="Bass",
            target_origin=TargetOrigin.NAMED,
            track_stated=1.0,
            utterance="Drumsの名前をBassにして",
        )

        refusal = service._authorize_target(intent)

        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["line"], render("error.named_track_missing", lang="ja"))


class FinalReviewPartialMatchTests(unittest.TestCase):
    @staticmethod
    def _snapshot_with_a_track():
        base = sample_snapshot()
        tracks = list(base.tracks)
        tracks[0] = replace(tracks[0], name="A")
        return replace(base, tracks=tuple(tracks))

    def test_setting_words_and_partial_names_reach_jev(self) -> None:
        snapshot = self._snapshot_with_a_track()

        for text in ("センドAを上げて", "BassのセンドAを上げて", "Bassのリバーブを上げて", "Bassを六dB下げて"):
            with self.subTest(text=text):
                requester = mock.Mock(return_value=response("none", action_conf=0.2))
                service = LiveJevService(
                    bridge=StatefulLive(names=("A", "Bass", "Drums"), selected=1),
                    snapshot=snapshot,
                    key="x",
                    requester=requester,
                )

                answer = service.process({"text": text})

                requester.assert_called_once()
                self.assertNotEqual(answer["kind"], "result")


class FinalReviewSmallSafetyTests(unittest.TestCase):
    @staticmethod
    def _send_snapshot():
        base = sample_snapshot()
        tracks = tuple(replace(track, sends=(0.2,)) for track in base.tracks)
        returned = ReturnTrack(0, "Verb", 0.5, "-6.0 dB", 0.0, "C", False, False, (), "live_set return_tracks 0")
        return replace(base, tracks=tracks, returns=(returned,))

    def test_manual_change_undo_message_shows_current_value(self) -> None:
        snapshot = self._send_snapshot()
        bridge = StatefulLive()
        bridge.sends[(0, 0)] = 0.2
        service = LiveJevService(
            bridge=bridge,
            snapshot=snapshot,
            key="x",
            requester=lambda *_: self.fail("local send must not reach Jev"),
        )
        self.assertEqual(service.process({"text": "Pad send A to 50%"})["kind"], "result")
        bridge.sends[(0, 0)] = 0.7

        answer = service.process({"cmd": "undo"})

        self.assertEqual(answer["kind"], "info")
        self.assertIn("0.7", answer["line"])
        self.assertNotIn("?", answer["line"])

    def test_negative_db_out_of_range_uses_range_message(self) -> None:
        class DbRangeBridge(StatefulLive):
            def run(self, arguments):
                arguments = list(arguments)
                if "--api-call" in arguments:
                    at = arguments.index("--api-call")
                    path, method, raw_args, request = arguments[at + 1:at + 5]
                    value = float(json.loads(raw_args)[0])
                    display = "-inf dB" if value == 0.0 else f"{-70.0 + 76.0 * value:g} dB"
                    return self._result((self._ack("api_call", request, display, path, method),), 1, 0, False)
                return super().run(arguments)

        service = LiveJevService(
            bridge=DbRangeBridge(),
            snapshot=sample_snapshot(),
            key="x",
        )
        intent = _local_intent(
            Action.VOLUME,
            track=1,
            step=Step.SET,
            number=Number(-200.0, "db"),
            target_origin=TargetOrigin.NAMED,
            track_stated=1.0,
            utterance="Bassを-200dBにして",
        )

        answer = service._execute(intent, "range", 0, 0, time.perf_counter(), intent.utterance, None)

        self.assertEqual(answer["kind"], "error")
        self.assertTrue(answer["line"].startswith("その値にはできません"), answer)
        self.assertNotEqual(answer["line"], render("error.live", lang="ja"))

    def test_input_over_500_characters_returns_info_without_parsing(self) -> None:
        service = LiveJevService(bridge=StatefulLive(), snapshot=sample_snapshot(), key="x")

        with mock.patch.object(D, "split_compound", side_effect=AssertionError("long input was parsed")):
            answer = service.process({"text": "x" * 501})

        self.assertEqual(answer["kind"], "info")
        self.assertEqual(answer["line"], render("info.input_too_long", lang="ja"))
        self.assertIn("info.input_too_long", MESSAGES)
        self.assertEqual(set(MESSAGES["info.input_too_long"]), {"ja", "en"})

    def test_receipt_undo_success_keeps_prefix_and_names_action(self) -> None:
        service = LiveJevService(
            bridge=StatefulLive(),
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda *_: self.fail("local mute must not reach Jev"),
        )
        self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")

        answer = service.process({"cmd": "undo"})

        self.assertEqual(answer["kind"], "result")
        self.assertTrue(answer["line"].startswith(render("readback.text.undo", lang="ja")))
        self.assertIn("Bass", answer["line"])
        self.assertIn("ミュート", answer["line"])

    def test_gemini_worker_uses_own_jev_client_for_default_requester(self) -> None:
        service = LiveJevService(
            bridge=StatefulLive(),
            snapshot=sample_snapshot(),
            key="jev-key",
            requester=D.request_jev,
            llm_key="gemini-key",
            rewriter=lambda *_: "make the low end silent",
        )
        initial = IntentResult(_local_intent(Action.NONE, utterance="make it quiet"), (), (), ())
        worker_requester = mock.Mock(return_value=response("mute", "t1"))
        shared_requester = mock.Mock(side_effect=AssertionError("shared Jev client used in worker"))

        with mock.patch.object(D, "JevClient", return_value=worker_requester) as client_type, \
             mock.patch.object(D, "_DEFAULT_JEV", shared_requester):
            answer = service._rewrite_and_process(
                "make it quiet",
                initial,
                {"id": "rewrite", "kind": "ask", "line": "fallback"},
                "rewrite",
                0,
                time.perf_counter(),
            )

        self.assertIn(answer["kind"], {"ask", "confirm"})
        self.assertEqual(answer.get("via"), "gemini")
        client_type.assert_called_once_with()
        worker_requester.assert_called_once()
        shared_requester.assert_not_called()


if __name__ == "__main__":
    unittest.main()
