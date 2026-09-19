"""User-visible Python messages in Japanese and English."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
import re
from string import Formatter
from typing import Iterator, Literal, Mapping


Language = Literal["ja", "en"]
LanguageSetting = Literal["auto", "ja", "en"]


MESSAGES: dict[str, dict[Language, str]] = {
    "state.on": {"ja": "オン", "en": "On"},
    "state.off": {"ja": "オフ", "en": "Off"},
    "state.playing": {"ja": "再生中", "en": "Playing"},
    "state.stopped": {"ja": "停止中", "en": "Stopped"},
    "label.master": {"ja": "マスター", "en": "Master"},
    "label.volume": {"ja": "音量", "en": "Volume"},
    "label.pan": {"ja": "パン", "en": "Pan"},
    "label.mute": {"ja": "ミュート", "en": "Mute"},
    "label.solo": {"ja": "ソロ", "en": "Solo"},
    "label.arm": {"ja": "録音待機", "en": "Arm"},
    "label.fold": {"ja": "折りたたみ", "en": "Fold"},
    "label.monitor": {"ja": "モニター", "en": "Monitor"},
    "label.loop": {"ja": "ループ", "en": "Loop"},
    "label.metronome": {"ja": "メトロノーム", "en": "Metronome"},
    "label.record": {"ja": "セッション録音", "en": "Record"},
    "label.overdub": {"ja": "オーバーダブ", "en": "Overdub"},
    "label.clip_loop": {"ja": "ループ", "en": "Loop"},
    "label.clip_warp": {"ja": "ワープ", "en": "Warp"},
    "label.clip_pitch": {"ja": "ピッチ", "en": "Pitch"},
    "label.clip_gain": {"ja": "ゲイン", "en": "Gain"},
    "status.connected": {"ja": "Live {tracks}トラック / {tempo:g} BPM", "en": "Live {tracks} tracks / {tempo:g} BPM"},
    "status.disconnected": {"ja": "Liveに未接続", "en": "Live disconnected"},
    "status.quit": {"ja": "終了します", "en": "Quitting"},
    "error.live": {"ja": "Liveに繋がりません。装置が載っているか確認してください", "en": "Can't connect to Live. Check that the bridge device is loaded."},
    "error.stale": {"ja": "曲の構成が変わり続けています。少し待ってからもう一度", "en": "The Live Set keeps changing. Wait a moment and try again."},
    "error.empty": {"ja": "一言を入力してください", "en": "Enter a command."},
    "error.jev_key": {"ja": "Jevの鍵が見つかりません", "en": "The Jev API key was not found."},
    "error.jev": {"ja": "Jevに繋がりません。少し待ってから試してください", "en": "Can't connect to Jev. Wait a moment and try again."},
    "error.track_required": {"ja": "トラックが必要です", "en": "A track is required."},
    "error.track_missing": {"ja": "トラックが見つかりません", "en": "The track was not found."},
    "error.named_track_missing": {"ja": "指定したトラックが見つかりません", "en": "The specified track was not found."},
    "error.master_volume_only": {"ja": "マスターは音量だけ操作できます", "en": "Only volume can be changed on Master."},
    "error.master_unsupported": {"ja": "その操作はマスターに対応していません", "en": "That action is not available on Master."},
    "error.db_internal": {"ja": "dB指定は表示値探索で処理します", "en": "dB values require display-value lookup."},
    "error.pan_unit": {"ja": "パンの単位が正しくありません", "en": "The pan unit is invalid."},
    "error.param_required": {"ja": "つまみが必要です", "en": "A parameter is required."},
    "error.no_action": {"ja": "実行する操作がありません", "en": "There is no action to run."},
    "error.bar_required": {"ja": "小節番号が必要です", "en": "A bar number is required."},
    "error.clip_required": {"ja": "クリップが必要です", "en": "A clip is required."},
    "error.clip_missing": {"ja": "クリップが見つかりません", "en": "The clip was not found."},
    "error.scene_required": {"ja": "シーンが必要です", "en": "A scene is required."},
    "error.scene_missing": {"ja": "シーンが見つかりません", "en": "The scene was not found."},
    "error.device_required": {"ja": "デバイスが必要です", "en": "A device is required."},
    "error.send_required": {"ja": "センドが必要です", "en": "A send is required."},
    "error.name_required": {"ja": "新しい名前が必要です", "en": "A new name is required."},
    "error.device_name_required": {"ja": "デバイス名が必要です", "en": "A device name is required."},
    "error.plugin_name_required": {"ja": "プラグイン名が必要です", "en": "A plug-in name is required."},
    "error.volume_silent": {"ja": "今の音量が無音（-inf dB）か読めないため、上げ下げできません。「−12dBに」のように値で指定してください", "en": "The current volume is silent (-inf dB) or unreadable. Set an exact value such as -12 dB."},
    "error.json": {"ja": "正しいJSONを1行で送ってください", "en": "Send one valid JSON object per line."},
    "error.generic": {"ja": "操作を完了できませんでした", "en": "The operation could not be completed."},
    "readback.track_value": {"ja": "{track}: {label} {value}", "en": "{track}: {label} {value}"},
    "readback.track_state": {"ja": "{track}: {label} {state}", "en": "{track}: {label} {state}"},
    "readback.song_state": {"ja": "{label} {state}", "en": "{label} {state}"},
    "readback.tempo": {"ja": "テンポ: {tempo:g} BPM", "en": "Tempo {tempo:g} BPM"},
    "readback.param_missing": {"ja": "つまみを読み取れませんでした", "en": "The parameter could not be read."},
    "readback.param": {"ja": "{track}: {device} / {param} {value}", "en": "{track}: {device} / {param} {value}"},
    "readback.position": {"ja": "再生位置: {bar}小節", "en": "Position bar {bar}"},
    "readback.text.undo": {"ja": "取り消しました", "en": "Undone"},
    "readback.text.redo": {"ja": "やり直しました", "en": "Redone"},
    "readback.text.capture": {"ja": "MIDIをキャプチャしました", "en": "Captured MIDI"},
    "readback.text.tap": {"ja": "タップしました", "en": "Tempo tapped"},
    "readback.text.stop_all": {"ja": "すべてのクリップを止めました", "en": "Stopped all clips"},
    "readback.text.stop_track": {"ja": "トラックのクリップを止めました", "en": "Stopped track clips"},
    "readback.clip_launch": {"ja": "{track}: クリップ「{clip}」を発射", "en": "{track}: Launched clip {clip}"},
    "readback.clip_stop": {"ja": "{track}: クリップ「{clip}」を停止", "en": "{track}: Stopped clip {clip}"},
    "readback.scene": {"ja": "シーン「{scene}」を発射", "en": "Launched scene {scene}"},
    "readback.scene_generic": {"ja": "シーンを発射", "en": "Launched scene"},
    "readback.device_missing": {"ja": "デバイスの状態を読み取れませんでした", "en": "The device state could not be read."},
    "readback.send": {"ja": "{track}: センド{send} {value:.0f}%", "en": "{track}: Send {send} {value:.0f}%"},
    "readback.rename": {"ja": "名前を「{name}」にしました", "en": "Renamed track to {name}"},
    "readback.add_track": {"ja": "{kind}を追加しました", "en": "Added {kind}"},
    "readback.add_device": {"ja": "新しいトラックに {device} を挿入しました", "en": "Added {device} on a new track"},
    "readback.insert_plugin": {"ja": "{plugin} を挿入しました", "en": "Inserted {plugin}"},
    "readback.add_plugin": {"ja": "新しいトラックに {plugin} を挿入しました", "en": "Added {plugin} on a new track"},
    "readback.clip_prop": {"ja": "{track}: クリップ「{clip}」 {label} {value}", "en": "{track}: Clip {clip} {label} {value}"},
    "readback.slot": {"ja": "スロット{slot}", "en": "Slot {slot}"},
    "unit.pitch": {"ja": "{value:+d}半音", "en": "{value:+d} st"},
    "kind.midi_track": {"ja": "MIDIトラック", "en": "MIDI track"},
    "kind.audio_track": {"ja": "オーディオトラック", "en": "audio track"},
    "ask.action": {"ja": "何をしますか？", "en": "What should I do?"},
    "ask.track": {"ja": "どのトラックですか？", "en": "Which track?"},
    "ask.insert_track": {"ja": "どのトラックに挿しますか？", "en": "Which track should I insert it on?"},
    "ask.param": {"ja": "どのつまみですか？", "en": "Which parameter?"},
    "ask.device_native": {"ja": "どの内蔵デバイスを載せますか？", "en": "Which Ableton device?"},
    "ask.send": {"ja": "どのセンドですか？", "en": "Which send?"},
    "ask.scene": {"ja": "どのシーンですか？", "en": "Which scene?"},
    "ask.clip": {"ja": "どのクリップですか？", "en": "Which clip?"},
    "ask.device": {"ja": "どのデバイスですか？", "en": "Which device?"},
    "ask.direction": {"ja": "上げますか、下げますか？", "en": "Raise it or lower it?"},
    "option.up_small": {"ja": "少し上げる", "en": "Raise a little"},
    "option.down_small": {"ja": "少し下げる", "en": "Lower a little"},
    "option.yes": {"ja": "はい", "en": "Yes"},
    "option.cancel": {"ja": "やめる", "en": "Cancel"},
    "info.one_at_a_time": {"ja": "1つずつお願いします（例: パッド下げて → ベース上げて）", "en": "Please ask for one action at a time, for example: lower Pad, then raise Bass."},
    "info.freeform": {"ja": "これは決まった操作では表せない依頼です（作曲や自由な編集には対応していません）", "en": "That is beyond the fixed actions Live Jev supports (no composing or free-form editing)."},
    "info.plugin_name_needed": {"ja": "どのプラグインか分かりませんでした。名前で言ってください（例: Serum 2 を挿して）", "en": "I could not tell which plug-in you mean. Say its name, for example: insert Serum 2."},
    "info.negated": {"ja": "何も変えていません（「〜しないで」と受け取りました）", "en": "Nothing changed (I took that as “don’t”)."},
    "info.no_undo": {"ja": "戻せる操作がありません", "en": "There is no action to undo."},
    "info.no_repeat": {"ja": "繰り返せる操作がありません", "en": "There is no action to repeat."},
    "info.no_confirmation": {"ja": "確認待ちの操作はありません", "en": "There is no action waiting for confirmation."},
    "info.cancelled": {"ja": "やめました", "en": "Cancelled"},
    "info.changed_manually": {"ja": "手で変更されているので戻しません（今 {value}）", "en": "Not undone because it was changed manually. Current value: {value}."},
    "info.unchanged": {"ja": "（変わりませんでした。この種類では使えない項目かもしれません）", "en": " (No change. This item may not support that setting.)"},
    "info.from_value": {"ja": "（{value} から）", "en": " (from {value})"},
    "info.readback_recovered": {"ja": "（書き込みの応答がなく、現在値を読み戻しました）", "en": " (No write response; read back the current value.)"},
    "plugin.not_found": {"ja": "「{name}」に当たるプラグインが一覧にありません", "en": "No plug-in matching {name} is in the list."},
    "plugin.phrase_hint": {"ja": "「{name}」は分かりましたが、言い方は「<トラック>に{name}を挿して」か「{name}入りのMIDIトラック作って」でお願いします", "en": "I found {name}. Say 'insert {name} on <track>' or 'add a new MIDI track with {name}'."},
    "plugin.enable_script": {"ja": "「{name}」は外部プラグインです。挿すには Live 側で LiveJev を有効にしてください（設定 → Link, Tempo & MIDI → コントロールサーフェス）", "en": "{name} is an external plug-in. Enable LiveJev in Live under Settings > Link, Tempo & MIDI > Control Surface."},
    "plugin.generic": {"ja": "「{word}」だけでは決められません。名前で指定してください（例: {hint}）", "en": "{word} is too broad. Name a specific plug-in, for example: {hint}."},
    "plugin.none": {"ja": "該当なし", "en": "none"},
    "clip.no_clip": {"ja": "開いているクリップがありません（Liveでクリップをダブルクリックして開いてから、もう一度）", "en": "No clip is open. Double-click a clip in Live and try again."},
    "clip.midi_only": {"ja": "これはMIDIクリップだけの操作です", "en": "This action only works on MIDI clips."},
    "clip.no_notes": {"ja": "このクリップにはノートがありません", "en": "This clip has no notes."},
    "clip.out_of_range": {"ja": "音域の端を超えるので移調できません", "en": "The notes would exceed the pitch range."},
    "clip.bad_grid": {"ja": "その細かさではクオンタイズできません", "en": "That quantize grid is not supported."},
    "clip.old_script": {"ja": "Liveの中の部品が古い版です。Liveを再起動してください", "en": "The Live component is outdated. Restart Live."},
    "clip.timeout": {"ja": "Liveが忙しくて返事がありません。少し待ってからもう一度", "en": "Live is busy. Wait a moment and try again."},
    "clip.quantized": {"ja": "{grid}でクオンタイズしました{amount}", "en": "Quantized to {grid}{amount}"},
    "clip.amount": {"ja": "（{percent}%）", "en": " ({percent}%)"},
    "clip.legato": {"ja": "レガートにしました", "en": "Made notes legato"},
    "clip.transpose": {"ja": "{size}{direction}ました", "en": "Transposed {direction} {size}"},
    "clip.up": {"ja": "上げ", "en": "up"},
    "clip.down": {"ja": "下げ", "en": "down"},
    "clip.octaves": {"ja": "{count}オクターブ", "en": "{count} octave(s)"},
    "clip.semitones": {"ja": "{count}半音", "en": "{count} st"},
    "clip.velocity_value": {"ja": "ベロシティを {value} にしました", "en": "Set velocity to {value}"},
    "clip.velocity_factor": {"ja": "ベロシティを {value}% にしました", "en": "Set velocity to {value}%"},
    "clip.double": {"ja": "ループを倍にしました", "en": "Doubled the loop"},
    "confirm.operation": {"ja": "{target}{label}{detail}を実行します。よろしいですか？", "en": "Run {target}{label}{detail}?"},
}


ACTION_LABELS: dict[str, dict[Language, str]] = {
    "volume": {"ja": "音量", "en": "Volume"}, "pan": {"ja": "パン", "en": "Pan"},
    "mute": {"ja": "ミュート", "en": "Mute"}, "unmute": {"ja": "ミュート解除", "en": "Unmute"},
    "solo": {"ja": "ソロ", "en": "Solo"}, "unsolo": {"ja": "ソロ解除", "en": "Unsolo"},
    "tempo": {"ja": "テンポ", "en": "Tempo"}, "play": {"ja": "再生", "en": "Play"}, "stop": {"ja": "停止", "en": "Stop"},
    "param": {"ja": "つまみ", "en": "Parameter"}, "none": {"ja": "該当なし", "en": "None"},
    "continue": {"ja": "続きから再生", "en": "Resume"}, "record_on": {"ja": "録音開始", "en": "Record on"}, "record_off": {"ja": "録音停止", "en": "Record off"},
    "overdub_on": {"ja": "オーバーダブ", "en": "Overdub on"}, "overdub_off": {"ja": "オーバーダブ解除", "en": "Overdub off"},
    "loop_on": {"ja": "ループ", "en": "Loop on"}, "loop_off": {"ja": "ループ解除", "en": "Loop off"},
    "metronome_on": {"ja": "メトロノーム", "en": "Metronome on"}, "metronome_off": {"ja": "メトロノーム停止", "en": "Metronome off"},
    "undo": {"ja": "取り消し", "en": "Undo"}, "redo": {"ja": "やり直し", "en": "Redo"}, "capture_midi": {"ja": "MIDIキャプチャ", "en": "Capture MIDI"},
    "tap_tempo": {"ja": "タップテンポ", "en": "Tap tempo"}, "stop_all_clips": {"ja": "全クリップ停止", "en": "Stop all clips"},
    "jump_to_bar": {"ja": "再生位置", "en": "Position"}, "arm": {"ja": "録音待機", "en": "Arm"}, "disarm": {"ja": "録音待機解除", "en": "Disarm"},
    "monitor_in": {"ja": "モニターIn", "en": "Monitor In"}, "monitor_auto": {"ja": "モニターAuto", "en": "Monitor Auto"}, "monitor_off": {"ja": "モニターOff", "en": "Monitor Off"},
    "fold": {"ja": "折りたたみ", "en": "Fold"}, "unfold": {"ja": "展開", "en": "Unfold"}, "track_stop_clips": {"ja": "トラックのクリップ停止", "en": "Stop track clips"},
    "launch_clip": {"ja": "クリップ発射", "en": "Launch clip"}, "stop_clip": {"ja": "クリップ停止", "en": "Stop clip"}, "launch_scene": {"ja": "シーン発射", "en": "Launch scene"},
    "device_on": {"ja": "デバイスON", "en": "Device on"}, "device_off": {"ja": "デバイスOFF", "en": "Device off"}, "send": {"ja": "センド", "en": "Send"},
    "rename": {"ja": "名前変更", "en": "Rename"}, "add_midi_track": {"ja": "MIDIトラック追加", "en": "Add MIDI track"}, "add_audio_track": {"ja": "オーディオトラック追加", "en": "Add audio track"},
    "clip_loop_on": {"ja": "クリップのループ", "en": "Clip loop on"}, "clip_loop_off": {"ja": "クリップのループ解除", "en": "Clip loop off"},
    "clip_warp_on": {"ja": "クリップのワープ", "en": "Clip warp on"}, "clip_warp_off": {"ja": "クリップのワープ解除", "en": "Clip warp off"},
    "clip_pitch": {"ja": "クリップのピッチ", "en": "Clip pitch"}, "clip_gain": {"ja": "クリップのゲイン", "en": "Clip gain"},
    "add_track_with_device": {"ja": "デバイス入りトラック追加", "en": "Add track with device"}, "insert_plugin": {"ja": "プラグイン挿入", "en": "Insert plug-in"},
    "add_track_with_plugin": {"ja": "プラグイン入りトラック追加", "en": "Add track with plug-in"},
}

STEP_LABELS = {
    "up_small": {"ja": "少し上げる", "en": "Raise a little"}, "up_big": {"ja": "大きく上げる", "en": "Raise a lot"},
    "down_small": {"ja": "少し下げる", "en": "Lower a little"}, "down_big": {"ja": "大きく下げる", "en": "Lower a lot"},
    "set": {"ja": "指定値にする", "en": "Set value"}, "none": {"ja": "", "en": ""},
}


def _fields(value: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(value) if name}


for _key, _translations in MESSAGES.items():
    if set(_translations) != {"ja", "en"} or _fields(_translations["ja"]) != _fields(_translations["en"]):
        raise ValueError(f"invalid message translations: {_key}")


_language: ContextVar[Language] = ContextVar("live_jev_output_language", default="ja")


def current_language() -> Language:
    return _language.get()


def render(key: str, /, *, lang: Language | None = None, **values: object) -> str:
    language = lang or current_language()
    return MESSAGES[key][language].format(**values)


@contextmanager
def using_language(language: Language) -> Iterator[None]:
    token = _language.set(language)
    try:
        yield
    finally:
        _language.reset(token)


def resolve_language(value: object, *, default: Language = "ja") -> Language:
    if value in {"ja", "en"}:
        return value  # type: ignore[return-value]
    if value == "auto":
        locale = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG", "")
        return "ja" if locale.casefold().startswith("ja") else "en"
    return default


def action_label(action: str, *, lang: Language | None = None) -> str:
    return ACTION_LABELS.get(action, {"ja": action, "en": action})[lang or current_language()]


def step_label(step: str, *, lang: Language | None = None) -> str | None:
    value = STEP_LABELS.get(step, {"ja": step, "en": step})[lang or current_language()]
    return value or None


@dataclass(frozen=True)
class LocalizedError(ValueError):
    key: str
    values: Mapping[str, object] | None = None

    def translated(self, lang: Language) -> str:
        return render(self.key, lang=lang, **dict(self.values or {}))

    def __str__(self) -> str:
        return self.translated("ja")


def contains_japanese(value: str) -> bool:
    return re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", value) is not None
