"""Live を import せずに LOM path と Remote Script 側の許可を扱う。"""

from dataclasses import dataclass
import re


class LomPathError(ValueError):
    pass


class LomPermissionError(ValueError):
    pass


@dataclass(frozen=True)
class PathStep:
    attribute: str
    index: int = None


_INDEX = re.compile(r"0|[1-9][0-9]*")

GET_MEMBERS = {
    "song": frozenset((
        "tempo", "is_playing", "loop", "metronome", "session_record",
        "overdub", "current_song_time", "signature_numerator",
        "signature_denominator",
    )),
    "track": frozenset((
        "name", "mute", "solo", "arm", "current_monitoring_state",
        "fold_state",
    )),
    "clip_slot": frozenset(("has_clip", "is_playing", "is_triggered")),
    "clip": frozenset((
        "name", "is_playing", "is_triggered", "looping", "length",
        "warping", "pitch_coarse", "gain", "gain_display_string",
    )),
    "scene": frozenset(("name",)),
    "track_send": frozenset(("value",)),
}

SET_MEMBERS = {
    "song": frozenset((
        "loop", "metronome", "session_record", "overdub",
        "current_song_time",
    )),
    "track": frozenset((
        "mute", "solo", "arm", "current_monitoring_state", "fold_state",
    )),
    "clip": frozenset(("looping", "warping", "pitch_coarse", "gain")),
}

CALL_MEMBERS = {
    "song": frozenset((
        "start_playing", "stop_playing", "continue_playing", "undo",
        "redo", "capture_midi", "tap_tempo", "stop_all_clips",
    )),
    "track": frozenset(("stop_all_clips",)),
    "clip_slot": frozenset(("fire", "stop")),
    "scene": frozenset(("fire",)),
    "track_volume": frozenset(("str_for_value",)),
    "track_panning": frozenset(("str_for_value",)),
    "master_volume": frozenset(("str_for_value",)),
}

PARAMETER_KINDS = frozenset((
    "track_volume", "track_panning", "master_volume", "track_send",
    "device_parameter",
))

NATIVE_DEVICES = frozenset((
    "Analog", "Collision", "Drift", "Drum Rack", "Electric", "Impulse",
    "Instrument Rack", "Meld", "Operator", "Sampler", "Simpler",
    "Tension", "Wavetable", "Amp", "Audio Effect Rack", "Auto Filter",
    "Auto Pan", "Beat Repeat", "Cabinet", "Channel EQ", "Chorus-Ensemble",
    "Compressor", "Corpus", "Delay", "Drum Buss", "Dynamic Tube", "Echo",
    "EQ Eight", "EQ Three", "Erosion", "Filter Delay", "Gate",
    "Glue Compressor", "Grain Delay", "Hybrid Reverb", "Limiter", "Looper",
    "Multiband Dynamics", "Overdrive", "Pedal", "Phaser-Flanger", "Redux",
    "Resonators", "Reverb", "Roar", "Saturator", "Shifter",
    "Spectral Resonator", "Spectral Time", "Spectrum", "Tuner", "Utility",
    "Vinyl Distortion", "Vocoder", "Arpeggiator", "Chord",
    "MIDI Effect Rack", "Note Length", "Pitch", "Random", "Scale",
    "Velocity",
))


def _indexed(tokens, position, attribute):
    if position + 1 >= len(tokens) or not _INDEX.fullmatch(tokens[position + 1]):
        raise LomPathError("invalid_index")
    return PathStep(attribute, int(tokens[position + 1])), position + 2


def parse_lom_path(path):
    tokens = path.split()
    if not tokens or tokens[0] != "live_set":
        raise LomPathError("invalid_root")
    steps = [PathStep("live_set")]
    kind = "song"
    position = 1
    while position < len(tokens):
        token = tokens[position]
        if kind == "song" and token in ("tracks", "return_tracks", "scenes"):
            step, position = _indexed(tokens, position, token)
            steps.append(step)
            kind = {"tracks": "track", "return_tracks": "return_track", "scenes": "scene"}[token]
            continue
        if kind == "song" and token in ("master_track", "view"):
            steps.append(PathStep(token))
            kind = "master_track" if token == "master_track" else "view"
            position += 1
            continue
        if kind == "track" and token == "clip_slots":
            step, position = _indexed(tokens, position, token)
            steps.append(step)
            kind = "clip_slot"
            continue
        if kind == "clip_slot" and token == "clip":
            steps.append(PathStep(token))
            kind = "clip"
            position += 1
            continue
        if kind in ("track", "return_track", "master_track") and token == "devices":
            step, position = _indexed(tokens, position, token)
            steps.append(step)
            kind = "device"
            continue
        if kind == "device" and token == "parameters":
            step, position = _indexed(tokens, position, token)
            steps.append(step)
            kind = "device_parameter"
            continue
        if kind in ("track", "return_track", "master_track") and token == "mixer_device":
            owner = kind
            steps.append(PathStep(token))
            kind = owner + "_mixer"
            position += 1
            continue
        if kind in ("track_mixer", "return_track_mixer", "master_track_mixer") and token in ("volume", "panning"):
            if kind == "master_track_mixer" and token == "panning":
                raise LomPathError("invalid_transition")
            owner = kind.split("_mixer", 1)[0]
            steps.append(PathStep(token))
            kind = ("master" if owner == "master_track" else owner) + "_" + token
            position += 1
            continue
        if kind == "track_mixer" and token == "sends":
            step, position = _indexed(tokens, position, token)
            steps.append(step)
            kind = "track_send"
            continue
        raise LomPathError("invalid_transition")
    return tuple(steps)


def path_kind(path):
    steps = parse_lom_path(path)
    kind = "song"
    for step in steps[1:]:
        attribute = step.attribute
        if attribute == "tracks":
            kind = "track"
        elif attribute == "return_tracks":
            kind = "return_track"
        elif attribute == "master_track":
            kind = "master_track"
        elif attribute == "scenes":
            kind = "scene"
        elif attribute == "view":
            kind = "view"
        elif attribute == "clip_slots":
            kind = "clip_slot"
        elif attribute == "clip":
            kind = "clip"
        elif attribute == "devices":
            kind = "device"
        elif attribute == "parameters":
            kind = "device_parameter"
        elif attribute == "mixer_device":
            kind = kind + "_mixer"
        elif attribute in ("volume", "panning"):
            owner = kind.split("_mixer", 1)[0]
            kind = ("master" if owner == "master_track" else owner) + "_" + attribute
        elif attribute == "sends":
            kind = "track_send"
    return kind


def resolve_lom_path(song, path):
    current = song
    for step in parse_lom_path(path)[1:]:
        try:
            current = getattr(current, step.attribute)
            if step.index is not None:
                current = current[step.index]
        except (AttributeError, IndexError, KeyError, TypeError):
            raise LomPathError("path_not_found")
        if current is None:
            raise LomPathError("path_not_found")
    return current


def allow_get(path, prop):
    return prop in GET_MEMBERS.get(path_kind(path), ())


def allow_set(path, prop):
    return prop in SET_MEMBERS.get(path_kind(path), ())


def allow_call(path, method):
    return method in CALL_MEMBERS.get(path_kind(path), ())


def allow_param_set(path):
    return path_kind(path) in PARAMETER_KINDS


def require_allowed(operation, path, member=None):
    checks = {
        "lom_get": allow_get,
        "lom_set": allow_set,
        "lom_call": allow_call,
    }
    try:
        allowed = allow_param_set(path) if operation == "param_set" else checks[operation](path, member)
    except (KeyError, LomPathError):
        allowed = False
    if not allowed:
        raise LomPermissionError("not_allowed")
