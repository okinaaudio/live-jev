"""Map allowed actions to live.py arguments."""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import json
from typing import Any, Callable

from intent import Action, Intent, Step
from messages import LocalizedError, render
from snapshot import Snapshot, Track


Apply = Callable[[Snapshot, Intent], list[list[str]]]
Readback = Callable[[Snapshot, Intent], str]


@dataclass(frozen=True)
class ActionSpec:
    needs_track: bool
    needs_param: bool
    needs_step: bool
    apply: Apply
    readback: Readback
    kind: str = "custom"
    prop: str | None = None
    confirm: bool = False
    readback_event: tuple[str, str | None] = ("api_get", None)
    needs_scene: bool = False
    needs_clip: bool = False
    needs_device: bool = False
    needs_send: bool = False


_IDS = itertools.count(1)


def request_id(kind: str) -> str:
    return f"{next(_IDS)}-{kind}"


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _track(snapshot: Snapshot, intent: Intent) -> Track:
    if not isinstance(intent.track, int):
        raise LocalizedError("error.track_required")
    found = next((track for track in snapshot.tracks if track.index == intent.track), None)
    if found is None:
        raise LocalizedError("error.track_missing")
    return found


def _mixer_target(snapshot: Snapshot, intent: Intent, parameter: str) -> tuple[str, str, float]:
    if intent.track == "master":
        if parameter != "volume":
            raise LocalizedError("error.master_volume_only")
        return "live_set master_track mixer_device volume", "master", snapshot.master_volume
    track = _track(snapshot, intent)
    value = track.volume if parameter == "volume" else track.pan
    return f"{track.path} mixer_device {parameter}", str(track.index), value


def _step_delta(step: Step, small: float, big: float) -> float:
    return {
        Step.UP_SMALL: small,
        Step.UP_BIG: big,
        Step.DOWN_SMALL: -small,
        Step.DOWN_BIG: -big,
    }.get(step, 0.0)


def _parameter_batches(path: str, value: float, read_flag: str, read_target: str) -> list[list[str]]:
    return [
        ["--write", "--api-parameter-set", path, json.dumps(value), request_id("set")],
        [read_flag, read_target, request_id("read")],
    ]


def _mixer_batches(path: str, target: str, value: float) -> list[list[str]]:
    return [
        ["--write", "--api-parameter-set", path, json.dumps(value), request_id("set")],
        ["--api-mixer-status", target, request_id("read")],
        ["--write", "--api-call", path, "str_for_value", json.dumps([value]), request_id("display")],
    ]


def apply_volume(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    path, target, current = _mixer_target(snapshot, intent, "volume")
    if intent.number and intent.number.unit == "raw":
        value = _clamp(intent.number.value, 0.0, 1.0)
    elif intent.number:
        raise LocalizedError("error.db_internal")
    else:
        value = _clamp(current + _step_delta(intent.step, 0.03, 0.10), 0.0, 1.0)
    return _mixer_batches(path, target, value)


def apply_pan(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    path, target, current = _mixer_target(snapshot, intent, "panning")
    if intent.number and intent.number.unit == "raw":
        value = _clamp(intent.number.value, -1.0, 1.0)
    elif intent.number and intent.number.unit == "percent":
        value = _clamp(intent.number.value / 100.0, -1.0, 1.0)
    elif intent.number and intent.number.unit == "pan":
        value = _clamp(intent.number.value / 50.0, -1.0, 1.0)
    elif intent.number:
        raise LocalizedError("error.pan_unit")
    else:
        value = _clamp(current + _step_delta(intent.step, 0.10, 0.30), -1.0, 1.0)
    return _mixer_batches(path, target, value)


def _apply_track_bool(property_name: str, value: bool) -> Apply:
    def apply(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
        track = _track(snapshot, intent)
        write_id = request_id("set")
        read_id = request_id("read")
        return [
            ["--write", "--api-set", track.path, property_name, "1" if value else "0", write_id],
            ["--api-get", track.path, property_name, read_id],
        ]
    return apply


def apply_tempo(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    value = intent.number.value if intent.number else snapshot.tempo + _step_delta(intent.step, 2.0, 10.0)
    value = _clamp(value, 20.0, 999.0)
    return [
        ["--write", "--tempo", f"{value:g}"],
        ["--api-get", "live_set", "tempo", request_id("tempo")],
    ]


def _apply_transport(method: str) -> Apply:
    def apply(_snapshot: Snapshot, _intent: Intent) -> list[list[str]]:
        return [
            ["--write", "--api-call", "live_set", method, "[]", request_id("transport")],
            ["--api-get", "live_set", "is_playing", request_id("playing")],
        ]
    return apply


def apply_param(_snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    if intent.param is None:
        raise LocalizedError("error.param_required")
    parameter = intent.param
    if intent.number:
        if intent.number.unit == "percent":
            value = parameter.min + (parameter.max - parameter.min) * intent.number.value / 100.0
        else:
            value = intent.number.value
    else:
        value = parameter.value + (parameter.max - parameter.min) * _step_delta(intent.step, 0.05, 0.15)
    value = _clamp(value, parameter.min, parameter.max)
    return _parameter_batches(parameter.path, value, "--api-device-parameters", parameter.path.rsplit(" parameters ", 1)[0])


def _track_name(snapshot: Snapshot, intent: Intent) -> str:
    if intent.track == "master":
        return render("label.master")
    return _track(snapshot, intent).name


def read_volume(snapshot: Snapshot, intent: Intent) -> str:
    shown = snapshot.master_display if intent.track == "master" else _track(snapshot, intent).volume_display
    return render("readback.track_value", track=_track_name(snapshot, intent), label=render("label.volume"), value=shown)


def read_pan(snapshot: Snapshot, intent: Intent) -> str:
    return render("readback.track_value", track=_track_name(snapshot, intent), label=render("label.pan"), value=_track(snapshot, intent).pan_display)


def _read_bool(label: str, property_name: str) -> Readback:
    def read(snapshot: Snapshot, intent: Intent) -> str:
        value = getattr(_track(snapshot, intent), property_name)
        state = render("state.on" if value else "state.off")
        return render("readback.track_state", track=_track_name(snapshot, intent), label=render(label), state=state)
    return read


def read_tempo(snapshot: Snapshot, _intent: Intent) -> str:
    return render("readback.tempo", tempo=snapshot.tempo)


def read_playing(snapshot: Snapshot, _intent: Intent) -> str:
    return render("state.playing" if snapshot.playing else "state.stopped")


def read_param(snapshot: Snapshot, intent: Intent) -> str:
    if intent.param is None:
        return render("readback.param_missing")
    for track in snapshot.tracks:
        for device in track.devices:
            for parameter in device.params:
                if parameter.path == intent.param.path:
                    return render("readback.param", track=track.name, device=device.name, param=parameter.name, value=parameter.display)
    return render("readback.param_missing")


def _unused(_snapshot: Snapshot, _intent: Intent) -> list[list[str]]:
    raise LocalizedError("error.no_action")


def _none_readback(_snapshot: Snapshot, _intent: Intent) -> str:
    return ""


MONITOR_NAMES = {0: "In", 1: "Auto", 2: "Off"}


def _apply_song_bool(property_name: str, value: bool) -> Apply:
    def apply(_snapshot: Snapshot, _intent: Intent) -> list[list[str]]:
        return [
            ["--write", "--api-set", "live_set", property_name, "1" if value else "0", request_id("set")],
            ["--api-get", "live_set", property_name, request_id("read")],
        ]
    return apply


def _apply_song_call(method: str) -> Apply:
    def apply(_snapshot: Snapshot, _intent: Intent) -> list[list[str]]:
        return [
            ["--write", "--api-call", "live_set", method, "[]", request_id("call")],
            ["--api-get", "live_set", "is_playing", request_id("playing")],
        ]
    return apply


def _apply_track_int(property_name: str, value: int) -> Apply:
    def apply(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
        track = _track(snapshot, intent)
        return [
            ["--write", "--api-set", track.path, property_name, str(value), request_id("set")],
            ["--api-get", track.path, property_name, request_id("read")],
        ]
    return apply


def _apply_track_call(method: str) -> Apply:
    def apply(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
        track = _track(snapshot, intent)
        return [
            ["--write", "--api-call", track.path, method, "[]", request_id("call")],
            ["--api-get", "live_set", "is_playing", request_id("playing")],
        ]
    return apply


def bar_to_beats(snapshot: Snapshot, bar: float) -> float:
    numerator = float(snapshot.song.get("signature_numerator", 4) or 4)
    return max(0.0, (bar - 1.0) * numerator)


def beats_to_bar(snapshot: Snapshot, beats: float) -> int:
    numerator = float(snapshot.song.get("signature_numerator", 4) or 4)
    return int(beats // numerator) + 1


def apply_jump(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    if intent.number is None:
        raise LocalizedError("error.bar_required")
    beats = bar_to_beats(snapshot, intent.number.value)
    return [
        ["--write", "--api-set", "live_set", "current_song_time", f"{beats:g}", request_id("set")],
        ["--api-get", "live_set", "current_song_time", request_id("read")],
    ]


def _read_song_bool(label: str, property_name: str) -> Readback:
    def read(snapshot: Snapshot, _intent: Intent) -> str:
        state = render("state.on" if snapshot.song.get(property_name) else "state.off")
        return render("readback.song_state", label=render(label), state=state)
    return read


def _read_track_int(label: str, property_name: str, names: dict[int, str]) -> Readback:
    def read(snapshot: Snapshot, intent: Intent) -> str:
        value = int(getattr(_track(snapshot, intent), property_name))
        return render("readback.track_value", track=_track_name(snapshot, intent), label=render(label), value=names.get(value, value))
    return read


def _read_text(key: str) -> Readback:
    def read(_snapshot: Snapshot, _intent: Intent) -> str:
        return render(key)
    return read


def read_position(snapshot: Snapshot, _intent: Intent) -> str:
    beats = float(snapshot.song.get("current_song_time", 0.0) or 0.0)
    return render("readback.position", bar=beats_to_bar(snapshot, beats))


def _track_bool(prop: str, value: bool, label: str) -> ActionSpec:
    return ActionSpec(True, False, False, _apply_track_bool(prop, value), _read_bool(label, prop),
                      kind="track_bool", prop=prop, readback_event=("api_get", prop))


def _song_bool(prop: str, value: bool, label: str, confirm: bool = False) -> ActionSpec:
    return ActionSpec(False, False, False, _apply_song_bool(prop, value), _read_song_bool(label, prop),
                      kind="song_bool", prop=prop, confirm=confirm, readback_event=("api_get", prop))


def _song_call(method: str, text: str) -> ActionSpec:
    return ActionSpec(False, False, False, _apply_song_call(method), _read_text(text),
                      kind="song_call", prop=method, readback_event=("api_get", "is_playing"))


def _transport(method: str) -> ActionSpec:
    return ActionSpec(False, False, False, _apply_transport(method), read_playing,
                      kind="transport", prop=method, readback_event=("api_get", "is_playing"))


def _monitor(value: int) -> ActionSpec:
    return ActionSpec(True, False, False, _apply_track_int("current_monitoring_state", value),
                      _read_track_int("label.monitor", "current_monitoring_state", MONITOR_NAMES),
                      kind="track_int", prop="current_monitoring_state",
                      readback_event=("api_get", "current_monitoring_state"))


def _slot_path(snapshot: Snapshot, intent: Intent) -> str:
    track = _track(snapshot, intent)
    if intent.clip is None:
        raise LocalizedError("error.clip_required")
    return f"{track.path} clip_slots {intent.clip}"


def _apply_slot_call(method: str) -> Apply:
    def apply(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
        return [
            ["--write", "--api-call", _slot_path(snapshot, intent), method, "[]", request_id("call")],
            ["--api-get", "live_set", "is_playing", request_id("playing")],
        ]
    return apply


def apply_launch_scene(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    if intent.scene is None:
        raise LocalizedError("error.scene_required")
    scene = next((item for item in snapshot.scenes if item.index == intent.scene), None)
    if scene is None:
        raise LocalizedError("error.scene_missing")
    return [
        ["--write", "--api-call", scene.path, "fire", "[]", request_id("call")],
        ["--api-get", "live_set", "is_playing", request_id("playing")],
    ]


def _apply_device_bool(value: bool) -> Apply:
    def apply(_snapshot: Snapshot, intent: Intent) -> list[list[str]]:
        if intent.param is None:
            raise LocalizedError("error.device_required")
        parameter = intent.param
        return _parameter_batches(parameter.path, 1.0 if value else 0.0, "--api-device-parameters", parameter.path.rsplit(" parameters ", 1)[0])
    return apply


def _clip_name(snapshot: Snapshot, intent: Intent) -> str:
    track = _track(snapshot, intent)
    clip = next((item for item in track.clips if item.slot == intent.clip), None)
    return clip.name if clip else render("readback.slot", slot=(intent.clip or 0) + 1)


def read_clip_launch(snapshot: Snapshot, intent: Intent) -> str:
    return render("readback.clip_launch", track=_track_name(snapshot, intent), clip=_clip_name(snapshot, intent))


def read_clip_stop(snapshot: Snapshot, intent: Intent) -> str:
    return render("readback.clip_stop", track=_track_name(snapshot, intent), clip=_clip_name(snapshot, intent))


def read_scene(snapshot: Snapshot, intent: Intent) -> str:
    scene = next((item for item in snapshot.scenes if item.index == intent.scene), None)
    return render("readback.scene", scene=scene.name) if scene else render("readback.scene_generic")


def read_device_state(snapshot: Snapshot, intent: Intent) -> str:
    if intent.device is None or intent.param is None:
        return render("readback.device_missing")
    for track in snapshot.tracks:
        for device in track.devices:
            for parameter in device.params:
                if parameter.path == intent.param.path:
                    state = render("state.on" if parameter.value >= 0.5 else "state.off")
                    return render("readback.track_state", track=track.name, label=device.name, state=state)
    return render("readback.device_missing")


def apply_send(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    track = _track(snapshot, intent)
    if intent.send is None:
        raise LocalizedError("error.send_required")
    path = f"{track.path} mixer_device sends {intent.send}"
    current = track.sends[intent.send] if intent.send < len(track.sends) else 0.0
    if intent.number:
        if intent.number.unit == "percent":
            value = intent.number.value / 100.0
        else:
            value = intent.number.value
    else:
        value = current + _step_delta(intent.step, 0.05, 0.15)
    value = _clamp(value, 0.0, 1.0)
    return [
        ["--write", "--api-parameter-set", path, f"{value:.4f}", request_id("set")],
        ["--api-get", path, "value", request_id("read")],
    ]


def read_send(snapshot: Snapshot, intent: Intent) -> str:
    track = _track(snapshot, intent)
    index = intent.send or 0
    value = track.sends[index] if index < len(track.sends) else 0.0
    label = chr(ord("A") + index)
    return render("readback.send", track=track.name, send=label, value=value * 100)


def apply_rename(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    track = _track(snapshot, intent)
    if not intent.text:
        raise LocalizedError("error.name_required")
    return [
        ["--write", "--rename-track-index", str(track.index), "--rename-track-name", intent.text],
        ["--api-get", track.path, "name", request_id("read")],
    ]


def read_rename(snapshot: Snapshot, intent: Intent) -> str:
    return render("readback.rename", name=_track(snapshot, intent).name)


def _apply_add_track(flag: str, name_flag: str) -> Apply:
    def apply(_snapshot: Snapshot, intent: Intent) -> list[list[str]]:
        arguments = ["--write", flag, "1"]
        if intent.text:
            arguments += [name_flag, intent.text]
        return [arguments]
    return apply


def _read_track_count(kind_key: str) -> Readback:
    def read(snapshot: Snapshot, _intent: Intent) -> str:
        return render("readback.add_track", kind=render(kind_key))
    return read


def _clip(snapshot: Snapshot, intent: Intent):
    track = _track(snapshot, intent)
    clip = next((item for item in track.clips if item.slot == intent.clip), None)
    if clip is None:
        raise LocalizedError("error.clip_missing")
    return clip


def _apply_clip_bool(prop: str, value: bool) -> Apply:
    def apply(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
        clip = _clip(snapshot, intent)
        return [
            ["--write", "--api-set", clip.path, prop, "1" if value else "0", request_id("set")],
            ["--api-get", clip.path, prop, request_id("read")],
        ]
    return apply


def apply_clip_pitch(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    clip = _clip(snapshot, intent)
    current = int(float(clip.props.get("pitch_coarse", 0) or 0))
    if intent.number:
        value = int(intent.number.value) if intent.step is Step.SET else current + int(intent.number.value)
    else:
        value = current + int(_step_delta(intent.step, 1.0, 12.0))
    value = int(_clamp(value, -48, 48))
    return [
        ["--write", "--api-set", clip.path, "pitch_coarse", str(value), request_id("set")],
        ["--api-get", clip.path, "pitch_coarse", request_id("read")],
    ]


def apply_clip_gain(snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    clip = _clip(snapshot, intent)
    current = float(clip.props.get("gain", 0.0) or 0.0)
    if intent.number:
        value = intent.number.value / 100.0 if intent.number.unit == "percent" else intent.number.value
    else:
        value = current + _step_delta(intent.step, 0.05, 0.15)
    value = _clamp(value, 0.0, 1.0)
    return [
        ["--write", "--api-set", clip.path, "gain", f"{value:.4f}", request_id("set")],
        ["--api-get", clip.path, "gain", request_id("read")],
    ]


def _read_clip_prop(label: str, prop: str, fmt: Callable[[Any], str]) -> Readback:
    def read(snapshot: Snapshot, intent: Intent) -> str:
        clip = _clip(snapshot, intent)
        return render("readback.clip_prop", track=_track_name(snapshot, intent), clip=clip.name, label=render(label), value=fmt(clip.props.get(prop)))
    return read


def _fmt_bool(value: Any) -> str:
    return render("state.on" if value else "state.off")


def _fmt_pitch(value: Any) -> str:
    pitch = int(float(value or 0))
    return render("unit.pitch", value=pitch)


def _fmt_gain(value: Any) -> str:
    return f"{float(value or 0.0) * 100:.0f}%"


def _clip_prop(prop: str, value: bool, label: str) -> ActionSpec:
    return ActionSpec(True, False, False, _apply_clip_bool(prop, value), _read_clip_prop(label, prop, _fmt_bool),
                      kind="clip_prop", prop=prop, readback_event=("api_get", prop), needs_clip=True)


def apply_add_track_with_device(_snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    if not intent.native_device and not intent.plugin:
        raise LocalizedError("error.device_name_required")
    audio = intent.track_kind == "audio"
    arguments = ["--write", "--add-audio-tracks" if audio else "--add-midi-tracks", "1"]
    if intent.text:
        arguments += ["--audio-prefix" if audio else "--midi-name", intent.text]
    return [arguments]


def read_add_track_with_device(snapshot: Snapshot, intent: Intent) -> str:
    return render("readback.add_device", device=intent.native_device)


def apply_plugin_noop(_snapshot: Snapshot, intent: Intent) -> list[list[str]]:
    if not intent.plugin:
        raise LocalizedError("error.plugin_name_required")
    return []


def read_plugin(snapshot: Snapshot, intent: Intent) -> str:
    if intent.action is Action.INSERT_PLUGIN:
        return render("readback.insert_plugin", plugin=intent.plugin)
    return render("readback.add_plugin", plugin=intent.plugin)


ACTIONS: dict[Action, ActionSpec] = {
    Action.INSERT_PLUGIN: ActionSpec(True, False, False, apply_plugin_noop, read_plugin, kind="plugin", confirm=True, readback_event=("api_device_list", None)),
    Action.ADD_TRACK_WITH_PLUGIN: ActionSpec(False, False, False, apply_add_track_with_device, read_plugin, kind="plugin_track", confirm=True, readback_event=("api_children", None)),
    Action.ADD_TRACK_WITH_DEVICE: ActionSpec(False, False, False, apply_add_track_with_device, read_add_track_with_device, kind="structure_device", confirm=True, readback_event=("api_children", None)),
    Action.CLIP_LOOP_ON: _clip_prop("looping", True, "label.clip_loop"),
    Action.CLIP_LOOP_OFF: _clip_prop("looping", False, "label.clip_loop"),
    Action.CLIP_WARP_ON: _clip_prop("warping", True, "label.clip_warp"),
    Action.CLIP_WARP_OFF: _clip_prop("warping", False, "label.clip_warp"),
    Action.CLIP_PITCH: ActionSpec(True, False, True, apply_clip_pitch, _read_clip_prop("label.clip_pitch", "pitch_coarse", _fmt_pitch), kind="clip_prop", prop="pitch_coarse", readback_event=("api_get", "pitch_coarse"), needs_clip=True),
    Action.CLIP_GAIN: ActionSpec(True, False, True, apply_clip_gain, _read_clip_prop("label.clip_gain", "gain", _fmt_gain), kind="clip_prop", prop="gain", readback_event=("api_get", "gain"), needs_clip=True),
    Action.SEND: ActionSpec(True, False, True, apply_send, read_send, kind="send", readback_event=("api_get", "value"), needs_send=True),
    Action.RENAME: ActionSpec(True, False, False, apply_rename, read_rename, kind="rename", prop="name", confirm=True, readback_event=("api_get", "name")),
    Action.ADD_MIDI_TRACK: ActionSpec(False, False, False, _apply_add_track("--add-midi-tracks", "--midi-name"), _read_track_count("kind.midi_track"), kind="structure", confirm=True, readback_event=("api_children", None)),
    Action.ADD_AUDIO_TRACK: ActionSpec(False, False, False, _apply_add_track("--add-audio-tracks", "--audio-prefix"), _read_track_count("kind.audio_track"), kind="structure", confirm=True, readback_event=("api_children", None)),
    Action.LAUNCH_CLIP: ActionSpec(True, False, False, _apply_slot_call("fire"), read_clip_launch, kind="clip_call", prop="fire", readback_event=("api_get", "is_playing"), needs_clip=True),
    Action.STOP_CLIP: ActionSpec(True, False, False, _apply_slot_call("stop"), read_clip_stop, kind="clip_call", prop="stop", readback_event=("api_get", "is_playing"), needs_clip=True),
    Action.LAUNCH_SCENE: ActionSpec(False, False, False, apply_launch_scene, read_scene, kind="scene_call", prop="fire", readback_event=("api_get", "is_playing"), needs_scene=True),
    Action.DEVICE_ON: ActionSpec(True, False, False, _apply_device_bool(True), read_device_state, kind="param", readback_event=("api_device_parameters", None), needs_device=True),
    Action.DEVICE_OFF: ActionSpec(True, False, False, _apply_device_bool(False), read_device_state, kind="param", readback_event=("api_device_parameters", None), needs_device=True),
    Action.VOLUME: ActionSpec(True, False, True, apply_volume, read_volume, kind="mixer", prop="volume", readback_event=("api_mixer_status", None)),
    Action.PAN: ActionSpec(True, False, True, apply_pan, read_pan, kind="mixer", prop="panning", readback_event=("api_mixer_status", None)),
    Action.MUTE: _track_bool("mute", True, "label.mute"),
    Action.UNMUTE: _track_bool("mute", False, "label.mute"),
    Action.SOLO: _track_bool("solo", True, "label.solo"),
    Action.UNSOLO: _track_bool("solo", False, "label.solo"),
    Action.ARM: _track_bool("arm", True, "label.arm"),
    Action.DISARM: _track_bool("arm", False, "label.arm"),
    Action.FOLD: _track_bool("fold_state", True, "label.fold"),
    Action.UNFOLD: _track_bool("fold_state", False, "label.fold"),
    Action.MONITOR_IN: _monitor(0),
    Action.MONITOR_AUTO: _monitor(1),
    Action.MONITOR_OFF: _monitor(2),
    Action.TEMPO: ActionSpec(False, False, True, apply_tempo, read_tempo, kind="tempo", prop="tempo", readback_event=("api_get", "tempo")),
    Action.PLAY: _transport("start_playing"),
    Action.STOP: _transport("stop_playing"),
    Action.CONTINUE: _transport("continue_playing"),
    Action.RECORD_ON: _song_bool("session_record", True, "label.record", confirm=True),
    Action.RECORD_OFF: _song_bool("session_record", False, "label.record"),
    Action.OVERDUB_ON: _song_bool("overdub", True, "label.overdub"),
    Action.OVERDUB_OFF: _song_bool("overdub", False, "label.overdub"),
    Action.LOOP_ON: _song_bool("loop", True, "label.loop"),
    Action.LOOP_OFF: _song_bool("loop", False, "label.loop"),
    Action.METRONOME_ON: _song_bool("metronome", True, "label.metronome"),
    Action.METRONOME_OFF: _song_bool("metronome", False, "label.metronome"),
    Action.UNDO: _song_call("undo", "readback.text.undo"),
    Action.REDO: _song_call("redo", "readback.text.redo"),
    Action.CAPTURE_MIDI: _song_call("capture_midi", "readback.text.capture"),
    Action.TAP_TEMPO: _song_call("tap_tempo", "readback.text.tap"),
    Action.STOP_ALL_CLIPS: _song_call("stop_all_clips", "readback.text.stop_all"),
    Action.TRACK_STOP_CLIPS: ActionSpec(True, False, False, _apply_track_call("stop_all_clips"), _read_text("readback.text.stop_track"), kind="track_call", prop="stop_all_clips", readback_event=("api_get", "is_playing")),
    Action.JUMP_TO_BAR: ActionSpec(False, False, True, apply_jump, read_position, kind="jump", prop="current_song_time", readback_event=("api_get", "current_song_time")),
    Action.PARAM: ActionSpec(True, True, True, apply_param, read_param, kind="param", readback_event=("api_device_parameters", None)),
    Action.NONE: ActionSpec(False, False, False, _unused, _none_readback, kind="none"),
}
