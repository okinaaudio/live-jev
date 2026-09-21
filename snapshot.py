"""Convert the current Live state into immutable data used for decisions and actions."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
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

    @property
    def ref(self) -> TargetRef:
        return TargetRef(TargetKind.TRACK, self.index)

    @property
    def capabilities(self) -> frozenset[TargetCapability]:
        return TRACK_CAPABILITIES


class TargetKind(Enum):
    TRACK = "track"
    RETURN = "return"
    MASTER = "master"


class TargetCapability(Enum):
    VOLUME = "volume"
    PAN = "pan"
    MUTE = "mute"
    SOLO = "solo"
    ARM = "arm"
    MONITOR = "monitor"
    FOLD = "fold"
    CLIPS = "clips"
    SENDS = "sends"
    RENAME = "rename"
    DEVICES = "devices"
    INSERT_DEVICE = "insert_device"


@dataclass(frozen=True)
class TargetRef:
    kind: TargetKind
    index: int | None = None

    @property
    def key(self) -> str:
        if self.kind is TargetKind.MASTER:
            return "master"
        prefix = "t" if self.kind is TargetKind.TRACK else "r"
        return f"{prefix}{self.index}"


TRACK_CAPABILITIES = frozenset({
    TargetCapability.VOLUME, TargetCapability.PAN, TargetCapability.MUTE,
    TargetCapability.SOLO, TargetCapability.ARM, TargetCapability.MONITOR,
    TargetCapability.FOLD, TargetCapability.CLIPS, TargetCapability.SENDS, TargetCapability.RENAME,
    TargetCapability.DEVICES, TargetCapability.INSERT_DEVICE,
})
RETURN_CAPABILITIES = frozenset({
    TargetCapability.VOLUME, TargetCapability.PAN, TargetCapability.MUTE,
    TargetCapability.SOLO, TargetCapability.RENAME, TargetCapability.DEVICES,
    TargetCapability.INSERT_DEVICE,
})
MASTER_CAPABILITIES = frozenset({
    TargetCapability.VOLUME, TargetCapability.DEVICES,
    TargetCapability.INSERT_DEVICE,
})


@dataclass(frozen=True)
class ReturnTrack:
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
    capabilities: frozenset[TargetCapability] = RETURN_CAPABILITIES

    @property
    def ref(self) -> TargetRef:
        return TargetRef(TargetKind.RETURN, self.index)

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class MasterTrack:
    name: str
    volume: float
    volume_display: str
    devices: tuple[Device, ...]
    path: str = "live_set master_track"
    capabilities: frozenset[TargetCapability] = MASTER_CAPABILITIES

    @property
    def ref(self) -> TargetRef:
        return TargetRef(TargetKind.MASTER)


AddressableTarget = Track | ReturnTrack | MasterTrack


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
    returns: tuple[ReturnTrack, ...] = ()
    master: MasterTrack | None = None

    def __post_init__(self) -> None:
        coerced = tuple(
            item if isinstance(item, ReturnTrack) else ReturnTrack(
                index=index,
                name=str(item),
                volume=0.0,
                volume_display="0",
                pan=0.0,
                pan_display="0",
                mute=False,
                solo=False,
                devices=(),
                path=f"live_set return_tracks {index}",
            )
            for index, item in enumerate(self.returns)
        )
        object.__setattr__(self, "returns", coerced)
        if self.master is None:
            object.__setattr__(self, "master", MasterTrack(
                name="Master",
                volume=self.master_volume,
                volume_display=self.master_display,
                devices=(),
            ))

    @property
    def addressable_targets(self) -> tuple[AddressableTarget, ...]:
        return self.tracks + self.returns + ((self.master,) if self.master is not None else ())

    def target(self, ref: int | str | TargetRef) -> AddressableTarget | None:
        if isinstance(ref, int):
            return next((track for track in self.tracks if track.index == ref), None)
        if ref == "master":
            return self.master
        if isinstance(ref, TargetRef):
            if ref.kind is TargetKind.TRACK:
                return next((track for track in self.tracks if track.index == ref.index), None)
            if ref.kind is TargetKind.RETURN:
                return next((track for track in self.returns if track.index == ref.index), None)
            return self.master
        return None


def addressable_targets(snapshot: Snapshot) -> tuple[AddressableTarget, ...]:
    return snapshot.addressable_targets


def target_ref(target: AddressableTarget) -> int | TargetRef:
    if isinstance(target, Track):
        return target.index
    return target.ref


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


def _devices_from_script(raw_devices: Any, owner_path: str) -> tuple[Device, ...]:
    if not isinstance(raw_devices, list):
        raise ValueError("invalid snapshot devices")
    devices: list[Device] = []
    for position, raw_device in enumerate(raw_devices):
        if not isinstance(raw_device, Mapping):
            raise ValueError("invalid snapshot device")
        index = int(raw_device.get("index", position))
        path = str(raw_device.get("path") or f"{owner_path} devices {index}")
        raw_parameters = raw_device.get("parameters")
        if not isinstance(raw_parameters, list):
            raise ValueError("invalid snapshot parameters")
        parameters = tuple(
            param_from_payload(
                int(raw_parameter.get("index", parameter_position)),
                {**raw_parameter, "path": str(raw_parameter.get("path") or f"{path} parameters {parameter_position}")},
            )
            for parameter_position, raw_parameter in enumerate(raw_parameters)
            if isinstance(raw_parameter, Mapping)
        )
        devices.append(Device(index, str(raw_device.get("name") or f"Device {index}"), parameters, path))
    return tuple(devices)


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

        devices = _devices_from_script(raw_devices, path)

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
            devices=devices,
            path=path,
            arm=_boolean(raw_track.get("arm")),
            current_monitoring_state=int(raw_track.get("current_monitoring_state", 1)),
            fold_state=_boolean(raw_track.get("fold_state")),
            clips=clips,
            sends=tuple(_number(value) for value in raw_sends),
        ))

    raw_master_mixer = raw_master.get("mixer")
    raw_master_volume = raw_master_mixer.get("volume") if isinstance(raw_master_mixer, Mapping) else raw_master.get("volume")
    if not isinstance(raw_master_volume, Mapping):
        raise ValueError("invalid snapshot master")
    master_volume = _number(raw_master_volume.get("value"))
    master_path = str(raw_master.get("path") or "live_set master_track")
    master_devices = _devices_from_script(raw_master.get("devices", []), master_path)
    scenes = tuple(
        Scene(
            index=int(raw_scene.get("index", position)),
            name=str(raw_scene.get("name") or f"Scene {position + 1}"),
            path=str(raw_scene.get("path") or f"live_set scenes {position}"),
        )
        for position, raw_scene in enumerate(raw_scenes)
        if isinstance(raw_scene, Mapping)
    )
    returns: list[ReturnTrack] = []
    for position, raw_return in enumerate(raw_returns):
        if not isinstance(raw_return, Mapping):
            raise ValueError("invalid snapshot return")
        index = int(raw_return.get("index", position))
        path = str(raw_return.get("path") or f"live_set return_tracks {index}")
        raw_mixer = raw_return.get("mixer")
        raw_devices = raw_return.get("devices", [])
        if not isinstance(raw_mixer, Mapping):
            # Schema-1 compatibility with the former name-only return payload.
            returns.append(ReturnTrack(index, str(raw_return.get("name") or f"Return {position + 1}"), 0.0, "0", 0.0, "0", False, False, (), path))
            continue
        raw_volume = raw_mixer.get("volume")
        raw_pan = raw_mixer.get("panning")
        if not isinstance(raw_volume, Mapping) or not isinstance(raw_pan, Mapping):
            raise ValueError("invalid snapshot return mixer")
        volume = _number(raw_volume.get("value"))
        pan = _number(raw_pan.get("value"))
        returns.append(ReturnTrack(
            index=index,
            name=str(raw_return.get("name") or f"Return {position + 1}"),
            volume=volume,
            volume_display=str(raw_volume.get("display") or f"{volume:g}"),
            pan=pan,
            pan_display=str(raw_pan.get("display") or f"{pan:g}"),
            mute=_boolean(raw_return.get("mute")),
            solo=_boolean(raw_return.get("solo")),
            devices=_devices_from_script(raw_devices, path),
            path=path,
        ))
    master = MasterTrack(
        name=str(raw_master.get("name") or "Master"),
        volume=master_volume,
        volume_display=str(raw_master_volume.get("display") or f"{master_volume:g}"),
        devices=master_devices,
        path=master_path,
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
        returns=tuple(returns),
        master=master,
    )


SONG_FIELDS = ("loop", "metronome", "session_record", "overdub", "current_song_time", "signature_numerator", "signature_denominator", "record_mode", "clip_trigger_quantization")


def song_fields(song: Mapping[str, Any]) -> dict[str, Any]:
    return {key: song[key] for key in SONG_FIELDS if key in song}


def replace_track(snapshot: Snapshot, index: int, **changes: Any) -> Snapshot:
    tracks = tuple(replace(track, **changes) if track.index == index else track for track in snapshot.tracks)
    return replace(snapshot, tracks=tracks, taken_at=time.time())


def replace_target(snapshot: Snapshot, ref: int | str | TargetRef, **changes: Any) -> Snapshot:
    target = snapshot.target(ref)
    if isinstance(target, Track):
        return replace_track(snapshot, target.index, **changes)
    if isinstance(target, ReturnTrack):
        returns = tuple(replace(item, **changes) if item.index == target.index else item for item in snapshot.returns)
        return replace(snapshot, returns=returns, taken_at=time.time())
    if isinstance(target, MasterTrack):
        master = replace(target, **changes)
        legacy = {}
        if "volume" in changes:
            legacy["master_volume"] = changes["volume"]
        if "volume_display" in changes:
            legacy["master_display"] = changes["volume_display"]
        return replace(snapshot, master=master, taken_at=time.time(), **legacy)
    return snapshot


def replace_param(snapshot: Snapshot, path: str, payload: Mapping[str, Any]) -> Snapshot:
    def changed_target(track: AddressableTarget) -> AddressableTarget:
        changed_devices: list[Device] = []
        for device in track.devices:
            params = tuple(
                param_from_payload(param.index, payload) if param.path == path else param
                for param in device.params
            )
            changed_devices.append(replace(device, params=params))
        return replace(track, devices=tuple(changed_devices))
    tracks = tuple(changed_target(track) for track in snapshot.tracks)
    returns = tuple(changed_target(track) for track in snapshot.returns)
    master = changed_target(snapshot.master) if snapshot.master is not None else None
    return replace(snapshot, tracks=tracks, returns=returns, master=master, taken_at=time.time())
