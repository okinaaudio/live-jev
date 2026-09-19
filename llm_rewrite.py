"""Use Gemini to rewrite an ambiguous phrase as an allowed command."""

from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from intent import load_aliases
from snapshot import Snapshot, is_bridge_track


MODEL = "gemini-3.5-flash"
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_OPENER = urllib.request.build_opener(_RejectRedirectHandler())


def build_prompt(snapshot: Snapshot, utterance: str) -> str:
    aliases = load_aliases()
    tracks = []
    for track in snapshot.tracks:
        if is_bridge_track(track):
            continue
        names = [track.name, *aliases.get(track.name, ())]
        devices = [device.name for device in track.devices]
        description = "・".join(names)
        if devices:
            description += "（デバイス: " + "・".join(devices) + "）"
        tracks.append(description)
    return (
        "あなたはAbleton Liveの操作の通訳です。次の一言を、下の語彙だけを使った短い日本語の命令文に言い換えてください。"
        f"トラック: {', '.join(tracks)}。"
        "操作: 音量を上げる/下げる、パンを左/右へ、ミュート/解除、ソロ/解除、テンポをNに、再生、停止、続きから再生、"
        "録音開始/停止、オーバーダブオン/オフ、ループオン/オフ、メトロノームオン/オフ、取り消し、やり直し、N小節へ、全クリップ停止、"
        "<トラック>を録音待機/解除、<トラック>のモニターをIn/Auto/Offに、<トラック>を折りたたむ/開く、<トラック>のクリップを止める、"
        "<デバイス名>の<つまみ名>を上げる/下げる。数値があれば残す。"
        "2つ以上の操作が含まれるなら1行に1つずつ改行で分ける。判断できなければ『不明』とだけ書く。"
        f"一言: {utterance}"
    )


class GeminiRewriter:
    def __init__(self, model: str = MODEL, timeout: float = 4.0) -> None:
        self.model = model
        self.timeout = timeout

    def __call__(self, snapshot: Snapshot, utterance: str, key: str) -> str:
        url = API_URL.format(urllib.parse.quote(self.model, safe=""))
        payload = {
            "contents": [{"role": "user", "parts": [{"text": build_prompt(snapshot, utterance)}]}],
            "generationConfig": {
                "maxOutputTokens": 120,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            method="POST",
        )
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                raw = response.read()
            decoded = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as error:
            status = error.code
            if status in {401, 403}:
                message = "Geminiの鍵が拒否されました（401/403）"
            elif status == 429:
                message = "Geminiの回数制限（429）"
            elif 500 <= status <= 599:
                message = "Gemini側の問題（5xx）"
            else:
                message = f"Geminiが受け付けません（{status}）"
            raise RuntimeError(message) from error
        except (
            urllib.error.URLError,
            socket.timeout,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            UnicodeError,
            json.JSONDecodeError,
        ) as error:
            raise RuntimeError("Geminiに繋がりません（通信）") from error
        candidates = decoded.get("candidates") if isinstance(decoded, dict) else None
        candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise RuntimeError("Geminiの応答を読めません")
        text = "".join(
            part["text"]
            for part in parts
            if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str)
        ).strip()
        if not text:
            raise RuntimeError("Geminiの応答を読めません")
        return text
