"""Live の現在値を、判定と実行で使う不変データへ変換する。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import time
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class Param:
    index: int
    name: str
    value: float
    min: float
    max: float
    display: str
    path: str


@dataclass(frozen=True)
class Device:
    index: int
    name: str
    params: tuple[Param, ...]
    path: str


@dataclass(frozen=True)
class Scene:
    index: int
    name: str
    path: str


@dataclass(frozen=True)
class Clip:
    slot: int
    name: str
    path: str
    props: Mapping[str, Any] = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True)
class Track:
    index: int
    name: str
    volume: float
    volume_display: str
    pan: float
    pan_display: str
    mute: bool
    solo: bool
    devices: tuple[Device, ...]
    path: str
    arm: bool = False
    current_monitoring_state: int = 1
    fold_state: bool = False
    clips: tuple[Clip, ...] = ()
    sends: tuple[float, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    tracks: tuple[Track, ...]
    master_volume: float
    master_display: str
    tempo: float
    playing: bool
    taken_at: float
    song: Mapping[str, Any] = field(default_factory=dict, hash=False, compare=False)
    scenes: tuple[Scene, ...] = ()
    returns: tuple[str, ...] = ()


def is_bridge_track(track: Track) -> bool:
    return any("liveudpbridge" in device.name.casefold() for device in track.devices)


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "off"}
    return bool(value)


def _percent_display(value: float, minimum: float, maximum: float) -> str:
    if maximum <= minimum:
        return "0%"
    percent = (value - minimum) * 100.0 / (maximum - minimum)
    return f"{percent:.0f}%"


def param_from_payload(index: int, payload: Mapping[str, Any]) -> Param:
    value = _number(payload.get("value"))
    minimum = _number(payload.get("min"))
    maximum = _number(payload.get("max"), 1.0)
    return Param(
        index=index,
        name=str(payload.get("name") or f"Parameter {index}"),
        value=value,
        min=minimum,
        max=maximum,
        display=_percent_display(value, minimum, maximum),
        path=str(payload.get("path") or ""),
    )


def _parameter(mixer: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    parameters = mixer.get("parameters")
    if isinstance(parameters, Mapping):
        value = parameters.get(name)
        if isinstance(value, Mapping):
            return value
    return {}


def _devices_by_track(device_payload: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    answer: dict[str, list[Mapping[str, Any]]] = {}
    tracks = device_payload.get("tracks")
    if not isinstance(tracks, list):
        return answer
    for item in tracks:
        if not isinstance(item, Mapping):
            continue
        track = item.get("track")
        path = track.get("path") if isinstance(track, Mapping) else item.get("track_path")
        devices = item.get("devices")
        if path and isinstance(devices, list):
            answer[str(path)] = [d for d in devices if isinstance(d, Mapping)]
    return answer


def build_snapshot(
    context: Mapping[str, Any],
    children: Iterable[Mapping[str, Any]],
    names: Mapping[int, str],
    mixers: Mapping[str, Mapping[str, Any]],
    device_list: Mapping[str, Any],
    device_parameters: Mapping[str, Mapping[str, Any]],
    mute: Mapping[int, bool],
    solo: Mapping[int, bool],
    displays: Mapping[str, str] | None = None,
    *,
    taken_at: float | None = None,
    scenes: Iterable[Mapping[str, Any]] | None = None,
    clips: Mapping[int, Iterable[Clip]] | None = None,
    returns: Iterable[str] | None = None,
    sends: Mapping[int, Iterable[float]] | None = None,
) -> Snapshot:
    displays = displays or {}
    clips = clips or {}
    sends = sends or {}
    scene_items = tuple(
        Scene(index=int(raw.get("index", position)), name=str(raw.get("name") or f"Scene {position + 1}"), path=str(raw.get("path") or f"live_set scenes {position}"))
        for position, raw in enumerate(scenes or [])
        if isinstance(raw, Mapping)
    )
    song = context.get("song")
    song = song if isinstance(song, Mapping) else {}
    listed_devices = _devices_by_track(device_list)
    tracks: list[Track] = []
    for child in children:
        index = int(child.get("index", len(tracks)))
        path = str(child.get("path") or f"live_set tracks {index}")
        mixer = mixers.get(path, {})
        volume = _parameter(mixer, "volume")
        panning = _parameter(mixer, "panning")
        devices: list[Device] = []
        for device_index, raw_device in enumerate(listed_devices.get(path, [])):
            device_path = str(raw_device.get("path") or f"{path} devices {device_index}")
            detail = device_parameters.get(device_path, raw_device)
            raw_params = detail.get("parameters") if isinstance(detail, Mapping) else []
            params = tuple(
                param_from_payload(param_index, raw_param)
                for param_index, raw_param in enumerate(raw_params or [])
                if isinstance(raw_param, Mapping)
            )
            devices.append(
                Device(
                    index=device_index,
                    name=str(raw_device.get("name") or f"Device {device_index}"),
                    params=params,
                    path=device_path,
                )
            )
        volume_value = _number(volume.get("value"))
        tracks.append(
            Track(
                index=index,
                name=names.get(index) or str(child.get("name") or f"Track {index + 1}"),
                volume=volume_value,
                volume_display=displays.get(f"{path} mixer_device volume", f"{volume_value:g}"),
                pan=_number(panning.get("value")),
                pan_display=displays.get(f"{path} mixer_device panning", f"{_number(panning.get('value')):g}"),
                mute=_boolean(mute.get(index, False)),
                solo=_boolean(solo.get(index, False)),
                devices=tuple(devices),
                path=path,
                clips=tuple(clips.get(index, ())),
                sends=tuple(float(value) for value in sends.get(index, ())),
            )
        )
    master = mixers.get("live_set master_track", {})
    master_volume = _parameter(master, "volume")
    master_value = _number(master_volume.get("value"))
    return Snapshot(
        tracks=tuple(tracks),
        master_volume=master_value,
        master_display=displays.get("live_set master_track mixer_device volume", f"{master_value:g}"),
        tempo=_number(song.get("tempo"), 120.0),
        playing=_boolean(song.get("is_playing")),
        taken_at=time.time() if taken_at is None else taken_at,
        song=song_fields(song),
        scenes=scene_items,
        returns=tuple(str(name) for name in (returns or ())),
    )


def snapshot_from_script(payload: Mapping[str, Any], *, taken_at: float | None = None) -> Snapshot:
    if payload.get("schema") != 1:
        raise ValueError("unsupported snapshot schema")
    raw_song = payload.get("song")
    raw_tracks = payload.get("tracks")
    raw_master = payload.get("master")
    raw_scenes = payload.get("scenes")
    raw_returns = payload.get("returns")
    if not isinstance(raw_song, Mapping) or not isinstance(raw_tracks, list):
        raise ValueError("invalid snapshot payload")
    if not isinstance(raw_master, Mapping) or not isinstance(raw_scenes, list) or not isinstance(raw_returns, list):
        raise ValueError("invalid snapshot payload")

    tracks: list[Track] = []
    for position, raw_track in enumerate(raw_tracks):
        if not isinstance(raw_track, Mapping):
            raise ValueError("invalid snapshot track")
        index = int(raw_track.get("index", position))
        path = str(raw_track.get("path") or f"live_set tracks {index}")
        raw_mixer = raw_track.get("mixer")
        raw_devices = raw_track.get("devices")
        raw_clips = raw_track.get("clips")
        raw_sends = raw_track.get("sends")
        if not isinstance(raw_mixer, Mapping) or not isinstance(raw_devices, list):
            raise ValueError("invalid snapshot track")
        if not isinstance(raw_clips, list) or not isinstance(raw_sends, list):
            raise ValueError("invalid snapshot track")
        raw_volume = raw_mixer.get("volume")
        raw_panning = raw_mixer.get("panning")
        if not isinstance(raw_volume, Mapping) or not isinstance(raw_panning, Mapping):
            raise ValueError("invalid snapshot mixer")

        devices: list[Device] = []
        for device_position, raw_device in enumerate(raw_devices):
            if not isinstance(raw_device, Mapping):
                raise ValueError("invalid snapshot device")
            device_index = int(raw_device.get("index", device_position))
            device_path = str(raw_device.get("path") or f"{path} devices {device_index}")
            raw_parameters = raw_device.get("parameters")
            if not isinstance(raw_parameters, list):
                raise ValueError("invalid snapshot parameters")
            parameters = tuple(
                param_from_payload(
                    int(raw_parameter.get("index", parameter_position)),
                    {
                        **raw_parameter,
                        "path": str(raw_parameter.get("path") or f"{device_path} parameters {parameter_position}"),
                    },
                )
                for parameter_position, raw_parameter in enumerate(raw_parameters)
                if isinstance(raw_parameter, Mapping)
            )
            devices.append(Device(
                index=device_index,
                name=str(raw_device.get("name") or f"Device {device_index}"),
                params=parameters,
                path=device_path,
            ))

        clips = tuple(
            Clip(
                slot=int(raw_clip.get("slot", clip_position)),
                name=str(raw_clip.get("name") or f"Clip {clip_position + 1}"),
                path=str(raw_clip.get("path") or f"{path} clip_slots {clip_position} clip"),
                props=raw_clip.get("props") if isinstance(raw_clip.get("props"), Mapping) else {},
            )
            for clip_position, raw_clip in enumerate(raw_clips)
            if isinstance(raw_clip, Mapping)
        )
        volume = _number(raw_volume.get("value"))
        panning = _number(raw_panning.get("value"))
        tracks.append(Track(
            index=index,
            name=str(raw_track.get("name") or f"Track {index + 1}"),
            volume=volume,
            volume_display=str(raw_volume.get("display") or f"{volume:g}"),
            pan=panning,
            pan_display=str(raw_panning.get("display") or f"{panning:g}"),
            mute=_boolean(raw_track.get("mute")),
            solo=_boolean(raw_track.get("solo")),
            devices=tuple(devices),
            path=path,
            arm=_boolean(raw_track.get("arm")),
            current_monitoring_state=int(raw_track.get("current_monitoring_state", 1)),
            fold_state=_boolean(raw_track.get("fold_state")),
            clips=clips,
            sends=tuple(_number(value) for value in raw_sends),
        ))

    raw_master_volume = raw_master.get("volume")
    if not isinstance(raw_master_volume, Mapping):
        raise ValueError("invalid snapshot master")
    master_volume = _number(raw_master_volume.get("value"))
    scenes = tuple(
        Scene(
            index=int(raw_scene.get("index", position)),
            name=str(raw_scene.get("name") or f"Scene {position + 1}"),
            path=str(raw_scene.get("path") or f"live_set scenes {position}"),
        )
        for position, raw_scene in enumerate(raw_scenes)
        if isinstance(raw_scene, Mapping)
    )
    returns = tuple(
        str(raw_return.get("name") or f"Return {position + 1}")
        for position, raw_return in enumerate(raw_returns)
        if isinstance(raw_return, Mapping)
    )
    return Snapshot(
        tracks=tuple(tracks),
        master_volume=master_volume,
        master_display=str(raw_master_volume.get("display") or f"{master_volume:g}"),
        tempo=_number(raw_song.get("tempo"), 120.0),
        playing=_boolean(raw_song.get("is_playing")),
        taken_at=time.time() if taken_at is None else taken_at,
        song=song_fields(raw_song),
        scenes=scenes,
        returns=returns,
    )


SONG_FIELDS = ("loop", "metronome", "session_record", "overdub", "current_song_time", "signature_numerator", "signature_denominator", "record_mode", "clip_trigger_quantization")


def song_fields(song: Mapping[str, Any]) -> dict[str, Any]:
    return {key: song[key] for key in SONG_FIELDS if key in song}


def replace_track(snapshot: Snapshot, index: int, **changes: Any) -> Snapshot:
    tracks = tuple(replace(track, **changes) if track.index == index else track for track in snapshot.tracks)
    return replace(snapshot, tracks=tracks, taken_at=time.time())


def replace_param(snapshot: Snapshot, path: str, payload: Mapping[str, Any]) -> Snapshot:
    changed_tracks: list[Track] = []
    for track in snapshot.tracks:
        changed_devices: list[Device] = []
        for device in track.devices:
            params = tuple(
                param_from_payload(param.index, payload) if param.path == path else param
                for param in device.params
            )
            changed_devices.append(replace(device, params=params))
        changed_tracks.append(replace(track, devices=tuple(changed_devices)))
    return replace(snapshot, tracks=tuple(changed_tracks), taken_at=time.time())
