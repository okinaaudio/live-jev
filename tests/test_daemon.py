from __future__ import annotations

from dataclasses import replace
import io
import json
import os
import subprocess
import sys
import time
from unittest import mock
import unittest

from daemon import JevClient, LiveJevService, Pending, StaleSnapshot, run_stdio
from bridge_client import Ack, BridgeError, BridgeResult
from intent import Action, Number, Step, interpret_response, _local_intent
from llm_rewrite import RewriteFailure
from snapshot import Clip, Scene
from tests.support import StatefulLive, choice, response, sample_snapshot


class NoWriteBridge:
    """Clarification may read the selected track through session context but must not write anything."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments):
        arguments = list(arguments)
        self.calls.append(arguments)
        if "--api-session-context" in arguments:
            return BridgeResult((Ack("api_session_context", arguments[-1], {"song": {}, "selected": {}}),), 1, 0, False)
        raise AssertionError("聞き返し中に bridge を呼んだ")


class DaemonDecisionTests(unittest.TestCase):
    class BoolBridge:
        def __init__(self, *, fail_on_write: int | None = None) -> None:
            self.calls: list[list[str]] = []
            self.write_count = 0
            self.fail_on_write = fail_on_write
            self.values = {}

        def run(self, arguments):
            arguments = list(arguments)
            self.calls.append(arguments)
            if "--api-set" in arguments:
                self.write_count += 1
                if self.write_count == self.fail_on_write:
                    raise BridgeError("write failed")
                for at, item in enumerate(arguments):
                    if item == "--api-set":
                        self.values[(arguments[at + 1], arguments[at + 2])] = arguments[at + 3] == "1"
                return BridgeResult((), 1, 0, False)
            if "--api-get" in arguments:
                names = {"live_set tracks 0": "Pad", "live_set tracks 1": "Bass", "live_set tracks 2": "Drums"}
                acks = []
                for at, item in enumerate(arguments):
                    if item != "--api-get":
                        continue
                    path, prop, request = arguments[at + 1:at + 4]
                    payload = names[path] if prop == "name" else self.values.get((path, prop), False)
                    acks.append(Ack("api_get", request, payload, path, prop))
                return BridgeResult(tuple(acks), 1, 0, False)
            raise AssertionError(arguments)

    def test_llm_rewrite_requires_confirmation_before_write(self) -> None:
        bridge = self.BoolBridge()
        jev_replies = iter([
            response("none", "t2", action_conf=0.2),
            response("mute", "t2"),
        ])
        rewrite = mock.Mock(return_value="ドラムをミュート")
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: next(jev_replies),
            llm_key="gemini-key",
            rewriter=rewrite,
        )
        answer = service.process({"id": "rewrite-1", "text": "ドラムを消しといて"})
        self.assertEqual(answer["kind"], "confirm")
        self.assertFalse(service.snapshot.tracks[2].mute)
        self.assertEqual(bridge.write_count, 0)
        self.assertIn("llm", answer["ms"])
        rewrite.assert_called_once()
        self.assertEqual(service.pending_confirm[0].action_conf, 0.2)

        applied = service.process({"id": "rewrite-1", "confirm": True})
        self.assertEqual(applied["kind"], "result")
        self.assertTrue(service.snapshot.tracks[2].mute)
        self.assertEqual(applied["decision"]["utterance"], "ドラムを消しといて")
        self.assertEqual(applied["decision"]["rewritten"], ["ドラムをミュート"])

    def test_llm_rewrite_cannot_produce_dispatch_only_actions(self) -> None:
        for rewritten, pending_track in (("undo", False), ("redo", False), ("undo", True)):
            with self.subTest(rewritten=rewritten, pending_track=pending_track):
                bridge = StatefulLive()
                service = LiveJevService(
                    bridge=bridge,
                    snapshot=sample_snapshot(),
                    key="x",
                    requester=lambda *_: response("none", action_conf=0.2),
                    llm_key="gemini-key",
                    rewriter=mock.Mock(return_value=rewritten),
                )
                self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")
                if pending_track:
                    unresolved = interpret_response(sample_snapshot(), "solo", response("solo", "none"), ())
                    service.pending = Pending(unresolved, "track", request_id="question")
                    message = {"id": "answer", "answering": "question", "text": "the low end one"}
                else:
                    message = {"id": "rewrite-undo", "text": "put it back how it was before"}

                answer = service.process(message)

                self.assertEqual(answer["kind"], "ask")
                self.assertTrue(answer["line"].startswith("わかりませんでした。別の言い方で言ってみてください。"))
                self.assertTrue(bridge.state[(1, "mute")])
                self.assertIsNone(service.pending_confirm)

    def test_llm_descriptive_plugin_rewrite_requires_named_confirmation(self) -> None:
        bridge = StatefulLive(selected=0)
        rewrite = mock.Mock(return_value="EQ Eightを入れて")
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=mock.Mock(return_value=response("none", action_conf=0.2)),
            llm_key="gemini-key",
            rewriter=rewrite,
        )
        with mock.patch.object(service, "_plugin_names", return_value=("FabFilter Pro-Q 3",)):
            answer = service.process({"id": "descriptive-eq", "text": "8バンドのEQを入れて"})

        self.assertEqual(answer["kind"], "confirm")
        self.assertIn("EQ Eight", answer["line"])
        self.assertFalse(any("--write" in call for call in bridge.calls))
        self.assertEqual(service.pending_confirm[0].plugin, "EQ Eight")
        args = rewrite.call_args.args
        self.assertEqual(args[3], ("FabFilter Pro-Q 3",))
        self.assertIn("EQ Eight", args[4])

    def test_plugin_fallback_does_not_override_missing_named_track_refusal(self) -> None:
        missing = response("insert_plugin")
        missing["answers"]["track_stated"]["noul"] = 0.9
        requester = mock.Mock(side_effect=[
            missing,
            {"answers": {"plugin": choice("Operator")}},
        ])
        service = LiveJevService(
            bridge=StatefulLive(selected=0),
            snapshot=sample_snapshot(),
            key="x",
            requester=requester,
        )

        with mock.patch.object(service, "_plugin_names", return_value=("Operator",)), \
             mock.patch.object(service, "_run_plugin_flow") as run_plugin:
            answer = service.process({"id": "missing-plugin-track", "text": "Ghost needs a synth"})

        self.assertEqual(answer["kind"], "error")
        self.assertEqual(answer["line"], "指定したトラックが見つかりません")
        self.assertNotIn("via", answer)
        requester.assert_called_once()
        run_plugin.assert_not_called()

    def test_bare_plugin_route_preserves_jev_named_target_refusal(self) -> None:
        snapshot = sample_snapshot()
        tracks = list(snapshot.tracks)
        tracks[0] = replace(tracks[0], name="Serum Bass")
        snapshot = replace(snapshot, tracks=tuple(tracks))
        answer_from_jev = response("none", "t0", action_conf=0.2)
        answer_from_jev["answers"]["track_stated"]["noul"] = 0.9
        service = LiveJevService(
            bridge=StatefulLive(names=("Serum Bass", "Bass", "Drums"), selected=1),
            snapshot=snapshot,
            key="x",
            requester=mock.Mock(return_value=answer_from_jev),
        )

        with mock.patch.object(service, "_plugin_names", return_value=("Serum 2",)), \
             mock.patch.object(service, "_run_plugin_flow") as run_plugin:
            answer = service.process({"text": "Serum"})

        self.assertNotEqual(answer["kind"], "result")
        run_plugin.assert_not_called()

    def test_ambiguous_plugin_answer_preserves_jev_named_target_evidence(self) -> None:
        snapshot = sample_snapshot()
        tracks = list(snapshot.tracks)
        tracks[0] = replace(tracks[0], name="Serum Bass")
        snapshot = replace(snapshot, tracks=tuple(tracks))
        answer_from_jev = response("none", "t0", action_conf=0.2)
        answer_from_jev["answers"]["track_stated"]["noul"] = 0.9
        service = LiveJevService(
            bridge=StatefulLive(names=("Serum Bass", "Bass", "Drums"), selected=1),
            snapshot=snapshot,
            key="x",
            requester=mock.Mock(return_value=answer_from_jev),
        )

        with mock.patch.object(service, "_plugin_names", return_value=("Serum 2", "Serum FX")), \
             mock.patch.object(service, "_run_plugin_flow") as run_plugin:
            question = service.process({"id": "plugin-question", "text": "Serum"})
            answer = service.process({"id": "plugin-answer", "answering": question["id"], "text": "Serum 2"})

        self.assertEqual(question["kind"], "ask")
        self.assertNotEqual(answer["kind"], "result")
        run_plugin.assert_not_called()

    def test_plugin_rewrite_preserves_uncertain_named_track_evidence(self) -> None:
        uncertain = response("insert_plugin", track_conf=0.4)
        uncertain["answers"]["track_stated"]["noul"] = 0.4
        requester = mock.Mock(side_effect=[
            uncertain,
            {"answers": {"plugin": choice("none")}},
        ])
        service = LiveJevService(
            bridge=StatefulLive(selected=0),
            snapshot=sample_snapshot(),
            key="x",
            requester=requester,
            llm_key="gemini-key",
            rewriter=mock.Mock(return_value="EQ Eightを入れて"),
        )

        with mock.patch.object(service, "_plugin_names", return_value=("FabFilter Pro-Q 3",)), \
             mock.patch.object(service, "_run_plugin_flow") as run_plugin:
            answer = service.process({"id": "uncertain-plugin-track", "text": "8バンドのEQを入れて"})

        self.assertNotEqual(answer["kind"], "confirm")
        self.assertIsNone(service.pending_confirm)
        run_plugin.assert_not_called()

    def test_confirmed_plugin_insertion_replaces_prior_receipt_history(self) -> None:
        bridge = StatefulLive(selected=0)
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=mock.Mock(return_value=response("none", action_conf=0.2)),
            llm_key="gemini-key",
            rewriter=mock.Mock(return_value="EQ Eightを入れて"),
        )
        self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")
        self.assertTrue(bridge.state[(1, "mute")])

        with mock.patch.object(service, "_plugin_names", return_value=("EQ Eight",)), \
             mock.patch.object(service, "_run_plugin_flow", return_value=0):
            confirmation = service.process({"id": "confirm-plugin", "text": "8バンドのEQを入れて"})
            self.assertEqual(confirmation["kind"], "confirm")
            self.assertEqual(service.process({"id": "confirm-plugin", "confirm": True})["kind"], "result")

        undo = service.process({"cmd": "undo"})

        self.assertEqual(undo["kind"], "info")
        self.assertTrue(bridge.state[(1, "mute")])

    def test_llm_scene_confirmation_names_resolved_scene(self) -> None:
        snapshot = replace(
            sample_snapshot(),
            scenes=(Scene(0, "Intro", "live_set scenes 0"), Scene(1, "Verse", "live_set scenes 1")),
        )
        initial = response("launch_scene")
        candidate = response("launch_scene")
        candidate["answers"]["scene"] = choice("s1")
        service = LiveJevService(
            bridge=NoWriteBridge(),
            snapshot=snapshot,
            key="x",
            requester=mock.Mock(side_effect=[initial, candidate]),
            llm_key="gemini-key",
            rewriter=mock.Mock(return_value="シーン2を再生"),
        )

        answer = service.process({"id": "scene-rewrite", "text": "Chorus scene please"})

        self.assertEqual(answer["kind"], "confirm")
        self.assertTrue("Verse" in answer["line"] or "シーン2" in answer["line"], answer["line"])

    def test_forced_confirmation_renders_every_rewrite_target_field(self) -> None:
        snapshot = sample_snapshot()
        tracks = list(snapshot.tracks)
        tracks[0] = replace(
            tracks[0],
            clips=(Clip(0, "Pad Loop", "live_set tracks 0 clip_slots 0 clip"),),
            sends=(0.0,),
        )
        snapshot = replace(
            snapshot,
            tracks=tuple(tracks),
            scenes=(Scene(1, "Verse", "live_set scenes 1"),),
            returns=("Verb",),
        )
        service = LiveJevService(bridge=NoWriteBridge(), snapshot=snapshot, key="x", requester=mock.Mock())
        device = snapshot.tracks[0].devices[0]
        parameter = device.params[0]
        cases = (
            ("track", _local_intent(Action.MUTE, track=0), "Pad"),
            ("parameter", replace(_local_intent(Action.PARAM, track=0, step=Step.UP_SMALL), param=parameter, param_conf=1.0), "Dry/Wet"),
            ("device", replace(_local_intent(Action.DEVICE_OFF, track=0), device=device, device_conf=1.0, param=parameter, param_conf=1.0), "Reverb"),
            ("clip", replace(_local_intent(Action.CLIP_LOOP_OFF, track=0, clip=0), clip_name="Pad Loop"), "Pad Loop"),
            ("send", _local_intent(Action.SEND, track=0, send=0, step=Step.SET, number=Number(50, "percent")), "Send 1"),
            ("scene", _local_intent(Action.LAUNCH_SCENE, scene=1), "Verse"),
            ("plugin", _local_intent(Action.INSERT_PLUGIN, track=0, plugin="EQ Eight"), "EQ Eight"),
            ("value", _local_intent(Action.TEMPO, step=Step.SET, number=Number(128, "bpm")), "128 bpm"),
            ("direction", _local_intent(Action.VOLUME, track=0, step=Step.UP_SMALL), "少し上げる"),
        )

        for name, intent, expected in cases:
            with self.subTest(field=name):
                answer = service._execute(intent, name, 0, 0, time.perf_counter(), "rewrite", ["rewrite"], force_confirm=True)
                self.assertIn(expected, answer["line"])

    def test_llm_rejects_plugin_name_outside_catalog(self) -> None:
        bridge = StatefulLive(selected=0)
        rewrite = mock.Mock(return_value="Imaginary Synthを入れて")
        requester = mock.Mock(side_effect=[
            response("insert_plugin", action_conf=0.95),
            {"answers": {"plugin": choice("none")}},
        ])
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=requester,
            llm_key="gemini-key",
            rewriter=rewrite,
        )
        with mock.patch.object(service, "_plugin_names", return_value=("Serum 2",)):
            answer = service.process({"text": "ふわっとするリバーブを足して"})

        self.assertEqual(answer["line"], "わかりませんでした。別の言い方で言ってみてください。")
        self.assertIsNone(service.pending_confirm)
        self.assertFalse(any("--write" in call for call in bridge.calls))
        rewrite.assert_called_once()

    def test_unresolved_plugin_after_jev_pick_reaches_llm_once_and_requires_confirmation(self) -> None:
        bridge = StatefulLive(selected=0)
        rewrite = mock.Mock(return_value="ValhallaVintageVerbを入れて")
        requester = mock.Mock(side_effect=[
            response("insert_plugin", action_conf=0.95),
            {"answers": {"plugin": choice("none")}},
        ])
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=requester,
            llm_key="gemini-key",
            rewriter=rewrite,
        )

        with mock.patch.object(service, "_plugin_names", return_value=("ValhallaVintageVerb",)):
            answer = service.process({"id": "descriptive-reverb", "text": "ふわっとするリバーブを足して"})

        self.assertEqual(answer["kind"], "confirm")
        self.assertIn("ValhallaVintageVerb", answer["line"])
        self.assertEqual(service.pending_confirm[0].plugin, "ValhallaVintageVerb")
        self.assertFalse(any("--write" in call for call in bridge.calls))
        rewrite.assert_called_once()

    def test_locally_resolved_plugin_names_do_not_reach_llm(self) -> None:
        for utterance in ("EQ Eightを入れて", "eq eigetを入れて"):
            with self.subTest(utterance=utterance):
                bridge = StatefulLive(selected=0)
                rewrite = mock.Mock(return_value="不明")
                requester = mock.Mock(side_effect=AssertionError("Jev must not be called"))
                service = LiveJevService(
                    bridge=bridge,
                    snapshot=sample_snapshot(),
                    key="x",
                    requester=requester,
                    llm_key="gemini-key",
                    rewriter=rewrite,
                )

                with mock.patch.object(service, "_plugin_names", return_value=("EQ Eight",)):
                    answer = service.process({"text": utterance})

                self.assertEqual(answer["decision"]["action"], "insert_plugin")
                self.assertNotIn("via", answer)
                rewrite.assert_not_called()
                requester.assert_not_called()

    def test_unresolved_plugin_without_gemini_key_keeps_not_found_reply(self) -> None:
        rewrite = mock.Mock(return_value="ValhallaVintageVerbを入れて")
        requester = mock.Mock(side_effect=[
            response("insert_plugin", action_conf=0.95),
            {"answers": {"plugin": choice("none")}},
        ])
        service = LiveJevService(
            bridge=StatefulLive(selected=0),
            snapshot=sample_snapshot(),
            key="x",
            requester=requester,
            llm_key=None,
            rewriter=rewrite,
        )

        with mock.patch.object(service, "_plugin_names", return_value=("ValhallaVintageVerb",)):
            answer = service.process({"text": "ふわっとするリバーブを足して"})

        self.assertEqual(answer["kind"], "info")
        self.assertEqual(answer["line"], "「ふわっとするリバーブ」に当たるプラグインを見つけられませんでした")
        self.assertNotIn("via", answer)
        self.assertIsNone(service.pending_confirm)
        rewrite.assert_not_called()

    def test_no_gemini_key_never_calls_rewriter_for_local_and_unresolved_requests(self) -> None:
        def fail_rewriter(*_args):
            raise AssertionError("Gemini was called without a key")

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertNotIn("LIVE_JEV_LLM", os.environ)
            mixer = LiveJevService(
                bridge=self.BoolBridge(), snapshot=sample_snapshot(), key="x",
                requester=mock.Mock(), llm_key=None, rewriter=fail_rewriter,
            )
            mixer_answer = mixer.process({"text": "Bassをミュート"})
            self.assertEqual(mixer_answer["kind"], "result")
            self.assertNotIn("via", mixer_answer)

            bare = LiveJevService(
                bridge=StatefulLive(selected=0), snapshot=sample_snapshot(), key="x",
                requester=mock.Mock(side_effect=AssertionError("Jev was called")), llm_key=None, rewriter=fail_rewriter,
            )
            with mock.patch.object(bare, "_plugin_names", return_value=("Serum 2",)), \
                 mock.patch.object(bare, "_run_plugin_flow", return_value=0):
                bare_answer = bare.process({"text": "Serum 2"})
            self.assertEqual(bare_answer["kind"], "result")
            self.assertNotIn("via", bare_answer)

            unresolved = LiveJevService(
                bridge=NoWriteBridge(), snapshot=sample_snapshot(), key="x",
                requester=lambda *_: response("none", action_conf=0.2), llm_key=None, rewriter=fail_rewriter,
            )
            unresolved_answer = unresolved.process({"text": "ドラムをいい感じにして"})
            self.assertEqual(unresolved_answer["kind"], "ask")
            self.assertNotIn("via", unresolved_answer)

            insertion = LiveJevService(
                bridge=StatefulLive(selected=0), snapshot=sample_snapshot(), key="x",
                requester=mock.Mock(side_effect=[
                    response("insert_plugin", action_conf=0.95),
                    {"answers": {"plugin": choice("none")}},
                ]),
                llm_key=None, rewriter=fail_rewriter,
            )
            with mock.patch.object(insertion, "_plugin_names", return_value=("ValhallaVintageVerb",)):
                answer = insertion.process({"text": "ふわっとするリバーブを足して"})
            self.assertEqual(answer["kind"], "info")
            self.assertIn("見つけられませんでした", answer["line"])

    def test_llm_descriptive_builtin_on_new_track_keeps_stated_scope(self) -> None:
        bridge = StatefulLive(selected=0)
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=mock.Mock(return_value=response("none", action_conf=0.2)),
            llm_key="gemini-key",
            rewriter=mock.Mock(return_value="EQ Eight入りの新しいトラックを作る"),
        )
        with mock.patch.object(service, "_plugin_names", return_value=("Serum 2",)):
            answer = service.process({"text": "8バンドのEQ入りの新しいトラックを作って"})

        self.assertEqual(answer["kind"], "confirm")
        self.assertEqual(answer["via"], "gemini")
        self.assertIn("EQ Eight", answer["line"])
        self.assertIn("トラック", answer["line"])
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_llm_plugin_rewrite_cannot_add_unstated_scope_direction_or_number(self) -> None:
        rewrites = (
            "EQ Eight入りの新しいトラックを作る",
            "EQ Eightを入れて、音量を上げる",
            "EQ Eightを3個入れて",
        )
        for rewritten in rewrites:
            with self.subTest(rewritten=rewritten):
                bridge = StatefulLive(selected=0)
                service = LiveJevService(
                    bridge=bridge,
                    snapshot=sample_snapshot(),
                    key="x",
                    requester=mock.Mock(return_value=response("none", action_conf=0.2)),
                    llm_key="gemini-key",
                    rewriter=mock.Mock(return_value=rewritten),
                )
                with mock.patch.object(service, "_plugin_names", return_value=("EQ Eight",)):
                    answer = service.process({"text": "8バンドのEQを入れて"})

                self.assertTrue(answer["line"].startswith("わかりませんでした。別の言い方で言ってみてください。"))
                self.assertEqual(answer["via"], "gemini")
                self.assertIsNone(service.pending_confirm)
                self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_llm_unknown_falls_back_to_ask(self) -> None:
        bridge = NoWriteBridge()
        rewrite = mock.Mock(return_value="不明")
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("none", action_conf=0.2),
            llm_key="gemini-key",
            rewriter=rewrite,
        )
        answer = service.process({"text": "いい感じにして"})
        self.assertEqual(answer["kind"], "ask")
        self.assertEqual(answer["line"], "わかりませんでした。別の言い方で言ってみてください。 何をしますか？")
        self.assertEqual(answer["via"], "gemini")
        self.assertEqual([call for call in bridge.calls if "--api-session-context" not in call], [])
        rewrite.assert_called_once()

    def test_llm_transport_error_is_returned_without_being_rewritten(self) -> None:
        service = LiveJevService(
            bridge=NoWriteBridge(),
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("none", action_conf=0.2),
            llm_key="gemini-key",
            rewriter=mock.Mock(side_effect=RewriteFailure("quota")),
        )
        answer = service.process({"id": "m1", "text": "いい感じにして"})
        self.assertEqual(answer["kind"], "ask")
        self.assertEqual(answer["line"], "Geminiの利用上限に達しました。 何をしますか？")
        self.assertEqual(answer["id"], "m1")
        self.assertEqual(answer["via"], "gemini")
        self.assertIsNotNone(service.pending)

    def test_llm_rewriter_rejects_multiple_commands(self) -> None:
        bridge = self.BoolBridge()
        jev_replies = iter([response("none", "t2", action_conf=0.2)])
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: next(jev_replies),
            llm_key="gemini-key",
            rewriter=lambda _snapshot, _text, _key: "ドラムをミュート\nベースをソロ",
        )
        answer = service.process({"text": "ドラムをいい感じにして"})
        self.assertEqual(answer["kind"], "ask")
        self.assertEqual(answer["line"], "わかりませんでした。別の言い方で言ってみてください。 何をしますか？")
        self.assertEqual(answer["via"], "gemini")
        self.assertFalse(service.snapshot.tracks[2].mute)
        self.assertEqual(bridge.write_count, 0)

    def test_llm_corrects_misspelled_track_then_requires_confirmation(self) -> None:
        bridge = self.BoolBridge()
        missing = response("mute")
        missing["answers"]["track_stated"]["noul"] = 0.9
        rewrite = mock.Mock(return_value="Drumsをミュート")
        requester = mock.Mock(side_effect=[missing, response("mute", "t2")])
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=requester,
            llm_key="gemini-key",
            rewriter=rewrite,
        )

        answer = service.process({"id": "typo", "text": "Dromsをミュート"})

        self.assertEqual(answer["kind"], "confirm")
        self.assertEqual(answer["via"], "gemini")
        self.assertIn("Drums", answer["line"])
        self.assertEqual(bridge.write_count, 0)
        rewrite.assert_called_once()
        applied = service.process({"id": "typo", "confirm": True})
        self.assertEqual(applied["kind"], "result")
        self.assertEqual(applied["via"], "gemini")
        self.assertEqual(bridge.write_count, 1)

    def test_llm_rejects_track_not_in_snapshot(self) -> None:
        bridge = self.BoolBridge()
        missing = response("mute")
        missing["answers"]["track_stated"]["noul"] = 0.9
        rewrite = mock.Mock(return_value="Ghostをミュート")
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=mock.Mock(side_effect=[missing, response("mute", "t99")]),
            llm_key="gemini-key",
            rewriter=rewrite,
        )

        answer = service.process({"text": "Gohstをミュート"})

        self.assertEqual(answer["kind"], "error")
        self.assertTrue(answer["line"].startswith("わかりませんでした。別の言い方で言ってみてください。"))
        self.assertEqual(bridge.write_count, 0)
        rewrite.assert_called_once()

    def test_llm_rejects_direction_the_owner_did_not_say(self) -> None:
        bridge = self.BoolBridge()
        rewrite = mock.Mock(return_value="Drumsの音量を上げる")
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=mock.Mock(side_effect=[response("volume", "t2"), response("volume", "t2", "up_small")]),
            llm_key="gemini-key",
            rewriter=rewrite,
        )

        answer = service.process({"text": "Drumsの音量を調整して"})

        self.assertEqual(answer["kind"], "ask")
        self.assertTrue(answer["line"].startswith("わかりませんでした。別の言い方で言ってみてください。"))
        self.assertEqual(bridge.write_count, 0)
        rewrite.assert_called_once()

    def test_llm_rejects_number_the_owner_did_not_say(self) -> None:
        bridge = self.BoolBridge()
        rewrite = mock.Mock(return_value="テンポを120に")
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=mock.Mock(return_value=response("tempo")),
            llm_key="gemini-key",
            rewriter=rewrite,
        )

        answer = service.process({"text": "テンポを調整して"})

        self.assertEqual(answer["kind"], "ask")
        self.assertTrue(answer["line"].startswith("わかりませんでした。別の言い方で言ってみてください。"))
        self.assertEqual(bridge.write_count, 0)
        rewrite.assert_called_once()

    def test_llm_unknown_uses_plain_english_reply(self) -> None:
        service = LiveJevService(
            bridge=NoWriteBridge(),
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("none", action_conf=0.2),
            llm_key="gemini-key",
            rewriter=mock.Mock(return_value="不明"),
        )
        service.lang = "en"

        answer = service.process({"text": "make it nice"})

        self.assertEqual(answer["line"], "I could not work that out. Please try saying it another way. What should I do?")
        self.assertEqual(answer["via"], "gemini")

    def test_llm_connection_failure_has_distinct_reply(self) -> None:
        service = LiveJevService(
            bridge=NoWriteBridge(),
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("none", action_conf=0.2),
            llm_key="gemini-key",
            rewriter=mock.Mock(side_effect=RewriteFailure("unavailable")),
        )

        answer = service.process({"text": "いい感じにして"})

        self.assertEqual(answer["line"], "Geminiに接続できませんでした。 何をしますか？")
        self.assertEqual(answer["via"], "gemini")

    def test_llm_is_tried_once_for_unresolved_parameter_device_and_clip(self) -> None:
        snapshot = sample_snapshot()
        tracks = list(snapshot.tracks)
        tracks[0] = replace(
            tracks[0],
            clips=(Clip(0, "Pad Loop", "live_set tracks 0 clip_slots 0 clip", {"looping": True}),),
        )
        snapshot = replace(snapshot, tracks=tuple(tracks))

        param_initial = response("param", "t0", "up_small", param="none")
        param_candidate = response("param", "t0", "up_small", param="d0p0")
        device_initial = response("device_off", "t0")
        device_candidate = response("device_off", "t0")
        device_candidate["answers"]["device_t0"] = choice("d0")
        clip_initial = response("clip_loop_off", "t0")
        clip_candidate = response("clip_loop_off", "t0")
        clip_candidate["answers"]["clip_t0"] = choice("c0")

        cases = (
            ("parameter", "PadのReverbのDry/Wetを上げる", param_initial, param_candidate),
            ("device", "PadのReverbをオフ", device_initial, device_candidate),
            ("clip", "PadのPad Loopのループをオフ", clip_initial, clip_candidate),
        )
        for name, rewritten, initial, candidate in cases:
            with self.subTest(name=name):
                bridge = self.BoolBridge()
                rewrite = mock.Mock(return_value=rewritten)
                service = LiveJevService(
                    bridge=bridge,
                    snapshot=snapshot,
                    key="x",
                    requester=mock.Mock(side_effect=[initial, candidate]),
                    llm_key="gemini-key",
                    rewriter=rewrite,
                )

                answer = service.process({"id": name, "text": "Padの対象を調整して"})

                self.assertEqual(answer["kind"], "confirm")
                self.assertEqual(bridge.write_count, 0)
                rewrite.assert_called_once()

    def test_llm_rewrite_cannot_widen_uncertain_target_authority(self) -> None:
        uncertain = response("none", action_conf=0.2)
        uncertain["answers"]["track_stated"]["noul"] = 0.35
        bridge = self.BoolBridge()
        rewrite = mock.Mock(return_value="ベースをミュート")
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=mock.Mock(side_effect=[uncertain, response("mute", "t1")]),
            llm_key="gemini-key",
            rewriter=rewrite,
        )

        answer = service.process({"id": "authority", "text": "ベルを消しといて"})

        self.assertEqual(answer["kind"], "ask")
        self.assertIsNone(service.pending_confirm)
        self.assertEqual(bridge.write_count, 0)

    def test_llm_requires_key_and_respects_kill_switch(self) -> None:
        for llm_key, environment in [(None, {}), ("gemini-key", {"LIVE_JEV_LLM": "0"})]:
            with self.subTest(llm_key=llm_key, environment=environment):
                rewrite = mock.Mock(return_value="ドラムをミュート")
                service = LiveJevService(
                    bridge=NoWriteBridge(),
                    snapshot=sample_snapshot(),
                    key="x",
                    requester=lambda _payload, _key: response("none", "t2", action_conf=0.2),
                    llm_key=llm_key,
                    rewriter=rewrite,
                )
                with mock.patch.dict("os.environ", environment, clear=False):
                    answer = service.process({"text": "ドラムをいい感じにして"})
                self.assertEqual(answer["kind"], "ask")
                self.assertNotIn("via", answer)
                rewrite.assert_not_called()

    def test_llm_is_not_a_replacement_for_missing_jev_key_or_failure(self) -> None:
        for key, requester, expected in (
            (None, mock.Mock(), "Jevの鍵が見つかりません"),
            ("x", mock.Mock(side_effect=RuntimeError("offline")), "Jevに繋がりません。少し待ってから試してください"),
        ):
            with self.subTest(key=key):
                rewrite = mock.Mock(return_value="Drumsをミュート")
                service = LiveJevService(
                    bridge=NoWriteBridge(),
                    snapshot=sample_snapshot(),
                    key=key,
                    requester=requester,
                    llm_key="gemini-key",
                    rewriter=rewrite,
                )

                answer = service.process({"text": "いい感じにして"})

                self.assertEqual(answer["line"], expected)
                rewrite.assert_not_called()

    def test_llm_attempt_and_burst_limits_are_preserved(self) -> None:
        rewrite = mock.Mock(return_value="不明")
        service = LiveJevService(
            bridge=NoWriteBridge(),
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("none", action_conf=0.2),
            llm_key="gemini-key",
            rewriter=rewrite,
        )

        service.process({"text": "same"})
        service.process({"text": "same"})
        for index in range(1, 6):
            service.process({"text": f"different {index}"})

        self.assertEqual(rewrite.call_count, 5)

    def test_non_action_clarification_never_calls_llm(self) -> None:
        rewrite = mock.Mock(return_value="ベースをミュート")
        service = LiveJevService(
            bridge=NoWriteBridge(),
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("mute"),
            llm_key="gemini-key",
            rewriter=rewrite,
        )

        answer = service.process({"text": "ベルをミュート"})

        self.assertIn(answer["kind"], {"ask", "error"})
        rewrite.assert_not_called()

    def test_multi_track_batch_skips_matching_values_and_undo_restores_without_live_undo(self) -> None:
        snapshot = sample_snapshot()
        snapshot = replace(snapshot, tracks=(
            replace(snapshot.tracks[0], mute=False),
            replace(snapshot.tracks[1], mute=True),
            replace(snapshot.tracks[2], mute=False),
        ))
        bridge = self.BoolBridge()
        bridge.values = {(track.path, "mute"): track.mute for track in snapshot.tracks}
        service = LiveJevService(
            bridge=bridge,
            snapshot=snapshot,
            key="x",
            requester=lambda *_args: self.fail("Jev was called"),
        )

        applied = service.process({"text": "mute all"})
        self.assertEqual(applied["kind"], "result")
        writes = [call for call in bridge.calls if "--write" in call]
        self.assertEqual(sum(item == "--api-set" for item in writes[0]), 2)
        self.assertTrue(all(track.mute for track in service.snapshot.tracks))

        restored = service.process({"text": "undo"})
        self.assertEqual(restored["kind"], "result")
        self.assertEqual([track.mute for track in service.snapshot.tracks], [False, True, False])
        self.assertFalse(any("--api-call" in call and "undo" in call for call in bridge.calls))

    def test_clear_single_command_never_calls_llm(self) -> None:
        bridge = self.BoolBridge()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("mute", "t2"),
            llm_key="gemini-key",
            rewriter=lambda *_args: self.fail("普通の一言でLLMを呼んだ"),
        )
        answer = service.process({"text": "ドラムをミュート"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["ms"]["llm"], 0)
        self.assertNotIn("via", answer)

    def test_local_template_calls_neither_jev_nor_llm(self) -> None:
        class TempoBridge:
            def __init__(self):
                self.calls = []

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                if "--tempo" in arguments:
                    return BridgeResult((), 1, 0, False)
                at = arguments.index("--api-get")
                return BridgeResult((Ack("api_get", arguments[at + 3], 90, "live_set", "tempo"),), 1, 0, False)

        bridge = TempoBridge()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda *_args: self.fail("Jev was called"),
            llm_key="x",
            rewriter=lambda *_args: self.fail("LLM was called"),
        )
        answer = service.process({"text": "テンポ90"})
        self.assertEqual(answer["kind"], "info")
        self.assertEqual(service.snapshot.tempo, 90)
        self.assertFalse(any("--tempo" in call for call in bridge.calls))
        self.assertEqual(answer["ms"]["jev"], 0)
        self.assertEqual(answer["ms"]["llm"], 0)
        self.assertNotIn("via", answer)

    def test_track_name_is_checked_before_write(self) -> None:
        class RenamedBridge:
            def __init__(self):
                self.calls = []

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                if "--write" in arguments:
                    self.fail("write must not be sent")
                at = arguments.index("--api-get")
                return BridgeResult((Ack("api_get", arguments[at + 3], "Other", arguments[at + 1], "name"),), 1, 0, False)

            def fail(self, message):
                raise AssertionError(message)

        bridge = RenamedBridge()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_args: self.fail("Jev was called"))
        rereads = []
        service.reader = type("R", (), {"read": staticmethod(lambda: rereads.append(1) or (sample_snapshot(), 1))})()
        answer = service.process({"text": "Drumsをミュート"})
        self.assertEqual(answer["kind"], "error")
        self.assertIn("曲の構成が変わり続けています", answer["line"])
        self.assertEqual(len(rereads), 1)
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_transport_readback_retries_without_resending(self) -> None:
        class DelayedTransportBridge:
            def __init__(self):
                self.calls = []
                self.values = iter((0, 0, 1))

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                if "--api-call" in arguments:
                    return BridgeResult((), 1, 0, False)
                at = arguments.index("--api-get")
                value = next(self.values)
                return BridgeResult((Ack("api_get", arguments[at + 3], value, "live_set", "is_playing"),), 1, 0, False)

        bridge = DelayedTransportBridge()
        service = LiveJevService(
            bridge=bridge,
            snapshot=replace(sample_snapshot(), playing=False),
            requester=lambda *_args: self.fail("Jev was called"),
        )
        with mock.patch("daemon.time.sleep") as sleep:
            answer = service.process({"text": "再生"})
        self.assertEqual(answer["kind"], "result")
        self.assertTrue(service.snapshot.playing)
        self.assertEqual(sum("--api-call" in call for call in bridge.calls), 1)
        self.assertEqual(sum("is_playing" in call for call in bridge.calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_undo_refuses_to_overwrite_manual_change(self) -> None:
        class MutableVolumeBridge:
            def __init__(self):
                self.calls = []
                self.value = 0.60

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                if "--api-get" in arguments:
                    at = arguments.index("--api-get")
                    return BridgeResult((Ack("api_get", arguments[at + 3], "Pad", arguments[at + 1], "name"),), 1, 0, False)
                if "--api-parameter-set" in arguments:
                    at = arguments.index("--api-parameter-set")
                    self.value = float(arguments[at + 2])
                    return BridgeResult((), 1, 0, False)
                if "--api-mixer-status" in arguments:
                    at = arguments.index("--api-mixer-status")
                    payload = {"parameters": {"volume": {"value": self.value}}}
                    return BridgeResult((Ack("api_mixer_status", arguments[at + 2], payload, "live_set tracks 0"),), 1, 0, False)
                at = arguments.index("--api-call")
                return BridgeResult((Ack("api_call", arguments[at + 4], f"{self.value:g}", arguments[at + 1], "str_for_value"),), 1, 0, False)

        bridge = MutableVolumeBridge()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_args: response("volume", "t0", "down_small"))
        self.assertEqual(service.process({"text": "パッド下げて"})["kind"], "result")
        writes = sum("--api-parameter-set" in call for call in bridge.calls)
        bridge.value = 0.42
        answer = service.process({"text": "戻して"})
        self.assertEqual(answer["kind"], "info")
        self.assertIn("手で変更されているので戻しません", answer["line"])
        self.assertEqual(sum("--api-parameter-set" in call for call in bridge.calls), writes)
        self.assertEqual(bridge.value, 0.42)

    def test_missing_track_asks_and_never_executes(self) -> None:
        bridge = NoWriteBridge()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="not-a-real-key",
            requester=lambda _payload, _key: response("volume", "none", "down_small", track_conf=0.2),
        )
        answer = service.process({"id": "7", "text": "下げて"})
        self.assertEqual(answer["kind"], "ask")
        self.assertEqual(answer["line"], "どのトラックですか？")
        self.assertEqual([call for call in bridge.calls if "--api-session-context" not in call], [])

    def test_generation_is_info_before_low_action_confidence(self) -> None:
        bridge = NoWriteBridge()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="not-a-real-key",
            requester=lambda _payload, _key: response("none", generation=0.51, action_conf=0.1),
        )
        answer = service.process({"text": "もっとエモくして"})
        self.assertEqual(answer["kind"], "info")
        self.assertEqual([call for call in bridge.calls if "--api-session-context" not in call], [])

    def test_every_ask_stage_avoids_bridge(self) -> None:
        cases = [
            (response("none", action_conf=0.2), "何をしますか？"),
            (response("volume", "none", "down_small", track_conf=0.2), "どのトラックですか？"),
            (response("param", "t0", "up_small", param="none", param_conf=0.2), "どのつまみですか？"),
            (response("volume", "t0", "none"), "上げますか、下げますか？"),
        ]
        for mocked, line in cases:
            with self.subTest(line=line):
                bridge = NoWriteBridge()
                service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda _p, _k, answer=mocked: answer)
                answer = service.process({"text": "曖昧な依頼"})
                self.assertEqual(answer["kind"], "ask")
                self.assertEqual(answer["line"], line)
                self.assertEqual([call for call in bridge.calls if "--api-session-context" not in call], [])

    def test_invalid_track_and_master_action_are_input_errors(self) -> None:
        cases = [
            (response("mute", "master"), "Masterはミュートに対応していません"),
            (response("pan", "master", "up_small"), "Masterはパンに対応していません"),
            (response("mute", "t999"), "指定したトラックが見つかりません"),
        ]
        for mocked, line in cases:
            with self.subTest(line=line):
                bridge = NoWriteBridge()
                service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda _p, _k, answer=mocked: answer)
                answer = service.process({"text": "操作"})
                self.assertEqual(answer["kind"], "error")
                self.assertEqual(answer["line"], line)
                self.assertTrue(service.live)
                self.assertEqual([call for call in bridge.calls if "--api-session-context" not in call], [])

    def test_pending_track_exact_name_fills_without_second_jev_request(self) -> None:
        count = 0

        def requester(_payload, _key):
            nonlocal count
            count += 1
            return response("volume", "none", "down_small", track_conf=0.2)

        bridge = NoWriteBridge()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=requester)
        self.assertEqual(service.process({"text": "下げて"})["kind"], "ask")
        with self.assertRaises(AssertionError):
            service.process({"text": "Bass"})
        self.assertEqual(count, 1)
        first_real = next(call for call in bridge.calls if "--api-session-context" not in call)
        self.assertIn("--api-mixer-status", first_real)

    def test_invalid_input_is_an_error(self) -> None:
        service = LiveJevService(bridge=NoWriteBridge(), snapshot=sample_snapshot(), key="x")
        self.assertEqual(service.process({"text": ""})["kind"], "error")

    def test_pan_readback_reports_live_display_units(self) -> None:
        class PanBridge:
            def __init__(self):
                self.value = 0.0

            def run(self, arguments):
                arguments = list(arguments)
                if "--api-get" in arguments:
                    at = arguments.index("--api-get")
                    return BridgeResult((Ack("api_get", arguments[at + 3], "Pad", arguments[at + 1], "name"),), 1, 0, False)
                if "--api-parameter-set" in arguments:
                    self.value = float(arguments[arguments.index("--api-parameter-set") + 2])
                    return BridgeResult((), 1, 0, False)
                if "--api-mixer-status" in arguments:
                    at = arguments.index("--api-mixer-status")
                    payload = {"parameters": {"panning": {"value": self.value}}}
                    return BridgeResult((Ack("api_mixer_status", arguments[at + 2], payload, "live_set tracks 0"),), 1, 0, False)
                at = arguments.index("--api-call")
                shown = "20L" if self.value == -0.4 else "unexpected"
                return BridgeResult((Ack("api_call", arguments[at + 4], shown, arguments[at + 1], "str_for_value"),), 1, 0, False)

        service = LiveJevService(
            bridge=PanBridge(), snapshot=sample_snapshot(), key="x",
            requester=lambda _payload, _key: response("pan", "t0", "set"),
        )
        answer = service.process({"text": "左20"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["line"], "パン 20L")
        self.assertEqual(service.snapshot.tracks[0].pan, -0.4)

    def test_unmute_and_unsolo_use_the_only_active_track_when_unspecified(self) -> None:
        class ReleaseBridge:
            def __init__(self):
                self.calls = []

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                if "--api-set" in arguments:
                    return BridgeResult((), 1, 0, False)
                at = arguments.index("--api-get")
                prop = arguments[at + 2]
                payload = "Bass" if prop == "name" else 0
                return BridgeResult((Ack("api_get", arguments[at + 3], payload, arguments[at + 1], prop),), 1, 0, False)

        for action, field in (("unmute", "mute"), ("unsolo", "solo")):
            with self.subTest(action=action):
                snapshot = sample_snapshot()
                active = replace(snapshot.tracks[1], **{field: True})
                snapshot = replace(snapshot, tracks=(snapshot.tracks[0], active, snapshot.tracks[2]))
                bridge = ReleaseBridge()
                service = LiveJevService(
                    bridge=bridge, snapshot=snapshot, key="x",
                    requester=lambda _payload, _key, selected=action: response(selected, "none", track_conf=0.2),
                )
                answer = service.process({"text": "解除"})
                self.assertEqual(answer["kind"], "result")
                write = next(call for call in bridge.calls if "--api-set" in call)
                self.assertEqual(write[2:5], ["live_set tracks 1", field, "0"])

    def test_unmute_without_exactly_one_active_track_still_asks(self) -> None:
        for active_indexes in ((), (0, 1)):
            with self.subTest(active_indexes=active_indexes):
                snapshot = sample_snapshot()
                tracks = tuple(replace(track, mute=track.index in active_indexes) for track in snapshot.tracks)
                service = LiveJevService(
                    bridge=NoWriteBridge(), snapshot=replace(snapshot, tracks=tracks), key="x",
                    requester=lambda _payload, _key: response("unmute", "none", track_conf=0.2),
                )
                answer = service.process({"text": "ミュート解除"})
                self.assertEqual(answer["kind"], "ask")
                self.assertEqual(answer["line"], "どのトラックですか？")

    def test_invalid_unit_is_treated_as_missing_number(self) -> None:
        bridge = NoWriteBridge()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("tempo", step="set"),
        )
        answer = service.process({"text": "テンポを90dBに"})
        self.assertEqual(answer["kind"], "ask")
        self.assertEqual(answer["line"], "上げますか、下げますか？")
        self.assertEqual([call for call in bridge.calls if "--api-session-context" not in call], [])

    def test_pending_parameter_answer_uses_same_250_candidates(self) -> None:
        from snapshot import Device, Param

        params = tuple(Param(index, f"P{index}", 0, 0, 1, "0%", f"live_set tracks 0 devices 0 parameters {index}") for index in range(251))
        snapshot = sample_snapshot()
        track = replace(snapshot.tracks[0], devices=(Device(0, "Huge", params, "live_set tracks 0 devices 0"),))
        snapshot = replace(snapshot, tracks=(track, *snapshot.tracks[1:]))
        result = interpret_response(snapshot, "つまみを上げて", response("param", "t0", "up_small", param="none", param_conf=0.2), (0,))
        service = LiveJevService(bridge=NoWriteBridge(), snapshot=snapshot, key="x")
        service.pending = Pending(result, "param")
        self.assertIsNone(service._fill_pending("P250"))

    def test_previous_target_continues_and_undo_restores_exact_value(self) -> None:
        class VolumeBridge:
            def __init__(self):
                self.calls = []
                self.value = 0.60

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                if "--api-get" in arguments:
                    at = arguments.index("--api-get")
                    return BridgeResult((Ack("api_get", arguments[at + 3], "Pad", arguments[at + 1], "name"),), 1, 0, False)
                if "--api-parameter-set" in arguments:
                    at = arguments.index("--api-parameter-set")
                    self.value = float(arguments[at + 2])
                    return BridgeResult((), 1, 0, False)
                if "--api-mixer-status" in arguments:
                    at = arguments.index("--api-mixer-status")
                    payload = {"parameters": {"volume": {"value": self.value}}}
                    return BridgeResult((Ack("api_mixer_status", arguments[at + 2], payload, "live_set tracks 0"),), 1, 0, False)
                at = arguments.index("--api-call")
                return BridgeResult((Ack("api_call", arguments[at + 4], f"{self.value:g}", arguments[at + 1], "str_for_value"),), 1, 0, False)

        replies = iter([
            response("volume", "t0", "down_small"),
            response("volume", "none", "down_small", track_conf=0.2, refers_previous=0.9),
            response("none", "none", "none", action_conf=0.1, track_conf=0.2, refers_previous=0.9),
        ])
        bridge = VolumeBridge()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: next(replies),
        )
        self.assertEqual(service.process({"text": "パッド下げて"})["kind"], "result")
        self.assertAlmostEqual(bridge.value, 0.57)
        self.assertEqual(service.process({"text": "もう少し"})["kind"], "result")
        self.assertAlmostEqual(bridge.value, 0.54)
        self.assertEqual(service.process({"text": "戻して"})["kind"], "result")
        self.assertAlmostEqual(bridge.value, 0.57)
        self.assertEqual(service.previous.track, 0)

    def test_write_timeout_reads_back_without_resending_change(self) -> None:
        class TimeoutThenReadBridge:
            def __init__(self):
                self.calls = []

            def run(self, arguments):
                self.calls.append(list(arguments))
                if "--api-get" in arguments and arguments[arguments.index("--api-get") + 2] == "name":
                    at = arguments.index("--api-get")
                    return BridgeResult((Ack("api_get", arguments[at + 3], "Drums", arguments[at + 1], "name"),), 1, 0, False)
                if "--api-set" in arguments:
                    return BridgeResult((), 10, 2, True)
                return BridgeResult((Ack("api_get", "read", 1, "live_set tracks 2", "mute"),), 5, 0, False)

        bridge = TimeoutThenReadBridge()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("mute", "t2"),
        )
        answer = service.process({"text": "ドラムをミュート"})
        self.assertEqual(answer["kind"], "unknown")
        self.assertEqual(sum("--api-set" in call for call in bridge.calls), 1)
        self.assertIn("変更が残っている可能性", answer["line"])
        call_count = len(bridge.calls)
        repeated = service.process({"text": "もう少し"})
        self.assertEqual(repeated["kind"], "info")
        self.assertEqual(len(bridge.calls), call_count)

    def test_write_and_readback_timeout_returns_unknown(self) -> None:
        from tests.support import StatefulLive

        class AcceptedWriteThenReadFailure(StatefulLive):
            def __init__(self):
                super().__init__()
                self.failed_readback = False

            def run(self, arguments):
                result = super().run(arguments)
                if "--api-get" in arguments and any("--api-set" in call for call in self.calls) and not self.failed_readback:
                    self.failed_readback = True
                    raise BridgeError("post-write readback failed")
                return result

        bridge = AcceptedWriteThenReadFailure()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("mute", "t2"),
        )
        answer = service.process({"text": "ドラムをミュート"})
        self.assertEqual(answer["kind"], "error")
        self.assertIn("変更は残っていません", answer["line"])
        self.assertEqual(sum("--api-set" in call for call in bridge.calls), 2)
        self.assertFalse(bridge.state[(2, "mute")])

    def test_db_volume_reaches_target_within_point_zero_five_db(self) -> None:
        class VolumeBridge:
            def __init__(self):
                self.calls = []
                self.values = []

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                if "--api-get" in arguments:
                    at = arguments.index("--api-get")
                    return BridgeResult((Ack("api_get", arguments[at + 3], "Pad", arguments[at + 1], "name"),), 1, 0, False)
                if "--api-parameter-set" in arguments:
                    value = float(arguments[arguments.index("--api-parameter-set") + 2])
                    self.values.append(value)
                    return BridgeResult((), 2, 0, False)
                if "--api-call" in arguments:
                    at = arguments.index("--api-call")
                    value = json.loads(arguments[at + 3])[0]
                    shown = f"{-60 + value * 60:.1f} dB"
                    return BridgeResult((Ack("api_call", arguments[at + 4], shown, arguments[at + 1], "str_for_value"),), 2, 0, False)
                at = arguments.index("--api-mixer-status")
                mixer = {"parameters": {"volume": {"path": "live_set tracks 0 mixer_device volume", "value": self.values[-1], "min": 0, "max": 1}}}
                return BridgeResult((Ack("api_mixer_status", arguments[at + 2], mixer, "live_set tracks 0"),), 2, 0, False)

        bridge = VolumeBridge()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _payload, _key: response("volume", "t0", "set"),
        )
        with mock.patch("daemon.request_id", side_effect=lambda kind: kind):
            answer = service.process({"text": "パッドを-3dBに"})
        self.assertEqual(answer["kind"], "result")
        self.assertLessEqual(abs(float(service.snapshot.tracks[0].volume_display.split()[0]) - -3.0), 0.05)
        self.assertLessEqual(sum("--api-call" in call for call in bridge.calls), 12)
        self.assertAlmostEqual(service.snapshot.tracks[0].volume, bridge.values[-1])

    def test_db_volume_stops_when_tolerance_is_met(self) -> None:
        class ExactBridge:
            def __init__(self):
                self.calls = []

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                if "--api-get" in arguments:
                    at = arguments.index("--api-get")
                    return BridgeResult((Ack("api_get", arguments[at + 3], "Pad", arguments[at + 1], "name"),), 1, 0, False)
                if "--api-call" in arguments:
                    at = arguments.index("--api-call")
                    return BridgeResult((Ack("api_call", arguments[at + 4], "-30.0 dB", arguments[at + 1], "str_for_value"),), 1, 0, False)
                if "--api-mixer-status" in arguments:
                    at = arguments.index("--api-mixer-status")
                    mixer = {"parameters": {"volume": {"path": "live_set tracks 0 mixer_device volume", "value": 0.0, "min": 0, "max": 1}}}
                    return BridgeResult((Ack("api_mixer_status", arguments[at + 2], mixer, "live_set tracks 0"),), 1, 0, False)
                return BridgeResult((), 1, 0, False)

        bridge = ExactBridge()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda _p, _k: response("volume", "t0", "set"))
        answer = service.process({"text": "パッドを-30dBに"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(sum("--api-parameter-set" in call for call in bridge.calls), 1)
        self.assertEqual(service.snapshot.tracks[0].volume_display, "-30.0 dB")

    def test_db_final_readback_timeout_is_unknown(self) -> None:
        class MissingMixerBridge:
            def __init__(self):
                self.mixer_reads = 0

            def run(self, arguments):
                arguments = list(arguments)
                if "--api-get" in arguments:
                    at = arguments.index("--api-get")
                    return BridgeResult((Ack("api_get", arguments[at + 3], "Pad", arguments[at + 1], "name"),), 1, 0, False)
                if "--api-call" in arguments:
                    at = arguments.index("--api-call")
                    return BridgeResult((Ack("api_call", arguments[at + 4], "-30.0 dB", arguments[at + 1], "str_for_value"),), 1, 0, False)
                if "--api-mixer-status" in arguments:
                    self.mixer_reads += 1
                    if self.mixer_reads == 1:
                        at = arguments.index("--api-mixer-status")
                        payload = {"parameters": {"volume": {"path": "live_set tracks 0 mixer_device volume", "value": 0.6}}}
                        return BridgeResult((Ack("api_mixer_status", arguments[at + 2], payload, "live_set tracks 0"),), 1, 0, False)
                    return BridgeResult((), 1, -1, True)
                return BridgeResult((), 1, 0, False)

        service = LiveJevService(
            bridge=MissingMixerBridge(),
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda _p, _k: response("volume", "t0", "set"),
        )
        answer = service.process({"text": "パッドを-30dBに"})
        self.assertEqual(answer["kind"], "unknown")
        self.assertIn("変更が残っている可能性", answer["line"])

    def test_receipt_undo_phrasings_never_call_live_undo(self) -> None:
        from tests.support import StatefulLive

        for phrase in ("undo", "undo that", "take that back", "アンドゥ", "元に戻して", "取り消して"):
            with self.subTest(phrase=phrase):
                bridge = StatefulLive()
                service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: response("mute", "t0"))
                self.assertEqual(service.process({"text": "mute Pad"})["kind"], "result")
                self.assertEqual(service.process({"text": phrase})["kind"], "result")
                self.assertFalse(bridge.state[(0, "mute")])
                self.assertFalse(any("--api-call" in call and "undo" in call for call in bridge.calls))

    def test_undo_and_redo_routes_never_reach_action_execution(self) -> None:
        for phrase in ("アンドゥ", "undo"):
            with self.subTest(route=phrase):
                bridge = StatefulLive()
                service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
                self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")
                with mock.patch.object(service, "_execute", side_effect=AssertionError("undo reached _execute")), \
                     mock.patch.object(service, "_execute_now", side_effect=AssertionError("undo reached _execute_now")):
                    answer = service.process({"text": phrase})
                self.assertEqual(answer["kind"], "result")

        for phrase in ("やり直し", "redo"):
            with self.subTest(route=phrase):
                service = LiveJevService(bridge=StatefulLive(), snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
                with mock.patch.object(service, "_execute", side_effect=AssertionError("redo reached _execute")), \
                     mock.patch.object(service, "_execute_now", side_effect=AssertionError("redo reached _execute_now")):
                    answer = service.process({"text": phrase})
                self.assertEqual(answer["kind"], "info")
                self.assertEqual(answer["line"], "これはLive Jevではやり直せません。Liveのやり直し（⇧⌘Z）をお使いください。")

        service = LiveJevService(bridge=StatefulLive(), snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
        with mock.patch.object(service, "_execute", side_effect=AssertionError("button reached _execute")), \
             mock.patch.object(service, "_execute_now", side_effect=AssertionError("button reached _execute_now")):
            button = service.process({"cmd": "undo"})
        self.assertEqual(button["kind"], "info")

        service = LiveJevService(bridge=StatefulLive(), snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
        with mock.patch.object(service, "_execute", side_effect=AssertionError("compound undo reached _execute")), \
             mock.patch.object(service, "_execute_now", side_effect=AssertionError("compound undo reached _execute_now")):
            compound = service.process({"text": "mute Bass and undo"})
        self.assertEqual(compound["kind"], "info")

        bridge = StatefulLive()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
        self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")
        service.pending = Pending(interpret_response(sample_snapshot(), "mute", response("mute", "none"), ()), "track", request_id="question")
        with mock.patch.object(service, "_execute", side_effect=AssertionError("clarification undo reached _execute")), \
             mock.patch.object(service, "_execute_now", side_effect=AssertionError("clarification undo reached _execute_now")):
            clarification = service.process({"id": "answer", "answering": "question", "text": "undo"})
        self.assertEqual(clarification["kind"], "result")

    def test_jev_classified_undo_restores_receipt(self) -> None:
        bridge = StatefulLive()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda *_: response("undo"),
        )
        self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")

        undo = service.process({"text": "put it back how it was before"})
        self.assertEqual(undo["kind"], "result")
        self.assertFalse(bridge.state[(1, "mute")])

    def test_low_confidence_jev_undo_asks_before_restoring_receipt(self) -> None:
        bridge = StatefulLive()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda *_: response("undo", action_conf=0.59),
        )
        self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")

        undo = service.process({"text": "put it back how it was before"})

        self.assertEqual(undo["kind"], "ask")
        self.assertTrue(bridge.state[(1, "mute")])
        self.assertIsNotNone(service.pending)
        self.assertEqual(service.pending.field, "action")

    def test_jev_classified_redo_is_refused(self) -> None:
        bridge = StatefulLive()
        service = LiveJevService(
            bridge=bridge,
            snapshot=sample_snapshot(),
            key="x",
            requester=lambda *_: response("redo"),
        )
        calls_before_redo = len(bridge.calls)
        redo = service.process({"text": "repeat the reverted change"})
        self.assertEqual(redo["kind"], "info")
        self.assertEqual(redo["line"], "これはLive Jevではやり直せません。Liveのやり直し（⇧⌘Z）をお使いください。")
        self.assertFalse(any("--api-call" in call and "redo" in call for call in bridge.calls[calls_before_redo:]))

    def test_successful_receipt_undo_is_consumed(self) -> None:
        bridge = StatefulLive()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
        self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")
        self.assertEqual(service.process({"cmd": "undo"})["kind"], "result")

        bridge.state[(1, "mute")] = True
        bridge.state[(2, "solo")] = True
        second = service.process({"cmd": "undo"})

        self.assertEqual(second["kind"], "info")
        self.assertTrue(bridge.state[(1, "mute")])
        self.assertTrue(bridge.state[(2, "solo")])

    def test_write_timeout_with_successful_restore_does_not_reactivate_receipt(self) -> None:
        class TimeoutAfterWrite(StatefulLive):
            def __init__(self) -> None:
                super().__init__()
                self.timeout_next_write = True

            def run(self, arguments):
                result = super().run(arguments)
                if self.timeout_next_write and "--api-set" in arguments:
                    self.timeout_next_write = False
                    return BridgeResult(result.acks, result.elapsed_ms, -1, True)
                return result

        bridge = TimeoutAfterWrite()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))

        failed = service.process({"text": "mute Bass"})
        self.assertEqual(failed["kind"], "error")
        self.assertFalse(bridge.state[(1, "mute")])

        bridge.state[(1, "mute")] = True
        undo = service.process({"cmd": "undo"})

        self.assertEqual(undo["kind"], "info")
        self.assertTrue(bridge.state[(1, "mute")])

    def test_compound_success_replaces_consumed_history_with_chain_receipts(self) -> None:
        bridge = StatefulLive()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))

        self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")
        self.assertEqual(service.process({"cmd": "undo"})["kind"], "result")
        self.assertEqual(service.process({"text": "mute Bass and solo Drums"})["kind"], "result")

        undo = service.process({"cmd": "undo"})

        self.assertEqual(undo["kind"], "result")
        self.assertFalse(bridge.state[(1, "mute")])
        self.assertFalse(bridge.state[(2, "solo")])

    def test_track_creation_undo_refuses_instead_of_consuming_manual_edit(self) -> None:
        class TrackUndoBridge(StatefulLive):
            def __init__(self) -> None:
                super().__init__()
                self.track_count = 3

            def run(self, arguments):
                if "--api-call" in arguments and "undo" in arguments:
                    self.calls.append(list(arguments))
                    if self.state[(1, "mute")]:
                        self.state[(1, "mute")] = False
                    else:
                        self.track_count = 3
                    return BridgeResult((), 1, 0, False)
                return super().run(arguments)

        bridge = TrackUndoBridge()
        original = sample_snapshot()
        added_track = replace(original.tracks[-1], index=3, name="4-MIDI", path="live_set tracks 3")

        def read():
            tracks = original.tracks if bridge.track_count == 3 else original.tracks + (added_track,)
            return replace(original, tracks=tracks, taken_at=time.time()), 1

        service = LiveJevService(bridge=bridge, snapshot=original, key="x", requester=lambda *_: self.fail("Jev called"))
        service.reader = type("Reader", (), {"read": staticmethod(read)})()

        def add_track(*_args, **_kwargs):
            bridge.track_count = 4
            return {"ok": True, "track_index": 3, "track": "4-MIDI", "devices_after": []}

        with mock.patch("daemon.plugin_script.ping", return_value=True), \
             mock.patch("daemon.plugin_script.add_track", side_effect=add_track):
            self.assertEqual(service.process({"text": "add a midi track"})["kind"], "result")

        bridge.state[(1, "mute")] = True
        answer = service.process({"cmd": "undo"})

        self.assertEqual(answer["kind"], "info")
        self.assertTrue(bridge.state[(1, "mute")])
        self.assertEqual(bridge.track_count, 4)
        self.assertFalse(any("--api-call" in call and "undo" in call for call in bridge.calls))

    def test_note_transform_undo_refuses_without_receipt(self) -> None:
        import daemon as daemon_module

        bridge = StatefulLive()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
        self.assertEqual(service.process({"text": "mute Bass"})["kind"], "result")
        self.assertEqual(service.process({"cmd": "undo"})["kind"], "result")
        with mock.patch.object(daemon_module.plugin_script, "ping", return_value=True), \
             mock.patch.object(daemon_module.plugin_script, "clip_notes", return_value={"ok": True, "track": "Bass"}):
            self.assertEqual(service.process({"text": "quantize Bass"})["kind"], "result")

        calls_before_undo = len(bridge.calls)
        self.assertEqual(service.process({"cmd": "undo"})["kind"], "info")
        self.assertFalse(any("--api-call" in call and "undo" in call for call in bridge.calls[calls_before_undo:]))

    def test_replaced_track_is_not_written_during_undo(self) -> None:
        from tests.support import StatefulLive

        bridge = StatefulLive()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: response("mute", "t0"))
        service.process({"text": "mute Pad"})
        writes = sum("--api-set" in call for call in bridge.calls)
        bridge.replace_track(0, "Replacement")
        answer = service.process({"cmd": "undo"})
        self.assertEqual(answer["kind"], "info")
        self.assertEqual(sum("--api-set" in call for call in bridge.calls), writes)
        self.assertTrue(bridge.state[(0, "mute")])

    def test_multi_track_readback_failure_is_not_success(self) -> None:
        from tests.support import StatefulLive

        bridge = StatefulLive(ignore_writes=True)
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
        answer = service.process({"text": "mute all"})
        self.assertNotEqual(answer["kind"], "result")
        self.assertEqual(bridge.flags("mute"), {"Pad": False, "Bass": False, "Drums": False})

    def test_solo_only_uses_live_state_for_every_track(self) -> None:
        from tests.support import StatefulLive

        bridge = StatefulLive()
        bridge.state[(1, "solo")] = True
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
        answer = service.process({"text": "solo only Pad"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(bridge.flags("solo"), {"Pad": True, "Bass": False, "Drums": False})

    def test_absolute_pan_send_and_parameter_undo_to_live_before_values(self) -> None:
        from tests.support import StatefulLive

        pan_bridge = StatefulLive()
        pan_bridge.values[(0, "panning")] = 0.4
        pan_service = LiveJevService(bridge=pan_bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: response("pan", "t0", "set"))
        self.assertEqual(pan_service.process({"text": "Padを左20"})["kind"], "result")
        self.assertEqual(pan_service.process({"cmd": "undo"})["kind"], "result")
        self.assertAlmostEqual(pan_bridge.values[(0, "panning")], 0.4)

        snapshot = sample_snapshot()
        snapshot = replace(snapshot, tracks=tuple(replace(track, sends=(0.2,)) for track in snapshot.tracks), returns=("Verb",))
        send_bridge = StatefulLive()
        send_bridge.sends[(0, 0)] = 0.7
        send_service = LiveJevService(bridge=send_bridge, snapshot=snapshot, key="x", requester=lambda *_: self.fail("Jev called"))
        self.assertEqual(send_service.process({"text": "Pad send A to 50%"})["kind"], "result")
        self.assertEqual(send_service.process({"cmd": "undo"})["kind"], "result")
        self.assertAlmostEqual(send_bridge.sends[(0, 0)], 0.7)

        param_bridge = StatefulLive()
        param_bridge.parameters["live_set tracks 0 devices 0 parameters 0"] = 0.7
        param_service = LiveJevService(bridge=param_bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: response("param", "t0", "set", param="d0p0"))
        self.assertEqual(param_service.process({"text": "PadのDry/Wetを80%に"})["kind"], "result")
        self.assertEqual(param_service.process({"cmd": "undo"})["kind"], "result")
        self.assertAlmostEqual(param_bridge.parameters["live_set tracks 0 devices 0 parameters 0"], 0.7)

    def test_prewrite_owner_replacement_aborts_before_track_write(self) -> None:
        from tests.support import StatefulLive

        class ReplaceDuringValueRead(StatefulLive):
            def run(self, arguments):
                result = super().run(arguments)
                if "--api-get" in arguments and arguments[arguments.index("--api-get") + 2] == "mute":
                    self.replace_track(0, "Replacement")
                return result

        bridge = ReplaceDuringValueRead()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: response("mute", "t0"))
        with self.assertRaises(StaleSnapshot):
            service._execute_now(_local_intent(Action.MUTE, track=0), None, 0, 0, time.perf_counter(), "mute Pad", None)
        self.assertFalse(bridge.state[(0, "mute")])

    def test_multi_track_runtime_error_rolls_back_applied_batch(self) -> None:
        from tests.support import StatefulLive

        class RaiseAfterMultiWrite(StatefulLive):
            def run(self, arguments):
                result = super().run(arguments)
                if arguments.count("--api-set") > 1:
                    raise RuntimeError("after apply")
                return result

        bridge = RaiseAfterMultiWrite()
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
        answer = service.process({"text": "mute all"})
        self.assertEqual(answer["kind"], "error")
        self.assertEqual(bridge.flags("mute"), {"Pad": False, "Bass": False, "Drums": False})

    def test_complete_failed_chain_rollback_blocks_live_undo(self) -> None:
        from tests.support import StatefulLive

        bridge = StatefulLive()
        bridge.inject_fault("set", "ok")
        bridge.inject_fault("set", "error")
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: self.fail("Jev called"))
        answer = service.process({"text": "mute Pad and solo Bass"})
        self.assertEqual(answer["kind"], "error")
        undo = service.process({"cmd": "undo"})
        self.assertEqual(undo["kind"], "info")
        self.assertFalse(any("--api-call" in call and "undo" in call for call in bridge.calls))

    def test_missing_prewrite_parameter_value_aborts_without_write(self) -> None:
        from tests.support import StatefulLive

        class MissingParameters(StatefulLive):
            def run(self, arguments):
                result = super().run(arguments)
                if "--api-device-parameters" in arguments:
                    ack = result.acks[-1]
                    return BridgeResult((replace(ack, payload={"parameters": []}),), result.elapsed_ms, 0, False)
                return result

        bridge = MissingParameters()
        bridge.parameters["live_set tracks 0 devices 0 parameters 0"] = 0.7
        service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x", requester=lambda *_: response("param", "t0", "set", param="d0p0"))
        answer = service.process({"text": "PadのDry/Wetを80%に"})
        self.assertEqual(answer["kind"], "error")
        self.assertFalse(any("--api-parameter-set" in call for call in bridge.calls))
        self.assertAlmostEqual(bridge.parameters["live_set tracks 0 devices 0 parameters 0"], 0.7)

    def test_ignored_send_write_is_not_reported_as_success(self) -> None:
        from tests.support import StatefulLive

        snapshot = replace(sample_snapshot(), tracks=tuple(replace(track, sends=(0.0,)) for track in sample_snapshot().tracks), returns=("Verb",))
        bridge = StatefulLive(ignore_writes=True)
        service = LiveJevService(bridge=bridge, snapshot=snapshot, key="x", requester=lambda *_: self.fail("Jev called"))
        answer = service.process({"text": "Pad send A to 50%"})
        self.assertNotEqual(answer["kind"], "result")
        self.assertEqual(bridge.sends[(0, 0)], 0.0)

    def test_numeric_undo_restores_small_raw_difference_exactly(self) -> None:
        from tests.support import StatefulLive

        snapshot = replace(sample_snapshot(), tracks=tuple(replace(track, sends=(0.49995,)) for track in sample_snapshot().tracks), returns=("Verb",))
        bridge = StatefulLive()
        bridge.sends[(0, 0)] = 0.49995
        service = LiveJevService(bridge=bridge, snapshot=snapshot, key="x", requester=lambda *_: self.fail("Jev called"))
        self.assertEqual(service.process({"text": "Pad send A to 50%"})["kind"], "result")
        self.assertEqual(service.process({"cmd": "undo"})["kind"], "result")
        self.assertEqual(bridge.sends[(0, 0)], 0.49995)

    def test_unchanged_send_parameter_and_tempo_do_not_write_or_replace_history(self) -> None:
        from tests.support import StatefulLive

        snapshot = replace(sample_snapshot(), tracks=tuple(replace(track, sends=(0.5,)) for track in sample_snapshot().tracks), returns=("Verb",))
        bridge = StatefulLive()
        bridge.sends[(0, 0)] = 0.5
        bridge.parameters["live_set tracks 0 devices 0 parameters 0"] = 0.25
        service = LiveJevService(bridge=bridge, snapshot=snapshot, key="x", requester=lambda *_: response("param", "t0", "set", param="d0p0"))
        marker = object()
        service.previous = marker
        for text in ("Pad send A to 50%", "PadのDry/Wetを25%に", "set tempo to 120"):
            self.assertEqual(service.process({"text": text})["kind"], "info")
            self.assertIs(service.previous, marker)
        self.assertFalse(any("--api-parameter-set" in call or "--tempo" in call for call in bridge.calls))


class JsonLineTests(unittest.TestCase):
    def test_stdio_prints_one_startup_notice_then_normal_status(self) -> None:
        class NoticeService:
            def startup_notice(self):
                return {"kind": "error", "line": "live.py経由に切り替えます"}

            def start(self):
                return {"kind": "status", "live": True}

            def close(self):
                pass

        stdout = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("")), mock.patch("sys.stdout", stdout):
            self.assertEqual(run_stdio(NoticeService()), 0)
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([line["kind"] for line in lines], ["error", "status"])

    def test_stdio_emits_one_json_object_per_input_line(self) -> None:
        class FakeService:
            def start(self):
                return {"kind": "status", "live": True}

            def process(self, message):
                if message.get("cmd") == "quit":
                    return {"kind": "status", "quit": True}
                return {"id": message.get("id"), "kind": "result", "line": message["text"]}

        stdin = io.StringIO('{"id":"1","text":"再生"}\n{"cmd":"quit"}\n')
        stdout = io.StringIO()
        with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout):
            self.assertEqual(run_stdio(FakeService()), 0)
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[1])["line"], "再生")
        self.assertTrue(json.loads(lines[2])["quit"])

    def test_stdio_process_exception_replies_and_continues(self) -> None:
        class FailingService:
            lang = "en"
            pending = object()
            pending_confirm = object()
            pending_confirm_created = 1.0

            def start(self):
                return {"kind": "status", "live": True}

            def process(self, message):
                if message.get("id") == "1":
                    raise RuntimeError("boom")
                return {"id": message.get("id"), "kind": "result", "line": "ok"}

        service = FailingService()
        stdin = io.StringIO('{"id":"1","text":"x"}\n{"id":"2","text":"y"}\n')
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            self.assertEqual(run_stdio(service), 0)
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual((replies[1]["id"], replies[1]["kind"]), ("1", "error"))
        self.assertEqual(replies[2]["line"], "ok")
        self.assertIn("RuntimeError: boom", stderr.getvalue())
        self.assertIsNone(service.pending)
        self.assertIsNone(service.pending_confirm)

    def test_stdio_eof_closes_service_and_stops_owned_child(self) -> None:
        class ChildOwningService:
            def __init__(self):
                self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

            def start(self):
                return {"kind": "status"}

            def close(self):
                self.child.terminate()
                self.child.wait(timeout=2)

        service = ChildOwningService()
        try:
            with mock.patch("sys.stdin", io.StringIO("")), mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(run_stdio(service), 0)
            self.assertIsNotNone(service.child.poll())
        finally:
            if service.child.poll() is None:
                service.child.kill()
                service.child.wait(timeout=2)


class JevClientTests(unittest.TestCase):
    class Response:
        status = 200

        def read(self):
            return b'{"answers":{}}'

    def test_connection_is_reused(self) -> None:
        connection = mock.Mock()
        connection.getresponse.side_effect = [self.Response(), self.Response()]
        with mock.patch("daemon.http.client.HTTPSConnection", return_value=connection) as factory:
            client = JevClient()
            client({"questions": {}}, "key")
            client({"questions": {}}, "key")
        factory.assert_called_once()
        self.assertEqual(connection.request.call_count, 2)

    def test_failed_connection_is_recreated_once(self) -> None:
        broken = mock.Mock()
        broken.request.side_effect = OSError("closed")
        working = mock.Mock()
        working.getresponse.return_value = self.Response()
        with mock.patch("daemon.http.client.HTTPSConnection", side_effect=[broken, working]) as factory:
            result = JevClient()({"questions": {}}, "key")
        self.assertEqual(result, {"answers": {}})
        self.assertEqual(factory.call_count, 2)
        broken.close.assert_called_once()

    def test_max_tokens_exceeded_is_not_retried(self) -> None:
        response_400 = mock.Mock(status=400)
        response_400.read.return_value = b'{"detail":{"error_type":"max_tokens_exceeded"}}'
        connection = mock.Mock()
        connection.getresponse.return_value = response_400
        with mock.patch("daemon.http.client.HTTPSConnection", return_value=connection):
            requester = JevClient()
            service = LiveJevService(
                bridge=NoWriteBridge(),
                snapshot=sample_snapshot(),
                key="x",
                requester=requester,
            )
            answer = service.process({"text": "いい感じにして"})
        self.assertEqual(answer["line"], "Jevに送る情報が多すぎて処理できませんでした。変更はしていません。")
        self.assertEqual(connection.request.call_count, 1)

    def test_other_400_responses_keep_generic_failure(self) -> None:
        for raw in (b'{"detail":{"error_type":"other"}}', b'not json'):
            with self.subTest(raw=raw):
                response_400 = mock.Mock(status=400)
                response_400.read.return_value = raw
                connection = mock.Mock()
                connection.getresponse.return_value = response_400
                with mock.patch("daemon.http.client.HTTPSConnection", return_value=connection):
                    service = LiveJevService(
                        bridge=NoWriteBridge(),
                        snapshot=sample_snapshot(),
                        key="x",
                        requester=JevClient(),
                    )
                    answer = service.process({"text": "いい感じにして"})
                self.assertEqual(answer["line"], "Jevに繋がりません。少し待ってから試してください")
                self.assertEqual(connection.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
