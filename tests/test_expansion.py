"""Version 0.08, phase 1: transport and track verbs."""

from __future__ import annotations

from dataclasses import replace
import unittest
import unittest.mock

from actions import ACTIONS, bar_to_beats
from bridge_client import Ack, BridgeResult, validate_arguments
from daemon import LiveJevService
from intent import Action, Number, Step, parse_local, parse_number
from snapshot import song_fields
from tests.support import sample_snapshot


class RecordingBridge:
    """Fake bridge that records writes and returns fixed values for reads."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments):
        arguments = list(arguments)
        self.calls.append(arguments)
        if "--api-set" in arguments or "--api-call" in arguments or "--tempo" in arguments:
            return BridgeResult((), 1, 0, False)
        at = arguments.index("--api-get")
        path, prop, request_id = arguments[at + 1], arguments[at + 2], arguments[at + 3]
        names = {"live_set tracks 0": "Pad", "live_set tracks 1": "Bass", "live_set tracks 2": "Drums"}
        if prop == "name":
            payload = names[path]
        elif prop == "current_song_time":
            payload = 64.0
        elif prop == "current_monitoring_state":
            payload = 0
        else:
            payload = 1
        return BridgeResult((Ack("api_get", request_id, payload, path, prop),), 1, 0, False)


def _snapshot_with_song():
    return replace(sample_snapshot(), song={"signature_numerator": 4, "loop": False, "metronome": False})


class LocalPhraseTests(unittest.TestCase):
    def test_transport_phrases_skip_jev(self) -> None:
        snapshot = _snapshot_with_song()
        cases = {
            "続きから": Action.CONTINUE,
            "録音開始": Action.RECORD_ON,
            "録音停止": Action.RECORD_OFF,
            "ループして": Action.LOOP_ON,
            "ループ解除": Action.LOOP_OFF,
            "メトロノームつけて": Action.METRONOME_ON,
            "メトロノーム消して": Action.METRONOME_OFF,
            "アンドゥ": Action.UNDO,
            "やり直し": Action.REDO,
            "全部止めて": Action.STOP_ALL_CLIPS,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                intent = parse_local(text, snapshot)
                self.assertIsNotNone(intent)
                self.assertIs(intent.action, expected)

    def test_bar_phrase_and_arm_toggle(self) -> None:
        snapshot = _snapshot_with_song()
        jump = parse_local("17小節へ", snapshot)
        self.assertIs(jump.action, Action.JUMP_TO_BAR)
        self.assertEqual(jump.number, Number(17.0, "raw"))
        self.assertEqual(bar_to_beats(snapshot, 17.0), 64.0)
        arm = parse_local("Bassを録音待機に", snapshot)
        self.assertIs(arm.action, Action.ARM)
        self.assertEqual(arm.track, 1)
        disarm = parse_local("Bassをアーム解除", snapshot)
        self.assertIs(disarm.action, Action.DISARM)

    def test_jump_number_reads_bars_only(self) -> None:
        self.assertEqual(parse_number("17小節から", Action.JUMP_TO_BAR), Number(17.0, "raw"))
        self.assertEqual(parse_number("頭から", Action.JUMP_TO_BAR), Number(1.0, "raw"))
        self.assertIsNone(parse_number("-6dBに", Action.JUMP_TO_BAR))


class ApplyAndAllowlistTests(unittest.TestCase):
    def test_new_batches_pass_allowlist(self) -> None:
        snapshot = _snapshot_with_song()
        samples = [
            (Action.LOOP_ON, None, Step.NONE, None),
            (Action.METRONOME_OFF, None, Step.NONE, None),
            (Action.UNDO, None, Step.NONE, None),
            (Action.CONTINUE, None, Step.NONE, None),
            (Action.STOP_ALL_CLIPS, None, Step.NONE, None),
            (Action.MONITOR_AUTO, 1, Step.NONE, None),
            (Action.ARM, 1, Step.NONE, None),
            (Action.TRACK_STOP_CLIPS, 2, Step.NONE, None),
            (Action.JUMP_TO_BAR, None, Step.SET, Number(17.0, "raw")),
        ]
        for action, track, step, number in samples:
            with self.subTest(action=action):
                intent = parse_local("再生", snapshot)
                intent = replace(intent, action=action, track=track, track_conf=1.0 if track is not None else 0.0, step=step, number=number)
                batches = ACTIONS[action].apply(snapshot, intent)
                self.assertEqual(len(batches), 2)
                for batch in batches:
                    validate_arguments(batch)
                self.assertEqual(batches[0][0], "--write")
                self.assertNotIn("--write", batches[1])

    def test_jump_writes_beats_from_bar(self) -> None:
        snapshot = _snapshot_with_song()
        intent = replace(parse_local("再生", snapshot), action=Action.JUMP_TO_BAR, step=Step.SET, number=Number(17.0, "raw"))
        write = ACTIONS[Action.JUMP_TO_BAR].apply(snapshot, intent)[0]
        self.assertEqual(write[:5], ["--write", "--api-set", "live_set", "current_song_time", "64"])

    def test_destructive_and_unknown_calls_are_rejected(self) -> None:
        rejected = [
            ["--write", "--api-call", "live_set", "delete_track", "[0]", "x"],
            ["--write", "--api-set", "live_set", "tempo", "120", "x"],
            ["--write", "--api-set", "live_set tracks 0", "name", "1", "x"],
            ["--write", "--api-call", "live_set tracks 0", "delete_device", "[]", "x"],
            ["--write", "--api-set", "live_set tracks 0", "current_monitoring_state", "5", "x"],
            ["--write", "--api-set", "live_set", "current_song_time", "-1", "x"],
            ["--delete-midi-tracks", "1"],
            ["--write", "--api-call", "live_set", "undo", "[1]", "x"],
        ]
        for arguments in rejected:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    validate_arguments(arguments)

    def test_song_fields_keeps_only_known_keys(self) -> None:
        song = song_fields({"loop": 1, "tempo": 120, "signature_numerator": 3, "record_mode": 0, "junk": 1})
        self.assertEqual(song, {"loop": 1, "signature_numerator": 3, "record_mode": 0})


class ConfirmGateTests(unittest.TestCase):
    def setUp(self) -> None:
        import daemon as D
        self._confirm_patch = unittest.mock.patch.object(D, "REQUIRE_CONFIRM", True)
        self._confirm_patch.start()
        self.addCleanup(self._confirm_patch.stop)

    def _service(self, bridge):
        return LiveJevService(
            bridge=bridge,
            snapshot=_snapshot_with_song(),
            key="x",
            requester=lambda _payload, _key: (_ for _ in ()).throw(AssertionError("Jev を呼んだ")),
            llm_key=None,
            rewriter=lambda *_args: (_ for _ in ()).throw(AssertionError("LLM を呼んだ")),
        )

    def test_record_on_waits_for_confirmation(self) -> None:
        bridge = RecordingBridge()
        service = self._service(bridge)
        answer = service.process({"id": "1", "text": "録音開始"})
        self.assertEqual(answer["kind"], "confirm")
        self.assertEqual(answer["options"], ["はい", "やめる"])
        self.assertEqual(bridge.calls, [])
        cancelled = service.process({"id": "2", "confirm": False})
        self.assertEqual(cancelled["kind"], "info")
        self.assertEqual(bridge.calls, [])

    def test_confirmation_executes_and_text_answer_works(self) -> None:
        bridge = RecordingBridge()
        service = self._service(bridge)
        service.process({"id": "1", "text": "録音開始"})
        answer = service.process({"id": "2", "text": "はい"})
        self.assertEqual(answer["kind"], "result")
        self.assertTrue(any("--api-set" in call and "session_record" in call for call in bridge.calls))
        self.assertTrue(service.snapshot.song.get("session_record"))

    def test_loop_on_runs_without_confirmation(self) -> None:
        bridge = RecordingBridge()
        service = self._service(bridge)
        answer = service.process({"id": "1", "text": "ループして"})
        self.assertEqual(answer["kind"], "result")
        self.assertIn("ループ オン", answer["line"])
        self.assertTrue(service.snapshot.song.get("loop"))

    def test_monitor_and_jump_readback(self) -> None:
        bridge = RecordingBridge()
        service = self._service(bridge)
        jump = service.process({"id": "1", "text": "17小節へ"})
        self.assertEqual(jump["kind"], "result")
        self.assertIn("17小節", jump["line"])
        self.assertEqual(service.snapshot.song.get("current_song_time"), 64.0)


if __name__ == "__main__":
    unittest.main()


class ClipSceneDeviceTests(unittest.TestCase):
    def _snapshot(self):
        from snapshot import Clip, Scene
        base = _snapshot_with_song()
        tracks = list(base.tracks)
        tracks[1] = replace(tracks[1], clips=(Clip(0, "Bass Loop", "live_set tracks 1 clip_slots 0 clip"), Clip(2, "Bass Fill", "live_set tracks 1 clip_slots 2 clip")))
        return replace(base, tracks=tuple(tracks), scenes=(Scene(0, "Intro", "live_set scenes 0"), Scene(1, "Verse", "live_set scenes 1")))

    def test_request_adds_scene_clip_device_heads(self) -> None:
        from intent import build_request
        questions = build_request(self._snapshot(), "x")["questions"]
        self.assertIn("scene", questions)
        self.assertEqual(set(questions["scene"]["criteria"]), {"s0", "s1", "none"})
        self.assertEqual(set(questions["clip_t1"]["criteria"]), {"c0", "c2", "none"})
        self.assertEqual(set(questions["device_t0"]["criteria"]), {"d0", "none"})

    def test_local_scene_and_clip_phrases(self) -> None:
        snapshot = self._snapshot()
        scene = parse_local("シーン2を発射", snapshot)
        self.assertIs(scene.action, Action.LAUNCH_SCENE)
        self.assertEqual(scene.scene, 1)
        clip = parse_local("Bassのクリップ3を再生", snapshot)
        self.assertIs(clip.action, Action.LAUNCH_CLIP)
        self.assertEqual((clip.track, clip.clip), (1, 2))
        stop = parse_local("Bassのクリップ1を止めて", snapshot)
        self.assertIs(stop.action, Action.STOP_CLIP)

    def test_launch_batches_pass_allowlist_and_scene_readback(self) -> None:
        snapshot = self._snapshot()
        scene_intent = parse_local("シーン1", snapshot)
        batches = ACTIONS[Action.LAUNCH_SCENE].apply(snapshot, scene_intent)
        self.assertEqual(batches[0][:4], ["--write", "--api-call", "live_set scenes 0", "fire"])
        for batch in batches:
            validate_arguments(batch)
        clip_intent = parse_local("Bassのクリップ3を再生", snapshot)
        clip_batches = ACTIONS[Action.LAUNCH_CLIP].apply(snapshot, clip_intent)
        self.assertEqual(clip_batches[0][:4], ["--write", "--api-call", "live_set tracks 1 clip_slots 2", "fire"])
        for batch in clip_batches:
            validate_arguments(batch)
        with self.assertRaises(ValueError):
            validate_arguments(["--write", "--api-call", "live_set tracks 1 clip_slots 2", "delete_clip", "[]", "x"])

    def test_interpret_picks_clip_and_device_from_track_heads(self) -> None:
        from intent import interpret_response
        from tests.support import choice
        snapshot = self._snapshot()
        answers = {
            "action": choice("launch_clip", 0.95, launch_clip=0.95),
            "track": choice("none", 0.5, none=0.5),
            "step": choice("none", 0.9, none=0.9),
            "clip_t1": choice("c2", 0.93, c2=0.93),
        }
        result = interpret_response(snapshot, "ベースフィル鳴らして", {"answers": answers})
        self.assertEqual((result.intent.track, result.intent.clip), (1, 2))
        device_answers = {
            "action": choice("device_off", 0.95, device_off=0.95),
            "track": choice("t0", 0.9, t0=0.9),
            "step": choice("none", 0.9, none=0.9),
            "device_t0": choice("d0", 0.97, d0=0.97),
        }
        result = interpret_response(snapshot, "リバーブ切って", {"answers": device_answers})
        self.assertIs(result.intent.action, Action.DEVICE_OFF)
        self.assertEqual(result.intent.device.name, "Reverb")
        self.assertIsNotNone(result.intent.param)
        batches = ACTIONS[Action.DEVICE_OFF].apply(snapshot, result.intent)
        self.assertEqual(batches[0][:3], ["--write", "--api-parameter-set", "live_set tracks 0 devices 0 parameters 0"]); self.assertEqual(float(batches[0][3]), 0.0)


class SendRenameAddTests(unittest.TestCase):
    def setUp(self) -> None:
        import daemon as D
        self._confirm_patch = unittest.mock.patch.object(D, "REQUIRE_CONFIRM", True)
        self._confirm_patch.start()
        self.addCleanup(self._confirm_patch.stop)

    def _snapshot(self):
        base = _snapshot_with_song()
        tracks = tuple(replace(track, sends=(0.2, 0.0)) for track in base.tracks)
        return replace(base, tracks=tracks, returns=("Reverb Return", "Delay Return"))

    def test_send_head_and_apply(self) -> None:
        from intent import build_request, interpret_response
        from tests.support import choice
        snapshot = self._snapshot()
        questions = build_request(snapshot, "x")["questions"]
        self.assertEqual(set(questions["send"]["criteria"]), {"send0", "send1", "none"})
        answers = {
            "action": choice("send", 0.95, send=0.95),
            "track": choice("t1", 0.95, t1=0.95),
            "step": choice("up_small", 0.9, up_small=0.9),
            "send": choice("send0", 0.92, send0=0.92),
            "track_stated": {"type": "noul", "noul": 0.9},
        }
        result = interpret_response(snapshot, "ベースのセンドA少し上げて", {"answers": answers})
        self.assertEqual((result.intent.track, result.intent.send), (1, 0))
        batches = ACTIONS[Action.SEND].apply(snapshot, result.intent)
        self.assertEqual(batches[0][:3], ["--write", "--api-parameter-set", "live_set tracks 1 mixer_device sends 0"])
        self.assertAlmostEqual(float(batches[0][3]), 0.25)
        for batch in batches:
            validate_arguments(batch)

    def test_rename_and_add_track_need_confirmation(self) -> None:
        snapshot = self._snapshot()
        rename = parse_local("Bassの名前をLow Endにして", snapshot)
        self.assertIs(rename.action, Action.RENAME)
        self.assertEqual((rename.track, rename.text), (1, "Low End"))
        batches = ACTIONS[Action.RENAME].apply(snapshot, rename)
        self.assertEqual(batches[0], ["--write", "--rename-track-index", "1", "--rename-track-name", "Low End"])
        validate_arguments(batches[0])
        added = parse_local("Padsという名前でMIDIトラックを追加", snapshot)
        self.assertIs(added.action, Action.ADD_MIDI_TRACK)
        self.assertEqual(added.text, "Pads")
        add_batches = ACTIONS[Action.ADD_MIDI_TRACK].apply(snapshot, added)
        self.assertEqual(add_batches[0], ["--write", "--add-midi-tracks", "1", "--midi-name", "Pads"])
        validate_arguments(add_batches[0])
        self.assertTrue(ACTIONS[Action.RENAME].confirm and ACTIONS[Action.ADD_MIDI_TRACK].confirm)
        with self.assertRaises(ValueError):
            validate_arguments(["--write", "--add-midi-tracks", "3"])
        with self.assertRaises(ValueError):
            validate_arguments(["--write", "--rename-track-name", "X"])

    def test_rename_flow_asks_then_updates_name(self) -> None:
        class Bridge(RecordingBridge):
            def __init__(self) -> None:
                super().__init__()
                self.renamed = False

            def run(self, arguments):
                arguments = list(arguments)
                if "--rename-track-index" in arguments:
                    self.calls.append(arguments)
                    self.renamed = True
                    return BridgeResult((), 1, 0, False)
                if "--api-get" in arguments and arguments[arguments.index("--api-get") + 2] == "name" and self.renamed and arguments[arguments.index("--api-get") + 1] == "live_set tracks 1":
                    self.calls.append(arguments)
                    return BridgeResult((Ack("api_get", arguments[-1], "Low End", "live_set tracks 1", "name"),), 1, 0, False)
                return super().run(arguments)

        bridge = Bridge()
        service = LiveJevService(bridge=bridge, snapshot=self._snapshot(), key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        first = service.process({"id": "1", "text": "Bassの名前をLow Endにして"})
        self.assertEqual(first["kind"], "confirm")
        self.assertIn("Low End", first["line"])
        done = service.process({"id": "2", "confirm": True})
        self.assertEqual(done["kind"], "result")
        self.assertEqual(service.snapshot.tracks[1].name, "Low End")


class ClipPropTests(unittest.TestCase):
    def _snapshot(self):
        from snapshot import Clip
        base = _snapshot_with_song()
        tracks = list(base.tracks)
        tracks[1] = replace(tracks[1], clips=(Clip(0, "Bass Loop", "live_set tracks 1 clip_slots 0 clip", {"pitch_coarse": 0, "gain": 0.5, "looping": False}),))
        return replace(base, tracks=tuple(tracks))

    def test_local_loop_and_pitch_parsing(self) -> None:
        snapshot = self._snapshot()
        loop = parse_local("Bassのクリップ1をループオン", snapshot)
        self.assertIs(loop.action, Action.CLIP_LOOP_ON)
        off = parse_local("Bassのクリップ1をループ解除", snapshot)
        self.assertIs(off.action, Action.CLIP_LOOP_OFF)
        self.assertEqual(parse_number("2半音上げて", Action.CLIP_PITCH), Number(2.0, "raw"))
        self.assertEqual(parse_number("3半音下げて", Action.CLIP_PITCH), Number(-3.0, "raw"))
        self.assertEqual(parse_number("1オクターブ下げて", Action.CLIP_PITCH), Number(-12.0, "raw"))

    def test_clip_prop_batches_and_allowlist(self) -> None:
        snapshot = self._snapshot()
        base = parse_local("Bassのクリップ1をループオン", snapshot)
        loop = ACTIONS[Action.CLIP_LOOP_ON].apply(snapshot, base)
        self.assertEqual(loop[0][:5], ["--write", "--api-set", "live_set tracks 1 clip_slots 0 clip", "looping", "1"])
        pitch_intent = replace(base, action=Action.CLIP_PITCH, step=Step.NONE, number=Number(2.0, "raw"))
        pitch = ACTIONS[Action.CLIP_PITCH].apply(snapshot, pitch_intent)
        self.assertEqual(pitch[0][:5], ["--write", "--api-set", "live_set tracks 1 clip_slots 0 clip", "pitch_coarse", "2"])
        gain_intent = replace(base, action=Action.CLIP_GAIN, step=Step.UP_SMALL, number=None)
        gain = ACTIONS[Action.CLIP_GAIN].apply(snapshot, gain_intent)
        self.assertAlmostEqual(float(gain[0][4]), 0.55)
        for batches in (loop, pitch, gain):
            for batch in batches:
                validate_arguments(batch)
        with self.assertRaises(ValueError):
            validate_arguments(["--write", "--api-set", "live_set tracks 1 clip_slots 0 clip", "pitch_coarse", "60", "x"])
        with self.assertRaises(ValueError):
            validate_arguments(["--write", "--api-set", "live_set tracks 1 clip_slots 0 clip", "name", "X", "x"])


class ClipValuePhraseTests(unittest.TestCase):
    def test_pitch_and_gain_phrases_are_local(self) -> None:
        from snapshot import Clip
        base = _snapshot_with_song()
        tracks = list(base.tracks)
        tracks[1] = replace(tracks[1], clips=(Clip(0, "Bass Loop", "live_set tracks 1 clip_slots 0 clip", {"pitch_coarse": 0, "gain": 0.5}),))
        snapshot = replace(base, tracks=tuple(tracks))
        up = parse_local("Bassのクリップ1を2半音上げて", snapshot)
        self.assertIs(up.action, Action.CLIP_PITCH)
        self.assertEqual((up.clip, up.number), (0, Number(2.0, "raw")))
        self.assertIsNot(up.step, Step.SET)
        zero = parse_local("Bassのクリップ1のピッチを0半音に", snapshot)
        self.assertIs(zero.step, Step.SET)
        self.assertEqual(zero.number, Number(0.0, "raw"))
        gain = parse_local("Bassのクリップ1のゲイン少し上げて", snapshot)
        self.assertIs(gain.action, Action.CLIP_GAIN)
        self.assertIs(gain.step, Step.UP_SMALL)
        self.assertIsNone(parse_local("Bassのクリップ1を", snapshot))


class AddTrackWithDeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        import daemon as D
        self._confirm_patch = unittest.mock.patch.object(D, "REQUIRE_CONFIRM", True)
        self._confirm_patch.start()
        self.addCleanup(self._confirm_patch.stop)

    def test_phrases_resolve_device_and_kind(self) -> None:
        from intent import resolve_native_device
        snapshot = _snapshot_with_song()
        midi = parse_local("Operator入りのMIDIトラック作って", snapshot)
        self.assertIs(midi.action, Action.ADD_TRACK_WITH_DEVICE)
        self.assertEqual((midi.native_device, midi.text), ("Operator", None))
        named = parse_local("Leadという名前でウェーブテーブル入りのトラック作って", snapshot)
        self.assertEqual((named.native_device, named.text), ("Wavetable", "Lead"))
        audio = parse_local("Reverb付きのオーディオトラック追加", snapshot)
        self.assertEqual((audio.native_device, audio.text), ("Reverb", "audio"))
        self.assertIsNone(parse_local("Serum入りのMIDIトラック作って", snapshot))
        self.assertIsNone(resolve_native_device("eq"))
        self.assertEqual(resolve_native_device("EQ Eight"), "EQ Eight")
        batches = ACTIONS[Action.ADD_TRACK_WITH_DEVICE].apply(snapshot, named)
        self.assertEqual(batches[0], ["--write", "--add-midi-tracks", "1", "--midi-name", "Lead"])
        validate_arguments(["--write", "--api-insert-device", "live_set tracks 4", "Operator", "", "x"])
        with self.assertRaises(ValueError):
            validate_arguments(["--write", "--api-insert-device", "live_set tracks 4", "Serum", "", "x"])

    def test_flow_adds_then_inserts_then_rereads(self) -> None:
        from snapshot import Device, Track
        calls: list[list[str]] = []
        states = [_snapshot_with_song()]
        added = replace(states[0], tracks=states[0].tracks + (Track(3, "Lead", 0.85, "0.0 dB", 0.0, "C", False, False, (), "live_set tracks 3"),))
        with_device = replace(added, tracks=added.tracks[:-1] + (replace(added.tracks[-1], devices=(Device(0, "Operator", (), "live_set tracks 3 devices 0"),)),))
        reads = iter([(added, 5), (with_device, 5)])

        class Bridge:
            def run(self, arguments):
                calls.append(list(arguments))
                return BridgeResult((), 1, 0, False)

        class Reader:
            def read(self):
                return next(reads)

        service = LiveJevService(bridge=Bridge(), snapshot=states[0], key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        service.reader = Reader()
        first = service.process({"id": "1", "text": "Leadという名前でOperator入りのMIDIトラック作って"})
        self.assertEqual(first["kind"], "confirm")
        self.assertIn("Operator", first["line"])
        self.assertEqual(calls, [])
        done = service.process({"id": "2", "confirm": True})
        self.assertEqual(done["kind"], "result", done)
        self.assertEqual(calls[0], ["--write", "--add-midi-tracks", "1", "--midi-name", "Lead"])
        self.assertEqual(calls[1][:4], ["--write", "--api-insert-device", "live_set tracks 3", "Operator"])
        self.assertIn("Operator", done["line"])
        self.assertEqual(service.snapshot.tracks[-1].devices[0].name, "Operator")


class LargeDeviceTests(unittest.TestCase):
    def test_reader_keeps_going_when_device_parameters_fail(self) -> None:
        from daemon import SnapshotReader

        class Bridge:
            def run(self, arguments):
                arguments = list(arguments)
                flag = next(item for item in arguments if item.startswith("--") and item != "--write")
                rid = arguments[-1]
                if flag == "--api-session-context":
                    return BridgeResult((Ack("api_session_context", rid, {"song": {"tempo": 120, "is_playing": 0}}),), 1, 0, False)
                if flag == "--api-children":
                    if arguments[1:3] == ["live_set", "tracks"]:
                        return BridgeResult((Ack("api_children", rid, [{"index": 0, "path": "live_set tracks 0", "name": "Lead"}], "live_set", "tracks"),), 1, 0, False)
                    return BridgeResult((Ack("api_children", rid, [], arguments[1], arguments[2]),), 1, 0, False)
                if flag == "--api-device-list":
                    return BridgeResult((Ack("api_device_list", rid, {"tracks": [{"track": {"path": "live_set tracks 0"}, "devices": [{"name": "Operator", "path": "live_set tracks 0 devices 0"}]}]}),), 1, 0, False)
                if flag == "--api-device-parameters":
                    return BridgeResult((), 1, 0, False)
                if flag == "--api-mixer-status":
                    return BridgeResult((Ack("api_mixer_status", rid, {"parameters": {}}, arguments[1]),), 1, 0, False)
                if flag == "--api-get":
                    prop = arguments[3]
                    payload = "Lead" if prop == "name" else 0
                    return BridgeResult((Ack("api_get", rid, payload, arguments[1], prop),), 1, 0, False)
                if flag == "--api-call":
                    return BridgeResult((Ack("api_call", rid, "0.0 dB", arguments[2], "str_for_value"),), 1, 0, False)
                raise AssertionError(arguments)

        reader = SnapshotReader(Bridge())
        snapshot, _elapsed = reader.read()
        self.assertEqual(snapshot.tracks[0].devices[0].name, "Operator")
        self.assertEqual(snapshot.tracks[0].devices[0].params, ())
        self.assertEqual(reader.skipped_devices, ["live_set tracks 0 devices 0"])


class PluginNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        import daemon as D
        self._confirm_patch = unittest.mock.patch.object(D, "REQUIRE_CONFIRM", True)
        self._confirm_patch.start()
        self.addCleanup(self._confirm_patch.stop)

    def test_generic_words_and_plugins_do_not_act(self) -> None:
        from unittest import mock
        import daemon as D
        bridge = RecordingBridge()
        service = LiveJevService(bridge=bridge, snapshot=_snapshot_with_song(), key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        with mock.patch.object(D, "load_plugin_catalog", return_value=("Serum2", "ValhallaVintageVerb", "Altiverb 8")), \
             mock.patch.object(D.plugin_script, "ping", return_value=False):
            generic = service.process({"id": "1", "text": "リバーブ入りのトラック作って"})
            self.assertEqual(generic["kind"], "info")
            self.assertIn("Altiverb 8", generic["line"])
            plugin = service.process({"id": "2", "text": "Serum2入りのMIDIトラック作って"})
            self.assertEqual(plugin["kind"], "confirm")
            self.assertIn("Serum2", plugin["line"])
            service.process({"id": "3", "confirm": False})
        self.assertEqual(bridge.calls, [])
        self.assertIsNone(parse_local("リバーブ入りのトラック作って", _snapshot_with_song()))


class PluginFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        import daemon as D
        self._confirm_patch = unittest.mock.patch.object(D, "REQUIRE_CONFIRM", True)
        self._confirm_patch.start()
        self.addCleanup(self._confirm_patch.stop)

    def test_insert_plugin_loads_then_waits_for_device(self) -> None:
        from unittest import mock
        import daemon as D
        from snapshot import Device
        base = _snapshot_with_song()
        tracks = list(base.tracks)
        with_plugin = replace(base, tracks=tuple(replace(t, devices=(Device(0, "Serum 2", (), "live_set tracks 1 devices 0"),)) if t.index == 1 else t for t in tracks))
        reads = iter([(with_plugin, 3)])
        loads: list[tuple[str, int | None]] = []
        bridge = RecordingBridge()
        service = LiveJevService(bridge=bridge, snapshot=base, key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        service.reader = type("R", (), {"read": staticmethod(lambda: next(reads))})()
        with mock.patch.object(D.plugin_script, "ping", return_value=True), \
             mock.patch.object(D.plugin_script, "list_plugins", return_value=[{"name": "Serum 2"}, {"name": "ValhallaVintageVerb"}]), \
             mock.patch.object(D.plugin_script, "load", side_effect=lambda name, track, uri="": loads.append((name, track)) or {"ok": True}), \
             mock.patch.object(D.time, "sleep", lambda *_: None):
            first = service.process({"id": "1", "text": "BassにSerum 2を挿して"})
            self.assertEqual(first["kind"], "confirm")
            self.assertEqual(loads, [])
            done = service.process({"id": "2", "confirm": True})
        self.assertEqual(done["kind"], "result", done)
        self.assertEqual(loads, [("Serum 2", 1)])
        self.assertIn("Serum 2", done["line"])
        self.assertTrue(all("--api-get" in call for call in bridge.calls))


class NoConfirmByDefaultTests(unittest.TestCase):
    def test_loop_and_record_run_without_confirmation_by_default(self) -> None:
        import daemon as D
        self.assertFalse(D.REQUIRE_CONFIRM)
        bridge = RecordingBridge()
        service = LiveJevService(bridge=bridge, snapshot=_snapshot_with_song(), key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        answer = service.process({"id": "1", "text": "録音開始"})
        self.assertEqual(answer["kind"], "result")
        self.assertTrue(any("session_record" in call for call in bridge.calls))

    def test_plugin_shortest_partial_and_alias(self) -> None:
        from unittest import mock
        import intent as I
        catalog = ("Serum 2", "Serum 2 FX", "ValhallaVintageVerb", "ValhallaRoom")
        self.assertEqual(I.resolve_plugin_name("serum", catalog), "Serum 2")
        with mock.patch.object(I, "load_plugin_aliases", return_value={"セラム": "Serum 2", "バルハラ": "ValhallaVintageVerb"}):
            self.assertEqual(I.resolve_plugin_name("セラム", catalog), "Serum 2")
            self.assertEqual(I.resolve_plugin_name("バルハラ", catalog), "ValhallaVintageVerb")
        self.assertIsNone(I.resolve_plugin_name("コンタクト", catalog))


class UndoButtonTests(unittest.TestCase):
    def test_undo_button_restores_or_falls_back_to_live_undo(self) -> None:
        bridge = RecordingBridge()
        service = LiveJevService(bridge=bridge, snapshot=_snapshot_with_song(), key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        first = service.process({"id": "0", "cmd": "undo"})
        self.assertEqual(first["kind"], "result")
        self.assertTrue(any("--api-call" in call and "undo" in call for call in bridge.calls))
        service.process({"id": "1", "text": "ループして"})
        bridge.calls.clear()
        back = service.process({"id": "2", "cmd": "undo"})
        self.assertEqual(back["kind"], "result")
        self.assertTrue(any("--api-set" in call and "loop" in call and "0" in call for call in bridge.calls))


class SelectedTrackTests(unittest.TestCase):
    """Explicit selected-track targets and the selected-track default when no track is given."""

    class SelectedBridge(RecordingBridge):
        def run(self, arguments):
            arguments = list(arguments)
            if "--api-session-context" in arguments:
                self.calls.append(arguments)
                payload = {"song": {}, "selected": {"track": {"path": "live_set tracks 1", "name": "Bass"}}}
                return BridgeResult((Ack("api_session_context", arguments[-1], payload),), 1, 0, False)
            if "--api-get" not in arguments:
                self.calls.append(arguments)
                return BridgeResult((), 1, 0, False)
            return super().run(arguments)

    def _service(self, requester):
        bridge = self.SelectedBridge()
        service = LiveJevService(bridge=bridge, snapshot=_snapshot_with_song(), key="x", requester=requester, llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        return bridge, service

    def test_unspecified_track_defaults_to_selected(self) -> None:
        from tests.support import response
        bridge, service = self._service(lambda *_: response("mute"))
        answer = service.process({"id": "1", "text": "ミュート"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["decision"]["track"], "Bass")
        self.assertTrue(any("--api-set" in call and "live_set tracks 1" in call and "mute" in call for call in bridge.calls))

    def test_selected_word_targets_selected_track(self) -> None:
        from tests.support import response
        bridge, service = self._service(lambda *_: response("volume", "selected", "down_small"))
        answer = service.process({"id": "1", "text": "選択トラックを少し下げて"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["decision"]["track"], "Bass")
        self.assertTrue(any("--write" in call and any("live_set tracks 1" in item for item in call) for call in bridge.calls))

    def test_guessed_track_without_name_defaults_to_selected(self) -> None:
        from tests.support import response
        answers = response("volume", "t0", "down_small", track_conf=0.32)
        answers["answers"]["track_stated"] = {"type": "noul", "noul": 0.05}
        bridge, service = self._service(lambda *_: answers)
        answer = service.process({"id": "1", "text": "ちょい下げて"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["decision"]["track"], "Bass")

    def test_named_but_missing_track_asks_instead_of_selected(self) -> None:
        from tests.support import response
        answers = response("volume", "none", "down_small", track_conf=0.5)
        answers["answers"]["track_stated"] = {"type": "noul", "noul": 0.85}
        bridge, service = self._service(lambda *_: answers)
        answer = service.process({"id": "1", "text": "パッドを少し下げて"})
        self.assertEqual(answer["kind"], "ask")
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_named_but_uncertain_track_still_asks(self) -> None:
        from tests.support import response
        bridge, service = self._service(lambda *_: response("mute", "t0", track_conf=0.4))
        answer = service.process({"id": "1", "text": "パッドっぽいのミュート"})
        self.assertEqual(answer["kind"], "ask")
        self.assertFalse(any("--api-set" in call for call in bridge.calls))


class NewTrackOpenPhraseTests(unittest.TestCase):
    def test_new_track_open_phrases(self) -> None:
        from intent import Action, extract_plugin_request, parse_local
        snapshot = _snapshot_with_song()
        for text in ("新しいトラックでDiva開いて", "新しいトラックにDivaを立ち上げて", "Divaを新しいトラックで開いて", "新規MIDIトラックでDiva開いて"):
            request = extract_plugin_request(text, snapshot)
            self.assertIsNotNone(request, text)
            self.assertIs(request.action, Action.ADD_TRACK_WITH_PLUGIN)
            self.assertEqual(request.raw_name, "Diva")
        audio = extract_plugin_request("新しいオーディオトラックでValhallaVintageVerbを開いて", snapshot)
        self.assertEqual((audio.raw_name, audio.text), ("ValhallaVintageVerb", "audio"))
        native = parse_local("新しいトラックでOperator開いて", snapshot)
        self.assertIs(native.action, Action.ADD_TRACK_WITH_DEVICE)
        self.assertIsNone(extract_plugin_request("新しいトラックでOperator開いて", snapshot))
        self.assertIsNone(extract_plugin_request("新しいトラック作って", snapshot))


class RelativeDbTests(unittest.TestCase):
    def test_relative_and_absolute_db(self) -> None:
        from daemon import relative_db_target
        from intent import Step
        self.assertEqual(relative_db_target(3.0, Step.DOWN_SMALL, "-6.0 dB"), -9.0)
        self.assertEqual(relative_db_target(3.0, Step.UP_BIG, "-6.0 dB"), -3.0)
        self.assertEqual(relative_db_target(-3.0, Step.DOWN_SMALL, "0.0 dB"), -3.0)
        self.assertEqual(relative_db_target(-3.0, Step.SET, "-6.0 dB"), -3.0)
        for unreadable in ("-inf dB", None, ""):
            with self.assertRaises(ValueError):
                relative_db_target(3.0, Step.DOWN_SMALL, unreadable)
            with self.assertRaises(ValueError):
                relative_db_target(3.0, Step.UP_BIG, unreadable)
        self.assertEqual(relative_db_target(-12.0, Step.SET, "-inf dB"), -12.0)

    def test_generic_name_with_high_confidence_is_kept(self) -> None:
        from tests.support import response
        answers = response("solo", "t0")
        answers["answers"]["track_stated"] = {"type": "noul", "noul": 0.47}
        answers["answers"]["track"]["confidence"] = 1.0
        bridge, service = SelectedTrackTests()._service(lambda *_: answers)
        answer = service.process({"id": "1", "text": "Padをソロ"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["decision"]["track"], "Pad")


class PluginFallbackTests(unittest.TestCase):
    """Search catalog plug-in names when Jev mistakes an unrecognized phrase for a built-in-device request."""

    def _service(self, requester):
        bridge = SelectedTrackTests.SelectedBridge()
        service = LiveJevService(bridge=bridge, snapshot=_snapshot_with_song(), key="x", requester=requester, llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        return bridge, service

    def test_open_verbs_and_bare_new_track_are_set_phrases(self) -> None:
        from intent import Action, extract_plugin_request
        snapshot = _snapshot_with_song()
        self.assertEqual(extract_plugin_request("Omnisphereを開いて", snapshot).action, Action.INSERT_PLUGIN)
        self.assertEqual(extract_plugin_request("オムニスフィア立ち上げて", snapshot).raw_name, "オムニスフィア")
        bare = extract_plugin_request("新しいトラックにオムニスフィア", snapshot)
        self.assertEqual((bare.action, bare.raw_name), (Action.ADD_TRACK_WITH_PLUGIN, "オムニスフィア"))

    def test_native_device_ask_falls_back_to_plugin_catalog(self) -> None:
        from unittest import mock
        import daemon as D
        from tests.support import choice
        calls: list[dict] = []

        def requester(payload, key):
            calls.append(payload)
            if "plugin" in payload["questions"]:
                return {"answers": {"plugin": choice("Omnisphere", 0.93)}}
            return {"answers": {
                "action": choice("add_track_with_device", 0.9),
                "track": choice("none", 0.5), "step": choice("none", 0.9),
                "track_stated": {"type": "noul", "noul": 0.05}, "needs_generation": {"type": "noul", "noul": 0.0},
                "compound": {"type": "noul", "noul": 0.0}, "refers_previous": {"type": "noul", "noul": 0.0},
                "native_device": choice("none", 0.4),
            }}

        bridge, service = self._service(requester)
        seen: list[tuple[str, int | None]] = []
        with mock.patch.object(service, "_plugin_names", return_value=("Omnisphere", "Serum 2")), \
             mock.patch.object(service, "_run_plugin_flow", side_effect=lambda intent, before: seen.append((intent.plugin, intent.track)) or 0):
            answer = service.process({"id": "1", "text": "オムニスフィアで新しいトラック"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertEqual(seen, [("Omnisphere", None)])
        self.assertTrue(any("plugin" in call["questions"] for call in calls))


class PhraseVocabularyTests(unittest.TestCase):
    """Normalize polite, desiderative, and terminal forms plus word-order variants to the same request."""

    def test_normalize_phrase(self) -> None:
        from intent import normalize_phrase
        cases = {
            "作りたい": "作って", "作ってください": "作って", "作る": "作って", "作成": "作って", "新規作成して": "作って",
            "開きたいです": "開いて", "開いてくれる？": "開いて", "立ち上げ": "立ち上げて", "起動": "起動して", "ロードして欲しい": "ロードして",
            "入れてみて": "入れて", "挿してもらえますか": "挿して", "パッドを少し下げてください": "パッドを少し下げて", "ミュート": "ミュート",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_phrase(raw), expected, raw)

    def test_many_ways_to_add_a_track_with_a_plugin(self) -> None:
        from intent import Action, extract_plugin_request
        snapshot = _snapshot_with_song()
        phrases = [
            "新しいトラックでOmnisphere開いて", "新しいトラックにOmnisphere", "Omnisphereを新しいトラックで開いて", "Omnisphereで新しいトラック作りたい",
            "Omnisphere入りのトラック作って", "Omnisphereの入ったトラックを追加", "トラック作ってOmnisphere入れて", "新規MIDIトラックにOmnisphereを立ち上げてください",
            "もう1本トラック作ってOmnisphere載せて", "Omnisphere用のトラック作って", "新しくトラックを作ってOmnisphereをロード", "別のトラックにOmnisphereを起動して",
            "Omnisphereを新規トラックで使いたい", "新しいトラックでOmnisphereを読み込んで", "トラックを追加してOmnisphereをインサート",
        ]
        for text in phrases:
            request = extract_plugin_request(text, snapshot)
            self.assertIsNotNone(request, text)
            self.assertIs(request.action, Action.ADD_TRACK_WITH_PLUGIN, text)
            self.assertEqual(request.raw_name, "Omnisphere", text)
        named = extract_plugin_request("Leadという名前でOmnisphere入りのトラック作って", snapshot)
        self.assertEqual((named.raw_name, named.text), ("Omnisphere", "Lead"))
        named2 = extract_plugin_request("Leadって名前で新しいトラックにOmnisphere開いて", snapshot)
        self.assertEqual((named2.raw_name, named2.text), ("Omnisphere", "Lead"))
        audio = extract_plugin_request("新しいオーディオトラックにValhallaVintageVerbを挿して", snapshot)
        self.assertEqual((audio.raw_name, audio.text), ("ValhallaVintageVerb", "audio"))

    def test_many_ways_to_insert_on_a_track(self) -> None:
        from intent import Action, extract_plugin_request
        snapshot = _snapshot_with_song()
        for text in ["Omnisphereを開いて", "Omnisphere立ち上げて", "Omnisphereを挿してください", "Omnisphereを使いたい", "Omnisphere読み込んで", "Omnisphereをロード", "Omnisphere起動", "Omnisphereぶち込んで"]:
            request = extract_plugin_request(text, snapshot)
            self.assertIsNotNone(request, text)
            self.assertEqual((request.action, request.raw_name, request.track), (Action.INSERT_PLUGIN, "Omnisphere", None), text)
        for text in ["BassにOmnisphereを開いて", "Bassで Omnisphere 立ち上げて", "BassのOmnisphereを起動して", "選択トラックにOmnisphere入れて"]:
            request = extract_plugin_request(text, snapshot)
            self.assertIsNotNone(request, text)
            self.assertIs(request.action, Action.INSERT_PLUGIN, text)
            self.assertEqual(request.raw_name, "Omnisphere", text)
            self.assertIn(request.track, (1, "selected"), text)

    def test_native_and_plain_track_phrases_stay_local(self) -> None:
        from intent import Action, extract_plugin_request, parse_local
        snapshot = _snapshot_with_song()
        self.assertIsNone(extract_plugin_request("新しいトラックにOperator", snapshot))
        self.assertIs(parse_local("新しいトラックにOperator", snapshot).action, Action.ADD_TRACK_WITH_DEVICE)
        self.assertIs(parse_local("Operator入りのトラック作りたい", snapshot).action, Action.ADD_TRACK_WITH_DEVICE)
        self.assertIs(parse_local("新しいトラック作って", snapshot).action, Action.ADD_MIDI_TRACK)
        self.assertIs(parse_local("オーディオトラックを追加", snapshot).action, Action.ADD_AUDIO_TRACK)
        self.assertIs(parse_local("トラック作りたい", snapshot).action, Action.ADD_MIDI_TRACK)
        self.assertIsNone(extract_plugin_request("新しいトラック作って", snapshot))
        self.assertIsNone(extract_plugin_request("パッドを少し下げて", snapshot))
        self.assertIsNone(extract_plugin_request("ミュート", snapshot))


class StaleSnapshotTests(unittest.TestCase):
    """Refresh the snapshot and retry the utterance when track count, names, or device counts change."""

    def test_changed_track_list_triggers_reread_and_retry(self) -> None:
        import time as _time
        from snapshot import Track
        base = _snapshot_with_song()
        old = replace(base, taken_at=_time.time() - 60)
        extra = Track(3, "Vox", 0.5, "-9.0 dB", 0.0, "C", False, False, (), "live_set tracks 3")
        fresh = replace(base, tracks=base.tracks + (extra,), taken_at=_time.time())

        class ListingBridge(RecordingBridge):
            def run(self, arguments):
                arguments = list(arguments)
                if "--api-device-list" in arguments:
                    self.calls.append(arguments)
                    tracks = [{"track": {"name": t.name, "path": t.path}, "devices": [{"name": d.name, "path": d.path} for d in t.devices]} for t in fresh.tracks]
                    return BridgeResult((Ack("api_device_list", arguments[-1], {"target": "all", "tracks": tracks}),), 1, 0, False)
                if "--api-get" in arguments and arguments[arguments.index("--api-get") + 2] == "name" and "live_set tracks 3" in arguments:
                    self.calls.append(arguments)
                    return BridgeResult((Ack("api_get", arguments[-1], "Vox", "live_set tracks 3", "name"),), 1, 0, False)
                return super().run(arguments)

        bridge = ListingBridge()
        service = LiveJevService(bridge=bridge, snapshot=old, key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        service.reader = type("R", (), {"read": staticmethod(lambda: (fresh, 5))})()
        answer = service.process({"id": "1", "text": "Voxをミュート"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertEqual(answer["decision"]["track"], "Vox")
        self.assertEqual(len(service.snapshot.tracks), 4)

    def test_unchanged_structure_is_not_reread(self) -> None:
        import time as _time
        base = _snapshot_with_song()
        service = LiveJevService(bridge=RecordingBridge(), snapshot=replace(base, taken_at=_time.time()), key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        service.reader = type("R", (), {"read": staticmethod(lambda: (_ for _ in ()).throw(AssertionError("再読込は不要")))})()
        self.assertEqual(service.process({"id": "1", "text": "Bassをミュート"})["kind"], "result")


class ClipNotesTests(unittest.TestCase):
    """Quantize, legato, transpose, velocity, and loop doubling through the component inside Live, faked here."""

    def test_phrases(self) -> None:
        from intent import parse_clip_notes_phrase as parse
        snapshot = _snapshot_with_song()
        self.assertEqual(parse("クオンタイズして", snapshot).op, "quantize")
        self.assertEqual(parse("今開いているMIDIノートをレガートにして", snapshot).op, "legato")
        self.assertEqual(parse("クリップを1オクターブ上げて", snapshot).semitones, 12)
        self.assertEqual(parse("二オクターブ下げてください", snapshot).semitones, -24)
        self.assertEqual(parse("このクリップを3半音下げて", snapshot).semitones, -3)
        loose = parse("16分3連で軽くクオンタイズしてほしい", snapshot)
        self.assertEqual((loose.grid, loose.amount), ("1/16t", 0.5))
        self.assertEqual(parse("8分で70%クオンタイズ", snapshot).amount, 0.7)
        self.assertEqual(parse("ベロシティ100に", snapshot).value, 100.0)
        self.assertEqual(parse("ベロシティを少し下げて", snapshot).factor, 0.9)
        self.assertEqual(parse("ループを倍にして", snapshot).op, "duplicate_loop")
        slotted = parse("Bassのクリップ2をクオンタイズ", snapshot)
        self.assertEqual((slotted.track, slotted.slot), (1, 1))
        for other in ("パッドを少し下げて", "ミュート", "テンポ120", "Bassのクリップ1を2半音上げて", "オクターブ", "Serum 2を挿して"):
            self.assertIsNone(parse(other, snapshot), other)

    def test_daemon_runs_script_and_reports(self) -> None:
        from unittest import mock
        import daemon as D
        calls: list[tuple] = []
        service = LiveJevService(bridge=RecordingBridge(), snapshot=_snapshot_with_song(), key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        def fake(op, track, slot, **fields):
            calls.append((op, track, slot, fields))
            return {"ok": True, "op": op, "clip": "Fill", "track": "Bass", "is_midi": True, "count": 12}
        with mock.patch.object(D.plugin_script, "ping", return_value=True), mock.patch.object(D.plugin_script, "clip_notes", side_effect=fake):
            answer = service.process({"id": "1", "text": "クリップを1オクターブ上げて"})
            self.assertEqual(answer["kind"], "result", answer)
            self.assertEqual(answer["line"], "1オクターブ上げました")
            self.assertEqual(calls[-1], ("transpose", None, None, {"semitones": 12}))
            quantized = service.process({"id": "2", "text": "8分でクオンタイズ"})
            self.assertIn("8分でクオンタイズ", quantized["line"])
        with mock.patch.object(D.plugin_script, "ping", return_value=True), \
             mock.patch.object(D.plugin_script, "clip_notes", side_effect=D.plugin_script.ScriptError("no_clip")):
            service._plugin_script_ok = None
            missing = service.process({"id": "3", "text": "レガートにして"})
        self.assertEqual(missing["kind"], "error")
        self.assertIn("開いているクリップがありません", missing["line"])


class ReviewFindingsTests(unittest.TestCase):
    """Regression coverage for defects found during review."""

    def test_negated_requests_do_nothing_locally(self) -> None:
        from intent import extract_plugin_request, parse_clip_notes_phrase
        snapshot = _snapshot_with_song()
        for text in ("ループを倍にしないで", "クオンタイズしないで", "ベロシティを下げないで", "オクターブ上げないで", "レガートは不要", "クオンタイズしなくていい"):
            self.assertIsNone(parse_clip_notes_phrase(text, snapshot), text)
        for text in ("新しいトラックにSerumは入れないで", "Omnisphereを挿さないで", "Serum 2はいらない"):
            self.assertIsNone(extract_plugin_request(text, snapshot), text)

    def test_new_track_sentences_that_are_not_plugin_requests(self) -> None:
        from intent import extract_plugin_request
        snapshot = _snapshot_with_song()
        for text in ("新しいトラックで録音を始めて", "新しいトラックで曲を作って", "新しいトラックでベースを録音して"):
            request = extract_plugin_request(text, snapshot)
            self.assertTrue(request is None or "録音" not in request.raw_name and "を" not in request.raw_name, (text, request))

    def test_slot_target_sends_expected_track_name_and_retries_when_changed(self) -> None:
        from unittest import mock
        import daemon as D
        seen: list[dict] = []
        service = LiveJevService(bridge=RecordingBridge(), snapshot=_snapshot_with_song(), key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        rereads: list[int] = []
        service.reader = type("R", (), {"read": staticmethod(lambda: rereads.append(1) or (_snapshot_with_song(), 1))})()
        def fake(op, track, slot, **fields):
            seen.append({"op": op, "track": track, "slot": slot, **fields})
            if len(seen) == 1:
                raise D.plugin_script.ScriptError("track_changed")
            return {"ok": True, "op": op, "clip": "Fill", "track": "Bass", "is_midi": True, "count": 3}
        with mock.patch.object(D.plugin_script, "ping", return_value=True), mock.patch.object(D.plugin_script, "clip_notes", side_effect=fake):
            answer = service.process({"id": "1", "text": "Bassのクリップ2をレガートにして"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertEqual(seen[0]["track_name"], "Bass")
        self.assertEqual((len(seen), len(rereads)), (2, 1))

    def test_plugin_result_line_is_short(self) -> None:
        from actions import read_plugin
        from intent import Action, _local_intent
        snapshot = _snapshot_with_song()
        self.assertEqual(read_plugin(snapshot, _local_intent(Action.INSERT_PLUGIN, track=1, plugin="Omnisphere")), "Omnisphere を挿入しました")
        self.assertEqual(read_plugin(snapshot, _local_intent(Action.ADD_TRACK_WITH_PLUGIN, plugin="Omnisphere")), "新しいトラックに Omnisphere を挿入しました")


class AbletonStyleStructureTests(unittest.TestCase):
    """The component inside Live adds tracks and plug-ins using Live's position, default name, and single-undo behavior."""

    def _service(self):
        service = LiveJevService(bridge=RecordingBridge(), snapshot=_snapshot_with_song(), key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        service.reader = type("R", (), {"read": staticmethod(lambda: (_snapshot_with_song(), 1))})()
        return service

    def test_new_track_with_plugin_uses_add_track_without_a_name(self) -> None:
        from unittest import mock
        import daemon as D
        calls: list[tuple] = []
        service = self._service()
        def fake(kind, name=None, device=None):
            calls.append((kind, name, device))
            return {"ok": True, "track_index": 2, "track": "Omnisphere", "devices_after": ["Omnisphere"]}
        with mock.patch.object(D.plugin_script, "ping", return_value=True), \
             mock.patch.object(D.plugin_script, "list_plugins", return_value=[{"name": "Omnisphere"}]), \
             mock.patch.object(D.plugin_script, "add_track", side_effect=fake):
            answer = service.process({"id": "1", "text": "新しいトラックでOmnisphere開いて"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertEqual(answer["line"], "新しいトラックに Omnisphere を挿入しました")
        self.assertEqual(calls, [("midi", None, "Omnisphere")])

    def test_plain_and_native_tracks_use_the_script_when_available(self) -> None:
        from unittest import mock
        import daemon as D
        calls: list[tuple] = []
        service = self._service()
        with mock.patch.object(D.plugin_script, "ping", return_value=True), \
             mock.patch.object(D.plugin_script, "add_track", side_effect=lambda kind, name=None, device=None: calls.append((kind, name, device)) or {"ok": True, "track_index": 2, "devices_after": []}):
            self.assertEqual(service.process({"id": "1", "text": "オーディオトラックを追加"})["kind"], "result")
            self.assertEqual(service.process({"id": "2", "text": "Leadという名前でOperator入りのMIDIトラック作って"})["kind"], "result")
        self.assertEqual(calls, [("audio", None, None), ("midi", "Lead", "Operator")])
        self.assertFalse(any("--add-midi-tracks" in call or "--add-audio-tracks" in call for call in service.bridge.calls))


class InsertVerbCoverageTests(unittest.TestCase):
    def test_many_verbs_mean_insert(self) -> None:
        from intent import Action, extract_plugin_request
        snapshot = _snapshot_with_song()
        verbs = ["入れて", "いれて", "読んで", "読み込んで", "呼んで", "よんで", "呼び出して", "挿して", "差して", "刺して", "さして", "挿入して", "インサート",
                 "載せて", "乗せて", "のせて", "開いて", "開けて", "あけて", "立ち上げて", "起動して", "出して", "つけて", "付けて", "追加して", "足して", "使って",
                 "使いたい", "ロードして", "ロード", "かけて", "セットして", "置いて", "ぶち込んで", "突っ込んで", "アサインして", "適用して", "差し込んで",
                 "入れといて", "入れておいて", "入れてみて", "入れてくれる？", "挿してください", "入れる", "追加"]
        for verb in verbs:
            request = extract_plugin_request("Serum 2を" + verb, snapshot)
            self.assertIsNotNone(request, verb)
            self.assertEqual((request.action, request.raw_name.strip()), (Action.INSERT_PLUGIN, "Serum 2"), verb)

    def test_bare_request_inserts_only_when_the_name_is_in_the_catalog(self) -> None:
        from unittest import mock
        import daemon as D
        from tests.support import response
        seen: list[tuple] = []
        service = LiveJevService(bridge=SelectedTrackTests.SelectedBridge(), snapshot=_snapshot_with_song(), key="x",
                                 requester=lambda *_: response("none", action_conf=0.3), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        with mock.patch.object(service, "_plugin_names", return_value=("Serum 2", "Omnisphere")), \
             mock.patch.object(service, "_run_plugin_flow", side_effect=lambda intent, before: seen.append((intent.plugin, intent.track)) or 0):
            ok = service.process({"id": "1", "text": "Serum 2をお願い"})
            other = service.process({"id": "2", "text": "なんかいい感じにお願い"})
        self.assertEqual(ok["kind"], "result", ok)
        self.assertEqual(seen, [("Serum 2", 1)])
        self.assertEqual(other["kind"], "ask")


class AmbiguousInsertVerbTests(unittest.TestCase):
    """Insertion verbs also describe other actions. Try normal parsing first when the name is absent from the catalog."""

    def _service(self, requester):
        service = LiveJevService(bridge=SelectedTrackTests.SelectedBridge(), snapshot=_snapshot_with_song(), key="x", requester=requester, llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        return service

    def test_metronome_on_is_not_hijacked(self) -> None:
        from unittest import mock
        from tests.support import response
        service = self._service(lambda *_: response("metronome_on"))
        with mock.patch.object(service, "_plugin_names", return_value=("Serum 2", "Omnisphere")):
            answer = service.process({"id": "1", "text": "メトロノームをつけて"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertIn("メトロノーム", answer["line"])

    def test_katakana_plugin_still_found_after_the_normal_path_gives_up(self) -> None:
        from unittest import mock
        from tests.support import choice, response
        seen: list[tuple] = []
        def requester(payload, key):
            if "plugin" in payload["questions"]:
                return {"answers": {"plugin": choice("Serum 2", 0.91)}}
            return response("none", action_conf=0.3)
        service = self._service(requester)
        with mock.patch.object(service, "_plugin_names", return_value=("Serum 2", "Omnisphere")), \
             mock.patch.object(service, "_run_plugin_flow", side_effect=lambda intent, before: seen.append((intent.plugin, intent.track)) or 0):
            answer = service.process({"id": "1", "text": "セラムをつけて"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertEqual(seen, [("Serum 2", 1)])


class DbDirectionFromWordsTests(unittest.TestCase):
    def test_words_beat_the_model_for_direction(self) -> None:
        from daemon import relative_db_target, step_from_words
        from intent import Step
        self.assertIs(step_from_words(Step.NONE, "Bassを3dB下げて"), Step.DOWN_SMALL)
        self.assertIs(step_from_words(Step.SET, "Bassを3dB下げて"), Step.DOWN_SMALL)
        self.assertIs(step_from_words(Step.NONE, "turn Bass down by 3 dB"), Step.DOWN_SMALL)
        self.assertIs(step_from_words(Step.NONE, "boost Bass 2 dB"), Step.UP_SMALL)
        self.assertIs(step_from_words(Step.DOWN_SMALL, "Bassの音量を-6dBにして"), Step.SET)
        self.assertIs(step_from_words(Step.UP_SMALL, "set Bass volume to -6 dB"), Step.SET)
        self.assertEqual(relative_db_target(3.0, step_from_words(Step.NONE, "3db下げて"), "-0.015 dB"), -3.015)
