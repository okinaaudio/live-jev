"""Version 0.08, phase 1: transport and track verbs."""

from __future__ import annotations

from dataclasses import replace
import unittest
import unittest.mock

from actions import ACTIONS, bar_to_beats
from bridge_client import Ack, BridgeError, BridgeResult, validate_arguments
from daemon import LiveJevService
from intent import Action, Number, Step, parse_local, parse_number
from snapshot import song_fields
from tests.support import sample_snapshot


class RecordingBridge:
    """Fake bridge that records writes and returns fixed values for reads."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.values = {}
        self.names = {"live_set tracks 0": "Pad", "live_set tracks 1": "Bass", "live_set tracks 2": "Drums",
                      "live_set tracks 0 devices 0": "Reverb", "live_set tracks 1 devices 0": "Reverb"}

    def run(self, arguments):
        arguments = list(arguments)
        self.calls.append(arguments)
        if "--api-set" in arguments:
            at = arguments.index("--api-set")
            path, prop, raw = arguments[at + 1:at + 4]
            self.values[(path, prop)] = float(raw) if prop in {"current_song_time", "gain"} else bool(int(float(raw)))
            return BridgeResult((), 1, 0, False)
        if "--api-parameter-set" in arguments:
            at = arguments.index("--api-parameter-set")
            path, raw = arguments[at + 1:at + 3]
            self.values[(path, "value")] = float(raw)
            return BridgeResult((), 1, 0, False)
        if "--rename-track-index" in arguments:
            at = arguments.index("--rename-track-index")
            self.names[f"live_set tracks {arguments[at + 1]}"] = arguments[at + 3]
            return BridgeResult((), 1, 0, False)
        if "--api-call" in arguments or "--tempo" in arguments:
            return BridgeResult((), 1, 0, False)
        at = arguments.index("--api-get")
        path, prop, request_id = arguments[at + 1], arguments[at + 2], arguments[at + 3]
        if prop == "name":
            payload = self.names[path]
        elif prop == "current_song_time":
            payload = 64.0
        elif prop == "current_monitoring_state":
            payload = 0
        else:
            payload = self.values.get((path, prop), False)
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

    def test_multi_track_range_all_except_and_only_phrases(self) -> None:
        from intent import TargetOrigin

        base = _snapshot_with_song()
        names = ("Kick", "Pad", "Keys", "Vox", "FX", "Bass")
        tracks = tuple(
            replace(base.tracks[min(index, 2)], index=index, name=name, path=f"live_set tracks {index}")
            for index, name in enumerate(names)
        )
        snapshot = replace(base, tracks=tracks)
        cases = {
            "3から6までミュート": (Action.MUTE, (2, 3, 4, 5), TargetOrigin.RANGE),
            "トラック3から6をソロ": (Action.SOLO, (2, 3, 4, 5), TargetOrigin.RANGE),
            "KickからBassまでミュート": (Action.MUTE, (0, 1, 2, 3, 4, 5), TargetOrigin.RANGE),
            "全部ミュート解除": (Action.UNMUTE, (0, 1, 2, 3, 4, 5), TargetOrigin.ALL),
            "ソロを全部外して": (Action.UNSOLO, (0, 1, 2, 3, 4, 5), TargetOrigin.ALL),
            "Bass以外全部ミュート": (Action.MUTE, (0, 1, 2, 3, 4), TargetOrigin.EXCEPT),
            "Bassだけソロ": (Action.SOLO, (5,), TargetOrigin.ONLY),
            "mute tracks 3 to 6": (Action.MUTE, (2, 3, 4, 5), TargetOrigin.RANGE),
            "mute Kick through Bass": (Action.MUTE, (0, 1, 2, 3, 4, 5), TargetOrigin.RANGE),
            "unmute everything": (Action.UNMUTE, (0, 1, 2, 3, 4, 5), TargetOrigin.ALL),
            "clear all solos": (Action.UNSOLO, (0, 1, 2, 3, 4, 5), TargetOrigin.ALL),
            "solo everything but Bass": (Action.SOLO, (0, 1, 2, 3, 4), TargetOrigin.EXCEPT),
            "solo only Bass": (Action.SOLO, (5,), TargetOrigin.ONLY),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                intent = parse_local(text, snapshot)
                self.assertIsNotNone(intent)
                self.assertEqual((intent.action, intent.tracks, intent.target_origin), expected)

        for text in ("全部止めて", "stop all clips"):
            with self.subTest(text=text):
                self.assertIs(parse_local(text, snapshot).action, Action.STOP_ALL_CLIPS)
        self.assertEqual(parse_local("全体を下げて", snapshot).track, "master")

    def test_compound_split_prefers_long_connectors_and_keeps_decimals(self) -> None:
        from intent import split_compound
        snapshot = _snapshot_with_song()
        self.assertEqual(split_compound("mute Pad, then solo Drums and arm Bass", snapshot), ["mute Pad", "solo Drums", "arm Bass"])
        self.assertEqual(split_compound("mute Pad; and then solo Drums", snapshot), ["mute Pad", "solo Drums"])
        self.assertEqual(split_compound("lower Pad by 2.5 dB and mute Bass", snapshot), ["lower Pad by 2.5 dB", "mute Bass"])
        self.assertEqual(split_compound("Padを2.5dB下げて", snapshot), ["Padを2.5dB下げて"])

    def test_compound_split_masks_names_quotes_and_rename_destinations(self) -> None:
        from intent import split_compound

        base = _snapshot_with_song()
        snapshot = replace(base, tracks=(replace(base.tracks[0], name="Drum and Bass"),) + base.tracks[1:])
        self.assertEqual(split_compound("mute Pad and solo Bass", base), ["mute Pad", "solo Bass"])
        self.assertEqual(split_compound("PadをミュートしてBassをソロ", base), ["Padをミュート", "Bassをソロ"])
        self.assertEqual(split_compound("mute Drum and Bass", snapshot), ["mute Drum and Bass"])
        self.assertEqual(split_compound("rename Bass to Rock and Roll", base), ["rename Bass to Rock and Roll"])


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

    def _service(self, bridge, **kwargs):
        return LiveJevService(
            bridge=bridge,
            snapshot=_snapshot_with_song(),
            key="x",
            requester=lambda _payload, _key: (_ for _ in ()).throw(AssertionError("Jev を呼んだ")),
            llm_key=None,
            rewriter=lambda *_args: (_ for _ in ()).throw(AssertionError("LLM を呼んだ")),
            **kwargs,
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

    def test_expired_confirmation_does_not_execute(self) -> None:
        now = [100.0]
        bridge = RecordingBridge()
        service = self._service(bridge, clock=lambda: now[0])
        self.assertEqual(service.process({"id": "1", "text": "録音開始"})["kind"], "confirm")
        now[0] += 31
        answer = service.process({"id": "2", "confirm": True})
        self.assertEqual(answer, {"id": "2", "kind": "info", "line": "時間が経ったので取り消しました。"})
        self.assertEqual(bridge.calls, [])
        self.assertFalse(service.snapshot.song.get("session_record"))

    def test_cancel_pending_clears_ask_and_confirm_without_write(self) -> None:
        from daemon import Pending
        from intent import IntentResult
        bridge = RecordingBridge()
        service = self._service(bridge)
        intent = parse_local("録音開始", service.snapshot)
        result = IntentResult(intent, (), (), ())
        service.pending = Pending(result, "track")
        service.pending_confirm = (intent, 0, 0, "録音開始", None)
        service.pending_confirm_created = service._clock()
        answer = service.process({"id": "2", "cmd": "cancel_pending"})
        self.assertEqual(answer["kind"], "status")
        self.assertIsNone(service.pending)
        self.assertIsNone(service.pending_confirm)
        self.assertIsNone(service.pending_confirm_created)
        self.assertEqual(bridge.calls, [])

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
        done = service.process({"id": "1", "confirm": True})
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
        self.assertEqual((audio.native_device, audio.track_kind, audio.text), ("Reverb", "audio", None))
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
        done = service.process({"id": "1", "confirm": True})
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
            done = service.process({"id": "1", "confirm": True})
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
        def __init__(self):
            super().__init__()
            self.selected_index = 1
            self.selection_fails = False

        def run(self, arguments):
            arguments = list(arguments)
            if "--api-session-context" in arguments:
                self.calls.append(arguments)
                if self.selection_fails:
                    raise BridgeError("selection unavailable")
                names = {0: "Pad", 1: "Bass", 2: "Drums"}
                payload = {"song": {}, "selected": {"track": {"path": f"live_set tracks {self.selected_index}", "name": names[self.selected_index]}}}
                return BridgeResult((Ack("api_session_context", arguments[-1], payload),), 1, 0, False)
            if "--api-mixer-status" in arguments:
                self.calls.append(arguments)
                target, request = arguments[-2], arguments[-1]
                prefix = "live_set master_track" if target == "master" else f"live_set tracks {target}"
                volume_path = f"{prefix} mixer_device volume"
                pan_path = f"{prefix} mixer_device panning"
                volume = self.values.get((volume_path, "value"), 0.75 if target == "master" else 0.55)
                pan = self.values.get((pan_path, "value"), 0.0)
                payload = {"parameters": {"volume": {"path": volume_path, "value": volume, "display": f"{volume:g}"}, "panning": {"path": pan_path, "value": pan, "display": f"{pan:g}"}}}
                return BridgeResult((Ack("api_mixer_status", request, payload),), 1, 0, False)
            if "--api-device-parameters" in arguments:
                self.calls.append(arguments)
                at = arguments.index("--api-device-parameters")
                path, request = arguments[at + 1:at + 3]
                parameter_path = f"{path} parameters 0"
                value = self.values.get((parameter_path, "value"), 1.0)
                return BridgeResult((Ack("api_device_parameters", request, {"parameters": [{"path": parameter_path, "value": value}]}, path),), 1, 0, False)
            return super().run(arguments)

    def _service(self, requester, snapshot=None, **kwargs):
        bridge = self.SelectedBridge()
        service = LiveJevService(bridge=bridge, snapshot=snapshot or _snapshot_with_song(), key="x", requester=requester, llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")), **kwargs)
        return bridge, service

    @staticmethod
    def _send_snapshot():
        base = _snapshot_with_song()
        tracks = tuple(replace(track, sends=(0.2, 0.0)) for track in base.tracks)
        return replace(base, tracks=tracks, returns=("Reverb", "Delay"))

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

    @staticmethod
    def _stated(answers, named: float):
        from tests.support import choice
        rest = 1.0 - named
        answers["answers"]["track_stated"] = choice("named" if named >= 0.5 else "absent", max(named, rest), named=named, absent=rest)
        return answers

    @staticmethod
    def _send_snapshot():
        base = _snapshot_with_song()
        return replace(base, tracks=tuple(replace(track, sends=(0.2, 0.0)) for track in base.tracks), returns=("Reverb", "Delay"))

    def test_rename_without_target_renames_selected_track(self) -> None:
        bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        for text in ("名前をLeadにして", "このトラックの名前をLeadに変えて", "トラック名をLeadにして"):
            bridge.calls.clear()
            service.process({"id": "1", "text": text})
            rename = next(call for call in bridge.calls if "--rename-track-index" in call)
            self.assertEqual((rename[rename.index("--rename-track-index") + 1], rename[rename.index("--rename-track-name") + 1]), ("1", "Lead"), text)

    def test_opposite_direction_is_not_a_continuation_of_the_previous_move(self) -> None:
        from tests.support import choice, response
        right = self._stated(response("pan", "none", "up_small"), 0.0)
        left = self._stated(response("pan", "none", "none", refers_previous=0.9), 0.0)
        left["answers"]["step"] = choice("set", 0.3)
        replies = iter([right, left])
        bridge, service = self._service(lambda *_: next(replies))
        service.process({"id": "1", "text": "右に振って"})
        bridge.calls.clear()
        service.process({"id": "2", "text": "少し左"})
        written = [float(call[3]) for call in bridge.calls if "--api-parameter-set" in call and "panning" in call[2]]
        self.assertTrue(written and written[-1] < 0.05, bridge.calls)

    def test_english_amount_and_politeness_words_are_not_target_names(self) -> None:
        for text in ("make it a bit louder please", "set the send A level to 100 percent", "send B up a bit", "could you turn it down a touch"):
            bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), snapshot=self._send_snapshot())
            try:
                answer = service.process({"id": "1", "text": text})
            except AssertionError:
                continue
            self.assertNotEqual(answer.get("line"), "指定したトラックが見つかりません", text)
        for text in ("make Ghost a bit louder please", "set strings send A to 100 percent"):
            bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), snapshot=self._send_snapshot())
            answer = service.process({"id": "1", "text": text})
            self.assertEqual(answer["kind"], "error", text)
            self.assertFalse(any("--write" in call for call in bridge.calls), text)

    def test_uncertain_band_keeps_confident_pick_and_asks_on_half_confident_pick(self) -> None:
        from tests.support import response
        for action, step in (("mute", "none"), ("volume", "up_small")):
            bridge, service = self._service(lambda *_: self._stated(response(action, "t2", step, track_conf=0.85), 0.35))
            kept = service.process({"id": "1", "text": "キック上げて"})
            self.assertEqual((kept["kind"], kept["decision"]["track"]), ("result", "Drums"), kept)
            bridge, service = self._service(lambda *_: self._stated(response(action, "t2", step, track_conf=0.6), 0.35))
            asked = service.process({"id": "1", "text": "キックっぽいの上げて"})
            self.assertEqual(asked["kind"], "ask", asked)
            self.assertFalse(any("--write" in call or "--api-set" in call for call in bridge.calls))
        bridge, service = self._service(lambda *_: self._stated(response("arm", "none", track_conf=0.5), 0.3))
        selected = service.process({"id": "1", "text": "アームして"})
        self.assertEqual((selected["kind"], selected["decision"]["track"]), ("result", "Bass"), selected)
        bridge, service = self._service(lambda *_: self._stated(response("volume", "none", "down_small", track_conf=0.38), 0.4))
        unknown = service.process({"id": "1", "text": "ベル下げて"})
        self.assertEqual(unknown["kind"], "ask", unknown)
        self.assertFalse(any("--write" in call for call in bridge.calls))
        bridge, service = self._service(lambda *_: self._stated(response("mute", "t2", track_conf=0.9), 0.1))
        unnamed = service.process({"id": "1", "text": "ミュート"})
        self.assertEqual((unnamed["kind"], unnamed["decision"]["track"]), ("result", "Bass"), unnamed)

    def test_numeric_track_name_is_not_treated_as_a_value(self) -> None:
        from intent import lower_setting_only_track_stated
        self.assertEqual(lower_setting_only_track_stated("808を下げて", 0.99, ("Kick", "808")), 0.99)
        self.assertEqual(lower_setting_only_track_stated("音量を-6dBにして", 0.83, ("Kick", "808")), 0.0)

    def test_device_spelled_out_in_text_is_matched_when_jev_is_unsure(self) -> None:
        from tests.support import choice, response
        answers = self._stated(response("device_off", "none", track_conf=0.54), 0.0)
        answers["answers"]["device_t0"] = choice("d0", 0.38)
        bridge, service = self._service(lambda *_: answers)
        answer = service.process({"id": "1", "text": "Reverbをオフ"})
        self.assertEqual((answer["kind"], answer["decision"]["track"]), ("result", "Pad"), answer)
        ghost = self._stated(response("device_off", "none", track_conf=0.8), 0.9)
        ghost["answers"]["device_t0"] = choice("d0", 0.95)
        bridge, service = self._service(lambda *_: ghost)
        refused = service.process({"id": "1", "text": "GhostのReverbをオフ"})
        self.assertEqual(refused["kind"], "error", refused)
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_selection_references_pass_the_target_gate(self) -> None:
        for text in ("solo this", "arm it", "mute it", "mute this track", "このトラックをミュート", "選択トラックをミュート"):
            bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
            answer = service.process({"id": "1", "text": text})
            self.assertEqual((answer["kind"], answer["decision"]["track"]), ("result", "Bass"), text)

    def test_named_track_with_db_amount_is_not_refused_locally(self) -> None:
        snapshot = _snapshot_with_song()
        for text in ("Bassを3dB下げて", "Bassの音量を3dB上げて", "Padを2.5デシベル下げて"):
            self.assertIsNone(parse_local(text, snapshot), text)
        named = parse_local("Bassを少し下げて", snapshot)
        self.assertEqual((named.action, named.track), (Action.VOLUME, 1))

    def test_every_undo_phrasing_restores_a_chain_without_live_undo(self) -> None:
        from tests.support import StatefulLive
        for phrase in ("undo that", "undo", "take that back", "元に戻して", "取り消して"):
            live = StatefulLive()
            service = LiveJevService(bridge=live, snapshot=_snapshot_with_song(), key="x", llm_key=None,
                                     requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
            self.assertEqual(service.process({"id": "1", "text": "mute Pad and solo Drums"})["kind"], "result", phrase)
            self.assertEqual((live.flags("mute")["Pad"], live.flags("solo")["Drums"]), (True, True), phrase)
            live.calls.clear()
            answer = service.process({"id": "2", "text": phrase})
            self.assertEqual(answer["kind"], "result", (phrase, answer))
            self.assertFalse(any("--api-call" in call for call in live.calls), phrase)
            self.assertFalse(any(live.flags("mute").values()) or any(live.flags("solo").values()), phrase)

    def test_stateful_chain_failures_leave_nothing_behind(self) -> None:
        from bridge_client import BridgeError
        from tests.support import StatefulLive

        def build():
            live = StatefulLive()
            service = LiveJevService(bridge=live, snapshot=_snapshot_with_song(), key="x", llm_key=None,
                                     requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
            return live, service

        live, service = build()
        plain_run = live.run

        def drift(arguments):
            if live.state[(0, "mute")]:
                live.names[1] = "Bass2"
            return plain_run(arguments)

        live.run = drift
        answer = service.process({"id": "1", "text": "mute Pad and solo Bass"})
        self.assertNotEqual(answer["kind"], "result", answer)
        self.assertFalse(any(live.flags("mute").values()) or any(live.flags("solo").values()), answer)

        live, service = build()
        service.process({"id": "0", "text": "solo Pad"})
        self.assertEqual(service.process({"id": "1", "text": "unmute all and mute Bass"})["kind"], "result")
        service.process({"id": "2", "text": "元に戻して"})
        self.assertEqual((live.flags("solo")["Pad"], live.flags("mute")["Bass"]), (True, False), "undoing the chain must not undo the earlier solo")

        live, service = build()
        live.state[(0, "mute")] = True
        service.process({"id": "1", "text": "unmute all"})
        self.assertFalse(any(live.flags("mute").values()), "the live value, not the stale snapshot, decides what to write")

        live, service = build()
        live.state[(1, "mute")] = True
        service.process({"id": "1", "text": "solo Pad and unmute"})
        self.assertEqual((live.flags("solo")["Pad"], live.flags("mute")["Bass"]), (True, True), "the bare second clause inherits Pad")

    def test_missing_name_from_jev_never_defaults_to_selected(self) -> None:
        from tests.support import response
        answers = self._stated(response("mute", "none", track_conf=0.83), 0.77)
        bridge, service = self._service(lambda *_: answers)
        answer = service.process({"id": "1", "text": "Ghostをミュート"})
        self.assertIn(answer["kind"], {"ask", "error"})
        self.assertFalse(any("--api-set" in call or "--write" in call for call in bridge.calls))

    def test_named_track_requires_positive_resolution_before_write(self) -> None:
        from tests.support import response
        low = self._stated(response("mute", "t0", track_conf=0.70), 0.67)
        bridge, service = self._service(lambda *_: low)
        answer = service.process({"id": "low", "text": "Ghostをミュート"})
        self.assertEqual(answer["kind"], "ask", answer)
        self.assertFalse(any("--api-set" in call or "--write" in call for call in bridge.calls))

        resolved = self._stated(response("mute", "t0", track_conf=0.93), 0.67)
        bridge, service = self._service(lambda *_: resolved)
        answer = service.process({"id": "resolved", "text": "Padをミュート"})
        self.assertEqual((answer["kind"], answer["decision"]["track"]), ("result", "Pad"), answer)

    def test_unnamed_guess_at_high_confidence_still_defaults_to_selected(self) -> None:
        from tests.support import choice, response
        answers = self._stated(response("send", "t0", "up_small", track_conf=0.90), 0.09)
        answers["answers"]["send"] = choice("send1")
        bridge, service = self._service(lambda *_: answers, snapshot=self._send_snapshot())
        answer = service.process({"id": "1", "text": "センドBを少し上げて"})
        self.assertEqual((answer["kind"], answer["decision"]["track"]), ("result", "Bass"), answer)

    def test_direction_is_read_from_words_when_jev_is_unsure(self) -> None:
        from daemon import direction_from_words
        from intent import Action, Step, interpret_response
        self.assertIs(direction_from_words(Action.PAN, "右に振って"), Step.UP_SMALL)
        self.assertIs(direction_from_words(Action.PAN, "少し左"), Step.DOWN_SMALL)
        self.assertIs(direction_from_words(Action.PAN, "pan hard left"), Step.DOWN_BIG)
        self.assertIs(direction_from_words(Action.SEND, "センドAを上げて"), Step.UP_SMALL)
        self.assertIs(direction_from_words(Action.SEND, "リバーブ送りをかなり減らして"), Step.DOWN_BIG)
        self.assertIsNone(direction_from_words(Action.VOLUME, "右のほう"))
        self.assertIsNone(direction_from_words(Action.VOLUME, "上げて下げて"))
        for text in ("仕上げて", "見上げて", "打ち上げて", "get the balance right", "right is correct", "pan right to center", "右から真ん中に"):
            self.assertIsNone(direction_from_words(Action.PAN, text), text)
        from tests.support import choice, response
        answers = self._stated(response("pan", "none", "none"), 0.0)
        answers["answers"]["step"] = choice("none", 0.42)
        bridge, service = self._service(lambda *_: answers)
        answer = service.process({"id": "1", "text": "右に振って"})
        self.assertEqual((answer["kind"], answer["decision"]["track"]), ("result", "Bass"), answer)

        confident_set = self._stated(response("pan", "none", "set"), 0.0)
        confident_set["answers"]["step"] = choice("set", 0.8)
        result = service._step_from_utterance(interpret_response(service.snapshot, "pan right", confident_set), "pan right")
        self.assertIs(result.intent.step, Step.SET)

    def test_named_but_missing_track_errors_instead_of_selected(self) -> None:
        from tests.support import response
        answers = response("volume", "none", "down_small", track_conf=0.5)
        answers["answers"]["track_stated"] = {"type": "noul", "noul": 0.85}
        bridge, service = self._service(lambda *_: answers)
        answer = service.process({"id": "1", "text": "パッドを少し下げて"})
        self.assertEqual(answer["kind"], "error")
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_named_but_uncertain_track_still_asks(self) -> None:
        from tests.support import response
        bridge, service = self._service(lambda *_: response("mute", "t0", track_conf=0.4))
        answer = service.process({"id": "1", "text": "パッドっぽいのミュート"})
        self.assertEqual(answer["kind"], "ask")
        self.assertFalse(any("--api-set" in call for call in bridge.calls))

    def test_send_without_named_track_defaults_to_selected(self) -> None:
        from tests.support import choice, response
        answer_data = response("send")
        answer_data["answers"]["send"] = choice("send0")
        answer_data["answers"]["step"] = choice("set")
        bridge, service = self._service(lambda *_: answer_data, snapshot=self._send_snapshot())
        answer = service.process({"id": "1", "text": "SendAを100%にして"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["decision"]["track"], "Bass")
        write = next(call for call in bridge.calls if "--api-parameter-set" in call)
        self.assertEqual(write[:3], ["--write", "--api-parameter-set", "live_set tracks 1 mixer_device sends 0"])
        self.assertEqual(float(write[3]), 1.0)

    def test_send_with_named_track_keeps_named_track(self) -> None:
        from tests.support import choice, response
        answer_data = response("send", "t2", "up_small", track_conf=0.95)
        answer_data["answers"]["send"] = choice("send0")
        bridge, service = self._service(lambda *_: answer_data, snapshot=self._send_snapshot())
        answer = service.process({"id": "1", "text": "ドラムのセンドAを上げて"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["decision"]["track"], "Drums")
        self.assertTrue(any("live_set tracks 2 mixer_device sends 0" in call for call in bridge.calls))

    def test_english_local_send_without_track_defaults_to_selected(self) -> None:
        bridge, service = self._service(
            lambda *_: (_ for _ in ()).throw(AssertionError("Jev")),
            snapshot=self._send_snapshot(),
        )
        answer = service.process({"id": "1", "text": "set send A to 100%"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["decision"]["track"], "Bass")
        self.assertTrue(any("live_set tracks 1 mixer_device sends 0" in call for call in bridge.calls))

    def test_expired_ask_processes_answer_as_fresh_text(self) -> None:
        from tests.support import response
        now = [100.0]
        replies = [response("volume", "none", "down_small", track_conf=0.5), response("mute", "t1")]
        replies[0]["answers"]["track_stated"] = {"type": "noul", "noul": 0.85}
        bridge, service = self._service(lambda *_: replies.pop(0), clock=lambda: now[0])
        self.assertEqual(service.process({"id": "1", "text": "パッドを下げて"})["kind"], "error")
        now[0] += 31
        answer = service.process({"id": "2", "text": "Bass"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(answer["decision"]["action"], "mute")
        self.assertTrue(any("--api-set" in call and "mute" in call for call in bridge.calls))

    def test_explicit_missing_tracks_never_default_to_selected(self) -> None:
        for text in ("mute Ghost", "mute track 99", "トラック99をミュート"):
            with self.subTest(text=text):
                bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
                answer = service.process({"id": "missing", "text": text})
                self.assertEqual(answer["kind"], "error")
                self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_identifier_numbers_are_not_values(self) -> None:
        snapshot = self._send_snapshot()
        send = parse_local("track 3 send A to 50 percent", snapshot)
        gain = parse_local("Bass clip 2 gain to 50 percent", snapshot)
        pan = parse_local("pan Bass left 20%", snapshot)
        self.assertEqual(send.number, Number(50.0, "percent"))
        self.assertEqual(gain.number, Number(50.0, "percent"))
        self.assertEqual(pan.number, Number(-20.0, "percent"))

    def test_local_compounds_run_as_chains_and_and_in_name_is_safe(self) -> None:
        class ChainBridge(self.SelectedBridge):
            def __init__(self, snapshot):
                super().__init__()
                self.names = {track.path: track.name for track in snapshot.tracks}

            def run(self, arguments):
                arguments = list(arguments)
                if "--api-get" in arguments and all(arguments[offset + 2] == "name" for offset, item in enumerate(arguments) if item == "--api-get"):
                    self.calls.append(arguments)
                    acks = []
                    for offset, item in enumerate(arguments):
                        if item == "--api-get":
                            path, prop, request = arguments[offset + 1:offset + 4]
                            acks.append(Ack("api_get", request, self.names[path], path, prop))
                    return BridgeResult(tuple(acks), 1, 0, False)
                return super().run(arguments)

        def service_for(snapshot=None):
            snapshot = snapshot or _snapshot_with_song()
            bridge = ChainBridge(snapshot)
            calls = []

            def requester(*args):
                from tests.support import response
                calls.append(args)
                return response("none", action_conf=0.1)

            service = LiveJevService(
                bridge=bridge, snapshot=snapshot, key="x", requester=requester,
                llm_key=None, rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")),
            )
            return bridge, service, calls

        for text in ("mute Pad and solo Bass", "PadをミュートしてBassをソロ"):
            with self.subTest(text=text):
                bridge, service, jev_calls = service_for()
                answer = service.process({"id": "compound", "text": text})
                self.assertEqual(answer["kind"], "result", answer)
                writes = [call for call in bridge.calls if "--write" in call]
                self.assertEqual([(call[2], call[3]) for call in writes], [("live_set tracks 0", "mute"), ("live_set tracks 1", "solo")])
                self.assertEqual(jev_calls, [])

        named = replace(_snapshot_with_song(), tracks=(replace(_snapshot_with_song().tracks[0], name="Drum and Bass"),) + _snapshot_with_song().tracks[1:])
        bridge, service, jev_calls = service_for(named)
        answer = service.process({"id": "named", "text": "mute Drum and Bass"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertEqual(len([call for call in bridge.calls if "--write" in call]), 1)
        self.assertEqual(jev_calls, [])

        for text in ("mute Pad and solo Ghost", "PadをミュートしてGhostをソロ"):
            with self.subTest(text=text):
                bridge, service, _jev_calls = service_for()
                answer = service.process({"id": "missing", "text": text})
                self.assertIn(answer["kind"], {"info", "error"})
                self.assertIn("Ghost", answer["line"])
                self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_device_toggle_resolves_after_selected_track(self) -> None:
        from snapshot import Device, Param
        base = _snapshot_with_song()
        power = Param(0, "Device On", 1.0, 0.0, 1.0, "On", "live_set tracks 1 devices 0 parameters 0")
        bass_reverb = Device(0, "Reverb", (power,), "live_set tracks 1 devices 0")
        snapshot = replace(base, tracks=(base.tracks[0], replace(base.tracks[1], devices=(bass_reverb,)), base.tracks[2]))
        bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), snapshot=snapshot)
        answer = service.process({"id": "device", "text": "turn Reverb off on Bass"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertTrue(any("live_set tracks 1 devices 0 parameters 0" in call for call in bridge.calls))

    def test_device_without_track_uses_the_only_owner_when_selected_track_lacks_it(self) -> None:
        bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        answer = service.process({"id": "device", "text": "turn Reverb off"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertTrue(any("live_set tracks 0 devices 0 parameters 0" in call for call in bridge.calls))
        bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        named = service.process({"id": "device", "text": "turn Reverb off on Bass"})
        self.assertEqual(named["kind"], "error", named)
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_round_five_target_authorization_regressions(self) -> None:
        from tests.support import choice, response

        f1 = self._stated(response("param", "t0", "down_small", track_conf=0.67, param="d0p0"), 0.90)
        bridge, service = self._service(lambda *_: f1)
        asked = service.process({"id": "f1", "text": "Ghostのつまみを下げて"})
        self.assertEqual(asked["kind"], "ask")
        refused = service.process({"id": "f1-answer", "text": "Bass", "answering": "f1"})
        self.assertEqual(refused["kind"], "error")
        self.assertFalse(any("--write" in call for call in bridge.calls))

        f2 = self._stated(response("device_off", "none", track_conf=0.2), 0.40)
        f2["answers"]["device_t0"] = choice("d0", 0.95)
        bridge, service = self._service(lambda *_: f2)
        refused = service.process({"id": "f2", "text": "ベルのReverbを切って"})
        self.assertIn(refused["kind"], {"ask", "error"})
        self.assertFalse(any("--write" in call for call in bridge.calls))

        replies = iter([self._stated(response("volume", "none", "down_small", refers_previous=0.95), 0.90)])
        bridge, service = self._service(lambda *_: next(replies))
        self.assertEqual(service.process({"id": "first", "text": "lower Bass"})["kind"], "result")
        bridge.calls.clear()
        refused = service.process({"id": "f3", "text": "Ghostも同じくらい下げて"})
        self.assertIn(refused["kind"], {"ask", "error"})
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_round_five_continuation_tracks_current_selection_or_named_target(self) -> None:
        bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        self.assertEqual(service.process({"id": "selected", "text": "lower it"})["kind"], "result")
        bridge.selected_index = 2
        bridge.calls.clear()
        self.assertEqual(service.process({"id": "more", "text": "a bit more"})["decision"]["track"], "Drums")
        self.assertTrue(any("live_set tracks 2" in call for call in bridge.calls))

        bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        self.assertEqual(service.process({"id": "named", "text": "lower Bass"})["kind"], "result")
        bridge.selected_index = 2
        bridge.calls.clear()
        self.assertEqual(service.process({"id": "more", "text": "a bit more"})["decision"]["track"], "Bass")

    def test_round_five_low_evidence_candidates_and_selection_failure_do_not_write(self) -> None:
        from snapshot import Clip
        from tests.support import choice, response
        master = self._stated(response("volume", "master", "down_small"), 0.10)
        bridge, service = self._service(lambda *_: master)
        answer = service.process({"id": "master", "text": "ちょい下げて"})
        self.assertEqual(answer["decision"]["track"], "Bass")
        self.assertFalse(any("master" in item for call in bridge.calls for item in call))

        base = _snapshot_with_song()
        pad_clip = Clip(0, "Pad Clip", "live_set tracks 0 clip_slots 0 clip", {"looping": True})
        bass_clip = Clip(0, "Bass Clip", "live_set tracks 1 clip_slots 0 clip", {"looping": True})
        clip_snapshot = replace(base, tracks=(replace(base.tracks[0], clips=(pad_clip,)), replace(base.tracks[1], clips=(bass_clip,)), base.tracks[2]))
        clip_pick = self._stated(response("clip_loop_off", "t0", track_conf=0.96), 0.10)
        clip_pick["answers"]["clip_t0"] = choice("c0", 0.96)
        bridge, service = self._service(lambda *_: clip_pick, snapshot=clip_snapshot)
        answer = service.process({"id": "clip", "text": "クリップのループを解除して"})
        self.assertEqual(answer["decision"]["track"], "Bass")
        self.assertTrue(any("live_set tracks 1 clip_slots 0 clip" in call for call in bridge.calls))

        guessed = self._stated(response("mute", "t0", track_conf=0.96), 0.10)
        bridge, service = self._service(lambda *_: guessed)
        bridge.selection_fails = True
        answer = service.process({"id": "failed", "text": "ミュートして"})
        self.assertIn(answer["kind"], {"ask", "error"})
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_round_five_hiragana_and_bare_number_are_name_like(self) -> None:
        from intent import lower_setting_only_track_stated
        self.assertEqual(lower_setting_only_track_stated("べるを下げて", 0.90), 0.90)
        bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        answer = service.process({"id": "909", "text": "mute 909"})
        self.assertEqual(answer["kind"], "error")
        self.assertFalse(any("--write" in call for call in bridge.calls))

    def test_round_five_stale_answer_id_never_becomes_a_plugin_request(self) -> None:
        from unittest import mock
        import daemon as D
        from tests.support import response
        answers = self._stated(response("mute", "none", track_conf=0.2), 0.40)
        bridge, service = self._service(lambda *_: answers)
        asked = service.process({"id": "ask-id", "text": "ベルをミュート"})
        self.assertEqual(asked["kind"], "ask")
        service.pending = None
        with mock.patch.object(service, "_plugin_names", return_value=("iZOzone12BassControl",)), \
             mock.patch.object(D.plugin_script, "load") as load:
            expired = service.process({"id": "answer", "text": "Bass", "answering": "ask-id"})
        self.assertEqual(expired["line"], service._m("info.expired"))
        load.assert_not_called()

    def test_value_phrases_are_never_a_rename(self) -> None:
        snapshot = _snapshot_with_song()
        for text in ("Bassをソロにして", "Bassをミュートにして", "Bassを-6dBにして", "Bassを右にして", "Bassをオフにして", "Bassを100%にして", "BassをLow Endにして"):
            parsed = parse_local(text, snapshot)
            self.assertFalse(parsed is not None and parsed.action is Action.RENAME, text)
        for text in ("Bassの名前をLow Endにして", "BassをLow Endに改名", "BassをLow Endにリネームして", "BassをLow Endという名前にして"):
            parsed = parse_local(text, snapshot)
            self.assertIs(parsed.action, Action.RENAME, text)
            self.assertEqual((parsed.track, parsed.text), (1, "Low End"), text)

    def test_english_rename_keeps_case(self) -> None:
        for text in ("rename Bass to Low End", "call Bass Low End", "Rename Bass to \"Low End\".", "rename Bass to Low End please"):
            parsed = parse_local(text, _snapshot_with_song())
            self.assertEqual((parsed.action, parsed.track, parsed.text), (Action.RENAME, 1, "Low End"), text)

    def test_note_transform_explicit_target_never_falls_back(self) -> None:
        from intent import parse_clip_notes_phrase
        for text in ("quantize Ghost clip 1", "quantize track 99 clip 1", "Ghostのクリップ1をクオンタイズ", "Ghostのクリップをクオンタイズ", "Ghostをクオンタイズ", "Ghost quantize", "double the loop on Ghost"):
            request = parse_clip_notes_phrase(text, _snapshot_with_song())
            self.assertTrue(request.target_missing, text)
        bass = parse_clip_notes_phrase("quantize Bass", _snapshot_with_song())
        self.assertEqual((bass.track, bass.slot, bass.target_missing), (1, None, False))

    def test_unresolved_clip_note_target_never_calls_the_script(self) -> None:
        from unittest import mock
        import daemon as D
        service = LiveJevService(
            bridge=RecordingBridge(), snapshot=_snapshot_with_song(), key="x",
            requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")),
        )
        with mock.patch.object(D.plugin_script, "ping", return_value=True), \
             mock.patch.object(D.plugin_script, "clip_notes") as clip_notes:
            for index, text in enumerate(("Ghostのクリップをクオンタイズ", "Ghostをクオンタイズ", "Ghost quantize", "double the loop on Ghost")):
                answer = service.process({"id": str(index), "text": text})
                self.assertEqual(answer["kind"], "error", text)
            clip_notes.assert_not_called()

    def test_named_audio_track_keeps_kind_and_case(self) -> None:
        en = parse_local("create an audio track named FX with Utility", _snapshot_with_song())
        ja = parse_local("FXという名前のオーディオトラックを作って", _snapshot_with_song())
        self.assertEqual((en.text, en.track_kind), ("FX", "audio"))
        self.assertEqual((ja.text, ja.action), ("FX", Action.ADD_AUDIO_TRACK))
        self.assertEqual(ACTIONS[en.action].apply(_snapshot_with_song(), en)[0][:5], ["--write", "--add-audio-tracks", "1", "--audio-prefix", "FX"])

    def test_mixed_case_ja_toggle_never_raises(self) -> None:
        intent = parse_local("BassをMUTEして", _snapshot_with_song())
        self.assertIs(intent.action, Action.MUTE)

    def test_round_six_missing_plugin_track_never_uses_selection(self) -> None:
        from unittest import mock
        import daemon as D

        for index, text in enumerate(("Ghostに Diva を入れて", "GhostにDivaを挿して", "insert Diva on Ghost", "put Diva on the Ghost track", "load Serum 2 onto Ghost")):
            bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
            with mock.patch.object(service, "_plugin_names", return_value=("Diva", "Serum 2")), \
                 mock.patch.object(D.plugin_script, "load") as load:
                answer = service.process({"id": str(index), "text": text})
            self.assertEqual(answer["kind"], "error", text)
            load.assert_not_called()
            self.assertFalse(any("--write" in call for call in bridge.calls), text)

    def test_round_six_plugin_target_and_selection_are_preserved(self) -> None:
        from unittest import mock
        import daemon as D
        from snapshot import Device

        cases = (("Divaを入れて", 1), ("insert Diva", 1), ("BassにDivaを入れて", 1), ("insert Diva on Bass", 1))
        for index, (text, expected_track) in enumerate(cases):
            base = _snapshot_with_song()
            loaded = replace(base, tracks=tuple(
                replace(track, devices=(Device(0, "Diva", (), f"live_set tracks {expected_track} devices 0"),))
                if track.index == expected_track else track for track in base.tracks
            ))
            bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), snapshot=base)
            service.reader = type("R", (), {"read": staticmethod(lambda snapshot=loaded: (snapshot, 1))})()
            with mock.patch.object(service, "_plugin_names", return_value=("Diva",)), \
                 mock.patch.object(D.plugin_script, "load", return_value={"track_index": expected_track, "devices_after": ["Diva"]}) as load:
                answer = service.process({"id": str(index), "text": text})
            self.assertEqual(answer["kind"], "result", text)
            load.assert_called_once_with("Diva", expected_track, "")

        base = _snapshot_with_song()
        whole_name = "Diva on Ghost"
        loaded = replace(base, tracks=tuple(
            replace(track, devices=(Device(0, whole_name, (), "live_set tracks 1 devices 0"),))
            if track.index == 1 else track for track in base.tracks
        ))
        _bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), snapshot=base)
        service.reader = type("R", (), {"read": staticmethod(lambda: (loaded, 1))})()
        with mock.patch.object(service, "_plugin_names", return_value=(whole_name,)), \
             mock.patch.object(D.plugin_script, "load", return_value={"track_index": 1, "devices_after": [whole_name]}) as load:
            answer = service.process({"id": "whole-name", "text": "insert Diva on Ghost"})
        self.assertEqual(answer["kind"], "result")
        load.assert_called_once_with(whole_name, 1, "")

    def test_round_six_literal_track_conflict_refuses_wrong_jev_pick(self) -> None:
        from tests.support import response

        answers = self._stated(response("mute", "t0", track_conf=0.96), 0.95)
        bridge, service = self._service(lambda *_: answers)
        refused = service.process({"id": "conflict", "text": "silence Bass"})
        self.assertEqual(refused["kind"], "error")
        self.assertFalse(any("--write" in call or "--api-set" in call for call in bridge.calls))

        for index, text in enumerate(("mute Padding", "mute SubBass", "SubBassをミュート")):
            bridge, service = self._service(lambda *_: answers)
            substring = service.process({"id": f"substring-{index}", "text": text})
            self.assertEqual(substring["kind"], "error", text)
            self.assertFalse(any("--write" in call or "--api-set" in call for call in bridge.calls), text)

    def test_round_six_clip_note_targets_require_exact_names(self) -> None:
        from unittest import mock
        import daemon as D

        bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        with mock.patch.object(D.plugin_script, "ping", return_value=True), \
             mock.patch.object(D.plugin_script, "clip_notes") as clip_notes:
            for index, text in enumerate(("SubBassのクリップ1をクオンタイズ", "quantize SubBass clip 1", "quantize clip 1 on Bassline")):
                answer = service.process({"id": str(index), "text": text})
                self.assertEqual(answer["kind"], "error", text)
            clip_notes.assert_not_called()

    def test_round_six_english_selection_rename_keeps_case(self) -> None:
        for index, text in enumerate(("rename this track to Lead", "rename it to Lead", "call this Lead")):
            bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
            answer = service.process({"id": str(index), "text": text})
            self.assertEqual(answer["kind"], "result", text)
            rename = next(call for call in bridge.calls if "--rename-track-index" in call)
            self.assertEqual((rename[rename.index("--rename-track-index") + 1], rename[rename.index("--rename-track-name") + 1]), ("1", "Lead"))

    def test_round_six_master_aliases_and_followups_keep_master(self) -> None:
        from unittest import mock

        for index, text in enumerate(("lower the master", "lower the whole mix by 3 dB", "turn the master down a bit", "master volume down 2 dB")):
            _bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
            with mock.patch.object(service, "_set_volume_db", return_value=(service.snapshot, 1, False)):
                answer = service.process({"id": str(index), "text": text})
            self.assertEqual((answer["kind"], answer["decision"]["track"]), ("result", "マスター"), text)

        _bridge, service = self._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        with mock.patch.object(service, "_set_volume_db", return_value=(service.snapshot, 1, False)):
            self.assertEqual(service.process({"id": "master", "text": "マスターを下げて"})["kind"], "result")
            for index, text in enumerate(("もう少し", "do it again", "元に戻して")):
                answer = service.process({"id": f"follow-{index}", "text": text})
                self.assertEqual((answer["kind"], answer["decision"]["track"]), ("result", "マスター"), text)

    def test_round_six_named_track_threshold_accepts_measured_valid_pick(self) -> None:
        from tests.support import response

        answers = self._stated(response("volume", "t2", "up_small", track_conf=0.79), 0.80)
        _bridge, service = self._service(lambda *_: answers)
        answer = service.process({"id": "threshold", "text": "キック上げて"})
        self.assertEqual((answer["kind"], answer["decision"]["track"]), ("result", "Drums"))


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

    def test_device_reorder_is_stale(self) -> None:
        import time as _time
        from snapshot import Device
        base = _snapshot_with_song()
        devices = (base.tracks[0].devices[0], Device(1, "Utility", (), "live_set tracks 0 devices 1"))
        snapshot = replace(base, tracks=(replace(base.tracks[0], devices=devices),) + base.tracks[1:], taken_at=_time.time() - 60)

        class ReorderedBridge(RecordingBridge):
            def run(self, arguments):
                if "--api-device-list" in arguments:
                    tracks = [{"track": {"name": track.name}, "devices": [{"name": device.name} for device in reversed(track.devices)]} for track in snapshot.tracks]
                    return BridgeResult((Ack("api_device_list", arguments[-1], {"tracks": tracks}),), 1, 0, False)
                return super().run(arguments)

        service = LiveJevService(bridge=ReorderedBridge(), snapshot=snapshot, key="x")
        self.assertTrue(service._snapshot_is_stale())

    def test_structure_refresh_clears_previous_and_undo_marker(self) -> None:
        base = _snapshot_with_song()
        changed = replace(base, tracks=(replace(base.tracks[0], name="Renamed"),) + base.tracks[1:])
        service = LiveJevService(bridge=RecordingBridge(), snapshot=base, key="x")
        service.previous = object()
        service._undo_target_tracks = (3, 4)
        service.reader = type("R", (), {"read": staticmethod(lambda: (changed, 1))})()
        service.refresh()
        self.assertIsNone(service.previous)
        self.assertIsNone(service._undo_target_tracks)


class ConnectionAndCacheRegressionTests(unittest.TestCase):
    def test_exact_native_device_precedes_generic_notice(self) -> None:
        import daemon as D
        bridge = RecordingBridge()
        service = LiveJevService(bridge=bridge, snapshot=_snapshot_with_song(), key="x", requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        with unittest.mock.patch.object(D, "REQUIRE_CONFIRM", True):
            exact = service.process({"id": "eq8", "text": "EQ Eight入りのトラック作って"})
        self.assertEqual(exact["kind"], "confirm")
        generic = service.process({"id": "eq", "text": "EQ入りのトラック作って"})
        self.assertEqual(generic["kind"], "info")

    def test_native_device_clarification_resolves_operator(self) -> None:
        import daemon as D
        from daemon import Pending
        from intent import IntentResult, _local_intent
        intent = _local_intent(Action.ADD_TRACK_WITH_DEVICE)
        service = LiveJevService(bridge=RecordingBridge(), snapshot=_snapshot_with_song(), key="x")
        service.pending = Pending(IntentResult(intent, (), (), ()), "native_device")
        with unittest.mock.patch.object(D, "REQUIRE_CONFIRM", True):
            answer = service.process({"id": "native", "text": "Operator"})
        self.assertEqual(answer["kind"], "confirm")
        self.assertEqual(service.pending_confirm[0].native_device, "Operator")

    def test_disconnected_status_and_command_reprobe(self) -> None:
        base = _snapshot_with_song()
        service = LiveJevService(bridge=SelectedTrackTests.SelectedBridge(), snapshot=None, key="x", requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        service.reader = type("R", (), {"read": staticmethod(lambda: (base, 1))})()
        status = service.process({"id": "s", "cmd": "status"})
        self.assertTrue(status["live"])
        answer = service.process({"id": "m", "text": "mute Bass"})
        self.assertEqual(answer["kind"], "result")

    def test_failed_script_ping_is_not_cached(self) -> None:
        import daemon as D
        service = LiveJevService(bridge=RecordingBridge(), snapshot=_snapshot_with_song(), key="x")
        with unittest.mock.patch.object(D.plugin_script, "ping", side_effect=[False, True]):
            self.assertFalse(service._script_available())
            self.assertTrue(service._script_available())

    def test_relative_change_refreshes_before_calculation(self) -> None:
        base = _snapshot_with_song()
        fresh = replace(base, tracks=(replace(base.tracks[0], volume_display="-20.0 dB"),) + base.tracks[1:])
        bridge, service = SelectedTrackTests()._service(lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), snapshot=base)
        seen = []
        service._refresh_target = lambda intent: (fresh, 1)
        service._set_volume_db = lambda intent: (seen.append(service.snapshot.tracks[0].volume_display) or service.snapshot, 1, False)
        answer = service.process({"id": "relative", "text": "lower Pad by 3 dB"})
        self.assertEqual(answer["kind"], "result")
        self.assertEqual(seen, ["-20.0 dB"])

    def test_swift_wire_and_hidden_request_regressions_are_encoded(self) -> None:
        root = __import__("pathlib").Path(__file__).parents[1]
        messages = (root / "LiveJev/Sources/LiveJev/Messages.swift").read_text()
        view_model = (root / "LiveJev/Sources/LiveJev/ViewModel.swift").read_text()
        panel = (root / "LiveJev/Sources/LiveJev/Panel.swift").read_text()
        self.assertIn("case status, result, ask, confirm, info, error, unknown", messages)
        self.assertIn("case .unknown:", messages)
        self.assertIn("let requestID: String?", view_model)
        self.assertIn("canUndoLastSuccess", view_model)
        self.assertIn("outstandingHiddenRequestIDs", panel)
        self.assertNotIn("awaitingHiddenResult", panel)

    def test_publish_sync_excludes_git(self) -> None:
        root = __import__("pathlib").Path(__file__).parents[1]
        path = root / "scripts/publish_sync.sh"
        if not path.exists():
            self.skipTest("the publishing script is not part of the public copy")
        self.assertIn('".git" "__pycache__"', path.read_text())

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


class PluginFormatPreferenceTests(unittest.TestCase):
    def test_vst3_is_preferred_over_au_and_vst2(self) -> None:
        from daemon import preferred_plugin_uris
        items = [
            {"name": "Omnisphere", "uri": "query:Plugins#AUv2:Spectrasonics:Omnisphere"},
            {"name": "Omnisphere", "uri": "query:Plugins#VST:Local:Omnisphere"},
            {"name": "Omnisphere", "uri": "query:Plugins#VST3:Spectrasonics:Omnisphere"},
            {"name": "OnlyAU", "uri": "query:Plugins#AUv2:Vendor:OnlyAU"},
            {"name": "Operator", "uri": "query:Synths#Operator"},
        ]
        uris = preferred_plugin_uris(items)
        self.assertEqual(uris["Omnisphere"], "query:Plugins#VST3:Spectrasonics:Omnisphere")
        self.assertEqual(uris["OnlyAU"], "query:Plugins#AUv2:Vendor:OnlyAU")
        self.assertEqual(uris["Operator"], "query:Synths#Operator")

    def test_new_track_loads_the_preferred_uri(self) -> None:
        from unittest import mock
        import daemon as D
        calls: list[tuple] = []
        service = LiveJevService(bridge=RecordingBridge(), snapshot=_snapshot_with_song(), key="x",
                                 requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")), llm_key=None,
                                 rewriter=lambda *_: (_ for _ in ()).throw(AssertionError("LLM")))
        service.reader = type("R", (), {"read": staticmethod(lambda: (_snapshot_with_song(), 1))})()
        items = [{"name": "Omnisphere", "uri": "query:Plugins#AUv2:Spectrasonics:Omnisphere"}, {"name": "Omnisphere", "uri": "query:Plugins#VST3:Spectrasonics:Omnisphere"}]
        with mock.patch.object(D.plugin_script, "ping", return_value=True), \
             mock.patch.object(D.plugin_script, "list_plugins", return_value=items), \
             mock.patch.object(D.plugin_script, "add_track", side_effect=lambda kind, name=None, device=None: calls.append(("add", kind, name, device)) or {"ok": True, "track_index": 3, "devices_after": []}), \
             mock.patch.object(D.plugin_script, "load", side_effect=lambda name, track, uri="": calls.append(("load", name, track, uri)) or {"ok": True, "track_index": track, "devices_after": ["Omnisphere"]}):
            answer = service.process({"id": "1", "text": "新規トラックでomnisphere開いて"})
        self.assertEqual(answer["kind"], "result", answer)
        self.assertEqual(calls, [("add", "midi", None, None), ("load", "Omnisphere", 3, "query:Plugins#VST3:Spectrasonics:Omnisphere")])


class RoundThreeRegressionTests(unittest.TestCase):
    def test_relative_db_fetches_missing_display_before_write_and_read_failure_is_plain_error(self) -> None:
        import json

        class Bridge:
            def __init__(self, fail_display=False):
                self.calls = []
                self.value = 52 / 60
                self.fail_display = fail_display

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                if "--api-get" in arguments:
                    at = arguments.index("--api-get")
                    return BridgeResult((Ack("api_get", arguments[at + 3], "Pad", arguments[at + 1], "name"),), 1, 0, False)
                if "--api-parameter-set" in arguments:
                    self.value = float(arguments[arguments.index("--api-parameter-set") + 2])
                    return BridgeResult((), 1, 0, False)
                if "--api-call" in arguments:
                    if self.fail_display:
                        return BridgeResult((), 1, 0, False)
                    at = arguments.index("--api-call")
                    value = json.loads(arguments[at + 3])[0]
                    return BridgeResult((Ack("api_call", arguments[at + 4], f"{-60 + value * 60:.1f} dB", arguments[at + 1], "str_for_value"),), 1, 0, False)
                at = arguments.index("--api-mixer-status")
                payload = {"parameters": {"volume": {"path": "live_set tracks 0 mixer_device volume", "value": self.value}}}
                return BridgeResult((Ack("api_mixer_status", arguments[at + 2], payload),), 1, 0, False)

        bridge = Bridge()
        service = LiveJevService(bridge=bridge, snapshot=_snapshot_with_song(), key="x", requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        self.assertEqual(service.process({"id": "db", "text": "lower Pad by 3 dB"})["kind"], "result")
        first_write = next(index for index, call in enumerate(bridge.calls) if "--api-parameter-set" in call)
        self.assertTrue(any("--api-call" in call for call in bridge.calls[:first_write]))
        from intent import Step
        self.assertIs(service.previous.step, Step.DOWN_SMALL, "a dB move must remember its direction so that 'a bit more' can repeat it")

        failed = Bridge(fail_display=True)
        service = LiveJevService(bridge=failed, snapshot=_snapshot_with_song(), key="x", requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        answer = service.process({"id": "db", "text": "lower Pad by 3 dB"})
        self.assertEqual(answer["kind"], "error")
        self.assertIn("現在値", answer["line"])
        self.assertFalse(any("--api-parameter-set" in call for call in failed.calls))

    def test_relative_parameter_rebinds_to_live_value_by_path_and_name(self) -> None:
        from intent import _local_intent
        snapshot = _snapshot_with_song()
        stale = snapshot.tracks[0].devices[0].params[0]
        fresh = replace(stale, value=0.80)
        fresh_device = replace(snapshot.tracks[0].devices[0], params=(fresh,))
        live = replace(snapshot, tracks=(replace(snapshot.tracks[0], devices=(fresh_device,)),) + snapshot.tracks[1:])
        service = LiveJevService(bridge=RecordingBridge(), snapshot=snapshot, key="x")
        rebound = service._rebind_refreshed_intent(live, replace(_local_intent(Action.PARAM, track=0, step=Step.UP_SMALL), param=stale))
        self.assertEqual(rebound.param.value, 0.80)
        missing = replace(live, tracks=(replace(live.tracks[0], devices=()),) + live.tracks[1:])
        with self.assertRaisesRegex(ValueError, "現在値"):
            service._rebind_refreshed_intent(missing, replace(_local_intent(Action.PARAM, track=0, step=Step.UP_SMALL), param=stale))

    def test_device_layout_refresh_invalidates_repeat_and_pending_targets(self) -> None:
        from snapshot import Device
        snapshot = _snapshot_with_song()
        changed = replace(snapshot, tracks=(replace(snapshot.tracks[0], devices=(Device(0, "Utility", (), "live_set tracks 0 devices 0"),)),) + snapshot.tracks[1:])
        service = LiveJevService(bridge=RecordingBridge(), snapshot=snapshot, key="x")
        service.previous = object()
        service.pending_confirm = (object(), 0, 0, "x", None, "A")
        service.pending_confirm_created = service._clock()
        service.reader = type("Reader", (), {"read": staticmethod(lambda: (changed, 1))})()
        service.refresh()
        self.assertIsNone(service.previous)
        self.assertIsNone(service.pending_confirm)

    def test_english_target_gate_rejects_unknown_names_and_accepts_selection_references(self) -> None:
        snapshot = _snapshot_with_song()
        for text in ("turn Reverb off on Ghost", "Ghost mute", "lower Ghost by 3 dB", "mute Ghost", "mute track 99"):
            intent = parse_local(text, snapshot)
            self.assertIsNone(intent.track, text)
            self.assertGreaterEqual(intent.track_stated, 0.8, text)
        for text in ("mute", "mute it", "solo this", "mute this track", "lower it by 3 dB", "pan left", "set send A to 100%", "arm for recording", "mute the track", "mute it now", "can you mute this", "please mute"):
            intent = parse_local(text, snapshot)
            self.assertIsNotNone(intent, text)
            self.assertFalse(intent.track is None and intent.track_stated >= 0.8, text)
        for text in ("mute Bass Synth", "pan it right on Ghost"):
            intent = parse_local(text, snapshot)
            self.assertIsNone(intent.track, text)
            self.assertGreaterEqual(intent.track_stated, 0.8, text)
        from intent import extract_plugin_request
        self.assertIsNone(extract_plugin_request("mute the track", snapshot))

    def test_relative_pan_modifier_is_a_step_not_absolute_position(self) -> None:
        relative = parse_local("pan it slightly to the right", _snapshot_with_song())
        self.assertEqual((relative.action, relative.step, relative.number), (Action.PAN, Step.UP_SMALL, None))
        for text, value in (("pan hard right", 50.0), ("pan right 50%", 50.0), ("pan center", 0.0)):
            intent = parse_local(text, _snapshot_with_song())
            self.assertEqual((intent.step, intent.number.value), (Step.SET, value), text)

    def test_named_missing_owner_candidates_do_not_supply_a_track(self) -> None:
        from intent import interpret_response
        from tests.support import choice, response
        snapshot = _snapshot_with_song()
        device = response("device_off", "none", track_conf=0.67)
        device["answers"]["track_stated"] = choice("named", 0.77, named=0.77, absent=0.23)
        device["answers"]["device_t0"] = choice("d0", 0.95)
        parsed = interpret_response(snapshot, "Ghostのリバーブをオフにして", device).intent
        self.assertIsNone(parsed.track)

        parameter = response("param", "none", "set", track_conf=0.67, param="d0p0", param_conf=0.95)
        parameter["answers"]["track_stated"] = choice("named", 0.77, named=0.77, absent=0.23)
        parsed = interpret_response(snapshot, "GhostのリバーブのDry/Wetを50%にして", parameter).intent
        self.assertIsNone(parsed.track)

    def test_setting_only_utterances_can_only_lower_track_stated(self) -> None:
        from intent import interpret_response, lower_setting_only_track_stated
        from tests.support import response
        for text in ("パンを真ん中に", "センドBを少し上げて", "音量を-6dBにして", "ミュートして", "set send A to 100%"):
            self.assertEqual(lower_setting_only_track_stated(text, 0.83), 0.0, text)
        for text in ("Ghostをミュート", "ボーカルのパンを真ん中に", "mute the vocals"):
            self.assertEqual(lower_setting_only_track_stated(text, 0.83), 0.83, text)
        self.assertEqual(lower_setting_only_track_stated("パンを真ん中に", 0.0), 0.0)
        outlier = response("pan", "t0", "set", track_conf=0.9)
        outlier["answers"]["track_stated"] = {"type": "noul", "noul": 0.83}
        self.assertEqual(interpret_response(_snapshot_with_song(), "パンを真ん中に", outlier).intent.track_stated, 0.0)

    def test_named_track_thresholds_use_shared_constants(self) -> None:
        from pathlib import Path
        root = Path(__file__).parents[1]
        daemon = (root / "daemon.py").read_text()
        english = (root / "intent_en.py").read_text()
        self.assertNotRegex(daemon, r"track_stated\s*(?:>=|<)\s*(?:0\.8|0\.5)")
        self.assertNotRegex(english, r"track_stated\s*(?:>=|<)\s*(?:0\.8|0\.5)")
        self.assertIn("intent.track_conf < NAMED_TRACK_CONF_MIN", daemon)

    def test_clip_note_factory_keeps_missing_target_and_modifiers_are_not_targets(self) -> None:
        from intent import parse_clip_notes_phrase
        snapshot = _snapshot_with_song()
        for text in ("transpose Ghost clip 1 up an octave", "increase velocity on Ghost clip 1", "Ghostのクリップ1のノートを1オクターブ上げて", "Ghostのクリップ1のベロシティを80にして"):
            request = parse_clip_notes_phrase(text, snapshot)
            self.assertTrue(request.target_stated, text)
            self.assertTrue(request.target_missing, text)
        for text in ("quantize to 1/8", "quantize lightly", "quantize the notes", "quantize the clip", "quantize this", "quantize to 1/16 50%", "1/8でクオンタイズ", "軽くクオンタイズ", "ノートをクオンタイズ"):
            self.assertFalse(parse_clip_notes_phrase(text, snapshot).target_missing, text)

    def test_confirmation_id_must_match_and_targeted_cancel_preserves_proposal(self) -> None:
        import daemon as D
        service = LiveJevService(bridge=RecordingBridge(), snapshot=_snapshot_with_song(), key="x", requester=lambda *_: (_ for _ in ()).throw(AssertionError("Jev")))
        with unittest.mock.patch.object(D, "REQUIRE_CONFIRM", True):
            self.assertEqual(service.process({"id": "A", "text": "record"})["kind"], "confirm")
            service.process({"id": "cancel", "cmd": "cancel_pending", "target": "old"})
            self.assertIsNotNone(service.pending_confirm)
            self.assertEqual(service.process({"id": "old", "confirm": True})["kind"], "info")
            self.assertIsNotNone(service.pending_confirm)

    def test_operation_words_are_valid_rename_destinations(self) -> None:
        service = LiveJevService(bridge=RecordingBridge(), snapshot=_snapshot_with_song(), key="x")
        for text in ("rename Bass to Play", "rename Bass to Stop", "Bassの名前をミュートにして", 'rename Bass to "Stop"'):
            self.assertFalse(service._has_compound_local_operations(text), text)

    def test_swift_proposal_and_undo_regressions_are_encoded(self) -> None:
        from pathlib import Path
        root = Path(__file__).parents[1] / "LiveJev/Sources/LiveJev"
        view_model = (root / "ViewModel.swift").read_text()
        panel = (root / "Panel.swift").read_text()
        messages = (root / "Messages.swift").read_text()
        self.assertIn("systemUptime", view_model)
        self.assertNotIn("case let .info(message):\n            canUndoLastSuccess = false", view_model)
        self.assertIn("outstandingHiddenRequestIDs.remove(requestID)", panel)
        self.assertIn("systemUptime - proposal.createdAt", panel)
        self.assertIn("case cancelPending(id: String, target: String? = nil)", messages)
        self.assertIn("guard pendingUndoID == nil, canUndoLastSuccess else { return }", view_model)
        self.assertIn("canUndoLastSuccess = false\n            onChange?()", view_model)
        self.assertIn("private func finishPendingUndo", view_model)
        self.assertIn("_ = finishPendingUndo(id: message.id)", view_model)
        self.assertIn("undoButton.isEnabled = viewModel.canUndoLastSuccess", panel)
