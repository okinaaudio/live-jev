# Live Jev ⇄ Ableton Live（Remote Script）。
#
# Purpose: access the browser for plug-in listing and loading from Python inside Live,
# because the Max for Live bridge cannot reach it. Only the following commands are accepted.
#
#   ping          → pong
#   list_plugins  -> list browser entries under Plug-ins / Instruments / Audio Effects / MIDI Effects
#   load          -> select the requested track and load one item with an exact name match
#   add_track     -> add one MIDI/audio track to the right of the selected track, matching Live's Insert Track position
#                    Leave the default name unchanged, so loading an instrument can rename it. Load device if provided. One undo reverts it.
#   clip_notes    -> transform notes in the open clip or specified slot
#                    (quantize / legato / transpose / velocity / duplicate_loop). One undo reverts it.
#
# Newline-delimited JSON over TCP at 127.0.0.1:9140, supporting both one command per connection and persistent connections. Follows other Remote Scripts.
# Write entries prefixed with "LiveJev:" to Live's log at ~/Library/Preferences/Ableton/Live 12.x/Log.txt.

import json
import math
import socket

import Live
from _Framework.ControlSurface import ControlSurface

from .lom_protocol import NATIVE_DEVICES, require_allowed, resolve_lom_path
from .socket_pump import SocketPump

SOCKET_HOST = "127.0.0.1"
SOCKET_PORT = 9140
BUFFER_SIZE = 65536
MAX_ITEMS = 5000
SECTIONS = ("plugins", "instruments", "audio_effects", "midi_effects")


def _safe(read, default):
# Live properties raise RuntimeError, not AttributeError, for track types that do not support them,
# such as crossfade_assign on the master, arm on group tracks, and fold_state on tracks that cannot fold.
    try:
        return read()
    except Exception:
        return default


class LiveJev(ControlSurface):
    def __init__(self, c_instance):
        super().__init__(c_instance)
        self._running = False
        self._timer = None
        self._last_socket_error = None
        self._catalog = None
        self._pump = SocketPump(self._create_listener, self._handle_command, on_error=self._socket_error)
        self._start_polling()
        self.log_message("LiveJev: started, listening on port %d" % SOCKET_PORT)

# ---- Network listener ---------------------------------------------------------

    def _start_polling(self):
        self._running = True
        try:
            self._timer = Live.Base.Timer(callback=self._poll, interval=10, repeat=True)
            self._timer.start()
        except Exception:
            self._timer = None
            self.schedule_message(1, self._poll_and_rearm)

    def _poll(self):
        if self._running:
            self._pump.poll()

    def _poll_and_rearm(self):
        if not self._running or self._timer is not None:
            return
        self._poll()
        if self._running and self._timer is None:
            self.schedule_message(1, self._poll_and_rearm)

    def _create_listener(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((SOCKET_HOST, SOCKET_PORT))
            server.listen(5)
            server.setblocking(False)
            self._last_socket_error = None
            return server
        except Exception:
            server.close()
            raise

    def _socket_error(self, error):
        message = str(error)
        if message != self._last_socket_error:
            self.log_message("LiveJev socket error: %s" % message)
            self._last_socket_error = message

    def _handle_command(self, raw):
        try:
            cmd = json.loads(raw)
        except Exception:
            return json.dumps({"ok": False, "error": "invalid_json"})
        action = cmd.get("action", "")
        try:
            if action == "ping":
                answer = {"ok": True, "message": "pong", "version": "0.16"}
            elif action == "bridge":
                request = cmd.get("request")
                ops = cmd.get("ops")
                if not isinstance(request, str) or not isinstance(ops, list):
                    answer = {"ok": False, "error": "invalid_bridge"}
                else:
                    answer = self._run_bridge(request, ops)
            elif action in ("lom_get", "lom_set", "lom_call", "param_set"):
                op = dict(cmd)
                op["op"] = action
                answer = self._execute_op(op)
            elif action == "snapshot":
                answer = {"ok": True, "snapshot": self._build_snapshot()}
            elif action == "list_plugins":
                answer = self._list_plugins(bool(cmd.get("refresh", False)))
            elif action == "load":
                name = str(cmd.get("name", "")).strip()
                uri = str(cmd.get("uri", "")).strip()
                track_index = cmd.get("track_index", None)
                answer = self._load(name, uri, track_index) if name or uri else {"ok": False, "error": "no_name"}
            elif action == "add_track":
                answer = self._add_track(cmd)
            elif action == "clip_notes":
                answer = self._clip_notes(cmd)
            else:
                answer = {"ok": False, "error": "unknown_action"}
        except Exception as error:
            answer = {"ok": False, "error": str(error)}
        return json.dumps(answer, ensure_ascii=False)

    def _run_bridge(self, request, ops):
        results = []
        for op in ops:
            if not isinstance(op, dict):
                return {"ok": False, "request": request, "error": "invalid_operation"}
            result = self._execute_op(op)
            if not result.get("ok"):
                return {"ok": False, "request": request, "error": result.get("error", "operation_failed")}
            results.append(result)
        return {"ok": True, "request": request, "results": results}

    def _execute_op(self, op):
        try:
            name = str(op.get("op", ""))
            if name in ("lom_get", "lom_set", "lom_call", "param_set"):
                return self._execute_lom(name, op)
            if name == "session_context":
                return {"ok": True, "value": self._session_context()}
            if name == "children":
                return {"ok": True, "value": self._children(str(op.get("path", "")), str(op.get("child", "")))}
            if name == "device_list":
                return {"ok": True, "value": self._device_list(str(op.get("target", "")))}
            if name == "device_parameters":
                return {"ok": True, "value": self._device_parameters(str(op.get("path", "")))}
            if name == "mixer_status":
                return {"ok": True, "value": self._mixer_status(str(op.get("target", "")))}
            if name == "tempo":
                value = float(op.get("value"))
                if not math.isfinite(value):
                    raise ValueError("invalid_value")
                self.song().tempo = value
                return {"ok": True, "value": value}
            if name == "rename_track":
                return self._rename_track(op)
            if name == "add_track":
                return self._bridge_add_track(op)
            if name == "insert_device":
                return self._insert_device(op)
            if name == "ping":
                return {"ok": True, "value": None}
            return {"ok": False, "error": "unknown_operation"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _execute_lom(self, name, op):
        path = str(op.get("path", ""))
        member = op.get("prop") if name in ("lom_get", "lom_set") else op.get("method")
        require_allowed(name, path, None if member is None else str(member))
        target = resolve_lom_path(self.song(), path)
        if name == "lom_get":
            return {"ok": True, "value": getattr(target, str(member))}
        if name == "lom_set":
            value = self._coerce_set_value(str(member), op.get("value"))
            setattr(target, str(member), value)
            return {"ok": True, "value": {"ok": True}}
        if name == "lom_call":
            args = op.get("args")
            if not isinstance(args, list):
                raise ValueError("invalid_args")
            if str(member) != "str_for_value" and args:
                raise ValueError("invalid_args")
            if str(member) == "str_for_value" and len(args) != 1:
                raise ValueError("invalid_args")
            return {"ok": True, "value": getattr(target, str(member))(*args)}
        value = float(op.get("value"))
        if not math.isfinite(value):
            raise ValueError("invalid_value")
        target.value = value
        return {"ok": True, "value": self._parameter_payload(target, path)}

    def _coerce_set_value(self, prop, raw):
        if prop in ("loop", "metronome", "session_record", "overdub", "mute", "solo", "arm", "fold_state", "looping", "warping"):
            if raw not in (0, 1, False, True):
                raise ValueError("invalid_value")
            return bool(raw)
        if prop == "current_monitoring_state":
            value = int(raw)
            if value not in (0, 1, 2):
                raise ValueError("invalid_value")
            return value
        if prop == "pitch_coarse":
            value = int(raw)
            if value < -48 or value > 48:
                raise ValueError("invalid_value")
            return value
        value = float(raw)
        if not math.isfinite(value) or value < 0 or (prop == "gain" and value > 1):
            raise ValueError("invalid_value")
        return value

    def _rename_track(self, op):
        index = int(op.get("index"))
        name = str(op.get("name", ""))
        tracks = list(self.song().tracks)
        if index < 0 or index >= len(tracks) or not name.strip() or len(name) > 64 or any(ord(ch) < 32 for ch in name):
            return {"ok": False, "error": "invalid_track"}
        tracks[index].name = name
        return {"ok": True, "value": None}

    def _bridge_add_track(self, op):
        kind = str(op.get("kind", ""))
        name = op.get("name")
        if kind not in ("midi", "audio"):
            return {"ok": False, "error": "invalid_track_kind"}
        shown = None if name is None else str(name)
        if shown is not None and (not shown.strip() or len(shown) > 64 or any(ord(ch) < 32 for ch in shown)):
            return {"ok": False, "error": "invalid_name"}
        song = self.song()
        track = song.create_audio_track(-1) if kind == "audio" else song.create_midi_track(-1)
        if shown is not None:
            track.name = shown
        return {"ok": True, "value": None}

    def _insert_device(self, op):
        path = str(op.get("path", ""))
        name = str(op.get("name", ""))
        position = str(op.get("position", ""))
        if name not in NATIVE_DEVICES or (position and not position.isdigit()):
            return {"ok": False, "error": "invalid_device"}
        prefix = "live_set tracks "
        if not path.startswith(prefix) or not path[len(prefix):].isdigit():
            return {"ok": False, "error": "invalid_track"}
        track_index = int(path[len(prefix):])
        tracks = list(self.song().tracks)
        if track_index < 0 or track_index >= len(tracks):
            return {"ok": False, "error": "track_not_found"}
        track = tracks[track_index]
        before = list(track.devices)
        insertion = len(before) if position == "" else int(position)
        if insertion < 0 or insertion > len(before):
            return {"ok": False, "error": "invalid_position"}
        if insertion > 0 and before:
            try:
                track.view.selected_device = before[insertion - 1]
            except Exception:
                pass
        answer = self._load(name, "", track_index)
        if not answer.get("ok"):
            return answer
        if position != "":
            after = list(track.devices)
            added = [device for device in after if device not in before]
            if added:
                try:
                    self.song().move_device(added[0], track, insertion)
                except Exception:
                    try:
                        self.song().undo()
                    except Exception:
                        pass
                    return {"ok": False, "error": "insert_position_failed"}
        return {"ok": True, "value": None}

    def _object_id(self, value):
        try:
            return int(getattr(value, "_live_ptr", 0))
        except Exception:
            return 0

    def _track_path(self, track):
        song = self.song()
        tracks = list(song.tracks)
        if track in tracks:
            return "live_set tracks %d" % tracks.index(track)
        returns = list(song.return_tracks)
        if track in returns:
            return "live_set return_tracks %d" % returns.index(track)
        if track == song.master_track:
            return "live_set master_track"
        return ""

    def _parameter_payload(self, parameter, path, include_display=False):
        value = float(parameter.value)
        answer = {
            "path": path,
            "id": self._object_id(parameter),
            "name": str(parameter.name),
            "value": value,
            "min": float(parameter.min),
            "max": float(parameter.max),
            "is_quantized": bool(parameter.is_quantized),
        }
        if include_display:
            try:
                answer["display"] = str(parameter.str_for_value(value))
            except Exception:
                answer["display"] = "%g" % value
        return answer

    def _mixer_payload(self, track, path):
        mixer = track.mixer_device
        parameters = {
            "volume": self._parameter_payload(mixer.volume, path + " mixer_device volume", True),
        }
        if path != "live_set master_track":
            parameters["panning"] = self._parameter_payload(mixer.panning, path + " mixer_device panning", True)
        return {
            "track_path": path,
            "mixer_path": path + " mixer_device",
            "mixer": {
                "crossfade_assign": _safe(lambda: int(mixer.crossfade_assign), 0),
                "panning_mode": _safe(lambda: int(mixer.panning_mode), 0),
            },
            "parameters": parameters,
        }

    def _device_header(self, device, path, index):
        return {
            "index": index,
            "path": path,
            "id": self._object_id(device),
            "name": str(device.name),
            "class_name": str(getattr(device, "class_name", "")),
        }

    def _device_parameters(self, path):
        device = resolve_lom_path(self.song(), path)
        parameters = []
        for index, parameter in enumerate(list(device.parameters)):
            parameters.append(self._parameter_payload(parameter, path + " parameters %d" % index))
        answer = self._device_header(device, path, int(path.rsplit(" devices ", 1)[1]))
        answer["parameters"] = parameters
        return answer

    def _session_context(self):
        song = self.song()
        song_fields = (
            "tempo", "is_playing", "loop", "metronome", "session_record",
            "overdub", "current_song_time", "signature_numerator",
            "signature_denominator", "record_mode", "clip_trigger_quantization",
        )
        values = {name: getattr(song, name) for name in song_fields}
        selected = song.view.selected_track
        selected_path = self._track_path(selected)
        return {
            "song": values,
            "selected": {"track": {
                "path": selected_path,
                "name": str(getattr(selected, "name", "")),
            }},
            "counts": {
                "tracks": len(song.tracks),
                "return_tracks": len(song.return_tracks),
                "scenes": len(song.scenes),
            },
        }

    def _children(self, path, child):
        song = self.song()
        if path == "live_set" and child in ("tracks", "return_tracks", "scenes"):
            collection = list(getattr(song, child))
            base = "live_set " + child
        elif child == "clip_slots" and path.startswith("live_set tracks "):
            collection = list(resolve_lom_path(song, path).clip_slots)
            base = path + " clip_slots"
        else:
            raise ValueError("not_allowed")
        answer = []
        for index, item in enumerate(collection):
            entry = {
                "index": index,
                "id": self._object_id(item),
                "path": base + " %d" % index,
                "name": str(getattr(item, "name", "")),
                "type": str(getattr(item, "type", "")),
            }
            answer.append(entry)
        return answer

    def _device_list(self, target):
        song = self.song()
        tracks = list(song.tracks)
        if target != "all":
            index = int(target)
            if index < 0 or index >= len(tracks):
                raise ValueError("track_not_found")
            selected = [(index, tracks[index])]
        else:
            selected = list(enumerate(tracks))
        items = []
        for index, track in selected:
            track_path = "live_set tracks %d" % index
            devices = [
                self._device_header(device, track_path + " devices %d" % device_index, device_index)
                for device_index, device in enumerate(list(track.devices))
            ]
            items.append({
                "track": {"index": index, "path": track_path, "name": str(track.name)},
                "track_path": track_path,
                "devices": devices,
            })
        return {"target": target, "tracks": items}

    def _mixer_status(self, target):
        song = self.song()
        if target == "master":
            track = song.master_track
            path = "live_set master_track"
        else:
            index = int(target)
            tracks = list(song.tracks)
            if index < 0 or index >= len(tracks):
                raise ValueError("track_not_found")
            track = tracks[index]
            path = "live_set tracks %d" % index
        return self._mixer_payload(track, path)

    def _clip_payload(self, clip, path, slot):
        props = {}
        for name in ("is_playing", "is_triggered", "looping", "length", "warping", "pitch_coarse", "gain", "gain_display_string"):
            try:
                props[name] = getattr(clip, name)
            except Exception:
                pass
        return {"slot": slot, "path": path, "name": str(clip.name), "props": props}

    def _snapshot_steps(self):
        song = self.song()
        context = self._session_context()
        payload = {
            "schema": 1,
            "song": context["song"],
            "selected_track_path": context["selected"]["track"]["path"],
            "tracks": [],
            "master": {"volume": self._mixer_payload(song.master_track, "live_set master_track")["parameters"]["volume"]},
            "scenes": [],
            "returns": [],
        }
        yield None
        for index, scene in enumerate(list(song.scenes)):
            payload["scenes"].append({
                "index": index, "path": "live_set scenes %d" % index,
                "name": str(scene.name),
            })
            yield None
        for index, track in enumerate(list(song.return_tracks)):
            payload["returns"].append({
                "index": index, "path": "live_set return_tracks %d" % index,
                "name": str(track.name),
            })
            yield None
        for index, track in enumerate(list(song.tracks)):
            path = "live_set tracks %d" % index
            mixer = self._mixer_payload(track, path)["parameters"]
            item = {
                "index": index,
                "path": path,
                "name": str(track.name),
                "mute": bool(track.mute),
                "solo": bool(track.solo),
                "arm": _safe(lambda track=track: bool(track.arm), False),
                "current_monitoring_state": _safe(lambda track=track: int(track.current_monitoring_state), 1),
                "fold_state": _safe(lambda track=track: bool(track.fold_state), False),
                "mixer": {"volume": mixer["volume"], "panning": mixer["panning"]},
                "sends": [float(parameter.value) for parameter in list(track.mixer_device.sends)],
                "devices": [],
                "clips": [],
            }
            yield None
            for device_index, device in enumerate(list(track.devices)):
                device_path = path + " devices %d" % device_index
                device_item = self._device_header(device, device_path, device_index)
                device_item["parameters"] = []
                for parameter_index, parameter in enumerate(list(device.parameters)):
                    parameter_path = device_path + " parameters %d" % parameter_index
                    raw = self._parameter_payload(parameter, parameter_path)
                    raw["index"] = parameter_index
                    device_item["parameters"].append(raw)
                    yield None
                item["devices"].append(device_item)
            for slot_index, slot in enumerate(list(track.clip_slots)[:64]):
                if slot.has_clip:
                    clip_path = path + " clip_slots %d clip" % slot_index
                    item["clips"].append(self._clip_payload(slot.clip, clip_path, slot_index))
                yield None
            payload["tracks"].append(item)
        return payload

    def _build_snapshot(self):
        steps = self._snapshot_steps()
        while True:
            try:
                next(steps)
            except StopIteration as done:
                return done.value

# ---- Browser -----------------------------------------------------------------

    def _browser(self):
        return Live.Application.get_application().browser

    def _walk(self, item, section, out, depth=0):
        if len(out) >= MAX_ITEMS or depth > 8:
            return
        try:
            children = list(item.iter_children) if hasattr(item, "iter_children") else list(item.children)
        except Exception:
            children = []
        for child in children:
            try:
                loadable = bool(getattr(child, "is_loadable", False))
                is_device = bool(getattr(child, "is_device", False))
                name = str(getattr(child, "name", ""))
                uri = str(getattr(child, "uri", ""))
            except Exception:
                continue
            if loadable and (is_device or section == "plugins"):
                out.append({"name": name, "uri": uri, "section": section})
            if getattr(child, "is_folder", False) or not loadable:
                self._walk(child, section, out, depth + 1)

    def _list_plugins(self, refresh):
        if self._catalog is not None and not refresh:
            return {"ok": True, "cached": True, "count": len(self._catalog), "items": self._catalog}
        browser = self._browser()
        items = []
        for section in SECTIONS:
            root = getattr(browser, section, None)
            if root is None:
                continue
            self._walk(root, section, items)
        self._catalog = items
        self.log_message("LiveJev: catalog %d items" % len(items))
        return {"ok": True, "cached": False, "count": len(items), "items": items}

    def _find_item(self, name, uri):
        if self._catalog is None:
            self._list_plugins(False)
        wanted = name.casefold().replace(" ", "")
        exact = []
        partial = []
        for entry in self._catalog:
            if uri and entry["uri"] == uri:
                return entry
            key = entry["name"].casefold().replace(" ", "")
            if key == wanted:
                exact.append(entry)
            elif wanted and wanted in key:
                partial.append(entry)
        if exact:
            return exact[0]
        if len(partial) == 1:
            return partial[0]
        return None

    def _resolve_browser_item(self, entry):
        browser = self._browser()
        section = entry["section"]
        root = getattr(browser, section, None)
        if root is None:
            return None
        stack = [root]
        while stack:
            item = stack.pop()
            try:
                children = list(item.iter_children) if hasattr(item, "iter_children") else list(item.children)
            except Exception:
                children = []
            for child in children:
                try:
                    if str(getattr(child, "uri", "")) == entry["uri"]:
                        return child
                except Exception:
                    continue
                stack.append(child)
        return None

    def _load(self, name, uri, track_index):
        song = self.song()
        tracks = list(song.tracks)
        if track_index is not None:
            index = int(track_index)
            if index < 0 or index >= len(tracks):
                return {"ok": False, "error": "track_not_found"}
            song.view.selected_track = tracks[index]
        entry = self._find_item(name, uri)
        if entry is None:
            return {"ok": False, "error": "plugin_not_found", "name": name}
        item = self._resolve_browser_item(entry)
        if item is None:
            return {"ok": False, "error": "browser_item_missing", "name": entry["name"]}
        browser = self._browser()
    # Calling load_item during hot-swap, opened with a device's Q button, replaces that device. Never replace it.
        hotswap = getattr(browser, "hotswap_target", None)
        if hotswap is not None:
            return {"ok": False, "error": "hotswap_active", "name": entry["name"]}
    # Let Live choose the insertion point, as it does on a browser double-click: effects follow the selected device, and instruments replace the existing instrument.
        target = song.view.selected_track
        before = [str(d.name) for d in target.devices]
        browser.load_item(item)
        after = [str(d.name) for d in target.devices]
        selected_index = tracks.index(target) if target in tracks else None
        self.log_message("LiveJev: loaded %s on track %s (%d -> %d devices)" % (entry["name"], selected_index, len(before), len(after)))
        return {"ok": True, "name": entry["name"], "uri": entry["uri"], "track_index": selected_index, "devices_before": before, "devices_after": after}

    def _add_track(self, cmd):
        song = self.song()
        kind = str(cmd.get("kind", "midi"))
        name = str(cmd.get("name") or "").strip()
        device = str(cmd.get("device") or "").strip()
        browser = self._browser()
        item = None
        if device:
            if getattr(browser, "hotswap_target", None) is not None:
                return {"ok": False, "error": "hotswap_active", "name": device}
            entry = self._find_item(device, "")
            if entry is None:
                return {"ok": False, "error": "plugin_not_found", "name": device}
            item = self._resolve_browser_item(entry)
            if item is None:
                return {"ok": False, "error": "browser_item_missing", "name": entry["name"]}
        tracks = list(song.tracks)
        selected = song.view.selected_track
        index = tracks.index(selected) + 1 if selected in tracks else -1
        if index >= len(tracks):
            index = -1
        song.begin_undo_step()
        try:
            track = song.create_audio_track(index) if kind == "audio" else song.create_midi_track(index)
            if name:
                track.name = name
            song.view.selected_track = track
            if item is not None:
                browser.load_item(item)
            new_index = list(song.tracks).index(track)
            after = [str(d.name) for d in track.devices]
            self.log_message("LiveJev: added %s track at %d (%s)" % (kind, new_index, ", ".join(after)))
            return {"ok": True, "track_index": new_index, "track": str(track.name), "devices_after": after}
        finally:
            song.end_undo_step()

# ---- Clip notes --------------------------------------------------------------

# Same order as Live's RecordingQuantization, used as the first clip.quantize argument.
    GRIDS = {"1/4": 1, "1/8": 2, "1/8t": 3, "1/8+t": 4, "1/16": 5, "1/16t": 6, "1/16+t": 7, "1/32": 8}

    def _target_clip(self, track_index, slot_index):
        song = self.song()
        if track_index is not None and slot_index is not None:
            tracks = list(song.tracks)
            ti, si = int(track_index), int(slot_index)
            if ti < 0 or ti >= len(tracks):
                return None, None
            slots = list(tracks[ti].clip_slots)
            if si < 0 or si >= len(slots) or not slots[si].has_clip:
                return None, tracks[ti]
            return slots[si].clip, tracks[ti]
        clip = song.view.detail_clip
        if clip is None:
            slot = song.view.highlighted_clip_slot
            clip = slot.clip if slot is not None and slot.has_clip else None
        if clip is None:
            return None, None
        owner = None
        try:
            owner = clip.canonical_parent.canonical_parent if clip.is_session_clip else clip.canonical_parent
        except Exception:
            owner = None
        return clip, owner

    def _clip_notes(self, cmd):
        op = str(cmd.get("op", ""))
        clip, owner = self._target_clip(cmd.get("track_index"), cmd.get("slot_index"))
        expected_name = cmd.get("track_name")
        if expected_name is not None and cmd.get("track_index") is not None:
            if owner is None or str(getattr(owner, "name", "")) != str(expected_name):
                return {"ok": False, "error": "track_changed"}
        if clip is None:
            return {"ok": False, "error": "no_clip"}
        info = {"ok": True, "op": op, "clip": str(clip.name), "track": str(getattr(owner, "name", "")), "is_midi": bool(clip.is_midi_clip)}
        song = self.song()
        song.begin_undo_step()
        try:
            if op == "duplicate_loop":
                if not clip.is_midi_clip:
                    return {"ok": False, "error": "midi_only"}
                clip.duplicate_loop()
                info["length"] = float(clip.length)
                return info
            if op == "quantize":
                grid = self.GRIDS.get(str(cmd.get("grid", "1/16")).lower())
                if grid is None:
                    return {"ok": False, "error": "bad_grid"}
                amount = max(0.0, min(1.0, float(cmd.get("amount", 1.0))))
                clip.quantize(grid, amount)
                info.update({"grid": str(cmd.get("grid", "1/16")), "amount": amount})
                return info
            if op == "transpose" and not clip.is_midi_clip:
                semitones = int(cmd.get("semitones", 0))
                before = int(clip.pitch_coarse)
                if before + semitones < -48 or before + semitones > 48:
                    return {"ok": False, "error": "out_of_range"}
                clip.pitch_coarse = before + semitones
                info.update({"semitones": semitones, "pitch_coarse": int(clip.pitch_coarse)})
                return info
            if not clip.is_midi_clip:
                return {"ok": False, "error": "midi_only"}
            notes = clip.get_notes_extended(0, 128, -100000.0, 200000.0)
            count = len(notes)
            info["count"] = count
            if count == 0:
                return {"ok": False, "error": "no_notes"}
            if op == "transpose":
                semitones = int(cmd.get("semitones", 0))
                if any(n.pitch + semitones < 0 or n.pitch + semitones > 127 for n in notes):
                    return {"ok": False, "error": "out_of_range"}
                for n in notes:
                    n.pitch = n.pitch + semitones
                info["semitones"] = semitones
            elif op == "velocity":
                factor = cmd.get("factor")
                value = cmd.get("value")
                for n in notes:
                    target = float(value) if value is not None else float(n.velocity) * float(factor if factor is not None else 1.0)
                    n.velocity = max(1.0, min(127.0, target))
                info.update({"factor": factor, "value": value})
            elif op == "legato":
                starts = sorted(set(round(float(n.start_time), 6) for n in notes))
                end = float(clip.loop_end) if clip.looping else float(clip.end_marker)
                for n in notes:
                    start = round(float(n.start_time), 6)
                    later = [t for t in starts if t > start + 1e-6]
                    until = later[0] if later else end
                    if until - start > 1e-3:
                        n.duration = until - start
            else:
                return {"ok": False, "error": "unknown_op"}
            clip.apply_note_modifications(notes)
            return info
        finally:
            song.end_undo_step()

    def disconnect(self):
        self._running = False
        timer = self._timer
        self._timer = None
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        self._pump.close()
        super().disconnect()
