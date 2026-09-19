"""Thin client for the LiveJev Remote Script running inside Live. Uses newline-delimited JSON over TCP 127.0.0.1:9140."""

from __future__ import annotations

import json
import socket
import sys
from typing import Any, Mapping

HOST = "127.0.0.1"
PORT = 9140


class ScriptError(RuntimeError):
    pass


def call(action: str, timeout: float = 20.0, **fields: Any) -> Mapping[str, Any]:
    payload = json.dumps({"action": action, **fields}, ensure_ascii=False).encode("utf-8") + b"\n"
    try:
        with socket.create_connection((HOST, PORT), timeout=timeout) as conn:
            conn.sendall(payload)
            data = b""
            while not data.endswith(b"\n") and len(data) < 4_000_000:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (OSError, socket.timeout) as error:
        raise ScriptError("LiveJevに繋がりません（Liveの設定で有効になっていますか）") from error
    try:
        decoded = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ScriptError("LiveJevの応答を読めません") from error
    if not isinstance(decoded, Mapping):
        raise ScriptError("LiveJevの応答を読めません")
    return decoded


def ping() -> bool:
    try:
        return bool(call("ping", timeout=2.0).get("ok"))
    except ScriptError:
        return False


def list_plugins(refresh: bool = False) -> list[dict[str, str]]:
    answer = call("list_plugins", timeout=60.0, refresh=refresh)
    if not answer.get("ok"):
        raise ScriptError(str(answer.get("error") or "list_failed"))
    items = answer.get("items")
    return [dict(item) for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def load(name: str, track_index: int | None, uri: str = "") -> Mapping[str, Any]:
    answer = call("load", timeout=70.0, name=name, uri=uri, track_index=track_index)
    if not answer.get("ok"):
        raise ScriptError(str(answer.get("error") or "load_failed"))
    return answer


def add_track(kind: str, name: str | None = None, device: str | None = None) -> Mapping[str, Any]:
    """Add a track to the right of the selected track, using Live's position and default name. Load device if provided."""
    answer = call("add_track", timeout=70.0, kind=kind, name=name, device=device)
    if not answer.get("ok"):
        raise ScriptError(str(answer.get("error") or "add_track_failed"))
    return answer


def clip_notes(op: str, track_index: int | None = None, slot_index: int | None = None, **fields: Any) -> Mapping[str, Any]:
    """Transform notes in the open clip or specified slot. Raise ScriptError on failure when the response contains 'error'."""
    answer = call("clip_notes", timeout=15.0, op=op, track_index=track_index, slot_index=slot_index, **fields)
    if not answer.get("ok"):
        raise ScriptError(str(answer.get("error") or "clip_notes_failed"))
    return answer


def version() -> str | None:
    try:
        return str(call("ping", timeout=2.0).get("version") or "") or None
    except ScriptError:
        return None


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"ping", "list", "load"}:
        print("使い方: plugin_script.py ping | list [--refresh] | load <名前> [トラック番号(0始まり)]", file=sys.stderr)
        return 2
    command = sys.argv[1]
    try:
        if command == "ping":
            print("pong" if ping() else "no answer")
            return 0
        if command == "list":
            items = list_plugins(refresh="--refresh" in sys.argv)
            for item in items:
                print(f"{item.get('section', ''):14s} {item.get('name', '')}")
            print(f"{len(items)} 件", file=sys.stderr)
            return 0
        track = int(sys.argv[3]) if len(sys.argv) > 3 else None
        print(json.dumps(load(sys.argv[2], track), ensure_ascii=False))
        return 0
    except ScriptError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
