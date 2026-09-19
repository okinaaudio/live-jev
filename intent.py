"""Jev の質問と回答を扱う純粋関数。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Callable

from bridge_client import NATIVE_DEVICES
from snapshot import Param, Snapshot, is_bridge_track, Device, Track


class Action(Enum):
    VOLUME = "volume"
    PAN = "pan"
    MUTE = "mute"
    UNMUTE = "unmute"
    SOLO = "solo"
    UNSOLO = "unsolo"
    TEMPO = "tempo"
    PLAY = "play"
    STOP = "stop"
    PARAM = "param"
    CONTINUE = "continue"
    RECORD_ON = "record_on"
    RECORD_OFF = "record_off"
    OVERDUB_ON = "overdub_on"
    OVERDUB_OFF = "overdub_off"
    LOOP_ON = "loop_on"
    LOOP_OFF = "loop_off"
    METRONOME_ON = "metronome_on"
    METRONOME_OFF = "metronome_off"
    UNDO = "undo"
    REDO = "redo"
    CAPTURE_MIDI = "capture_midi"
    TAP_TEMPO = "tap_tempo"
    STOP_ALL_CLIPS = "stop_all_clips"
    JUMP_TO_BAR = "jump_to_bar"
    ARM = "arm"
    DISARM = "disarm"
    MONITOR_IN = "monitor_in"
    MONITOR_AUTO = "monitor_auto"
    MONITOR_OFF = "monitor_off"
    FOLD = "fold"
    UNFOLD = "unfold"
    TRACK_STOP_CLIPS = "track_stop_clips"
    LAUNCH_CLIP = "launch_clip"
    STOP_CLIP = "stop_clip"
    LAUNCH_SCENE = "launch_scene"
    DEVICE_ON = "device_on"
    DEVICE_OFF = "device_off"
    SEND = "send"
    RENAME = "rename"
    ADD_MIDI_TRACK = "add_midi_track"
    ADD_AUDIO_TRACK = "add_audio_track"
    CLIP_LOOP_ON = "clip_loop_on"
    CLIP_LOOP_OFF = "clip_loop_off"
    CLIP_WARP_ON = "clip_warp_on"
    CLIP_WARP_OFF = "clip_warp_off"
    CLIP_PITCH = "clip_pitch"
    CLIP_GAIN = "clip_gain"
    ADD_TRACK_WITH_DEVICE = "add_track_with_device"
    INSERT_PLUGIN = "insert_plugin"
    ADD_TRACK_WITH_PLUGIN = "add_track_with_plugin"
    NONE = "none"


class Step(Enum):
    UP_SMALL = "up_small"
    UP_BIG = "up_big"
    DOWN_SMALL = "down_small"
    DOWN_BIG = "down_big"
    SET = "set"
    NONE = "none"


@dataclass(frozen=True)
class Number:
    value: float
    unit: Literal["db", "percent", "bpm", "pan", "raw"]


@dataclass(frozen=True)
class Intent:
    action: Action
    action_conf: float
    track: int | None | Literal["master", "selected"]
    track_conf: float
    track_stated: float
    param: Param | None
    param_conf: float
    step: Step
    step_conf: float
    number: Number | None
    needs_generation: float
    compound: float = 0.0
    refers_previous: float = 0.0
    scene: int | None = None
    scene_conf: float = 0.0
    clip: int | None = None
    clip_conf: float = 0.0
    device: Device | None = None
    device_conf: float = 0.0
    send: int | None = None
    send_conf: float = 0.0
    text: str | None = None
    native_device: str | None = None
    native_device_conf: float = 0.0
    plugin: str | None = None


@dataclass(frozen=True)
class IntentResult:
    intent: Intent
    action_options: tuple[str, ...]
    track_options: tuple[str, ...]
    param_options: tuple[str, ...]
    scene_options: tuple[str, ...] = ()
    clip_options: tuple[str, ...] = ()
    device_options: tuple[str, ...] = ()
    send_options: tuple[str, ...] = ()


ACTION_CRITERIA = {
    "volume": "トラックやマスターの音量を上げ下げ・指定する（おんりょう・ボリューム・上げて・下げて）",
    "pan": "左右の定位を動かす",
    "mute": "トラックを消音する・鳴らさないようにする（消して・鳴らさないで）",
    "unmute": "消音を解除する",
    "solo": "そのトラックだけを聞く（ソロ・そろ・だけ聞かせて・だけ鳴らして）",
    "unsolo": "ソロを解除する",
    "tempo": "曲のテンポを変える（てんぽ・BPM）",
    "play": "再生を始める（再生・スタート・流して）",
    "stop": "再生を止める（止めて・ストップ）",
    "param": "エフェクトやシンセなど、デバイスのつまみを動かす（つまみ・ノブ・パラメータ）",
    "continue": "止めた位置から再生を続ける（続きから・続けて）",
    "record_on": "録音を始める（録音・レコーディング開始・REC）",
    "record_off": "録音を止める（録音停止・録音やめて）",
    "overdub_on": "オーバーダブ（重ね録り）をオンにする",
    "overdub_off": "オーバーダブを切る",
    "loop_on": "曲のループ再生をオンにする（ループして・繰り返して）",
    "loop_off": "曲のループ再生を切る（ループ解除・ループやめて）",
    "metronome_on": "メトロノーム（クリック）を鳴らす",
    "metronome_off": "メトロノームを止める",
    "undo": "直前の編集を取り消す（アンドゥ・元に戻す）",
    "redo": "取り消した編集をやり直す（リドゥ）",
    "capture_midi": "今弾いたMIDIをあとから取り込む（キャプチャ・今の取っといて）",
    "tap_tempo": "テンポをタップで刻む（タップテンポ）",
    "stop_all_clips": "すべてのクリップの再生を止める（全部止めて・全クリップ停止）",
    "jump_to_bar": "再生位置を指定の小節へ飛ばす（17小節へ・頭から）",
    "arm": "トラックを録音待機（アーム）にする",
    "disarm": "トラックの録音待機（アーム）を解除する",
    "monitor_in": "トラックのモニターを In にする（入力を常に聞く）",
    "monitor_auto": "トラックのモニターを Auto にする",
    "monitor_off": "トラックのモニターを Off にする",
    "fold": "グループトラックを折りたたむ（閉じる）",
    "unfold": "グループトラックを開く（展開する）",
    "track_stop_clips": "そのトラックで鳴っているクリップを止める",
    "launch_clip": "セッションのクリップを鳴らす・発射する（クリップ名やスロット番号を指して）",
    "stop_clip": "鳴っているクリップを止める（クリップ名やスロット番号を指して）",
    "launch_scene": "シーンを発射する（シーン名や番号、横一列をまとめて鳴らす）",
    "device_on": "エフェクトやシンセ（デバイス）をオンにする・有効にする",
    "device_off": "エフェクトやシンセ（デバイス）をオフにする・バイパスする",
    "send": "トラックのセンド量（リターンA/Bへ送る量）を上げ下げ・指定する",
    "rename": "トラックの名前を変える（名前を〜にして・改名）",
    "add_midi_track": "MIDIトラックを1本追加する（新しいMIDIトラック）",
    "add_audio_track": "オーディオトラックを1本追加する",
    "clip_loop_on": "クリップのループをオンにする（クリップを繰り返す）",
    "clip_loop_off": "クリップのループをオフにする",
    "clip_warp_on": "クリップのワープをオンにする（テンポに追従）",
    "clip_warp_off": "クリップのワープをオフにする",
    "clip_pitch": "クリップのピッチ（音の高さ）を半音単位で上げ下げ・指定する（トランスポーズ）",
    "clip_gain": "クリップのゲイン（クリップ単位の音量）を上げ下げ・指定する",
    "add_track_with_device": "新しいトラックを1本作って、Live内蔵のデバイス（シンセやエフェクト）を載せる（〜入りのトラック作って）",
    "insert_plugin": "既存のトラックに外部プラグイン（VST/AU）や内蔵デバイスを名前で挿す（〜に〜を挿して）",
    "add_track_with_plugin": "新しいトラックを作って外部プラグイン（VST/AU）を載せる",
    "none": "上のどれにも当てはまらない。クリップやノートの作成、雰囲気の変更、質問、雑談など",
}

_ACTION_CRITERIA_EN = {
    "volume": "Raise, lower, or set track or master volume",
    "pan": "Move a track left, right, or center",
    "mute": "Mute a track", "unmute": "Unmute a track", "solo": "Solo a track", "unsolo": "Unsolo a track",
    "tempo": "Change tempo or BPM", "play": "Start playback", "stop": "Stop playback",
    "param": "Change a device parameter", "continue": "Resume playback from the current position",
    "record_on": "Start session recording", "record_off": "Stop session recording",
    "overdub_on": "Turn overdub on", "overdub_off": "Turn overdub off",
    "loop_on": "Turn song loop on", "loop_off": "Turn song loop off",
    "metronome_on": "Turn the metronome or click on", "metronome_off": "Turn the metronome or click off",
    "undo": "Undo the last edit", "redo": "Redo the last undone edit", "capture_midi": "Capture recently played MIDI",
    "tap_tempo": "Tap the tempo", "stop_all_clips": "Stop every playing clip", "jump_to_bar": "Jump to a bar",
    "arm": "Arm a track for recording", "disarm": "Disarm a track",
    "monitor_in": "Set track monitoring to In", "monitor_auto": "Set track monitoring to Auto", "monitor_off": "Set track monitoring to Off",
    "fold": "Fold a group track", "unfold": "Unfold a group track", "track_stop_clips": "Stop clips on one track",
    "launch_clip": "Launch a Session clip", "stop_clip": "Stop a Session clip", "launch_scene": "Launch a Session scene",
    "device_on": "Enable a device", "device_off": "Disable or bypass a device", "send": "Change a track send level",
    "rename": "Rename a track", "add_midi_track": "Add one MIDI track", "add_audio_track": "Add one audio track",
    "clip_loop_on": "Turn clip looping on", "clip_loop_off": "Turn clip looping off",
    "clip_warp_on": "Turn clip warping on", "clip_warp_off": "Turn clip warping off",
    "clip_pitch": "Transpose a clip in semitones", "clip_gain": "Change clip gain",
    "add_track_with_device": "Add a track containing an Ableton device",
    "insert_plugin": "Insert a named plug-in or Ableton device on an existing track",
    "add_track_with_plugin": "Add a track containing an external plug-in",
    "none": "None of the above, including composition, questions, and conversation",
}
ACTION_CRITERIA = {key: f"{value} / {_ACTION_CRITERIA_EN[key]}" for key, value in ACTION_CRITERIA.items()}


def _bi(ja: str, en: str) -> str:
    return f"{ja} / {en}"

ALIASES_PATH = Path(__file__).with_name("aliases.json")


def load_aliases() -> dict[str, tuple[str, ...]]:
    try:
        raw = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(name): tuple(alias for alias in aliases if isinstance(alias, str))
        for name, aliases in raw.items()
        if isinstance(aliases, list)
    }

ACTION_LABELS = {
    "volume": "音量", "pan": "パン", "mute": "ミュート", "unmute": "ミュート解除",
    "solo": "ソロ", "unsolo": "ソロ解除", "tempo": "テンポ", "play": "再生",
    "stop": "停止", "param": "つまみ", "none": "該当なし",
    "continue": "続きから再生", "record_on": "録音開始", "record_off": "録音停止",
    "overdub_on": "オーバーダブ", "overdub_off": "オーバーダブ解除", "loop_on": "ループ", "loop_off": "ループ解除",
    "metronome_on": "メトロノーム", "metronome_off": "メトロノーム停止", "undo": "取り消し", "redo": "やり直し",
    "capture_midi": "MIDIキャプチャ", "tap_tempo": "タップテンポ", "stop_all_clips": "全クリップ停止",
    "jump_to_bar": "再生位置", "arm": "録音待機", "disarm": "録音待機解除", "monitor_in": "モニターIn",
    "monitor_auto": "モニターAuto", "monitor_off": "モニターOff", "fold": "折りたたみ", "unfold": "展開",
    "track_stop_clips": "トラックのクリップ停止",
    "launch_clip": "クリップ発射", "stop_clip": "クリップ停止", "launch_scene": "シーン発射",
    "device_on": "デバイスON", "device_off": "デバイスOFF",
    "send": "センド", "rename": "名前変更", "add_midi_track": "MIDIトラック追加", "add_audio_track": "オーディオトラック追加",
    "clip_loop_on": "クリップのループ", "clip_loop_off": "クリップのループ解除", "clip_warp_on": "クリップのワープ",
    "clip_warp_off": "クリップのワープ解除", "clip_pitch": "クリップのピッチ", "clip_gain": "クリップのゲイン",
    "add_track_with_device": "デバイス入りトラック追加", "insert_plugin": "プラグイン挿入", "add_track_with_plugin": "プラグイン入りトラック追加",
}

STEP_CRITERIA = {
    "up_small": "少し上げる・ちょっと上げる",
    "up_big": "大きく上げる・かなり上げる・ガッと上げる",
    "down_small": "少し下げる",
    "down_big": "大きく下げる",
    "set": "具体的な数値や位置を指定している（120に、-6dBに、真ん中に）",
    "none": "方向も大きさも言っていない、または関係ない操作",
}

_SIGN = r"(?:-|−|－|マイナス)?"
_NUMBER = rf"{_SIGN}\d+(?:\.\d+)?"
NUMBER_RE = re.compile(
    rf"(?<![0-9A-Za-z.])(?P<number>{_NUMBER})\s*(?P<unit>dB|デシベル|%|％|bpm|raw)?(?![0-9A-Za-z.])",
    re.IGNORECASE,
)
PAN_RE = re.compile(
    rf"(?<![0-9A-Za-z.])(?:(?P<side_before>左|右|L|R)\s*(?P<number_before>{_NUMBER})\s*(?P<unit_before>%|％)?|"
    rf"(?P<number_after>{_NUMBER})\s*(?P<unit_after>%|％)?\s*(?P<side_after>左|右|L|R))(?![0-9A-Za-z.])",
    re.IGNORECASE,
)
ALLOWED_UNITS = {
    Action.VOLUME: {"db", "raw"},
    Action.PAN: {"pan", "percent"},
    Action.TEMPO: {"bpm", "raw"},
    Action.PARAM: {"percent", "raw"},
    Action.JUMP_TO_BAR: {"raw"},
    Action.SEND: {"percent", "raw"},
    Action.CLIP_PITCH: {"raw"},
    Action.CLIP_GAIN: {"percent", "raw"},
}


@lru_cache(maxsize=8)
def candidate_params(snapshot: Snapshot) -> dict[int, dict[str, Param]]:
    result: dict[int, dict[str, Param]] = {}
    for track in snapshot.tracks:
        if is_bridge_track(track):
            continue
        candidates: dict[str, Param] = {}
        for device in track.devices:
            for param in device.params:
                if len(candidates) >= 250:
                    break
                candidates[f"d{device.index}p{param.index}"] = param
            if len(candidates) >= 250:
                break
        result[track.index] = candidates
    return result


def _param_label(snapshot: Snapshot, track_index: int, parameter: Param) -> str:
    track = next((item for item in snapshot.tracks if item.index == track_index), None)
    for device in track.devices if track else ():
        if parameter in device.params:
            return f"{device.name}: {parameter.name}"
    return parameter.name


def build_request(snapshot: Snapshot, utterance: str) -> dict[str, Any]:
    candidates_by_track = candidate_params(snapshot)
    aliases = load_aliases()
    state = {
        "utterance": utterance,
        "tracks": [
            {
                "i": track.index,
                "name": track.name,
                "volume": track.volume_display,
                "pan": track.pan,
                "mute": track.mute,
                "solo": track.solo,
            }
            for track in snapshot.tracks
        ],
        "tempo": snapshot.tempo,
        "playing": snapshot.playing,
        "scenes": [{"i": scene.index, "name": scene.name} for scene in snapshot.scenes],
    }
    track_criteria = {
        f"t{track.index}": (
            f"{track.name}（{track.index + 1}番目）。"
            f"デバイス: {', '.join(device.name for device in track.devices)}。"
            f"別名: {', '.join(aliases.get(track.name, ()))}"
        )
        for track in snapshot.tracks
        if not is_bridge_track(track)
    }
    track_criteria.update({"selected": "今Liveの画面で選択中のトラック（選択トラック・このトラック・今のトラック）", "master": "マスター（全体の音量）", "none": "トラックは指定されていない、またはトラックに関係ない操作"})
    questions: dict[str, Any] = {
        "action": {"type": "choice", "instructions": _bi("この一言がLiveに求めている操作を1つ選ぶ。数値の指定があっても操作の種類だけを選ぶ", "Choose the one Ableton Live action requested. Choose only the action type even when a value is given"), "criteria": ACTION_CRITERIA},
        "track": {"type": "choice", "instructions": _bi("一言が指しているトラックを選ぶ", "Choose the track named by the utterance"), "criteria": track_criteria},
        "step": {"type": "choice", "instructions": _bi("変化の方向と大きさ", "Choose the direction and size of the change"), "criteria": STEP_CRITERIA},
        "track_stated": {"type": "noul", "instructions": _bi("一言の中でトラック名や『マスター』『全体』が明示されているか", "Whether a track name, master, or the whole mix is explicitly stated")},
        "needs_generation": {"type": "noul", "instructions": _bi("新しいノート・クリップ・フレーズの作成、曲の雰囲気やジャンルの変更など、あらかじめ用意した選択肢では表せない自由な作業を求めているか", "Whether the request needs free-form generation not covered by the choices")},
        "compound": {"type": "noul", "instructions": _bi("一言に2つ以上の別々の操作が含まれているか", "Whether the utterance contains two or more separate actions")},
        "refers_previous": {"type": "noul", "instructions": _bi("直前の操作の続きや取り消し（もう少し・もっと・戻して）を指しているか", "Whether it refers to continuing or undoing the previous action")},
    }
    for track in snapshot.tracks:
        if is_bridge_track(track):
            continue
        params = candidates_by_track.get(track.index, {})
        if not params:
            continue
        criteria = {
            key: f"{_param_label(snapshot, track.index, param)}（今 {param.display}）"
            for key, param in params.items()
        }
        criteria["none"] = "このトラックのつまみは指していない"
        questions[f"param_t{track.index}"] = {"type": "choice", "instructions": _bi(f"トラック{track.name}の中で、一言が指しているつまみを選ぶ", f"Choose the parameter named on track {track.name}"), "criteria": criteria}
    questions["native_device"] = {
        "type": "choice",
        "instructions": _bi("一言が載せたいと言っているLive内蔵デバイス（シンセ・エフェクト）を選ぶ。言っていなければ none", "Choose the named Ableton device, or none if no device is named"),
        "criteria": {**{name: name for name in sorted(NATIVE_DEVICES)}, "none": "内蔵デバイスは指定されていない"},
    }
    if snapshot.returns:
        send_criteria = {f"send{index}": f"{chr(ord('A') + index)}（{name}）" for index, name in enumerate(snapshot.returns[:26])}
        send_criteria["none"] = "センドは指定されていない"
        questions["send"] = {"type": "choice", "instructions": _bi("一言が指しているセンド（リターンA/B…）を選ぶ", "Choose the send or return named by the utterance"), "criteria": send_criteria}
    if snapshot.scenes:
        scene_criteria = {f"s{scene.index}": f"{scene.name}（{scene.index + 1}番目のシーン）" for scene in snapshot.scenes[:250]}
        scene_criteria["none"] = "シーンは指定されていない"
        questions["scene"] = {"type": "choice", "instructions": _bi("一言が指しているシーン（横一列）を選ぶ", "Choose the Session scene named by the utterance"), "criteria": scene_criteria}
    for track in snapshot.tracks:
        if is_bridge_track(track):
            continue
        if track.clips:
            clip_criteria = {f"c{clip.slot}": f"{clip.name}（スロット{clip.slot + 1}）" for clip in track.clips[:250]}
            clip_criteria["none"] = "このトラックのクリップは指していない"
            questions[f"clip_t{track.index}"] = {"type": "choice", "instructions": _bi(f"トラック{track.name}の中で、一言が指しているクリップを選ぶ", f"Choose the clip named on track {track.name}"), "criteria": clip_criteria}
        if track.devices:
            device_criteria = {f"d{device.index}": f"{device.name}（{device.index + 1}番目のデバイス）" for device in track.devices[:250]}
            device_criteria["none"] = "このトラックのデバイスは指していない"
            questions[f"device_t{track.index}"] = {"type": "choice", "instructions": _bi(f"トラック{track.name}の中で、一言が指しているデバイス（エフェクトやシンセ）を選ぶ", f"Choose the device named on track {track.name}"), "criteria": device_criteria}
    return {"state": state, "model": "jev-latest", "questions": questions}


CLIP_ACTIONS = frozenset({
    Action.LAUNCH_CLIP, Action.STOP_CLIP, Action.CLIP_LOOP_ON, Action.CLIP_LOOP_OFF,
    Action.CLIP_WARP_ON, Action.CLIP_WARP_OFF, Action.CLIP_PITCH, Action.CLIP_GAIN,
})


def _pick_per_track(
    snapshot: Snapshot,
    answers: Mapping[str, Any],
    prefix: str,
    track: int | None | Literal["master"],
    track_conf: float,
    keys_for: Callable[[Track], dict[str, Any]],
) -> tuple[Any, float, int | None]:
    """トラックごとの頭（clip_t / device_t）から、選ばれた項目と、逆引きしたトラックを返す。"""
    if isinstance(track, int) and track_conf >= 0.6:
        indexes = [track]
    elif track is None or track_conf < 0.6:
        indexes = [item.index for item in snapshot.tracks if not is_bridge_track(item)]
    else:
        indexes = []
    best: tuple[float, int, Any] | None = None
    for index in indexes:
        item = next((candidate for candidate in snapshot.tracks if candidate.index == index), None)
        if item is None:
            continue
        keys = keys_for(item)
        name, confidence = _choice(answers.get(f"{prefix}{index}"))
        if name == "none" or name not in keys:
            continue
        if best is None or confidence > best[0]:
            best = (confidence, index, keys[name])
    if best is None:
        return None, 0.0, None
    return best[2], best[0], best[1]


def _numeric_value(text: str) -> float:
    normalized = re.sub(r"^(?:−|－|マイナス)", "-", text)
    return float(normalized)


def _without_names(snapshot: Snapshot, utterance: str) -> str:
    names = [track.name for track in snapshot.tracks]
    names.extend(device.name for track in snapshot.tracks for device in track.devices)
    names.extend(param.name for track in snapshot.tracks for device in track.devices for param in device.params)
    masked = utterance
    for name in sorted((name for name in names if name), key=len, reverse=True):
        masked = re.sub(re.escape(name), lambda match: " " * len(match.group()), masked, flags=re.IGNORECASE)
    return masked


def parse_number(utterance: str, action: Action) -> Number | None:
    utterance = re.sub(r"(?<!\w)ー(?=\d)", "-", utterance)
    allowed = ALLOWED_UNITS.get(action)
    if allowed is None:
        return None
    if action is Action.CLIP_PITCH:
        semitone = re.search(rf"(?P<n>{_NUMBER})\s*(?:半音|st|semitone)", utterance, re.IGNORECASE)
        if semitone:
            value = _numeric_value(semitone.group("n"))
            if re.search(r"下げ|さげ|落と", utterance) and value > 0:
                value = -value
            return Number(value, "raw")
        if re.search(r"(?:1|一)?オクターブ", utterance):
            return Number(-12.0 if re.search(r"下|さげ|落と", utterance) else 12.0, "raw")
        return None
    if action is Action.JUMP_TO_BAR:
        if re.search(r"頭から|最初から|冒頭", utterance):
            return Number(1.0, "raw")
        bar = re.search(r"(\d+)\s*小節", utterance)
        return Number(float(bar.group(1)), "raw") if bar else None
    if action is Action.PAN:
        if re.search(r"真ん中|センター", utterance):
            return Number(0.0, "pan")
        pan = PAN_RE.search(utterance)
        if pan:
            side = pan.group("side_before") or pan.group("side_after")
            number = pan.group("number_before") or pan.group("number_after")
            unit = pan.group("unit_before") or pan.group("unit_after")
            trailing = utterance[pan.end():]
            if re.match(r"\s*(?:dB|デシベル|bpm|raw)", trailing, re.IGNORECASE):
                return None
            value = _numeric_value(number)
            signed = -abs(value) if side.casefold() in {"左", "l"} else abs(value)
            return Number(signed, "percent" if unit else "pan")
    match = NUMBER_RE.search(utterance)
    if not match:
        return None
    value = _numeric_value(match.group("number"))
    unit_text = match.group("unit")
    normalized = (unit_text or "").lower()
    if normalized in {"db", "デシベル"}:
        unit = "db"
    elif normalized in {"%", "％"}:
        unit = "percent"
    elif normalized == "bpm":
        unit = "bpm"
    elif normalized == "raw":
        unit = "raw"
    else:
        unit = {Action.TEMPO: "bpm", Action.PAN: "pan", Action.VOLUME: "db", Action.PARAM: "percent", Action.JUMP_TO_BAR: "raw", Action.SEND: "percent", Action.CLIP_GAIN: "percent"}.get(action, "raw")
    return Number(value, unit) if unit in allowed else None


def _local_intent(
    action: Action,
    *,
    track: int | None = None,
    step: Step = Step.NONE,
    number: Number | None = None,
    refers_previous: float = 0.0,
    scene: int | None = None,
    clip: int | None = None,
    text: str | None = None,
    send: int | None = None,
    native_device: str | None = None,
    plugin: str | None = None,
) -> Intent:
    return Intent(
        action=action,
        action_conf=1.0,
        track=track,
        track_conf=1.0 if track is not None else 0.0,
        track_stated=1.0 if track is not None else 0.0,
        param=None,
        param_conf=0.0,
        step=step,
        step_conf=1.0 if step is not Step.NONE else 0.0,
        number=number,
        needs_generation=0.0,
        refers_previous=refers_previous,
        scene=scene,
        scene_conf=1.0 if scene is not None else 0.0,
        clip=clip,
        clip_conf=1.0 if clip is not None else 0.0,
        text=text,
        send=send,
        send_conf=1.0 if send is not None else 0.0,
        native_device=native_device,
        native_device_conf=1.0 if native_device is not None else 0.0,
        plugin=plugin,
    )


SELECTED_WORDS = re.compile(r"^(?:選択(?:中の|した)?トラック|今のトラック|このトラック|現在のトラック)$")


def _local_track(snapshot: Snapshot, text: str) -> "int | None | Literal['selected']":
    if SELECTED_WORDS.fullmatch(text.strip()):
        return "selected"
    numbered = re.fullmatch(
        r"(?:トラック\s*(\d+)|(\d+)\s*番目(?:のトラック)?|(\d+)\s*番トラック)",
        text.strip(),
        re.IGNORECASE,
    )
    if numbered:
        position = int(next(group for group in numbered.groups() if group is not None))
        track = next((item for item in snapshot.tracks if item.index == position - 1), None)
        return track.index if position > 0 and track is not None else None
    matches = [
        track for track in snapshot.tracks
        if not is_bridge_track(track) and track.name and track.name.casefold() == text.strip().casefold()
    ]
    return matches[0].index if len(matches) == 1 else None


DEVICE_ALIASES = {
    "オペレーター": "Operator", "ウェーブテーブル": "Wavetable", "シンプラー": "Simpler", "サンプラー": "Sampler",
    "ドラムラック": "Drum Rack", "ドリフト": "Drift", "コリジョン": "Collision", "テンション": "Tension", "メルド": "Meld",
    "インパルス": "Impulse", "ハイブリッドリバーブ": "Hybrid Reverb", "グルーコンプ": "Glue Compressor",
    "イーキューエイト": "EQ Eight", "ドラムバス": "Drum Buss", "ビートリピート": "Beat Repeat",
}
GENERIC_DEVICE_WORDS = {
    "リバーブ": ("reverb", "verb"), "コンプ": ("comp",), "コンプレッサー": ("comp",), "イコライザー": ("eq",), "eq": ("eq",),
    "ディレイ": ("delay",), "エコー": ("echo",), "コーラス": ("chorus",), "フェイザー": ("phaser",), "サチュレーター": ("satur",),
    "リミッター": ("limit",), "ゲート": ("gate",), "フィルター": ("filter",), "シンセ": ("synth",), "ピアノ": ("piano",),
    "reverb": ("reverb", "verb"), "compressor": ("comp",), "comp": ("comp",), "eq": ("eq",),
    "delay": ("delay",), "echo": ("echo",), "chorus": ("chorus",), "phaser": ("phaser",),
}


def resolve_native_device(text: str) -> str | None:
    """日本語や表記ゆれから内蔵デバイスの正式名を引く。見つからなければ None。"""
    key = text.strip().strip("「」\"'").casefold().replace(" ", "")
    for name in NATIVE_DEVICES:
        if key == name.casefold().replace(" ", ""):
            return name
    for alias, name in DEVICE_ALIASES.items():
        if key == alias.casefold().replace(" ", ""):
            return name
    return None


def _track_name_in(utterance: str) -> str | None:
    named = re.search(r"(?P<name>[^\s、。]+?)\s*(?:という|って)\s*(?:名前|名)\s*(?:で|の)", utterance)
    return named.group("name").strip() if named else None


LOCAL_TRANSPORT: tuple[tuple[str, Action], ...] = (
    (r"(?:続きから|続きから再生|続けて再生)", Action.CONTINUE),
    (r"録音\s*(?:停止|やめて|終了|オフ|止めて)", Action.RECORD_OFF),
    (r"(?:録音|レコーディング)\s*(?:開始|して|スタート|オン)?", Action.RECORD_ON),
    (r"オーバーダブ\s*(?:オフ|解除|切って)", Action.OVERDUB_OFF),
    (r"オーバーダブ\s*(?:オン|して)?", Action.OVERDUB_ON),
    (r"ループ\s*(?:オフ|解除|やめて|外して|切って)", Action.LOOP_OFF),
    (r"ループ\s*(?:オン|して|する)?", Action.LOOP_ON),
    (r"メトロノーム\s*(?:オフ|消して|切って|外して|止めて)", Action.METRONOME_OFF),
    (r"メトロノーム\s*(?:オン|つけて|入れて|鳴らして)?", Action.METRONOME_ON),
    (r"(?:アンドゥ|undo)", Action.UNDO),
    (r"(?:やり直し|リドゥ|redo)", Action.REDO),
    (r"(?:キャプチャ|今の(?:を)?取っといて|キャプチャーMIDI)", Action.CAPTURE_MIDI),
    (r"(?:全部止めて|全クリップ停止|クリップ(?:を)?全部止めて)", Action.STOP_ALL_CLIPS),
)


def _parse_local_ja(utterance: str, snapshot: Snapshot) -> Intent | None:
    """完全に形が決まった操作だけを Jev なしで解釈する。"""
    text = normalize_phrase(utterance)
    if re.fullmatch(r"再生(?:して)?", text):
        return _local_intent(Action.PLAY)
    if re.fullmatch(r"(?:停止(?:して)?|止めて)", text):
        return _local_intent(Action.STOP)
    if re.fullmatch(r"(?:戻して|元に戻して|取り消し(?:て)?)", text):
        return _local_intent(Action.NONE, refers_previous=1.0)
    if re.fullmatch(r"(?:もう少し|もうちょい)", text):
        return _local_intent(Action.NONE, refers_previous=1.0)

    with_device = re.fullmatch(
        rf"(?:(?P<name>.+?)\s*(?:という|って)\s*(?:名前|名)\s*(?:で|の)\s*)?(?P<device>.+?)\s*{_WITH}\s*(?:の)?\s*(?P<kind>{_KIND})?\s*トラック\s*(?:を|も)?\s*{MAKE_VERB}",
        text,
        re.IGNORECASE,
    )
    if with_device:
        device = resolve_native_device(with_device.group("device"))
        if device is not None:
            kind = (with_device.group("kind") or "").lower()
            name = (with_device.group("name") or "").strip() or None
            return _local_intent(Action.ADD_TRACK_WITH_DEVICE, text=name or ("audio" if kind in {"オーディオ", "audio"} else None), native_device=device)
    opened = match_new_track_open(text)
    if opened is not None:
        device = resolve_native_device(opened[0])
        if device is not None:
            return _local_intent(Action.ADD_TRACK_WITH_DEVICE, text=opened[2] or opened[1], native_device=device)
    added = re.fullmatch(rf"(?:(?P<name>.+?)\s*(?:という|って)\s*(?:名前|名)\s*(?:で|の)\s*)?(?:{_NEW_MARK}\s*)?(?P<kind>{_KIND})?\s*トラック\s*(?:を|も)?\s*(?:{_NEW_MARK}\s*)?{MAKE_VERB}", text, re.IGNORECASE)
    if added:
        kind = (added.group("kind") or "midi").lower()
        action = Action.ADD_AUDIO_TRACK if kind in {"オーディオ", "audio"} else Action.ADD_MIDI_TRACK
        return _local_intent(action, text=(added.group("name") or "").strip() or None)
    scene = re.fullmatch(r"シーン\s*(\d+)\s*(?:を)?\s*(?:発射|再生|スタート|鳴らして)?", text)
    if scene:
        index = int(scene.group(1)) - 1
        if any(item.index == index for item in snapshot.scenes):
            return _local_intent(Action.LAUNCH_SCENE, scene=index)
    for pattern, action in LOCAL_TRANSPORT:
        if re.fullmatch(pattern, text, re.IGNORECASE):
            return _local_intent(action)
    bar = re.fullmatch(r"(\d+)\s*小節(?:目)?(?:へ|に|から)?(?:飛んで|移動)?", text)
    if bar:
        return _local_intent(Action.JUMP_TO_BAR, step=Step.SET, number=Number(float(bar.group(1)), "raw"))
    if re.fullmatch(r"(?:頭から|最初から|冒頭へ)", text):
        return _local_intent(Action.JUMP_TO_BAR, step=Step.SET, number=Number(1.0, "raw"))
    tempo = re.fullmatch(rf"テンポ(?:を)?\s*({_NUMBER})\s*(?:BPM)?(?:に)?", text, re.IGNORECASE)
    if tempo:
        return _local_intent(
            Action.TEMPO,
            step=Step.SET,
            number=Number(_numeric_value(tempo.group(1)), "bpm"),
        )

    named_tracks = [track for track in snapshot.tracks if not is_bridge_track(track)]
    target_pattern = (
        r"(?:選択(?:中の|した)?トラック|今のトラック|このトラック|現在のトラック|トラック\s*\d+|\d+\s*番目(?:のトラック)?|\d+\s*番トラック|"
        + "|".join(re.escape(track.name) for track in sorted(named_tracks, key=lambda item: len(item.name), reverse=True) if track.name)
        + r")"
    )
    restore = re.fullmatch(rf"(?P<target>{target_pattern})\s*(?:を\s*)?戻して", text, re.IGNORECASE)
    if restore:
        target_text = restore.group("target")
        if not re.match(r"^(?:トラック\s*\d+|\d+\s*番)", target_text, re.IGNORECASE):
            track = _local_track(snapshot, target_text)
            if track is not None:
                return _local_intent(Action.NONE, track=track, refers_previous=1.0)
    volume = re.fullmatch(
        rf"(?P<target>{target_pattern})\s*の\s*音量\s*を\s*(?P<number>{_NUMBER})\s*(?:dB|デシベル)\s*に(?:して)?",
        text,
        re.IGNORECASE,
    )
    if volume:
        track = _local_track(snapshot, volume.group("target"))
        if track is not None:
            return _local_intent(
                Action.VOLUME,
                track=track,
                step=Step.SET,
                number=Number(_numeric_value(volume.group("number")), "db"),
            )

    rename = re.fullmatch(
        rf"(?P<target>{target_pattern})\s*(?:の名前|の名)?\s*を\s*(?P<name>.+?)\s*に\s*(?:して|変えて|改名|リネーム)(?:して)?",
        text,
        re.IGNORECASE,
    )
    if rename:
        track = _local_track(snapshot, rename.group("target"))
        new_name = rename.group("name").strip().strip("「」\"'")
        if track is not None and new_name:
            return _local_intent(Action.RENAME, track=track, text=new_name)
    clip_value = re.fullmatch(
        rf"(?P<target>{target_pattern})\s*の\s*クリップ\s*(?P<slot>\d+)\s*(?:の|を)?\s*(?P<what>ピッチ|ゲイン|音の高さ|音量)?\s*(?:を)?\s*(?P<rest>.+)",
        text,
        re.IGNORECASE,
    )
    if clip_value and clip_value.group("what") is not None or (clip_value and re.search(r"半音|オクターブ", clip_value.group("rest"))):
        track = _local_track(snapshot, clip_value.group("target"))
        slot = int(clip_value.group("slot")) - 1
        what = clip_value.group("what") or "ピッチ"
        rest = clip_value.group("rest")
        if track is not None:
            action = Action.CLIP_GAIN if what in {"ゲイン", "音量"} else Action.CLIP_PITCH
            number = parse_number(rest, action)
            if re.search(r"(?:少し|ちょい|ちょっと)?(?:上げ|あげ|大きく)", rest):
                step = Step.UP_BIG if re.search(r"大きく|かなり|ガッと", rest) else Step.UP_SMALL
            elif re.search(r"(?:少し|ちょい|ちょっと)?(?:下げ|さげ|小さく)", rest):
                step = Step.DOWN_BIG if re.search(r"大きく|かなり|ガッと", rest) else Step.DOWN_SMALL
            else:
                step = Step.SET if number is not None else Step.NONE
            if action is Action.CLIP_PITCH and number is not None and re.search(r"(?:に|へ)(?:して)?$", rest.strip()):
                step = Step.SET
            if number is not None or step is not Step.NONE:
                return _local_intent(action, track=track, clip=slot, step=step, number=number)
    clip_cmd = re.fullmatch(
        rf"(?P<target>{target_pattern})\s*の\s*クリップ\s*(?P<slot>\d+)\s*(?:を)?\s*(?P<verb>発射|再生|鳴らして|スタート|止めて|停止|ループ(?:オン|して)?|ループ(?:オフ|解除)|ワープ(?:オン|して)?|ワープ(?:オフ|解除))(?:して)?",
        text,
        re.IGNORECASE,
    )
    if clip_cmd:
        track = _local_track(snapshot, clip_cmd.group("target"))
        slot = int(clip_cmd.group("slot")) - 1
        if track is not None:
            verb = clip_cmd.group("verb")
            if verb in {"止めて", "停止"}:
                action = Action.STOP_CLIP
            elif verb.startswith("ループ"):
                action = Action.CLIP_LOOP_OFF if verb.endswith(("オフ", "解除")) else Action.CLIP_LOOP_ON
            elif verb.startswith("ワープ"):
                action = Action.CLIP_WARP_OFF if verb.endswith(("オフ", "解除")) else Action.CLIP_WARP_ON
            else:
                action = Action.LAUNCH_CLIP
            return _local_intent(action, track=track, clip=slot)
    toggle = re.fullmatch(
        rf"(?P<target>{target_pattern})\s*を\s*(?P<action>ミュート|ソロ|アーム|録音待機|mute|unmute|solo|unsolo|arm|disarm)\s*(?P<undo>解除)?(?:して|に)?",
        text,
        re.IGNORECASE,
    )
    if toggle:
        track = _local_track(snapshot, toggle.group("target"))
        if track is not None:
            action = {
                ("ミュート", False): Action.MUTE,
                ("ミュート", True): Action.UNMUTE,
                ("ソロ", False): Action.SOLO,
                ("ソロ", True): Action.UNSOLO,
                ("アーム", False): Action.ARM,
                ("アーム", True): Action.DISARM,
                ("録音待機", False): Action.ARM,
                ("録音待機", True): Action.DISARM,
                ("mute", False): Action.MUTE,
                ("unmute", False): Action.UNMUTE,
                ("solo", False): Action.SOLO,
                ("unsolo", False): Action.UNSOLO,
                ("arm", False): Action.ARM,
                ("disarm", False): Action.DISARM,
            }[(toggle.group("action"), bool(toggle.group("undo")))]
            return _local_intent(action, track=track)
    return None


def detect_language(utterance: str) -> Literal["ja", "en"]:
    return "ja" if re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", utterance) else "en"


def parse_local(utterance: str, snapshot: Snapshot) -> Intent | None:
    if detect_language(utterance) == "ja":
        return _parse_local_ja(utterance, snapshot)
    from intent_en import parse_local_en
    return parse_local_en(utterance, snapshot)


def _score(answer: Any, field: str) -> float:
    if not isinstance(answer, Mapping):
        return 0.0
    value = answer.get(field)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _choice(answer: Any, default: str = "none") -> tuple[str, float]:
    if not isinstance(answer, Mapping) or answer.get("type") != "choice":
        return default, 0.0
    return str(answer.get("choice", default)), _score(answer, "confidence")


def _top(answer: Any, labels: Mapping[str, str]) -> tuple[str, ...]:
    probabilities = answer.get("probabilities") if isinstance(answer, Mapping) else None
    if not isinstance(probabilities, Mapping):
        return ()
    ranked = sorted(probabilities, key=lambda key: _score({"v": probabilities[key]}, "v"), reverse=True)
    return tuple(labels[key] for key in ranked if key in labels and key != "none")[:3]


def interpret_response(snapshot: Snapshot, utterance: str, response: Mapping[str, Any]) -> IntentResult:
    candidates_by_track = candidate_params(snapshot)
    answers = response.get("answers")
    answers = answers if isinstance(answers, Mapping) else {}
    action_name, action_conf = _choice(answers.get("action"))
    try:
        action = Action(action_name)
    except ValueError:
        action = Action.NONE
        action_conf = 0.0
    track_name, track_conf = _choice(answers.get("track"))
    if track_name == "master":
        track: int | None | Literal["master", "selected"] = "master"
    elif track_name == "selected":
        track = "selected"
    elif track_name.startswith("t") and track_name[1:].isdigit():
        track = int(track_name[1:])
    else:
        track = None
    step_name, step_conf = _choice(answers.get("step"))
    try:
        step = Step(step_name)
    except ValueError:
        step = Step.NONE
        step_conf = 0.0
    param: Param | None = None
    param_conf = 0.0
    param_answer: Any = {}
    param_labels: dict[str, str] = {}
    if action is Action.PARAM:
        if isinstance(track, int) and track_conf >= 0.6:
            track_indexes = [track]
        elif track is None or track_conf < 0.6:
            track_indexes = list(candidates_by_track)
        else:
            track_indexes = []
        selected: tuple[float, int, str, Any] | None = None
        for track_index in track_indexes:
            answer = answers.get(f"param_t{track_index}", {})
            param_name, confidence = _choice(answer)
            candidates = candidates_by_track.get(track_index, {})
            if param_name == "none" or param_name not in candidates:
                continue
            item = (confidence, track_index, param_name, answer)
            if selected is None or item[0] > selected[0]:
                selected = item
        if selected is not None:
            param_conf, selected_track_index, param_name, param_answer = selected
            param = candidates_by_track[selected_track_index][param_name]
            if not isinstance(track, int) or track_conf < 0.6:
                track = selected_track_index
                track_conf = param_conf
        if isinstance(track, int):
            for key, candidate in candidates_by_track.get(track, {}).items():
                param_labels[key] = _param_label(snapshot, track, candidate)
            if not param_answer:
                param_answer = answers.get(f"param_t{track}", {})
    native_name, native_conf = _choice(answers.get("native_device"))
    native_device = native_name if native_name in NATIVE_DEVICES else None
    if native_device is None:
        native_conf = 0.0
    send: int | None = None
    send_conf = 0.0
    send_name, send_conf_raw = _choice(answers.get("send"))
    if send_name.startswith("send") and send_name[4:].isdigit() and int(send_name[4:]) < len(snapshot.returns):
        send, send_conf = int(send_name[4:]), send_conf_raw
    scene: int | None = None
    scene_conf = 0.0
    scene_name, scene_conf_raw = _choice(answers.get("scene"))
    if scene_name.startswith("s") and scene_name[1:].isdigit() and any(item.index == int(scene_name[1:]) for item in snapshot.scenes):
        scene, scene_conf = int(scene_name[1:]), scene_conf_raw
    clip: int | None = None
    clip_conf = 0.0
    device: Device | None = None
    device_conf = 0.0
    if action in CLIP_ACTIONS:
        picked, clip_conf, picked_track = _pick_per_track(
            snapshot, answers, "clip_t", track, track_conf, lambda item: {f"c{c.slot}": c.slot for c in item.clips}
        )
        if picked is not None:
            clip = int(picked)
            if not isinstance(track, int) or track_conf < 0.6:
                track, track_conf = picked_track, clip_conf
    if action in {Action.DEVICE_ON, Action.DEVICE_OFF}:
        picked, device_conf, picked_track = _pick_per_track(
            snapshot, answers, "device_t", track, track_conf, lambda item: {f"d{d.index}": d for d in item.devices}
        )
        if picked is not None:
            device = picked
            if not isinstance(track, int) or track_conf < 0.6:
                track, track_conf = picked_track, device_conf
            param = next((p for p in device.params if p.index == 0), None)
            param_conf = device_conf
    selectable_tracks = {item.index: item for item in snapshot.tracks if not is_bridge_track(item)}
    if (
        isinstance(track, int)
        and track not in selectable_tracks
        and any(item.index == track for item in snapshot.tracks)
    ):
        track = None
        track_conf = 0.0
    track_labels = {f"t{item.index}": item.name for item in selectable_tracks.values()}
    track_labels["master"] = "マスター"
    track_labels["selected"] = "選択中のトラック"
    intent = Intent(
        action=action,
        action_conf=action_conf,
        track=track,
        track_conf=track_conf,
        track_stated=_score(answers.get("track_stated"), "noul"),
        param=param,
        param_conf=param_conf,
        step=step,
        step_conf=step_conf,
        number=parse_number(_without_names(snapshot, utterance), action),
        needs_generation=_score(answers.get("needs_generation"), "noul"),
        compound=_score(answers.get("compound"), "noul"),
        refers_previous=_score(answers.get("refers_previous"), "noul"),
        scene=scene,
        scene_conf=scene_conf,
        clip=clip,
        clip_conf=clip_conf,
        device=device,
        device_conf=device_conf,
        send=send,
        send_conf=send_conf,
        native_device=native_device,
        native_device_conf=native_conf,
        text=_track_name_in(utterance),
    )
    send_labels = {f"send{index}": f"{chr(ord('A') + index)}（{name}）" for index, name in enumerate(snapshot.returns)}
    scene_labels = {f"s{item.index}": item.name for item in snapshot.scenes}
    clip_labels = {}
    device_labels = {}
    if isinstance(track, int):
        owner = next((item for item in snapshot.tracks if item.index == track), None)
        if owner is not None:
            clip_labels = {f"c{c.slot}": c.name for c in owner.clips}
            device_labels = {f"d{d.index}": d.name for d in owner.devices}
    return IntentResult(
        intent=intent,
        action_options=_top(answers.get("action"), ACTION_LABELS),
        track_options=_top(answers.get("track"), track_labels),
        param_options=_top(param_answer, param_labels),
        scene_options=_top(answers.get("scene"), scene_labels),
        clip_options=_top(answers.get(f"clip_t{track}") if isinstance(track, int) else {}, clip_labels),
        device_options=_top(answers.get(f"device_t{track}") if isinstance(track, int) else {}, device_labels),
        send_options=_top(answers.get("send"), send_labels),
    )


# ---- 言い回しの語彙。丁寧語・願望・終止形を「て形」にそろえてから照合する ----
_POLITE_TAIL = r"(?:\s*(?:ください|下さい|くれますか|くれない|くれる|くれ|ほしいな|ほしい|欲しい|もらえますか|もらえる|もらいたい|お願いします|お願いね|お願い|頂戴|ちょうだい|みて|おいて|ね|よ|な|か))*\s*[。．.!！?？]*\s*$"
_TE_FORMS = {
    "作り": "作って", "つくり": "作って", "開き": "開いて", "ひらき": "開いて", "入れ": "入れて", "いれ": "入れて",
    "挿し": "挿して", "差し": "挿して", "刺し": "挿して", "さし": "挿して", "載せ": "載せて", "乗せ": "載せて", "のせ": "載せて",
    "立ち上げ": "立ち上げて", "たちあげ": "立ち上げて", "起動し": "起動して", "追加し": "追加して", "使い": "使って", "出し": "出して",
    "読み込み": "読み込んで", "ロードし": "ロードして", "足し": "足して", "増やし": "増やして", "用意し": "用意して", "置き": "置いて",
    "呼び出し": "呼び出して", "セットし": "セットして", "挿入し": "挿入して", "適用し": "適用して", "かけ": "かけて", "立て": "立てて",
    "インサートし": "インサートして", "突っ込み": "突っ込んで", "ぶち込み": "ぶち込んで", "アサインし": "アサインして", "作成し": "作って", "生成し": "作って",
}
_DICT_FORMS = {
    "作る": "作って", "つくる": "作って", "開く": "開いて", "ひらく": "開いて", "入れる": "入れて", "いれる": "入れて", "挿す": "挿して", "差す": "挿して", "さす": "挿して",
    "載せる": "載せて", "乗せる": "載せて", "のせる": "載せて", "立ち上げる": "立ち上げて", "立ち上げ": "立ち上げて", "起動する": "起動して", "起動": "起動して",
    "追加する": "追加して", "追加": "追加して", "使う": "使って", "出す": "出して", "読み込む": "読み込んで", "ロードする": "ロードして", "ロード": "ロードして",
    "足す": "足して", "増やす": "増やして", "用意する": "用意して", "用意": "用意して", "置く": "置いて", "呼び出す": "呼び出して", "セットする": "セットして", "セット": "セットして",
    "挿入する": "挿入して", "挿入": "挿入して", "インサートする": "インサートして", "インサート": "インサートして", "適用する": "適用して", "適用": "適用して", "かける": "かけて",
    "立てる": "立てて", "新規作成する": "作って", "新規作成": "作って", "作成する": "作って", "作成": "作って", "生成する": "作って", "生成": "作って",
    "突っ込む": "突っ込んで", "ぶち込む": "ぶち込んで",
}
_STEM_ALT = "|".join(sorted(map(re.escape, _TE_FORMS), key=len, reverse=True))
_DICT_ALT = "|".join(sorted(map(re.escape, _DICT_FORMS), key=len, reverse=True))
_WISH_TAIL = r"(?:たいと思います|たいと思う|たいんですが|たいんだけど|たいです|たいな|たい|ましょうか|ましょう|ませんか|ます)"


# 「〜しないで」「〜は不要」などの打ち消し。曲を変える依頼として扱わない（確認なしで即実行するため、誤爆の害が大きい）。
NEGATION = re.compile(r"ないで|なくて(?:いい|よい|良い|OK|大丈夫)|なくていい|しなくて|せずに|するな|しないこと|不要|いらない|要らない|禁止|やらないで")


def _is_negated_ja(utterance: str) -> bool:
    return NEGATION.search(utterance) is not None


def is_negated(utterance: str) -> bool:
    if detect_language(utterance) == "ja":
        return _is_negated_ja(utterance)
    from intent_en import is_negated_en
    return is_negated_en(utterance)


def normalize_phrase(utterance: str) -> str:
    """「作りたい」「作ってください」「作る」「作成」→「作って」のように、語尾を て形 にそろえる。"""
    text = utterance.strip()
    text = re.sub(_POLITE_TAIL, "", text)
    text = re.sub(rf"({_STEM_ALT}){_WISH_TAIL}$", lambda m: _TE_FORMS[m.group(1)], text)
    text = re.sub(rf"({_DICT_ALT})$", lambda m: _DICT_FORMS[m.group(1)], text)
    text = re.sub(_POLITE_TAIL, "", text)
    text = re.sub(r"(?:新規作成して|作成して|生成して|つくって)$", "作って", text)
    text = re.sub(r"といて$", "て", text)  # 入れといて → 入れて
    text = re.sub(r"どいて$", "で", text)  # 読んどいて → 読んで
    return text.strip()


INSERT_WORDS = r"(?:挿し込んで|差し込んで|挿して|差して|刺して|さして|入れて|いれて|載せて|乗せて|のせて|読んで|呼んで|よんで|開けて|あけて|付けて|つけて|インサートして|追加して|足して|開いて|ひらいて|立ち上げて|たちあげて|起動して|出して|呼び出して|読み込んで|ロードして|使って|置いて|セットして|挿入して|適用して|かけて|アサインして|突っ込んで|ぶち込んで)"
INSERT_VERB = rf"(?:を|も)?\s*{INSERT_WORDS}"
MAKE_VERB = r"(?:作って|つくって|追加して|足して|増やして|用意して|立てて)"
_NEW_MARK = r"(?:新しい|新規|あたらしい|新しく|新たな|新たに|別の|もう\s*(?:1|一|１)\s*(?:本|つ|個))"
_KIND = r"(?:MIDI|ミディ|オーディオ|Audio|音声|インスト(?:ゥルメント)?)"
_WITH = r"(?:入り|付き|つき|が入った|の入った|を入れた|を載せた|をのせた|を挿した|を立ち上げた|用の|用|向けの)"
_PARTICLE_EDGE = r"^(?:を|に|で|へ|の|は|も|と|が|から)+|(?:を|に|で|へ|の|は|も|と|が|から)+$"


def match_new_track_open(text: str) -> tuple[str, str | None, str | None] | None:
    """「新しいトラックでXを開いて」「Xを新しいトラックで」「トラック作ってX入れて」など、
    「新しいトラック（または トラック＋作る動詞）」と名前Xが同居する言い方 → (X, 種類, トラック名)。名前の照合はしない。
    決まり文句を全部消して、残った1かたまりを名前とみなす（引き算方式。語順に依存しない）。"""
    if is_negated(text):
        return None
    text = normalize_phrase(text)
    if "トラック" not in text:
        return None
    track_name = None
    named = re.match(r"(?P<name>.+?)\s*(?:という|って)\s*(?:名前|名)\s*(?:で|の)\s*", text)
    if named:
        track_name = named.group("name").strip() or None
        text = text[named.end():]
    has_new = re.search(_NEW_MARK, text) is not None
    has_make = re.search(rf"トラック\s*(?:を|も)?\s*{MAKE_VERB}", text) is not None
    if not (has_new or has_make):
        return None
    kind_found = re.search(rf"({_KIND})\s*トラック", text, re.IGNORECASE)
    kind = (kind_found.group(1) if kind_found else "").lower()
    rest = text
    rest = re.sub(rf"(?:{INSERT_WORDS}|{MAKE_VERB})", "\0", rest)
    rest = re.sub(_NEW_MARK, "\0", rest)
    rest = re.sub(rf"(?:{_KIND})?\s*トラック", "\0", rest, flags=re.IGNORECASE)
    rest = re.sub(_WITH, "\0", rest)
    parts = []
    for part in rest.split("\0"):
        part = re.sub(_PARTICLE_EDGE, "", part.strip()).strip()
        if part:
            parts.append(part)
    if len(parts) != 1:
        return None
    # 残りが「録音を始めて」「曲を」のような文なら、プラグイン名ではない（名前に助詞「を」や動詞の語尾は入らない）
    if re.search(r"を|(?:て|で|る|た|だ|う|く|す)$", parts[0]):
        return None
    return parts[0], ("audio" if kind in {"オーディオ", "audio", "音声"} else None), track_name


# ---- クリップのノート変形（クオンタイズ・レガート・移調・強弱・ループ倍） ----
# 実行は Live の中の部品（Remote Script）の clip_notes。対象は「開いているクリップ」か「<トラック>のクリップN」。

@dataclass(frozen=True)
class ClipNotesRequest:
    op: Literal["quantize", "legato", "transpose", "velocity", "duplicate_loop"]
    track: "int | None" = None
    slot: "int | None" = None
    grid: str = "1/16"
    amount: float = 1.0
    semitones: int = 0
    factor: "float | None" = None
    value: "float | None" = None


_KANJI_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "1": 1, "１": 1, "２": 2, "３": 3, "４": 4}
_COUNT = r"(?P<count>\d+|[一二三四五六七八九十])"
_QUANTIZE = r"クオンタイズ|クォンタイズ|クウォンタイズ|クワンタイズ|quantize|グリッドに\s*(?:合わせ|揃え|そろえ)|タイミング\s*(?:を)?\s*(?:揃え|そろえ|合わせ|整え)|ジャストに"
_LEGATO = r"レガート|legato|(?:ノート|音)\s*(?:を|同士を)?\s*(?:つなげ|繋げ|つない|繋い)|隙間\s*(?:を)?\s*(?:埋め|なく|無く)"
_DUPLICATE = r"ループ\s*(?:を|の長さを)?\s*(?:倍|2倍|２倍|二倍)|倍の長さ|長さ\s*(?:を)?\s*(?:倍|2倍|２倍|二倍)|デュプリケート\s*ループ|duplicate\s*loop"
_VELOCITY = r"ベロシティ|ヴェロシティ|velocity|強弱|打鍵"
_NOTE_WORDS = r"クリップ|ノート|MIDI|ミディ|フレーズ|メロディ|打ち込み|音符"
_UP = r"上げ|あげ|アップ|高く|上に"
_DOWN = r"下げ|さげ|ダウン|低く|下に"


def _count_value(text: "str | None", default: int = 1) -> int:
    if not text:
        return default
    return int(text) if text.isdigit() else _KANJI_DIGITS.get(text, default)


def _parse_clip_notes_phrase_ja(utterance: str, snapshot: Snapshot) -> "ClipNotesRequest | None":
    """ノート変形の一言を取り出す。語順は問わず、決め手の語（クオンタイズ・レガート・オクターブ…）で判定する。"""
    if is_negated(utterance):
        return None
    text = normalize_phrase(utterance)
    track: "int | None" = None
    slot: "int | None" = None
    named_tracks = [item for item in snapshot.tracks if not is_bridge_track(item) and item.name]
    names = "|".join(re.escape(item.name) for item in sorted(named_tracks, key=lambda item: len(item.name), reverse=True))
    target = rf"(?:トラック\s*\d+|\d+\s*番目(?:のトラック)?|{names})" if names else r"(?:トラック\s*\d+|\d+\s*番目(?:のトラック)?)"
    slotted = re.search(rf"(?P<target>{target})\s*の\s*クリップ\s*(?P<slot>\d+)", text, re.IGNORECASE)
    if slotted:
        found = _local_track(snapshot, slotted.group("target"))
        if not isinstance(found, int):
            return None
        track, slot = found, int(slotted.group("slot")) - 1
        if slot < 0:
            return None

    if re.search(_DUPLICATE, text, re.IGNORECASE):
        return ClipNotesRequest("duplicate_loop", track, slot)
    if re.search(_QUANTIZE, text, re.IGNORECASE):
        triplet = re.search(r"3連|三連|トリプレット|triplet", text, re.IGNORECASE) is not None
        base = "1/16"
        for words, grid in ((r"32分|三十二分|1/32", "1/32"), (r"16分|十六分|1/16", "1/16"), (r"8分|八分|1/8", "1/8"), (r"4分|四分|1/4", "1/4")):
            if re.search(words, text):
                base = grid
                break
        if triplet and base in {"1/8", "1/16"}:
            base += "t"
        amount = 1.0
        percent = re.search(r"(\d+)\s*(?:%|％|パーセント)", text)
        if percent:
            amount = max(0.0, min(1.0, int(percent.group(1)) / 100.0))
        elif re.search(r"半分|軽く|ゆるく|緩く|少し|ちょっと|やや", text):
            amount = 0.5
        return ClipNotesRequest("quantize", track, slot, grid=base, amount=amount)
    if re.search(_LEGATO, text, re.IGNORECASE):
        return ClipNotesRequest("legato", track, slot)
    if re.search(_VELOCITY, text, re.IGNORECASE) or (re.search(r"ノート|音符", text) and re.search(r"強く|弱く|強め|弱め", text)):
        fixed = re.search(r"(\d+)\s*(?:に|へ)", text)
        if fixed:
            return ClipNotesRequest("velocity", track, slot, value=float(max(1, min(127, int(fixed.group(1))))))
        small = re.search(r"少し|ちょっと|やや|軽く|強め|弱め", text) is not None
        if re.search(rf"{_UP}|強く|強め|大きく", text):
            return ClipNotesRequest("velocity", track, slot, factor=1.1 if small else 1.25)
        if re.search(rf"{_DOWN}|弱く|弱め|小さく|抑え", text):
            return ClipNotesRequest("velocity", track, slot, factor=0.9 if small else 0.8)
        return None
    octave = re.search(rf"(?:{_COUNT}\s*)?オクターブ", text)
    semis = re.search(r"(?P<semis>\d+)\s*(?:半音|セミトーン|st\b)", text, re.IGNORECASE)
    wants_transpose = re.search(r"トランスポーズ|移調|transpose", text, re.IGNORECASE) is not None
    if octave or ((semis or wants_transpose) and (slotted or re.search(_NOTE_WORDS, text, re.IGNORECASE))):
        if slotted and semis and not octave and not re.search(_NOTE_WORDS.replace("クリップ|", ""), text, re.IGNORECASE):
            return None  # 「<トラック>のクリップNを2半音上げて」は従来のクリップのピッチ（定型文）に任せる
        amount_semis = _count_value(octave.group("count") if octave else None) * 12 if octave else int(semis.group("semis")) if semis else 0
        if amount_semis == 0:
            return None
        if re.search(_DOWN, text):
            amount_semis = -amount_semis
        elif not re.search(_UP, text):
            return None
        return ClipNotesRequest("transpose", track, slot, semitones=amount_semis)
    return None


def parse_clip_notes_phrase(utterance: str, snapshot: Snapshot) -> "ClipNotesRequest | None":
    if detect_language(utterance) == "ja":
        return _parse_clip_notes_phrase_ja(utterance, snapshot)
    from intent_en import parse_clip_notes_phrase_en
    return parse_clip_notes_phrase_en(utterance, snapshot)


PLUGIN_ALIASES_PATH = Path(__file__).with_name("plugin_aliases.json")


def load_plugin_aliases() -> dict[str, str]:
    """{"セラム": "Serum 2"} のような利用者ごとの別名。無ければ空。"""
    try:
        raw = json.loads(PLUGIN_ALIASES_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return {str(k).casefold().replace(" ", ""): str(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}


def resolve_plugin_name(text: str, catalog: "tuple[str, ...]") -> str | None:
    """一覧の中から名前を1つに決める。別名→完全一致→部分一致（複数なら最も短い名前）。決まらなければ None。"""
    key = text.strip().strip("「」\"'").casefold()
    squeezed = key.replace(" ", "")
    if not squeezed:
        return None
    alias = load_plugin_aliases().get(squeezed)
    if alias and alias in catalog:
        return alias
    exact = [name for name in catalog if name.casefold() == key or name.casefold().replace(" ", "") == squeezed]
    if exact:
        return exact[0]
    partial = [name for name in catalog if squeezed in name.casefold().replace(" ", "")]
    if partial:
        return sorted(partial, key=lambda name: (len(name), name))[0]
    return None


@dataclass(frozen=True)
class PluginRequest:
    action: Action
    raw_name: str
    track: "int | None | Literal['selected']"
    text: str | None


def _extract_plugin_request_ja(utterance: str, snapshot: Snapshot) -> PluginRequest | None:
    """「<トラック>に<X>を挿して」「<X>入りの(MIDI|オーディオ)トラック作って」の形だけを取り出す（名前の照合はしない）。"""
    if is_negated(utterance):
        return None
    text = normalize_phrase(utterance)
    named_tracks = [track for track in snapshot.tracks if not is_bridge_track(track)]
    names = "|".join(re.escape(track.name) for track in sorted(named_tracks, key=lambda item: len(item.name), reverse=True) if track.name)
    selected = r"選択(?:中の|した)?トラック|今のトラック|このトラック|現在のトラック"
    target = rf"(?:{selected}|トラック\s*\d+|\d+\s*番目(?:のトラック)?|{names})" if names else rf"(?:{selected}|トラック\s*\d+|\d+\s*番目(?:のトラック)?)"
    with_plugin = re.fullmatch(
        rf"(?:(?P<name>.+?)\s*(?:という|って)\s*(?:名前|名)\s*(?:で|の)\s*)?(?P<plugin>.+?)\s*{_WITH}\s*(?:の)?\s*(?P<kind>{_KIND})?\s*トラック\s*(?:を|も)?\s*{MAKE_VERB}",
        text,
        re.IGNORECASE,
    )
    if with_plugin and resolve_native_device(with_plugin.group("plugin")) is None:
        kind = (with_plugin.group("kind") or "").lower()
        name = (with_plugin.group("name") or "").strip() or None
        return PluginRequest(Action.ADD_TRACK_WITH_PLUGIN, with_plugin.group("plugin"), None, name or ("audio" if kind in {"オーディオ", "audio", "音声"} else None))
    opened = match_new_track_open(text)
    if opened is not None:
        if resolve_native_device(opened[0]) is None:
            return PluginRequest(Action.ADD_TRACK_WITH_PLUGIN, opened[0], None, opened[2] or opened[1])
        return None
    insert = re.fullmatch(rf"(?:(?P<target>{target})\s*(?:に|へ|で|の)\s*)?(?P<plugin>.+?)\s*{INSERT_VERB}", text, re.IGNORECASE)
    if insert:
        track = _local_track(snapshot, insert.group("target")) if insert.group("target") else None
        # 「オーディオトラックを追加して」「クリップを開いて」は装置の依頼ではない
        if re.search(r"トラック|クリップ|シーン", insert.group("plugin")):
            return None
        if insert.group("target") is None or track is not None:
            return PluginRequest(Action.INSERT_PLUGIN, insert.group("plugin"), track, None)
    return None


def extract_plugin_request(utterance: str, snapshot: Snapshot) -> PluginRequest | None:
    if detect_language(utterance) == "ja":
        return _extract_plugin_request_ja(utterance, snapshot)
    from intent_en import extract_plugin_request_en
    return extract_plugin_request_en(utterance, snapshot)


def plugin_intent(request: PluginRequest, plugin: str) -> Intent:
    return _local_intent(request.action, track=request.track, text=request.text, plugin=plugin)


def parse_plugin_phrase(utterance: str, snapshot: Snapshot, catalog: "tuple[str, ...]") -> Intent | None:
    request = extract_plugin_request(utterance, snapshot)
    if request is None:
        return None
    plugin = resolve_plugin_name(request.raw_name, catalog)
    return plugin_intent(request, plugin) if plugin is not None else None
