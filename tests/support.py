from __future__ import annotations

import time

from snapshot import Device, Param, Snapshot, Track


def sample_snapshot() -> Snapshot:
    dry_wet = Param(0, "Dry/Wet", 0.25, 0.0, 1.0, "25%", "live_set tracks 0 devices 0 parameters 0")
    reverb = Device(0, "Reverb", (dry_wet,), "live_set tracks 0 devices 0")
    return Snapshot(
        tracks=(
            Track(0, "Pad", 0.60, "-6.0 dB", 0.0, "C", False, False, (reverb,), "live_set tracks 0"),
            Track(1, "Bass", 0.55, "-8.0 dB", 0.0, "C", False, False, (), "live_set tracks 1"),
            Track(2, "Drums", 0.70, "-3.0 dB", 0.0, "C", False, False, (), "live_set tracks 2"),
        ),
        master_volume=0.75,
        master_display="-1.0 dB",
        tempo=120.0,
        playing=True,
        taken_at=time.time(),
    )


def choice(name: str, confidence: float = 0.95, **probabilities: float) -> dict[str, object]:
    return {"type": "choice", "choice": name, "confidence": confidence, "probabilities": probabilities or {name: confidence}}


def response(action: str, track: str = "none", step: str = "none", *, generation: float = 0.0, compound: float = 0.0, refers_previous: float = 0.0, param: str = "none", action_conf: float = 0.95, track_conf: float = 0.95, param_conf: float = 0.95) -> dict[str, object]:
    return {
        "answers": {
            "action": choice(action, action_conf, **{action: action_conf, "none": 1 - action_conf}),
            "track": choice(track, track_conf, **{track: track_conf, "none": 1 - track_conf}),
            "step": choice(step),
            "track_stated": {"type": "noul", "noul": 0.9 if track != "none" else 0.1},
            "needs_generation": {"type": "noul", "noul": generation},
            "compound": {"type": "noul", "noul": compound},
            "refers_previous": {"type": "noul", "noul": refers_previous},
            "param_t0": choice(param, param_conf, **{param: param_conf, "none": 1 - param_conf}),
        }
    }


class StatefulLive:
    """A bridge double that keeps track state, so tests can assert what Live would hold after writes and restores.
    It understands several get/set operations in one argument list, which is how batched reads are sent."""

    def __init__(self, names=("Pad", "Bass", "Drums"), selected=1, *, ignore_writes=False):
        from bridge_client import Ack, BridgeError, BridgeResult
        self._ack, self._error, self._result = Ack, BridgeError, BridgeResult
        self.names = dict(enumerate(names))
        self.selected = selected
        self.state = {(index, prop): False for index in self.names for prop in ("mute", "solo", "arm")}
        self.values = {(index, "volume"): (0.60, 0.55, 0.70)[index] if index < 3 else 0.5 for index in self.names}
        self.values.update({(index, "panning"): 0.0 for index in self.names})
        self.sends = {(index, 0): 0.0 for index in self.names}
        self.parameters = {"live_set tracks 0 devices 0 parameters 0": 0.25}
        self.device_names = {"live_set tracks 0 devices 0": "Reverb"}
        self.tempo = 120.0
        self.ignore_writes = ignore_writes
        self.faults = {}
        self.calls = []

    def inject_fault(self, operation, outcome="error", times=1):
        self.faults.setdefault(operation, []).extend([outcome] * times)

    def replace_track(self, index, name):
        self.names[index] = name

    def _fault(self, operation):
        queued = self.faults.get(operation)
        if not queued:
            return None
        outcome = queued.pop(0)
        if outcome == "error":
            raise self._error(f"injected {operation} failure")
        return outcome

    def run(self, arguments):
        arguments = list(arguments)
        self.calls.append(arguments)
        acks = []
        offset = 0
        while offset < len(arguments):
            flag = arguments[offset]
            if flag == "--write":
                offset += 1
            elif flag == "--api-get":
                path, prop, request = arguments[offset + 1:offset + 4]
                self._fault("get")
                if path == "live_set" and prop == "tempo":
                    payload = self.tempo
                elif "mixer_device sends" in path:
                    parts = path.split()
                    payload = self.sends[(int(parts[2]), int(parts[-1]))]
                elif " devices " in path and prop == "name":
                    payload = self.device_names[path]
                else:
                    index = int(path.split()[2])
                    payload = self.names[index] if prop == "name" else self.state[(index, prop)]
                acks.append(self._ack("api_get", request, payload, path, prop))
                offset += 4
            elif flag == "--api-set":
                path, prop, value, _request = arguments[offset + 1:offset + 5]
                self._fault("set")
                if not self.ignore_writes:
                    self.state[(int(path.split()[2]), prop)] = bool(int(float(value)))
                offset += 5
            elif flag == "--api-parameter-set":
                path, value, _request = arguments[offset + 1:offset + 4]
                self._fault("parameter_set")
                if not self.ignore_writes:
                    if "mixer_device volume" in path:
                        self.values[(int(path.split()[2]), "volume")] = float(value)
                    elif "mixer_device panning" in path:
                        self.values[(int(path.split()[2]), "panning")] = float(value)
                    elif "mixer_device sends" in path:
                        self.sends[(int(path.split()[2]), int(path.split()[-1]))] = float(value)
                    else:
                        self.parameters[path] = float(value)
                offset += 4
            elif flag == "--api-mixer-status":
                target, request = arguments[offset + 1:offset + 3]
                self._fault("mixer_status")
                index = int(target)
                payload = {"parameters": {
                    "volume": {"path": f"live_set tracks {index} mixer_device volume", "value": self.values[(index, "volume")]},
                    "panning": {"path": f"live_set tracks {index} mixer_device panning", "value": self.values[(index, "panning")]},
                }}
                acks.append(self._ack("api_mixer_status", request, payload, f"live_set tracks {index}"))
                offset += 3
            elif flag == "--api-device-parameters":
                path, request = arguments[offset + 1:offset + 3]
                self._fault("device_parameters")
                params = [{"path": param_path, "name": "Dry/Wet", "value": value, "min": 0.0, "max": 1.0} for param_path, value in self.parameters.items() if param_path.startswith(path + " parameters ")]
                acks.append(self._ack("api_device_parameters", request, {"parameters": params}, path))
                offset += 3
            elif flag == "--api-call":
                path, method, raw_args, request = arguments[offset + 1:offset + 5]
                self._fault("call")
                if method != "str_for_value":
                    raise self._error("StatefulLive does not model " + method)
                value = float(__import__("json").loads(raw_args)[0])
                acks.append(self._ack("api_call", request, f"{value:g}", path, method))
                offset += 5
            elif flag == "--tempo":
                self._fault("tempo")
                if not self.ignore_writes:
                    self.tempo = float(arguments[offset + 1])
                offset += 2
            elif flag == "--rename-track-index":
                index = int(arguments[offset + 1])
                if arguments[offset + 2] != "--rename-track-name":
                    raise self._error("missing rename value")
                self._fault("rename")
                if not self.ignore_writes:
                    self.names[index] = arguments[offset + 3]
                offset += 4
            elif flag == "--api-session-context":
                payload = {"song": {}, "selected": {"track": {"path": "live_set tracks {}".format(self.selected), "name": self.names[self.selected]}}}
                acks.append(self._ack("api_session_context", arguments[offset + 1], payload))
                offset += 2
            else:
                raise self._error("StatefulLive does not model " + flag)
        return self._result(tuple(acks), 1, 0, False)

    def flags(self, prop):
        return {self.names[index]: self.state[(index, prop)] for index in self.names}
