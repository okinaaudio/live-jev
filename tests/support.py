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
