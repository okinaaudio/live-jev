from __future__ import annotations

from dataclasses import replace
import unittest
from unittest import mock

from actions import ACTIONS
from daemon import LiveJevService
from intent import (
    ACTION_CRITERIA,
    Action,
    Number,
    Step,
    _local_intent,
    build_request,
    detect_language,
    extract_plugin_request,
    is_negated,
    parse_clip_notes_phrase,
    parse_local,
)
from messages import ACTION_LABELS as MESSAGE_ACTION_LABELS, MESSAGES, STEP_LABELS, contains_japanese, using_language
from snapshot import Clip, Scene
from tests.support import sample_snapshot
from tests.test_expansion import RecordingBridge


def english_snapshot():
    base = sample_snapshot()
    tracks = list(base.tracks)
    tracks[0] = replace(tracks[0], clips=(Clip(0, "Pad Loop", "live_set tracks 0 clip_slots 0 clip", {"looping": True, "warping": True, "pitch_coarse": 2, "gain": 0.5}),), sends=(0.25, 0.5))
    tracks[1] = replace(tracks[1], clips=(Clip(0, "Bass Loop", "live_set tracks 1 clip_slots 0 clip", {"looping": False, "warping": False, "pitch_coarse": 0, "gain": 0.4}),), sends=(0.1, 0.2))
    return replace(
        base,
        tracks=tuple(tracks),
        scenes=(Scene(0, "Intro", "live_set scenes 0"), Scene(1, "Verse", "live_set scenes 1")),
        returns=("Verb", "Delay"),
        song={"signature_numerator": 4, "current_song_time": 64.0, "loop": True, "metronome": True, "session_record": True, "overdub": True},
    )


LOCAL_CASES = {
    "play": Action.PLAY,
    "please play": Action.PLAY,
    "can you start": Action.PLAY,
    "start playback now": Action.PLAY,
    "stop": Action.STOP,
    "please stop": Action.STOP,
    "would you stop playback for me": Action.STOP,
    "continue": Action.CONTINUE,
    "resume": Action.CONTINUE,
    "continue playback": Action.CONTINUE,
    "record": Action.RECORD_ON,
    "start recording": Action.RECORD_ON,
    "record on": Action.RECORD_ON,
    "stop recording": Action.RECORD_OFF,
    "record off": Action.RECORD_OFF,
    "overdub": Action.OVERDUB_ON,
    "turn overdub on": Action.OVERDUB_ON,
    "overdub off": Action.OVERDUB_OFF,
    "disable overdub": Action.OVERDUB_OFF,
    "loop": Action.LOOP_ON,
    "turn loop on": Action.LOOP_ON,
    "loop off": Action.LOOP_OFF,
    "disable loop": Action.LOOP_OFF,
    "metronome": Action.METRONOME_ON,
    "turn the click on": Action.METRONOME_ON,
    "click off": Action.METRONOME_OFF,
    "turn metronome off": Action.METRONOME_OFF,
    "undo": Action.UNDO,
    "redo": Action.REDO,
    "capture midi": Action.CAPTURE_MIDI,
    "tap tempo": Action.TAP_TEMPO,
    "stop all clips": Action.STOP_ALL_CLIPS,
    "mute Bass": Action.MUTE,
    "Bass mute": Action.MUTE,
    "mute the selected track": Action.MUTE,
    "unmute Bass": Action.UNMUTE,
    "Bass mute off": Action.UNMUTE,
    "solo Bass": Action.SOLO,
    "unsolo Bass": Action.UNSOLO,
    "arm Bass": Action.ARM,
    "record arm Bass": Action.ARM,
    "disarm Bass": Action.DISARM,
    "fold Bass": Action.FOLD,
    "expand Bass": Action.UNFOLD,
    "monitor Bass in": Action.MONITOR_IN,
    "monitor Bass auto": Action.MONITOR_AUTO,
    "monitor Bass off": Action.MONITOR_OFF,
    "stop Bass clips": Action.TRACK_STOP_CLIPS,
    "tempo 120": Action.TEMPO,
    "set tempo to 128": Action.TEMPO,
    "90 bpm": Action.TEMPO,
    "go to bar 17": Action.JUMP_TO_BAR,
    "jump to bar 3": Action.JUMP_TO_BAR,
    "go to the start": Action.JUMP_TO_BAR,
    "turn Bass down by 3 dB": Action.VOLUME,
    "Bass 3 dB down": Action.VOLUME,
    "set Bass volume to -6 dB": Action.VOLUME,
    "make Bass louder": Action.VOLUME,
    "lower this track a bit": Action.VOLUME,
    "pan Bass left 20": Action.PAN,
    "pan Bass 20 right": Action.PAN,
    "center Bass": Action.PAN,
    "raise Bass send A a little": Action.SEND,
    "set Bass send B to 50%": Action.SEND,
    "rename Bass to Low End": Action.RENAME,
    "call Bass Sub": Action.RENAME,
    "create a new midi track": Action.ADD_MIDI_TRACK,
    "make another track": Action.ADD_MIDI_TRACK,
    "add an audio track": Action.ADD_AUDIO_TRACK,
    "create a new midi track named Lead": Action.ADD_MIDI_TRACK,
    "add Operator on a new track": Action.ADD_TRACK_WITH_DEVICE,
    "create a new midi track with Wavetable": Action.ADD_TRACK_WITH_DEVICE,
    "turn Reverb off": Action.DEVICE_OFF,
    "enable Reverb": Action.DEVICE_ON,
    "Bass clip 1 launch": Action.LAUNCH_CLIP,
    "stop Bass clip 1": Action.STOP_CLIP,
    "Bass clip 1 loop on": Action.CLIP_LOOP_ON,
    "Bass clip 1 loop off": Action.CLIP_LOOP_OFF,
    "Bass clip 1 warp on": Action.CLIP_WARP_ON,
    "Bass clip 1 warp off": Action.CLIP_WARP_OFF,
    "Bass clip 1 pitch up 2 semitones": Action.CLIP_PITCH,
    "Bass clip 1 gain up": Action.CLIP_GAIN,
    "launch scene 1": Action.LAUNCH_SCENE,
    "fire scene 2": Action.LAUNCH_SCENE,
}


NOTE_CASES = {
    "quantize": ("quantize", "1/16"),
    "quantize to 1/32": ("quantize", "1/32"),
    "quantize to sixteenths": ("quantize", "1/16"),
    "quantize to eighths": ("quantize", "1/8"),
    "quantize to quarter notes": ("quantize", "1/4"),
    "quantize to eighth triplets": ("quantize", "1/8t"),
    "quantize to 16th triplets": ("quantize", "1/16t"),
    "quantize lightly": ("quantize", "1/16"),
    "quantize loosely": ("quantize", "1/16"),
    "make it legato": ("legato", None),
    "connect the notes": ("legato", None),
    "fill the gaps": ("legato", None),
    "transpose notes up 2 octaves": ("transpose", 24),
    "shift midi down 3 semitones": ("transpose", -3),
    "move notes up 4 half steps": ("transpose", 4),
    "pitch the clip down 2 st": ("transpose", -2),
    "velocity to 100": ("velocity", 100.0),
    "velocity up": ("velocity", 1.25),
    "velocity down": ("velocity", 0.8),
    "louder notes": ("velocity", 1.25),
    "softer notes a bit": ("velocity", 0.9),
    "harder notes a little": ("velocity", 1.1),
    "double the loop": ("duplicate_loop", None),
    "duplicate loop": ("duplicate_loop", None),
    "twice as long": ("duplicate_loop", None),
    "Bass clip 1 quantize to 16th": ("quantize", "1/16"),
}


PLUGIN_CASES = {
    "insert Omnisphere on Bass": (Action.INSERT_PLUGIN, "omnisphere", 1),
    "add Serum to Bass": (Action.INSERT_PLUGIN, "serum", 1),
    "load Diva": (Action.INSERT_PLUGIN, "diva", None),
    "open Kontakt": (Action.INSERT_PLUGIN, "kontakt", None),
    "put Serum on the selected track": (Action.INSERT_PLUGIN, "serum", "selected"),
    "drop ValhallaVintageVerb onto Bass": (Action.INSERT_PLUGIN, "valhallavintageverb", 1),
    "throw Saturn on Bass": (Action.INSERT_PLUGIN, "saturn", 1),
    "place Pro-Q 3 on Bass": (Action.INSERT_PLUGIN, "pro-q 3", 1),
    "bring up Diva": (Action.INSERT_PLUGIN, "diva", None),
    "fire up Omnisphere": (Action.INSERT_PLUGIN, "omnisphere", None),
    "pull up Kontakt": (Action.INSERT_PLUGIN, "kontakt", None),
    "use Serum": (Action.INSERT_PLUGIN, "serum", None),
    "apply Saturn on Bass": (Action.INSERT_PLUGIN, "saturn", 1),
    "stick Diva on Bass": (Action.INSERT_PLUGIN, "diva", 1),
    "slap reverb on Bass": (Action.INSERT_PLUGIN, "reverb", 1),
    "add Omnisphere on a new track": (Action.ADD_TRACK_WITH_PLUGIN, "omnisphere", None),
    "create a new midi track with Serum": (Action.ADD_TRACK_WITH_PLUGIN, "serum", None),
    "make another track with Diva": (Action.ADD_TRACK_WITH_PLUGIN, "diva", None),
    "new audio track with ValhallaVintageVerb": (Action.ADD_TRACK_WITH_PLUGIN, "valhallavintageverb", None),
    "Omnisphere on a new track": (Action.ADD_TRACK_WITH_PLUGIN, "omnisphere", None),
    "track with Kontakt": (Action.ADD_TRACK_WITH_PLUGIN, "kontakt", None),
    "Omnisphere track": (Action.ADD_TRACK_WITH_PLUGIN, "omnisphere", None),
}


class EnglishIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = english_snapshot()

    def test_language_detection_and_mixed_input(self) -> None:
        self.assertEqual(detect_language("mute Bass"), "en")
        self.assertEqual(detect_language("Bassをmuteして"), "ja")
        self.assertIs(parse_local("Bassをmuteして", self.snapshot).action, Action.MUTE)

    def test_local_phrase_matrix(self) -> None:
        self.assertGreaterEqual(len(LOCAL_CASES), 80)
        for phrase, expected in LOCAL_CASES.items():
            with self.subTest(phrase=phrase):
                intent = parse_local(phrase, self.snapshot)
                self.assertIsNotNone(intent, phrase)
                self.assertIs(intent.action, expected, phrase)

    def test_note_phrase_matrix(self) -> None:
        for phrase, (op, expected) in NOTE_CASES.items():
            with self.subTest(phrase=phrase):
                request = parse_clip_notes_phrase(phrase, self.snapshot)
                self.assertIsNotNone(request, phrase)
                self.assertEqual(request.op, op)
                if isinstance(expected, str):
                    self.assertEqual(request.grid, expected)
                elif op == "transpose":
                    self.assertEqual(request.semitones, expected)
                elif op == "velocity" and expected is not None:
                    self.assertIn(expected, {request.value, request.factor})

    def test_plugin_phrase_matrix(self) -> None:
        for phrase, expected in PLUGIN_CASES.items():
            with self.subTest(phrase=phrase):
                request = extract_plugin_request(phrase, self.snapshot)
                self.assertIsNotNone(request, phrase)
                self.assertEqual((request.action, request.raw_name.casefold(), request.track), expected)

    def test_negations_never_execute_locally(self) -> None:
        phrases = (
            "don't mute Bass", "do not stop", "never arm Bass", "no need to quantize",
            "quantize is not necessary", "without quantizing", "stop short of muting Bass",
            "skip the velocity change", "don't insert Serum on Bass", "do not add a new track with Diva",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                self.assertTrue(is_negated(phrase))
                self.assertIsNone(parse_local(phrase, self.snapshot))
                self.assertIsNone(parse_clip_notes_phrase(phrase, self.snapshot))
                self.assertIsNone(extract_plugin_request(phrase, self.snapshot))

    def test_jev_instructions_are_bilingual(self) -> None:
        self.assertTrue(all(" / " in value for value in ACTION_CRITERIA.values()))
        questions = build_request(self.snapshot, "mute Bass")["questions"]
        self.assertTrue(all(" / " in question["instructions"] for question in questions.values()))


class EnglishOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = english_snapshot()

    def test_every_action_readback_is_english(self) -> None:
        device = self.snapshot.tracks[0].devices[0]
        param = device.params[0]
        for action, spec in ACTIONS.items():
            intent = _local_intent(action, track=0, step=Step.UP_SMALL, number=Number(1, "raw"), scene=0, clip=0, text="Lead", send=0, native_device="Operator", plugin="Omnisphere")
            intent = replace(intent, param=param, param_conf=1.0, device=device, device_conf=1.0)
            with self.subTest(action=action), using_language("en"):
                line = spec.readback(self.snapshot, intent)
                self.assertFalse(contains_japanese(line), line)

    def test_daemon_language_command_and_visible_errors(self) -> None:
        service = LiveJevService(snapshot=self.snapshot, key=None)
        status = service.process({"id": "1", "cmd": "lang", "value": "en"})
        self.assertEqual(status["line"], "Live 3 tracks / 120 BPM")
        empty = service.process({"id": "2", "text": ""})
        self.assertEqual(empty["line"], "Enter a command.")
        self.assertFalse(contains_japanese(empty["line"]))

    def test_daemon_english_result_confirmation_and_guidance(self) -> None:
        import daemon as daemon_module

        service = LiveJevService(bridge=RecordingBridge(), snapshot=self.snapshot, key="x", requester=lambda *_: self.fail("Jev was called"))
        service.process({"cmd": "lang", "value": "en"})
        result = service.process({"id": "1", "text": "mute Bass"})
        self.assertEqual(result["kind"], "result")
        self.assertEqual(result["line"], "Mute On")
        self.assertEqual(result["decision"]["action_label"], "Mute")
        mixed = service.process({"id": "mixed", "text": "Bassをmuteして"})
        self.assertFalse(contains_japanese(mixed["line"]))
        with mock.patch.object(daemon_module, "REQUIRE_CONFIRM", True):
            confirmation = service.process({"id": "2", "text": "record"})
        self.assertEqual(confirmation["options"], ["Yes", "Cancel"])
        self.assertFalse(contains_japanese(confirmation["line"]))
        guidance = service.process({"id": "3", "text": "slap reverb on Bass"})
        self.assertEqual(guidance["kind"], "info")
        self.assertFalse(contains_japanese(guidance["line"]))

    def test_every_catalogued_english_message_has_no_japanese(self) -> None:
        for catalog in (MESSAGES, MESSAGE_ACTION_LABELS, STEP_LABELS):
            for key, translations in catalog.items():
                with self.subTest(key=key):
                    self.assertFalse(contains_japanese(translations["en"]), translations["en"])


class EnglishPhraseCases(unittest.TestCase):
    pass


def _local_case(phrase: str, expected: Action):
    def test(self) -> None:
        intent = parse_local(phrase, english_snapshot())
        self.assertIsNotNone(intent, phrase)
        self.assertIs(intent.action, expected, phrase)
    return test


for _index, (_phrase, _expected) in enumerate(LOCAL_CASES.items(), start=1):
    setattr(EnglishPhraseCases, f"test_phrase_{_index:03d}", _local_case(_phrase, _expected))


if __name__ == "__main__":
    unittest.main()


class EnglishInsertVocabularyTests(unittest.TestCase):
    def test_many_verbs_and_request_forms_mean_insert(self) -> None:
        from intent import Action, extract_plugin_request
        from tests.support import sample_snapshot
        snapshot = sample_snapshot()
        verbs = ["insert", "add", "load", "open", "put in", "put", "drop in", "drop", "throw on", "throw in", "place", "bring up", "fire up",
                 "launch", "pull up", "use", "apply", "stick", "slap on", "give me", "i need", "i want", "get me", "can i get", "let's use",
                 "let me have", "pop in", "chuck in", "whack on", "set up", "spin up", "call up", "summon", "instantiate", "mount", "attach",
                 "plug in", "hook up", "bring in", "try", "load up", "open up"]
        for verb in verbs:
            request = extract_plugin_request(f"{verb} Serum 2", snapshot)
            self.assertIsNotNone(request, verb)
            self.assertEqual((request.action, request.raw_name.casefold()), (Action.INSERT_PLUGIN, "serum 2"), verb)
        for text in ("Could you open up Serum 2 for me?", "Serum 2 on this track", "chuck Serum 2 on the Bass", "put Serum 2 on Bass, please"):
            request = extract_plugin_request(text, snapshot)
            self.assertIsNotNone(request, text)
            self.assertEqual(request.raw_name.casefold(), "serum 2", text)

    def test_new_track_forms(self) -> None:
        from intent import Action, extract_plugin_request
        from tests.support import sample_snapshot
        snapshot = sample_snapshot()
        for text in ("give me a new track with Serum 2", "I'd like Serum 2 on a fresh track", "create an audio track with Serum 2",
                     "put Serum 2 on its own track", "another track with Serum 2 please", "make a new track and load Serum 2", "Serum 2 on a separate track"):
            request = extract_plugin_request(text, snapshot)
            self.assertIsNotNone(request, text)
            self.assertEqual((request.action, request.raw_name.casefold()), (Action.ADD_TRACK_WITH_PLUGIN, "serum 2"), text)

    def test_other_commands_are_not_plugin_requests(self) -> None:
        from intent import extract_plugin_request
        from tests.support import sample_snapshot
        snapshot = sample_snapshot()
        for text in ("volume on Bass", "mute on Bass", "set tempo to 120", "start recording", "launch scene 2", "turn Bass up a bit", "don't add Serum 2"):
            self.assertIsNone(extract_plugin_request(text, snapshot), text)
