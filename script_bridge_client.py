"""Adapt the persistent Remote Script connection to the existing bridge API."""

from __future__ import annotations

from dataclasses import dataclass
import json
import select
import socket
import sys
import threading
import time
from typing import Any, Mapping, Sequence

from bridge_client import Ack, BridgeError, BridgeResult, validate_arguments
from snapshot import Snapshot, snapshot_from_script


HOST = "127.0.0.1"
PORT = 9140


@dataclass(frozen=True)
class ScriptCommand:
    wire: Mapping[str, Any]
    event: str
    request_id: str | None = None
    path: str | None = None
    property: str | None = None


def _decode_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise BridgeError("Liveへの命令を作れません") from error


def translate_arguments(arguments: Sequence[str]) -> tuple[ScriptCommand, ...]:
    validate_arguments(arguments)
    arities = {
        "--api-get": 3,
        "--api-set": 4,
        "--api-call": 4,
        "--api-parameter-set": 3,
        "--api-session-context": 1,
        "--api-children": 3,
        "--api-device-list": 2,
        "--api-device-parameters": 2,
        "--api-mixer-status": 2,
        "--api-insert-device": 4,
        "--tempo": 1,
        "--add-midi-tracks": 1,
        "--midi-name": 1,
        "--add-audio-tracks": 1,
        "--audio-prefix": 1,
        "--rename-track-index": 1,
        "--rename-track-name": 1,
    }
    parsed: list[tuple[int, str, tuple[str, ...]]] = []
    index = 0
    while index < len(arguments):
        flag = arguments[index]
        if flag == "--write":
            index += 1
            continue
        count = arities[flag]
        parsed.append((index, flag, tuple(arguments[index + 1:index + count + 1])))
        index += count + 1

    values_by_flag = {flag: values for _position, flag, values in parsed}
    commands: list[tuple[int, ScriptCommand]] = []
    modifiers = {"--midi-name", "--audio-prefix", "--rename-track-index", "--rename-track-name"}
    for position, flag, values in parsed:
        if flag in modifiers:
            continue
        if flag == "--api-get":
            path, prop, request_id = values
            command = ScriptCommand({"op": "lom_get", "path": path, "prop": prop}, "api_get", request_id, path, prop)
        elif flag == "--api-set":
            path, prop, raw_value, request_id = values
            command = ScriptCommand({"op": "lom_set", "path": path, "prop": prop, "value": _decode_json(raw_value)}, "api_set", request_id, path, prop)
        elif flag == "--api-call":
            path, method, raw_args, request_id = values
            args = _decode_json(raw_args)
            if not isinstance(args, list):
                raise BridgeError("Liveへの命令を作れません")
            command = ScriptCommand({"op": "lom_call", "path": path, "method": method, "args": args}, "api_call", request_id, path, method)
        elif flag == "--api-parameter-set":
            path, raw_value, request_id = values
            command = ScriptCommand({"op": "param_set", "path": path, "value": _decode_json(raw_value)}, "api_parameter_set", request_id, path)
        elif flag == "--api-session-context":
            command = ScriptCommand({"op": "session_context"}, "api_session_context", values[0])
        elif flag == "--api-children":
            path, child, request_id = values
            command = ScriptCommand({"op": "children", "path": path, "child": child}, "api_children", request_id, path, child)
        elif flag == "--api-device-list":
            target, request_id = values
            command = ScriptCommand({"op": "device_list", "target": target}, "api_device_list", request_id)
        elif flag == "--api-device-parameters":
            path, request_id = values
            command = ScriptCommand({"op": "device_parameters", "path": path}, "api_device_parameters", request_id, path)
        elif flag == "--api-mixer-status":
            target, request_id = values
            path = "live_set master_track" if target == "master" else "live_set tracks " + target
            command = ScriptCommand({"op": "mixer_status", "target": target}, "api_mixer_status", request_id, path)
        elif flag == "--api-insert-device":
            path, name, insertion, request_id = values
            command = ScriptCommand({"op": "insert_device", "path": path, "name": name, "position": insertion}, "api_insert_device", request_id)
        elif flag == "--tempo":
            value = float(values[0])
            command = ScriptCommand({"op": "tempo", "value": value}, "tempo")
        elif flag == "--add-midi-tracks":
            name = values_by_flag.get("--midi-name", (None,))[0]
            command = ScriptCommand({"op": "add_track", "kind": "midi", "name": name}, "add_midi_tracks")
        elif flag == "--add-audio-tracks":
            name = values_by_flag.get("--audio-prefix", (None,))[0]
            command = ScriptCommand({"op": "add_track", "kind": "audio", "name": name}, "add_audio_tracks")
        else:
            raise BridgeError("Liveへの命令を作れません")
        commands.append((position, command))

    rename_index = values_by_flag.get("--rename-track-index")
    rename_name = values_by_flag.get("--rename-track-name")
    if rename_index is not None and rename_name is not None:
        positions = [position for position, flag, _values in parsed if flag in {"--rename-track-index", "--rename-track-name"}]
        commands.append((min(positions), ScriptCommand({
            "op": "rename_track", "index": int(rename_index[0]), "name": rename_name[0],
        }, "track_renamed")))
    return tuple(command for _position, command in sorted(commands, key=lambda item: item[0]))


def acks_from_results(commands: Sequence[ScriptCommand], results: Sequence[Mapping[str, Any]]) -> tuple[Ack, ...]:
    if len(commands) != len(results):
        raise BridgeError("Liveの応答を読めません")
    acks: list[Ack] = []
    for command, result in zip(commands, results):
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise BridgeError("Liveが操作を拒否しました")
        payload = result.get("value")
        if command.event in {"api_insert_device", "track_renamed", "add_midi_tracks", "add_audio_tracks"}:
            payload = None
        acks.append(Ack(command.event, command.request_id, payload, command.path, command.property))
    return tuple(acks)


class ScriptBridgeClient:
    def __init__(
        self,
        *,
        host: str = HOST,
        port: int = PORT,
        timeout: float = 0.6,
        verbose: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.startup_error: str | None = None
        self._socket: socket.socket | None = None
        self._buffer = b""
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._discard_connection()

    def ping(self) -> bool:
        started = time.perf_counter()
        with self._lock:
            try:
                response = self._request({"action": "ping"})
            except (BridgeError, TimeoutError):
                return False
        if self.verbose:
            elapsed = round((time.perf_counter() - started) * 1000)
            print(f"[bridge] script ping ms={elapsed}", file=sys.stderr)
        return response.get("ok") is True

    def run(self, arguments: Sequence[str]) -> BridgeResult:
        validate_arguments(arguments)
        if not arguments:
            started = time.perf_counter()
            ok = self.ping()
            elapsed = round((time.perf_counter() - started) * 1000)
            return BridgeResult((Ack("pong", None, None),) if ok else (), elapsed, 0 if ok else -1, not ok)
        commands = translate_arguments(arguments)
        request_id = __import__("uuid").uuid4().hex
        started = time.perf_counter()
        with self._lock:
            try:
                response = self._request({
                    "action": "bridge",
                    "request": request_id,
                    "ops": [dict(command.wire) for command in commands],
                })
            except TimeoutError:
                elapsed = round((time.perf_counter() - started) * 1000)
                return BridgeResult((), elapsed, -1, True)
        if response.get("ok") is not True or response.get("request") != request_id:
            raise BridgeError("Liveが操作を拒否しました")
        results = response.get("results")
        if not isinstance(results, list):
            raise BridgeError("Liveの応答を読めません")
        acks = acks_from_results(commands, results)
        elapsed = round((time.perf_counter() - started) * 1000)
        if self.verbose:
            events = ",".join(ack.event for ack in acks) or "none"
            print(f"[bridge] script ack={len(acks)} events={events} ms={elapsed}", file=sys.stderr)
        return BridgeResult(acks, elapsed, 0, False)

    def read_snapshot(self) -> tuple[Snapshot, int]:
        started = time.perf_counter()
        with self._lock:
            try:
                response = self._request({"action": "snapshot"})
            except TimeoutError as error:
                raise BridgeError("Liveに繋がりません") from error
        raw = response.get("snapshot")
        if response.get("ok") is not True or not isinstance(raw, Mapping):
            raise BridgeError("Liveの状態を読めません")
        try:
            snapshot = snapshot_from_script(raw)
        except (TypeError, ValueError) as error:
            raise BridgeError("Liveの状態を読めません") from error
        return snapshot, round((time.perf_counter() - started) * 1000)

    def _connect(self) -> None:
        try:
            connection = socket.create_connection((self.host, self.port), timeout=self.timeout)
            connection.settimeout(self.timeout)
        except (OSError, socket.timeout) as error:
            raise BridgeError("Liveに繋がりません") from error
        self._socket = connection
        self._buffer = b""

    def _discard_connection(self) -> None:
        connection = self._socket
        self._socket = None
        self._buffer = b""
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _discard_if_closed(self) -> None:
        if self._socket is None:
            return
        try:
            readable, _writable, _errors = select.select([self._socket], [], [], 0)
            if readable and self._socket.recv(1, socket.MSG_PEEK) == b"":
                self._discard_connection()
        except (OSError, ValueError):
            self._discard_connection()

    def _request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._discard_if_closed()
        if self._socket is None:
            self._connect()
        assert self._socket is not None
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self._socket.sendall(encoded)
            line = self._read_line()
        except socket.timeout as error:
            self._discard_connection()
            raise TimeoutError from error
        except OSError as error:
            self._discard_connection()
            raise BridgeError("Liveに繋がりません") from error
        try:
            decoded = json.loads(line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            self._discard_connection()
            raise BridgeError("Liveの応答を読めません") from error
        if not isinstance(decoded, Mapping):
            self._discard_connection()
            raise BridgeError("Liveの応答を読めません")
        return decoded

    def _read_line(self) -> bytes:
        assert self._socket is not None
        while b"\n" not in self._buffer:
            chunk = self._socket.recv(65536)
            if not chunk:
                self._discard_connection()
                raise OSError("connection closed")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line
