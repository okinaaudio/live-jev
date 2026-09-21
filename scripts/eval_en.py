#!/usr/bin/env python3.13
"""Manual 40-case Jev evaluation for English Live commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daemon import read_key, request_jev
from intent import Action, build_request, interpret_response
from snapshot import Device, Param, Scene, Snapshot, Track


@dataclass(frozen=True)
class Case:
    utterance: str
    action: Action
    track: int | str | None


CASES = (
    Case("turn the drums down a bit", Action.VOLUME, 2),
    Case("set Bass to -6 dB", Action.VOLUME, 1),
    Case("pan Pad left 20", Action.PAN, 0),
    Case("center the Bass track", Action.PAN, 1),
    Case("mute Drums", Action.MUTE, 2),
    Case("unmute the current track", Action.UNMUTE, "selected"),
    Case("solo Bass", Action.SOLO, 1),
    Case("take Pad out of solo", Action.UNSOLO, 0),
    Case("set tempo to 124", Action.TEMPO, None),
    Case("play", Action.PLAY, None),
    Case("stop playback", Action.STOP, None),
    Case("resume from here", Action.CONTINUE, None),
    Case("start recording", Action.RECORD_ON, None),
    Case("stop recording", Action.RECORD_OFF, None),
    Case("turn overdub on", Action.OVERDUB_ON, None),
    Case("disable overdub", Action.OVERDUB_OFF, None),
    Case("turn the loop on", Action.LOOP_ON, None),
    Case("turn the click off", Action.METRONOME_OFF, None),
    Case("undo that", Action.UNDO, None),
    Case("redo", Action.REDO, None),
    Case("capture midi", Action.CAPTURE_MIDI, None),
    Case("tap tempo", Action.TAP_TEMPO, None),
    Case("stop all clips", Action.STOP_ALL_CLIPS, None),
    Case("jump to bar 17", Action.JUMP_TO_BAR, None),
    Case("arm Bass", Action.ARM, 1),
    Case("disarm Drums", Action.DISARM, 2),
    Case("monitor Pad in", Action.MONITOR_IN, 0),
    Case("set Bass monitoring to auto", Action.MONITOR_AUTO, 1),
    Case("fold the Pad group", Action.FOLD, 0),
    Case("stop the clips on Bass", Action.TRACK_STOP_CLIPS, 1),
    Case("launch Bass Loop", Action.LAUNCH_CLIP, 1),
    Case("stop Bass clip 1", Action.STOP_CLIP, 1),
    Case("launch scene 2", Action.LAUNCH_SCENE, None),
    Case("bypass Reverb on Pad", Action.DEVICE_OFF, 0),
    Case("turn Reverb back on", Action.DEVICE_ON, 0),
    Case("raise Bass send A a touch", Action.SEND, 1),
    Case("rename Bass to Sub", Action.RENAME, 1),
    Case("add a MIDI track", Action.ADD_MIDI_TRACK, None),
    Case("add an audio track", Action.ADD_AUDIO_TRACK, None),
    Case("add a new track with Operator", Action.ADD_TRACK_WITH_DEVICE, None),
)


def snapshot() -> Snapshot:
    dry_wet = Param(0, "Dry/Wet", 0.25, 0.0, 1.0, "25%", "live_set tracks 0 devices 0 parameters 0")
    reverb = Device(0, "Reverb", (dry_wet,), "live_set tracks 0 devices 0")
    return Snapshot(
        tracks=(
            Track(0, "Pad", 0.60, "-6.0 dB", 0.0, "C", False, False, (reverb,), "live_set tracks 0"),
            Track(1, "Bass", 0.55, "-8.0 dB", 0.0, "C", False, False, (), "live_set tracks 1"),
            Track(2, "Drums", 0.70, "-3.0 dB", 0.0, "C", False, False, (), "live_set tracks 2"),
        ),
        master_volume=0.75,
        master_display="-1.0 dB",
        tempo=120.0,
        playing=True,
        scenes=(Scene(0, "Intro", "live_set scenes 0"), Scene(1, "Verse", "live_set scenes 1")),
        returns=("Reverb", "Delay"),
        song={"signature_numerator": 4},
        taken_at=time.time(),
    )


def main() -> int:
    key = read_key()
    if not key:
        print("TYPESAFE_API_KEY was not found", file=sys.stderr)
        return 2
    current = snapshot()
    passed = 0
    for case in CASES:
        payload = build_request(current, case.utterance)
        response = request_jev(payload, key)
        intent = interpret_response(current, case.utterance, response, payload["state"].get("detail_tracks", ())).intent
        ok = intent.action is case.action and intent.track == case.track
        passed += int(ok)
        print(f"{'PASS' if ok else 'FAIL'}\t{case.utterance}\t{intent.action.value}\t{intent.track}")
    print(f"{passed}/{len(CASES)} ({passed / len(CASES):.0%})")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
