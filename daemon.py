#!/opt/homebrew/bin/python3.13
"""Live Jev の常駐プロセス。stdin/stdout は1行1 JSON。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import http.client
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sys
import time
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlsplit

from actions import ACTIONS, request_id, beats_to_bar, MONITOR_NAMES
from bridge_client import Ack, BridgeClient, BridgeError, BridgeResult, ack_map, make_bridge_client
import plugin_script
from intent import ClipNotesRequest, parse_clip_notes_phrase, PluginRequest, extract_plugin_request, plugin_intent, resolve_plugin_name,  GENERIC_DEVICE_WORDS,  ACTION_LABELS, Action, Intent, IntentResult, Number, Step, build_request, candidate_params, interpret_response, parse_local
from llm_rewrite import GeminiRewriter
from messages import LocalizedError, action_label, contains_japanese, render, resolve_language, step_label, using_language
from snapshot import Param, Snapshot, build_snapshot, is_bridge_track, replace_param, replace_track, Clip


API_URL = "https://api.typesafe.ai/v1/systemone"
ERROR_LINE = "Liveに繋がりません。装置が載っているか確認してください"


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
    try:
        lines = (Path.home() / ".zshrc").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
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


@dataclass(frozen=True)
class PreviousIntent:
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


def _normalize(text: str) -> str:
    return "".join(text.casefold().split())


BOOL_PAIRS: tuple[tuple[Action, Action], ...] = (
    (Action.MUTE, Action.UNMUTE), (Action.SOLO, Action.UNSOLO), (Action.PLAY, Action.STOP),
    (Action.ARM, Action.DISARM), (Action.FOLD, Action.UNFOLD),
    (Action.LOOP_ON, Action.LOOP_OFF), (Action.METRONOME_ON, Action.METRONOME_OFF),
    (Action.RECORD_ON, Action.RECORD_OFF), (Action.OVERDUB_ON, Action.OVERDUB_OFF),
    (Action.CLIP_LOOP_ON, Action.CLIP_LOOP_OFF), (Action.CLIP_WARP_ON, Action.CLIP_WARP_OFF),
    (Action.DEVICE_ON, Action.DEVICE_OFF),
)
MONITOR_ACTIONS = {0: Action.MONITOR_IN, 1: Action.MONITOR_AUTO, 2: Action.MONITOR_OFF}
MAX_CLIP_SLOTS = 64
REQUIRE_CONFIRM = os.environ.get("LIVE_JEV_CONFIRM", "0") == "1"  # 実行確認は不要。必要なら LIVE_JEV_CONFIRM=1
PLUGIN_CATALOG_PATH = Path(__file__).with_name("plugins.json")
PLUGIN_HINT = re.compile(r"トラック|挿|差|入れ|いれ|載せ|のせ|開|立ち上げ|起動|プラグイン|シンセ|音源|エフェクト|読み込|ロード|インサート|使|\b(?:track|insert|add|load|open|put|drop|place|plugin|plug-in|synth|instrument|effect|use|apply|launch)\b", re.IGNORECASE)
STRONG_NEGATION = re.compile(r"ないで|しなくて|するな|不要|いらない|要らない|禁止|\b(?:don't|do not|never|no need|not necessary)\b", re.IGNORECASE)
SNAPSHOT_TRUST_SECONDS = 10.0  # この秒数以内に写しを取った/確かめたなら、曲の構成は変わっていないとみなす
PLUGIN_LOAD_WAIT_SECONDS = 25.0  # Omnisphere・Kontakt など重い音源は読み込みに10秒以上かかる


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


def relative_db_target(value: float, step: Step, current_display: str | None) -> float:
    """「3dB下げて」は今の値から −3、「3dB上げて」は +3、「−3dBに」は指定値そのもの。
    上げ下げなのに今の値が読めない（-inf dB など）ときは実行しない。数字を絶対値として書くと無音から大音量になるため。"""
    if step in {Step.DOWN_SMALL, Step.DOWN_BIG, Step.UP_SMALL, Step.UP_BIG}:
        match = re.search(r"-?\d+(?:\.\d+)?", current_display or "")
        if not match or "inf" in (current_display or "").lower():
            raise LocalizedError("error.volume_silent")
        current = float(match.group())
        return current - abs(value) if step in {Step.DOWN_SMALL, Step.DOWN_BIG} else current + abs(value)
    return value


class StaleSnapshot(Exception):
    """写し（トラック数・名前・装置数）が今の曲と食い違っている。取り直してからやり直す。"""


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
        self.previous: PreviousIntent | None = None
        self._startup_notice_emitted = False
        self.lang = resolve_language(os.environ.get("LIVE_JEV_LANG"), default="ja")

    def _m(self, key: str, **values: object) -> str:
        return render(key, lang=self.lang, **values)

    def _error_text(self, error: Exception) -> str:
        if isinstance(error, LocalizedError):
            return error.translated(self.lang)
        line = str(error)
        return self._m("error.generic") if self.lang == "en" and contains_japanese(line) else line

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
            # LLM での言い換え（迷ったときの遅い車線）は公開版では使わない。
            # 試すときだけ LIVE_JEV_LLM=1 で有効にする。無効のときは聞き返しをそのまま返す。
            if self.llm_key is None and os.environ.get("LIVE_JEV_LLM", "0") == "1":
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
        line = self._m("status.connected", tracks=tracks, tempo=tempo) if self.live and self.snapshot else self._m("status.disconnected")
        result: dict[str, Any] = {"kind": "status", "live": self.live, "jev": bool(self.key), "tracks": tracks, "tempo": tempo, "line": line}
        if message_id is not None:
            result["id"] = message_id
        return result

    def refresh(self, message_id: Any = None) -> dict[str, Any]:
        try:
            self.snapshot, elapsed = self.reader.read()
            self.live = True
        except BridgeError:
            self.live = False
            return {"id": message_id, "kind": "error", "line": self._m("error.live")}
        answer = self.status(message_id)
        answer["ms"] = {"jev": 0, "llm": 0, "bridge": elapsed, "total": elapsed}
        return answer

    @staticmethod
    def _ms(started: float, jev: int, llm: int, bridge: int) -> dict[str, int]:
        return {"jev": jev, "llm": llm, "bridge": bridge, "total": round((time.perf_counter() - started) * 1000)}

    def process(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """一言を処理する。曲の構成（トラック数・名前・装置数）が写しと違っていたら写しを取り直して、その一言をもう一度やり直す。"""
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

    def _process_once(self, message: Mapping[str, Any]) -> dict[str, Any]:
        message_id = message.get("id")
        command = message.get("cmd")
        if command == "lang":
            value = message.get("value")
            if value not in {"auto", "ja", "en"}:
                return {"id": message_id, "kind": "error", "line": self._m("error.generic")}
            self.lang = resolve_language(value, default="ja")
            return self.status(message_id)
        if command == "status":
            return self.status(message_id)
        if command == "refresh":
            return self.refresh(message_id)
        if command == "quit":
            return {"id": message_id, "kind": "status", "line": self._m("status.quit"), "quit": True}
        if "confirm" in message:
            return self._answer_confirm(message_id, bool(message.get("confirm")))
        if command == "undo":
            return self._undo_button(message_id)
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"id": message_id, "kind": "error", "line": self._m("error.empty")}
        if self.snapshot is None:
            return {"id": message_id, "kind": "error", "line": self._m("error.live")}
        if STRONG_NEGATION.search(text.replace("’", "'")):
            # 打ち消しの一言は Jev にも回さない（確認なしで即実行する道具なので、聞き返しより「何もしない」が安全）
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
        started = time.perf_counter()
        filled = self._fill_pending(text)
        if filled is not None:
            filled = self._apply_selected_track(filled)
            decision = self._decision(filled, message_id)
            if decision is not None and decision.get("kind") == "ask":
                return self._rewrite_and_process(text, filled, decision, message_id, 0, started)
            if decision is not None:
                decision["ms"] = self._ms(started, 0, 0, 0)
                return decision
            return self._execute(filled.intent, message_id, 0, 0, started, text, None)
        request = extract_plugin_request(text, self.snapshot)
        if request is not None and request.raw_name.strip().casefold() in {word.casefold() for word in GENERIC_DEVICE_WORDS}:
            request = None
        deferred_plugin: PluginRequest | None = None
        if request is not None and request.action is Action.INSERT_PLUGIN and resolve_plugin_name(request.raw_name, self._plugin_names()) is None:
            # 「メトロノームをつけて」「ループを入れて」のように、挿す動詞は他の操作にも使う。名前が一覧にすぐ当たらないときは
            # 先にふつうの判定を試し、決まらなかったときだけプラグインとして探す（カタカナ名はそこで Jev が一覧から選ぶ）。
            deferred_plugin, request = request, None
        if request is not None:
            return self._process_plugin_request(request, text, message_id, started)
        notes_request = parse_clip_notes_phrase(text, self.snapshot)
        if notes_request is not None and self._script_available():
            return self._run_clip_notes(notes_request, text, message_id, started)
        plugin_notice = self._plugin_notice(message_id, text)
        if plugin_notice is not None:
            plugin_notice["ms"] = self._ms(started, 0, 0, 0)
            return plugin_notice
        local = parse_local(text, self.snapshot)
        if local is not None:
            result = self._resolve_previous(IntentResult(local, (), (), ()), text)
            result = self._apply_selected_track(self._resolve_release_target(result))
            if result.intent.action is Action.NONE:
                line = self._m("info.no_undo") if re.search(r"戻|取り消|undo", text, re.IGNORECASE) else self._m("info.no_repeat")
                return {"id": message_id, "kind": "info", "line": line, "ms": self._ms(started, 0, 0, 0)}
            decision = self._decision(result, message_id)
            if decision is not None:
                decision["ms"] = self._ms(started, 0, 0, 0)
                return decision
            return self._execute(result.intent, message_id, 0, 0, started, text, None)
        if not self.key:
            return {"id": message_id, "kind": "error", "line": self._m("error.jev_key")}
        jev_started = time.perf_counter()
        try:
            response = self.requester(build_request(self.snapshot, text), self.key)
        except RuntimeError:
            return {"id": message_id, "kind": "error", "line": self._m("error.jev")}
        jev_ms = round((time.perf_counter() - jev_started) * 1000)
        if self.verbose:
            print(f"[jev] questions={len(build_request(self.snapshot, text)['questions'])} ms={jev_ms}", file=sys.stderr)
        result = interpret_response(self.snapshot, text, response)
        result = self._resolve_previous(result, text)
        result = self._apply_selected_track(self._resolve_release_target(result))
        decision = self._decision(result, message_id)
        undecided = decision is not None and decision.get("kind") in {"ask", "info"}
        if undecided and deferred_plugin is not None and (
            result.intent.action is Action.NONE or result.intent.action_conf < 0.6 or ACTIONS[result.intent.action].kind == "structure_device"
        ):
            return self._process_plugin_request(deferred_plugin, text, message_id, started, jev_ms)
        if decision is not None and decision.get("kind") in {"ask", "info"} and result.intent.action is Action.NONE:
            bare = self._bare_plugin_request(text, message_id, started, jev_ms)
            if bare is not None:
                return bare
        if decision is not None and decision.get("kind") in {"ask", "info"} and PLUGIN_HINT.search(text) and (
            ACTIONS[result.intent.action].kind == "structure_device" or result.intent.action is Action.NONE
        ):
            fallback = self._plugin_fallback(text, message_id, started, jev_ms)
            if fallback is not None:
                return fallback
        if result.intent.compound > 0.7 or (decision is not None and decision.get("kind") == "ask"):
            return self._rewrite_and_process(text, result, decision, message_id, jev_ms, started)
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
    ) -> dict[str, Any]:
        if not self.llm_key:
            fallback = initial_decision or self._ask_for_action(initial, message_id)
            fallback["ms"] = self._ms(started, jev_ms, 0, 0)
            return fallback
        llm_started = time.perf_counter()
        try:
            rewritten_text = self.rewriter(self.snapshot, utterance, self.llm_key)  # type: ignore[arg-type]
        except RuntimeError as error:
            return {
                "id": message_id,
                "kind": "error",
                "line": self._error_text(error),
                "ms": self._ms(started, jev_ms, round((time.perf_counter() - llm_started) * 1000), 0),
            }
        llm_ms = round((time.perf_counter() - llm_started) * 1000)
        rewritten = [line.strip() for line in rewritten_text.splitlines() if line.strip()]
        if not rewritten or any(line == "不明" for line in rewritten):
            fallback = (
                initial_decision
                if initial_decision is not None and initial_decision.get("kind") == "ask"
                else self._ask_for_action(initial, message_id)
            )
            fallback["ms"] = self._ms(started, jev_ms, llm_ms, 0)
            return fallback

        completed: list[str] = []
        bridge_ms = 0
        latest: dict[str, Any] | None = None
        for index, line in enumerate(rewritten):
            line_started = time.perf_counter()
            try:
                response = self.requester(build_request(self.snapshot, line), self.key)  # type: ignore[arg-type]
            except RuntimeError:
                return {
                    "id": message_id,
                    "kind": "error",
                    "line": self._m("error.jev"),
                    "ms": self._ms(started, jev_ms, llm_ms, 0),
                }
            jev_ms += round((time.perf_counter() - line_started) * 1000)
            result = interpret_response(self.snapshot, line, response)
            result = self._apply_selected_track(self._resolve_release_target(result))
            decision = self._decision(result, message_id)
            if decision is not None:
                fallback = decision if decision.get("kind") == "ask" else self._ask_for_action(result, message_id)
                if completed:
                    marker = f"{index + 1}行目" if self.lang == "ja" else f"line {index + 1}"
                    fallback["line"] = " / ".join(completed) + f" / {marker}: {fallback['line']}"
                fallback["ms"] = self._ms(started, jev_ms, llm_ms, bridge_ms)
                return fallback
            executed = self._execute(result.intent, message_id, jev_ms, llm_ms, started, utterance, rewritten)
            bridge_ms += int(executed.get("ms", {}).get("bridge", 0))
            executed["ms"] = self._ms(started, jev_ms, llm_ms, bridge_ms)
            if executed.get("kind") != "result":
                prefix = " / ".join(completed)
                marker = f"{index + 1}行目" if self.lang == "ja" else f"line {index + 1}"
                failed = f"{marker}: {executed.get('line', '')}"
                remaining = rewritten[index + 1:]
                suffix = (f"。未実行: {' / '.join(remaining)}" if self.lang == "ja" else f". Not run: {' / '.join(remaining)}") if remaining else ""
                executed["line"] = " / ".join(item for item in (prefix, failed) if item) + suffix
                return executed
            completed.append(str(executed.get("line", "")))
            latest = executed
        assert latest is not None
        latest["line"] = " / ".join(completed)
        latest["ms"] = self._ms(started, jev_ms, llm_ms, bridge_ms)
        return latest

    def _ask_for_action(self, result: IntentResult, message_id: Any) -> dict[str, Any]:
        self.pending = Pending(result, "action")
        options = [action_label(name.value, lang=self.lang) for name in Action if action_label(name.value, lang="ja") in result.action_options]
        return {"id": message_id, "kind": "ask", "line": self._m("ask.action"), "options": options or list(result.action_options)}

    def _localized_options(self, options: tuple[str, ...]) -> list[str]:
        if self.lang == "ja":
            return list(options)
        replacements = {"マスター": "Master", "選択中のトラック": "Selected track"}
        return [replacements.get(option, option) for option in options]

    def _selected_track_index(self) -> int | None:
        """Live の画面で選択中のトラックの index（写しではなく今の値を1命令で読む）。"""
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
        match = re.fullmatch(r"live_set tracks (\d+)", path)
        return int(match.group(1)) if match else None

    def _apply_selected_track(self, result: IntentResult) -> IntentResult:
        """「選択トラック」の指定と、トラック未指定のときの既定（選択中のトラック）を解決する。"""
        intent = result.intent
        spec = ACTIONS[intent.action]
        wants_selected = intent.track == "selected"
        # トラック名が言われていない（track_stated が低い）なら、Jev の当て推量の track は使わず選択中のトラックにする。
        # クリップ・デバイス・センドの頭から逆引きしたトラックはそのまま使う。
        # 「名前を言った」と見なすのは、Jev がほぼ確信している（0.9以上）か、確からしさ 0.6 以上で言った度合いも 0.5 以上のとき。
        # 「MIDI」のような一般語の名前は track_stated が低く出るので、確からしさ側でも救う。
        conf = intent.track_conf if isinstance(intent.track, int) else 0.0
        named = isinstance(intent.track, int) and (conf >= 0.9 or (conf >= 0.6 and intent.track_stated >= 0.5))
        unspecified = (
            spec.needs_track
            and intent.action is not Action.PARAM
            and intent.track != "master"
            and not named
            and intent.track_stated < 0.8
            and intent.clip is None
            and intent.device is None
            and intent.send is None
        )
        if not wants_selected and not unspecified:
            return result
        index = self._selected_track_index()
        if index is None:
            return result if not wants_selected else replace(result, intent=replace(intent, track=None, track_conf=0.0))
        if any(is_bridge_track(track) and track.index == index for track in self.snapshot.tracks):
            return result if not wants_selected else replace(result, intent=replace(intent, track=None, track_conf=0.0))
        return replace(result, intent=replace(intent, track=index, track_conf=1.0, track_stated=1.0))

    def _decision(self, result: IntentResult, message_id: Any) -> dict[str, Any] | None:
        intent = result.intent
        spec = ACTIONS[intent.action]
        if intent.compound > 0.7:
            self.pending = None
            return {"id": message_id, "kind": "info", "line": self._m("info.one_at_a_time")}
        if intent.action is Action.NONE and intent.needs_generation > 0.5:
            return {"id": message_id, "kind": "info", "line": self._m("info.freeform")}
        if intent.action_conf < 0.6 or intent.action is Action.NONE:
            return self._ask_for_action(result, message_id)
        if isinstance(intent.track, int) and not any(track.index == intent.track for track in self.snapshot.tracks):
            self.pending = None
            return {"id": message_id, "kind": "error", "line": self._m("error.named_track_missing")}
        if intent.track == "master" and intent.action is not Action.VOLUME:
            self.pending = None
            return {"id": message_id, "kind": "error", "line": self._m("error.master_unsupported")}
        track_is_uncertain = intent.track is None or intent.track_conf < 0.6
        if intent.action is not Action.PARAM and spec.needs_track and intent.track_stated < 0.5 and intent.track_conf < 0.9:
            track_is_uncertain = True
        if spec.needs_track and track_is_uncertain:
            self.pending = Pending(result, "track")
            return {"id": message_id, "kind": "ask", "line": self._m("ask.track"), "options": self._localized_options(result.track_options)}
        if spec.needs_param and (intent.param is None or intent.param_conf < 0.6):
            self.pending = Pending(result, "param")
            return {"id": message_id, "kind": "ask", "line": self._m("ask.param"), "options": list(result.param_options)}
        if spec.kind == "structure_device" and (intent.native_device is None or intent.native_device_conf < 0.6):
            self.pending = Pending(result, "native_device")
            return {"id": message_id, "kind": "ask", "line": self._m("ask.device_native"), "options": ["Operator", "Wavetable", "Drum Rack", "Reverb", "EQ Eight"]}
        if spec.needs_send and (intent.send is None or intent.send_conf < 0.6):
            self.pending = Pending(result, "send")
            return {"id": message_id, "kind": "ask", "line": self._m("ask.send"), "options": list(result.send_options)}
        if spec.needs_scene and (intent.scene is None or intent.scene_conf < 0.6):
            self.pending = Pending(result, "scene")
            return {"id": message_id, "kind": "ask", "line": self._m("ask.scene"), "options": list(result.scene_options)}
        if spec.needs_clip and (intent.clip is None or intent.clip_conf < 0.6):
            self.pending = Pending(result, "clip")
            return {"id": message_id, "kind": "ask", "line": self._m("ask.clip"), "options": list(result.clip_options)}
        if spec.needs_device and (intent.device is None or intent.device_conf < 0.6 or intent.param is None):
            self.pending = Pending(result, "device")
            return {"id": message_id, "kind": "ask", "line": self._m("ask.device"), "options": list(result.device_options)}
        if spec.needs_step and intent.number is None and (intent.step in {Step.NONE, Step.SET} or intent.step_conf < 0.6):
            self.pending = Pending(result, "step")
            return {"id": message_id, "kind": "ask", "line": self._m("ask.direction"), "options": [self._m("option.up_small"), self._m("option.down_small")]}
        self.pending = None
        return None

    def _resolve_previous(self, result: IntentResult, text: str) -> IntentResult:
        intent = result.intent
        previous = self.previous
        if previous is None or intent.refers_previous <= 0.6:
            return result
        if intent.track is not None and intent.track != previous.track:
            return result
        restoring = bool(re.search(r"戻|もど|元|取り消", text))
        if not restoring and (not previous.confirmed or previous.step not in {Step.UP_SMALL, Step.UP_BIG, Step.DOWN_SMALL, Step.DOWN_BIG}):
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
        changes: dict[str, Any] = {
            "action": previous.action,
            "action_conf": 1.0,
            "track": previous.track,
            "track_conf": 1.0,
            "track_stated": 1.0,
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
        if restoring:
            action, number = self._restore_operation(previous)
            changes.update(action=action, number=number, step=Step.SET, step_conf=1.0)
        return replace(result, intent=replace(intent, **changes))

    def _resolve_release_target(self, result: IntentResult) -> IntentResult:
        assert self.snapshot is not None
        intent = result.intent
        if intent.action not in {Action.UNMUTE, Action.UNSOLO} or intent.track_stated >= 0.5:
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
            intent=replace(intent, track=active[0].index, track_conf=1.0, track_stated=1.0),
        )

    @staticmethod
    def _restore_operation(previous: PreviousIntent) -> tuple[Action, Number | None]:
        for on_action, off_action in BOOL_PAIRS:
            if previous.action in {on_action, off_action}:
                return (on_action if previous.before else off_action), None
        if previous.action in MONITOR_ACTIONS:
            return MONITOR_ACTIONS[int(previous.before)] if int(previous.before) in MONITOR_ACTIONS else previous.action, None
        if ACTIONS[previous.action].kind in {"song_call", "track_call", "clip_call", "scene_call", "structure", "structure_device", "plugin", "plugin_track", "rename", "jump", "none"}:
            return Action.NONE, None
        return previous.action, Number(float(previous.before), "raw")

    @staticmethod
    def _same_value(left: float | bool, right: float | bool) -> bool:
        if isinstance(left, bool) or isinstance(right, bool):
            return left is right
        return abs(float(left) - float(right)) <= 1e-4

    @staticmethod
    def _value_before(snapshot: Snapshot, intent: Intent) -> float | bool:
        if intent.action is Action.VOLUME:
            if intent.track == "master":
                return snapshot.master_volume
            return next(track.volume for track in snapshot.tracks if track.index == intent.track)
        if intent.action is Action.PAN:
            return next(track.pan for track in snapshot.tracks if track.index == intent.track)
        if intent.action in {Action.MUTE, Action.UNMUTE}:
            return next(track.mute for track in snapshot.tracks if track.index == intent.track)
        if intent.action in {Action.SOLO, Action.UNSOLO}:
            return next(track.solo for track in snapshot.tracks if track.index == intent.track)
        if intent.action is Action.TEMPO:
            return snapshot.tempo
        if intent.action in {Action.PLAY, Action.STOP}:
            return snapshot.playing
        if ACTIONS[intent.action].kind == "param" and intent.param is not None:
            return next(
                parameter.value
                for track in snapshot.tracks
                for device in track.devices
                for parameter in device.params
                if parameter.path == intent.param.path
            )
        spec = ACTIONS[intent.action]
        if spec.kind in {"track_bool", "track_int"} and isinstance(intent.track, int) and spec.prop:
            return getattr(next(track for track in snapshot.tracks if track.index == intent.track), spec.prop)
        if spec.kind == "song_bool" and spec.prop:
            return bool(snapshot.song.get(spec.prop))
        if spec.kind == "jump":
            return float(snapshot.song.get("current_song_time", 0.0) or 0.0)
        if spec.kind in {"song_call", "track_call", "transport", "clip_call", "scene_call"}:
            return snapshot.playing
        if spec.kind == "send" and isinstance(intent.track, int) and intent.send is not None:
            track = next(item for item in snapshot.tracks if item.index == intent.track)
            return track.sends[intent.send] if intent.send < len(track.sends) else 0.0
        if spec.kind in {"rename", "structure", "structure_device", "plugin", "plugin_track"}:
            return float(len(snapshot.tracks))
        if spec.kind == "clip_prop" and isinstance(intent.track, int) and intent.clip is not None and spec.prop:
            track = next(item for item in snapshot.tracks if item.index == intent.track)
            clip = next((item for item in track.clips if item.slot == intent.clip), None)
            raw = clip.props.get(spec.prop) if clip else None
            if spec.prop in {"looping", "warping"}:
                return bool(raw)
            return float(raw or 0.0)
        raise ValueError("直前の値を保存できません")

    def _fill_pending(self, text: str) -> IntentResult | None:
        pending = self.pending
        self.pending = None
        if pending is None or self.snapshot is None:
            return None
        normalized = _normalize(text)
        intent = pending.result.intent
        if pending.field == "track":
            for track in self.snapshot.tracks:
                if is_bridge_track(track):
                    continue
                if normalized == _normalize(track.name):
                    return replace(pending.result, intent=replace(intent, track=track.index, track_conf=1.0, track_stated=1.0))
            if normalized in {_normalize("マスター"), _normalize("master"), _normalize("master track")}:
                return replace(pending.result, intent=replace(intent, track="master", track_conf=1.0, track_stated=1.0))
        elif pending.field == "param" and isinstance(intent.track, int):
            track = next((item for item in self.snapshot.tracks if item.index == intent.track), None)
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
        elif pending.field == "send":
            for index, name in enumerate(self.snapshot.returns):
                letter = chr(ord("A") + index)
                if normalized in {_normalize(name), _normalize(letter), _normalize(f"センド{letter}"), _normalize(f"send {letter}"), _normalize(f"{letter}（{name}）"), _normalize(f"{letter} ({name})")}:
                    return replace(pending.result, intent=replace(intent, send=index, send_conf=1.0))
        elif pending.field == "scene":
            for scene in self.snapshot.scenes:
                if normalized in {_normalize(scene.name), _normalize(f"シーン{scene.index + 1}"), _normalize(f"scene {scene.index + 1}"), str(scene.index + 1)}:
                    return replace(pending.result, intent=replace(intent, scene=scene.index, scene_conf=1.0))
        elif pending.field == "clip" and isinstance(intent.track, int):
            track = next((item for item in self.snapshot.tracks if item.index == intent.track), None)
            for clip in track.clips if track else ():
                if normalized in {_normalize(clip.name), _normalize(f"スロット{clip.slot + 1}"), _normalize(f"slot {clip.slot + 1}"), str(clip.slot + 1)}:
                    return replace(pending.result, intent=replace(intent, clip=clip.slot, clip_conf=1.0))
        elif pending.field == "device" and isinstance(intent.track, int):
            track = next((item for item in self.snapshot.tracks if item.index == intent.track), None)
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
    ) -> dict[str, Any]:
        spec = ACTIONS[intent.action]
        if spec.confirm and REQUIRE_CONFIRM:
            self.pending_confirm = (intent, jev_ms, llm_ms, utterance, rewritten)
            label = action_label(intent.action.value, lang=self.lang)
            target = ""
            if isinstance(intent.track, int) and self.snapshot is not None:
                track = next((item for item in self.snapshot.tracks if item.index == intent.track), None)
                target = (f"{track.name} の" if self.lang == "ja" else f"{track.name}: ") if track else ""
            if intent.text and intent.action is Action.RENAME:
                detail = f"（→「{intent.text}」）" if self.lang == "ja" else f" to {intent.text}"
            elif intent.text and intent.text != "audio":
                detail = f"（名前: {intent.text}）" if self.lang == "ja" else f" named {intent.text}"
            else:
                detail = ""
            if intent.action is Action.ADD_TRACK_WITH_DEVICE:
                kind_label = self._m("kind.audio_track" if intent.text == "audio" else "kind.midi_track")
                label = f"「{intent.native_device}」入りの{kind_label}追加" if self.lang == "ja" else f"add {kind_label} with {intent.native_device}"
            if intent.action is Action.ADD_TRACK_WITH_PLUGIN:
                kind_label = self._m("kind.audio_track" if intent.text == "audio" else "kind.midi_track")
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
        return self._execute_now(intent, message_id, jev_ms, llm_ms, started, utterance, rewritten)

    _plugin_names_cache: tuple[str, ...] | None = None
    _plugin_script_ok: bool | None = None

    def _plugin_names(self) -> tuple[str, ...]:
        if self._plugin_names_cache is not None:
            return self._plugin_names_cache
        names: tuple[str, ...] = ()
        self._plugin_script_ok = plugin_script.ping()
        if self._plugin_script_ok:
            try:
                names = tuple(sorted({str(item.get("name")) for item in plugin_script.list_plugins() if item.get("name")}))
            except plugin_script.ScriptError:
                names = ()
        if not names:
            names = load_plugin_catalog()
        self._plugin_names_cache = names
        return names

    def _process_plugin_request(self, request: PluginRequest, text: str, message_id: Any, started: float, jev_ms: int = 0) -> dict[str, Any]:
        """外部プラグインの依頼（挿す／入りのトラックを作る）を、名前の照合→選択トラックの解決→実行まで進める。"""
        catalog = self._plugin_names()
        plugin = resolve_plugin_name(request.raw_name, catalog)
        if plugin is None and catalog and self.key:
            plugin, picked_ms = self._pick_plugin_with_jev(request.raw_name, catalog)
            jev_ms += picked_ms
        if plugin is None:
            return {"id": message_id, "kind": "info", "line": self._m("plugin.not_found", name=request.raw_name), "ms": self._ms(started, jev_ms, 0, 0)}
        if request.action is Action.INSERT_PLUGIN and (request.track is None or request.track == "selected"):
            index = self._selected_track_index()
            if index is None or any(is_bridge_track(t) and t.index == index for t in self.snapshot.tracks):
                return {"id": message_id, "kind": "ask", "line": self._m("ask.insert_track"), "options": [t.name for t in self.snapshot.tracks if not is_bridge_track(t)][:5], "ms": self._ms(started, jev_ms, 0, 0)}
            request = replace(request, track=index)
        result = self._apply_selected_track(IntentResult(plugin_intent(request, plugin), (), (), ()))
        decision = self._decision(result, message_id)
        if decision is not None:
            decision["ms"] = self._ms(started, jev_ms, 0, 0)
            return decision
        return self._execute(result.intent, message_id, jev_ms, 0, started, text, None)

    def _short_line(self, line: str) -> str:
        """結果の文から先頭の「<トラック名>: 」を外す（使う人に不要な情報は出さない。対象は decision.track に残る）。"""
        names = [track.name for track in self.snapshot.tracks if track.name] if self.snapshot else []
        for name in sorted(names + ["マスター", "Master", "Main"], key=len, reverse=True):
            if line.startswith(f"{name}: "):
                return line[len(name) + 2:]
        return line

    def _script_available(self) -> bool:
        if self._plugin_script_ok is None:
            self._plugin_script_ok = plugin_script.ping()
        return bool(self._plugin_script_ok)

    CLIP_NOTES_ERRORS = {
        "no_clip": "clip.no_clip", "midi_only": "clip.midi_only", "no_notes": "clip.no_notes",
        "out_of_range": "clip.out_of_range", "bad_grid": "clip.bad_grid",
        "unknown_action": "clip.old_script", "unknown_op": "clip.old_script", "main_thread_timeout": "clip.timeout",
    }

    def _run_clip_notes(self, request: ClipNotesRequest, text: str, message_id: Any, started: float) -> dict[str, Any]:
        """クリップのノート変形（クオンタイズ・レガート・移調・強弱・ループ倍）。Live の中の部品が1回の取り消しで戻せる形で実行する。"""
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
            fields["track_name"] = expected.name  # Live 側で名前が違えば書かずに断る（番号ずれで別トラックに書かない）
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
        self.previous = None  # 「元に戻す」は Live の取り消し1回で戻る
        return {
            "id": message_id, "kind": "result", "line": what,
            "decision": {"utterance": text, "rewritten": None, "action": f"clip_{request.op}", "action_label": "ノートの変形" if self.lang == "ja" else "Note transform", "track": answer.get("track") or None,
                         "param": None, "step_label": None, "number": None, "conf": {"action": 1.0, "track": None, "param": None}, "before": None, "after": None},
            "ms": {"jev": 0, "llm": 0, "bridge": script_ms, "total": round((time.perf_counter() - started) * 1000)},
        }

    def _bare_plugin_request(self, text: str, message_id: Any, started: float, jev_ms: int) -> dict[str, Any] | None:
        """「Serum 2をお願い」「セラムちょうだい」のように動詞が無い頼み方。何をするか決められなかったときだけ、
        残りの言葉が一覧のプラグイン名（別名・完全一致・部分一致）に当たるかを Jev なしで確かめ、当たれば選択トラックへ挿す。"""
        from intent import normalize_phrase
        name = re.sub(r"(?:を|が|も)$", "", normalize_phrase(text)).strip()
        if not name or len(name) > 40 or name.casefold() in {word.casefold() for word in GENERIC_DEVICE_WORDS}:
            return None
        catalog = self._plugin_names()
        if not catalog or resolve_plugin_name(name, catalog) is None:
            return None
        return self._process_plugin_request(PluginRequest(Action.INSERT_PLUGIN, name, "selected", None), text, message_id, started, jev_ms)

    def _plugin_fallback(self, text: str, message_id: Any, started: float, jev_ms: int) -> dict[str, Any] | None:
        """定型文に当たらず Jev が「内蔵デバイス入り」と判定して聞き返す直前の保険。
        一言全体を Jev に渡して一覧のプラグイン名を1つ選ばせ、当たれば外部プラグインの依頼として進める（Omnisphere など）。"""
        catalog = self._plugin_names()
        if not catalog or not self.key:
            return None
        plugin, picked_ms = self._pick_plugin_with_jev(text, catalog)
        if plugin is None:
            return None
        wants_new_track = re.search(r"新しい|新規|あたらしい|トラック\s*(?:を)?\s*(?:作|追加|足|増や)|\b(?:new|another|fresh)\s+(?:midi\s+|audio\s+|instrument\s+)?track\b|\b(?:create|make|add)\s+(?:a\s+)?(?:midi\s+|audio\s+|instrument\s+)?track\b", text, re.IGNORECASE) is not None
        audio = "audio" if re.search(r"オーディオ|audio", text, re.IGNORECASE) else None
        if wants_new_track:
            request = PluginRequest(Action.ADD_TRACK_WITH_PLUGIN, plugin, None, audio)
        else:
            request = PluginRequest(Action.INSERT_PLUGIN, plugin, "selected", None)
        return self._process_plugin_request(request, text, message_id, started, jev_ms + picked_ms)

    def _pick_plugin_with_jev(self, raw_name: str, catalog: tuple[str, ...]) -> tuple[str | None, int]:
        """カタカナや略称（セラム・バルハラ）を、一覧の名前に Jev で当てる。250件ずつに分けて最も確からしい1件。"""
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
        """外部プラグインや「リバーブ」のような一般語での挿入依頼は、実行せず案内だけ返す。"""
        if not re.search(r"入り|付き|つき|載せ|のせ|挿し|さして|インサート|追加|\b(?:insert|add|load|open|put|drop|throw|place|bring up|fire up|launch|pull up|use|apply|stick|slap)\b", text, re.IGNORECASE):
            return None
        lowered = text.casefold()
        catalog = load_plugin_catalog()
        exact = [name for name in catalog if len(name) >= 3 and name.casefold() in lowered]
        if exact:
            longest = max(exact, key=len)
            if self._plugin_script_ok:
                return {"id": message_id, "kind": "info", "line": self._m("plugin.phrase_hint", name=longest)}
            return {"id": message_id, "kind": "info", "line": self._m("plugin.enable_script", name=longest)}
        for word, keys in GENERIC_DEVICE_WORDS.items():
            if word.casefold() in lowered:
                matches = [name for name in catalog if any(key in name.casefold() for key in keys)][:6]
                hint = ("、" if self.lang == "ja" else ", ").join(matches) if matches else self._m("plugin.none")
                shown_word = word if self.lang == "ja" or not contains_japanese(word) else "That term"
                return {"id": message_id, "kind": "info", "line": self._m("plugin.generic", word=shown_word, hint=hint)}
        return None

    def _undo_button(self, message_id: Any) -> dict[str, Any]:
        """窓の「元に戻す」。Live Jev が戻せる直前の変更ならそれを戻し、戻せない種類なら Live の取り消し。"""
        if self.snapshot is None:
            return {"id": message_id, "kind": "error", "line": self._m("error.live")}
        previous = self.previous
        if previous is not None and self._restore_operation(previous)[0] is not Action.NONE:
            return self.process({"id": message_id, "text": "戻して"})
        started = time.perf_counter()
        from intent import _local_intent
        # 「新しいトラック（＋デバイス）」は Live の取り消しが2〜3回ぶんになる（実測。回数は一定しない）。
        # 回数で決め打ちせず、トラックの本数が操作前に戻るまで取り消す（最大4回）。
        target = getattr(self, "_undo_target_tracks", None)
        self._undo_target_tracks = None
        if target is not None:
            for _ in range(4):
                try:
                    self.bridge.run(["--write", "--api-call", "live_set", "undo", "[]", request_id("undo")])
                    time.sleep(0.35)
                    self.snapshot, _ = self.reader.read()
                except BridgeError:
                    return {"id": message_id, "kind": "error", "line": self._m("error.live")}
                if len(self.snapshot.tracks) <= target:
                    break
            self.previous = None
            return {"id": message_id, "kind": "result", "line": self._m("readback.text.undo"), "ms": self._ms(started, 0, 0, 0)}
        return self._execute(_local_intent(Action.UNDO), message_id, 0, 0, started, "元に戻す", None)

    def _answer_confirm(self, message_id: Any, confirmed: bool) -> dict[str, Any]:
        pending = self.pending_confirm
        self.pending_confirm = None
        if pending is None:
            return {"id": message_id, "kind": "info", "line": self._m("info.no_confirmation")}
        if not confirmed:
            return {"id": message_id, "kind": "info", "line": self._m("info.cancelled")}
        intent, jev_ms, llm_ms, utterance, rewritten = pending
        return self._execute_now(intent, message_id, jev_ms, llm_ms, time.perf_counter(), utterance, rewritten)

    def _execute_now(
        self,
        intent: Intent,
        message_id: Any,
        jev_ms: int,
        llm_ms: int,
        started: float,
        utterance: str,
        rewritten: list[str] | None,
    ) -> dict[str, Any]:
        assert self.snapshot is not None
        before = self.snapshot
        bridge_ms = 0
        write_unknown = False
        confirmed = False
        try:
            restoring = bool(intent.refers_previous > 0.6 and re.search(r"戻|もど|元|取り消", utterance))
            if restoring and self.previous is not None:
                self.snapshot, read_ms = self._refresh_before_restore(intent)
                bridge_ms += read_ms
                current = self._value_before(self.snapshot, intent)
                if not self._same_value(current, self.previous.after):
                    shown = self._shown_value(self.snapshot, intent) or str(current)
                    return {
                        "id": message_id,
                        "kind": "info",
                        "line": self._m("info.changed_manually", value=shown),
                        "ms": self._ms(started, jev_ms, llm_ms, bridge_ms),
                    }
                before = self.snapshot
            bridge_ms += self._confirm_track_name(intent)
            self._undo_target_tracks = None
            if ACTIONS[intent.action].kind in {"plugin", "plugin_track"}:
                bridge_ms += self._run_plugin_flow(intent, before)
                confirmed = True
                if ACTIONS[intent.action].kind == "plugin_track":
                    self._undo_target_tracks = len(before.tracks)
            elif ACTIONS[intent.action].kind in {"structure", "structure_device"} and self._script_available():
                bridge_ms += self._run_add_track_via_script(intent)
                confirmed = True
                self._undo_target_tracks = len(before.tracks)
            elif intent.action is Action.VOLUME and intent.number and intent.number.unit == "db":
                self.snapshot, write_ms, write_unknown = self._set_volume_db(intent)
                bridge_ms += write_ms
                confirmed = not write_unknown
            else:
                batches = ACTIONS[intent.action].apply(before, intent)
                if not batches:
                    raise LocalizedError("error.no_action")
                all_acks: list[Ack] = []
                expected_after: float | bool | None = None
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
            line = self._short_line(ACTIONS[intent.action].readback(self.snapshot, intent))
            if not confirmed and not write_unknown and ACTIONS[intent.action].kind in {"clip_prop", "song_bool", "track_bool", "track_int"}:
                line += self._m("info.unchanged")
            if intent.action is Action.VOLUME:
                old_display = before.master_display if intent.track == "master" else next(track.volume_display for track in before.tracks if track.index == intent.track)
                # 直前の操作が「戻して」だと写しの表示が生の値（0.805391）になっている。利用者に意味が無いので付けない。
                if "dB" in str(old_display) or "inf" in str(old_display):
                    line += self._m("info.from_value", value=old_display)
            if write_unknown:
                line += self._m("info.readback_recovered")
        except ValueError as error:
            return self._execution_failure(message_id, "error", self._error_text(error), intent, before, utterance, rewritten, jev_ms, llm_ms, bridge_ms, started)
        except WriteResultUnknown as error:
            return self._execution_failure(message_id, "unknown", self._error_text(error), intent, before, utterance, rewritten, jev_ms, llm_ms, bridge_ms, started)
        except BridgeError:
            self.live = False
            return self._execution_failure(message_id, "error", self._m("error.live"), intent, before, utterance, rewritten, jev_ms, llm_ms, bridge_ms, started)
        self.live = True
        self.previous = PreviousIntent(
            intent.action,
            intent.track,
            intent.param,
            self._value_before(before, intent),
            self._value_before(self.snapshot, intent),
            intent.step,
            confirmed,
            clip=intent.clip,
            scene=intent.scene,
            send=intent.send,
            device=intent.device,
        )
        return {
            "id": message_id,
            "kind": "result",
            "line": line,
            "decision": self._decision_details(intent, before, self.snapshot, utterance, rewritten),
            "ms": self._ms(started, jev_ms, llm_ms, bridge_ms),
        }

    def _snapshot_is_stale(self) -> bool:
        """写しが古くなっていないかを1命令（装置一覧・約100ms）で確かめる。最後の写し取り/確認から10秒以内なら見ない。
        別の曲を開いた・トラックを手で足した/消した/改名した・装置を手で足した、を拾うため。"""
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
            (str((item.get("track") or {}).get("name", "")), len(item.get("devices") or []))
            for item in tracks if isinstance(item, Mapping)
        ]
        mine = [(track.name, len(track.devices)) for track in self.snapshot.tracks]
        if live == mine:
            self.snapshot = replace(self.snapshot, taken_at=time.time())
            return False
        return True

    def _confirm_track_name(self, intent: Intent) -> int:
        if not isinstance(intent.track, int):
            return 0
        assert self.snapshot is not None
        track = next(item for item in self.snapshot.tracks if item.index == intent.track)
        name_id = request_id("name")
        result = self.bridge.run(["--api-get", track.path, "name", name_id])
        name = _find(result, name_id).payload
        if str(name) != track.name:
            raise StaleSnapshot()
        return result.elapsed_ms

    @staticmethod
    def _expected_batch_value(arguments: list[str]) -> float | bool | None:
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
        """書き込み直後は Live 側の反映前の値が返ることがあるので、期待値になるまで最大350ms読み直す。"""
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
        if spec.kind == "track_bool" and isinstance(intent.track, int) and spec.prop:
            track = next((item for item in snapshot.tracks if item.index == intent.track), None)
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
            if intent.track == "master":
                return snapshot.master_display
            track = next((item for item in snapshot.tracks if item.index == intent.track), None)
            return track.volume_display if track else None
        if intent.action is Action.PAN:
            track = next((item for item in snapshot.tracks if item.index == intent.track), None)
            return track.pan_display if track else None
        if intent.action in {Action.MUTE, Action.UNMUTE}:
            track = next((item for item in snapshot.tracks if item.index == intent.track), None)
            return self._m("state.on" if track and track.mute else "state.off")
        if intent.action in {Action.SOLO, Action.UNSOLO}:
            track = next((item for item in snapshot.tracks if item.index == intent.track), None)
            return self._m("state.on" if track and track.solo else "state.off")
        if intent.action is Action.TEMPO:
            return f"{snapshot.tempo:g} BPM"
        if intent.action in {Action.PLAY, Action.STOP}:
            return self._m("state.playing" if snapshot.playing else "state.stopped")
        if ACTIONS[intent.action].kind == "param" and intent.param is not None:
            for track in snapshot.tracks:
                for device in track.devices:
                    for parameter in device.params:
                        if parameter.path == intent.param.path:
                            return parameter.display
        return None

    def _track_label(self, snapshot: Snapshot, intent: Intent) -> str | None:
        if intent.track == "master":
            return self._m("label.master")
        track = next((item for item in snapshot.tracks if item.index == intent.track), None)
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

    def _set_volume_db(self, intent: Intent) -> tuple[Snapshot, int, bool]:
        assert self.snapshot is not None and intent.number is not None
        if intent.track == "master":
            path, track_ref = "live_set master_track mixer_device volume", "master"
            current_display = self.snapshot.master_display
        else:
            track = next(item for item in self.snapshot.tracks if item.index == intent.track)
            path, track_ref = f"{track.path} mixer_device volume", str(track.index)
            current_display = track.volume_display
        target = relative_db_target(intent.number.value, intent.step, current_display)
        low, high = 0.0, 1.0
        elapsed = 0
        attempts: list[tuple[float, float, str]] = []
        latest_midpoint: float | None = None
        max_attempts = 12
        for _ in range(max_attempts):
            midpoint = (low + high) / 2.0
            latest_midpoint = midpoint
            written = self.bridge.run([
                "--write", "--api-parameter-set", path, json.dumps(midpoint), request_id("set"),
            ])
            elapsed += written.elapsed_ms
            if written.timed_out:
                refreshed, read_ms = self._refresh_target(intent)
                return refreshed, elapsed + read_ms, True
            display_id = request_id("display")
            displayed = self.bridge.run([
                "--write", "--api-call", path, "str_for_value", json.dumps([midpoint]), display_id,
            ])
            elapsed += displayed.elapsed_ms
            if displayed.timed_out:
                refreshed, read_ms = self._refresh_target(intent)
                return refreshed, elapsed + read_ms, False
            display_ack = ack_map(displayed).get(display_id)
            shown = display_ack.payload if display_ack else None
            if isinstance(shown, list) and shown:
                shown = shown[-1]
            display = str(shown or "")
            match = re.search(r"-?\d+(?:\.\d+)?", display)
            if not match:
                break
            measured = float(match.group())
            error = abs(measured - target)
            attempts.append((error, midpoint, display))
            if error <= 0.05:
                break
            if measured < target:
                low = midpoint
            else:
                high = midpoint
        if not attempts:
            raise BridgeError("音量を書き込めません")
        _error, best_value, best_display = min(attempts, key=lambda item: item[0])
        if len(attempts) == max_attempts or latest_midpoint != best_value:
            final_write = self.bridge.run([
                "--write", "--api-parameter-set", path, json.dumps(best_value), request_id("set"),
            ])
            elapsed += final_write.elapsed_ms
            if final_write.timed_out:
                refreshed, read_ms = self._refresh_target(intent)
                return refreshed, elapsed + read_ms, True
        mixer = self.bridge.run(["--api-mixer-status", track_ref, request_id("mixer")])
        elapsed += mixer.elapsed_ms
        _require_readback(mixer, "api_mixer_status")
        updated = self._update_from_result(self.snapshot, intent, mixer)
        if intent.track == "master":
            updated = replace(updated, master_display=best_display)
        else:
            updated = replace_track(updated, int(intent.track), volume_display=best_display)
        return updated, elapsed, False

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
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

    def _refresh_clip_prop(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        prop = ACTIONS[intent.action].prop or "looping"
        track = next(item for item in self.snapshot.tracks if item.index == intent.track)
        clip = next(item for item in track.clips if item.slot == intent.clip)
        result = self.bridge.run(["--api-get", clip.path, prop, request_id(prop)])
        _require_readback(result, "api_get", prop)
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

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
    }

    def _run_plugin_flow(self, intent: Intent, before: Snapshot) -> int:
        """既存トラックへ挿す（plugin）か、トラックを足して挿す（plugin_track）。どちらも Live の中の部品が Live と同じ作法で行う:
        新しいトラックは選択中のトラックの右・既定名のまま（音源を入れると Live がその名前に変える）、
        エフェクトは選択中の装置の後ろ、音源は既存の音源と入れ替え。部品の返事に装置名があれば、その場で成功とする。"""
        started = time.perf_counter()
        plugin = str(intent.plugin)
        try:
            if ACTIONS[intent.action].kind == "plugin_track":
                audio = intent.text == "audio"
                name = None if audio else (intent.text or None)
                answer = plugin_script.add_track("audio" if audio else "midi", name, plugin)
            else:
                answer = plugin_script.load(plugin, int(intent.track))
        except plugin_script.ScriptError as error:
            if "main_thread_timeout" not in str(error):
                raise ValueError(self.SCRIPT_LOAD_ERRORS.get(str(error), str(error))) from error
            answer = {}  # 重い音源は読み込みに時間がかかるだけ。装置が現れるまで下の読み直しで待つ。
        track_index = answer.get("track_index")
        loaded = [str(name) for name in answer.get("devices_after") or []]
        deadline = time.monotonic() + PLUGIN_LOAD_WAIT_SECONDS
        while True:
            try:
                self.snapshot, _ = self.reader.read()
            except BridgeError:
                if time.monotonic() >= deadline:
                    raise ValueError(f"{plugin} が載ったことを確認できませんでした（Liveが読み込み中かもしれません）")
                time.sleep(0.5)
                continue
            if any(plugin.casefold() in name.casefold() or name.casefold() in plugin.casefold() for name in loaded):
                break
            index = track_index if isinstance(track_index, int) else (int(intent.track) if isinstance(intent.track, int) else None)
            current = next((item for item in self.snapshot.tracks if item.index == index), None) if index is not None else None
            names = [device.name for device in current.devices] if current else []
            if any(plugin.casefold() in name.casefold() or name.casefold() in plugin.casefold() for name in names):
                break
            if time.monotonic() >= deadline:
                raise ValueError(f"{plugin} が載ったことを確認できませんでした")
            time.sleep(0.5)
        if isinstance(track_index, int):
            object.__setattr__(intent, "track", track_index)
        return round((time.perf_counter() - started) * 1000)

    def _run_add_track_via_script(self, intent: Intent) -> int:
        """トラック追加（と内蔵デバイス入り）を Live の中の部品で行う。位置と名前は Live の作法どおり。"""
        started = time.perf_counter()
        audio = intent.action is Action.ADD_AUDIO_TRACK or intent.text == "audio"
        name = None if intent.text == "audio" else (intent.text or None)
        device = str(intent.native_device) if intent.action is Action.ADD_TRACK_WITH_DEVICE else None
        try:
            plugin_script.add_track("audio" if audio else "midi", name, device)
        except plugin_script.ScriptError as error:
            raise ValueError(self.SCRIPT_LOAD_ERRORS.get(str(error), str(error))) from error
        self.snapshot, _ = self.reader.read()
        return round((time.perf_counter() - started) * 1000)

    def _rollback_added_track(self) -> None:
        """「トラックを足してからプラグイン」の途中で失敗したとき、足したトラックを Live の取り消しで消す。"""
        try:
            self.bridge.run(["--write", "--api-call", "live_set", "undo", "[]", request_id("undo")])
            self.snapshot, _ = self.reader.read()
        except Exception:
            pass

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
        if ack is None or not isinstance(intent.track, int):
            return snapshot
        raw = ack.payload[-1] if isinstance(ack.payload, list) and ack.payload else ack.payload
        return replace_track(snapshot, intent.track, name=str(raw))

    def _refresh_song_prop(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        prop = ACTIONS[intent.action].prop or "is_playing"
        result = self.bridge.run(["--api-get", "live_set", prop, request_id(prop)])
        _require_readback(result, "api_get", prop)
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

    def _refresh_before_restore(self, intent: Intent) -> tuple[Snapshot, int]:
        if intent.action not in {Action.PLAY, Action.STOP}:
            return self._refresh_target(intent)
        assert self.snapshot is not None
        playing_id = request_id("playing")
        result = self.bridge.run(["--api-get", "live_set", "is_playing", playing_id])
        _require_readback(result, "api_get", "is_playing")
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
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

    def _refresh_track_bool(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        track = next(item for item in self.snapshot.tracks if item.index == intent.track)
        prop = ACTIONS[intent.action].prop or "mute"
        result = self.bridge.run(["--api-get", track.path, prop, request_id(prop)])
        _require_readback(result, "api_get", prop)
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

    def _refresh_param(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None and intent.param is not None
        device_path = intent.param.path.rsplit(" parameters ", 1)[0]
        result = self.bridge.run(["--api-device-parameters", device_path, request_id("params")])
        _require_readback(result, "api_device_parameters")
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

    def _refresh_mixer(self, intent: Intent) -> tuple[Snapshot, int]:
        assert self.snapshot is not None
        target = "master" if intent.track == "master" else str(intent.track)
        result = self.bridge.run(["--api-mixer-status", target, request_id("mixer")])
        _require_readback(result, "api_mixer_status")
        return self._update_from_result(self.snapshot, intent, result), result.elapsed_ms

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
        return replace_track(snapshot, int(intent.track), **{prop: value})

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
        shown = str(display_value or f"{value:g}")
        if intent.track == "master":
            return replace(snapshot, master_volume=value, master_display=shown, taken_at=time.time())
        changes = {"volume": value, "volume_display": shown} if field == "volume" else {"pan": value, "pan_display": shown}
        return replace_track(snapshot, int(intent.track), **changes)

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
            try:
                message = json.loads(line)
                if not isinstance(message, Mapping):
                    raise ValueError
                response = service.process(message)
            except (ValueError, json.JSONDecodeError, UnicodeError):
                response = {"kind": "error", "line": render("error.json", lang=service.lang)}
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
