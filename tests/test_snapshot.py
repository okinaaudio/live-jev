from __future__ import annotations

import unittest

from bridge_client import Ack, BridgeResult
from daemon import SnapshotReader
from snapshot import build_snapshot, snapshot_from_script


class SnapshotTests(unittest.TestCase):
    def test_script_snapshot_uses_one_capability_call(self) -> None:
        expected = snapshot_from_script({
            "schema": 1,
            "song": {"tempo": 120, "is_playing": False},
            "tracks": [],
            "master": {"volume": {"value": 0.8, "display": "-2.0 dB"}},
            "scenes": [],
            "returns": [],
        }, taken_at=10)

        class ScriptBridge:
            def __init__(self) -> None:
                self.calls = 0

            def read_snapshot(self):
                self.calls += 1
                return expected, 7

            def run(self, _arguments):
                raise AssertionError("run should not be called")

        bridge = ScriptBridge()
        snapshot, elapsed = SnapshotReader(bridge).read()
        self.assertIs(snapshot, expected)
        self.assertEqual(elapsed, 7)
        self.assertEqual(bridge.calls, 1)

    def test_reader_requests_exactly_one_command_at_a_time(self) -> None:
        class RecordingBridge:
            def __init__(self) -> None:
                self.calls = []

            def run(self, arguments):
                arguments = list(arguments)
                self.calls.append(arguments)
                flags = [item for item in arguments if item.startswith("--") and item != "--write"]
                if len(flags) != 1:
                    raise AssertionError(arguments)
                flag = flags[0]
                if flag == "--api-session-context":
                    ack = Ack("api_session_context", arguments[1], {"song": {"tempo": 120, "is_playing": 0}})
                elif flag == "--api-children":
                    ack = Ack("api_children", arguments[3], [], "live_set", "tracks")
                elif flag == "--api-device-list":
                    ack = Ack("api_device_list", arguments[2], {"tracks": []})
                elif flag == "--api-mixer-status":
                    ack = Ack("api_mixer_status", arguments[2], {"parameters": {}}, "live_set master_track")
                else:
                    raise AssertionError(arguments)
                return BridgeResult((ack,), 1, 0, False)

        bridge = RecordingBridge()
        snapshot, elapsed = SnapshotReader(bridge).read()
        self.assertEqual(snapshot.tracks, ())
        self.assertEqual(elapsed, len(bridge.calls))
        self.assertEqual(len(bridge.calls), 6)
        self.assertEqual(snapshot.scenes, ())

    def test_builds_frozen_snapshot_from_wrapper_payloads(self) -> None:
        path = "live_set tracks 0"
        device_path = path + " devices 0"
        mixer = {
            "parameters": {
                "volume": {"path": path + " mixer_device volume", "id": 11, "name": "Volume", "value": 0.5, "min": 0, "max": 1},
                "panning": {"path": path + " mixer_device panning", "id": 12, "name": "Pan", "value": -0.2, "min": -1, "max": 1},
            }
        }
        snapshot = build_snapshot(
            {"song": {"tempo": 99, "is_playing": 0}},
            [{"index": 0, "path": path, "name": "Old"}],
            {0: "Pad"},
            {path: mixer, "live_set master_track": mixer},
            {"tracks": [{"track": {"path": path}, "devices": [{"path": device_path, "name": "Verb"}]}]},
            {device_path: {"path": device_path, "id": 20, "name": "Verb", "parameters": [{"path": device_path + " parameters 0", "id": 21, "name": "Dry/Wet", "value": 0.25, "min": 0, "max": 1, "is_quantized": 0}]}},
            {0: True},
            {0: False},
            {
                path + " mixer_device volume": "-9.0 dB",
                path + " mixer_device panning": "20L",
                "live_set master_track mixer_device volume": "-9.0 dB",
            },
            taken_at=10,
        )
        self.assertEqual(snapshot.tracks[0].name, "Pad")
        self.assertEqual(snapshot.tracks[0].volume_display, "-9.0 dB")
        self.assertEqual(snapshot.tracks[0].pan_display, "20L")
        self.assertTrue(snapshot.tracks[0].mute)
        self.assertEqual(snapshot.tracks[0].devices[0].params[0].name, "Dry/Wet")
        self.assertEqual(snapshot.tracks[0].devices[0].params[0].display, "25%")
        self.assertEqual(snapshot.tempo, 99)


if __name__ == "__main__":
    unittest.main()
