from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest import mock
import json
import unittest

import daemon as D
import plugin_script
from actions import ACTIONS
from bridge_client import Ack, BridgeResult, validate_arguments
from daemon import LiveJevService, Pending
from intent import Action, IntentResult, Step, TargetOrigin, _local_intent, build_request, extract_plugin_request, interpret_response, parse_local
from snapshot import Device, MasterTrack, Param, ReturnTrack, TargetKind, TargetRef
from tests.support import choice, response, sample_snapshot


def target_snapshot():
    base = sample_snapshot()
    return_device = Device(0, "Echo", (), "live_set return_tracks 0 devices 0")
    master_device = Device(0, "Limiter", (), "live_set master_track devices 0")
    returned = ReturnTrack(0, "Hall", 0.5, "-6.0 dB", 0.0, "C", False, False, (return_device,), "live_set return_tracks 0")
    master = MasterTrack("Master", base.master_volume, base.master_display, (master_device,))
    return replace(base, returns=(returned,), master=master)


class FirstClassTargetIntentTests(unittest.TestCase):
    def test_local_plugin_targets_master_return_alias_and_return_name(self) -> None:
        snapshot = target_snapshot()
        cases = {
            "マスターにValhalla入れて": TargetRef(TargetKind.MASTER),
            "リターンAにValhalla入れて": TargetRef(TargetKind.RETURN, 0),
            "HallにValhalla入れて": TargetRef(TargetKind.RETURN, 0),
            "insert Valhalla on master": TargetRef(TargetKind.MASTER),
            "insert Valhalla on return A": TargetRef(TargetKind.RETURN, 0),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                request = extract_plugin_request(text, snapshot)
                self.assertIsNotNone(request)
                self.assertFalse(request.target_missing)
                self.assertEqual(request.track, expected)

    def test_return_numbers_and_ordinals_resolve_without_becoming_ordinary_tracks(self) -> None:
        snapshot = target_snapshot()
        cases = (
            "リターン1にリバーブを入れて",
            "リターン１にリバーブ入れて",
            "リターントラック1にリバーブを入れて",
            "1番目のリターンにReverb入れて",
            "リターンの1番にReverb入れて",
            "put reverb on return 1",
            "put reverb on return track 1",
            "put reverb on the 1st return",
            "put reverb on first return",
        )
        for text in cases:
            with self.subTest(text=text):
                request = extract_plugin_request(text, snapshot)
                self.assertIsNotNone(request)
                self.assertEqual(request.track, TargetRef(TargetKind.RETURN, 0))
                self.assertFalse(request.target_missing)
                self.assertNotIn("リターン", request.raw_name)
                self.assertIn(request.raw_name.casefold(), {"reverb", "リバーブ"})

    def test_return_number_is_supported_by_local_actions_and_missing_numbers_fail_closed(self) -> None:
        snapshot = target_snapshot()
        cases = (
            ("リターン1をミュート", Action.MUTE),
            ("mute return 1", Action.MUTE),
            ("リターン1の名前をPlateにして", Action.RENAME),
            ("turn Echo off on return 1", Action.DEVICE_OFF),
        )
        for text, action in cases:
            with self.subTest(text=text):
                intent = parse_local(text, snapshot)
                self.assertIsNotNone(intent)
                self.assertEqual((intent.action, intent.track), (action, TargetRef(TargetKind.RETURN, 0)))
        missing = extract_plugin_request("リターン2にReverb入れて", snapshot)
        self.assertIsNotNone(missing)
        self.assertIsNone(missing.track)
        self.assertTrue(missing.target_missing)
        missing_action = parse_local("mute return 2", snapshot)
        self.assertIsNotNone(missing_action)
        self.assertIsNone(missing_action.track)
        self.assertGreaterEqual(missing_action.track_stated, 0.8)

    def test_return_ordinal_requests_return_details_without_ordinary_track_details(self) -> None:
        request = build_request(detailed_target_snapshot(), "1番目のリターンのEchoのFeedbackを下げて")
        self.assertIn("r0", request["state"]["detail_tracks"])
        self.assertNotIn(0, request["state"]["detail_tracks"])
        self.assertIn("リターン1", request["questions"]["track"]["criteria"]["r0"])

    def test_numeric_sends_remain_sends_on_the_selected_track(self) -> None:
        snapshot = target_snapshot()
        for text in ("センド1を上げて", "send 1 up"):
            with self.subTest(text=text):
                intent = parse_local(text, snapshot)
                self.assertIsNotNone(intent)
                self.assertEqual((intent.action, intent.track, intent.send), (Action.SEND, None, 0))

    def test_gemini_choices_include_first_class_targets_and_decode_typed_refs(self) -> None:
        snapshot = target_snapshot()
        request = build_request(snapshot, "Hallをミュート")
        criteria = request["questions"]["track"]["criteria"]
        self.assertIn("r0", criteria)
        self.assertIn("master", criteria)
        answer = {
            "answers": {
                "action": choice("mute"), "track": choice("r0"), "step": choice("none"),
                "track_stated": {"type": "choice", "choice": "named", "confidence": 0.9, "probabilities": {"named": 0.9}},
            }
        }
        intent = interpret_response(snapshot, "Hallをミュート", answer, ()).intent
        self.assertEqual(intent.track, TargetRef(TargetKind.RETURN, 0))
        self.assertEqual((intent.target_origin, intent.named_evidence), (TargetOrigin.NAMED, 0.9))


class FirstClassTargetActionTests(unittest.TestCase):
    class ReturnBridge:
        def __init__(self) -> None:
            self.mute = False
            self.name = "Hall"
            self.calls = []

        def run(self, arguments):
            arguments = list(arguments)
            self.calls.append(arguments)
            if "--api-get" in arguments:
                acks = []
                for at, flag in enumerate(arguments):
                    if flag != "--api-get":
                        continue
                    path, prop, request = arguments[at + 1:at + 4]
                    value = self.name if prop == "name" else self.mute
                    acks.append(Ack("api_get", request, value, path, prop))
                return BridgeResult(tuple(acks), 1, 0, False)
            if "--api-set" in arguments:
                at = arguments.index("--api-set")
                prop = arguments[at + 2]
                if prop == "name":
                    self.name = json.loads(arguments[at + 3])
                else:
                    self.mute = bool(int(arguments[at + 3]))
                return BridgeResult((), 1, 0, False)
            raise AssertionError(arguments)

    def test_return_mixer_actions_use_its_stable_path(self) -> None:
        snapshot = target_snapshot()
        ref = TargetRef(TargetKind.RETURN, 0)
        cases = (
            (Action.VOLUME, Step.UP_SMALL, "live_set return_tracks 0 mixer_device volume"),
            (Action.PAN, Step.UP_SMALL, "live_set return_tracks 0 mixer_device panning"),
            (Action.MUTE, Step.NONE, "live_set return_tracks 0"),
            (Action.SOLO, Step.NONE, "live_set return_tracks 0"),
        )
        for action, step, expected_path in cases:
            with self.subTest(action=action):
                intent = _local_intent(action, track=ref, step=step, utterance="Hall")
                batches = ACTIONS[action].apply(snapshot, intent)
                self.assertTrue(any(expected_path in batch for batch in batches), batches)

    def test_return_and_master_rename_use_their_stable_paths(self) -> None:
        snapshot = target_snapshot()
        cases = (
            (TargetRef(TargetKind.RETURN, 0), "live_set return_tracks 0"),
            (TargetRef(TargetKind.MASTER), "live_set master_track"),
        )
        for ref, path in cases:
            with self.subTest(path=path):
                intent = _local_intent(Action.RENAME, track=ref, text="New Name", target_origin=TargetOrigin.NAMED)
                batches = ACTIONS[Action.RENAME].apply(snapshot, intent)
                self.assertIn(["--write", "--api-set", path, "name", '"New Name"', mock.ANY], batches)

    def test_capability_refusals_name_the_unsupported_property(self) -> None:
        service = LiveJevService(bridge=mock.Mock(), snapshot=target_snapshot())
        cases = (
            (_local_intent(Action.SOLO, track="master", target_origin=TargetOrigin.MASTER, utterance="マスターをソロ"), "ソロ"),
            (_local_intent(Action.ARM, track=TargetRef(TargetKind.RETURN, 0), target_origin=TargetOrigin.NAMED, utterance="Hallをアーム"), "録音待機"),
            (_local_intent(Action.FOLD, track=TargetRef(TargetKind.RETURN, 0), target_origin=TargetOrigin.NAMED, utterance="Hallを折りたたむ"), "折りたたみ"),
            (_local_intent(Action.TRACK_STOP_CLIPS, track="master", target_origin=TargetOrigin.MASTER, utterance="マスターのクリップを止める"), "クリップ操作"),
        )
        for intent, reason in cases:
            with self.subTest(action=intent.action):
                answer = service._decision(IntentResult(intent, (), (), ()), "x")
                self.assertEqual(answer["kind"], "error")
                self.assertIn(reason, answer["line"])

    def test_return_write_and_receipt_undo_use_return_path_without_native_undo(self) -> None:
        bridge = self.ReturnBridge()
        service = LiveJevService(bridge=bridge, snapshot=target_snapshot())

        applied = service.process({"id": "write", "text": "Hallをミュート"})
        undone = service.process({"id": "undo", "cmd": "undo"})

        self.assertEqual((applied["kind"], bridge.mute, undone["kind"]), ("result", False, "result"))
        self.assertTrue(any("live_set return_tracks 0" in call for call in bridge.calls))
        self.assertFalse(any("--api-call" in call and "undo" in call for call in bridge.calls))

    def test_return_rename_receipt_restores_by_path_without_native_undo(self) -> None:
        bridge = self.ReturnBridge()
        bridge.name = "New Name"
        service = LiveJevService(bridge=bridge, snapshot=replace(target_snapshot(), returns=(replace(target_snapshot().returns[0], name="New Name"),)))
        receipt = D.Receipt(
            Action.RENAME, TargetRef(TargetKind.RETURN, 0), None, "Hall", "New Name", Step.NONE, True,
            entries=(D.ReceiptEntry("live_set return_tracks 0", "name", "New Name", "Hall", "New Name", write_kind="rename"),),
        )

        self.assertIsNone(service._restore_receipt(receipt))
        self.assertEqual(bridge.name, "Hall")
        self.assertFalse(any("--api-call" in call and "undo" in call for call in bridge.calls))


class FirstClassTargetPluginTests(unittest.TestCase):
    def test_plugin_flow_passes_exact_target_path_and_verifies_that_target(self) -> None:
        snapshot = target_snapshot()
        loaded = replace(snapshot.returns[0], devices=snapshot.returns[0].devices + (Device(1, "Valhalla", (), "live_set return_tracks 0 devices 1"),))
        fresh = replace(snapshot, returns=(loaded,))
        service = LiveJevService(bridge=mock.Mock(), snapshot=snapshot)
        service.reader = mock.Mock()
        service.reader.read.return_value = (fresh, 1)
        intent = _local_intent(Action.INSERT_PLUGIN, track=TargetRef(TargetKind.RETURN, 0), plugin="Valhalla", target_origin=TargetOrigin.NAMED, utterance="HallにValhalla")
        with mock.patch.object(D.plugin_script, "load", return_value={"target_path": "live_set return_tracks 0", "devices_after": ["Valhalla"]}) as load:
            service._run_plugin_flow(intent, snapshot)
        load.assert_called_once_with("Valhalla", "live_set return_tracks 0", "")

    def test_script_client_and_builtin_command_keep_master_and_return_paths(self) -> None:
        with mock.patch.object(plugin_script, "call", return_value={"ok": True}) as call:
            plugin_script.load("Reverb", "live_set master_track")
        self.assertEqual(call.call_args.kwargs["target_path"], "live_set master_track")
        self.assertNotIn("track_index", call.call_args.kwargs)
        for path in ("live_set master_track", "live_set return_tracks 0"):
            validate_arguments(["--write", "--api-insert-device", path, "Reverb", "", "request"])


def detailed_target_snapshot():
    base = sample_snapshot()
    echo_on = Param(0, "Device On", 1.0, 0.0, 1.0, "On", "live_set return_tracks 0 devices 0 parameters 0")
    feedback = Param(1, "Feedback", 0.6, 0.0, 1.0, "60%", "live_set return_tracks 0 devices 0 parameters 1")
    limiter_on = Param(0, "Device On", 1.0, 0.0, 1.0, "On", "live_set master_track devices 0 parameters 0")
    ceiling = Param(1, "Ceiling", 0.8, 0.0, 1.0, "80%", "live_set master_track devices 0 parameters 1")
    returned = ReturnTrack(
        0, "Hall", 0.5, "-6.0 dB", 0.0, "C", False, False,
        (Device(0, "Echo", (echo_on, feedback), "live_set return_tracks 0 devices 0"),),
        "live_set return_tracks 0",
    )
    master = MasterTrack(
        "Master", base.master_volume, base.master_display,
        (Device(0, "Limiter", (limiter_on, ceiling), "live_set master_track devices 0"),),
    )
    return replace(base, returns=(returned,), master=master)


class TypedTargetBridge:
    def __init__(self, snapshot=None, selected_path="live_set tracks 1") -> None:
        self.snapshot = snapshot or detailed_target_snapshot()
        self.selected_path = selected_path
        self.calls = []
        self.names = {target.path: target.name for target in self.snapshot.addressable_targets}
        self.props = {
            (target.path, prop): getattr(target, prop)
            for target in self.snapshot.addressable_targets
            for prop in ("mute", "solo") if hasattr(target, prop)
        }
        self.params = {
            parameter.path: parameter.value
            for target in self.snapshot.addressable_targets
            for device in target.devices
            for parameter in device.params
        }
        self.param_names = {
            parameter.path: parameter.name
            for target in self.snapshot.addressable_targets
            for device in target.devices
            for parameter in device.params
        }
        self.device_names = {
            device.path: device.name
            for target in self.snapshot.addressable_targets
            for device in target.devices
        }

    def run(self, arguments):
        arguments = list(arguments)
        self.calls.append(arguments)
        acks = []
        offset = 0
        while offset < len(arguments):
            flag = arguments[offset]
            if flag == "--write":
                offset += 1
            elif flag == "--api-session-context":
                request = arguments[offset + 1]
                payload = {"selected": {"track": {"path": self.selected_path}}}
                acks.append(Ack("api_session_context", request, payload))
                offset += 2
            elif flag == "--api-get":
                path, prop, request = arguments[offset + 1:offset + 4]
                if prop == "name":
                    value = self.device_names.get(path, self.names.get(path))
                else:
                    value = self.props[(path, prop)]
                acks.append(Ack("api_get", request, value, path, prop))
                offset += 4
            elif flag == "--api-set":
                path, prop, raw = arguments[offset + 1:offset + 4]
                if prop == "name":
                    self.names[path] = json.loads(raw)
                else:
                    self.props[(path, prop)] = bool(int(raw))
                offset += 5
            elif flag == "--api-parameter-set":
                path, raw = arguments[offset + 1:offset + 3]
                self.params[path] = float(raw)
                offset += 4
            elif flag == "--api-device-parameters":
                path, request = arguments[offset + 1:offset + 3]
                parameters = [
                    {"path": param_path, "name": self.param_names[param_path], "value": value, "min": 0.0, "max": 1.0}
                    for param_path, value in self.params.items() if param_path.startswith(path + " parameters ")
                ]
                acks.append(Ack("api_device_parameters", request, {"parameters": parameters}, path))
                offset += 3
            else:
                raise AssertionError(arguments[offset:])
        return BridgeResult(tuple(acks), 1, 0, False)


class FirstClassTargetBatchTwoTests(unittest.TestCase):
    def test_item1_plugin_insert_process_keeps_master_and_return_paths(self) -> None:
        snapshot = detailed_target_snapshot()
        cases = (
            ("マスターにValhallaを挿して", "live_set master_track"),
            ("HallにValhallaを挿して", "live_set return_tracks 0"),
        )
        for text, path in cases:
            with self.subTest(text=text):
                service = LiveJevService(bridge=TypedTargetBridge(snapshot), snapshot=snapshot)
                service._plugin_names_cache = ("Valhalla",)
                service.reader = mock.Mock()
                service.reader.read.return_value = (snapshot, 1)
                with mock.patch.object(D.plugin_script, "load", return_value={"target_path": path, "devices_after": ["Valhalla"]}) as load:
                    answer = service.process({"text": text})
                self.assertEqual(answer["kind"], "result")
                load.assert_called_once_with("Valhalla", path, "")

    def test_item1_builtin_insert_process_keeps_master_path(self) -> None:
        snapshot = detailed_target_snapshot()
        service = LiveJevService(bridge=TypedTargetBridge(snapshot), snapshot=snapshot)
        service._plugin_names_cache = ()
        service.reader = mock.Mock()
        service.reader.read.return_value = (snapshot, 1)
        with mock.patch.object(D.plugin_script, "load", return_value={"target_path": "live_set master_track", "devices_after": ["Limiter"]}) as load:
            answer = service.process({"text": "マスターにLimiter入れて"})
        self.assertEqual(answer["kind"], "result")
        load.assert_called_once_with("Limiter", "live_set master_track", "")

    def test_item2_return_rename_process_and_undo_restore_name(self) -> None:
        snapshot = detailed_target_snapshot()
        bridge = TypedTargetBridge(snapshot)
        service = LiveJevService(bridge=bridge, snapshot=snapshot)

        renamed = service.process({"text": "Hallの名前をPlateにして"})
        undone = service.process({"cmd": "undo"})

        self.assertEqual((renamed["kind"], undone["kind"]), ("result", "result"))
        self.assertEqual(bridge.names["live_set return_tracks 0"], "Hall")
        self.assertFalse(any("--api-call" in call and "undo" in call for call in bridge.calls))

    def test_item2_master_rename_is_capability_refusal_without_write(self) -> None:
        snapshot = detailed_target_snapshot()
        bridge = TypedTargetBridge(snapshot)
        answer = LiveJevService(bridge=bridge, snapshot=snapshot).process({"text": "マスターの名前をBusにして"})
        self.assertEqual(answer["kind"], "error")
        self.assertIn("名前変更", answer["line"])
        self.assertFalse(any("--api-set" in call for call in bridge.calls))

    def test_item2_expected_batch_value_accepts_json_string(self) -> None:
        self.assertEqual(
            LiveJevService._expected_batch_value(["--write", "--api-set", "live_set return_tracks 0", "name", '"Bus"', "id"]),
            "Bus",
        )

    def test_item3_owner_device_resolution_targets_master_and_return_and_undoes(self) -> None:
        snapshot = detailed_target_snapshot()
        cases = (
            ("Limiterをオフ", "live_set master_track devices 0 parameters 0"),
            ("Echoをオフ", "live_set return_tracks 0 devices 0 parameters 0"),
        )
        for text, path in cases:
            with self.subTest(text=text):
                bridge = TypedTargetBridge(snapshot)
                service = LiveJevService(bridge=bridge, snapshot=snapshot, key="x", requester=lambda *_: response("device_off", "none"))
                applied = service.process({"text": text})
                self.assertEqual(applied["kind"], "result")
                self.assertEqual(bridge.params[path], 0.0)
                undone = service.process({"cmd": "undo"})
                self.assertEqual(undone["kind"], "result")
                self.assertEqual(bridge.params[path], 1.0)

    def test_item4_send_clarification_accepts_letter_phrase_and_return_name(self) -> None:
        snapshot = detailed_target_snapshot()
        base = _local_intent(Action.SEND, track=0, step=Step.UP_SMALL, utterance="センドを上げて")
        for answer_text in ("A", "センドA", "send a", "Hall"):
            with self.subTest(answer=answer_text):
                service = LiveJevService(bridge=TypedTargetBridge(snapshot), snapshot=snapshot)
                service.pending = Pending(IntentResult(base, (), (), ()), "send", request_id="q")
                filled = service._fill_pending(answer_text)
                self.assertIsNotNone(filled)
                self.assertEqual(filled.intent.send, 0)

    def test_item5_request_details_and_response_use_typed_target_keys(self) -> None:
        snapshot = detailed_target_snapshot()
        request = build_request(snapshot, "マスターのCeilingを上げて")
        self.assertIn("param_master", request["questions"])
        self.assertIn("device_master", request["questions"])
        request = build_request(snapshot, "HallのEchoのFeedbackを下げて")
        self.assertIn("param_r0", request["questions"])
        self.assertIn("device_r0", request["questions"])

    def test_item5_param_on_return_and_master_process_with_receipt_and_undo(self) -> None:
        snapshot = detailed_target_snapshot()
        cases = (
            ("マスターのCeilingを上げて", "master", "d0p1", "live_set master_track devices 0 parameters 1"),
            ("HallのEchoのFeedbackを下げて", "r0", "d0p1", "live_set return_tracks 0 devices 0 parameters 1"),
        )
        for text, target, param, path in cases:
            with self.subTest(text=text):
                bridge = TypedTargetBridge(snapshot)
                answer = response("param", target, "up_small" if target == "master" else "down_small")
                answer["answers"][f"param_{target}"] = choice(param)
                service = LiveJevService(bridge=bridge, snapshot=snapshot, key="x", requester=lambda *_: answer)
                old = bridge.params[path]
                applied = service.process({"text": text})
                self.assertEqual(applied["kind"], "result")
                self.assertNotEqual(bridge.params[path], old)
                self.assertEqual(service.process({"cmd": "undo"})["kind"], "result")
                self.assertEqual(bridge.params[path], old)

    def test_item5_track_clarification_accepts_return_and_master(self) -> None:
        snapshot = detailed_target_snapshot()
        unresolved = _local_intent(Action.DEVICE_OFF, device_name="Echo", utterance="Echoをオフ")
        for answer_text, expected in (("Hall", TargetRef(TargetKind.RETURN, 0)), ("マスター", TargetRef(TargetKind.MASTER))):
            with self.subTest(answer=answer_text):
                service = LiveJevService(bridge=TypedTargetBridge(snapshot), snapshot=snapshot)
                service.pending = Pending(IntentResult(unresolved, (), (), ()), "track", request_id="q")
                filled = service._fill_pending(answer_text)
                self.assertIsNotNone(filled)
                self.assertEqual(filled.intent.track, expected)

    def test_item6_selected_return_and_master_become_typed_selected_targets(self) -> None:
        snapshot = detailed_target_snapshot()
        cases = (
            ("live_set return_tracks 0", "ミュート", TargetRef(TargetKind.RETURN, 0), "result"),
            ("live_set master_track", "ミュート", TargetRef(TargetKind.MASTER), "error"),
        )
        for path, text, expected, kind in cases:
            with self.subTest(path=path):
                bridge = TypedTargetBridge(snapshot, selected_path=path)
                service = LiveJevService(bridge=bridge, snapshot=snapshot, key="x", requester=lambda *_: response("mute", "none"))
                answer = service.process({"text": text})
                self.assertEqual(answer["kind"], kind)
                selected = service._apply_selected_track(IntentResult(_local_intent(Action.MUTE, utterance=text), (), (), ())).intent
                self.assertEqual((selected.track, selected.target_origin), (expected, TargetOrigin.SELECTED))

    def test_item7_english_return_names_and_aliases_resolve(self) -> None:
        snapshot = detailed_target_snapshot()
        cases = {
            "mute return a": TargetRef(TargetKind.RETURN, 0),
            "mute Hall": TargetRef(TargetKind.RETURN, 0),
            "turn down return a 3 db": TargetRef(TargetKind.RETURN, 0),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                intent = parse_local(text, snapshot)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.track, expected)

    def test_item7_exact_ordinary_master_name_wins_and_keywords_are_ambiguous(self) -> None:
        snapshot = detailed_target_snapshot()
        tracks = list(snapshot.tracks)
        tracks[0] = replace(tracks[0], name="Master")
        snapshot = replace(snapshot, tracks=tuple(tracks))
        exact = parse_local("mute Master", snapshot)
        longer = parse_local("mute master track", snapshot)
        self.assertEqual(exact.track, 0)
        self.assertIsNone(longer.track)

    def test_item7_ordinal_consumed_before_numeric_track_name(self) -> None:
        snapshot = detailed_target_snapshot()
        tracks = list(snapshot.tracks)
        tracks[0] = replace(tracks[0], name="2")
        snapshot = replace(snapshot, tracks=tuple(tracks))
        self.assertEqual(parse_local("mute track 2", snapshot).track, 1)
        self.assertEqual(parse_local("トラック2をミュート", snapshot).track, 1)


class ReturnTrackCreationTests(unittest.TestCase):
    def test_local_return_creation_parses_plain_named_and_device_forms(self) -> None:
        snapshot = target_snapshot()
        plain = {
            "リターントラックを作って": None,
            "リターンを追加して": None,
            "新しいリターントラック": None,
            "リターントラックを新しく作って": None,
            "Plateという名前でリターンを作って": "Plate",
            "add a return track": None,
            "new return": None,
            "create a return track called Plate": "Plate",
        }
        for text, name in plain.items():
            with self.subTest(text=text):
                self.assertIsNone(extract_plugin_request(text, snapshot))
                intent = parse_local(text, snapshot)
                self.assertIsNotNone(intent)
                self.assertEqual((intent.action, intent.text, intent.track_kind), (Action.ADD_RETURN_TRACK, name, "return"))
        native = parse_local("Reverb入りのリターンを作って", snapshot)
        self.assertIsNotNone(native)
        self.assertEqual((native.action, native.track_kind, native.native_device), (Action.ADD_TRACK_WITH_DEVICE, "return", "Reverb"))
        plugin = extract_plugin_request("new return with Valhalla", snapshot)
        self.assertIsNotNone(plugin)
        self.assertEqual((plugin.action, plugin.track_kind, plugin.raw_name), (Action.ADD_TRACK_WITH_PLUGIN, "return", "valhalla"))
        japanese_plugin = extract_plugin_request("ValhallaVintageVerb入りのリターンを作って", snapshot)
        self.assertIsNotNone(japanese_plugin)
        self.assertEqual(
            (japanese_plugin.action, japanese_plugin.track_kind, japanese_plugin.raw_name),
            (Action.ADD_TRACK_WITH_PLUGIN, "return", "ValhallaVintageVerb"),
        )

    def test_existing_return_commands_are_not_misparsed_as_creation(self) -> None:
        snapshot = target_snapshot()
        for text in ("リターンAにValhalla入れて", "リターン1にリバーブを入れて"):
            with self.subTest(text=text):
                request = extract_plugin_request(text, snapshot)
                self.assertEqual((request.action, request.track), (Action.INSERT_PLUGIN, TargetRef(TargetKind.RETURN, 0)))
        self.assertEqual(parse_local("mute return a", snapshot).action, Action.MUTE)
        self.assertIsNone(extract_plugin_request("センドAを上げて", snapshot))
        self.assertIsNone(parse_local("センドAを上げて", snapshot))

    def test_plugin_script_forwards_return_kind_without_track_index(self) -> None:
        with mock.patch.object(plugin_script, "call", return_value={"ok": True, "return_index": 2}) as call:
            plugin_script.add_track("return", "Plate", "Reverb")
        self.assertEqual(call.call_args.kwargs["kind"], "return")
        self.assertNotIn("track_index", call.call_args.kwargs)

    def test_remote_script_adds_return_and_rejects_limit_unknown_kind_and_instrument(self) -> None:
        from tests.test_hardening import _livejev_class

        cls = _livejev_class()
        instance = object.__new__(cls)
        instance.log_message = mock.Mock()
        browser = SimpleNamespace(hotswap_target=None, load_item=mock.Mock())
        instance._browser = mock.Mock(return_value=browser)
        instance._find_item = mock.Mock(return_value={"name": "Reverb", "section": "audio_effects"})
        instance._resolve_browser_item = mock.Mock(return_value=object())
        created = SimpleNamespace(name="C-Return", devices=[])

        class Song:
            def __init__(self):
                self.tracks = [SimpleNamespace(name="Track")]
                self.return_tracks = [SimpleNamespace(name="A"), SimpleNamespace(name="B")]
                self.view = SimpleNamespace(selected_track=self.tracks[0])
                self.midi_calls = 0
                self.audio_calls = 0

            def create_return_track(self):
                self.return_tracks.append(created)
                return created

            def create_midi_track(self, _index):
                self.midi_calls += 1

            def create_audio_track(self, _index):
                self.audio_calls += 1

            def begin_undo_step(self):
                pass

            def end_undo_step(self):
                pass

        song = Song()
        instance.song = mock.Mock(return_value=song)
        answer = instance._add_track({"kind": "return", "name": "Plate", "device": "Reverb"})
        self.assertEqual(answer, {
            "ok": True, "kind": "return", "return_index": 2,
            "target_path": "live_set return_tracks 2", "track": "Plate", "devices_after": [],
        })
        self.assertEqual(song.view.selected_track, created)
        browser.load_item.assert_called_once()
        self.assertEqual((song.midi_calls, song.audio_calls), (0, 0))

        invalid = instance._add_track({"kind": "video"})
        self.assertEqual(invalid, {"ok": False, "error": "invalid_kind"})
        self.assertEqual((song.midi_calls, song.audio_calls), (0, 0))

        song.return_tracks = [SimpleNamespace(name=str(i)) for i in range(12)]
        self.assertEqual(instance._add_track({"kind": "return"}), {"ok": False, "error": "return_limit"})
        instance._find_item.return_value = {"name": "Operator", "section": "instruments"}
        song.return_tracks = []
        self.assertEqual(
            instance._add_track({"kind": "return", "device": "Operator"}),
            {"ok": False, "error": "instrument_on_non_midi", "name": "Operator"},
        )
        self.assertEqual(song.return_tracks, [])

    def test_daemon_creates_return_rereads_snapshot_and_refuses_undo(self) -> None:
        before = target_snapshot()
        second = replace(before.returns[0], index=1, name="B-Return", path="live_set return_tracks 1", devices=())
        before = replace(before, returns=(before.returns[0], second))
        third = replace(before.returns[0], index=2, name="C-Return", path="live_set return_tracks 2", devices=())
        after = replace(before, returns=before.returns + (third,))
        bridge = SimpleNamespace(run=lambda _arguments: BridgeResult((), 1, 0, False))
        service = LiveJevService(bridge=bridge, snapshot=before, key="x", requester=lambda *_: self.fail("Jev called"))
        service._selected_track_index = mock.Mock(return_value=None)
        service.reader = mock.Mock()
        service.reader.read.return_value = (after, 1)
        with mock.patch.object(D.plugin_script, "ping", return_value=True), mock.patch.object(
            D.plugin_script, "add_track", return_value={"ok": True, "kind": "return", "return_index": 2, "target_path": third.path, "track": third.name, "devices_after": []},
        ) as add:
            result = service.process({"id": "create", "text": "リターントラックを作って"})
        self.assertEqual(result["line"], "リターントラック「C-Return」を追加しました")
        add.assert_called_once_with("return", None, None)
        self.assertEqual(extract_plugin_request("リターンCにEcho入れて", service.snapshot).track, TargetRef(TargetKind.RETURN, 2))
        self.assertEqual(extract_plugin_request("リターン3にEcho入れて", service.snapshot).track, TargetRef(TargetKind.RETURN, 2))
        self.assertEqual(service.process({"cmd": "undo"})["kind"], "info")

    def test_return_plugin_uri_load_uses_return_path_and_verifies_new_return(self) -> None:
        before = target_snapshot()
        added = ReturnTrack(
            1, "B-Return", 0.5, "-6.0 dB", 0.0, "C", False, False,
            (Device(0, "Valhalla", (), "live_set return_tracks 1 devices 0"),),
            "live_set return_tracks 1",
        )
        after = replace(before, returns=before.returns + (added,))
        service = LiveJevService(bridge=SimpleNamespace(run=lambda _arguments: BridgeResult((), 1, 0, False)), snapshot=before)
        service.reader = mock.Mock()
        service.reader.read.return_value = (after, 1)
        service._plugin_uris = {"Valhalla": "browser://valhalla"}
        intent = _local_intent(Action.ADD_TRACK_WITH_PLUGIN, plugin="Valhalla", track_kind="return")
        with mock.patch.object(D.plugin_script, "ping", return_value=True), mock.patch.object(
            D.plugin_script, "add_track", return_value={"ok": True, "kind": "return", "return_index": 1, "target_path": added.path, "track": added.name, "devices_after": []},
        ) as add, mock.patch.object(
            D.plugin_script, "load", return_value={"ok": True, "target_path": added.path, "devices_after": ["Valhalla"]},
        ) as load:
            _elapsed, rebound = service._run_plugin_flow(intent, before)
        add.assert_called_once_with("return", None, None)
        load.assert_called_once_with("Valhalla", added.path, "browser://valhalla")
        self.assertEqual(rebound.track, TargetRef(TargetKind.RETURN, 1))

    def test_return_limit_and_unavailable_script_are_localized_without_bridge_writes(self) -> None:
        bridge = SimpleNamespace(calls=[], run=lambda arguments: bridge.calls.append(list(arguments)) or BridgeResult((), 1, 0, False))
        service = LiveJevService(bridge=bridge, snapshot=target_snapshot())
        intent = _local_intent(Action.ADD_RETURN_TRACK, track_kind="return")
        with mock.patch.object(D.plugin_script, "add_track", side_effect=D.plugin_script.ScriptError("return_limit")):
            with self.assertRaisesRegex(ValueError, "12本"):
                service._run_add_track_via_script(intent)
            service.lang = "en"
            with self.assertRaisesRegex(ValueError, "12 return tracks"):
                service._run_add_track_via_script(intent)
            service.lang = "ja"
        service._selected_track_index = mock.Mock(return_value=None)
        with mock.patch.object(D.plugin_script, "ping", return_value=False):
            answer = service.process({"text": "リターントラックを作って"})
        self.assertEqual(answer["line"], "Liveに繋がりません。装置が載っているか確認してください")
        self.assertEqual(bridge.calls, [])


if __name__ == "__main__":
    unittest.main()
