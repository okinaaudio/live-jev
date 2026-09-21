from __future__ import annotations

from types import SimpleNamespace
import unittest

from remote_script.LiveJev.lom_protocol import (
    LomPathError,
    allow_call,
    allow_get,
    allow_param_set,
    allow_set,
    path_kind,
    resolve_lom_path,
)


def _song():
    parameter = SimpleNamespace(value=0.5)
    device = SimpleNamespace(parameters=[parameter])
    mixer = SimpleNamespace(
        volume=SimpleNamespace(value=0.8),
        panning=SimpleNamespace(value=0.0),
        sends=[SimpleNamespace(value=0.2)],
    )
    clip = SimpleNamespace(name="Clip")
    slot = SimpleNamespace(clip=clip)
    track = SimpleNamespace(devices=[device], mixer_device=mixer, clip_slots=[slot])
    master = SimpleNamespace(devices=[], mixer_device=SimpleNamespace(volume=SimpleNamespace(value=0.9)))
    return SimpleNamespace(
        tracks=[track], return_tracks=[], master_track=master,
        scenes=[SimpleNamespace(name="Scene")], view=SimpleNamespace(),
    )


class LomProtocolTests(unittest.TestCase):
    def test_resolves_every_supported_path_step_without_live(self) -> None:
        song = _song()
        cases = {
            "live_set": song,
            "live_set tracks 0": song.tracks[0],
            "live_set tracks 0 clip_slots 0 clip": song.tracks[0].clip_slots[0].clip,
            "live_set tracks 0 devices 0 parameters 0": song.tracks[0].devices[0].parameters[0],
            "live_set tracks 0 mixer_device sends 0": song.tracks[0].mixer_device.sends[0],
            "live_set master_track mixer_device volume": song.master_track.mixer_device.volume,
            "live_set scenes 0": song.scenes[0],
            "live_set view": song.view,
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertIs(resolve_lom_path(song, path), expected)

    def test_rejects_arbitrary_negative_missing_and_trailing_paths(self) -> None:
        song = _song()
        for path in (
            "live_set tracks -1",
            "live_set tracks 01",
            "live_set tracks 2",
            "live_set tracks 0 canonical_parent",
            "live_set tracks 0 clip_slots 0 clip name",
            "other tracks 0",
        ):
            with self.subTest(path=path), self.assertRaises(LomPathError):
                resolve_lom_path(song, path)

    def test_rejects_index_tokens_longer_than_six_digits(self) -> None:
        with self.assertRaises(LomPathError):
            resolve_lom_path(_song(), "live_set tracks 1000000")

    def test_runtime_error_while_resolving_is_path_not_found(self) -> None:
        class RuntimeProperty:
            @property
            def tracks(self):
                raise RuntimeError("unsupported track property")

        with self.assertRaisesRegex(LomPathError, "path_not_found"):
            resolve_lom_path(RuntimeProperty(), "live_set tracks 0")

    def test_permission_table_matches_the_python_boundary(self) -> None:
        self.assertTrue(allow_get("live_set", "tempo"))
        self.assertTrue(allow_get("live_set tracks 0", "name"))
        self.assertTrue(allow_get("live_set tracks 0 mixer_device sends 0", "value"))
        self.assertTrue(allow_set("live_set tracks 0", "mute"))
        self.assertTrue(allow_call("live_set tracks 0 clip_slots 0", "fire"))
        self.assertTrue(allow_call("live_set master_track mixer_device volume", "str_for_value"))
        self.assertTrue(allow_param_set("live_set tracks 0 devices 0 parameters 0"))
        self.assertFalse(allow_get("live_set tracks 0", "color"))
        self.assertTrue(allow_set("live_set tracks 0", "name"))
        self.assertTrue(allow_set("live_set return_tracks 0", "name"))
        self.assertTrue(allow_set("live_set master_track", "name"))
        self.assertFalse(allow_call("live_set", "delete_track"))
        self.assertTrue(allow_param_set("live_set return_tracks 0 mixer_device volume"))
        self.assertTrue(allow_param_set("live_set return_tracks 0 mixer_device panning"))
        self.assertTrue(allow_set("live_set return_tracks 0", "mute"))
        self.assertFalse(allow_set("live_set return_tracks 0", "arm"))
        self.assertEqual(path_kind("live_set tracks 0 mixer_device panning"), "track_panning")


if __name__ == "__main__":
    unittest.main()
