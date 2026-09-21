#!/opt/homebrew/bin/python3.13
"""Live Jev background service using newline-delimited JSON over stdin and stdout."""

from __future__ import annotations

import argparse
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
import http.client
import inspect
import json
import math
import os
from pathlib import Path
import queue
import re
import shlex
import socket
import sys
import threading
import time
import traceback
from typing import Any, Callable, Literal, Mapping, Union
from urllib.parse import urlsplit

from actions import ACTIONS, DISPATCH_ONLY_ACTIONS, request_id, beats_to_bar, MONITOR_NAMES, target_capability_error, target_for_intent
from bridge_client import Ack, BridgeClient, BridgeError, BridgeResult, NATIVE_DEVICES, ack_map, make_bridge_client
import plugin_script
from intent import NAMED_TRACK_CONF_MIN, TRACK_STATED_MIN, TRACK_UNSTATED_MAX, ClipNotesRequest, parse_clip_notes_phrase, PluginRequest, extract_plugin_request, plugin_intent, plugin_name_candidates, resolve_exact_plugin_name, resolve_plugin_name, resolve_native_device, resolve_device_request_name, GENERIC_DEVICE_WORDS, ACTION_LABELS, Action, Intent, IntentResult, Number, Step, TargetOrigin, _local_intent, build_request, candidate_params, interpret_response, parse_local, split_compound, eligible_track_indices, MULTI_TOGGLE_ACTIONS, MULTI_TARGET_ORIGINS
from llm_rewrite import GeminiRewriter, RewriteFailure
from messages import LocalizedError, action_label, contains_japanese, render, resolve_language, step_label, using_language
from snapshot import Param, ReturnTrack, Snapshot, TargetKind, TargetRef, Track, addressable_targets, build_snapshot, is_bridge_track, replace_param, replace_target, replace_track, target_ref, Clip


API_URL = "https://api.typesafe.ai/v1/systemone"
ERROR_LINE = "Liveに繋がりません。装置が載っているか確認してください"
MIN_SCRIPT_VERSION = "0.18"


def _version_is_older(version: str, minimum: str) -> bool:
    try:
        current = tuple(int(part) for part in version.split("."))
        required = tuple(int(part) for part in minimum.split("."))
    except (AttributeError, TypeError, ValueError):
        return True
    return current < required


class WriteResultUnknown(RuntimeError):
    pass


def strip_zsh_comment(value: str) -> str:
    quote = None
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
            continue
        if character == "#" and quote is None and index > 0 and value[index - 1].isspace():
            return value[:index].rstrip()
    return value.strip()


def read_key(variable: str = "TYPESAFE_API_KEY") -> str | None:
    key = os.environ.get(variable, "").strip()
    if key:
        return key
    if variable == "GEMINI_API_KEY":
        return None
    # Apps launched from Finder do not inherit shell environment variables, so also read export lines from shell configuration files.
    lines: list[str] = []
    for name in (".zshenv", ".zprofile", ".zshrc", ".bash_profile", ".bashrc", ".profile"):
        try:
            lines.extend((Path.home() / name).read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError):
            continue
    if not lines:
        return None
    pattern = re.compile(rf"^\s*export\s+{re.escape(variable)}\s*=(.*)$")
    found_key = None
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        try:
            parts = shlex.split(strip_zsh_comment(match.group(1)), comments=False, posix=True)
        except ValueError:
            continue
        if not parts:
            found_key = None
        elif len(parts) == 1 and parts[0].strip():
            found_key = parts[0].strip()
    return found_key


class JevRequestTooLarge(RuntimeError):
    pass


class JevClient:
    def __init__(self, url: str = API_URL, timeout: float = 5.0) -> None:
        parsed = urlsplit(url)
        self.host = parsed.hostname or ""
        self.port = parsed.port
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self.connection: http.client.HTTPSConnection | None = None

    def _connect(self) -> http.client.HTTPSConnection:
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout)
        return self.connection

    def _discard_connection(self) -> None:
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None

    def __call__(self, payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                connection = self._connect()
                connection.request("POST", self.path, body=body, headers=headers)
                response = connection.getresponse()
                raw = response.read()
                if not 200 <= response.status < 300:
                    if response.status == 400:
                        try:
                            error = json.loads(raw.decode("utf-8"))
                        except (ValueError, UnicodeError, json.JSONDecodeError):
                            error = None
                        if isinstance(error, Mapping):
                            detail = error.get("detail")
                            if isinstance(detail, Mapping) and detail.get("error_type") == "max_tokens_exceeded":
                                self._discard_connection()
                                raise JevRequestTooLarge()
                    raise http.client.HTTPException(f"Jev HTTP {response.status}")
                decoded = json.loads(raw.decode("utf-8"))
                if not isinstance(decoded, Mapping) or not isinstance(decoded.get("answers"), Mapping):
                    raise ValueError("invalid Jev response")
                return decoded
            except (
                http.client.HTTPException,
                TimeoutError,
                socket.timeout,
                OSError,
                ValueError,
                UnicodeError,
                json.JSONDecodeError,
            ) as error:
                last_error = error
                self._discard_connection()
        raise RuntimeError("Jevに繋がりません") from last_error


_DEFAULT_JEV = JevClient()
_REPROCESS_PENDING = object()


def request_jev(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    return _DEFAULT_JEV(payload, key)


def _find(result: BridgeResult, request: str) -> Ack:
    ack = ack_map(result).get(request)
    if ack is None:
        raise BridgeError("必要な応答がありません")
    return ack


def _require_readback(result: BridgeResult, event: str, property_name: str | None = None) -> None:
    found = any(
        ack.event == event and (property_name is None or ack.property == property_name)
        for ack in result.acks
    )
    if result.timed_out or not found:
        raise WriteResultUnknown("書き込み結果が不明です。現在値を読み戻せませんでした")


class SnapshotReader:
    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self.skipped_devices: list[str] = []

    def read(self) -> tuple[Snapshot, int]:
        elapsed = 0
        self.skipped_devices = []

        read_snapshot = getattr(self.bridge, "read_snapshot", None)
        if callable(read_snapshot):
            return read_snapshot()

        def required(arguments: list[str], request: str, final: bool = False) -> Ack:
            nonlocal elapsed
            result = self.bridge.run(arguments)
            elapsed += result.elapsed_ms
            ack = ack_map(result).get(request)
            if result.timed_out or ack is None:
                message = "Liveの状態を最後まで読めません" if final else "必要な応答がありません"
                raise BridgeError(message)
            return ack

        context_id = request_id("context")
        children_id = request_id("tracks")
        devices_id = request_id("devices")
        context = required(["--api-session-context", context_id], context_id).payload
        children = required(["--api-children", "live_set", "tracks", children_id], children_id).payload
        devices = required(["--api-device-list", "all", devices_id], devices_id).payload
        if not isinstance(context, Mapping) or not isinstance(children, list) or not isinstance(devices, Mapping):
            raise BridgeError("Liveの状態を読めません")

        names: dict[int, str] = {}
        mixers: dict[str, Mapping[str, Any]] = {}
        mute: dict[int, bool] = {}
        solo: dict[int, bool] = {}
        parameters: dict[str, Mapping[str, Any]] = {}
        displays: dict[str, str] = {}
        for child in children:
            if not isinstance(child, Mapping):
                continue
            index = int(child.get("index", len(names)))
            path = str(child.get("path") or f"live_set tracks {index}")
            name_id = request_id("name")
            mixer_id = request_id("mixer")
            mute_id = request_id("mute")
            solo_id = request_id("solo")
            names[index] = str(required(["--api-get", path, "name", name_id], name_id, True).payload)
            mixer = required(["--api-mixer-status", str(index), mixer_id], mixer_id, True).payload
            if isinstance(mixer, Mapping):
                mixers[path] = mixer
            mute[index] = bool(required(["--api-get", path, "mute", mute_id], mute_id, True).payload)
            solo[index] = bool(required(["--api-get", path, "solo", solo_id], solo_id, True).payload)
        master_id = request_id("mixer")
        master = required(["--api-mixer-status", "master", master_id], master_id, True).payload
        if isinstance(master, Mapping):
            mixers["live_set master_track"] = master
        tracks_payload = devices.get("tracks")
        for track_payload in tracks_payload if isinstance(tracks_payload, list) else []:
            raw_devices = track_payload.get("devices") if isinstance(track_payload, Mapping) else []
            for device in raw_devices if isinstance(raw_devices, list) else []:
                if not isinstance(device, Mapping) or not device.get("path"):
                    continue
                path = str(device["path"])
                parameter_id = request_id("params")
                detail_result = self.bridge.run(["--api-device-parameters", path, parameter_id])
                elapsed += detail_result.elapsed_ms
                detail_ack = ack_map(detail_result).get(parameter_id)
                if detail_ack is not None and isinstance(detail_ack.payload, Mapping):
                    parameters[path] = detail_ack.payload
                else:
                    parameters[path] = {"parameters": []}
                    self.skipped_devices.append(path)
        for path, mixer in mixers.items():
            raw_parameters = mixer.get("parameters") if isinstance(mixer, Mapping) else None
            if not isinstance(raw_parameters, Mapping):
                continue
            parameter_names = ("volume",) if path == "live_set master_track" else ("volume", "panning")
            for parameter_name in parameter_names:
                raw_parameter = raw_parameters.get(parameter_name)
                if not isinstance(raw_parameter, Mapping):
                    continue
                parameter_path = f"{path} mixer_device {parameter_name}"
                display_id = request_id("display")
                display_result = self.bridge.run([
                    "--write",
                    "--api-call", parameter_path, "str_for_value",
                    json.dumps([float(raw_parameter.get("value", 0.0))]), display_id,
                ])
                elapsed += display_result.elapsed_ms
                shown = ack_map(display_result).get(display_id)
                if shown is not None:
                    displays[parameter_path] = str(shown.payload)
        scenes_id = request_id("scenes")
        scenes_result = self.bridge.run(["--api-children", "live_set", "scenes", scenes_id])
        elapsed += scenes_result.elapsed_ms
        scenes_ack = ack_map(scenes_result).get(scenes_id)
        scenes = scenes_ack.payload if scenes_ack is not None and isinstance(scenes_ack.payload, list) else []
        clips: dict[int, list[Clip]] = {}
        for child in children:
            if not isinstance(child, Mapping):
                continue
            index = int(child.get("index", 0))
            path = str(child.get("path") or f"live_set tracks {index}")
            slots_id = request_id("slots")
            slots_result = self.bridge.run(["--api-children", path, "clip_slots", slots_id])
            elapsed += slots_result.elapsed_ms
            slots_ack = ack_map(slots_result).get(slots_id)
            slots = slots_ack.payload if slots_ack is not None and isinstance(slots_ack.payload, list) else []
            found: list[Clip] = []
            for slot in slots[:MAX_CLIP_SLOTS]:
                if not isinstance(slot, Mapping):
                    continue
                slot_index = int(slot.get("index", len(found)))
                slot_path = str(slot.get("path") or f"{path} clip_slots {slot_index}")
                has_id = request_id("has_clip")
                has_result = self.bridge.run(["--api-get", slot_path, "has_clip", has_id])
                elapsed += has_result.elapsed_ms
                has_ack = ack_map(has_result).get(has_id)
                raw = has_ack.payload if has_ack is not None else 0
                if isinstance(raw, list) and raw:
                    raw = raw[-1]
                if not bool(raw):
                    continue
                name_id = request_id("clip_name")
                name_result = self.bridge.run(["--api-get", f"{slot_path} clip", "name", name_id])
                elapsed += name_result.elapsed_ms
                name_ack = ack_map(name_result).get(name_id)
                clip_name = name_ack.payload if name_ack is not None else ""
                if isinstance(clip_name, list) and clip_name:
                    clip_name = clip_name[-1]
                found.append(Clip(slot=slot_index, name=str(clip_name or f"Clip {slot_index + 1}"), path=f"{slot_path} clip"))
            clips[index] = found
        returns_id = request_id("returns")
        returns_result = self.bridge.run(["--api-children", "live_set", "return_tracks", returns_id])
        elapsed += returns_result.elapsed_ms
        returns_ack = ack_map(returns_result).get(returns_id)
        returns_raw = returns_ack.payload if returns_ack is not None and isinstance(returns_ack.payload, list) else []
        returns = [str(item.get("name") or f"Return {position + 1}") for position, item in enumerate(returns_raw) if isinstance(item, Mapping)]
        sends: dict[int, list[float]] = {}
        for child in children:
            if not isinstance(child, Mapping):
                continue
            index = int(child.get("index", 0))
            path = str(child.get("path") or f"live_set tracks {index}")
            values: list[float] = []
            for send_index in range(len(returns)):
                send_id = request_id("send")
                send_result = self.bridge.run(["--api-get", f"{path} mixer_device sends {send_index}", "value", send_id])
                elapsed += send_result.elapsed_ms
                send_ack = ack_map(send_result).get(send_id)
                raw = send_ack.payload if send_ack is not None else 0.0
                if isinstance(raw, list) and raw:
                    raw = raw[-1]
                try:
                    values.append(float(raw))
                except (TypeError, ValueError):
                    values.append(0.0)
            sends[index] = values
        snapshot = build_snapshot(context, children, names, mixers, devices, parameters, mute, solo, displays, scenes=scenes, clips=clips, returns=returns, sends=sends)
        return snapshot, elapsed


@dataclass
class Pending:
    result: IntentResult
    field: str
    created: float = field(default_factory=time.monotonic)
    request_id: Any = None
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReceiptEntry:
    path: str
    prop: str
    owner: str
    before: float | bool | str | int
    after: float | bool | str | int
    parameter: bool = False
    device_owner: str | None = None
    write_kind: str = "set"


@dataclass(frozen=True)
class Receipt:
    action: Action
    track: int | None | Literal["master"]
    param: Param | None
    before: float | bool
    after: float | bool
    step: Step
    confirmed: bool
    clip: int | None = None
    scene: int | None = None
    send: int | None = None
    device: Any = None
    target_origin: TargetOrigin = TargetOrigin.NONE
    tracks: tuple[int, ...] = ()
    multi_before: tuple[tuple[int, str, bool], ...] = ()
    utterance: str = ""
    entries: tuple[ReceiptEntry, ...] = ()


PreviousIntent = Receipt


@dataclass(frozen=True)
class PreviousChain:
    items: tuple[Receipt, ...]
    clauses: tuple[str, ...]


@dataclass
class Transaction:
    pairs: list[tuple[str, Receipt]] = field(default_factory=list)

    def register(self, clause: str, receipt: Receipt | None) -> None:
        if receipt is not None:
            self.pairs.append((clause, receipt))


def _normalize(text: str) -> str:
    return "".join(text.casefold().split())


MONITOR_ACTIONS = {0: Action.MONITOR_IN, 1: Action.MONITOR_AUTO, 2: Action.MONITOR_OFF}
MAX_CLIP_SLOTS = 64
REQUIRE_CONFIRM = os.environ.get("LIVE_JEV_CONFIRM", "0") == "1"  # Actions need no confirmation by default. Set LIVE_JEV_CONFIRM=1 to require it.
PLUGIN_CATALOG_PATH = Path(__file__).with_name("plugins.json")
PLUGIN_HINT = re.compile(r"トラック|挿|差|入れ|いれ|載せ|のせ|開|立ち上げ|起動|プラグイン|シンセ|音源|エフェクト|読み込|ロード|インサート|使|\b(?:track|insert|add|load|open|put|drop|place|plugin|plug-in|synth|instrument|effect|use|apply|launch)\b", re.IGNORECASE)
STRONG_NEGATION = re.compile(r"ないで|しなくて|するな|不要|いらない|要らない|禁止|\b(?:don't|do not|never|no need|not necessary)\b", re.IGNORECASE)
SNAPSHOT_TRUST_SECONDS = 10.0  # Assume the song structure is unchanged if the snapshot was taken or checked within this interval.
PENDING_TTL_SECONDS = 30.0
LLM_DEADLINE_SECONDS = 2.0
LLM_ATTEMPT_TTL_SECONDS = 60.0
LLM_ATTEMPT_LIMIT = 128
LLM_RATE_LIMIT = 5
LLM_RATE_WINDOW_SECONDS = 60.0
PLUGIN_LOAD_WAIT_SECONDS = 25.0  # Large instruments such as Omnisphere and Kontakt can take more than 10 seconds to load.


def load_plugin_catalog() -> tuple[str, ...]:
    try:
        raw = json.loads(PLUGIN_CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    plugins = raw.get("plugins") if isinstance(raw, Mapping) else None
    return tuple(str(item.get("name")) for item in plugins if isinstance(item, Mapping) and item.get("name")) if isinstance(plugins, list) else ()
RETRY_READ_KINDS = frozenset({"transport", "song_bool", "jump", "track_bool", "track_int", "clip_prop"})


def _matches_expected(payload: Any, expected: float | bool) -> bool:
    value = payload[-1] if isinstance(payload, list) and payload else payload
    try:
        if isinstance(expected, bool):
            return bool(value) is expected
        if isinstance(expected, int):
            return int(float(value)) == expected
        return abs(float(value) - float(expected)) < 1e-6
    except (TypeError, ValueError):
        return False


def _is_change_batch(arguments: list[str]) -> bool:
    if any(flag in arguments for flag in ("--api-parameter-set", "--api-set", "--tempo")):
        return True
    if "--api-call" not in arguments:
        return False
    call_at = arguments.index("--api-call")
    return arguments[call_at + 2] != "str_for_value"


_DB_ABSOLUTE = re.compile(r"(?:dB|デシベル)\s*(?:に|へ|まで)|\bto\s+(?:-|−|minus\s+)?\d", re.IGNORECASE)
_DB_DOWN = re.compile(r"下げ|さげ|落と|絞|小さく|抑え|\b(?:down|lower|reduce|decrease|quieter|cut|drop|pull)\b", re.IGNORECASE)
_DB_UP = re.compile(r"上げ|あげ|大きく|持ち上げ|\b(?:up|raise|boost|increase|louder|push|bump)\b", re.IGNORECASE)


def step_from_words(step: Step, utterance: str) -> Step:
    """Prefer the utterance's dB direction over Jev's answer.
    In an observed case, Jev omitted the step for "lower by 3 dB," causing 3 to be treated as an absolute value and written as +3 dB."""
    if _DB_ABSOLUTE.search(utterance):
        return Step.SET
    if _DB_DOWN.search(utterance):
        return step if step in {Step.DOWN_SMALL, Step.DOWN_BIG} else Step.DOWN_SMALL
    if _DB_UP.search(utterance):
        return step if step in {Step.UP_SMALL, Step.UP_BIG} else Step.UP_SMALL
    return step


_PAN_RIGHT = re.compile(r"右(?:に|へ|寄り|側|方向|ward)|(?:少し|ちょっと|やや)\s*右|(?:to the |pan (?:hard )?|more )right|right(?: a bit|ward)", re.IGNORECASE)
_PAN_LEFT = re.compile(r"左(?:に|へ|寄り|側|方向)|(?:少し|ちょっと|やや)\s*左|(?:to the |pan (?:hard )?|more )left|left(?: a bit|ward)", re.IGNORECASE)
_DIRECTION_UP = re.compile(_DB_UP.pattern + r"|増や|ふや|強く|\bmore\b", re.IGNORECASE)
_DIRECTION_DOWN = re.compile(_DB_DOWN.pattern + r"|減ら|へら|弱く|\bless\b", re.IGNORECASE)
_DIRECTION_BIG = re.compile(r"大きく|かなり|ガッと|がっつり|思い切り|ぐっと|\b(?:a lot|much|way|heavily|hard)\b", re.IGNORECASE)
_ABSOLUTE_POSITION = re.compile(r"\b(?:center|centre|middle)\b|真ん中|センター|中央", re.IGNORECASE)
_JA_UP_EMBEDDED = re.compile(r"(?:仕|見|打ち)上げ")


def direction_from_words(action: Action, utterance: str) -> Step | None:
    """Read the direction from the utterance when Jev is unsure of it.
    Jev was measured answering "none" or "set" below 0.4 for "右に振って" and 0.58 for "センドAを上げて", which turned plain requests into a question."""
    if _ABSOLUTE_POSITION.search(utterance):
        return None
    directional_text = _JA_UP_EMBEDDED.sub("", utterance)
    up = bool(_DIRECTION_UP.search(directional_text)) or (action is Action.PAN and bool(_PAN_RIGHT.search(utterance)))
    down = bool(_DIRECTION_DOWN.search(directional_text)) or (action is Action.PAN and bool(_PAN_LEFT.search(utterance)))
    if up == down:
        return None
    big = bool(_DIRECTION_BIG.search(utterance))
    if up:
        return Step.UP_BIG if big else Step.UP_SMALL
    return Step.DOWN_BIG if big else Step.DOWN_SMALL


def relative_db_target(value: float, step: Step, current_display: str | None) -> float:
    """Treat "lower by 3 dB" as current minus 3, "raise by 3 dB" as current plus 3, and "set to -3 dB" as absolute.
    Do not act on a relative request when the current value is unreadable, such as -inf dB; an absolute write could jump from silence to high volume."""
    if step in {Step.DOWN_SMALL, Step.DOWN_BIG, Step.UP_SMALL, Step.UP_BIG}:
        match = re.search(r"-?\d+(?:\.\d+)?", current_display or "")
        if not match or "inf" in (current_display or "").lower():
            raise LocalizedError("error.volume_silent")
        current = float(match.group())
        return current - abs(value) if step in {Step.DOWN_SMALL, Step.DOWN_BIG} else current + abs(value)
    return value


PLUGIN_FORMAT_ORDER = tuple(
    part.strip().lower() for part in os.environ.get("LIVE_JEV_PLUGIN_FORMATS", "vst3,au,vst").split(",") if part.strip()
)


def _plugin_format(uri: str) -> str:
    lowered = uri.lower()
    if "#vst3:" in lowered:
        return "vst3"
    if "#auv2:" in lowered or "#au:" in lowered:
        return "au"
    if "#vst:" in lowered:
        return "vst"
    return ""


def preferred_plugin_uris(items: list[dict[str, str]]) -> dict[str, str]:
    """Pick one browser entry per plug-in name when the same plug-in is installed in several formats.

    Live lists Omnisphere four times (AU, VST3 and two VST2 entries). Taking the first match loaded the AU,
    whose editor window does not open on insert; the VST3 one does (measured). Default order: VST3, AU, VST2.
    """
    rank = {name: index for index, name in enumerate(PLUGIN_FORMAT_ORDER)}
    best: dict[str, tuple[int, str]] = {}
    for item in items:
        name, uri = str(item.get("name") or ""), str(item.get("uri") or "")
        if not name or not uri:
            continue
        score = rank.get(_plugin_format(uri), len(rank))
        if name not in best or score < best[name][0]:
            best[name] = (score, uri)
    return {name: uri for name, (_, uri) in best.items()}


class StaleSnapshot(Exception):
    """The snapshot's track count, names, or device counts differ from the current song. Refresh it and retry."""


class LiveJevService:
    def __init__(
        self,
        bridge: BridgeClient | None = None,
        *,
        snapshot: Snapshot | None = None,
        key: str | None = None,
        requester: Callable[[Mapping[str, Any], str], Mapping[str, Any]] | None = None,
        llm_key: str | None = None,
        rewriter: Callable[[Snapshot, str, str], str] | None = None,
        verbose: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bridge = bridge or make_bridge_client(verbose=verbose)
        self.reader = SnapshotReader(self.bridge)
        self.snapshot = snapshot
        self.key = key
        self.requester = requester or JevClient()
        self.llm_key = llm_key
        self.rewriter = rewriter or GeminiRewriter()
        self.verbose = verbose
        self.live = snapshot is not None
        self.pending: Pending | None = None
        self.pending_confirm = None
        self.pending_confirm_created: float | None = None
        self._clock = clock
        self.previous: Union[PreviousIntent, PreviousChain, None] = None
        self._undo_target_tracks = None
        self._history_kind = "none"
        self._plugin_names_cache = None
        self._plugin_uris = {}
        self._plugin_script_ok = None
        self._startup_notice_emitted = False
        self._llm_attempts: OrderedDict[str, float] = OrderedDict()
        self._llm_rate: deque[float] = deque()
        self._llm_cooldown_until = 0.0
        self._llm_disabled = False
        self._llm_worker: threading.Thread | None = None
        self.lang = resolve_language(os.environ.get("LIVE_JEV_LANG"), default="ja")

    def _m(self, key: str, **values: object) -> str:
        return render(key, lang=self.lang, **values)

    def _error_text(self, error: Exception) -> str:
        if isinstance(error, LocalizedError):
            return error.translated(self.lang)
        line = str(error)
        return self._m("error.generic") if self.lang == "en" and contains_japanese(line) else line

    def _llm_enabled(self, require_key: bool = True) -> bool:
        if os.environ.get("LIVE_JEV_LLM", "1") == "0":
            return False
        return bool(self.llm_key) if require_key else True

    def close(self) -> None:
        close = getattr(self.bridge, "close", None)
        if callable(close):
            close()

    def startup_notice(self) -> dict[str, Any] | None:
        if self._startup_notice_emitted:
            return None
        line = getattr(self.bridge, "startup_error", None)
        if not isinstance(line, str) or not line:
            return None
        self._startup_notice_emitted = True
        shown = self._m("error.generic") if self.lang == "en" and contains_japanese(line) else line
        return {"kind": "error", "line": shown}

    def start(self) -> dict[str, Any]:
        with using_language(self.lang):
            if self.key is None:
                self.key = read_key()
            if self.llm_key is None and self._llm_enabled(require_key=False):
                self.llm_key = read_key("GEMINI_API_KEY")
            self.live = self.bridge.ping()
            if self.live:
                try:
                    self.snapshot, _ = self.reader.read()
                except BridgeError:
                    self.live = False
            return self.status()

    def status(self, message_id: Any = None) -> dict[str, Any]:
        tracks = len(self.snapshot.tracks) if self.snapshot else 0
        tempo = self.snapshot.tempo if self.snapshot else 0.0
        outdated = self._script_outdated()
        line = self._m("error.script_outdated") if self.live and outdated else self._m("status.connected", tracks=tracks, tempo=tempo) if self.live and self.snapshot else self._m("status.disconnected")
        result: dict[str, Any] = {"kind": "status", "live": self.live, "jev": bool(self.key), "tracks": tracks, "tempo": tempo, "line": line}
        if message_id is not None:
            result["id"] = message_id
        return result

    def _script_outdated(self) -> bool:
        if not hasattr(self.bridge, "script_version"):
            return False
        version = getattr(self.bridge, "script_version")
        return not isinstance(version, str) or _version_is_older(version, MIN_SCRIPT_VERSION)

    def _ensure_script_current(self) -> bool:
        if not hasattr(self.bridge, "script_version"):
            return True
        ping = getattr(self.bridge, "ping", None)
        if callable(ping):
            ping()
        return not self._script_outdated()

    def _outdated_response(self, message_id: Any, started: float) -> dict[str, Any]:
        return {"id": message_id, "kind": "error", "line": self._m("error.script_outdated"), "ms": self._ms(started, 0, 0, 0)}

    def refresh(self, message_id: Any = None) -> dict[str, Any]:
        old_tracks = self._structure_fingerprint(self.snapshot) if self.snapshot else ()
        try:
            self.snapshot, elapsed = self.reader.read()
            self.live = True
        except BridgeError:
            self.live = False
            return {"id": message_id, "kind": "error", "line": self._m("error.live")}
        new_tracks = self._structure_fingerprint(self.snapshot)
        if old_tracks and old_tracks != new_tracks:
            self.previous = None
            self._undo_target_tracks = None
            self.pending = None
            self.pending_confirm = None
            self.pending_confirm_created = None
        self._plugin_names_cache = None
        self._plugin_uris = {}
        self._plugin_script_ok = None
        answer = self.status(message_id)
        answer["ms"] = {"jev": 0, "llm": 0, "bridge": elapsed, "total": elapsed}
        return answer

    @staticmethod
    def _structure_fingerprint(snapshot: Snapshot) -> tuple[Any, ...]:
        return tuple((track.path, track.name, tuple((device.path, device.name) for device in track.devices)) for track in addressable_targets(snapshot))

    @staticmethod
    def _ms(started: float, jev: int, llm: int, bridge: int) -> dict[str, int]:
        return {"jev": jev, "llm": llm, "bridge": bridge, "total": round((time.perf_counter() - started) * 1000)}

    def process(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """Process one utterance. If the song structure differs from the snapshot, refresh it and retry the utterance once."""
        with using_language(self.lang):
            try:
                return self._process_once(message)
            except StaleSnapshot:
                pass
            refreshed = self.refresh(message.get("id"))
            if refreshed.get("kind") == "error":
                return refreshed
            try:
                return self._process_once(message)
            except StaleSnapshot:
                return {"id": message.get("id"), "kind": "error", "line": self._m("error.stale")}

    def _expire_pending(self) -> bool:
        now = self._clock()
        if self.pending is not None and now - self.pending.created >= PENDING_TTL_SECONDS:
            self.pending = None
        confirm_expired = (
            self.pending_confirm is not None
            and self.pending_confirm_created is not None
            and now - self.pending_confirm_created >= PENDING_TTL_SECONDS
        )
        if confirm_expired:
            self.pending_confirm = None
            self.pending_confirm_created = None
        return confirm_expired

    def _process_once(self, message: Mapping[str, Any]) -> dict[str, Any]:
        confirm_expired = self._expire_pending()
        message_id = message.get("id")
        command = message.get("cmd")
        if command == "lang":
            value = message.get("value")
            if value not in {"auto", "ja", "en"}:
                return {"id": message_id, "kind": "error", "line": self._m("error.generic")}
            self.lang = resolve_language(value, default="ja")
            return self.status(message_id)
        if command == "status":
            if not self.live or self.snapshot is None:
                refreshed = self.refresh(message_id)
                if refreshed.get("kind") != "error":
                    return refreshed
            return self.status(message_id)
        if command == "cancel_pending":
            target = message.get("target")
            if target is None or (self.pending is not None and self.pending.request_id == target):
                self.pending = None
            confirm_id = self.pending_confirm[-1] if self.pending_confirm is not None else None
            if target is None or confirm_id == target:
                self.pending_confirm = None
                self.pending_confirm_created = None
            return self.status(message_id)
        if command == "refresh":
            return self.refresh(message_id)
        if command == "quit":
            return {"id": message_id, "kind": "status", "line": self._m("status.quit"), "quit": True}
        if "confirm" in message:
            if confirm_expired:
                return {"id": message_id, "kind": "info", "line": self._m("info.expired")}
            if self.pending_confirm is not None and self.pending_confirm[-1] != message_id:
                return {"id": message_id, "kind": "info", "line": self._m("info.expired")}
            return self._answer_confirm(message_id, bool(message.get("confirm")))
        if command == "undo":
            return self._dispatch_only_action(Action.UNDO, message_id)
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"id": message_id, "kind": "error", "line": self._m("error.empty")}
        if len(text) > 500:
            return {"id": message_id, "kind": "info", "line": self._m("info.input_too_long")}
        answering = message.get("answering")
        if answering is not None and (self.pending is None or self.pending.request_id != answering):
            return {"id": message_id, "kind": "info", "line": self._m("info.expired")}
        if self.snapshot is None or not self.live:
            refreshed = self.refresh(message_id)
            if refreshed.get("kind") == "error":
                return refreshed
        if STRONG_NEGATION.search(text.replace("’", "'")):
            # Do not send negated requests to Jev. Actions run without confirmation, so doing nothing is safer than asking a follow-up.
            return {"id": message_id, "kind": "info", "line": render("info.negated", lang=self.lang), "ms": self._ms(time.perf_counter(), 0, 0, 0)}
        if self._snapshot_is_stale():
            raise StaleSnapshot()
        if self.pending_confirm is not None:
            answer = _normalize(text)
            if answer in {"はい", "yes", "ok", "y", "うん", "実行"}:
                return self._answer_confirm(message_id, True)
            if answer in {"やめる", "いいえ", "no", "n", "キャンセル", "やめて"}:
                return self._answer_confirm(message_id, False)
            self.pending_confirm = None
            self.pending_confirm_created = None
        started = time.perf_counter()
        if self._is_undo_request(text):
            return self._dispatch_only_action(Action.UNDO, message_id, started)
        if self._is_redo_request(text):
            return self._dispatch_only_action(Action.REDO, message_id, started)
        clauses = split_compound(text, self.snapshot)
        if not clauses:
            return {"id": message_id, "kind": "info", "line": self._m("info.one_at_a_time"), "ms": self._ms(started, 0, 0, 0)}
        if len(clauses) > 1:
            try:
                return self._process_chain(clauses, message_id, started)
            except JevRequestTooLarge:
                return {"id": message_id, "kind": "error", "line": self._m("error.jev_too_large")}
        pending_answer = self.pending
        filled = self._fill_pending(text)
        if filled is _REPROCESS_PENDING:
            return self._process_once({key: value for key, value in message.items() if key != "answering"})
        if filled is not None:
            dispatched = self._dispatch_only_action(filled.intent.action, message_id, started)
            if dispatched is not None:
                return dispatched
            filled = self._apply_selected_track(filled)
            filled = self._resolve_clip_target(filled)
            decision = self._decision(filled, message_id)
            if decision is not None:
                decision["ms"] = self._ms(started, 0, 0, 0)
                return decision
            return self._execute(filled.intent, message_id, 0, 0, started, text, None)
        if pending_answer is not None:
            if pending_answer.field == "plugin":
                self.pending = pending_answer
                answer = self._plugin_candidate_question(message_id, pending_answer.options)
                answer["ms"] = self._ms(started, 0, 0, 0)
                return answer
            fallback = self._decision(pending_answer.result, message_id)
            if fallback is None:
                fallback = self._ask_for_action(pending_answer.result, message_id)
            return self._rewrite_and_process(
                text,
                pending_answer.result,
                fallback,
                message_id,
                0,
                started,
                saved_pending=pending_answer,
                pending_field=pending_answer.field,
            )
        request = extract_plugin_request(text, self.snapshot)
        if request is not None:
            request_name = resolve_device_request_name(request.raw_name)
            request = replace(request, raw_name=request_name) if request_name is not None else None
        deferred_plugin: PluginRequest | None = None
        if request is not None and request.action in {Action.INSERT_PLUGIN, Action.ADD_TRACK_WITH_PLUGIN} and resolve_native_device(request.raw_name) is None and resolve_plugin_name(request.raw_name, self._plugin_names()) is None:
            # Insertion verbs also describe other actions, such as enabling the metronome or loop.
            # If the name has no immediate catalog match, try normal parsing first. Search for a plug-in only if that fails; Jev then resolves katakana names against the catalog.
            deferred_plugin, request = request, None
        if request is not None:
            return self._process_plugin_request(request, text, message_id, started)
        notes_request = parse_clip_notes_phrase(text, self.snapshot)
        if notes_request is not None and notes_request.target_missing:
            return {"id": message_id, "kind": "error", "line": self._m("error.named_track_missing"), "ms": self._ms(started, 0, 0, 0)}
        if notes_request is not None and self._script_available():
            if not self._ensure_script_current():
                return self._outdated_response(message_id, started)
            return self._run_clip_notes(notes_request, text, message_id, started)
        llm_available = self._llm_enabled()
        plugin_notice = None if deferred_plugin is not None and llm_available else self._plugin_notice(message_id, text)
        if plugin_notice is not None:
            plugin_notice["ms"] = self._ms(started, 0, 0, 0)
            return plugin_notice
        local = parse_local(text, self.snapshot)
        if local is not None:
            dispatched = self._dispatch_only_action(local.action, message_id, started)
            if dispatched is not None:
                return dispatched
            result = self._resolve_previous(IntentResult(local, (), (), ()), text)
            track_named = result.intent.track_stated >= TRACK_STATED_MIN
            result = self._apply_selected_track(self._resolve_release_target(result))
            result = self._resolve_clip_target(result)
            result = self._resolve_device_name(result, track_named)
            if result.intent.action is Action.NONE:
                line = self._m("info.no_undo") if re.search(r"戻|取り消|undo", text, re.IGNORECASE) else self._m("info.no_repeat")
                return {"id": message_id, "kind": "info", "line": line, "ms": self._ms(started, 0, 0, 0)}
            decision = self._decision(result, message_id)
            if decision is not None:
                decision["ms"] = self._ms(started, 0, 0, 0)
                return decision
            return self._execute(result.intent, message_id, 0, 0, started, text, None)
        if deferred_plugin is not None and len(plugin_name_candidates(deferred_plugin.raw_name, self._plugin_names())) > 1:
            return self._process_plugin_request(deferred_plugin, text, message_id, started)
        bare = self._local_bare_plugin_request(text, message_id, started)
        if bare is not None:
            return bare
        if not self.key:
            return {"id": message_id, "kind": "error", "line": self._m("error.jev_key")}
        jev_started = time.perf_counter()
        selected_track_index = self._selected_track_index()
        jev_request = build_request(self.snapshot, text, selected_track_index)
        try:
            response = self.requester(jev_request, self.key)
        except JevRequestTooLarge:
            return {"id": message_id, "kind": "error", "line": self._m("error.jev_too_large")}
        except RuntimeError:
            return {"id": message_id, "kind": "error", "line": self._m("error.jev")}
        jev_ms = round((time.perf_counter() - jev_started) * 1000)
        if self.verbose:
            print(f"[jev] questions={len(jev_request['questions'])} ms={jev_ms}", file=sys.stderr)
        detail_tracks = jev_request["state"].get("detail_tracks", ())
        result = interpret_response(self.snapshot, text, response, detail_tracks)
        if result.intent.action in DISPATCH_ONLY_ACTIONS:
            decision = self._decision(result, message_id)
            assert decision is not None
            decision["ms"] = self._ms(started, jev_ms, 0, 0)
            return decision
        result = self._step_from_utterance(result, text)
        result = self._resolve_previous(result, text)
        track_named = result.intent.track_stated >= TRACK_STATED_MIN
        result = self._apply_selected_track(self._resolve_release_target(result))
        result = self._resolve_clip_target(result)
        result = self._device_named_in_text(result, text, track_named)
        decision = self._decision(result, message_id)
        detail_omitted = (
            decision is not None
            and decision.get("kind") == "ask"
            and isinstance(result.intent.track, int)
            and result.intent.track not in result.evaluated_detail_tracks
            and (
                ACTIONS[result.intent.action].needs_param
                or ACTIONS[result.intent.action].needs_clip
                or ACTIONS[result.intent.action].needs_device
            )
        )
        if ACTIONS[result.intent.action].kind in {"plugin", "plugin_track"} and not result.intent.plugin:
            if decision is not None:
                decision["ms"] = self._ms(started, jev_ms, 0, 0)
                return decision
            if deferred_plugin is not None:
                return self._process_plugin_request(deferred_plugin, text, message_id, started, jev_ms, initial_result=result)
            found = self._plugin_fallback(text, message_id, started, jev_ms, initial_result=result)
            if found is not None:
                return found
            return {"id": message_id, "kind": "info", "line": self._m("info.plugin_name_needed"), "ms": self._ms(started, jev_ms, 0, 0)}
        undecided = decision is not None and decision.get("kind") in {"ask", "info"}
        if undecided and deferred_plugin is not None and llm_available and self._rewrite_eligible(result):
            return self._rewrite_and_process(text, result, decision, message_id, jev_ms, started)
        if undecided and deferred_plugin is not None and (
            result.intent.action is Action.NONE or result.intent.action_conf < 0.6 or ACTIONS[result.intent.action].kind == "structure_device"
        ):
            return self._process_plugin_request(deferred_plugin, text, message_id, started, jev_ms, initial_result=result)
        if decision is not None and decision.get("kind") in {"ask", "info"} and result.intent.action is Action.NONE:
            bare = self._bare_plugin_request(text, message_id, started, jev_ms, initial_result=result)
            if bare is not None:
                return bare
        if decision is not None and decision.get("kind") in {"ask", "info"} and PLUGIN_HINT.search(text) and (
            ACTIONS[result.intent.action].kind == "structure_device" or result.intent.action is Action.NONE
        ):
            fallback = self._plugin_fallback(text, message_id, started, jev_ms, initial_result=result)
            if fallback is not None:
                return fallback
        if decision is not None and self._rewrite_eligible(result):
            return self._rewrite_and_process(text, result, decision, message_id, jev_ms, started)
        if detail_omitted:
            decision["ms"] = self._ms(started, jev_ms, 0, 0)
            return decision
        if decision is not None:
            decision["ms"] = self._ms(started, jev_ms, 0, 0)
            return decision
        return self._execute(result.intent, message_id, jev_ms, 0, started, text, None)

    def _rewrite_and_process(
        self,
        utterance: str,
        initial: IntentResult,
        initial_decision: dict[str, Any] | None,
        message_id: Any,
        jev_ms: int,
        started: float,
        *,
        saved_pending: Pending | None = None,
        pending_field: str | None = None,
        semantic_fallback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback = initial_decision or self._ask_for_action(initial, message_id)
        gemini_called = False
        if saved_pending is None:
            saved_pending = self.pending

        def finish_fallback(
            llm_ms: int = 0,
            prefix: str | None = None,
            *,
            semantic: bool = False,
            prefix_only: bool = False,
        ) -> dict[str, Any]:
            self.pending = saved_pending
            answer = dict(semantic_fallback if semantic and semantic_fallback is not None else fallback)
            if prefix:
                answer["line"] = prefix if prefix_only else f"{prefix} {answer['line']}"
            answer["ms"] = self._ms(started, jev_ms, llm_ms, 0)
            if gemini_called:
                answer["via"] = "gemini"
            return answer

        def finish_semantic_failure(llm_ms: int) -> dict[str, Any]:
            if semantic_fallback is not None:
                return finish_fallback(llm_ms, semantic=True)
            return finish_fallback(llm_ms, self._m("llm.response"))

        if not self._llm_enabled():
            return finish_fallback()

        now = self._clock()
        normalized = _normalize(utterance)
        while self._llm_attempts:
            oldest, attempted = next(iter(self._llm_attempts.items()))
            if now - attempted < LLM_ATTEMPT_TTL_SECONDS:
                break
            self._llm_attempts.pop(oldest)
        while self._llm_rate and now - self._llm_rate[0] >= LLM_RATE_WINDOW_SECONDS:
            self._llm_rate.popleft()
        if (
            self._llm_disabled
            or now < self._llm_cooldown_until
            or normalized in self._llm_attempts
            or len(self._llm_rate) >= LLM_RATE_LIMIT
            or (self._llm_worker is not None and self._llm_worker.is_alive())
        ):
            return finish_fallback(semantic=semantic_fallback is not None)
        self._llm_attempts[normalized] = now
        self._llm_attempts.move_to_end(normalized)
        while len(self._llm_attempts) > LLM_ATTEMPT_LIMIT:
            self._llm_attempts.popitem(last=False)
        self._llm_rate.append(now)

        llm_started = time.perf_counter()
        completed: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        snapshot = self.snapshot
        rewriter = self.rewriter
        requester = self.requester
        if requester is request_jev:
            requester = JevClient()
        llm_key = self.llm_key
        jev_key = self.key
        plugin_catalog = self._plugin_names()
        built_in_devices = tuple(sorted(NATIVE_DEVICES))
        insertion_catalog = tuple(sorted(set(plugin_catalog) | set(built_in_devices)))

        def rewrite_and_classify() -> None:
            try:
                assert snapshot is not None and llm_key is not None
                try:
                    inspect.signature(rewriter).bind(snapshot, utterance, llm_key, plugin_catalog, built_in_devices)
                except (TypeError, ValueError):
                    rewritten_text = rewriter(snapshot, utterance, llm_key)
                else:
                    rewritten_text = rewriter(snapshot, utterance, llm_key, plugin_catalog, built_in_devices)
                lines = [line.strip() for line in rewritten_text.splitlines() if line.strip()]
                if len(lines) != 1 or lines[0] == "不明" or len(split_compound(lines[0], snapshot)) != 1:
                    completed.put_nowait(("response", lines))
                    return
                rewritten = lines[0]
                plugin_request = extract_plugin_request(rewritten, snapshot)
                plugin = resolve_exact_plugin_name(plugin_request.raw_name, insertion_catalog) if plugin_request is not None else None
                if plugin_request is not None and plugin is None:
                    completed.put_nowait(("response", lines))
                    return
                if plugin_request is not None and plugin is not None:
                    candidate = IntentResult(plugin_intent(plugin_request, plugin), (), (), ())
                elif (local := parse_local(rewritten, snapshot)) is not None:
                    if local.action is Action.ADD_TRACK_WITH_DEVICE:
                        plugin = resolve_exact_plugin_name(local.native_device or "", insertion_catalog)
                        if plugin is None:
                            completed.put_nowait(("response", lines))
                            return
                        local = replace(
                            local,
                            action=Action.ADD_TRACK_WITH_PLUGIN,
                            native_device=None,
                            native_device_conf=0.0,
                            plugin=plugin,
                        )
                    candidate = IntentResult(local, (), (), ())
                elif jev_key:
                    request = build_request(snapshot, rewritten, None)
                    response = requester(request, jev_key)
                    details = request["state"].get("detail_tracks", ())
                    candidate = interpret_response(snapshot, rewritten, response, details)
                else:
                    completed.put_nowait(("response", lines))
                    return
                completed.put_nowait(("ok", (lines, candidate)))
            except RewriteFailure as error:
                completed.put_nowait(("failure", error.reason))
            except Exception:
                completed.put_nowait(("failure", "unavailable"))

        self._llm_worker = threading.Thread(target=rewrite_and_classify, name="live-jev-gemini", daemon=True)
        gemini_called = True
        self._llm_worker.start()
        try:
            kind, payload = completed.get(timeout=max(0.0, LLM_DEADLINE_SECONDS - (time.perf_counter() - llm_started)))
        except queue.Empty:
            self._llm_cooldown_until = now + 30.0
            elapsed = round((time.perf_counter() - llm_started) * 1000)
            return finish_fallback(
                elapsed,
                self._m("llm.unavailable"),
                semantic=semantic_fallback is not None,
                prefix_only=semantic_fallback is not None,
            )
        llm_ms = round((time.perf_counter() - llm_started) * 1000)
        if kind == "failure":
            if payload == "quota":
                self._llm_cooldown_until = now + 300.0
            elif payload in {"auth", "configuration"}:
                self._llm_disabled = True
            elif payload == "unavailable":
                self._llm_cooldown_until = now + 30.0
            key = {
                "auth": "llm.auth",
                "configuration": "llm.configuration",
                "quota": "llm.quota",
                "unavailable": "llm.unavailable",
            }.get(payload, "llm.response")
            return finish_fallback(
                llm_ms,
                self._m(key),
                semantic=semantic_fallback is not None,
                prefix_only=semantic_fallback is not None,
            )
        if kind != "ok":
            return finish_semantic_failure(llm_ms)
        rewritten, candidate = payload
        if candidate.intent.action in DISPATCH_ONLY_ACTIONS:
            return finish_semantic_failure(llm_ms)
        candidate = self._step_from_utterance(candidate, rewritten[0])
        constrained_result = self._constrain_rewrite(
            initial,
            candidate,
            utterance=utterance,
            insertion_catalog=insertion_catalog,
            pending_field=pending_field,
        )
        if constrained_result is None:
            return finish_semantic_failure(llm_ms)
        constrained, trusted_fields = constrained_result
        original_track_conf = constrained.intent.track_conf
        constrained = self._apply_selected_track(constrained)
        if constrained.intent.target_origin is TargetOrigin.SELECTED:
            constrained = replace(constrained, intent=replace(constrained.intent, track_conf=original_track_conf))
        decision = self._decision(constrained, message_id, trusted_fields=trusted_fields)
        if decision is not None:
            return finish_semantic_failure(llm_ms)
        denied = self._authorize_target(constrained.intent)
        if denied is not None:
            return finish_semantic_failure(llm_ms)
        answer = self._execute(
            constrained.intent,
            message_id,
            jev_ms,
            llm_ms,
            started,
            utterance,
            rewritten,
            force_confirm=True,
            trusted_fields=trusted_fields,
        )
        answer["via"] = "gemini"
        return answer

    def _rewrite_eligible(self, result: IntentResult) -> bool:
        intent = result.intent
        if intent.action is Action.NONE or intent.action_conf < 0.6:
            return True
        if self.pending is not None and self.pending.field != "track":
            return True
        if intent.named_evidence >= TRACK_STATED_MIN and intent.track is None:
            return True
        if intent.named_evidence >= TRACK_UNSTATED_MAX and isinstance(intent.track, (int, TargetRef)) and intent.track_conf < NAMED_TRACK_CONF_MIN:
            return True
        return ACTIONS[intent.action].needs_device and bool(intent.device_name) and intent.device is None

    def _constrain_rewrite(
        self,
        initial: IntentResult,
        candidate: IntentResult,
        *,
        utterance: str = "",
        insertion_catalog: tuple[str, ...] = (),
        pending_field: str | None = None,
    ) -> tuple[IntentResult, frozenset[str]] | None:
        original = initial.intent
        proposed = candidate.intent
        if ACTIONS[proposed.action].kind in {"plugin", "plugin_track"}:
            return self._constrain_plugin_rewrite(initial, candidate, utterance, insertion_catalog, pending_field)
        allowed_kinds = {
            "track_bool", "track_int", "mixer", "send", "param", "clip_prop", "song_bool",
            "jump", "tempo", "rename", "transport", "song_call", "track_call", "clip_call", "scene_call",
        }
        if proposed.action is Action.NONE or ACTIONS[proposed.action].kind not in allowed_kinds:
            return None
        if proposed.target_origin in MULTI_TARGET_ORIGINS or proposed.tracks:
            return None
        if original.action_conf >= 0.6 and proposed.action is not original.action:
            return None
        if pending_field is not None:
            field_for_pending = {
                "action": "action", "track": "track", "param": "param", "device": "device",
                "clip": "clip", "send": "send", "scene": "scene", "step": "step",
            }.get(pending_field)
            changed = {
                name for name in ("action", "track", "param", "device", "clip", "send", "scene", "step")
                if getattr(proposed, name) not in {None, Step.NONE} and getattr(proposed, name) != getattr(original, name)
            }
            if any(name != field_for_pending for name in changed):
                return None

        track = original.track
        target_origin = original.target_origin
        if proposed.track is not None and proposed.track != original.track:
            can_correct_named_track = (
                original.track is None
                and original.named_evidence >= TRACK_STATED_MIN
                and isinstance(proposed.track, (int, TargetRef))
                and self.snapshot is not None
                and self.snapshot.target(proposed.track) is not None
            )
            if not can_correct_named_track:
                return None
            track = proposed.track
            target_origin = TargetOrigin.NAMED

        if proposed.number is not None and proposed.number != original.number:
            return None
        if proposed.step is not Step.NONE and proposed.step != original.step:
            return None
        if proposed.text is not None and proposed.text != original.text:
            return None
        if proposed.native_device is not None and proposed.native_device != original.native_device:
            return None
        if proposed.plugin is not None and proposed.plugin != original.plugin:
            return None

        trusted: set[str] = set()
        if original.action is Action.NONE or original.action_conf < 0.6:
            trusted.add("action")

        values: dict[str, Any] = {"action": proposed.action, "track": track, "target_origin": target_origin}
        proposed_spec = ACTIONS[proposed.action]
        for name, confidence_name in (
            ("param", "param_conf"), ("device", "device_conf"), ("clip", "clip_conf"),
            ("send", "send_conf"), ("scene", "scene_conf"),
        ):
            old_value = getattr(original, name)
            new_value = getattr(proposed, name)
            needed = {
                "param": proposed_spec.needs_param or proposed_spec.needs_device,
                "device": proposed_spec.needs_device,
                "clip": proposed_spec.needs_clip,
                "send": proposed_spec.needs_send,
                "scene": proposed_spec.needs_scene,
            }[name]
            if old_value is None and new_value is not None and not needed:
                return None
            if old_value is not None and new_value is not None and new_value != old_value:
                return None
            chosen = old_value if old_value is not None else new_value
            if chosen is not None and (old_value is None or getattr(original, confidence_name) < 0.6):
                trusted.add(name)
            values[name] = chosen

        if values["param"] is not None:
            parameter = values["param"]
            if not isinstance(track, (int, TargetRef)) or self.snapshot is None:
                return None
            owner = self.snapshot.target(track)
            if owner is None or not any(parameter in device.params for device in owner.devices):
                return None
        if values["device"] is not None:
            device = values["device"]
            if not isinstance(track, (int, TargetRef)) or self.snapshot is None:
                return None
            owner = self.snapshot.target(track)
            if owner is None or device not in owner.devices:
                return None
            values["device_name"] = device.name
        if values["clip"] is not None:
            if not isinstance(track, int) or self.snapshot is None:
                return None
            owner = next((item for item in self.snapshot.tracks if item.index == track), None)
            clip = next((item for item in owner.clips if item.slot == values["clip"]), None) if owner else None
            if clip is None:
                return None
            values["clip_name"], values["clip_path"] = clip.name, clip.path

        if original.step is not Step.NONE and proposed.step is original.step and original.step_conf < 0.6:
            trusted.add("step")
        intent = replace(original, **values)
        return replace(initial, intent=intent), frozenset(trusted)

    def _constrain_plugin_rewrite(
        self,
        initial: IntentResult,
        candidate: IntentResult,
        utterance: str,
        catalog: tuple[str, ...],
        pending_field: str | None,
    ) -> tuple[IntentResult, frozenset[str]] | None:
        original = initial.intent
        proposed = candidate.intent
        if pending_field is not None or original.action_conf >= 0.6 and proposed.action is not original.action:
            return None
        if proposed.action not in {Action.INSERT_PLUGIN, Action.ADD_TRACK_WITH_PLUGIN}:
            return None
        if proposed.tracks or proposed.target_origin in MULTI_TARGET_ORIGINS:
            return None
        plugin = resolve_exact_plugin_name(proposed.plugin or "", catalog)
        source = extract_plugin_request(utterance, self.snapshot) if self.snapshot is not None else None
        if plugin is None or source is None or source.target_missing or source.action is not proposed.action:
            return None
        expected = plugin_intent(source, plugin)
        if (
            proposed.track != expected.track
            or proposed.text != expected.text
            or proposed.track_kind != expected.track_kind
            or proposed.number is not None
            or proposed.step is not Step.NONE
            or proposed.native_device is not None
        ):
            return None
        if isinstance(expected.track, (int, TargetRef)) and original.track != expected.track:
            can_correct_named_track = (
                original.track is None
                and original.named_evidence >= TRACK_STATED_MIN
                and self.snapshot.target(expected.track) is not None
            )
            if not can_correct_named_track:
                return None
        if expected.track is None and original.named_evidence >= TRACK_STATED_MIN:
            return None
        constrained = replace(
            expected,
            action_conf=original.action_conf,
            track_conf=min(expected.track_conf, original.track_conf),
            track_stated=max(expected.track_stated, original.track_stated),
            named_evidence=max(expected.named_evidence, original.named_evidence),
            needs_generation=original.needs_generation,
            compound=original.compound,
            refers_previous=original.refers_previous,
            utterance=utterance,
        )
        return replace(initial, intent=constrained), frozenset({"action"})

    def _ask_for_action(self, result: IntentResult, message_id: Any) -> dict[str, Any]:
        self.pending = Pending(result, "action", self._clock(), message_id)
        options = [action_label(name.value, lang=self.lang) for name in Action if action_label(name.value, lang="ja") in result.action_options]
        return {"id": message_id, "kind": "ask", "line": self._m("ask.action"), "options": options or list(result.action_options)}

    def _localized_options(self, options: tuple[str, ...]) -> list[str]:
        if self.lang == "ja":
            return list(options)
        replacements = {"マスター": "Master", "選択中のトラック": "Selected track"}
        return [replacements.get(option, option) for option in options]

    def _selected_track_index(self) -> int | TargetRef | None:
        """Read the currently selected addressable target from Live."""
        request = request_id("context")
        try:
            result = self.bridge.run(["--api-session-context", request])
        except Exception:
            return None
        ack = ack_map(result).get(request)
        payload = ack.payload if ack is not None else None
        selected = payload.get("selected") if isinstance(payload, Mapping) else None
        track = selected.get("track") if isinstance(selected, Mapping) else None
        path = str(track.get("path", "")) if isinstance(track, Mapping) else ""
        match = re.fullmatch(r"live_set (tracks|return_tracks) (\d+)", path)
        if match:
            index = int(match.group(2))
            return index if match.group(1) == "tracks" else TargetRef(TargetKind.RETURN, index)
        return TargetRef(TargetKind.MASTER) if path == "live_set master_track" else None

    @staticmethod
    def _step_from_utterance(result: IntentResult, text: str) -> IntentResult:
        intent = result.intent
        if not ACTIONS[intent.action].needs_step or intent.number is not None:
            return result
        if intent.step is not Step.NONE and intent.step_conf >= 0.6:
            return result
        step = direction_from_words(intent.action, text)
        if step is None:
            return result
        return replace(result, intent=replace(intent, step=step, step_conf=1.0))

    def _apply_selected_track(self, result: IntentResult) -> IntentResult:
        """Resolve an explicit "selected track" target and default unspecified targets to the selected track."""
        intent = result.intent
        spec = ACTIONS[intent.action]
        wants_selected = intent.track == "selected"
        if wants_selected and intent.target_origin in {TargetOrigin.EXCEPT, TargetOrigin.ONLY}:
            index = self._selected_track_index()
            if index is None or isinstance(index, int) and any(is_bridge_track(track) and track.index == index for track in self.snapshot.tracks):
                return replace(result, intent=replace(intent, track=None, tracks=()))
            if intent.target_origin is TargetOrigin.ONLY and not isinstance(index, int):
                return replace(result, intent=replace(intent, track=None, tracks=()))
            members = (index,) if intent.target_origin is TargetOrigin.ONLY else tuple(item for item in eligible_track_indices(self.snapshot) if item != index)
            return replace(result, intent=replace(intent, track=None, tracks=members, track_conf=1.0))
        # When no target is named, ignore Jev's guessed track and use the selected track.
        # Keep tracks derived from clip or device prefixes.
        # Jev's confidence alone is not enough: "センドBを少し上げて" was measured picking a track at 0.90 with no name in the utterance.
        # In the uncertain band a confident pick is kept ("キック上げて" -> Drums) and no pick means the selected track ("アームして").
        # Measured: "キック上げて" scores P(named) 0.35-0.58 while Jev picks Drums at 0.85-0.89. In this band a pick is
        # evidence of a name, so it must not be replaced by the selected track: it is used when confident, asked about otherwise.
        # Setting-only utterances were already forced to 0.0, so a score left in the band means name-like words remain
        # ("ベル下げて", no such track, scored 0.35-0.49 with no pick): ask rather than touch the selected track.
        unspecified = (
            spec.needs_track
            and intent.named_evidence < TRACK_UNSTATED_MAX
            and intent.target_origin is TargetOrigin.NONE
        )
        if not wants_selected and not unspecified:
            return result
        index = self._selected_track_index()
        if index is None:
            return replace(result, intent=replace(intent, track=None, track_conf=0.0, target_origin=TargetOrigin.NONE))
        if isinstance(index, int) and any(is_bridge_track(track) and track.index == index for track in self.snapshot.tracks):
            return replace(result, intent=replace(intent, track=None, track_conf=0.0, target_origin=TargetOrigin.NONE))
        return replace(result, intent=replace(intent, track=index, track_conf=1.0, target_origin=TargetOrigin.SELECTED))

    def _has_compound_local_operations(self, text: str) -> bool:
        parsed = parse_local(text, self.snapshot) if self.snapshot is not None else None
        if parsed is not None and parsed.action is Action.RENAME:
            return False
        masked = text
        masked = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', " NAME ", masked)
        names = [track.name for track in self.snapshot.tracks if track.name] if self.snapshot else []
        names += [device.name for track in self.snapshot.tracks for device in track.devices if device.name] if self.snapshot else []
        for name in sorted(names, key=len, reverse=True):
            masked = re.sub(re.escape(name), " TARGET ", masked, flags=re.IGNORECASE)
        operations = re.findall(
            r"\b(?:mute|unmute|solo|unsolo|arm|disarm|quantize|quantise|play|stop|rename|pan|lower|raise|increase|decrease)\b|(?:ミュート|ソロ|アーム|クオンタイズ|再生|停止|止め|下げ|上げ|改名)",
            masked,
            re.IGNORECASE,
        )
        return len(operations) > 1

    def _plan_chain_clause(self, text: str, inherited: Intent | None) -> tuple[Intent | None, int]:
        assert self.snapshot is not None
        if STRONG_NEGATION.search(text.replace("’", "'")):
            return None, 0
        if extract_plugin_request(text, self.snapshot) is not None or parse_clip_notes_phrase(text, self.snapshot) is not None:
            return None, 0
        local = parse_local(text, self.snapshot)
        jev_ms = 0
        if local is not None:
            if self._dispatch_only_action(local.action, None, dispatch=False) is not None:
                return None, jev_ms
            result = self._resolve_previous(IntentResult(local, (), (), ()), text)
        else:
            if not self.key:
                return None, 0
            began = time.perf_counter()
            selected_track_index = self._selected_track_index()
            jev_request = build_request(self.snapshot, text, selected_track_index)
            try:
                response = self.requester(jev_request, self.key)
            except RuntimeError:
                return None, 0
            jev_ms = round((time.perf_counter() - began) * 1000)
            detail_tracks = jev_request["state"].get("detail_tracks", ())
            result = interpret_response(self.snapshot, text, response, detail_tracks)
            if self._dispatch_only_action(result.intent.action, None, dispatch=False) is not None:
                return None, jev_ms
            result = self._step_from_utterance(result, text)
            result = self._resolve_previous(result, text)
        intent = result.intent
        if (
            inherited is not None
            and inherited.target_origin in {TargetOrigin.NAMED, TargetOrigin.CLARIFIED}
            and isinstance(inherited.track, int)
            and intent.track is None
            and not intent.tracks
            and intent.named_evidence < TRACK_UNSTATED_MAX
            and ACTIONS[intent.action].needs_track
        ):
            result = replace(result, intent=replace(intent, track=inherited.track, track_conf=1.0, target_origin=TargetOrigin.CLARIFIED))
        result = self._apply_selected_track(self._resolve_release_target(result))
        result = self._resolve_clip_target(result)
        result = self._resolve_device_name(result, result.intent.named_evidence >= TRACK_STATED_MIN)
        intent = result.intent
        if ACTIONS[intent.action].kind in {"plugin", "plugin_track", "structure", "structure_device", "clip_call", "scene_call", "song_call", "track_call", "transport"}:
            return None, jev_ms
        old_pending = self.pending
        decision = self._decision(result, None)
        self.pending = old_pending
        if decision is not None or self._authorize_target(intent) is not None:
            return None, jev_ms
        return intent, jev_ms

    def _confirm_chain_names(self, intents: tuple[Intent, ...]) -> int:
        assert self.snapshot is not None
        indices: list[int] = []
        for intent in intents:
            members = intent.tracks if intent.tracks else ((intent.track,) if isinstance(intent.track, int) else ())
            for index in members:
                if index not in indices:
                    indices.append(index)
        return self._confirm_multi_track_names(tuple(indices)) if indices else 0

    def _read_receipt_value(self, entry: ReceiptEntry) -> tuple[Any, str, str | None]:
        """Read the live value behind a receipt entry with the reads the allow-list permits.
        A plain get of "value" is only allowed on sends; faders and device parameters have their own read commands."""
        mixer = re.fullmatch(r"live_set (?:(tracks|return_tracks) (\d+)|(master_track)) mixer_device (volume|panning)", entry.path)
        if mixer:
            owner_path = "live_set master_track" if mixer.group(3) else f"live_set {mixer.group(1)} {mixer.group(2)}"
            target = "master" if mixer.group(3) else mixer.group(2) if mixer.group(1) == "tracks" else owner_path
            arguments: list[str] = []
            name_id = None
            if not mixer.group(3):
                name_id = request_id("restore-owner")
                arguments.extend(["--api-get", owner_path, "name", name_id])
            arguments.extend(["--api-mixer-status", target, request_id("restore-read")])
            result = self.bridge.run(arguments)
            ack = next((item for item in reversed(result.acks) if item.event == "api_mixer_status"), None)
            if ack is None and name_id:
                mixer_result = self.bridge.run(["--api-mixer-status", target, request_id("restore-read")])
                result = BridgeResult(tuple(result.acks) + tuple(mixer_result.acks), result.elapsed_ms + mixer_result.elapsed_ms, mixer_result.returncode, mixer_result.timed_out)
                ack = next((item for item in reversed(result.acks) if item.event == "api_mixer_status"), None)
            parameters = ack.payload.get("parameters") if ack and isinstance(ack.payload, Mapping) else None
            parameter = parameters.get(mixer.group(4)) if isinstance(parameters, Mapping) else None
            if not isinstance(parameter, Mapping) or "value" not in parameter:
                raise BridgeError("restore read failed")
            owner = str(ack_map(result)[name_id].payload) if name_id else "master"
            return float(parameter["value"]), owner, None
        if " devices " in entry.path and " parameters " in entry.path:
            device_path = entry.path.rsplit(" parameters ", 1)[0]
            track_path = entry.path.split(" devices ", 1)[0]
            track_name_id, device_name_id = request_id("restore-owner"), request_id("restore-device")
            result = self.bridge.run(["--api-get", track_path, "name", track_name_id, "--api-get", device_path, "name", device_name_id, "--api-device-parameters", device_path, request_id("restore-read")])
            ack = next((item for item in reversed(result.acks) if item.event == "api_device_parameters"), None)
            parameters = ack.payload.get("parameters") if ack and isinstance(ack.payload, Mapping) else []
            for raw in parameters if isinstance(parameters, list) else []:
                if isinstance(raw, Mapping) and raw.get("path") == entry.path and "value" in raw:
                    found = ack_map(result)
                    return float(raw["value"]), str(found[track_name_id].payload), str(found[device_name_id].payload)
            raise BridgeError("restore read failed")
        read_id = request_id("restore-read")
        track_match = re.match(r"(live_set (?:tracks|return_tracks) \d+)(?: |$)", entry.path)
        if track_match and entry.write_kind != "rename":
            owner_id = request_id("restore-owner")
            result = self.bridge.run(["--api-get", track_match.group(1), "name", owner_id, "--api-get", entry.path, entry.prop, read_id])
            return _find(result, read_id).payload, str(_find(result, owner_id).payload), None
        result = self.bridge.run(["--api-get", entry.path, entry.prop, read_id])
        return _find(result, read_id).payload, entry.owner, None

    def _restore_receipt(self, receipt: Receipt, unresolved_values: list[Any] | None = None) -> Receipt | None:
        if not receipt.entries:
            return receipt
        unresolved: list[ReceiptEntry] = []
        for entry in reversed(receipt.entries):
            try:
                current, owner, device_owner = self._read_receipt_value(entry)
                if owner != entry.owner or (entry.device_owner is not None and device_owner != entry.device_owner):
                    unresolved.append(entry)
                    if unresolved_values is not None:
                        unresolved_values.append(current)
                    continue
                if self._same_restored_value(current, entry.before):
                    continue
                if not self._same_value(current, entry.after):
                    unresolved.append(entry)
                    if unresolved_values is not None:
                        unresolved_values.append(current)
                    continue
                if entry.write_kind == "tempo":
                    write = ["--write", "--tempo", f"{float(entry.before):g}"]
                elif entry.write_kind == "rename" and entry.path.startswith("live_set tracks "):
                    index = entry.path.split()[2]
                    write = ["--write", "--rename-track-index", index, "--rename-track-name", str(entry.before)]
                elif entry.write_kind == "rename":
                    write = ["--write", "--api-set", entry.path, "name", json.dumps(str(entry.before), ensure_ascii=False), request_id("restore")]
                elif entry.parameter:
                    write = ["--write", "--api-parameter-set", entry.path, json.dumps(entry.before), request_id("restore")]
                else:
                    raw = str(int(entry.before)) if isinstance(entry.before, bool) else json.dumps(entry.before)
                    write = ["--write", "--api-set", entry.path, entry.prop, raw, request_id("restore")]
                result = self.bridge.run(write)
                if result.timed_out:
                    pass
                restored, restored_owner, restored_device = self._read_receipt_value(entry)
                if restored_owner != entry.owner or (entry.device_owner is not None and restored_device != entry.device_owner) or not self._same_restored_value(restored, entry.before):
                    unresolved.append(entry)
                    continue
                match = re.fullmatch(r"live_set (tracks|return_tracks) (\d+)", entry.path)
                if match and self.snapshot is not None:
                    ref: int | TargetRef = int(match.group(2)) if match.group(1) == "tracks" else TargetRef(TargetKind.RETURN, int(match.group(2)))
                    self.snapshot = replace_target(self.snapshot, ref, **{entry.prop: entry.before})
                elif entry.path == "live_set master_track" and self.snapshot is not None:
                    self.snapshot = replace_target(self.snapshot, TargetRef(TargetKind.MASTER), **{entry.prop: entry.before})
            except Exception:
                unresolved.append(entry)
        return replace(receipt, entries=tuple(reversed(unresolved))) if unresolved else None

    def _rollback_transaction(self, transaction: Transaction) -> list[tuple[str, Receipt]]:
        unresolved_pairs: list[tuple[str, Receipt]] = []
        for clause, receipt in reversed(transaction.pairs):
            unresolved = self._restore_receipt(receipt)
            if unresolved is not None:
                unresolved_pairs.append((clause, unresolved))
        return unresolved_pairs

    def _process_chain(
        self,
        clauses: list[str],
        message_id: Any,
        started: float,
        jev_ms: int = 0,
        llm_ms: int = 0,
        rewritten: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self._ensure_script_current():
            return self._outdated_response(message_id, started)
        self.pending = None
        planned: list[Intent] = []
        inherited: Intent | None = None
        for clause in clauses:
            intent, elapsed = self._plan_chain_clause(clause, inherited)
            jev_ms += elapsed
            if intent is None:
                return {"id": message_id, "kind": "info", "line": self._m("info.chain_unclear", clause=clause), "ms": self._ms(started, jev_ms, llm_ms, 0)}
            if ACTIONS[intent.action].kind not in {"track_bool", "track_int", "mixer", "send", "param", "clip_prop", "song_bool", "jump", "tempo", "rename"}:
                return {"id": message_id, "kind": "info", "line": self._m("info.chain_unsupported", clause=clause), "ms": self._ms(started, jev_ms, llm_ms, 0)}
            planned.append(intent)
            inherited = intent
        bridge_ms = self._confirm_chain_names(tuple(planned))
        receipts: list[Receipt] = []
        receipt_clauses: list[str] = []
        results: list[dict[str, Any]] = []
        for clause, intent in zip(clauses, planned):
            try:
                result, receipt = self._execute_now(intent, message_id, 0, 0, started, clause, None)
            except Exception:
                result, receipt = {"kind": "error"}, None
            if result.get("kind") not in {"result", "info"}:
                if receipt is not None:
                    receipts.append(receipt)
                    receipt_clauses.append(clause)
                transaction = Transaction(list(zip(receipt_clauses, receipts)))
                failures = self._rollback_transaction(transaction)
                self.previous = PreviousChain(tuple(item for _clause, item in reversed(failures)), tuple(clause for clause, _item in reversed(failures))) if failures else None
                self._history_kind = "receipt" if failures else "nonreceipt"
                key = "error.chain_partial" if failures else "error.chain_rolled_back"
                values = {"clause": ", ".join(clause for clause, _receipt in failures)} if failures else {}
                answer = {"id": message_id, "kind": "error", "line": self._m(key, **values), "ms": self._ms(started, jev_ms, llm_ms, bridge_ms)}
                if rewritten is not None:
                    answer["decision"] = {"rewritten": rewritten, "chain": [item.get("decision", {}) for item in results]}
                return answer
            results.append(result)
            if receipt is not None:
                receipts.append(receipt)
                receipt_clauses.append(clause)
        history = PreviousChain(tuple(receipts), tuple(receipt_clauses))
        decisions = [item.get("decision", {}) for item in results]
        answer = {
            "id": message_id,
            "kind": "result",
            "line": " → ".join(str(item.get("line", "")) for item in results),
            "decision": {"chain": decisions, **({"rewritten": rewritten} if rewritten is not None else {})},
            "ms": self._ms(started, jev_ms, llm_ms, bridge_ms + sum(int(item.get("ms", {}).get("bridge", 0)) for item in results)),
        }
        return self._record_execution_history(answer, history)

    def _device_named_in_text(self, result: IntentResult, text: str, track_named: bool) -> IntentResult:
        """Jev was measured at 0.26-0.38 on "Reverbをオフ" even though the device name is spelled out; match it literally instead of asking."""
        intent = result.intent
        if intent.action not in {Action.DEVICE_ON, Action.DEVICE_OFF} or self.snapshot is None or intent.named_evidence >= TRACK_UNSTATED_MAX:
            return result
        if intent.device is not None and intent.device_conf >= 0.6:
            if intent.named_evidence >= TRACK_UNSTATED_MAX:
                return result
            intent = replace(intent, device_name=intent.device.name, device=None, device_conf=0.0, param=None, param_conf=0.0)
            return self._resolve_device_name(replace(result, intent=intent), False)
        folded = text.casefold()
        names = {
            device.name for track in addressable_targets(self.snapshot)
            if not (isinstance(track, Track) and is_bridge_track(track))
            for device in track.devices if device.name and device.name.casefold() in folded
        }
        if len(names) != 1:
            return result
        return self._resolve_device_name(replace(result, intent=replace(intent, device_name=next(iter(names)))), track_named)

    def _resolve_clip_target(self, result: IntentResult) -> IntentResult:
        intent = result.intent
        if intent.clip is None or not intent.clip_name or self.snapshot is None or intent.named_evidence >= TRACK_UNSTATED_MAX:
            return result
        owner = target_for_intent(self.snapshot, intent)
        if not isinstance(owner, Track):
            owner = None
        matches = [clip for clip in owner.clips if clip.slot == intent.clip] if owner and intent.target_origin is TargetOrigin.SELECTED else []
        if not matches:
            matches = [clip for clip in owner.clips if clip.name.casefold() == intent.clip_name.casefold()] if owner else []
        origin = intent.target_origin
        if not matches:
            global_matches = [(track, clip) for track in self.snapshot.tracks for clip in track.clips if clip.name.casefold() == intent.clip_name.casefold()]
            if len(global_matches) == 1:
                owner, clip = global_matches[0]
                matches = [clip]
                origin = TargetOrigin.OWNER
        if len(matches) != 1 or owner is None:
            return replace(result, intent=replace(intent, clip=None, clip_conf=0.0, clip_name=None, clip_path=None))
        clip = matches[0]
        return replace(result, intent=replace(intent, track=owner.index, track_conf=1.0, target_origin=origin, clip=clip.slot, clip_conf=1.0, clip_name=clip.name, clip_path=clip.path))

    def _resolve_device_name(self, result: IntentResult, track_named: bool) -> IntentResult:
        # track_named is captured before the selected-track default, which also sets track_stated to 1.0.
        intent = result.intent
        if not intent.device_name or self.snapshot is None:
            return result
        owner = self.snapshot.target(intent.track) if isinstance(intent.track, (int, TargetRef)) else None
        matches = [device for device in owner.devices if device.name.casefold() == intent.device_name.casefold()] if owner else []
        if not matches and intent.named_evidence < TRACK_UNSTATED_MAX:
            global_matches = [
                (track, device) for track in addressable_targets(self.snapshot) if not (isinstance(track, Track) and is_bridge_track(track))
                for device in track.devices if device.name.casefold() == intent.device_name.casefold()
            ]
            if len(global_matches) == 1:
                owner, device = global_matches[0]
                matches = [device]
                intent = replace(intent, track=target_ref(owner), track_conf=1.0, target_origin=TargetOrigin.OWNER)
        if len(matches) != 1:
            return replace(result, intent=replace(intent, device=None, device_conf=0.0, param=None, param_conf=0.0))
        device = matches[0]
        parameter = next((item for item in device.params if item.index == 0), None)
        return replace(result, intent=replace(intent, device=device, device_conf=1.0, param=parameter, param_conf=1.0 if parameter else 0.0))

    def _decision(
        self,
        result: IntentResult,
        message_id: Any,
        *,
        trusted_fields: frozenset[str] = frozenset(),
    ) -> dict[str, Any] | None:
        intent = result.intent
        if intent.compound > 0.7:
            self.pending = None
            return {"id": message_id, "kind": "info", "line": self._m("info.one_at_a_time")}
        if intent.action is Action.NONE and intent.needs_generation > 0.5:
            return {"id": message_id, "kind": "info", "line": self._m("info.freeform")}
        if (intent.action_conf < 0.6 and "action" not in trusted_fields) or intent.action is Action.NONE:
            return self._ask_for_action(result, message_id)
        dispatched = self._dispatch_only_action(intent.action, message_id)
        if dispatched is not None:
            return dispatched
        spec = ACTIONS[intent.action]
        if intent.target_origin in MULTI_TARGET_ORIGINS:
            self.pending = None
            return self._authorize_target(intent)
        if isinstance(intent.track, int) and not any(track.index == intent.track for track in self.snapshot.tracks):
            self.pending = None
            return {"id": message_id, "kind": "error", "line": self._m("error.named_track_missing")}
        if isinstance(intent.track, TargetRef) and self.snapshot.target(intent.track) is None:
            self.pending = None
            return {"id": message_id, "kind": "error", "line": self._m("error.named_track_missing")}
        if spec.needs_track and intent.track is None and intent.track_stated >= TRACK_STATED_MIN:
            self.pending = None
            return {"id": message_id, "kind": "error", "line": self._m("error.named_track_missing")}
        unsupported = target_capability_error(self.snapshot, intent)
        if unsupported is not None:
            self.pending = None
            target_name, action_name = unsupported
            return {"id": message_id, "kind": "error", "line": self._m("error.target_capability", target=target_name, action=action_name)}
        named_track_uncertain = (
            intent.track_stated >= TRACK_UNSTATED_MAX
            and isinstance(intent.track, (int, TargetRef))
            and intent.track_conf < NAMED_TRACK_CONF_MIN
        )
        track_is_uncertain = intent.track is None or named_track_uncertain
        if spec.needs_track and track_is_uncertain:
            self.pending = Pending(result, "track", self._clock(), message_id)
            return {"id": message_id, "kind": "ask", "line": self._m("ask.track"), "options": self._localized_options(result.track_options)}
        if spec.needs_param and (intent.param is None or (intent.param_conf < 0.6 and "param" not in trusted_fields)):
            if intent.target_origin is TargetOrigin.CLARIFIED:
                self.pending = None
                return {"id": message_id, "kind": "error", "line": self._m("error.target_changed")}
            self.pending = Pending(result, "param", self._clock(), message_id)
            return {"id": message_id, "kind": "ask", "line": self._m("ask.param"), "options": list(result.param_options)}
        if spec.kind == "structure_device" and (intent.native_device is None or intent.native_device_conf < 0.6):
            self.pending = Pending(result, "native_device", self._clock(), message_id)
            return {"id": message_id, "kind": "ask", "line": self._m("ask.device_native"), "options": ["Operator", "Wavetable", "Drum Rack", "Reverb", "EQ Eight"]}
        if spec.needs_send and (intent.send is None or (intent.send_conf < 0.6 and "send" not in trusted_fields)):
            self.pending = Pending(result, "send", self._clock(), message_id)
            return {"id": message_id, "kind": "ask", "line": self._m("ask.send"), "options": list(result.send_options)}
        if spec.needs_scene and (intent.scene is None or (intent.scene_conf < 0.6 and "scene" not in trusted_fields)):
            self.pending = Pending(result, "scene", self._clock(), message_id)
            return {"id": message_id, "kind": "ask", "line": self._m("ask.scene"), "options": list(result.scene_options)}
        if spec.needs_clip and (intent.clip is None or (intent.clip_conf < 0.6 and "clip" not in trusted_fields)):
            if intent.target_origin is TargetOrigin.CLARIFIED:
                self.pending = None
                return {"id": message_id, "kind": "error", "line": self._m("error.target_changed")}
            self.pending = Pending(result, "clip", self._clock(), message_id)
            return {"id": message_id, "kind": "ask", "line": self._m("ask.clip"), "options": list(result.clip_options)}
        if spec.needs_device and intent.device_name and intent.track_stated >= TRACK_STATED_MIN and intent.device is None:
            self.pending = None
            return {"id": message_id, "kind": "error", "line": self._m("error.named_device_missing")}
        if spec.needs_device and (
            intent.device is None
            or (intent.device_conf < 0.6 and "device" not in trusted_fields)
            or intent.param is None
        ):
            if intent.target_origin is TargetOrigin.CLARIFIED:
                self.pending = None
                return {"id": message_id, "kind": "error", "line": self._m("error.target_changed")}
            self.pending = Pending(result, "device", self._clock(), message_id)
            return {"id": message_id, "kind": "ask", "line": self._m("ask.device"), "options": list(result.device_options)}
        if spec.needs_step and intent.number is None and (
            intent.step in {Step.NONE, Step.SET}
            or (intent.step_conf < 0.6 and "step" not in trusted_fields)
        ):
            self.pending = Pending(result, "step", self._clock(), message_id)
            return {"id": message_id, "kind": "ask", "line": self._m("ask.direction"), "options": [self._m("option.up_small"), self._m("option.down_small")]}
        self.pending = None
        return None

    def _resolve_previous(self, result: IntentResult, text: str) -> IntentResult:
        intent = result.intent
        previous = self.previous
        if isinstance(previous, PreviousChain):
            previous = previous.items[-1] if previous.items else None
        if previous is None or intent.refers_previous <= 0.6:
            return result
        if intent.named_evidence >= TRACK_UNSTATED_MAX:
            return result
        if intent.track is not None and intent.track != previous.track:
            return result
        if previous.multi_before:
            return replace(result, intent=replace(
                intent,
                action=previous.action,
                action_conf=1.0,
                track=None,
                track_conf=0.0,
                tracks=previous.tracks,
                target_origin=previous.target_origin,
                refers_previous=0.0,
            ))
        if previous.step not in {Step.UP_SMALL, Step.UP_BIG, Step.DOWN_SMALL, Step.DOWN_BIG}:
            return result
        # "少し左" right after "右に振って" was scored as a continuation and repeated the move to the right.
        # An utterance that states the opposite direction is a new request, not "a little more".
        continuation_only = re.fullmatch(r"\s*(?:もう少し|もうちょい|more|a bit more|a little more|a touch more)\s*", text, re.IGNORECASE)
        spoken = None if continuation_only else direction_from_words(previous.action, text)
        ups, downs = {Step.UP_SMALL, Step.UP_BIG}, {Step.DOWN_SMALL, Step.DOWN_BIG}
        if spoken is not None and (spoken in ups) != (previous.step in ups):
            return result
        param = previous.param
        if param is not None and self.snapshot is not None:
            param = next(
                (
                    current
                    for track in self.snapshot.tracks
                    for device in track.devices
                    for current in device.params
                    if current.path == param.path
                ),
                param,
            )
        if previous.target_origin in {TargetOrigin.SELECTED, TargetOrigin.OWNER}:
            changes = {
                "action": previous.action, "action_conf": 1.0,
                "track": None, "track_conf": 0.0, "target_origin": TargetOrigin.NONE,
                "param": None, "param_conf": 0.0, "device": None, "device_conf": 0.0,
                "clip": None, "clip_conf": 0.0, "scene": previous.scene,
                "scene_conf": 1.0 if previous.scene is not None else 0.0,
                "send": previous.send, "send_conf": 1.0 if previous.send is not None else 0.0,
                "step": previous.step, "step_conf": 1.0,
            }
            return self._apply_selected_track(replace(result, intent=replace(intent, **changes)))
        changes: dict[str, Any] = {
            "action": previous.action,
            "action_conf": 1.0,
            "track": previous.track,
            "track_conf": 1.0,
            "target_origin": TargetOrigin.PREVIOUS,
            "param": param,
            "param_conf": 1.0 if param is not None else intent.param_conf,
            "step": previous.step,
            "step_conf": 1.0,
            "clip": previous.clip,
            "clip_conf": 1.0 if previous.clip is not None else 0.0,
            "scene": previous.scene,
            "scene_conf": 1.0 if previous.scene is not None else 0.0,
            "send": previous.send,
            "send_conf": 1.0 if previous.send is not None else 0.0,
            "device": previous.device,
            "device_conf": 1.0 if previous.device is not None else 0.0,
        }
        return replace(result, intent=replace(intent, **changes))

    def _resolve_release_target(self, result: IntentResult) -> IntentResult:
        assert self.snapshot is not None
        intent = result.intent
        if (intent.action not in {Action.UNMUTE, Action.UNSOLO}
                or intent.named_evidence >= TRACK_UNSTATED_MAX
                or intent.track is not None or intent.tracks):
            return result
        property_name = "mute" if intent.action is Action.UNMUTE else "solo"
        active = [
            track for track in self.snapshot.tracks
            if not is_bridge_track(track) and getattr(track, property_name)
        ]
        if len(active) != 1:
            return result
        return replace(
            result,
            intent=replace(intent, track=active[0].index, track_conf=1.0, target_origin=TargetOrigin.OWNER),
        )

    @staticmethod
    def _same_value(left: Any, right: Any) -> bool:
        if isinstance(left, str) or isinstance(right, str):
            return str(left) == str(right)
        if isinstance(left, bool) or isinstance(right, bool):
            # Transports differ: the legacy bridge reports 0/1 where the Remote Script reports false/true.
            return left in (0, 1, False, True) and right in (0, 1, False, True) and bool(left) == bool(right)
        return abs(float(left) - float(right)) <= 1e-4

    @staticmethod
    def _same_restored_value(left: Any, right: Any) -> bool:
        if isinstance(left, (str, bool)) or isinstance(right, (str, bool)):
            return LiveJevService._same_value(left, right)
        return abs(float(left) - float(right)) <= 1e-9

    @staticmethod
    def _value_before(snapshot: Snapshot, intent: Intent) -> Any:
        target = snapshot.target(intent.track) if isinstance(intent.track, (int, TargetRef)) or intent.track == "master" else None
        if intent.action is Action.VOLUME:
            return target.volume if target is not None else snapshot.master_volume
        if intent.action is Action.PAN:
            return target.pan
        if intent.action in {Action.MUTE, Action.UNMUTE}:
            return target.mute
        if intent.action in {Action.SOLO, Action.UNSOLO}:
            return target.solo
        if intent.action is Action.TEMPO:
            return snapshot.tempo
        if intent.action in {Action.PLAY, Action.STOP}:
            return snapshot.playing
        if ACTIONS[intent.action].kind == "param" and intent.param is not None:
            return next(
                parameter.value
                for track in addressable_targets(snapshot)
                for device in track.devices
                for parameter in device.params
                if parameter.path == intent.param.path
            )
        spec = ACTIONS[intent.action]
        if spec.kind in {"track_bool", "track_int"} and target is not None and spec.prop:
            return getattr(target, spec.prop)
        if spec.kind == "song_bool" and spec.prop:
            return bool(snapshot.song.get(spec.prop))
        if spec.kind == "jump":
            return float(snapshot.song.get("current_song_time", 0.0) or 0.0)
        if spec.kind in {"song_call", "track_call", "transport", "clip_call", "scene_call"}:
            return snapshot.playing
        if spec.kind == "send" and isinstance(intent.track, int) and intent.send is not None:
            track = next(item for item in snapshot.tracks if item.index == intent.track)
            return track.sends[intent.send] if intent.send < len(track.sends) else 0.0
        if spec.kind == "rename" and target is not None:
            return target.name
        if spec.kind in {"structure", "structure_device", "plugin", "plugin_track"}:
            return float(len(snapshot.tracks))
        if spec.kind == "clip_prop" and isinstance(intent.track, int) and intent.clip is not None and spec.prop:
            track = next(item for item in snapshot.tracks if item.index == intent.track)
            clip = next((item for item in track.clips if item.slot == intent.clip), None)
            raw = clip.props.get(spec.prop) if clip else None
            if spec.prop in {"looping", "warping"}:
                return bool(raw)
            return float(raw or 0.0)
        raise ValueError("直前の値を保存できません")

    def _fill_pending(self, text: str) -> IntentResult | None | object:
        pending = self.pending
        self.pending = None
        if pending is None or self.snapshot is None:
            return None
        normalized = _normalize(text)
        intent = pending.result.intent
        if pending.field == "track":
            for track in addressable_targets(self.snapshot):
                if isinstance(track, Track) and is_bridge_track(track):
                    continue
                if normalized == _normalize(track.name):
                    device_name = intent.device.name if intent.device is not None else None
                    if device_name is None and intent.param is not None:
                        device_name = next((d.name for t in addressable_targets(self.snapshot) for d in t.devices if intent.param in d.params), None)
                    param_name = intent.param.name if intent.param is not None else None
                    clip_name = None
                    if isinstance(intent.track, int) and intent.clip is not None:
                        old = next((t for t in self.snapshot.tracks if t.index == intent.track), None)
                        clip_name = next((c.name for c in old.clips if c.slot == intent.clip), None) if old else None
                    device = next((d for d in track.devices if device_name and d.name.casefold() == device_name.casefold()), None)
                    param = next((p for p in device.params if param_name and p.name.casefold() == param_name.casefold()), None) if device else None
                    clip = next((c for c in getattr(track, "clips", ()) if clip_name and c.name.casefold() == clip_name.casefold()), None)
                    return replace(pending.result, intent=replace(
                        intent, track=target_ref(track), track_conf=1.0, target_origin=TargetOrigin.CLARIFIED,
                        device=device, device_conf=1.0 if device else 0.0,
                        param=param, param_conf=1.0 if param else 0.0,
                        clip=clip.slot if clip else None, clip_conf=1.0 if clip else 0.0,
                        clip_name=clip.name if clip else None, clip_path=clip.path if clip else None,
                    ))
                if isinstance(track, ReturnTrack):
                    letter = chr(ord("A") + track.index)
                    number = track.index + 1
                    ordinal = f"{number}{'st' if number % 10 == 1 and number % 100 != 11 else 'nd' if number % 10 == 2 and number % 100 != 12 else 'rd' if number % 10 == 3 and number % 100 != 13 else 'th'}"
                    aliases = (
                        f"リターン{letter}", f"return {letter}", f"リターン{number}",
                        f"リターントラック{number}", f"{number}番目のリターン", f"リターンの{number}番",
                        f"return {number}", f"return track {number}", f"{ordinal} return", f"the {ordinal} return",
                    )
                    if number <= 3:
                        aliases += (f"{('first', 'second', 'third')[number - 1]} return",)
                    if normalized in {_normalize(alias) for alias in aliases}:
                        return replace(pending.result, intent=replace(intent, track=track.ref, track_conf=1.0, target_origin=TargetOrigin.CLARIFIED, utterance=f"{intent.utterance} {text}"))
                if track.path == "live_set master_track" and normalized in {_normalize("マスター"), _normalize("master"), _normalize("master track")}:
                    return replace(pending.result, intent=replace(intent, track=track.ref, track_conf=1.0, target_origin=TargetOrigin.CLARIFIED, utterance=f"{intent.utterance} {text}"))
        elif pending.field == "param" and isinstance(intent.track, (int, TargetRef)):
            track = self.snapshot.target(intent.track)
            candidates = candidate_params(self.snapshot).get(intent.track, {})
            for parameter in candidates.values():
                device = next((item for item in track.devices if parameter in item.params), None) if track else None
                labels = {_normalize(parameter.name)}
                if device is not None:
                    labels.add(_normalize(f"{device.name}: {parameter.name}"))
                if normalized in labels:
                    return replace(pending.result, intent=replace(intent, param=parameter, param_conf=1.0))
        elif pending.field == "native_device":
            resolved = resolve_native_device(text)
            if resolved is not None:
                return replace(pending.result, intent=replace(intent, native_device=resolved, native_device_conf=1.0))
        elif pending.field == "plugin":
            for option in pending.options:
                if normalized == _normalize(option):
                    return replace(pending.result, intent=replace(intent, plugin=option, utterance=f"{intent.utterance} {text}".strip()))
        elif pending.field == "send":
            for returned in self.snapshot.returns:
                letter = chr(ord("A") + returned.index)
                name = returned.name
                if normalized in {_normalize(name), _normalize(letter), _normalize(f"センド{letter}"), _normalize(f"send {letter}"), _normalize(f"{letter}（{name}）"), _normalize(f"{letter} ({name})")}:
                    return replace(pending.result, intent=replace(intent, send=returned.index, send_conf=1.0))
        elif pending.field == "scene":
            for scene in self.snapshot.scenes:
                if normalized in {_normalize(scene.name), _normalize(f"シーン{scene.index + 1}"), _normalize(f"scene {scene.index + 1}"), str(scene.index + 1)}:
                    return replace(pending.result, intent=replace(intent, scene=scene.index, scene_conf=1.0))
        elif pending.field == "clip" and isinstance(intent.track, int):
            track = next((item for item in self.snapshot.tracks if item.index == intent.track), None)
            for clip in track.clips if track else ():
                if normalized in {_normalize(clip.name), _normalize(f"スロット{clip.slot + 1}"), _normalize(f"slot {clip.slot + 1}"), str(clip.slot + 1)}:
                    return replace(pending.result, intent=replace(intent, clip=clip.slot, clip_conf=1.0))
        elif pending.field == "device" and isinstance(intent.track, (int, TargetRef)):
            track = self.snapshot.target(intent.track)
            for device in track.devices if track else ():
                if normalized in {_normalize(device.name), str(device.index + 1)}:
                    on_param = next((p for p in device.params if p.index == 0), None)
                    return replace(pending.result, intent=replace(intent, device=device, device_conf=1.0, param=on_param, param_conf=1.0))
        elif pending.field == "step":
            choices = {
                _normalize("少し上げる"): Step.UP_SMALL, _normalize("上げる"): Step.UP_SMALL,
                _normalize("かなり上げる"): Step.UP_BIG, _normalize("少し下げる"): Step.DOWN_SMALL,
                _normalize("下げる"): Step.DOWN_SMALL, _normalize("かなり下げる"): Step.DOWN_BIG,
                _normalize("raise a little"): Step.UP_SMALL, _normalize("raise"): Step.UP_SMALL,
                _normalize("raise a lot"): Step.UP_BIG, _normalize("lower a little"): Step.DOWN_SMALL,
                _normalize("lower"): Step.DOWN_SMALL, _normalize("lower a lot"): Step.DOWN_BIG,
            }
            if normalized in choices:
                return replace(pending.result, intent=replace(intent, step=choices[normalized], step_conf=1.0))
        elif pending.field == "action":
            for name, label in ACTION_LABELS.items():
                if normalized in {_normalize(label), _normalize(action_label(name, lang="en"))} and name != "none":
                    return replace(pending.result, intent=replace(intent, action=Action(name), action_conf=1.0))
            local = parse_local(text, self.snapshot)
            carries_command_detail = local is not None and (
                local.track is not None
                or bool(local.tracks)
                or local.target_origin is not TargetOrigin.NONE
                or local.named_evidence >= TRACK_UNSTATED_MAX
                or local.track_stated >= TRACK_UNSTATED_MAX
                or local.number is not None
                or local.text is not None
                or local.device is not None
                or local.device_name is not None
                or local.send is not None
                or local.scene is not None
                or local.clip is not None
                or local.plugin is not None
                or local.native_device is not None
            )
            if local is not None and local.action is not Action.NONE and not carries_command_detail:
                return replace(pending.result, intent=replace(intent, action=local.action, action_conf=1.0))
            if carries_command_detail:
                return _REPROCESS_PENDING
            if extract_plugin_request(text, self.snapshot) is not None or len(split_compound(text, self.snapshot)) > 1:
                return _REPROCESS_PENDING
        if pending.field != "action" and (
            len(split_compound(text, self.snapshot)) > 1
            or extract_plugin_request(text, self.snapshot) is not None
            or ((local := parse_local(text, self.snapshot)) is not None and local.action is not Action.NONE)
        ):
            return _REPROCESS_PENDING
        return None

    pending_confirm: tuple[Any, ...] | None = None

    def _execute(
        self,
        intent: Intent,
        message_id: Any,
        jev_ms: int,
        llm_ms: int,
        started: float,
        utterance: str,
        rewritten: list[str] | None,
        force_confirm: bool = False,
        trusted_fields: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        if not self._ensure_script_current():
            return self._outdated_response(message_id, started)
        dispatched = self._dispatch_only_action(intent.action, message_id, started)
        if dispatched is not None:
            return dispatched
        spec = ACTIONS[intent.action]
        if force_confirm or ((spec.confirm or intent.target_origin in MULTI_TARGET_ORIGINS) and REQUIRE_CONFIRM):
            self.pending_confirm = (intent, jev_ms, llm_ms, utterance, rewritten, force_confirm, trusted_fields, message_id)
            self.pending_confirm_created = self._clock()
            label = action_label(intent.action.value, lang=self.lang)
            target = ""
            if isinstance(intent.track, int) and self.snapshot is not None:
                track = next((item for item in self.snapshot.tracks if item.index == intent.track), None)
                target = (f"{track.name} の" if self.lang == "ja" else f"{track.name}: ") if track else ""
            if intent.text and intent.action is Action.RENAME:
                detail = f"（→「{intent.text}」）" if self.lang == "ja" else f" to {intent.text}"
            elif intent.text:
                detail = f"（名前: {intent.text}）" if self.lang == "ja" else f" named {intent.text}"
            else:
                detail = ""
            if force_confirm:
                parts = []
                if intent.param is not None:
                    parts.append(self._param_label(self.snapshot, intent) or intent.param.name)
                if intent.step is not Step.NONE:
                    parts.append(step_label(intent.step.value, lang=self.lang))
                if intent.number is not None:
                    parts.append(f"{intent.number.value:g} {intent.number.unit}")
                if intent.send is not None:
                    parts.append(f"Send {intent.send + 1}")
                if intent.clip is not None:
                    slot = self._m("readback.slot", slot=intent.clip + 1)
                    parts.append(f"{intent.clip_name} ({slot})" if intent.clip_name else slot)
                if intent.scene is not None:
                    scene = next((item for item in self.snapshot.scenes if item.index == intent.scene), None) if self.snapshot else None
                    ordinal = f"シーン{intent.scene + 1}" if self.lang == "ja" else f"Scene {intent.scene + 1}"
                    parts.append(f"{scene.name} ({ordinal})" if scene else ordinal)
                if parts:
                    detail += f" ({' / '.join(parts)})"
            if intent.action is Action.ADD_TRACK_WITH_DEVICE:
                kind_label = self._m("kind.return_track" if intent.track_kind == "return" else "kind.audio_track" if intent.track_kind == "audio" else "kind.midi_track")
                label = f"「{intent.native_device}」入りの{kind_label}追加" if self.lang == "ja" else f"add {kind_label} with {intent.native_device}"
            if intent.action is Action.ADD_TRACK_WITH_PLUGIN:
                kind_label = self._m("kind.return_track" if intent.track_kind == "return" else "kind.audio_track" if intent.track_kind == "audio" else "kind.midi_track")
                label = f"「{intent.plugin}」入りの{kind_label}追加" if self.lang == "ja" else f"add {kind_label} with {intent.plugin}"
            if intent.action is Action.INSERT_PLUGIN:
                label = f"「{intent.plugin}」の挿入" if self.lang == "ja" else f"insert {intent.plugin}"
            return {
                "id": message_id,
                "kind": "confirm",
                "line": self._m("confirm.operation", target=target, label=label, detail=detail),
                "options": [self._m("option.yes"), self._m("option.cancel")],
                "ms": self._ms(started, jev_ms, llm_ms, 0),
            }
        result, receipt = self._execute_now(intent, message_id, jev_ms, llm_ms, started, utterance, rewritten)
        return self._record_execution_history(result, receipt)

    def _record_execution_history(
        self,
        result: dict[str, Any],
        history: Receipt | PreviousChain | None,
    ) -> dict[str, Any]:
        if history is not None:
            self.previous = history
            self._history_kind = "receipt"
        elif result.get("kind") == "result":
            self.previous = None
            self._history_kind = "marker" if self._undo_target_tracks is not None else "nonreceipt"
        return result

    _plugin_names_cache: tuple[str, ...] | None = None
    _plugin_uris: dict[str, str] = {}
    _plugin_script_ok: bool | None = None

    def _plugin_names(self) -> tuple[str, ...]:
        if self._plugin_names_cache is not None:
            return self._plugin_names_cache
        names: tuple[str, ...] = ()
        script_ok = plugin_script.ping()
        if script_ok:
            self._plugin_script_ok = True
            try:
                items = plugin_script.list_plugins()
                names = tuple(sorted({str(item.get("name")) for item in items if item.get("name")}))
                self._plugin_uris = preferred_plugin_uris(items)
            except plugin_script.ScriptError:
                names = ()
        if not names:
            names = load_plugin_catalog()
        if names:
            self._plugin_names_cache = names
        return names

    def _process_plugin_request(
        self,
        request: PluginRequest,
        text: str,
        message_id: Any,
        started: float,
        jev_ms: int = 0,
        *,
        initial_result: IntentResult | None = None,
    ) -> dict[str, Any]:
        """Handle an external plug-in request by resolving its name and selected track, then executing it."""
        catalog = self._plugin_names()
        native = resolve_native_device(request.raw_name)
        if native is not None and native not in catalog:
            catalog = catalog + (native,)
        if request.target_text:
            whole_names = (
                f"{request.target_text}に{request.raw_name}",
                f"{request.raw_name} on {request.target_text}",
                f"{request.raw_name} onto {request.target_text}",
                f"{request.raw_name} to {request.target_text}",
                f"{request.raw_name} in {request.target_text}",
            )
            whole_plugin = next(
                (
                    catalog_name for name in whole_names for catalog_name in catalog
                    if catalog_name.casefold().replace(" ", "") == name.casefold().replace(" ", "")
                ),
                None,
            )
            if whole_plugin is not None:
                request = replace(request, raw_name=whole_plugin, track=None, target_text=None, target_missing=False)
        if request.target_missing:
            return {"id": message_id, "kind": "error", "line": self._m("error.named_track_missing"), "ms": self._ms(started, jev_ms, 0, 0)}
        candidates = plugin_name_candidates(request.raw_name, catalog)
        if len(candidates) > 1:
            unresolved = replace(plugin_intent(request, request.raw_name), plugin=None, utterance=text)
            if request.action is Action.INSERT_PLUGIN and (request.track is None or request.track == "selected"):
                original = initial_result.intent if initial_result is not None else None
                unresolved = replace(
                    unresolved,
                    target_origin=TargetOrigin.SELECTED,
                    named_evidence=original.named_evidence if original is not None else 0.0,
                    track_stated=original.track_stated if original is not None else 0.0,
                )
            self.pending = Pending(IntentResult(unresolved, (), (), ()), "plugin", self._clock(), message_id, candidates)
            answer = self._plugin_candidate_question(message_id, candidates)
            answer["ms"] = self._ms(started, jev_ms, 0, 0)
            return answer
        plugin = candidates[0] if candidates else None
        if plugin is None and catalog and self.key:
            try:
                plugin, picked_ms = self._pick_plugin_with_jev(request.raw_name, catalog)
            except JevRequestTooLarge:
                return {"id": message_id, "kind": "error", "line": self._m("error.jev_too_large")}
            jev_ms += picked_ms
        if plugin is None:
            not_found = {"id": message_id, "kind": "info", "line": self._m("plugin.not_found", name=request.raw_name)}
            unknown = {"id": message_id, "kind": "info", "line": self._m("llm.response")}
            if initial_result is None:
                unresolved = plugin_intent(request, request.raw_name)
                unresolved = replace(unresolved, utterance=text)
                initial_result = IntentResult(unresolved, (), (), ())
            return self._rewrite_and_process(
                text,
                initial_result,
                not_found,
                message_id,
                jev_ms,
                started,
                semantic_fallback=unknown,
            )
        selected_target = request.action is Action.INSERT_PLUGIN and (request.track is None or request.track == "selected")
        if selected_target:
            index = self._selected_track_index()
            if index is None or any(is_bridge_track(t) and t.index == index for t in self.snapshot.tracks):
                return {"id": message_id, "kind": "ask", "line": self._m("ask.insert_track"), "options": [t.name for t in self.snapshot.tracks if not is_bridge_track(t)][:5], "ms": self._ms(started, jev_ms, 0, 0)}
            request = replace(request, track=index)
        plugin_target = plugin_intent(request, plugin)
        if selected_target:
            original = initial_result.intent if initial_result is not None else None
            plugin_target = replace(
                plugin_target,
                target_origin=TargetOrigin.SELECTED,
                named_evidence=original.named_evidence if original is not None else 0.0,
                track_stated=original.track_stated if original is not None else 0.0,
                utterance=text,
            )
        else:
            plugin_target = replace(
                plugin_target, utterance=text,
                target_origin=plugin_target.target_origin if isinstance(plugin_target.track, TargetRef) and plugin_target.track.kind is TargetKind.MASTER else TargetOrigin.NAMED if request.target_text else plugin_target.target_origin,
                named_evidence=1.0 if request.target_text else plugin_target.named_evidence,
                track_stated=1.0 if request.target_text else plugin_target.track_stated,
            )
        result = self._apply_selected_track(IntentResult(plugin_target, (), (), ()))
        decision = self._decision(result, message_id)
        if decision is not None:
            decision["ms"] = self._ms(started, jev_ms, 0, 0)
            return decision
        return self._execute(result.intent, message_id, jev_ms, 0, started, text, None)

    def _short_line(self, line: str) -> str:
        """Remove the leading "<track name>: " from result text. The target remains in decision.track, so users do not need it repeated."""
        names = [track.name for track in addressable_targets(self.snapshot) if track.name] if self.snapshot else []
        for name in sorted(names + ["マスター", "Master", "Main"], key=len, reverse=True):
            if line.startswith(f"{name}: "):
                return line[len(name) + 2:]
        return line

    def _script_available(self) -> bool:
        if self._plugin_script_ok:
            return True
        if plugin_script.ping():
            self._plugin_script_ok = True
            return True
        return False

    CLIP_NOTES_ERRORS = {
        "no_clip": "clip.no_clip", "midi_only": "clip.midi_only", "no_notes": "clip.no_notes",
        "out_of_range": "clip.out_of_range", "bad_grid": "clip.bad_grid",
        "unknown_action": "clip.old_script", "unknown_op": "clip.old_script", "main_thread_timeout": "clip.timeout",
    }

    def _run_clip_notes(self, request: ClipNotesRequest, text: str, message_id: Any, started: float) -> dict[str, Any]:
        """Transform clip notes by quantizing, applying legato, transposing, changing velocity, or doubling the loop. The Live component groups it into one undo step."""
        self._undo_target_tracks = None
        fields: dict[str, Any] = {}
        if request.op == "quantize":
            fields = {"grid": request.grid, "amount": request.amount}
        elif request.op == "transpose":
            fields = {"semitones": request.semitones}
        elif request.op == "velocity":
            fields = {"factor": request.factor, "value": request.value}
        if request.track is not None:
            expected = next((item for item in self.snapshot.tracks if item.index == request.track), None)
            if expected is None:
                return {"id": message_id, "kind": "error", "line": self._m("error.named_track_missing"), "ms": self._ms(started, 0, 0, 0)}
            authorization = _local_intent(
                Action.CLIP_PITCH, track=request.track,
                target_origin=TargetOrigin.NAMED, track_stated=1.0, utterance=text,
            )
            denied = self._authorize_target(authorization)
            if denied is not None:
                return {"id": message_id, **denied, "ms": self._ms(started, 0, 0, 0)}
            fields["track_name"] = expected.name  # Refuse to write if Live reports a different name, preventing an index shift from changing the wrong track.
        script_started = time.perf_counter()
        try:
            answer = plugin_script.clip_notes(request.op, request.track, request.slot, **fields)
        except plugin_script.ScriptError as error:
            if str(error) == "track_changed":
                raise StaleSnapshot() from error
            key = self.CLIP_NOTES_ERRORS.get(str(error))
            line = self._m(key) if key else self._m("error.generic") if self.lang == "en" else str(error)
            return {"id": message_id, "kind": "error", "line": line, "ms": self._ms(started, 0, 0, 0)}
        script_ms = round((time.perf_counter() - script_started) * 1000)
        if request.op == "quantize":
            if self.lang == "en":
                grid = request.grid
            else:
                grid = request.grid.replace("1/", "").replace("t", "分3連") if request.grid.endswith("t") else request.grid.replace("1/", "") + "分"
            amount = "" if request.amount >= 0.999 else self._m("clip.amount", percent=round(request.amount * 100))
            what = self._m("clip.quantized", grid=grid, amount=amount)
        elif request.op == "legato":
            what = self._m("clip.legato")
        elif request.op == "transpose":
            octaves, rest = divmod(abs(request.semitones), 12)
            size = self._m("clip.octaves", count=octaves) if rest == 0 and octaves else self._m("clip.semitones", count=abs(request.semitones))
            direction = self._m("clip.up" if request.semitones > 0 else "clip.down")
            what = self._m("clip.transpose", size=size, direction=direction)
        elif request.op == "velocity":
            what = self._m("clip.velocity_value", value=round(request.value)) if request.value is not None else self._m("clip.velocity_factor", value=round((request.factor or 1.0) * 100))
        else:
            what = self._m("clip.double")
        self.previous = None  # Live's single undo step handles this operation.
        return {
            "id": message_id, "kind": "result", "line": what,
            "decision": {"utterance": text, "rewritten": None, "action": f"clip_{request.op}", "action_label": "ノートの変形" if self.lang == "ja" else "Note transform", "track": answer.get("track") or None,
                         "param": None, "step_label": None, "number": None, "conf": {"action": 1.0, "track": None, "param": None}, "before": None, "after": None},
            "ms": {"jev": 0, "llm": 0, "bridge": script_ms, "total": round((time.perf_counter() - started) * 1000)},
        }

    def _bare_plugin_request(
        self,
        text: str,
        message_id: Any,
        started: float,
        jev_ms: int,
        *,
        initial_result: IntentResult | None = None,
    ) -> dict[str, Any] | None:
        """Handle verb-free requests equivalent to "Serum 2, please" only when the action is otherwise unresolved.
        Match the remaining words to a plug-in alias, exact name, or partial name without Jev, then insert a match on the selected track."""
        from intent import detect_language, normalize_phrase
        if detect_language(text) == "en":
            from intent_en import normalize_english_phrase
            name = re.sub(r"^(?:the|a|an|some)\s+", "", normalize_english_phrase(text)).strip()
        else:
            name = re.sub(r"(?:を|が|も)$", "", normalize_phrase(text)).strip()
        if not name or len(name) > 40:
            return None
        name = resolve_device_request_name(name)
        if name is None:
            return None
        normalized = re.sub(r"[ \-_.\u2010-\u2015\u2212\u30fc\uff0d\u3000]", "", name.casefold())
        if len(normalized) < 3:
            return None
        catalog = self._plugin_names()
        if not catalog or not plugin_name_candidates(name, catalog):
            return None
        return self._process_plugin_request(
            PluginRequest(Action.INSERT_PLUGIN, name, "selected", None),
            text,
            message_id,
            started,
            jev_ms,
            initial_result=initial_result,
        )

    def _plugin_candidate_question(self, message_id: Any, candidates: tuple[str, ...]) -> dict[str, Any]:
        shown = list(candidates[:5])
        remaining = len(candidates) - len(shown)
        key = "ask.plugin_more" if remaining else "ask.plugin"
        return {"id": message_id, "kind": "ask", "line": self._m(key, count=remaining), "options": shown}

    def _local_bare_plugin_request(self, text: str, message_id: Any, started: float) -> dict[str, Any] | None:
        from intent import detect_language, load_aliases, normalize_phrase
        if detect_language(text) == "en":
            from intent_en import normalize_english_phrase
            name = re.sub(r"^(?:the|a|an|some)\s+", "", normalize_english_phrase(text)).strip()
        else:
            name = re.sub(r"(?:を|が|も)$", "", normalize_phrase(text)).strip()
            if re.search(r"(?:を|に|へ|で|と|から|まで|が|も|は|して|してる|する|した|してね|してください)$", normalize_phrase(text)):
                return None
        track_names = []
        aliases = load_aliases()
        for track in self.snapshot.tracks:
            track_names.extend((track.name, *aliases.get(track.name, ())))
        if any(value and re.sub(r"[\W_]", "", value.casefold()).startswith(re.sub(r"[\W_]", "", name.casefold())) for value in track_names):
            return None
        request_name = resolve_device_request_name(name)
        if request_name is None:
            return None
        normalized = re.sub(r"[ \-_.\u2010-\u2015\u2212\u30fc\uff0d\u3000]", "", request_name.casefold())
        if len(normalized) < 3:
            return None
        return self._bare_plugin_request(text, message_id, started, 0)

    def _plugin_fallback(
        self,
        text: str,
        message_id: Any,
        started: float,
        jev_ms: int,
        *,
        initial_result: IntentResult | None = None,
    ) -> dict[str, Any] | None:
        """Fallback before Jev asks about a suspected built-in-device request.
        Ask Jev to choose one catalog plug-in from the full utterance, then continue a match such as Omnisphere as an external plug-in request."""
        catalog = self._plugin_names()
        if not catalog or not self.key:
            return None
        try:
            plugin, picked_ms = self._pick_plugin_with_jev(text, catalog)
        except JevRequestTooLarge:
            return {"id": message_id, "kind": "error", "line": self._m("error.jev_too_large")}
        if plugin is None:
            return None
        wants_new_track = re.search(r"新しい|新規|あたらしい|トラック\s*(?:を)?\s*(?:作|追加|足|増や)|\b(?:new|another|fresh)\s+(?:midi\s+|audio\s+|instrument\s+)?track\b|\b(?:create|make|add)\s+(?:a\s+)?(?:midi\s+|audio\s+|instrument\s+)?track\b", text, re.IGNORECASE) is not None
        audio = "audio" if re.search(r"オーディオ|audio", text, re.IGNORECASE) else None
        if wants_new_track:
            request = PluginRequest(Action.ADD_TRACK_WITH_PLUGIN, plugin, None, audio)
        else:
            request = PluginRequest(Action.INSERT_PLUGIN, plugin, "selected", None)
        return self._process_plugin_request(
            request,
            text,
            message_id,
            started,
            jev_ms + picked_ms,
            initial_result=initial_result,
        )

    def _pick_plugin_with_jev(self, raw_name: str, catalog: tuple[str, ...]) -> tuple[str | None, int]:
        """Use Jev to match katakana or abbreviations to catalog names in batches of 250, returning the most likely match."""
        started = time.perf_counter()
        best: tuple[float, str] | None = None
        for offset in range(0, len(catalog), 240):
            chunk = catalog[offset:offset + 240]
            criteria = {name: name for name in chunk}
            criteria["none"] = "この中には無い / None of these"
            payload = {
                "state": {"utterance": raw_name},
                "model": "jev-latest",
                "questions": {"plugin": {"type": "choice", "instructions": "この言葉（カタカナ・略称・表記ゆれを含む）が指しているプラグイン名を1つ選ぶ。無ければ none / Choose the plug-in name meant by this spelling or alias, or none", "criteria": criteria}},
            }
            try:
                response = self.requester(payload, self.key)
            except JevRequestTooLarge:
                raise
            except RuntimeError:
                continue
            answer = response.get("answers", {}).get("plugin", {}) if isinstance(response, Mapping) else {}
            name = str(answer.get("choice", "none"))
            confidence = float(answer.get("confidence", 0.0) or 0.0)
            if name != "none" and name in chunk and (best is None or confidence > best[0]):
                best = (confidence, name)
        elapsed = round((time.perf_counter() - started) * 1000)
        if best is not None and best[0] >= 0.5:
            return best[1], elapsed
        return None, elapsed

    def _plugin_notice(self, message_id: Any, text: str) -> dict[str, Any] | None:
        """Return guidance without acting on insertion requests for external plug-ins or generic terms such as "reverb."""
        if not re.search(r"入り|付き|つき|載せ|のせ|挿し|さして|インサート|追加|\b(?:insert|add|load|open|put|drop|throw|place|bring up|fire up|launch|pull up|use|apply|stick|slap)\b", text, re.IGNORECASE):
            return None
        lowered = text.casefold()
        generic_names = {word.casefold() for word in GENERIC_DEVICE_WORDS}
        if any(name.casefold() not in generic_names and name.casefold() in lowered for name in NATIVE_DEVICES):
            return None
        catalog = load_plugin_catalog()
        exact = [name for name in catalog if len(name) >= 3 and name.casefold() in lowered]
        if exact:
            longest = max(exact, key=len)
            if self._plugin_script_ok:
                return {"id": message_id, "kind": "info", "line": self._m("plugin.phrase_hint", name=longest)}
            return {"id": message_id, "kind": "info", "line": self._m("plugin.enable_script", name=longest)}
        for word, keys in GENERIC_DEVICE_WORDS.items():
            matched = word.casefold() in lowered if contains_japanese(word) else re.search(rf"(?<![A-Za-z0-9_]){re.escape(word.casefold())}(?![A-Za-z0-9_])", lowered)
            if matched:
                matches = [name for name in catalog if any(key in name.casefold() for key in keys)][:6]
                hint = ("、" if self.lang == "ja" else ", ").join(matches) if matches else self._m("plugin.none")
                shown_word = word if self.lang == "ja" or not contains_japanese(word) else "That term"
                return {"id": message_id, "kind": "info", "line": self._m("plugin.generic", word=shown_word, hint=hint)}
        return None

    def _is_undo_request(self, text: str) -> bool:
        """Every phrasing of undo must reach the receipt-based restore. "undo that" used to miss the exact-match list and fall
        through to Live's own undo, which does not record mute/solo/arm and therefore reverted some older, unrelated edit."""
        if re.fullmatch(r"\s*(?:戻して|元に戻して|元に戻す|取り消し(?:て)?|アンドゥ|undo(?:\s+that)?|take\s+that\s+back|revert)\s*", text, re.IGNORECASE):
            return True
        if self.snapshot is None:
            return False
        parsed = parse_local(text, self.snapshot)
        return parsed is not None and parsed.action is Action.UNDO

    def _is_redo_request(self, text: str) -> bool:
        if self.snapshot is None:
            return re.fullmatch(r"\s*(?:やり直し(?:て)?|リドゥ|redo(?:\s+that)?)\s*", text, re.IGNORECASE) is not None
        parsed = parse_local(text, self.snapshot)
        return parsed is not None and parsed.action is Action.REDO

    def _refuse_native_history(self, message_id: Any, started: float | None = None) -> dict[str, Any]:
        started = started or time.perf_counter()
        return {"id": message_id, "kind": "info", "line": self._m("info.cannot_undo"), "ms": self._ms(started, 0, 0, 0)}

    def _refuse_redo(self, message_id: Any, started: float | None = None) -> dict[str, Any]:
        started = started or time.perf_counter()
        return {"id": message_id, "kind": "info", "line": self._m("info.cannot_redo"), "ms": self._ms(started, 0, 0, 0)}

    def _dispatch_only_action(
        self,
        action: Action,
        message_id: Any,
        started: float | None = None,
        *,
        dispatch: bool = True,
    ) -> dict[str, Any] | None:
        if action not in DISPATCH_ONLY_ACTIONS:
            return None
        if dispatch and action is Action.UNDO and not self._ensure_script_current():
            return self._outdated_response(message_id, started or time.perf_counter())
        if not dispatch:
            return {"id": message_id, "kind": "dispatch_only"}
        self.pending = None
        if action is Action.UNDO:
            return self._dispatch_undo(message_id, started)
        if action is Action.REDO:
            return self._refuse_redo(message_id, started)
        raise RuntimeError(f"unhandled dispatch-only action {action.value!r}")

    def _dispatch_undo(self, message_id: Any, started: float | None = None) -> dict[str, Any]:
        started = started or time.perf_counter()
        if self._history_kind == "nonreceipt":
            return {"id": message_id, "kind": "info", "line": self._m("info.cannot_undo"), "ms": self._ms(started, 0, 0, 0)}
        if isinstance(self.previous, PreviousChain):
            return self._undo_previous_chain(message_id, started)
        if isinstance(self.previous, PreviousIntent):
            prior = self.previous
            unresolved_values: list[Any] = []
            unresolved = self._restore_receipt(prior, unresolved_values)
            self.previous = unresolved or prior
            self._history_kind = "receipt" if unresolved is not None else "nonreceipt"
            if unresolved is not None:
                value = unresolved_values[-1] if unresolved_values else "?"
                return {"id": message_id, "kind": "info", "line": self._m("info.changed_manually", value=value), "ms": self._ms(started, 0, 0, 0)}
            track = self._track_label(self.snapshot, prior)
            line = self._m("readback.text.undo_detail", track=track, action=action_label(prior.action.value, lang=self.lang)) if track else self._m("readback.text.undo")
            return {"id": message_id, "kind": "result", "line": line, "decision": {"track": track}, "ms": self._ms(started, 0, 0, 0)}
        return self._undo_button(message_id)

    def _undo_button(self, message_id: Any) -> dict[str, Any]:
        """Handle Undo when no receipt is available without touching Live's shared undo history."""
        started = time.perf_counter()
        self._undo_target_tracks = None
        self.previous = None
        self._history_kind = "nonreceipt"
        return {"id": message_id, "kind": "info", "line": self._m("info.cannot_undo"), "ms": self._ms(started, 0, 0, 0)}

    def _undo_previous_chain(self, message_id: Any, started: float) -> dict[str, Any]:
        previous = self.previous
        if not isinstance(previous, PreviousChain):
            return {"id": message_id, "kind": "info", "line": self._m("info.no_undo")}
        failures = self._rollback_transaction(Transaction(list(zip(previous.clauses, previous.items))))
        self.previous = PreviousChain(tuple(item for _clause, item in reversed(failures)), tuple(clause for clause, _item in reversed(failures))) if failures else None
        self._history_kind = "receipt" if failures else "nonreceipt"
        if failures:
            return {"id": message_id, "kind": "error", "line": self._m("error.chain_partial", clause=", ".join(clause for clause, _item in failures)), "ms": self._ms(started, 0, 0, 0)}
        return {"id": message_id, "kind": "result", "line": self._m("readback.text.undo"), "ms": self._ms(started, 0, 0, 0)}

    def _answer_confirm(self, message_id: Any, confirmed: bool) -> dict[str, Any]:
        if confirmed and not self._ensure_script_current():
            return self._outdated_response(message_id, time.perf_counter())
        pending = self.pending_confirm
        self.pending_confirm = None
        self.pending_confirm_created = None
        if pending is None:
            return {"id": message_id, "kind": "info", "line": self._m("info.no_confirmation")}
        if len(pending) == 8:
            intent, jev_ms, llm_ms, utterance, rewritten, forced, trusted_fields, _creator_id = pending
        elif len(pending) == 7:
            intent, jev_ms, llm_ms, utterance, rewritten, forced, _creator_id = pending
            trusted_fields = frozenset()
        else:
            intent, jev_ms, llm_ms, utterance, rewritten, _creator_id = pending
            forced = False
            trusted_fields = frozenset()
        if not confirmed:
            answer = {"id": message_id, "kind": "info", "line": self._m("info.cancelled")}
            if forced:
                answer["via"] = "gemini"
            return answer
        dispatched = self._dispatch_only_action(intent.action, message_id)
        if dispatched is not None:
            return dispatched
        if forced:
            if ACTIONS[intent.action].kind in {"plugin", "plugin_track"}:
                catalog = tuple(sorted(set(self._plugin_names()) | set(NATIVE_DEVICES)))
                if resolve_exact_plugin_name(intent.plugin or "", catalog) != intent.plugin:
                    return {"id": message_id, "kind": "error", "line": self._m("llm.response"), "via": "gemini"}
            checked = IntentResult(intent, (), (), ())
            decision = self._decision(checked, message_id, trusted_fields=trusted_fields)
            if decision is not None:
                decision["via"] = "gemini"
                return decision
            denied = self._authorize_target(intent)
            if denied is not None:
                return {"id": message_id, **denied, "via": "gemini"}
        result, receipt = self._execute_now(intent, message_id, jev_ms, llm_ms, time.perf_counter(), utterance, rewritten)
        if forced:
            result["via"] = "gemini"
        return self._record_execution_history(result, receipt)

    def _confirm_multi_track_names(self, indices: tuple[int, ...]) -> int:
        assert self.snapshot is not None
        arguments: list[str] = []
        expected: list[tuple[str, str, str]] = []
        for index in indices:
            track = next(item for item in self.snapshot.tracks if item.index == index)
            identity = request_id("name")
            arguments.extend(["--api-get", track.path, "name", identity])
            expected.append((identity, track.path, track.name))
        result = self.bridge.run(arguments)
        found = ack_map(result)
        if any(identity not in found or str(found[identity].payload) != name for identity, _path, name in expected):
            raise StaleSnapshot()
        return result.elapsed_ms

    def _read_multi_before(self, indices: tuple[int, ...], prop: str) -> tuple[dict[int, tuple[str, bool]], int]:
        assert self.snapshot is not None
        arguments: list[str] = []
        requests: list[tuple[int, str, str]] = []
        for index in indices:
            track = next(item for item in self.snapshot.tracks if item.index == index)
            name_id, value_id = request_id("name"), request_id(prop)
            arguments.extend(["--api-get", track.path, "name", name_id, "--api-get", track.path, prop, value_id])
            requests.append((index, name_id, value_id))
        result = self.bridge.run(arguments)
        found = ack_map(result)
        values: dict[int, tuple[str, bool]] = {}
        for index, name_id, value_id in requests:
            track = next(item for item in self.snapshot.tracks if item.index == index)
            if name_id not in found or value_id not in found or str(found[name_id].payload) != track.name:
                raise StaleSnapshot()
            values[index] = (track.name, bool(found[value_id].payload))
        return values, result.elapsed_ms

    @staticmethod
    def _multi_property(action: Action) -> tuple[str, bool]:
        if action in {Action.MUTE, Action.UNMUTE}:
            return "mute", action is Action.MUTE
        if action in {Action.SOLO, Action.UNSOLO}:
            return "solo", action is Action.SOLO
        return "arm", action is Action.ARM

    def _multi_label(self, intent: Intent) -> str:
        assert self.snapshot is not None
        count = len(intent.tracks)
        if intent.target_origin is TargetOrigin.RANGE and intent.tracks:
            return f"{intent.tracks[0] + 1}–{intent.tracks[-1] + 1} ({count} tracks)"
        if intent.target_origin is TargetOrigin.ALL:
            return f"all ({count} tracks)"
        if intent.target_origin is TargetOrigin.ONLY:
            track = next((item for item in self.snapshot.tracks if item.index == intent.tracks[0]), None)
            return f"only {track.name}" if track else f"only ({count} tracks)"
        excluded = [item for item in self.snapshot.tracks if not is_bridge_track(item) and item.index not in intent.tracks]
        return f"all except {excluded[0].name}" if len(excluded) == 1 else f"all except ({count} tracks)"

    def _write_multi_values(self, values: tuple[tuple[int, str, bool], ...]) -> int:
        assert self.snapshot is not None
        if not values:
            return 0
        arguments = ["--write"]
        for index, prop, value in values:
            track = next(item for item in self.snapshot.tracks if item.index == index)
            arguments.extend(["--api-set", track.path, prop, "1" if value else "0", request_id("set")])
        result = self.bridge.run(arguments)
        if result.timed_out:
            raise WriteResultUnknown(self._m("error.live"))
        return result.elapsed_ms

    def _execute_multi(
        self,
        intent: Intent,
        message_id: Any,
        jev_ms: int,
        llm_ms: int,
        started: float,
        utterance: str,
    ) -> tuple[dict[str, Any], Receipt | None]:
        assert self.snapshot is not None
        dispatched = self._dispatch_only_action(intent.action, message_id, started)
        if dispatched is not None:
            return dispatched, None
        bridge_ms = 0
        prop, requested = self._multi_property(intent.action)
        restoring = False
        eligible = tuple(track.index for track in self.snapshot.tracks if not is_bridge_track(track))
        indices = eligible if intent.target_origin is TargetOrigin.ONLY else tuple(dict.fromkeys(intent.tracks))
        live_values, read_ms = self._read_multi_before(indices, prop)
        bridge_ms += read_ms
        desired_list: list[tuple[int, str, bool]] = [(index, prop, requested) for index in intent.tracks]
        if intent.target_origin is TargetOrigin.ONLY:
            target = intent.tracks[0]
            desired_list = [(target, prop, True)] + [
                (index, prop, False) for index, (_name, active) in live_values.items()
                if index != target and active
            ]
        desired = tuple(desired_list)
        changes: list[tuple[int, str, bool]] = []
        remembered: list[tuple[int, str, bool]] = []
        for index, property_name, value in desired:
            track = next(item for item in self.snapshot.tracks if item.index == index)
            name, old = live_values[index]
            self.snapshot = replace_track(self.snapshot, index, **{property_name: old})
            if old != value:
                changes.append((index, property_name, value))
                remembered.append((index, name, old))
        if not changes:
            return {"id": message_id, "kind": "info", "line": self._m("info.already_state"), "decision": {"track": self._multi_label(intent)}, "ms": self._ms(started, jev_ms, llm_ms, bridge_ms)}, None
        entries = tuple(ReceiptEntry(next(item.path for item in self.snapshot.tracks if item.index == index), property_name, name, old, value) for (index, name, old), (_i, property_name, value) in zip(remembered, changes))
        receipt = Receipt(intent.action, None, None, False, True, Step.NONE, True, target_origin=intent.target_origin, tracks=intent.tracks, multi_before=tuple(remembered), utterance=utterance, entries=entries)
        transaction = Transaction()
        transaction.register(utterance, receipt)
        try:
            bridge_ms += self._write_multi_values(tuple(changes))
        except Exception:
            failures = self._rollback_transaction(transaction)
            unresolved = failures[0][1] if failures else None
            line = self._m("error.chain_rolled_back") if unresolved is None else self._m("error.chain_partial", clause=utterance)
            return {"id": message_id, "kind": "error" if unresolved is None else "unknown", "line": line, "ms": self._ms(started, jev_ms, llm_ms, bridge_ms)}, unresolved
        try:
            readback, read_ms = self._read_multi_before(tuple(index for index, _prop, _value in changes), prop)
            bridge_ms += read_ms
            mismatch = any(readback[index][1] != value for index, _property_name, value in changes)
        except Exception:
            mismatch = True
        if mismatch:
            unresolved = self._restore_receipt(receipt)
            line = self._m("error.chain_rolled_back") if unresolved is None else self._m("error.chain_partial", clause=utterance)
            return {"id": message_id, "kind": "error" if unresolved is None else "unknown", "line": line, "ms": self._ms(started, jev_ms, llm_ms, bridge_ms)}, unresolved
        for index, property_name, value in changes:
            self.snapshot = replace_track(self.snapshot, index, **{property_name: value})
        key = f"result.multi.{intent.action.value}"
        return {
            "id": message_id,
            "kind": "result",
            "line": self._m(key, count=len(changes)) if not restoring else self._m("readback.text.undo"),
            "decision": {"utterance": utterance, "action": intent.action.value, "track": self._multi_label(intent)},
            "ms": self._ms(started, jev_ms, llm_ms, bridge_ms),
        }, None if restoring else receipt

    def _execute_now(
        self,
        intent: Intent,
        message_id: Any,
        jev_ms: int,
        llm_ms: int,
        started: float,
        utterance: str,
        rewritten: list[str] | None,
    ) -> tuple[dict[str, Any], Receipt | None]:
        assert self.snapshot is not None
        dispatched = self._dispatch_only_action(intent.action, message_id, started)
        if dispatched is not None:
            return dispatched, None
        denied = self._authorize_target(intent)
        if denied is not None:
            return {"id": message_id, **denied, "ms": self._ms(started, jev_ms, llm_ms, 0)}, None
        if intent.target_origin in MULTI_TARGET_ORIGINS:
            return self._execute_multi(intent, message_id, jev_ms, llm_ms, started, utterance)
        if intent.action is Action.VOLUME and intent.number and intent.number.unit == "db":
            # Fix the direction once so the remembered operation matches what was done; otherwise "もう少し" after "3dB下げて" finds no direction to repeat.
            intent = replace(intent, step=step_from_words(intent.step, utterance))
        before = self.snapshot
        bridge_ms = 0
        write_unknown = False
        confirmed = False
        receipt: Receipt | None = None
        transaction = Transaction()
        try:
            receipt_kinds = {"track_bool", "track_int", "mixer", "send", "param", "clip_prop", "song_bool", "jump", "tempo", "rename"}
            if ACTIONS[intent.action].kind != "mixer":
                bridge_ms += self._confirm_track_name(intent)
            if ACTIONS[intent.action].kind in receipt_kinds:
                try:
                    self.snapshot, read_ms = self._refresh_target(intent)
                except IndexError:
                    if not (intent.action is Action.VOLUME and intent.number and intent.number.unit == "db"):
                        raise
                    read_ms = 0
                except (AssertionError, ValueError):
                    raise
                except (BridgeError, WriteResultUnknown) as error:
                    raise ValueError(self._m("error.current_value")) from error
                bridge_ms += read_ms
                before = self.snapshot
                intent = self._rebind_refreshed_intent(before, intent)
            if ACTIONS[intent.action].kind == "mixer":
                bridge_ms += self._confirm_track_name(intent)
            self._undo_target_tracks = None
            if ACTIONS[intent.action].kind in {"plugin", "plugin_track"}:
                plugin_result = self._run_plugin_flow(intent, before)
                if isinstance(plugin_result, tuple):
                    plugin_ms, intent = plugin_result
                else:
                    plugin_ms = plugin_result
                bridge_ms += plugin_ms
                confirmed = True
                if ACTIONS[intent.action].kind == "plugin_track":
                    self._undo_target_tracks = (
                        (len(before.returns), len(self.snapshot.returns))
                        if intent.track_kind == "return"
                        else (len(before.tracks), len(self.snapshot.tracks))
                    )
            elif ACTIONS[intent.action].kind in {"structure", "structure_device"} and (
                intent.action is Action.ADD_RETURN_TRACK or intent.track_kind == "return" or self._script_available()
            ):
                if (intent.action is Action.ADD_RETURN_TRACK or intent.track_kind == "return") and not self._script_available():
                    raise ValueError(self._m("error.live"))
                bridge_ms += self._run_add_track_via_script(intent)
                confirmed = True
                self._undo_target_tracks = (
                    (len(before.returns), len(self.snapshot.returns))
                    if intent.action is Action.ADD_RETURN_TRACK or intent.track_kind == "return"
                    else (len(before.tracks), len(self.snapshot.tracks))
                )
            elif intent.action is Action.VOLUME and intent.number and intent.number.unit == "db":
                self._current_transaction = transaction
                db_result = self._set_volume_db(replace(intent, step=step_from_words(intent.step, utterance)))
                if len(db_result) == 3:
                    self.snapshot, write_ms, write_unknown = db_result
                    old = self._value_before(before, intent)
                    after_value = self._value_before(self.snapshot, intent)
                    receipt_target = target_for_intent(before, intent)
                    if receipt_target is None:
                        raise StaleSnapshot()
                    path = f"{receipt_target.path} mixer_device volume"
                    owner = "master" if receipt_target.path == "live_set master_track" else receipt_target.name
                    receipt = Receipt(intent.action, intent.track, intent.param, old, after_value, intent.step, not write_unknown, target_origin=intent.target_origin, utterance=utterance, entries=(ReceiptEntry(path, "value", owner, old, after_value, True),))
                else:
                    self.snapshot, write_ms, write_unknown, receipt = db_result
                bridge_ms += write_ms
                confirmed = not write_unknown
            else:
                batches = ACTIONS[intent.action].apply(before, intent)
                if not batches:
                    raise LocalizedError("error.no_action")
                old = self._value_before(before, intent)
                expected_values = [self._expected_batch_value(batch) for batch in batches]
                expected_after = next((value for value in expected_values if value is not None), None)
                if intent.action is Action.RENAME:
                    expected_after = str(intent.text or "")
                    target = target_for_intent(before, intent)
                    if target is None:
                        raise StaleSnapshot()
                    receipt = Receipt(intent.action, intent.track, intent.param, old, expected_after, intent.step, False, target_origin=intent.target_origin, utterance=utterance, entries=(ReceiptEntry(target.path, "name", expected_after, old, expected_after, write_kind="rename"),))
                elif expected_after is not None and ACTIONS[intent.action].kind in receipt_kinds:
                    change_batch = next(batch for batch in batches if self._expected_batch_value(batch) is not None)
                    if "--tempo" in change_batch:
                        path, prop, parameter, write_kind = "live_set", "tempo", False, "tempo"
                    else:
                        marker = "--api-parameter-set" if "--api-parameter-set" in change_batch else "--api-set"
                        at = change_batch.index(marker)
                        path = change_batch[at + 1]
                        prop = "value" if marker == "--api-parameter-set" else change_batch[at + 2]
                        parameter, write_kind = marker == "--api-parameter-set", "set"
                    receipt_target = target_for_intent(before, intent)
                    owner = receipt_target.name if intent.param is not None and receipt_target is not None else "master" if receipt_target is not None and receipt_target.path == "live_set master_track" else (self._track_label(before, intent) or "song")
                    device_owner = None
                    if intent.param is not None:
                        device_owner = next((device.name for track in addressable_targets(before) for device in track.devices if intent.param.path.startswith(device.path + " ")), None)
                    receipt = Receipt(intent.action, intent.track, intent.param, old, expected_after, intent.step, False, clip=intent.clip, scene=intent.scene, send=intent.send, device=intent.device, target_origin=intent.target_origin, utterance=utterance, entries=(ReceiptEntry(path, prop, owner, old, expected_after, parameter, device_owner, write_kind),))
                if ACTIONS[intent.action].kind in {"mixer", "send", "param", "tempo"} and receipt is not None and expected_after is not None and self._same_restored_value(old, expected_after):
                    return {"id": message_id, "kind": "info", "line": self._m("info.already_state"), "ms": self._ms(started, jev_ms, llm_ms, bridge_ms)}, None
                transaction.register(utterance, receipt)
                all_acks: list[Ack] = []
                for batch in batches:
                    batch_expected = self._expected_batch_value(batch)
                    if batch_expected is not None:
                        expected_after = batch_expected
                    if ACTIONS[intent.action].kind in RETRY_READ_KINDS and "--api-get" in batch:
                        result = self._read_until(batch, expected_after)
                    else:
                        result = self.bridge.run(batch)
                    bridge_ms += result.elapsed_ms
                    all_acks.extend(result.acks)
                    if result.timed_out:
                        write_unknown = _is_change_batch(batch)
                        self.snapshot, read_ms = self._refresh_target(intent)
                        bridge_ms += read_ms
                        if write_unknown:
                            raise WriteResultUnknown(self._m("error.live"))
                        break
                else:
                    combined = BridgeResult(tuple(all_acks), bridge_ms, 0, False)
                    if ACTIONS[intent.action].kind in {"structure", "structure_device"}:
                        self.snapshot, read_ms = self.reader.read()
                        bridge_ms += read_ms
                        expected_after = float(len(self.snapshot.tracks))
                        if ACTIONS[intent.action].kind == "structure_device":
                            if len(self.snapshot.tracks) <= len(before.tracks):
                                raise ValueError("トラックが増えていません")
                            new_track = self.snapshot.tracks[-1]
                            insert = self.bridge.run(["--write", "--api-insert-device", new_track.path, str(intent.native_device), "", request_id("insert")])
                            bridge_ms += insert.elapsed_ms
                            if insert.timed_out:
                                write_unknown = True
                            self.snapshot, read_ms = self.reader.read()
                            bridge_ms += read_ms
                    else:
                        self.snapshot = self._update_from_result(before, intent, combined)
                    confirmed = (
                        expected_after is not None
                        and self._has_readback(intent, combined)
                        and self._same_value(self._value_before(self.snapshot, intent), expected_after)
                    )
                    if receipt is not None and expected_after is not None and not confirmed:
                        raise ValueError(self._m("error.live"))
            line = self._short_line(ACTIONS[intent.action].readback(self.snapshot, intent))
            if not confirmed and not write_unknown and ACTIONS[intent.action].kind in {"clip_prop", "song_bool", "track_bool", "track_int"}:
                line += self._m("info.unchanged")
            if intent.action is Action.VOLUME:
                old_target = target_for_intent(before, intent)
                old_display = old_target.volume_display if old_target is not None else before.master_display
                # After an undo request, the snapshot may contain a raw value such as 0.805391. Omit it because it is not useful to users.
                if "dB" in str(old_display) or "inf" in str(old_display):
                    line += self._m("info.from_value", value=old_display)
            if write_unknown:
                line += self._m("info.readback_recovered")
        except ValueError as error:
            receipt = receipt or (transaction.pairs[-1][1] if transaction.pairs else None)
            unresolved = self._restore_receipt(receipt) if receipt is not None else None
            restored = unresolved is None
            line = self._error_text(error) if receipt is None else self._m("error.chain_rolled_back" if restored else "error.chain_partial", **({} if restored else {"clause": utterance}))
            return self._execution_failure(message_id, "error" if restored else "unknown", line, intent, before, utterance, rewritten, jev_ms, llm_ms, bridge_ms, started), unresolved
        except WriteResultUnknown as error:
            receipt = receipt or (transaction.pairs[-1][1] if transaction.pairs else None)
            unresolved = self._restore_receipt(receipt) if receipt is not None else None
            restored = receipt is not None and unresolved is None
            line = self._m("error.chain_rolled_back" if restored else "error.chain_partial", **({} if restored else {"clause": utterance}))
            return self._execution_failure(message_id, "error" if restored else "unknown", line, intent, before, utterance, rewritten, jev_ms, llm_ms, bridge_ms, started), unresolved
        except BridgeError:
            receipt = receipt or (transaction.pairs[-1][1] if transaction.pairs else None)
            self.live = False
            unresolved = self._restore_receipt(receipt) if receipt is not None else None
            restored = unresolved is None
            line = self._m("error.live") if receipt is None else self._m("error.chain_rolled_back" if restored else "error.chain_partial", **({} if restored else {"clause": utterance}))
            return self._execution_failure(message_id, "error" if restored else "unknown", line, intent, before, utterance, rewritten, jev_ms, llm_ms, bridge_ms, started), unresolved
        except Exception:
            if not transaction.pairs:
                raise
            failures = self._rollback_transaction(transaction)
            line = self._m("error.chain_partial", clause=", ".join(clause for clause, _receipt in failures)) if failures else self._m("error.chain_rolled_back")
            unresolved = failures[0][1] if failures else None
            return self._execution_failure(message_id, "unknown" if failures else "error", line, intent, before, utterance, rewritten, jev_ms, llm_ms, bridge_ms, started), unresolved
        self.live = True
        if receipt is not None:
            receipt = replace(receipt, confirmed=confirmed)
        return {
            "id": message_id,
            "kind": "result",
            "line": line,
            "decision": self._decision_details(intent, before, self.snapshot, utterance, rewritten),
            "ms": self._ms(started, jev_ms, llm_ms, bridge_ms),
        }, receipt

    def _rebind_refreshed_intent(self, snapshot: Snapshot, intent: Intent) -> Intent:
        if intent.action is not Action.PARAM or intent.param is None:
            return intent
        refreshed_param = next((param for track in addressable_targets(snapshot) for device in track.devices for param in device.params if param.path == intent.param.path and param.name == intent.param.name), None)
        if refreshed_param is None:
            raise ValueError(self._m("error.current_value"))
        return replace(intent, param=refreshed_param)

    def _snapshot_is_stale(self) -> bool:
        """Check snapshot freshness with one device-list command, which takes about 100 ms; skip it within 10 seconds of the last snapshot or validation.
        This detects a different song, manually added, removed, or renamed tracks, and manually added devices."""
        assert self.snapshot is not None
        if time.time() - self.snapshot.taken_at < SNAPSHOT_TRUST_SECONDS:
            return False
        request = request_id("devices")
        try:
            result = self.bridge.run(["--api-device-list", "all", request])
        except BridgeError:
            return False
        ack = ack_map(result).get(request)
        payload = ack.payload if ack is not None else None
        tracks = payload.get("tracks") if isinstance(payload, Mapping) else None
        if not isinstance(tracks, list):
            return False
        live = [
            (
                str((item.get("track") or {}).get("name", "")),
                tuple(str(device.get("name", "")) for device in (item.get("devices") or []) if isinstance(device, Mapping)),
            )
            for item in tracks if isinstance(item, Mapping)
        ]
        mine = [(track.name, tuple(device.name for device in track.devices)) for track in self.snapshot.tracks]
        if live == mine:
            self.snapshot = replace(self.snapshot, taken_at=time.time())
            return False
        return True

    def _authorize_target(self, intent: Intent) -> dict[str, Any] | None:
        assert self.snapshot is not None
        if intent.target_origin in MULTI_TARGET_ORIGINS or intent.tracks:
            valid = (
                intent.track is None
                and bool(intent.tracks)
                and intent.action in MULTI_TOGGLE_ACTIONS
                and intent.target_origin in MULTI_TARGET_ORIGINS
                and len(set(intent.tracks)) == len(intent.tracks)
                and (intent.target_origin is not TargetOrigin.ONLY or intent.action in {Action.SOLO, Action.ARM})
            )
            eligible = set(eligible_track_indices(self.snapshot))
            if not valid or any(index not in eligible for index in intent.tracks):
                return {"kind": "error", "line": self._m("error.named_track_missing")}
            return None
        spec = ACTIONS[intent.action]
        needs_target = spec.needs_track or intent.device is not None or intent.param is not None or intent.clip is not None
        if not needs_target:
            return None
        if intent.track in {None, "selected"} or intent.target_origin is TargetOrigin.NONE:
            return {"kind": "error", "line": self._m("error.named_track_missing")}
        if intent.named_evidence >= TRACK_UNSTATED_MAX:
            allowed = {TargetOrigin.NAMED, TargetOrigin.CLARIFIED, TargetOrigin.MASTER}
        else:
            allowed = {TargetOrigin.SELECTED, TargetOrigin.OWNER, TargetOrigin.PREVIOUS, TargetOrigin.CLARIFIED}
        if intent.target_origin not in allowed:
            return {"kind": "error", "line": self._m("error.named_track_missing")}
        is_master = intent.track == "master" or (isinstance(intent.track, TargetRef) and intent.track.kind is TargetKind.MASTER)
        if is_master:
            literal = re.search(r"マスター|全体|\bmaster\b|\bwhole\s+mix\b|\bthe\s+mix\b|\bmain(?:\s+out)?\b|\beverything\b", intent.utterance, re.IGNORECASE)
            selected_master = intent.target_origin is TargetOrigin.SELECTED and self._selected_track_index() == TargetRef(TargetKind.MASTER)
            if (literal is None and intent.target_origin not in {TargetOrigin.PREVIOUS, TargetOrigin.OWNER} and not selected_master) or intent.target_origin not in {TargetOrigin.MASTER, TargetOrigin.CLARIFIED, TargetOrigin.PREVIOUS, TargetOrigin.SELECTED, TargetOrigin.OWNER} or intent.target_origin is TargetOrigin.SELECTED and not selected_master:
                return {"kind": "error", "line": self._m("error.named_track_missing")}
        if not is_master and not isinstance(intent.track, (int, TargetRef)):
            return {"kind": "error", "line": self._m("error.named_track_missing")}
        track = target_for_intent(self.snapshot, intent)
        if track is None:
            return {"kind": "error", "line": self._m("error.named_track_missing")}
        unsupported = target_capability_error(self.snapshot, intent)
        if unsupported is not None:
            target_name, action_name = unsupported
            return {"kind": "error", "line": self._m("error.target_capability", target=target_name, action=action_name)}
        literal_utterance = intent.utterance
        ignored_literals: tuple[str | None, ...] = ()
        if intent.action is Action.RENAME:
            ignored_literals = (intent.text,)
        elif intent.action is Action.INSERT_PLUGIN:
            ignored_literals = (intent.plugin, intent.native_device)
        for ignored in ignored_literals:
            if ignored:
                literal_utterance = re.sub(re.escape(str(ignored)), "", literal_utterance, flags=re.IGNORECASE)
        literal_tracks = [
            item for item in addressable_targets(self.snapshot)
            if item.name and re.search(rf"(?<![A-Za-z0-9_]){re.escape(item.name)}(?![A-Za-z0-9_])", literal_utterance, re.IGNORECASE)
        ]
        if intent.action is Action.SEND and intent.send is not None:
            literal_tracks = [
                item for item in literal_tracks
                if not (isinstance(item, ReturnTrack) and item.index == intent.send)
            ]
        if literal_tracks and all(item.path != track.path for item in literal_tracks):
            return {"kind": "error", "line": self._m("error.named_track_missing")}
        prefix = track.path + " "
        device = intent.device
        if device is None and intent.param is not None:
            device = next((item for item in track.devices if intent.param.path.startswith(item.path + " ")), None)
        if device is not None:
            current_device = next((item for item in track.devices if item.path == device.path), None)
            if not device.path.startswith(prefix) or current_device is None or current_device.name != device.name:
                key = "error.named_device_missing" if intent.device_name else "error.target_changed"
                return {"kind": "error", "line": self._m(key)}
            if intent.param is not None:
                current_param = next((item for item in current_device.params if item.path == intent.param.path), None)
                if not intent.param.path.startswith(current_device.path + " ") or current_param is None or current_param.name != intent.param.name:
                    return {"kind": "error", "line": self._m("error.target_changed")}
        elif intent.param is not None:
            return {"kind": "error", "line": self._m("error.target_changed")}
        if intent.clip is not None:
            if not isinstance(track, Track):
                return {"kind": "error", "line": self._m("error.target_changed")}
            current_clip = next((item for item in track.clips if item.slot == intent.clip), None)
            if current_clip is None or not current_clip.path.startswith(prefix):
                return {"kind": "error", "line": self._m("error.target_changed")}
            if intent.clip_name is not None and (current_clip.name != intent.clip_name or current_clip.path != intent.clip_path):
                return {"kind": "error", "line": self._m("error.target_changed")}
        return None

    def _confirm_track_name(self, intent: Intent) -> int:
        if not isinstance(intent.track, (int, TargetRef)):
            return 0
        assert self.snapshot is not None
        track = target_for_intent(self.snapshot, intent)
        if track is None:
            raise StaleSnapshot()
        elapsed = 0
        if not (isinstance(intent.track, TargetRef) and intent.track.kind is TargetKind.MASTER):
            name_id = request_id("name")
            result = self.bridge.run(["--api-get", track.path, "name", name_id])
            name = _find(result, name_id).payload
            if str(name) != track.name:
                raise StaleSnapshot()
            elapsed = result.elapsed_ms
        device = intent.device
        if device is None and intent.param is not None:
            device = next((item for item in track.devices if intent.param.path.startswith(item.path + " ")), None)
        if device is not None:
            device_id = request_id("device-name")
            device_result = self.bridge.run(["--api-get", device.path, "name", device_id])
            if str(_find(device_result, device_id).payload) != device.name:
                raise StaleSnapshot()
            elapsed += device_result.elapsed_ms
        return elapsed

    @staticmethod
    def _expected_batch_value(arguments: list[str]) -> float | bool | str | None:
        if "--api-parameter-set" in arguments:
            at = arguments.index("--api-parameter-set")
            return float(arguments[at + 2])
        if "--api-set" in arguments:
            at = arguments.index("--api-set")
            prop, raw = arguments[at + 2], arguments[at + 3]
            if prop in {"current_monitoring_state", "pitch_coarse"}:
                return int(raw)
            if prop in {"current_song_time", "gain"}:
                return float(raw)
            if prop == "name":
                return str(json.loads(raw))
            return bool(int(raw))
        if "--tempo" in arguments:
            return float(arguments[arguments.index("--tempo") + 1])
        if "--api-call" in arguments:
            at = arguments.index("--api-call")
            if arguments[at + 2] in {"start_playing", "continue_playing"}:
                return True
            if arguments[at + 2] == "stop_playing":
                return False
        return None

    @staticmethod
    def _has_readback(intent: Intent, result: BridgeResult) -> bool:
        if ACTIONS[intent.action].kind in {"structure", "structure_device", "plugin", "plugin_track"}:
            return True
        event, prop = ACTIONS[intent.action].readback_event
        return any(ack.event == event and (prop is None or ack.property == prop) for ack in result.acks)

    def _read_until(self, batch: list[str], expected: float | bool | None) -> BridgeResult:
        """Live may return the old value immediately after a write, so reread for up to 350 ms until the expected value appears."""
        started = time.monotonic()
        all_acks: list[Ack] = []
        elapsed_ms = 0
        last = BridgeResult((), 0, 0, False)
        at = batch.index("--api-get")
        for attempt in range(8):
            read_id = request_id("read")
            arguments = list(batch)
            arguments[at + 3] = read_id
            last = self.bridge.run(arguments)
            elapsed_ms += last.elapsed_ms
            all_acks.extend(last.acks)
            ack = ack_map(last).get(read_id)
            if ack is not None and (expected is None or _matches_expected(ack.payload, expected)):
                break
            remaining = 0.35 - (time.monotonic() - started)
            if remaining <= 0 or attempt == 7:
                break
            time.sleep(min(0.05, remaining))
        return BridgeResult(tuple(all_acks), elapsed_ms, last.returncode, last.timed_out)

    def _read_transport_until(self, intent: Intent) -> BridgeResult:
        expected = intent.action is Action.PLAY
        started = time.monotonic()
        all_acks: list[Ack] = []
        elapsed_ms = 0
        last = BridgeResult((), 0, 0, False)
        for attempt in range(8):
            playing_id = request_id("playing")
            last = self.bridge.run(["--api-get", "live_set", "is_playing", playing_id])
            elapsed_ms += last.elapsed_ms
            all_acks.extend(last.acks)
            ack = ack_map(last).get(playing_id)
            if ack is not None and bool(ack.payload) is expected:
                break
            remaining = 0.35 - (time.monotonic() - started)
            if remaining <= 0 or attempt == 7:
                break
            time.sleep(min(0.05, remaining))
        return BridgeResult(tuple(all_acks), elapsed_ms, last.returncode, last.timed_out)

    def _execution_failure(
        self,
        message_id: Any,
        kind: str,
        line: str,
        intent: Intent,
        before: Snapshot,
        utterance: str,
        rewritten: list[str] | None,
        jev_ms: int,
        llm_ms: int,
        bridge_ms: int,
        started: float,
    ) -> dict[str, Any]:
        return {
            "id": message_id,
            "kind": kind,
            "line": line,
            "decision": self._decision_details(intent, before, self.snapshot or before, utterance, rewritten),
            "ms": self._ms(started, jev_ms, llm_ms, bridge_ms),
        }

    def _shown_value(self, snapshot: Snapshot, intent: Intent) -> str | None:
        spec = ACTIONS[intent.action]
        if spec.kind == "track_bool" and spec.prop:
            track = target_for_intent(snapshot, intent)
            return self._m("state.on" if track and getattr(track, spec.prop) else "state.off")
        if spec.kind == "track_int" and isinstance(intent.track, int) and spec.prop:
            track = next((item for item in snapshot.tracks if item.index == intent.track), None)
            return MONITOR_NAMES.get(int(getattr(track, spec.prop)), None) if track else None
        if spec.kind == "song_bool" and spec.prop:
            return self._m("state.on" if snapshot.song.get(spec.prop) else "state.off")
        if spec.kind == "jump":
            bar = beats_to_bar(snapshot, float(snapshot.song.get('current_song_time', 0.0) or 0.0))
            return f"{bar}小節" if self.lang == "ja" else f"bar {bar}"
        if spec.kind in {"song_call", "track_call", "clip_call", "scene_call"}:
            return self._m("state.playing" if snapshot.playing else "state.stopped")
        if spec.kind == "send" and isinstance(intent.track, int) and intent.send is not None:
            track = next((item for item in snapshot.tracks if item.index == intent.track), None)
            value = track.sends[intent.send] if track and intent.send < len(track.sends) else 0.0
            return f"{value * 100:.0f}%"
        if spec.kind in {"rename", "structure", "structure_device", "plugin", "plugin_track"}:
            return f"{len(snapshot.tracks)}本" if self.lang == "ja" else f"{len(snapshot.tracks)} tracks"
        if spec.kind == "clip_prop" and isinstance(intent.track, int) and intent.clip is not None and spec.prop:
            track = next((item for item in snapshot.tracks if item.index == intent.track), None)
            clip = next((item for item in track.clips if item.slot == intent.clip), None) if track else None
            raw = clip.props.get(spec.prop) if clip else None
            return self._m("state.on" if raw else "state.off") if spec.prop in {"looping", "warping"} else str(raw)
        if intent.action is Action.VOLUME:
            target = target_for_intent(snapshot, intent)
            return target.volume_display if target else None
        if intent.action is Action.PAN:
            target = target_for_intent(snapshot, intent)
            return target.pan_display if target and hasattr(target, "pan_display") else None
        if intent.action in {Action.MUTE, Action.UNMUTE}:
            track = target_for_intent(snapshot, intent)
            return self._m("state.on" if track and track.mute else "state.off")
        if intent.action in {Action.SOLO, Action.UNSOLO}:
            track = target_for_intent(snapshot, intent)
            return self._m("state.on" if track and track.solo else "state.off")
        if intent.action is Action.TEMPO:
            return f"{snapshot.tempo:g} BPM"
        if intent.action in {Action.PLAY, Action.STOP}:
            return self._m("state.playing" if snapshot.playing else "state.stopped")
        if ACTIONS[intent.action].kind == "param" and intent.param is not None:
            for track in addressable_targets(snapshot):
                for device in track.devices:
                    for parameter in device.params:
                        if parameter.path == intent.param.path:
                            return parameter.display
        return None

    def _track_label(self, snapshot: Snapshot, intent: Intent) -> str | None:
        if intent.track == "master" or (isinstance(intent.track, TargetRef) and intent.track.kind is TargetKind.MASTER):
            return self._m("label.master")
        track = target_for_intent(snapshot, intent)
        return track.name if track else None

    @staticmethod
    def _param_label(snapshot: Snapshot, intent: Intent) -> str | None:
        if intent.param is None:
            return None
        for track in snapshot.tracks:
            for device in track.devices:
                if intent.param in device.params:
                    return f"{device.name} / {intent.param.name}"
        return intent.param.name

    def _decision_details(
        self,
        intent: Intent,
        before: Snapshot,
        after: Snapshot,
        utterance: str,
        rewritten: list[str] | None,
    ) -> dict[str, Any]:
        shown_rewrite = rewritten
        if self.lang == "en" and rewritten and any(contains_japanese(line) for line in rewritten):
            shown_rewrite = None
        return {
            "utterance": utterance,
            "rewritten": shown_rewrite,
            "action": intent.action.value,
            "action_label": action_label(intent.action.value, lang=self.lang),
            "track": self._track_label(before, intent),
            "param": self._param_label(before, intent),
            "step_label": step_label(intent.step.value, lang=self.lang),
            "number": intent.number.value if intent.number is not None else None,
            "conf": {
                "action": intent.action_conf,
                "track": intent.track_conf if intent.track is not None else None,
                "param": intent.param_conf if intent.param is not None else None,
            },
            "before": self._shown_value(before, intent),
            "after": self._shown_value(after, intent),
        }

    def _set_volume_db(self, intent: Intent, utterance: str = "", transaction: Transaction | None = None) -> tuple[Snapshot, int, bool, Receipt]:
        assert self.snapshot is not None and intent.number is not None
        transaction = transaction or getattr(self, "_current_transaction", None)
        target_owner = target_for_intent(self.snapshot, intent)
        if target_owner is None:
            raise StaleSnapshot()
        path = f"{target_owner.path} mixer_device volume"
        track_ref = "master" if target_owner.path == "live_set master_track" else str(target_owner.index) if isinstance(target_owner, Track) else target_owner.path
        current_display = target_owner.volume_display
        target = relative_db_target(intent.number.value, intent.step, current_display)
        if target < -100.0:
            raise ValueError(self._m("error.value_range"))
        # Search with str_for_value only. Writing every probe made the fader sweep audibly through up to twelve
        # values (overshooting the target on the way) and stopped at +-0.05 dB, so "0dBにして" landed on -0.015 dB.
        low, high = 0.0, 1.0
        elapsed = 0
        attempts: list[tuple[float, float, str]] = []
        endpoints: list[tuple[float, float, str]] = []
        def parse_display(display: str) -> float:
            match = re.fullmatch(r"\s*(-inf|[+-]?\d+(?:\.\d+)?)\s*(?:dB)?\s*", display, re.IGNORECASE)
            if match is None:
                raise BridgeError(self._m("error.live"))
            return float("-inf") if match.group(1).casefold() == "-inf" else float(match.group(1))
        for value in (low, high):
            display_id = request_id("display")
            displayed = self.bridge.run(["--write", "--api-call", path, "str_for_value", json.dumps([value]), display_id])
            elapsed += displayed.elapsed_ms
            _require_readback(displayed, "api_call")
            shown = _find(displayed, display_id).payload
            shown = shown[-1] if isinstance(shown, list) and shown else shown
            display = str(shown or "")
            measured = parse_display(display)
            endpoints.append((measured, value, display))
        minimum, maximum = endpoints[0][0], endpoints[1][0]
        if maximum < minimum:
            raise BridgeError(self._m("error.live"))
        if target < minimum or target > maximum:
            low_label = endpoints[0][2]
            high_label = endpoints[1][2]
            text = f"その値にはできません（このフェーダーは {low_label}〜{high_label}）" if self.lang == "ja" else f"Out of range (this fader goes from {low_label} to {high_label})"
            raise ValueError(text)
        attempts.extend((abs(measured - target), value, display) for measured, value, display in endpoints if math.isfinite(measured))
        # Live's fader is piecewise linear in dB, so interpolating inside the bracket lands on the target in two to four
        # probes where bisection needed up to sixteen (measured: "3dB下げて" went from 0.45 s to 0.95 s with bisection).
        low_measured, high_measured = minimum, maximum
        for _ in range(12):
            if math.isfinite(low_measured) and math.isfinite(high_measured) and high_measured > low_measured:
                midpoint = low + (target - low_measured) / (high_measured - low_measured) * (high - low)
                # Stay strictly inside the bracket, or a flat stretch of the display would stall the search.
                margin = (high - low) * 0.02
                midpoint = min(max(midpoint, low + margin), high - margin)
            else:
                midpoint = (low + high) / 2.0
            display_id = request_id("display")
            displayed = self.bridge.run([
                "--write", "--api-call", path, "str_for_value", json.dumps([midpoint]), display_id,
            ])
            elapsed += displayed.elapsed_ms
            if displayed.timed_out:
                raise BridgeError("音量を書き込めません")
            display_ack = ack_map(displayed).get(display_id)
            shown = display_ack.payload if display_ack else None
            if isinstance(shown, list) and shown:
                shown = shown[-1]
            display = str(shown or "")
            measured = parse_display(display)
            if measured < low_measured or measured > high_measured:
                raise BridgeError(self._m("error.live"))
            error = abs(measured - target)
            attempts.append((error, midpoint, display))
            if error <= 0.001:
                break
            if measured < target:
                low, low_measured = midpoint, measured
            else:
                high, high_measured = midpoint, measured
        if not attempts:
            raise BridgeError("音量を書き込めません")
        error, best_value, best_display = min(attempts, key=lambda item: item[0])
        if error > 0.05 and endpoints[0][0] != endpoints[1][0]:
            raise BridgeError(self._m("error.live"))
        before_value = target_owner.volume
        owner = "master" if target_owner.path == "live_set master_track" else target_owner.name
        receipt = Receipt(intent.action, intent.track, intent.param, before_value, best_value, intent.step, False, target_origin=intent.target_origin, utterance=utterance, entries=(ReceiptEntry(path, "value", owner, before_value, best_value, True),))
        if transaction is not None:
            transaction.register(utterance, receipt)
        final_write = self.bridge.run([
            "--write", "--api-parameter-set", path, json.dumps(best_value), request_id("set"),
        ])
        elapsed += final_write.elapsed_ms
        if final_write.timed_out:
            raise WriteResultUnknown(self._m("error.live"))
        mixer = self.bridge.run(["--api-mixer-status", track_ref, request_id("mixer")])
        elapsed += mixer.elapsed_ms
        _require_readback(mixer, "api_mixer_status")
        display_ack = Ack("api_call", request_id("display"), best_display, path, "str_for_value")
        updated = self._update_from_result(self.snapshot, intent, BridgeResult(tuple(mixer.acks) + (display_ack,), mixer.elapsed_ms, 0, False))
        if not self._same_value(self._value_before(updated, intent), best_value):
            raise ValueError(self._m("error.live"))
        updated = replace_target(updated, intent.track, volume_display=best_display)
        return updated, elapsed, False, receipt

    def _refresh_target(self, intent: Intent) -> tuple[Snapshot, int]:
        refreshers = {
            "transport": self._refresh_transport,
            "song_call": self._refresh_transport,
            "track_call": self._refresh_transport,
            "clip_call": self._refresh_transport,
            "scene_call": self._refresh_transport,
            "send": self._refresh_send,
            "rename": self._refresh_track_bool,
            "structure": self._refresh_structure,
            "structure_device": self._refresh_structure,
            "plugin": self._refresh_structure,
            "plugin_track": self._refresh_structure,
            "clip_prop": self._refresh_clip_prop,
            "tempo": self._refresh_tempo,
            "track_bool": self._refresh_track_bool,
            "track_int": self._refresh_track_bool,
            "song_bool": self._refresh_song_prop,
            "jump": self._refresh_song_prop,
            "param": self._refresh_param,
            "mixer": self._refresh_mixer,
        }
        return refreshers[ACTIONS[intent.action].kind](intent)

    def _refresh_send(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        track = next(item for item in self.snapshot.tracks if item.index == intent.track)
        path = f"{track.path} mixer_device sends {intent.send or 0}"
        result = self.bridge.run(["--api-get", path, "value", request_id("send")])
        _require_readback(result, "api_get", "value")
        ack = next(item for item in reversed(result.acks) if item.event == "api_get" and item.property == "value")
        raw = ack.payload[-1] if isinstance(ack.payload, list) and ack.payload else ack.payload
        if not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            raise ValueError(self._m("error.current_value"))
        elapsed = result.elapsed_ms + self._confirm_track_name(intent)
        return self._update_from_result(self.snapshot, intent, result), elapsed

    def _refresh_clip_prop(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        prop = ACTIONS[intent.action].prop or "looping"
        track = next(item for item in self.snapshot.tracks if item.index == intent.track)
        clip = next(item for item in track.clips if item.slot == intent.clip)
        result = self.bridge.run(["--api-get", clip.path, prop, request_id(prop)])
        _require_readback(result, "api_get", prop)
        ack = next(item for item in reversed(result.acks) if item.event == "api_get" and item.property == prop)
        raw = ack.payload[-1] if isinstance(ack.payload, list) and ack.payload else ack.payload
        valid = isinstance(raw, str) if ACTIONS[intent.action].kind == "rename" else isinstance(raw, (bool, int, float))
        if not valid or (isinstance(raw, float) and not math.isfinite(raw)):
            raise ValueError(self._m("error.current_value"))
        elapsed = result.elapsed_ms + self._confirm_track_name(intent)
        return self._update_from_result(self.snapshot, intent, result), elapsed

    def _update_clip_prop(self, snapshot: Snapshot, intent: Intent, result: BridgeResult) -> Snapshot:
        prop = ACTIONS[intent.action].prop or ""
        ack = next((item for item in reversed(result.acks) if item.event == "api_get" and item.property == prop), None)
        if ack is None or not isinstance(intent.track, int) or intent.clip is None:
            return snapshot
        raw = ack.payload[-1] if isinstance(ack.payload, list) and ack.payload else ack.payload
        if prop in {"looping", "warping"}:
            value: Any = bool(raw)
        elif prop == "pitch_coarse":
            value = int(float(raw))
        else:
            value = float(raw)
        track = next(item for item in snapshot.tracks if item.index == intent.track)
        clips = tuple(
            replace(clip, props={**clip.props, prop: value}) if clip.slot == intent.clip else clip
            for clip in track.clips
        )
        return replace_track(snapshot, intent.track, clips=clips)

    SCRIPT_LOAD_ERRORS = {
        "hotswap_active": "Liveがホットスワップ中です（装置のQボタンを解除してからもう一度）",
        "plugin_not_found": "その名前のデバイスがLiveのブラウザに見つかりません",
        "browser_item_missing": "その名前のデバイスがLiveのブラウザに見つかりません",
        "track_not_found": "指定したトラックが見つかりません",
        "load_rejected": "plugin.load_rejected",
        "load_created_track": "plugin.load_created_track",
        "instrument_on_non_midi": "plugin.instrument_on_non_midi",
        "return_limit": "error.return_limit",
    }

    def _script_load_error(self, error: Exception) -> str:
        value = self.SCRIPT_LOAD_ERRORS.get(str(error), str(error))
        return self._m(value) if value.startswith(("plugin.", "error.")) else value

    def _plugin_verification_error(self, plugin: str, loading: bool = False) -> str:
        key = "plugin.verify_loading" if loading else "plugin.verify_failed"
        return self._m(key, plugin=plugin)

    def _run_plugin_flow(self, intent: Intent, before: Snapshot) -> tuple[int, Intent]:
        """Insert a plug-in on an existing track or add a track and insert it, following Live's behavior.
        New tracks appear right of the selected track with a default name; effects follow the selected device and instruments replace the existing one.
        If the component returns a device name, treat the operation as successful immediately."""
        started = time.perf_counter()
        plugin = str(intent.plugin)
        uri = getattr(self, "_plugin_uris", {}).get(plugin, "")
        target = target_for_intent(before, intent)
        try:
            if ACTIONS[intent.action].kind == "plugin_track":
                kind = intent.track_kind or "midi"
                if kind == "return" and not self._script_available():
                    raise ValueError(self._m("error.live"))
                name = intent.text or None
                if uri:
                    # Add the track first, then load by browser URI so the preferred format is used.
                    added = plugin_script.add_track(kind, name, None)
                    destination = str(added["target_path"]) if kind == "return" else int(added["track_index"])
                    answer = dict(plugin_script.load(plugin, destination, uri))
                    if kind == "return":
                        answer.setdefault("return_index", added["return_index"])
                        answer.setdefault("target_path", added["target_path"])
                    else:
                        answer.setdefault("track_index", added["track_index"])
                else:
                    answer = plugin_script.add_track(kind, name, plugin)
            else:
                if target is None:
                    raise ValueError(self._m("error.named_track_missing"))
                destination: int | str = target.index if isinstance(intent.track, int) else target.path
                answer = plugin_script.load(plugin, destination, uri)
        except plugin_script.ScriptError as error:
            if "main_thread_timeout" not in str(error):
                raise ValueError(self._script_load_error(error)) from error
            answer = {}  # Large instruments may simply need more loading time. Keep rereading below until the device appears.
        track_index = answer.get("track_index")
        return_index = answer.get("return_index")
        loaded = [str(name) for name in answer.get("devices_after") or []]
        deadline = time.monotonic() + PLUGIN_LOAD_WAIT_SECONDS
        while True:
            try:
                self.snapshot, _ = self.reader.read()
            except BridgeError:
                if time.monotonic() >= deadline:
                    raise ValueError(self._plugin_verification_error(plugin, loading=True))
                time.sleep(0.5)
                continue
            if any(plugin.casefold() in name.casefold() or name.casefold() in plugin.casefold() for name in loaded):
                break
            index = track_index if isinstance(track_index, int) else (int(intent.track) if isinstance(intent.track, int) else None)
            if isinstance(return_index, int):
                current = next((item for item in self.snapshot.returns if item.index == return_index), None)
            else:
                current = next((item for item in self.snapshot.tracks if item.index == index), None) if index is not None else target_for_intent(self.snapshot, intent)
            names = [device.name for device in current.devices] if current else []
            if any(plugin.casefold() in name.casefold() or name.casefold() in plugin.casefold() for name in names):
                break
            if time.monotonic() >= deadline:
                raise ValueError(self._plugin_verification_error(plugin))
            time.sleep(0.5)
        if isinstance(track_index, int) and isinstance(intent.track, int):
            intent = replace(intent, track=track_index, target_origin=TargetOrigin.SELECTED)
        elif isinstance(return_index, int):
            intent = replace(intent, track=TargetRef(TargetKind.RETURN, return_index), target_origin=TargetOrigin.SELECTED)
        return round((time.perf_counter() - started) * 1000), intent

    def _run_add_track_via_script(self, intent: Intent) -> int:
        """Add a track and optional built-in device through the component inside Live, following Live's positioning and naming behavior."""
        started = time.perf_counter()
        kind = "return" if intent.action is Action.ADD_RETURN_TRACK or intent.track_kind == "return" else "audio" if intent.action is Action.ADD_AUDIO_TRACK or intent.track_kind == "audio" else "midi"
        name = intent.text or None
        device = str(intent.native_device) if intent.action is Action.ADD_TRACK_WITH_DEVICE else None
        try:
            plugin_script.add_track(kind, name, device)
        except plugin_script.ScriptError as error:
            raise ValueError(self._script_load_error(error)) from error
        self.snapshot, _ = self.reader.read()
        return round((time.perf_counter() - started) * 1000)

    def _refresh_structure(self, _intent: Intent) -> tuple[Snapshot, int]:
        return self.reader.read()

    def _update_send(self, snapshot: Snapshot, intent: Intent, result: BridgeResult) -> Snapshot:
        ack = next((item for item in reversed(result.acks) if item.event == "api_get" and item.property == "value"), None)
        if ack is None or not isinstance(intent.track, int) or intent.send is None:
            return snapshot
        raw = ack.payload[-1] if isinstance(ack.payload, list) and ack.payload else ack.payload
        track = next(item for item in snapshot.tracks if item.index == intent.track)
        sends = list(track.sends) + [0.0] * max(0, intent.send + 1 - len(track.sends))
        sends[intent.send] = float(raw)
        return replace_track(snapshot, intent.track, sends=tuple(sends))

    def _update_rename(self, snapshot: Snapshot, intent: Intent, result: BridgeResult) -> Snapshot:
        ack = next((item for item in reversed(result.acks) if item.event == "api_get" and item.property == "name"), None)
        if ack is None:
            return snapshot
        raw = ack.payload[-1] if isinstance(ack.payload, list) and ack.payload else ack.payload
        return replace_target(snapshot, intent.track, name=str(raw))

    def _refresh_song_prop(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        prop = ACTIONS[intent.action].prop or "is_playing"
        result = self.bridge.run(["--api-get", "live_set", prop, request_id(prop)])
        _require_readback(result, "api_get", prop)
        ack = next(item for item in reversed(result.acks) if item.event == "api_get" and item.property == prop)
        raw = ack.payload[-1] if isinstance(ack.payload, list) and ack.payload else ack.payload
        if not isinstance(raw, (bool, int, float)) or (isinstance(raw, float) and not math.isfinite(raw)):
            raise ValueError(self._m("error.current_value"))
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

    def _refresh_transport(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        if ACTIONS[intent.action].kind != "transport":
            result = self.bridge.run(["--api-get", "live_set", "is_playing", request_id("playing")])
        else:
            result = self._read_transport_until(intent)
        _require_readback(result, "api_get", "is_playing")
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

    def _refresh_tempo(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        result = self.bridge.run(["--api-get", "live_set", "tempo", request_id("tempo")])
        _require_readback(result, "api_get", "tempo")
        ack = next(item for item in reversed(result.acks) if item.event == "api_get" and item.property == "tempo")
        if not isinstance(ack.payload, (int, float)) or not math.isfinite(float(ack.payload)):
            raise ValueError(self._m("error.current_value"))
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

    def _refresh_track_bool(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        track = target_for_intent(self.snapshot, intent)
        if track is None:
            raise StaleSnapshot()
        prop = ACTIONS[intent.action].prop or "mute"
        value_id = request_id(prop)
        result = self.bridge.run(["--api-get", track.path, prop, value_id])
        _require_readback(result, "api_get", prop)
        ack = next(item for item in reversed(result.acks) if item.event == "api_get" and item.property == prop)
        raw = ack.payload[-1] if isinstance(ack.payload, list) and ack.payload else ack.payload
        valid = isinstance(raw, str) if ACTIONS[intent.action].kind == "rename" else isinstance(raw, (bool, int, float))
        if not valid or (isinstance(raw, float) and not math.isfinite(raw)):
            raise ValueError(self._m("error.current_value"))
        if ACTIONS[intent.action].kind == "rename" and str(raw) != track.name:
            raise StaleSnapshot()
        elapsed = result.elapsed_ms if ACTIONS[intent.action].kind == "rename" else result.elapsed_ms + self._confirm_track_name(intent)
        return self._update_from_result(self.snapshot, intent, result), elapsed

    def _refresh_param(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None and intent.param is not None
        track = target_for_intent(self.snapshot, intent)
        if track is None:
            raise StaleSnapshot()
        device = next(item for item in track.devices if intent.param.path.startswith(item.path + " "))
        device_path = intent.param.path.rsplit(" parameters ", 1)[0]
        result = self.bridge.run(["--api-device-parameters", device_path, request_id("params")])
        _require_readback(result, "api_device_parameters")
        elapsed = result.elapsed_ms + self._confirm_track_name(intent)
        parameters_ack = next((item for item in reversed(result.acks) if item.event == "api_device_parameters"), None)
        parameters = parameters_ack.payload.get("parameters") if parameters_ack and isinstance(parameters_ack.payload, Mapping) else None
        matched = next((item for item in parameters if isinstance(item, Mapping) and item.get("path") == intent.param.path), None) if isinstance(parameters, list) else None
        if not isinstance(matched, Mapping) or not isinstance(matched.get("value"), (int, float)) or not math.isfinite(float(matched["value"])):
            raise ValueError(self._m("error.current_value"))
        updated = self._update_from_result(self.snapshot, intent, result)
        return updated, elapsed

    def _refresh_mixer(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        owner = target_for_intent(self.snapshot, intent)
        if owner is None:
            raise StaleSnapshot()
        target = "master" if owner.path == "live_set master_track" else str(owner.index) if isinstance(owner, Track) else owner.path
        result = self.bridge.run(["--api-mixer-status", target, request_id("mixer")])
        _require_readback(result, "api_mixer_status")
        owner_ms = self._confirm_track_name(intent)
        ack = next((item for item in reversed(result.acks) if item.event == "api_mixer_status"), None)
        parameters = ack.payload.get("parameters") if ack and isinstance(ack.payload, Mapping) else None
        field = "volume" if intent.action is Action.VOLUME else "panning"
        parameter = parameters.get(field) if isinstance(parameters, Mapping) else None
        if not isinstance(parameter, Mapping) or not isinstance(parameter.get("value"), (int, float)) or not math.isfinite(float(parameter["value"])):
            raise ValueError(self._m("error.current_value"))
        value = float(parameter["value"])
        payload_display = parameter.get("display")
        if payload_display is not None and str(payload_display) != "":
            display_ack = Ack("api_call", request_id("display"), str(payload_display), str(parameter.get("path") or ""), "str_for_value")
            combined = BridgeResult(tuple(result.acks) + (display_ack,), result.elapsed_ms, 0, False)
            return self._update_from_result(self.snapshot, intent, combined), combined.elapsed_ms + owner_ms
        path = f"{owner.path} mixer_device {field}"
        display = self.bridge.run(["--write", "--api-call", path, "str_for_value", json.dumps([value]), request_id("display")])
        _require_readback(display, "api_call", "str_for_value")
        combined = BridgeResult(tuple(result.acks) + tuple(display.acks), result.elapsed_ms + display.elapsed_ms, 0, False)
        return self._update_from_result(self.snapshot, intent, combined), combined.elapsed_ms + owner_ms

    def _update_from_result(self, snapshot: Snapshot, intent: Intent, result: BridgeResult) -> Snapshot:
        updaters = {
            "tempo": self._update_tempo,
            "transport": self._update_transport,
            "song_call": self._update_transport,
            "track_call": self._update_transport,
            "clip_call": self._update_transport,
            "scene_call": self._update_transport,
            "send": self._update_send,
            "rename": self._update_rename,
            "structure": self._keep_snapshot,
            "structure_device": self._keep_snapshot,
            "plugin": self._keep_snapshot,
            "plugin_track": self._keep_snapshot,
            "clip_prop": self._update_clip_prop,
            "track_bool": self._update_track_bool,
            "track_int": self._update_track_bool,
            "song_bool": self._update_song_prop,
            "jump": self._update_song_prop,
            "mixer": self._update_mixer,
            "param": self._update_param,
            "none": self._keep_snapshot,
        }
        return updaters[ACTIONS[intent.action].kind](snapshot, intent, result)

    def _update_song_prop(self, snapshot: Snapshot, intent: Intent, result: BridgeResult) -> Snapshot:
        prop = ACTIONS[intent.action].prop or ""
        ack = next((item for item in reversed(result.acks) if item.event == "api_get" and item.property == prop), None)
        if ack is None:
            return snapshot
        value: Any = ack.payload
        if isinstance(value, list) and value:
            value = value[-1]
        song = dict(snapshot.song)
        song[prop] = float(value) if prop == "current_song_time" else bool(value)
        return replace(snapshot, song=song, taken_at=time.time())

    def _update_tempo(self, snapshot: Snapshot, _intent: Intent, result: BridgeResult) -> Snapshot:
        records = list(result.acks)
        ack = next((item for item in reversed(records) if item.event == "api_get" and item.property == "tempo"), None)
        return replace(snapshot, tempo=float(ack.payload), taken_at=time.time()) if ack else snapshot

    def _update_transport(self, snapshot: Snapshot, _intent: Intent, result: BridgeResult) -> Snapshot:
        direct = next((item for item in reversed(result.acks) if item.event == "api_get" and item.property == "is_playing"), None)
        if direct is not None:
            return replace(snapshot, playing=bool(direct.payload), taken_at=time.time())
        ack = next((item for item in reversed(result.acks) if item.event == "api_session_context"), None)
        song = ack.payload.get("song") if ack and isinstance(ack.payload, Mapping) else None
        return replace(snapshot, playing=bool(song.get("is_playing")), taken_at=time.time()) if isinstance(song, Mapping) else snapshot

    def _update_track_bool(self, snapshot: Snapshot, intent: Intent, result: BridgeResult) -> Snapshot:
        spec = ACTIONS[intent.action]
        prop = spec.prop or "mute"
        ack = next((item for item in reversed(result.acks) if item.event == "api_get" and item.property == prop), None)
        if ack is None:
            return snapshot
        raw: Any = ack.payload
        if isinstance(raw, list) and raw:
            raw = raw[-1]
        value: Any = int(float(raw)) if spec.kind == "track_int" else bool(raw)
        return replace_target(snapshot, intent.track, **{prop: value})

    def _update_mixer(self, snapshot: Snapshot, intent: Intent, result: BridgeResult) -> Snapshot:
        ack = next((item for item in reversed(result.acks) if item.event == "api_mixer_status"), None)
        parameters = ack.payload.get("parameters") if ack and isinstance(ack.payload, Mapping) else None
        fields = {Action.VOLUME: "volume", Action.PAN: "panning"}
        field = fields[intent.action]
        parameter = parameters.get(field) if isinstance(parameters, Mapping) else None
        if not isinstance(parameter, Mapping):
            return snapshot
        value = float(parameter.get("value", 0.0))
        display_ack = next((item for item in reversed(result.acks) if item.event == "api_call" and item.property == "str_for_value"), None)
        display_value = display_ack.payload if display_ack else None
        if isinstance(display_value, list) and display_value:
            display_value = display_value[-1]
        if display_value is None:
            display_value = parameter.get("display")
        if display_value is None or str(display_value) == "":
            raise ValueError(self._m("error.current_value"))
        shown = str(display_value)
        changes = {"volume": value, "volume_display": shown} if field == "volume" else {"pan": value, "pan_display": shown}
        return replace_target(snapshot, intent.track, **changes)

    def _update_param(self, snapshot: Snapshot, intent: Intent, result: BridgeResult) -> Snapshot:
        if intent.param is None:
            return snapshot
        ack = next((item for item in reversed(result.acks) if item.event == "api_device_parameters"), None)
        parameters = ack.payload.get("parameters") if ack and isinstance(ack.payload, Mapping) else []
        for raw in parameters if isinstance(parameters, list) else []:
            if isinstance(raw, Mapping) and raw.get("path") == intent.param.path:
                return replace_param(snapshot, intent.param.path, raw)
        return snapshot

    def _keep_snapshot(self, snapshot: Snapshot, _intent: Intent, _result: BridgeResult) -> Snapshot:
        return snapshot


def run_stdio(service: LiveJevService) -> int:
    try:
        startup_notice = getattr(service, "startup_notice", None)
        notice = startup_notice() if callable(startup_notice) else None
        if notice is not None:
            print(json.dumps(notice, ensure_ascii=False, separators=(",", ":")), flush=True)
        print(json.dumps(service.start(), ensure_ascii=False, separators=(",", ":")), flush=True)
        for line in sys.stdin:
            message: Mapping[str, Any] | None = None
            try:
                message = json.loads(line)
                if not isinstance(message, Mapping):
                    raise ValueError
            except (ValueError, json.JSONDecodeError, UnicodeError):
                response = {"kind": "error", "line": render("error.json", lang=service.lang)}
            else:
                try:
                    response = service.process(message)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                    service.pending = None
                    service.pending_confirm = None
                    service.pending_confirm_created = None
                    response = {"id": message.get("id"), "kind": "error", "line": render("error.generic", lang=service.lang)}
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
            if response.get("quit"):
                return 0
        return 0
    finally:
        close = getattr(service, "close", None)
        if callable(close):
            close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Jev daemon")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    return run_stdio(LiveJevService(verbose=args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
