"""English fixed-phrase parsing for Live Jev."""

from __future__ import annotations

import re
from typing import Literal

from bridge_client import NATIVE_DEVICES
from snapshot import Snapshot, is_bridge_track
from intent import (
    Action,
    ClipNotesRequest,
    Intent,
    Number,
    PluginRequest,
    Step,
    _local_intent,
    resolve_native_device,
)


ENGLISH_PHRASES: dict[str, tuple[str, ...]] = {
    "polite_prefix": (
        "please", "can you", "could you", "would you", "i want to",
        "i'd like to", "i would like to", "i wanna", "let's", "go ahead and", "just", "hey", "ok", "okay",
    ),
    "polite_suffix": ("for me", "now", "thanks", "thank you", "please", "if you can", "real quick"),
    "insert": (
        "insert", "add", "load", "open", "put", "drop", "throw", "place",
        "bring up", "fire up", "launch", "pull up", "use", "apply", "stick", "slap",
        "put in", "put on", "drop in", "drop on", "throw on", "throw in", "slap on", "stick on", "stick in",
        "pop in", "pop on", "chuck", "chuck in", "chuck on", "whack on", "toss on", "toss in",
        "set up", "spin up", "call up", "load up", "open up", "boot up", "start up", "bring in", "plug in", "hook up",
        "summon", "instantiate", "mount", "attach", "try", "try out",
        "give me", "get me", "i need", "i want", "i'd like", "i would like", "can i get", "can i have",
        "let me have", "let's use", "let's try", "let's have", "how about", "we need",
    ),
    "request_prefix": (
        "give me", "get me", "i need", "i want", "i'd like", "i would like", "can i get", "can i have",
        "let me have", "let's have", "we need", "how about",
    ),
    "mixer_words": (
        "volume", "pan", "mute", "solo", "send", "tempo", "loop", "metronome", "click", "record", "recording",
        "monitor", "arm", "level", "gain", "pitch", "warp",
    ),
    "insert_preposition": ("on", "onto", "in", "into", "to"),
    "new_track": ("new", "another", "a fresh", "fresh"),
    "selected_track": ("selected track", "this track", "current track", "the track i'm on"),
    "up": ("up", "raise", "increase", "boost", "louder", "turn up"),
    "down": ("down", "lower", "decrease", "reduce", "quieter", "turn down", "pull down"),
    "small": ("a bit", "a little", "slightly", "a touch", "a hair"),
    "large": ("a lot", "way", "much"),
    "negation": ("don't", "do not", "never", "no need", "not necessary", "without", "stop short of", "skip"),
    "play": ("play", "start", "start playback"),
    "continue": ("continue", "resume", "continue playback", "resume playback"),
    "stop": ("stop", "stop playback"),
    "quantize": ("quantize", "quantise"),
    "legato": ("legato", "make it legato", "connect the notes", "fill the gaps"),
    "duplicate_loop": ("double the loop", "duplicate loop", "twice as long"),
}


_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")


def _alternation(name: str) -> str:
    return "|".join(re.escape(value) for value in sorted(ENGLISH_PHRASES[name], key=len, reverse=True))


def normalize_english_phrase(utterance: str) -> str:
    text = utterance.replace("’", "'").strip().casefold()
    text = re.sub(r"[.!?]+$", "", text).strip()
    prefixes = _alternation("polite_prefix")
    suffixes = _alternation("polite_suffix")
    while True:
        changed = re.sub(rf"^(?:{prefixes})\b[\s,]*", "", text).strip()
        if changed == text:
            break
        text = changed
    while True:
        changed = re.sub(rf"[\s,]*\b(?:{suffixes})$", "", text).strip()
        if changed == text:
            break
        text = changed
    return re.sub(r"\s+", " ", text)


def is_negated_en(utterance: str) -> bool:
    text = utterance.replace("’", "'").casefold()
    return re.search(rf"(?:^|\b)(?:{_alternation('negation')})(?:\b|$)", text) is not None


def _track_target_pattern(snapshot: Snapshot, include_master: bool = True) -> str:
    names = [re.escape(track.name) for track in snapshot.tracks if track.name and not is_bridge_track(track)]
    names.sort(key=len, reverse=True)
    fixed = [
        _alternation("selected_track"),
        r"track\s*\d+",
        r"(?:\d+)(?:st|nd|rd|th)\s+track",
    ]
    if include_master:
        fixed += [r"master(?:\s+track)?", r"main(?:\s+track)?"]
    return "(?:" + "|".join(fixed + names) + ")"


def _track_en(snapshot: Snapshot, value: str) -> int | None | Literal["master", "selected"]:
    text = value.strip().casefold()
    text = re.sub(r"^the\s+", "", text)
    if text in {item.casefold() for item in ENGLISH_PHRASES["selected_track"]}:
        return "selected"
    if text in {"master", "master track", "main", "main track"}:
        return "master"
    numbered = re.fullmatch(r"(?:track\s*(\d+)|(\d+)(?:st|nd|rd|th)\s+track)", text)
    if numbered:
        position = int(next(group for group in numbered.groups() if group))
        found = next((track for track in snapshot.tracks if track.index == position - 1), None)
        return found.index if found is not None and position > 0 else None
    if text.endswith(" track"):
        text = text[:-6].strip()
    matches = [
        track.index for track in snapshot.tracks
        if not is_bridge_track(track) and track.name.casefold() == text
    ]
    return matches[0] if len(matches) == 1 else None


def _find_target(snapshot: Snapshot, text: str, include_master: bool = True):
    match = re.search(rf"(?<![\w])(?P<target>{_track_target_pattern(snapshot, include_master)})(?![\w])", text, re.IGNORECASE)
    if not match:
        return None, None
    return _track_en(snapshot, match.group("target")), match.span()


def _step(text: str, direction: str | None = None) -> Step:
    small = re.search(rf"\b(?:{_alternation('small')})\b", text) is not None
    large = re.search(rf"\b(?:{_alternation('large')})\b", text) is not None
    if direction == "up":
        return Step.UP_BIG if large else Step.UP_SMALL
    if direction == "down":
        return Step.DOWN_BIG if large else Step.DOWN_SMALL
    if re.search(rf"\b(?:{_alternation('up')})\b", text):
        return Step.UP_BIG if large else Step.UP_SMALL
    if re.search(rf"\b(?:{_alternation('down')})\b", text):
        return Step.DOWN_BIG if large else Step.DOWN_SMALL
    return Step.SET if re.search(r"\b(?:to|at)\s+-?\d", text) else Step.NONE


def _number(text: str, unit: str) -> Number | None:
    match = re.search(r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*(?:db|bpm|%|percent|st|semitones?|half steps?)?\b", text, re.IGNORECASE)
    if not match:
        return None
    return Number(float(match.group(1)), unit)  # type: ignore[arg-type]


def _named_track(text: str) -> tuple[str, str | None]:
    match = re.search(r"\b(?:named|called)\s+([\w .'-]+?)(?=\s+(?:with|and|on|in|containing|track)\b|$)", text, re.IGNORECASE)
    if not match:
        return text, None
    name = match.group(1).strip().strip("\"'")
    return (text[:match.start()] + " " + text[match.end():]).strip(), name or None


def _clean_object(value: str) -> str:
    text = value.strip(" ,\"'")
    text = re.sub(r"^(?:the|a|an)\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:please|for me|now)$", "", text, flags=re.IGNORECASE)
    return text.strip(" ,\"'")


def _new_track_request(text: str, snapshot: Snapshot) -> tuple[str, str | None, str | None] | None:
    working, track_name = _named_track(text)
    working = re.sub(rf"^(?:{_alternation('request_prefix')})\s+", "", working, flags=re.IGNORECASE)
    kind_match = re.search(r"\b(audio|midi|instrument)\s+track\b", working, re.IGNORECASE)
    kind = "audio" if kind_match and kind_match.group(1).casefold() == "audio" else None
    insert = _alternation("insert")
    patterns = (
        rf"^(?:create|make|add|set up|spin up|start|open)\s+(?:an?\s+)?(?:(?:new|another|fresh)\s+)?(?:(?:midi|audio|instrument)\s+)?track\s+(?:with|for|and\s+(?:{insert}))\s+(?P<object>.+)$",
        rf"^(?:an?\s+)?(?:new|another|fresh)\s+(?:(?:midi|audio|instrument)\s+)?track\s+(?:with|for|and\s+(?:{insert})|(?:{insert}))\s+(?P<object>.+)$",
        rf"^(?P<object>.+?)\s+(?:on|in|into|to|onto)\s+(?:(?:an?\s+)?(?:new|another|fresh|separate)|its own)\s+(?:(?:midi|audio|instrument)\s+)?track$",
        rf"^(?:{insert})\s+(?P<object>.+?)\s+(?:on|in|into|to|onto)\s+(?:(?:an?\s+)?(?:new|another|fresh|separate)|its own)\s+(?:(?:midi|audio|instrument)\s+)?track$",
        r"^track\s+with\s+(?P<object>.+)$",
        r"^(?P<object>.+?)\s+track$",
    )
    for index, pattern in enumerate(patterns):
        match = re.fullmatch(pattern, working, re.IGNORECASE)
        if not match:
            continue
        raw = _clean_object(match.group("object"))
        raw = re.sub(rf"^(?:{insert})\s+", "", raw, flags=re.IGNORECASE)
        if not raw or raw in {"audio", "midi", "instrument", "new", "another", "fresh"}:
            continue
        if index == len(patterns) - 1 and _track_en(snapshot, raw) is not None:
            continue
        if index == len(patterns) - 1 and (
            re.match(rf"^(?:{insert})\b", working, re.IGNORECASE)
            or re.search(r"\b(?:selected|current|this)\b", raw)
        ):
            continue
        return raw, kind, track_name
    return None


def extract_plugin_request_en(utterance: str, snapshot: Snapshot) -> PluginRequest | None:
    if is_negated_en(utterance):
        return None
    text = normalize_english_phrase(utterance)
    new_request = _new_track_request(text, snapshot)
    if new_request is not None:
        raw, kind, track_name = new_request
        if resolve_native_device(raw) is None:
            return PluginRequest(Action.ADD_TRACK_WITH_PLUGIN, raw, None, track_name or kind)
        return None

    insert = _alternation("insert")
    prep = _alternation("insert_preposition")
    target_pattern = _track_target_pattern(snapshot, include_master=False)
    patterns = (
        rf"^(?:{insert})\s+(?P<object>.+?)\s+(?:{prep})\s+(?:the\s+)?(?P<target>{target_pattern})$",
        rf"^(?:{insert})\s+(?P<object>.+)$",
        rf"^(?P<object>.+?)\s+(?:{prep})\s+(?:the\s+)?(?P<target>{target_pattern})\s*(?:please)?$",
        rf"^(?:the\s+)?(?P<target>{target_pattern})\s+(?:with|gets?)\s+(?P<object>.+)$",
    )
    for index, pattern in enumerate(patterns):
        match = re.fullmatch(pattern, text, re.IGNORECASE)
        if not match:
            continue
        raw = _clean_object(match.group("object"))
        target_text = match.groupdict().get("target")
        if not raw or re.search(r"\b(?:track|clip|scene)\b", raw):
            continue
        if index == 2 and not re.search(rf"\b(?:{insert})\b", text) and re.search(rf"\b(?:{_alternation('mixer_words')})\b", raw, re.IGNORECASE):
            continue
        track = _track_en(snapshot, target_text) if target_text else None
        if target_text is None or track is not None:
            return PluginRequest(Action.INSERT_PLUGIN, raw, track, None)
    return None


def _device_in(snapshot: Snapshot, text: str):
    choices = [
        (track.index, device)
        for track in snapshot.tracks if not is_bridge_track(track)
        for device in track.devices if device.name
    ]
    choices.sort(key=lambda pair: len(pair[1].name), reverse=True)
    for track_index, device in choices:
        if re.search(rf"(?<![\w]){re.escape(device.name)}(?![\w])", text, re.IGNORECASE):
            return track_index, device
    return None, None


def parse_local_en(utterance: str, snapshot: Snapshot) -> Intent | None:
    if is_negated_en(utterance):
        return None
    text = normalize_english_phrase(utterance)

    exact: tuple[tuple[str, Action], ...] = (
        (r"(?:play|start|start playback)", Action.PLAY),
        (r"(?:continue|resume|continue playback|resume playback)", Action.CONTINUE),
        (r"(?:stop|stop playback)", Action.STOP),
        (r"(?:record|start recording|record on)", Action.RECORD_ON),
        (r"(?:stop recording|record off)", Action.RECORD_OFF),
        (r"(?:overdub|overdub on|turn overdub on)", Action.OVERDUB_ON),
        (r"(?:overdub off|turn overdub off|disable overdub)", Action.OVERDUB_OFF),
        (r"(?:loop|loop on|turn loop on)", Action.LOOP_ON),
        (r"(?:loop off|turn loop off|disable loop)", Action.LOOP_OFF),
        (r"(?:(?:metronome|click)(?: on)?|turn (?:the )?(?:metronome|click) on)", Action.METRONOME_ON),
        (r"(?:(?:metronome|click) off|turn (?:the )?(?:metronome|click) off)", Action.METRONOME_OFF),
        (r"(?:undo|revert|take (?:that|it) back|put (?:that|it) back|go back|scratch that|never ?mind)(?: (?:that|it|this|the last (?:one|thing|change)))?", Action.UNDO),
        (r"redo(?: (?:that|it))?", Action.REDO),
        (r"capture midi", Action.CAPTURE_MIDI), (r"tap tempo", Action.TAP_TEMPO),
        (r"stop all clips", Action.STOP_ALL_CLIPS),
    )
    for pattern, action in exact:
        if re.fullmatch(pattern, text, re.IGNORECASE):
            return _local_intent(action)

    tempo = re.fullmatch(r"(?:(?:set|change)\s+)?tempo(?:\s+to|\s+at)?\s+(-?\d+(?:\.\d+)?)\s*(?:bpm)?|(-?\d+(?:\.\d+)?)\s*bpm", text)
    if tempo:
        value = float(tempo.group(1) or tempo.group(2))
        return _local_intent(Action.TEMPO, step=Step.SET, number=Number(value, "bpm"))
    bar = re.fullmatch(r"(?:go|jump|move)\s+to\s+bar\s+(\d+)|bar\s+(\d+)", text)
    if bar:
        return _local_intent(Action.JUMP_TO_BAR, step=Step.SET, number=Number(float(bar.group(1) or bar.group(2)), "raw"))
    if text in {"go to the start", "jump to the start", "from the beginning"}:
        return _local_intent(Action.JUMP_TO_BAR, step=Step.SET, number=Number(1.0, "raw"))

    scene = re.fullmatch(r"(?:(?:launch|fire|play)\s+)?scene\s+(\d+)(?:\s+(?:launch|fire|play))?", text)
    if scene:
        index = int(scene.group(1)) - 1
        if any(item.index == index for item in snapshot.scenes):
            return _local_intent(Action.LAUNCH_SCENE, scene=index)

    new_request = _new_track_request(text, snapshot)
    if new_request is not None:
        raw, kind, track_name = new_request
        device = resolve_native_device(raw)
        if device is not None:
            return _local_intent(Action.ADD_TRACK_WITH_DEVICE, text=track_name or kind, native_device=device)
    plain_track = re.fullmatch(r"(?:create|make|add)\s+(?:(?:a|an)\s+)?(?:(?:new|another|fresh)\s+)?(?:(midi|audio|instrument)\s+)?track(?:\s+(?:named|called)\s+(.+))?", text)
    if plain_track:
        action = Action.ADD_AUDIO_TRACK if plain_track.group(1) == "audio" else Action.ADD_MIDI_TRACK
        return _local_intent(action, text=_clean_object(plain_track.group(2) or "") or None)

    target_pattern = _track_target_pattern(snapshot)
    rename = re.fullmatch(rf"(?:rename|call)\s+(?:the\s+)?(?P<target>{target_pattern})\s+(?:to\s+)?(?P<name>.+)|(?P<target2>{target_pattern})\s+(?:rename|name)\s+(?:to\s+)?(?P<name2>.+)", text, re.IGNORECASE)
    if rename:
        target_text = rename.group("target") or rename.group("target2")
        name = _clean_object(rename.group("name") or rename.group("name2"))
        track = _track_en(snapshot, target_text)
        if isinstance(track, int) and name:
            return _local_intent(Action.RENAME, track=track, text=name)

    clip_match = re.search(rf"(?:(?P<target>{target_pattern})\s+)?clip\s+(?P<slot>\d+)(?:\s+(?:on|in)\s+(?P<target2>{target_pattern}))?", text, re.IGNORECASE)
    if clip_match:
        target_text = clip_match.group("target") or clip_match.group("target2")
        track = _track_en(snapshot, target_text) if target_text else None
        slot = int(clip_match.group("slot")) - 1
        if track is not None and slot >= 0:
            if re.search(r"\b(?:stop|halt)\b", text):
                return _local_intent(Action.STOP_CLIP, track=track, clip=slot)
            if re.search(r"\b(?:launch|fire|play)\b", text):
                return _local_intent(Action.LAUNCH_CLIP, track=track, clip=slot)
            if re.search(r"\b(?:loop)\s+(?:on|off)\b", text):
                action = Action.CLIP_LOOP_OFF if re.search(r"\boff\b", text) else Action.CLIP_LOOP_ON
                return _local_intent(action, track=track, clip=slot)
            if re.search(r"\bwarp\s+(?:on|off)\b", text):
                action = Action.CLIP_WARP_OFF if re.search(r"\boff\b", text) else Action.CLIP_WARP_ON
                return _local_intent(action, track=track, clip=slot)
            pitch = re.search(r"(?:transpose|shift|move|pitch)?\s*(?:up|down)\s+(\d+)\s*(?:st|semitones?|half steps?)", text)
            if pitch:
                value = float(pitch.group(1)) * (-1 if "down" in pitch.group(0) else 1)
                return _local_intent(Action.CLIP_PITCH, track=track, clip=slot, step=Step.UP_SMALL if value > 0 else Step.DOWN_SMALL, number=Number(value, "raw"))
            gain = re.search(r"\b(?:gain|volume)\b", text)
            if gain:
                step = _step(text)
                number = _number(text, "percent")
                if step is not Step.NONE or number is not None:
                    return _local_intent(Action.CLIP_GAIN, track=track, clip=slot, step=step, number=number)

    device_track, device = _device_in(snapshot, text)
    if device is not None and re.search(r"\b(?:turn|switch|enable|disable|bypass)\b|\b(?:on|off)\b", text):
        off = re.search(r"\b(?:off|disable|bypass)\b", text) is not None
        intent = _local_intent(Action.DEVICE_OFF if off else Action.DEVICE_ON, track=device_track)
        parameter = next((item for item in device.params if item.index == 0), None)
        return Intent(**{**intent.__dict__, "device": device, "device_conf": 1.0, "param": parameter, "param_conf": 1.0 if parameter else 0.0})

    track, target_span = _find_target(snapshot, text)
    rest = text if target_span is None else (text[:target_span[0]] + " " + text[target_span[1]:]).strip()
    monitor = re.search(r"\bmonitor(?:ing)?\s+(?:the\s+)?(?:[\w .'-]+\s+)?(in|auto|off)\b", text)
    if monitor:
        state = monitor.group(1)
        action = {"in": Action.MONITOR_IN, "auto": Action.MONITOR_AUTO, "off": Action.MONITOR_OFF}[state]
        return _local_intent(action, track=track)

    toggle_patterns = (
        (r"\bunmute\b|\bmute\s+off\b", Action.UNMUTE),
        (r"\bmute\b", Action.MUTE),
        (r"\bunsolo\b|\bsolo\s+off\b", Action.UNSOLO),
        (r"\bsolo\b", Action.SOLO),
        (r"\bdisarm\b|\brecord\s+arm\s+off\b", Action.DISARM),
        (r"\barm\b|\brecord\s+arm\b", Action.ARM),
        (r"\bunfold\b|\bexpand\b", Action.UNFOLD),
        (r"\bfold\b|\bcollapse\b", Action.FOLD),
    )
    for pattern, action in toggle_patterns:
        if re.search(pattern, rest):
            return _local_intent(action, track=track)

    if re.search(r"\bstop\s+(?:the\s+)?(?:track(?:'s)?\s+)?clips\b", text) or re.fullmatch(r"stop\s+(?:the\s+)?clips", rest):
        return _local_intent(Action.TRACK_STOP_CLIPS, track=track)

    send = re.search(r"\bsend\s+([a-z]|\d+)\b", text)
    if send:
        token = send.group(1)
        index = int(token) - 1 if token.isdigit() else ord(token.upper()) - ord("A")
        number = _number(text, "percent")
        step = _step(text)
        if index >= 0 and (number is not None or step is not Step.NONE):
            return _local_intent(Action.SEND, track=track, send=index, step=step, number=number)

    if re.search(r"\bpan\b|\bcenter\b", text):
        if re.search(r"\bcenter\b", text):
            number = Number(0.0, "pan")
        else:
            pan = re.search(r"(?:left\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*left|right\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*right)", text)
            if pan:
                groups = pan.groups()
                value = float(next(item for item in groups if item is not None))
                number = Number(-value if groups[0] or groups[1] else value, "pan")
            else:
                number = None
        if number is not None:
            return _local_intent(Action.PAN, track=track, step=Step.SET, number=number)

    volume_hint = re.search(r"\b(?:volume|louder|quieter|turn up|turn down|raise|lower|increase|decrease|reduce|boost|pull down)\b", rest)
    db = re.search(r"(-?\d+(?:\.\d+)?)\s*dB\b", text, re.IGNORECASE)
    if volume_hint or db:
        number = Number(float(db.group(1)), "db") if db else None
        direction = "down" if re.search(rf"\b(?:{_alternation('down')})\b", rest) else "up" if re.search(rf"\b(?:{_alternation('up')})\b", rest) else None
        step = Step.SET if number is not None and re.search(r"\b(?:to|at)\s+-?\d", text) and direction is None else _step(rest, direction)
        if number is not None or step is not Step.NONE:
            return _local_intent(Action.VOLUME, track=track, step=step, number=number)
    return None


def _note_target(snapshot: Snapshot, text: str) -> tuple[int | None, int | None]:
    pattern = _track_target_pattern(snapshot, include_master=False)
    match = re.search(rf"(?P<target>{pattern})\s+(?:clip|slot)\s+(?P<slot>\d+)", text, re.IGNORECASE)
    if not match:
        match = re.search(rf"(?:clip|slot)\s+(?P<slot>\d+)\s+(?:on|in)\s+(?P<target>{pattern})", text, re.IGNORECASE)
    if not match:
        return None, None
    track = _track_en(snapshot, match.group("target"))
    return (track if isinstance(track, int) else None), int(match.group("slot")) - 1


def parse_clip_notes_phrase_en(utterance: str, snapshot: Snapshot) -> ClipNotesRequest | None:
    if is_negated_en(utterance):
        return None
    text = normalize_english_phrase(utterance)
    track, slot = _note_target(snapshot, text)
    if slot is not None and slot < 0:
        return None
    if re.search(r"\b(?:double the loop|duplicate loop|twice as long)\b", text):
        return ClipNotesRequest("duplicate_loop", track, slot)
    if re.search(r"\b(?:quantize|quantise)\b", text):
        grid = "1/16"
        grids = (
            (r"\b(?:1/32|32nd|thirty-second)s?\b", "1/32"),
            (r"\b(?:1/16|16th|sixteenth)s?\b", "1/16"),
            (r"\b(?:1/8|8th|eighth)s?\b", "1/8"),
            (r"\b(?:1/4|quarter)s?\b", "1/4"),
        )
        for pattern, value in grids:
            if re.search(pattern, text):
                grid = value
                break
        if re.search(r"\btriplets?\b", text) and grid in {"1/8", "1/16"}:
            grid += "t"
        percent = re.search(r"(\d+)\s*%", text)
        amount = max(0.0, min(1.0, int(percent.group(1)) / 100.0)) if percent else 0.5 if re.search(r"\b(?:lightly|loosely|a bit|a little)\b", text) else 1.0
        return ClipNotesRequest("quantize", track, slot, grid=grid, amount=amount)
    if re.search(r"\b(?:legato|make it legato|connect the notes|fill the gaps)\b", text):
        return ClipNotesRequest("legato", track, slot)
    velocity = re.search(r"\bvelocity\b|\b(?:louder|softer|harder)\s+notes?\b|\bnotes?\s+(?:louder|softer|harder)\b", text)
    if velocity:
        fixed = re.search(r"\bvelocity\s+(?:to|at)\s+(\d+)", text)
        if fixed:
            return ClipNotesRequest("velocity", track, slot, value=float(max(1, min(127, int(fixed.group(1))))))
        small = re.search(r"\b(?:a bit|a little|slightly|a touch|a hair)\b", text) is not None
        if re.search(r"\b(?:up|louder|harder|increase|raise|boost)\b", text):
            return ClipNotesRequest("velocity", track, slot, factor=1.1 if small else 1.25)
        if re.search(r"\b(?:down|softer|decrease|lower|reduce)\b", text):
            return ClipNotesRequest("velocity", track, slot, factor=0.9 if small else 0.8)
        return None
    transpose = re.search(r"\b(?:transpose|shift|move|pitch)\b", text)
    amount = re.search(r"\b(\d+|an?|one|two|three|four)\s*(octaves?|semitones?|half steps?|st)\b", text)
    # 「an octave up」のようにオクターブと向きがあれば、transpose と言わなくてもノートの移調（日本語の「オクターブ上げて」と同じ扱い）
    if amount and (transpose or amount.group(2).startswith("octave") or re.search(r"\b(?:notes?|midi|clip)\b", text)):
        words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4}
        number = int(amount.group(1)) if amount.group(1).isdigit() else words[amount.group(1)]
        count = number * (12 if amount.group(2).startswith("octave") else 1)
        if re.search(r"\bdown\b", text):
            count = -count
        elif not re.search(r"\bup\b", text):
            return None
        return ClipNotesRequest("transpose", track, slot, semitones=count)
    return None
