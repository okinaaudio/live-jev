"""既存の live.py を呼び、相関IDつき ACK を安全に取り出す。"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence
import uuid


PYTHON = "/opt/homebrew/bin/python3.13"
LIVE_PY = Path(__file__).resolve().parent.parent / "live.py"
UPSTREAM_PY = Path(__file__).resolve().parents[2] / "bridge" / "ableton_udp_bridge.py"
RAW_ACK = re.compile(r"^ack:\s+/ack(?:\s+(.*))?$")


def _load_module(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class _LazyModule:
    """旧来の UDP の橋渡し（codex-live-bridge）は任意の部品。使うときに初めて読み込み、無ければ分かる言葉で断る。
    ふだんの通信は Live の中の部品（Remote Script）なので、橋渡しが無い環境でもこのモジュールは import できる。"""

    def __init__(self, name: str, path: Path) -> None:
        self.__dict__["_name"] = name
        self.__dict__["_path"] = path
        self.__dict__["_module"] = None

    def __getattr__(self, attribute: str) -> Any:
        module = self.__dict__["_module"]
        if module is None:
            path = self.__dict__["_path"]
            if not path.exists():
                raise BridgeError("Liveに繋がりません（Live の設定でコントロールサーフェス LiveJev を有効にしてください）")
            module = _load_module(self.__dict__["_name"], path)
            self.__dict__["_module"] = module
        return getattr(module, attribute)


_UPSTREAM = _LazyModule("_live_jev_ableton_udp_bridge", UPSTREAM_PY)
_LIVE_WRAPPER = _LazyModule("_live_jev_live_wrapper", LIVE_PY)


@dataclass(frozen=True)
class Ack:
    event: str
    request_id: str | None
    payload: Any
    path: str | None = None
    property: str | None = None


@dataclass(frozen=True)
class BridgeResult:
    acks: tuple[Ack, ...]
    elapsed_ms: int
    returncode: int
    timed_out: bool


class BridgeError(RuntimeError):
    pass


_TRACK_PATH = re.compile(r"^live_set tracks (0|[1-9]\d*)$")
_MIXER_PARAMETER_PATH = re.compile(
    r"^(?:live_set tracks (?:0|[1-9]\d*) mixer_device (?:volume|panning)|live_set master_track mixer_device volume)$"
)
_DEVICE_PARAMETER_PATH = re.compile(
    r"^live_set .+ devices (?:0|[1-9]\d*) parameters (?:0|[1-9]\d*)$"
)
_DEVICE_PATH = re.compile(r"^live_set .+ devices (?:0|[1-9]\d*)$")
_SLOT_PATH = re.compile(r"^live_set tracks (?:0|[1-9]\d*) clip_slots (?:0|[1-9]\d*)$")
_CLIP_PATH = re.compile(r"^live_set tracks (?:0|[1-9]\d*) clip_slots (?:0|[1-9]\d*) clip$")
_SCENE_PATH = re.compile(r"^live_set scenes (?:0|[1-9]\d*)$")
_SEND_PATH = re.compile(r"^live_set tracks (?:0|[1-9]\d*) mixer_device sends (?:0|[1-9]\d*)$")
SLOT_GET_PROPS = frozenset({"has_clip", "is_playing", "is_triggered"})
CLIP_GET_PROPS = frozenset({"name", "is_playing", "is_triggered", "looping", "length", "warping", "pitch_coarse", "gain", "gain_display_string"})
CLIP_SET_PROPS = frozenset({"looping", "warping", "pitch_coarse", "gain"})
SLOT_CALL_METHODS = frozenset({"fire", "stop"})
SCENE_CALL_METHODS = frozenset({"fire"})


TRACK_GET_PROPS = frozenset({"name", "mute", "solo", "arm", "current_monitoring_state", "fold_state"})
SONG_GET_PROPS = frozenset({
    "tempo", "is_playing", "loop", "metronome", "session_record", "overdub",
    "current_song_time", "signature_numerator", "signature_denominator",
})
TRACK_SET_PROPS = frozenset({"mute", "solo", "arm", "current_monitoring_state", "fold_state"})
SONG_SET_PROPS = frozenset({"loop", "metronome", "session_record", "overdub", "current_song_time"})
SONG_CALL_METHODS = frozenset({
    "start_playing", "stop_playing", "continue_playing", "undo", "redo",
    "capture_midi", "tap_tempo", "stop_all_clips",
})
TRACK_CALL_METHODS = frozenset({"stop_all_clips"})
NATIVE_INSTRUMENTS = ("Analog", "Collision", "Drift", "Drum Rack", "Electric", "Impulse", "Instrument Rack", "Meld", "Operator", "Sampler", "Simpler", "Tension", "Wavetable")
NATIVE_AUDIO_EFFECTS = (
    "Amp", "Audio Effect Rack", "Auto Filter", "Auto Pan", "Beat Repeat", "Cabinet", "Channel EQ", "Chorus-Ensemble", "Compressor", "Corpus",
    "Delay", "Drum Buss", "Dynamic Tube", "Echo", "EQ Eight", "EQ Three", "Erosion", "Filter Delay", "Gate", "Glue Compressor",
    "Grain Delay", "Hybrid Reverb", "Limiter", "Looper", "Multiband Dynamics", "Overdrive", "Pedal", "Phaser-Flanger", "Redux",
    "Resonators", "Reverb", "Roar", "Saturator", "Shifter", "Spectral Resonator", "Spectral Time", "Spectrum", "Tuner", "Utility",
    "Vinyl Distortion", "Vocoder",
)
NATIVE_MIDI_EFFECTS = ("Arpeggiator", "Chord", "MIDI Effect Rack", "Note Length", "Pitch", "Random", "Scale", "Velocity")
NATIVE_DEVICES = frozenset(NATIVE_INSTRUMENTS + NATIVE_AUDIO_EFFECTS + NATIVE_MIDI_EFFECTS)


def _check_set_value(prop: str, value: str) -> None:
    if prop == "current_monitoring_state":
        if value not in {"0", "1", "2"}:
            raise ValueError("モニターの値は 0/1/2 です")
        return
    if prop == "current_song_time":
        try:
            if float(value) < 0:
                raise ValueError
        except ValueError as error:
            raise ValueError("再生位置は 0 以上の数値です") from error
        return
    if value not in {"0", "1"}:
        raise ValueError(f"{prop} の値は 0 か 1 です")


def _check_clip_value(prop: str, value: str) -> None:
    if prop in {"looping", "warping"}:
        if value not in {"0", "1"}:
            raise ValueError(f"{prop} の値は 0 か 1 です")
        return
    if prop == "pitch_coarse":
        try:
            pitch = int(value)
        except ValueError as error:
            raise ValueError("ピッチは整数（半音）です") from error
        if not -48 <= pitch <= 48:
            raise ValueError("ピッチは -48〜48 半音です")
        return
    try:
        gain = float(value)
    except ValueError as error:
        raise ValueError("ゲインは 0〜1 の数値です") from error
    if not 0.0 <= gain <= 1.0:
        raise ValueError("ゲインは 0〜1 の数値です")


def validate_arguments(arguments: Sequence[str]) -> None:
    arities = {
        "--api-insert-device": 4,
        "--rename-track-index": 1,
        "--rename-track-name": 1,
        "--add-midi-tracks": 1,
        "--midi-name": 1,
        "--add-audio-tracks": 1,
        "--audio-prefix": 1,
        "--api-session-context": 1,
        "--api-children": 3,
        "--api-get": 3,
        "--api-mixer-status": 2,
        "--api-device-list": 2,
        "--api-device-parameters": 2,
        "--api-parameter-set": 3,
        "--api-set": 4,
        "--api-call": 4,
        "--tempo": 1,
    }
    index = 0
    write_seen = False
    commands: list[tuple[str, tuple[str, ...]]] = []
    while index < len(arguments):
        flag = arguments[index]
        if flag == "--write":
            if write_seen:
                raise ValueError("--write は1回だけ指定できます")
            write_seen = True
            index += 1
            continue
        count = arities.get(flag)
        if count is None or index + count >= len(arguments) + 1:
            raise ValueError(f"許可されていない引数です: {flag}")
        values = tuple(arguments[index + 1:index + count + 1])
        if len(values) != count:
            raise ValueError(f"引数が足りません: {flag}")
        _validate_command(flag, values)
        commands.append((flag, values))
        index += count + 1
    protected = {"--api-parameter-set", "--api-set", "--api-call", "--tempo", "--api-insert-device", "--rename-track-index", "--rename-track-name", "--add-midi-tracks", "--add-audio-tracks", "--midi-name", "--audio-prefix"}
    flags_present = {flag for flag, _values in commands}
    if ("--rename-track-index" in flags_present) != ("--rename-track-name" in flags_present):
        raise ValueError("名前変更は index と name をそろえて指定してください")
    if "--midi-name" in flags_present and "--add-midi-tracks" not in flags_present:
        raise ValueError("--midi-name は --add-midi-tracks と一緒に指定してください")
    if "--audio-prefix" in flags_present and "--add-audio-tracks" not in flags_present:
        raise ValueError("--audio-prefix は --add-audio-tracks と一緒に指定してください")
    if any(flag in protected for flag, _values in commands) and not write_seen:
        raise ValueError("書き込み操作には --write が必要です")
    if write_seen:
        if not commands:
            raise ValueError("--write だけの呼び出しは許可されていません")
        read_flags = {
            "--api-session-context", "--api-children", "--api-get", "--api-mixer-status",
            "--api-device-list", "--api-device-parameters",
        }
        if any(flag in read_flags for flag, _values in commands):
            raise ValueError("書き込みと読み戻しは別の呼び出しにしてください")
        has_display = any(flag == "--api-call" and values[1] == "str_for_value" for flag, values in commands)
        has_change = any(
            flag in {"--api-parameter-set", "--api-set", "--tempo"}
            or (flag == "--api-call" and values[1] != "str_for_value")
            for flag, values in commands
        )
        if has_display and has_change:
            raise ValueError("変更と表示値の取得は別の呼び出しにしてください")


def _validate_command(flag: str, values: tuple[str, ...]) -> None:
    if flag == "--api-session-context":
        return
    if flag == "--api-children":
        parent, child = values[0], values[1]
        if (parent, child) in {("live_set", "tracks"), ("live_set", "scenes"), ("live_set", "return_tracks")}:
            return
        if _TRACK_PATH.fullmatch(parent) and child == "clip_slots":
            return
        raise ValueError("許可されていない children です")
    if flag == "--api-get":
        path, prop, _request_id = values
        if _SLOT_PATH.fullmatch(path):
            if prop not in SLOT_GET_PROPS:
                raise ValueError(f"スロットで取得できない property です: {prop}")
            return
        if _CLIP_PATH.fullmatch(path):
            if prop not in CLIP_GET_PROPS:
                raise ValueError(f"クリップで取得できない property です: {prop}")
            return
        if _SCENE_PATH.fullmatch(path):
            if prop != "name":
                raise ValueError("シーンは name だけ取得できます")
            return
        if _SEND_PATH.fullmatch(path):
            if prop != "value":
                raise ValueError("センドは value だけ取得できます")
            return
        if prop in TRACK_GET_PROPS:
            if not _TRACK_PATH.fullmatch(path):
                raise ValueError("トラック以外の property は取得できません")
            return
        if prop in SONG_GET_PROPS:
            if path != "live_set":
                raise ValueError("曲以外の song property は取得できません")
            return
        raise ValueError(f"取得できない property です: {prop}")
    if flag == "--api-mixer-status":
        target = values[0]
        if target != "master" and not target.isdigit():
            raise ValueError("mixer-status の対象が不正です")
        return
    if flag == "--api-device-list":
        target = values[0]
        if target != "all" and not target.isdigit():
            raise ValueError("device-list の対象が不正です")
        return
    if flag == "--api-device-parameters":
        if not _DEVICE_PATH.fullmatch(values[0]):
            raise ValueError("device-parameters の path が不正です")
        return
    if flag == "--api-insert-device":
        path, name, position, _request_id = values
        if not _TRACK_PATH.fullmatch(path):
            raise ValueError("デバイスを挿せるのはトラックだけです")
        if name not in NATIVE_DEVICES:
            raise ValueError(f"内蔵デバイスの名前ではありません: {name}")
        if position not in {""} and not position.isdigit():
            raise ValueError("挿入位置は空か整数です")
        return
    if flag in {"--rename-track-index", "--add-midi-tracks", "--add-audio-tracks"}:
        if not values[0].isdigit():
            raise ValueError(f"{flag} は整数で指定してください")
        if flag != "--rename-track-index" and values[0] != "1":
            raise ValueError("トラックの追加は1本ずつです")
        return
    if flag in {"--rename-track-name", "--midi-name", "--audio-prefix"}:
        name = values[0]
        if not name.strip() or len(name) > 64 or any(ord(ch) < 32 for ch in name):
            raise ValueError("名前は1〜64文字の通常の文字にしてください")
        return
    if flag == "--api-parameter-set":
        if not (_MIXER_PARAMETER_PATH.fullmatch(values[0]) or _DEVICE_PARAMETER_PATH.fullmatch(values[0]) or _SEND_PATH.fullmatch(values[0])):
            raise ValueError("parameter-set の path が不正です")
        return
    if flag == "--api-set":
        path, prop, value, _request_id = values
        if _CLIP_PATH.fullmatch(path) and prop in CLIP_SET_PROPS:
            _check_clip_value(prop, value)
            return
        if _TRACK_PATH.fullmatch(path) and prop in TRACK_SET_PROPS:
            _check_set_value(prop, value)
            return
        if path == "live_set" and prop in SONG_SET_PROPS:
            _check_set_value(prop, value)
            return
        raise ValueError("許可されていない set です")
    if flag == "--api-call":
        path, method, args, _request_id = values
        song_call = path == "live_set" and method in SONG_CALL_METHODS
        track_call = bool(_TRACK_PATH.fullmatch(path)) and method in TRACK_CALL_METHODS
        slot_call = bool(_SLOT_PATH.fullmatch(path)) and method in SLOT_CALL_METHODS
        scene_call = bool(_SCENE_PATH.fullmatch(path)) and method in SCENE_CALL_METHODS
        display = method == "str_for_value" and _MIXER_PARAMETER_PATH.fullmatch(path)
        if not (song_call or track_call or slot_call or scene_call or display):
            raise ValueError("許可されていない call です")
        if (song_call or track_call or slot_call or scene_call) and args != "[]":
            raise ValueError("call の引数は空にしてください")
        return
    if flag == "--tempo":
        try:
            float(values[0])
        except ValueError as error:
            raise ValueError("tempo は数値で指定してください") from error


def _json_prefix(text: str) -> tuple[Any, str]:
    stripped = text.lstrip()
    value, end = json.JSONDecoder().raw_decode(stripped)
    return value, stripped[end:].strip()


def _consume_path(body: str, expected_path: str, next_field: str | None = None) -> str:
    if next_field is None:
        boundary = r"(?P<rest>(?:[\[{\"]|true\b|false\b|null\b).*)"
    else:
        boundary = rf"{re.escape(next_field)}\s+(?P<rest>.*)"
    match = re.match(
        rf'^(?:"(?P<quoted>(?:\\.|[^"\\])*)"|(?P<plain>.*?))\s+{boundary}$',
        body,
    )
    if match is None:
        raise ValueError("ACK path is missing")
    if match.group("quoted") is not None:
        path = json.loads(f'"{match.group("quoted")}"')
    else:
        path = match.group("plain").strip()
    if path != expected_path:
        raise ValueError("ACK path mismatch")
    return match.group("rest")


def _api_get_value(payload: Any) -> Any:
    if isinstance(payload, list) and len(payload) == 1:
        return payload[0]
    return payload


def _expected(arguments: Sequence[str]) -> dict[str, tuple[str, tuple[str, ...]]]:
    arities = {
        "--api-session-context": ("api_session_context", 1),
        "--api-children": ("api_children", 3),
        "--api-get": ("api_get", 3),
        "--api-call": ("api_call", 4),
        "--api-device-list": ("api_device_list", 2),
        "--api-device-parameters": ("api_device_parameters", 2),
        "--api-parameter-set": ("api_parameter_set", 3),
        "--api-mixer-status": ("api_mixer_status", 2),
        "--api-set": ("api_set", 4),
    }
    result: dict[str, tuple[str, tuple[str, ...]]] = {}
    index = 0
    while index < len(arguments):
        flag = arguments[index]
        definition = arities.get(flag)
        if definition is None:
            index += 1
            continue
        event, count = definition
        parts = tuple(arguments[index + 1:index + 1 + count])
        if len(parts) == count:
            result[parts[-1]] = (event, parts[:-1])
        index += count + 1
    return result


def _parse_known(event: str, body: str, fields: tuple[str, ...], request_id: str) -> Ack:
    path = fields[0] if fields and event not in {"api_session_context", "api_device_list"} else None
    prop = fields[1] if len(fields) > 1 and event in {"api_get", "api_set", "api_call", "api_children"} else None
    remainder = body
    if event == "api_mixer_status":
        requested = fields[0]
        path = "live_set master_track" if requested == "master" else f"live_set tracks {requested}" if requested.isdigit() else requested
        remainder = _consume_path(remainder, path)
    elif event in {"api_get", "api_set", "api_call", "api_children"}:
        remainder = _consume_path(remainder, fields[0], fields[1])
    elif event in {"api_device_parameters", "api_parameter_set"}:
        remainder = _consume_path(remainder, fields[0])
    elif event == "api_device_list":
        prefix = fields[0] + " "
        if not remainder.startswith(prefix):
            raise ValueError("ACK prefix mismatch")
        remainder = remainder[len(prefix):]
    payload, suffix = _json_prefix(remainder)
    if suffix != request_id:
        raise ValueError("ACK correlation mismatch")
    if event == "api_get":
        payload = _api_get_value(payload)
    return Ack(event, request_id, payload, path, prop)


def parse_ack_output(stdout: str, arguments: Sequence[str] = ()) -> tuple[Ack, ...]:
    expected = _expected(arguments)
    parsed: list[Ack] = []
    for line in stdout.splitlines():
        match = RAW_ACK.match(line)
        if not match or not match.group(1):
            continue
        raw = match.group(1)
        if raw == "pong":
            parsed.append(Ack("pong", None, None))
            continue
        event, separator, body = raw.partition(" ")
        if not separator:
            continue
        candidates = [(request_id, definition) for request_id, definition in expected.items() if definition[0] == event and body.endswith(" " + request_id)]
        if not candidates:
            continue
        request_id, (_, fields) = max(candidates, key=lambda item: len(item[0]))
        try:
            parsed.append(_parse_known(event, body, fields, request_id))
        except (ValueError, json.JSONDecodeError):
            continue
    return tuple(parsed)


class BridgeClient:
    def __init__(self, *, timeout: float = 8.0, verbose: bool = False) -> None:
        self.timeout = timeout
        self.verbose = verbose

    def run(self, arguments: Sequence[str]) -> BridgeResult:
        validate_arguments(arguments)
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [PYTHON, str(LIVE_PY), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            elapsed = round((time.perf_counter() - started) * 1000)
            return BridgeResult((), elapsed, -1, True)
        except OSError as error:
            raise BridgeError("Liveに繋がりません") from error
        elapsed = round((time.perf_counter() - started) * 1000)
        timed_out = "no acknowledgement" in completed.stderr.lower()
        if completed.returncode != 0 and not timed_out:
            raise BridgeError("Liveに繋がりません")
        acks = parse_ack_output(completed.stdout, arguments)
        if self.verbose:
            events = ",".join(ack.event for ack in acks) or "none"
            print(
                f"[bridge] exit={completed.returncode} ack={len(acks)} events={events} ms={elapsed}",
                file=__import__("sys").stderr,
            )
        return BridgeResult(acks, elapsed, completed.returncode, timed_out)

    def ping(self) -> bool:
        try:
            result = self.run(())
        except BridgeError:
            return False
        return any(ack.event == "pong" for ack in result.acks)


class PersistentBridgeClient:
    """Daemon-lifetime UDP client with a subprocess fallback for bind failure."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        command_port: int = 9000,
        ack_port: int = 9001,
        timeout: float = 0.6,
        min_send_interval: float = 0.010,
        token_loader: Callable[[], str] | None = None,
        fallback: Any = None,
        verbose: bool = False,
    ) -> None:
        self.host = host
        self.command_port = command_port
        self.timeout = timeout
        self.min_send_interval = min_send_interval
        self.verbose = verbose
        self.startup_error: str | None = None
        self._fallback = fallback
        self._socket: socket.socket | None = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._lock = threading.Lock()
        self._last_send = 0.0
        self._token: str | None = None
        self._token_unavailable = False
        try:
            self._socket.bind((host, ack_port))
        except OSError:
            self._socket.close()
            self._socket = None
            self._fallback = fallback or BridgeClient(timeout=max(8.0, timeout), verbose=verbose)
            self.startup_error = (
                f"常設の通信口 {host}:{ack_port} を開けません。live.py経由に切り替えます"
            )
            self.ack_port = ack_port
            return
        self.ack_port = int(self._socket.getsockname()[1])
        loader = token_loader or _LIVE_WRAPPER.keychain_token
        try:
            self._token = loader()
        except RuntimeError:
            self._token_unavailable = True

    def close(self) -> None:
        with self._lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None
        close = getattr(self._fallback, "close", None)
        if callable(close):
            close()

    def ping(self) -> bool:
        validate_arguments(())
        if self._fallback is not None:
            return bool(self._fallback.ping())
        command = _UPSTREAM.OscCommand("/api/ping", (f"ping-{uuid.uuid4().hex}",))
        try:
            result = self._exchange((command,))
        except BridgeError:
            return False
        return any(ack.event == "pong" for ack in result.acks)

    def run(self, arguments: Sequence[str]) -> BridgeResult:
        validate_arguments(arguments)
        if self._fallback is not None:
            return self._fallback.run(arguments)
        if not arguments:
            started = time.perf_counter()
            ok = self.ping()
            elapsed = round((time.perf_counter() - started) * 1000)
            return BridgeResult((Ack("pong", None, None),) if ok else (), elapsed, 0 if ok else -1, not ok)
        commands = self._build_commands(arguments)
        return self._exchange(commands)

    def _build_commands(self, arguments: Sequence[str]) -> tuple[Any, ...]:
        prepared, write = _LIVE_WRAPPER.prepare(arguments)
        if write and self._token_unavailable:
            raise BridgeError("書き込みの鍵を取得できません")
        configured = [
            *prepared,
            "--host", self.host,
            "--port", str(self.command_port),
            "--ack-port", str(self.ack_port),
            "--ack-timeout", str(self.timeout),
            "--no-ping-first",
        ]
        if write:
            configured.extend(["--auth-token", self._token])
        try:
            config = _UPSTREAM.parse_args(configured)
            commands = tuple(_UPSTREAM.build_commands(config))
        except (SystemExit, TypeError, ValueError) as error:
            raise BridgeError("Liveへの命令を作れません") from error
        if not commands:
            raise BridgeError("Liveへの命令がありません")
        return commands

    def _exchange(self, commands: Sequence[Any]) -> BridgeResult:
        started = time.perf_counter()
        acks: list[Ack] = []
        timed_out = False
        with self._lock:
            if self._socket is None:
                raise BridgeError("Liveに繋がりません")
            for command in commands:
                self._wait_for_send_slot()
                self._drain()
                try:
                    packet = _UPSTREAM.encode_osc_message(command.address, command.args)
                    self._socket.sendto(packet, (self.host, self.command_port))
                except OSError as error:
                    raise BridgeError("Liveに繋がりません") from error
                self._last_send = time.monotonic()
                ack = self._receive(command, self._last_send + self.timeout)
                if ack is None:
                    timed_out = True
                    break
                acks.append(ack)
        elapsed = round((time.perf_counter() - started) * 1000)
        if self.verbose:
            events = ",".join(ack.event for ack in acks) or "none"
            print(
                f"[bridge] udp ack={len(acks)} events={events} ms={elapsed}",
                file=sys.stderr,
            )
        return BridgeResult(tuple(acks), elapsed, -1 if timed_out else 0, timed_out)

    def _wait_for_send_slot(self) -> None:
        remaining = self.min_send_interval - (time.monotonic() - self._last_send)
        if remaining > 0:
            time.sleep(remaining)

    def _drain(self) -> None:
        assert self._socket is not None
        self._socket.setblocking(False)
        try:
            while True:
                self._socket.recvfrom(65_507)
        except BlockingIOError:
            pass

    def _receive(self, command: Any, deadline: float) -> Ack | None:
        assert self._socket is not None
        expected_event, expected_request = _UPSTREAM._command_ack_expectation(command)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._socket.settimeout(remaining)
            try:
                packet, sender = self._socket.recvfrom(65_507)
            except socket.timeout:
                return None
            except OSError as error:
                raise BridgeError("Liveに繋がりません") from error
            if not isinstance(sender, tuple) or not sender or sender[0] != self.host:
                continue
            try:
                address, args = _UPSTREAM.decode_osc_message(packet)
                event = _UPSTREAM.parse_ack_event(address, args)
            except (TypeError, ValueError):
                continue
            if address != "/ack":
                continue
            if event.is_error:
                if event.request_id == expected_request:
                    raise BridgeError("Liveが操作を拒否しました")
                continue
            if event.event != expected_event or event.request_id != expected_request:
                continue
            try:
                _UPSTREAM.validate_command_acks(command, [(address, args)])
            except _UPSTREAM.BridgeAcknowledgementError:
                continue
            return self._to_ack(event, args)

    @staticmethod
    def _to_ack(event: Any, args: Sequence[Any]) -> Ack:
        payload_keys = {
            "api_get": "value",
            "api_set": "result",
            "api_call": "result",
            "api_children": "children",
            "api_session_context": "context",
            "api_device_list": "devices",
            "api_device_parameters": "parameters",
            "api_parameter_set": "parameter",
            "api_mixer_status": "mixer",
        }
        key = payload_keys.get(event.event)
        payload = event.payload.get(key) if key is not None else None
        if event.event == "api_get":
            payload = _api_get_value(payload)
        if event.event in {"tempo", "sig_num", "sig_den"} and len(args) > 1:
            payload = args[1]
        path_keys = {
            "api_get": "path",
            "api_set": "path",
            "api_call": "path",
            "api_children": "path",
            "api_device_parameters": "device_path",
            "api_parameter_set": "parameter_path",
            "api_mixer_status": "track_path",
        }
        property_keys = {
            "api_get": "property",
            "api_set": "property",
            "api_call": "method",
            "api_children": "child_name",
        }
        path = event.payload.get(path_keys[event.event]) if event.event in path_keys else None
        prop = event.payload.get(property_keys[event.event]) if event.event in property_keys else None
        return Ack(
            str(event.event),
            event.request_id,
            payload,
            str(path) if path is not None else None,
            str(prop) if prop is not None else None,
        )


def ack_map(result: BridgeResult) -> Mapping[str, Ack]:
    return {ack.request_id: ack for ack in result.acks if ack.request_id is not None}


def make_bridge_client(
    transport: str | None = None,
    *,
    verbose: bool = False,
) -> Any:
    mode = (transport if transport is not None else os.environ.get("LIVE_JEV_TRANSPORT", "auto")).strip().lower() or "auto"
    if mode == "udp":
        return PersistentBridgeClient(verbose=verbose)
    from script_bridge_client import ScriptBridgeClient
    if mode == "script":
        return ScriptBridgeClient(verbose=verbose)
    if mode == "auto":
        script = ScriptBridgeClient(verbose=verbose)
        # 旧来の UDP の橋渡しが入っていない環境（公開版の通常の姿）では、Live がまだ起動していなくても
        # Remote Script の口を使い続ける（次の命令で繋ぎ直す）。
        if script.ping() or not UPSTREAM_PY.exists():
            return script
        script.close()
        return PersistentBridgeClient(verbose=verbose)
    raise ValueError("LIVE_JEV_TRANSPORT must be udp, script, or auto")
