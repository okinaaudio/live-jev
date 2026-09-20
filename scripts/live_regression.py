#!/usr/bin/env python3
"""Run guarded regression cases against the open Ableton Live set."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, TextIO, Tuple
import uuid


Snapshot = Mapping[str, Any]
Reply = Mapping[str, Any]
Assertion = Callable[[Snapshot, Snapshot, int, Reply], Optional[str]]
PREPARE = (
    "Prepare a test set: create a new empty Live set, add a MIDI track named "
    "LJ-TEST (or the --marker value), add two more MIDI tracks, add at least one return track, "
    "and select one of the non-marker tracks."
)


class SafetyRefusal(Exception):
    pass


class SafetyDrift(Exception):
    pass


@dataclass(frozen=True)
class SafetyContext:
    marker: str
    track_count: int
    marker_index: int
    selected_index: int
    selected_name: str
    other_index: int
    other_name: str
    initial_fingerprint: Tuple[Any, ...]


@dataclass(frozen=True)
class Case:
    utterance: str
    language: str
    expected_kinds: FrozenSet[str] = frozenset({"result"})
    assertion: Optional[Assertion] = None
    restore_utterances: Tuple[str, ...] = ()
    setup_utterances: Tuple[str, ...] = ()
    optional: bool = False
    compound_tolerant: bool = False


@dataclass
class CaseResult:
    status: str
    elapsed_ms: int
    utterance: str
    detail: str
    language: str
    route: str = "local"
    reply: Dict[str, Any] = field(default_factory=dict)
    restore_replies: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class RunReport:
    exit_code: int
    results: List[CaseResult]
    before_fingerprint: Tuple[Any, ...]
    after_fingerprint: Tuple[Any, ...]
    aborted: Optional[str] = None


def _tracks(snapshot: Snapshot) -> Sequence[Mapping[str, Any]]:
    tracks = snapshot.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("snapshot.tracks is not a list")
    return tracks


def fingerprint(snapshot: Snapshot) -> Tuple[Any, ...]:
    answer = []
    for track in _tracks(snapshot):
        mixer = track["mixer"]
        answer.append((
            str(track["name"]), bool(track["mute"]), bool(track["solo"]), bool(track["arm"]),
            # A dB target is reached by bisection, so "0 dB" comes back as 0.850006 instead of 0.85: the same fader position.
            round(float(mixer["volume"]["value"]), 4), round(float(mixer["panning"]["value"]), 4),
            tuple(round(float(value), 4) for value in track["sends"]),
        ))
    return tuple(answer)


def inspect_set(
    read_snapshot: Callable[[], Snapshot],
    read_selected: Callable[[], str],
    marker: str = "LJ-TEST",
    max_tracks: int = 16,
) -> SafetyContext:
    snapshot = read_snapshot()
    tracks = _tracks(snapshot)
    names = [str(track.get("name", "")) for track in tracks]
    markers = [index for index, name in enumerate(names) if name == marker]
    problems = []
    if len(markers) != 1:
        problems.append("the set must contain exactly one track named {!r}".format(marker))
    if len(tracks) > max_tracks:
        problems.append("the set has {} tracks; the limit is {}".format(len(tracks), max_tracks))
    non_marker = [index for index, name in enumerate(names) if name != marker]
    if len(non_marker) < 2:
        problems.append("the set needs at least two non-marker test tracks")
    returns = snapshot.get("returns")
    if not isinstance(returns, list) or not returns:
        problems.append("the set needs at least one return track for Send A")

    selected_path = read_selected()
    match = re.fullmatch(r"live_set tracks ([0-9]+)", selected_path or "")
    selected_index = int(match.group(1)) if match else -1
    if not match or not 0 <= selected_index < len(tracks):
        problems.append("select a regular non-marker test track, not master or a return track")
    elif names[selected_index] == marker:
        problems.append("the marker track is selected; select a non-marker test track")

    if problems:
        raise SafetyRefusal("; ".join(problems) + ". " + PREPARE)
    assert markers and selected_index >= 0
    other_index = next(index for index in non_marker if index != selected_index)
    selected_name = names[selected_index]
    other_name = names[other_index]
    if not selected_name or not other_name:
        raise SafetyRefusal("Test track names cannot be empty. " + PREPARE)
    if names.count(selected_name) != 1 or names.count(other_name) != 1:
        raise SafetyRefusal("Selected test track names must be unique. " + PREPARE)
    return SafetyContext(
        marker, len(tracks), markers[0], selected_index, selected_name, other_index, other_name,
        fingerprint(snapshot),
    )


def _guard(snapshot: Snapshot, context: SafetyContext) -> None:
    tracks = _tracks(snapshot)
    if len(tracks) != context.track_count:
        raise SafetyDrift("track count changed from {} to {}".format(context.track_count, len(tracks)))
    if not any(str(track.get("name", "")) == context.marker for track in tracks):
        raise SafetyDrift("marker track {!r} disappeared".format(context.marker))


def _track(snapshot: Snapshot, index: int) -> Mapping[str, Any]:
    return _tracks(snapshot)[index]


def _field(index: int, name: str, expected: Any) -> Assertion:
    def check(_before: Snapshot, after: Snapshot, _selected: int, _reply: Reply) -> Optional[str]:
        actual = _track(after, index).get(name)
        return None if actual == expected else "{} is {!r}, expected {!r}".format(name, actual, expected)
    return check


def _unchanged(before: Snapshot, after: Snapshot, _selected: int, _reply: Reply) -> Optional[str]:
    return None if fingerprint(before) == fingerprint(after) else "Live state changed"


def _send_value(index: int, send_index: int, expected: float, tolerance: float = 0.015) -> Assertion:
    def check(_before: Snapshot, after: Snapshot, _selected: int, _reply: Reply) -> Optional[str]:
        sends = _track(after, index).get("sends", [])
        if send_index >= len(sends):
            return "Send A is unavailable"
        actual = float(sends[send_index])
        return None if abs(actual - expected) <= tolerance else "Send A is {:.3f}, expected {:.3f}".format(actual, expected)
    return check


def _parse_db(value: Any) -> Optional[float]:
    match = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _db(index: int, expected: float, name: Optional[str] = None) -> Assertion:
    def check(_before: Snapshot, after: Snapshot, _selected: int, reply: Reply) -> Optional[str]:
        track = _track(after, index)
        if name is not None and track.get("name") != name:
            return "value phrase renamed the track"
        display = track["mixer"]["volume"].get("display")
        actual = _parse_db(display)
        if actual is None:
            actual = _parse_db((reply.get("decision") or {}).get("after"))
        return None if actual is not None and abs(actual - expected) <= 0.2 else "volume is {!r}, expected {:.1f} dB".format(display, expected)
    return check


def _db_delta(index: int, delta: float) -> Assertion:
    def check(before: Snapshot, after: Snapshot, _selected: int, reply: Reply) -> Optional[str]:
        old = _parse_db(_track(before, index)["mixer"]["volume"].get("display"))
        new = _parse_db(_track(after, index)["mixer"]["volume"].get("display"))
        if new is None:
            new = _parse_db((reply.get("decision") or {}).get("after"))
        if old is None or new is None:
            return "could not read dB display"
        return None if abs((new - old) - delta) <= 0.2 else "dB delta is {:.2f}, expected {:.2f}".format(new - old, delta)
    return check


def _pan(index: int, predicate: Callable[[float], bool], label: str) -> Assertion:
    def check(_before: Snapshot, after: Snapshot, _selected: int, _reply: Reply) -> Optional[str]:
        actual = float(_track(after, index)["mixer"]["panning"]["value"])
        return None if predicate(actual) else "pan is {:.3f}, expected {}".format(actual, label)
    return check


def _both(index: int, mute: bool, solo: bool) -> Assertion:
    def check(_before: Snapshot, after: Snapshot, _selected: int, _reply: Reply) -> Optional[str]:
        track = _track(after, index)
        return None if bool(track["mute"]) == mute and bool(track["solo"]) == solo else "compound change was incomplete"
    return check


def _all_muted(except_index: Optional[int] = None) -> Assertion:
    def check(_before: Snapshot, after: Snapshot, _selected: int, _reply: Reply) -> Optional[str]:
        wrong = [
            str(track.get("name", index)) for index, track in enumerate(_tracks(after))
            if index != except_index and not bool(track.get("mute"))
        ]
        return None if not wrong else "tracks were not muted: " + ", ".join(wrong)
    return check


def _boolean_restore(name: str, language: str, field_name: str, value: bool) -> str:
    if language == "ja":
        words = {"mute": ("ミュート解除", "ミュート"), "solo": ("ソロ解除", "ソロ"), "arm": ("アーム解除", "アーム")}
        return "{}を{}して".format(name, words[field_name][1 if value else 0])
    words = {"mute": ("unmute", "mute"), "solo": ("unsolo", "solo"), "arm": ("disarm", "arm")}
    return "{} {}".format(words[field_name][1 if value else 0], name)


def _volume_restore(name: str, snapshot: Snapshot, index: int) -> str:
    display = str(_track(snapshot, index)["mixer"]["volume"].get("display", ""))
    value = _parse_db(display)
    if value is None:
        return ""
    return "set {} to {:.2f} dB".format(name, value)


def _pan_restore(snapshot: Snapshot, index: int) -> str:
    value = float(_track(snapshot, index)["mixer"]["panning"]["value"])
    if abs(value) < 0.005:
        return "center pan"
    return "pan {} {} {}".format("right" if value > 0 else "left", abs(round(value * 100)), "percent")


def build_cases(context: SafetyContext, baseline: Snapshot) -> List[Case]:
    s, o = context.selected_index, context.other_index
    sn, on = context.selected_name, context.other_name
    selected = _track(baseline, s)
    other = _track(baseline, o)
    cases: List[Case] = []
    for text, lang, index, field_name, value in (
        ("mute it", "en", s, "mute", True), ("unmute it", "en", s, "mute", False),
        ("このトラックをミュート", "ja", s, "mute", True), ("このトラックをミュート解除", "ja", s, "mute", False),
        ("solo it", "en", s, "solo", True), ("unsolo it", "en", s, "solo", False),
        ("このトラックをソロ", "ja", s, "solo", True), ("このトラックをソロ解除", "ja", s, "solo", False),
        ("arm it", "en", s, "arm", True), ("disarm it", "en", s, "arm", False),
        ("このトラックをアーム", "ja", s, "arm", True), ("このトラックをアーム解除", "ja", s, "arm", False),
        ("mute " + on, "en", o, "mute", True), ("unmute " + on, "en", o, "mute", False),
        (on + "をミュート", "ja", o, "mute", True), (on + "をミュート解除", "ja", o, "mute", False),
        ("solo " + on, "en", o, "solo", True), ("unsolo " + on, "en", o, "solo", False),
        ("arm " + on, "en", o, "arm", True), ("disarm " + on, "en", o, "arm", False),
    ):
        name = str(_track(baseline, index)["name"])
        restore = _boolean_restore(name, lang, field_name, bool(_track(baseline, index)[field_name]))
        # Turning something off is a no-op ("already in that state") unless it is on first; every case restores after itself.
        setup = () if value else (_boolean_restore(name, lang, field_name, True),)
        cases.append(Case(text, lang, assertion=_field(index, field_name, value), restore_utterances=(restore,), setup_utterances=setup))

    initial_send = float(selected["sends"][0])
    send_restore = ("set send A to {:.6f}%".format(initial_send * 100),)
    cases.extend([
        Case("SendAを100%にして", "ja", assertion=_send_value(s, 0, 1.0), restore_utterances=send_restore),
        Case("set send A to 50%", "en", assertion=_send_value(s, 0, 0.5), restore_utterances=send_restore),
        # Setting a value the track already has is a no-op ("already in that state"), so move it away first.
        Case("センドAを0にして", "ja", assertion=_send_value(s, 0, 0.0), restore_utterances=send_restore, setup_utterances=("set send A to 50%",)),
    ])
    s_volume = _volume_restore(sn, baseline, s)
    o_volume = _volume_restore(on, baseline, o)
    cases.extend([
        Case("3dB下げて", "ja", assertion=_db_delta(s, -3.0), restore_utterances=(s_volume,) if s_volume else ()),
        Case("3dB上げて", "ja", assertion=_db_delta(s, 3.0), restore_utterances=(s_volume,) if s_volume else ()),
        Case(on + "を-6dBにして", "ja", assertion=_db(o, -6.0, on), restore_utterances=(o_volume,) if o_volume else ()),
        Case(on + "を0dBにして", "ja", assertion=_db(o, 0.0, on), restore_utterances=(o_volume,) if o_volume else ()),
    ])
    pan_restore = (_pan_restore(baseline, s),)
    all_mute_restore = tuple(
        _boolean_restore(str(track["name"]), "en", "mute", bool(track["mute"]))
        for track in _tracks(baseline)
    )
    cases.extend([
        Case("pan right", "en", assertion=_pan(s, lambda value: value > 0.0, "right"), restore_utterances=pan_restore),
        Case("少し左", "ja", assertion=_pan(s, lambda value: value < 0.0, "left"), restore_utterances=pan_restore),
        Case("パンを真ん中に", "ja", assertion=_pan(s, lambda value: abs(value) <= 0.01, "center"), restore_utterances=pan_restore, setup_utterances=("pan right",)),
        Case("もう少し", "ja", assertion=_pan(s, lambda value: value < -0.1, "further left"), restore_utterances=pan_restore, setup_utterances=("少し左",)),
        Case("元に戻して", "ja", assertion=_field(s, "mute", bool(selected["mute"])), restore_utterances=(_boolean_restore(sn, "ja", "mute", bool(selected["mute"])),), setup_utterances=(_boolean_restore(sn, "ja", "mute", not bool(selected["mute"])),)),
    ])
    for text, lang in (
        ("Ghostをミュート", "ja"), ("ボーカルをソロ", "ja"), ("mute Ghost", "en"), ("mute 909", "en"),
        ("Ghostに Diva を入れて", "ja"), ("insert Diva on Ghost", "en"),
        ("Ghostのクリップをクオンタイズ", "ja"), ("turn Reverb off on Ghost", "en"),
    ):
        cases.append(Case(text, lang, frozenset({"error", "ask", "info"}), _unchanged))
    cases.extend([
        Case("名前をLJ-TMPにして", "ja", assertion=_field(s, "name", "LJ-TMP"), restore_utterances=("名前を{}にして".format(sn),)),
        Case("mute it and solo it", "en", frozenset({"result", "info"}), _both(s, True, True),
             (_boolean_restore(sn, "en", "mute", bool(selected["mute"])), _boolean_restore(sn, "en", "solo", bool(selected["solo"]))), compound_tolerant=True),
        Case("全部ミュート", "ja", frozenset({"result", "ask", "info", "error"}), _all_muted(), all_mute_restore, optional=True),
        Case(sn + "以外をミュート", "ja", frozenset({"result", "ask", "info", "error"}), _all_muted(s), all_mute_restore, optional=True),
        Case("mute all", "en", frozenset({"result", "ask", "info", "error"}), _all_muted(), all_mute_restore, optional=True),
    ])
    # Issue #8: the side word stood away from the number and the pan went to the opposite side.
    cases.extend([
        Case("pan left by 20", "en", assertion=_pan(s, lambda value: value < -0.05, "left"), restore_utterances=pan_restore),
        Case("pan 20 percent left", "en", assertion=_pan(s, lambda value: value < -0.05, "left"), restore_utterances=pan_restore),
        Case("pan right by 15", "en", assertion=_pan(s, lambda value: value > 0.05, "right"), restore_utterances=pan_restore),
        Case("パンを左に30", "ja", assertion=_pan(s, lambda value: value < -0.05, "left"), restore_utterances=pan_restore),
    ])
    o_mute_restore = _boolean_restore(on, "ja", "mute", bool(other["mute"]))
    unchanged_after_undo = (o_mute_restore,) + ((o_volume,) if o_volume else ())
    cases.extend([
        # A named track plus a dB amount was once refused locally as an unknown track.
        Case(on + "を3dB下げて", "ja", assertion=_db_delta(o, -3.0), restore_utterances=(o_volume,) if o_volume else ()),
        Case("lower " + on + " by 2 dB", "en", assertion=_db_delta(o, -2.0), restore_utterances=(o_volume,) if o_volume else ()),
        # Undo of a chain and of a multi-track operation must bring everything back, faders included.
        Case("元に戻して", "ja", assertion=_unchanged, restore_utterances=unchanged_after_undo,
             setup_utterances=(on + "をミュートして3dB下げて",)),
        Case("元に戻して", "ja", assertion=_unchanged, restore_utterances=all_mute_restore, setup_utterances=("全部ミュート",)),
        Case("undo that", "en", assertion=_unchanged, restore_utterances=unchanged_after_undo,
             setup_utterances=("mute " + on + ", then solo " + on,)),
    ])
    return cases


def _poll(
    assertion: Assertion, before: Snapshot, selected_index: int, reply: Reply,
    read_snapshot: Callable[[], Snapshot], sleeper: Callable[[float], None], timeout: float = 1.5,
) -> Tuple[Snapshot, Optional[str]]:
    deadline = time.monotonic() + timeout
    last = read_snapshot()
    detail = assertion(before, last, selected_index, reply)
    while detail is not None and time.monotonic() < deadline:
        sleeper(0.05)
        last = read_snapshot()
        detail = assertion(before, last, selected_index, reply)
    return last, detail


def _percentile(values: Sequence[int], percentile: float) -> Optional[int]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def summarize(results: Sequence[CaseResult]) -> Dict[str, Any]:
    counts = {name: sum(result.status == name for result in results) for name in ("PASS", "FAIL", "SKIP")}
    latency: Dict[str, Any] = {}
    for route in ("local", "jev"):
        values = [result.elapsed_ms for result in results if result.route == route]
        latency[route] = {"count": len(values), "p50": _percentile(values, 0.50), "p95": _percentile(values, 0.95)}
    return {"counts": counts, "latency_ms": latency}


def run_regression(
    cases: Sequence[Case], *, context: SafetyContext,
    read_snapshot: Callable[[], Snapshot], read_selected: Callable[[], str], send: Callable[[str], Reply],
    output: TextIO = sys.stdout, sleeper: Callable[[float], None] = time.sleep,
) -> RunReport:
    results: List[CaseResult] = []
    before_fp = context.initial_fingerprint
    aborted = None

    def safe_send(text: str) -> Reply:
        current = read_snapshot()
        _guard(current, context)
        selected = read_selected()
        if selected != "live_set tracks {}".format(context.selected_index):
            raise SafetyDrift("selected track changed; no further utterances were sent")
        return send(text)

    try:
        _guard(read_snapshot(), context)  # Repeat immediately before the first utterance.
        for case in cases:
            case_before = read_snapshot()
            _guard(case_before, context)
            started = time.monotonic()
            reply: Reply = {}
            detail = ""
            status = "FAIL"
            restore_replies: List[Dict[str, Any]] = []
            dirty = False
            interrupted: BaseException | None = None
            try:
                for setup in case.setup_utterances:
                    dirty = True
                    safe_send(setup)
                dirty = True
                reply = safe_send(case.utterance)
                kind = str(reply.get("kind", ""))
                elapsed = int((time.monotonic() - started) * 1000)
                elapsed = int((reply.get("ms") or {}).get("total", elapsed))
                after = read_snapshot()
                unchanged = fingerprint(case_before) == fingerprint(after)
                if case.optional and kind in {"ask", "info", "error"} and unchanged:
                    status, detail = "SKIP", "optional feature did not write"
                elif kind not in case.expected_kinds:
                    detail = "reply kind {!r}, expected {}".format(kind, sorted(case.expected_kinds))
                elif case.compound_tolerant and kind == "info" and unchanged:
                    status, detail = "PASS", "daemon accepted one command at a time"
                elif case.assertion is None:
                    status, detail = "PASS", str(reply.get("line", "ok"))
                else:
                    _after, error = _poll(case.assertion, case_before, context.selected_index, reply, read_snapshot, sleeper)
                    if error is None:
                        status, detail = "PASS", str(reply.get("line", "state verified"))
                    else:
                        detail = error
            except SafetyDrift:
                raise
            except BaseException as error:
                elapsed = int((time.monotonic() - started) * 1000)
                detail = "{}: {}".format(type(error).__name__, error)
                interrupted = error
            finally:
                if dirty:
                    for restore in case.restore_utterances:
                        if not restore:
                            continue
                        try:
                            restore_replies.append(dict(safe_send(restore)))
                        except SafetyDrift:
                            raise
                        except Exception as error:
                            status = "FAIL"
                            detail += "; restore failed: {}".format(error)
                    try:
                        _restored, restore_error = _poll(_unchanged, case_before, context.selected_index, {}, read_snapshot, sleeper)
                    except Exception as error:
                        restore_error = "restore verification failed: {}".format(error)
                    if restore_error is not None:
                        status = "FAIL"
                        detail += "; restore mismatch: " + restore_error
            if interrupted is not None:
                raise interrupted
            ms = int((reply.get("ms") or {}).get("total", int((time.monotonic() - started) * 1000)))
            route = "jev" if int((reply.get("ms") or {}).get("jev", 0) or 0) > 0 else "local"
            result = CaseResult(status, ms, case.utterance, detail.strip("; "), case.language, route, dict(reply), restore_replies)
            results.append(result)
            print("{}  {}  {}  {}".format(status, ms, case.utterance, result.detail), file=output, flush=True)
    except SafetyDrift as error:
        aborted = str(error)
        print("FAIL  0  SAFETY  {}".format(aborted), file=output, flush=True)

    try:
        after_fp = fingerprint(read_snapshot())
    except Exception:
        after_fp = ()
    if aborted is not None:
        exit_code = 3
    elif after_fp != before_fp:
        results.append(CaseResult("FAIL", 0, "FINAL FINGERPRINT", "before and after differ", "en"))
        print("FAIL  0  FINAL FINGERPRINT  before and after differ", file=output, flush=True)
        exit_code = 1
    else:
        exit_code = 1 if any(result.status == "FAIL" for result in results) else 0
    summary = summarize(results)
    print("SUMMARY  PASS={PASS} FAIL={FAIL} SKIP={SKIP}".format(**summary["counts"]), file=output)
    for route in ("local", "jev"):
        item = summary["latency_ms"][route]
        p50 = "n/a" if item["p50"] is None else str(item["p50"])
        p95 = "n/a" if item["p95"] is None else str(item["p95"])
        print("{}  n={} p50={}ms p95={}ms".format(route.upper(), item["count"], p50, p95), file=output)
    return RunReport(exit_code, results, before_fp, after_fp, aborted)


def run_injected(
    *, read_snapshot: Callable[[], Snapshot], read_selected: Callable[[], str],
    send: Callable[[str], Reply], marker: str = "LJ-TEST", max_tracks: int = 16,
    cases: Optional[Sequence[Case]] = None, output: TextIO = sys.stdout,
    sleeper: Callable[[float], None] = time.sleep,
) -> RunReport:
    """Run through injected boundaries. Unit tests use this without opening sockets."""
    try:
        context = inspect_set(read_snapshot, read_selected, marker, max_tracks)
    except SafetyRefusal as error:
        print("REFUSED: {}".format(error), file=output)
        return RunReport(2, [], (), (), str(error))
    baseline = read_snapshot()
    chosen = list(cases) if cases is not None else build_cases(context, baseline)
    return run_regression(
        chosen, context=context, read_snapshot=read_snapshot, read_selected=read_selected,
        send=send, output=output, sleeper=sleeper,
    )


class LiveSocket:
    def __init__(self, host: str = "127.0.0.1", port: int = 9140, timeout: float = 5.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout

    def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as connection:
            connection.settimeout(self.timeout)
            connection.sendall(data)
            chunks = bytearray()
            while b"\n" not in chunks:
                part = connection.recv(65536)
                if not part:
                    raise RuntimeError("Live Remote Script closed the connection")
                chunks.extend(part)
        answer = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            raise RuntimeError("Live Remote Script rejected the request")
        return answer

    def read_snapshot(self) -> Snapshot:
        answer = self.request({"action": "snapshot"})
        snapshot = answer.get("snapshot")
        if not isinstance(snapshot, dict):
            raise RuntimeError("Live returned an invalid snapshot")
        return snapshot

    def read_selected(self) -> str:
        request_id = uuid.uuid4().hex
        answer = self.request({"action": "bridge", "request": request_id, "ops": [{"op": "session_context"}]})
        try:
            return str(answer["results"][0]["value"]["selected"]["track"]["path"])
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("Live returned an invalid session_context") from error


class DaemonProcess:
    def __init__(self, python: str, root: Path, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.counter = 0
        self.process = subprocess.Popen(
            [python, "daemon.py"], cwd=str(root), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        deadline = time.monotonic() + timeout
        while True:
            status = self._read(max(0.0, deadline - time.monotonic()))
            if status.get("kind") == "status":
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("daemon did not emit its startup status")

    def _read(self, timeout: float) -> Dict[str, Any]:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("daemon reply timed out")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("daemon exited")
        answer = json.loads(line)
        if not isinstance(answer, dict):
            raise RuntimeError("daemon returned invalid JSON")
        return answer

    def send(self, text: str) -> Reply:
        self.counter += 1
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"id": str(self.counter), "text": text}, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        return self._read(self.timeout)

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            assert self.process.stdin is not None
            self.process.stdin.write('{"id":"q","cmd":"quit"}\n')
            self.process.stdin.flush()
            self.process.wait(timeout=1.0)
        except Exception:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.process.kill()


def _write_json(path: str, report: RunReport, context: SafetyContext) -> None:
    payload = {
        "exit_code": report.exit_code,
        "safety": asdict(context),
        "results": [asdict(result) for result in report.results],
        "summary": summarize(report.results),
        "before_fingerprint": report.before_fingerprint,
        "after_fingerprint": report.after_fingerprint,
        "aborted": report.aborted,
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--marker", default="LJ-TEST")
    parser.add_argument("--max-tracks", type=int, default=16)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", metavar="PATH")
    parser.add_argument("--only", metavar="SUBSTR")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)
    live = LiveSocket()
    try:
        context = inspect_set(live.read_snapshot, live.read_selected, args.marker, args.max_tracks)
        baseline = live.read_snapshot()
    except (SafetyRefusal, OSError, RuntimeError, ValueError, KeyError) as error:
        print("REFUSED: {}".format(error), file=sys.stderr)
        return 2
    cases = build_cases(context, baseline)
    if args.only:
        cases = [case for case in cases if args.only.lower() in case.utterance.lower()]
    if args.check:
        print("Safety check passed. Selected={!r}, other={!r}, tracks={}.".format(
            context.selected_name, context.other_name, context.track_count))
        for case in cases:
            print("WOULD RUN  " + case.utterance)
        return 0
    daemon: Optional[DaemonProcess] = None
    try:
        daemon = DaemonProcess(args.python, Path(__file__).resolve().parents[1])
        report = run_regression(
            cases, context=context, read_snapshot=live.read_snapshot,
            read_selected=live.read_selected, send=daemon.send,
        )
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print("FAIL: {}".format(error), file=sys.stderr)
        return 1
    finally:
        if daemon is not None:
            daemon.close()
    if args.json:
        _write_json(args.json, report, context)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
