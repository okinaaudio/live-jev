from __future__ import annotations

import importlib
import io
import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

from bridge_client import BridgeResult
from daemon import LiveJevService, MIN_SCRIPT_VERSION, _version_is_older, read_key
from tests.support import StatefulLive, response, sample_snapshot
import cli
import plugin_script
from scripts import scan_plugins


def _livejev_class():
    live = ModuleType("Live")
    live.Base = SimpleNamespace(Timer=object)
    framework = ModuleType("_Framework")
    control_surface = ModuleType("_Framework.ControlSurface")
    control_surface.ControlSurface = object
    with mock.patch.dict(sys.modules, {
        "Live": live,
        "_Framework": framework,
        "_Framework.ControlSurface": control_surface,
    }):
        module = importlib.import_module("remote_script.LiveJev.LiveJev")
    return module.LiveJev


class RemoteScriptHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.LiveJev = _livejev_class()

    def instance(self):
        instance = object.__new__(self.LiveJev)
        instance.log_message = mock.Mock()
        instance._last_socket_error = None
        return instance

    def test_bridge_rejects_oversized_batch_before_any_operation(self) -> None:
        instance = self.instance()
        instance._execute_op = mock.Mock(return_value={"ok": True})

        answer = instance._run_bridge("large", [{}] * 1025)

        self.assertEqual(answer, {"ok": False, "request": "large", "error": "too_many_ops"})
        instance._execute_op.assert_not_called()

    def test_poll_logs_and_contains_each_distinct_error(self) -> None:
        instance = self.instance()
        instance._running = True
        instance._pump = mock.Mock()
        instance._pump.poll.side_effect = [RuntimeError("first"), RuntimeError("second"), RuntimeError("first")]

        instance._poll()
        instance._poll()
        instance._poll()

        self.assertEqual(instance.log_message.call_count, 2)

    def test_unexpected_command_error_is_hidden_and_logged(self) -> None:
        instance = self.instance()
        instance._build_snapshot = mock.Mock(side_effect=RuntimeError("private detail"))

        answer = json.loads(instance._handle_command('{"action":"snapshot"}'))

        self.assertEqual(answer, {"ok": False, "error": "internal_error"})
        instance.log_message.assert_called_once()

    def test_code_like_value_error_remains_public(self) -> None:
        instance = self.instance()
        instance._build_snapshot = mock.Mock(side_effect=ValueError("invalid_value"))

        answer = json.loads(instance._handle_command('{"action":"snapshot"}'))

        self.assertEqual(answer, {"ok": False, "error": "invalid_value"})

    def test_load_reports_pending_when_no_device_or_track_appears(self) -> None:
        instance = self.instance()
        target = SimpleNamespace(name="Bass", devices=[SimpleNamespace(name="Existing")])
        song = SimpleNamespace(
            tracks=[target],
            view=SimpleNamespace(selected_track=target),
        )
        browser = SimpleNamespace(hotswap_target=None, load_item=mock.Mock())
        instance.song = mock.Mock(return_value=song)
        instance._find_item = mock.Mock(return_value={"name": "Echo", "uri": "device://echo", "section": "audio_effects"})
        instance._resolve_browser_item = mock.Mock(return_value=object())
        instance._browser = mock.Mock(return_value=browser)
        instance._track_path = mock.Mock(return_value="live_set tracks 0")

        answer = instance._load("Echo", "device://echo", 0)

        self.assertEqual(answer["ok"], True)
        self.assertEqual(answer["pending"], True)
        self.assertEqual(answer["devices_before"], ["Existing"])
        self.assertEqual(answer["devices_after"], ["Existing"])
        self.assertIn("pending", instance.log_message.call_args.args[0])


class VersionHandshakeTests(unittest.TestCase):
    def test_remote_script_version_uses_numeric_components(self) -> None:
        self.assertTrue(_version_is_older("0.9", "0.16"))
        self.assertTrue(_version_is_older("0.16", MIN_SCRIPT_VERSION))
        self.assertFalse(_version_is_older("0.100", MIN_SCRIPT_VERSION))

    def test_old_script_notice_appears_in_status_and_blocks_write(self) -> None:
        bridge = StatefulLive()
        bridge.script_version = "0.16"
        bridge.ping = lambda: True
        bridge.read_snapshot = lambda: (sample_snapshot(), 1)
        with mock.patch.dict("os.environ", {"LIVE_JEV_LANG": "en"}):
            service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x")
            status = service.start()
            answer = service.process({"text": "mute Bass"})

        self.assertIn("Setup", status["line"])
        self.assertEqual(answer["kind"], "error")
        self.assertEqual(answer["line"], status["line"])
        self.assertFalse(bridge.state[(1, "mute")])

    def test_injected_bridge_without_version_concept_stays_compatible(self) -> None:
        class FakeBridge:
            def ping(self):
                return True

            def read_snapshot(self):
                return sample_snapshot(), 0

            def run(self, _arguments):
                return BridgeResult((), 0, 0, False)

        service = LiveJevService(bridge=FakeBridge(), snapshot=sample_snapshot(), key="x")
        self.assertNotIn("Setup", service.start()["line"])

    def test_version_is_refreshed_before_a_write(self) -> None:
        bridge = StatefulLive()
        bridge.script_version = "0.18"

        def ping():
            bridge.script_version = "0.16"
            return True

        bridge.ping = ping
        with mock.patch.dict("os.environ", {"LIVE_JEV_LANG": "en"}):
            service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x")
            answer = service.process({"text": "mute Bass"})

        self.assertEqual(answer["kind"], "error")
        self.assertIn("Setup", answer["line"])
        self.assertFalse(bridge.state[(1, "mute")])


class GeminiOptInTests(unittest.TestCase):
    def test_gemini_key_is_not_loaded_from_shell_profile(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, ".zshrc").write_text("export GEMINI_API_KEY=from-profile\n", encoding="utf-8")
            with mock.patch("daemon.Path.home", return_value=Path(folder)), mock.patch.dict(
                "os.environ", {}, clear=True
            ):
                self.assertIsNone(read_key("GEMINI_API_KEY"))

    def test_gemini_key_from_process_environment_is_available(self) -> None:
        bridge = StatefulLive()
        bridge.ping = lambda: True
        bridge.read_snapshot = lambda: (sample_snapshot(), 0)
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "from-env"}, clear=True):
            service = LiveJevService(bridge=bridge, snapshot=sample_snapshot(), key="x")
            service.start()
            self.assertEqual(service.llm_key, "from-env")
            self.assertTrue(service._llm_enabled())

    def test_llm_zero_disables_injected_key(self) -> None:
        with mock.patch.dict("os.environ", {"LIVE_JEV_LLM": "0"}, clear=True):
            service = LiveJevService(snapshot=sample_snapshot(), key="x", llm_key="direct")
            self.assertFalse(service._llm_enabled())

    def test_profile_only_gemini_key_never_calls_rewriter(self) -> None:
        bridge = StatefulLive()
        bridge.ping = lambda: True
        bridge.read_snapshot = lambda: (sample_snapshot(), 0)
        rewrite = mock.Mock(return_value="mute Drums")
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, ".zshrc").write_text("export GEMINI_API_KEY=from-profile\n", encoding="utf-8")
            with mock.patch("daemon.Path.home", return_value=Path(folder)), mock.patch.dict(
                "os.environ", {}, clear=True
            ):
                service = LiveJevService(
                    bridge=bridge,
                    snapshot=sample_snapshot(),
                    key="x",
                    requester=lambda *_args: response("none", action_conf=0.2),
                    rewriter=rewrite,
                )
                service.start()
                service.process({"text": "do something vague to the drums"})
        rewrite.assert_not_called()


class LocalizedUtilityOutputTests(unittest.TestCase):
    def test_cli_detail_labels_follow_english_language(self) -> None:
        output = io.StringIO()
        payload = {
            "kind": "result",
            "line": "Done",
            "decision": {
                "action_label": "Mute",
                "conf": {"action": 0.9},
                "rewritten": ["mute Bass"],
            },
            "ms": {"total": 2, "llm": 100},
        }
        with mock.patch.dict("os.environ", {"LIVE_JEV_LANG": "en"}), mock.patch("sys.stdout", output):
            cli._print(payload)
        self.assertIn("Rewritten: mute Bass", output.getvalue())
        self.assertNotRegex(output.getvalue(), "[ぁ-んァ-ン一-龯]")

    def test_plugin_script_usage_and_count_follow_english_language(self) -> None:
        error = io.StringIO()
        with mock.patch.dict("os.environ", {"LIVE_JEV_LANG": "en"}), mock.patch.object(
            sys, "argv", ["plugin_script.py"]
        ), mock.patch("sys.stderr", error):
            self.assertEqual(plugin_script.main(), 2)
        self.assertIn("Usage:", error.getvalue())

        error = io.StringIO()
        with mock.patch.dict("os.environ", {"LIVE_JEV_LANG": "en"}), mock.patch.object(
            sys, "argv", ["plugin_script.py", "list"]
        ), mock.patch("plugin_script.list_plugins", return_value=[]), mock.patch("sys.stderr", error):
            self.assertEqual(plugin_script.main(), 0)
        self.assertEqual(error.getvalue().strip(), "0 items")

    def test_plugin_scan_summary_follows_english_language(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(
            "os.environ", {"LIVE_JEV_LANG": "en"}
        ), mock.patch.object(scan_plugins, "OUTPUT", Path(folder, "plugins.json")), mock.patch.object(
            scan_plugins, "scan", return_value={"count": 0, "plugins": []}
        ), mock.patch("sys.stdout", output):
            self.assertEqual(scan_plugins.main(), 0)
        self.assertIn("Wrote 0 items to", output.getvalue())

    def test_plugin_verification_errors_are_localized(self) -> None:
        with mock.patch.dict("os.environ", {"LIVE_JEV_LANG": "en"}):
            service = LiveJevService(bridge=StatefulLive(), snapshot=sample_snapshot(), key="x")
        self.assertEqual(
            service._plugin_verification_error("Serum", loading=True),
            "Could not confirm that Serum was loaded. Live may still be loading it.",
        )
        self.assertEqual(
            service._plugin_verification_error("Serum"),
            "Could not confirm that Serum was loaded.",
        )


class DisclosureTests(unittest.TestCase):
    def test_gemini_disclosure_names_installed_plugin_names_in_both_languages(self) -> None:
        source = Path("LiveJev/Sources/LiveJev/Messages.swift").read_text(encoding="utf-8")
        disclosure = next(line for line in source.splitlines() if "case .geminiDisclosure" in line)
        self.assertIn("インストール済みプラグイン名", disclosure)
        self.assertIn("installed plug-in names", disclosure)

    def test_setup_script_statuses_tell_both_languages_to_restart_live(self) -> None:
        source = Path("LiveJev/Sources/LiveJev/Messages.swift").read_text(encoding="utf-8")
        installed = next(line for line in source.splitlines() if "case .installedRestart" in line)
        outdated = next(line for line in source.splitlines() if "case .runningScriptOutdated" in line)
        self.assertIn("インストールしました。Liveを再起動すると反映されます", installed)
        self.assertIn("Installed. Restart Live to load it", installed)
        self.assertIn("Live本体を終了して起動し直してください", outdated)
        self.assertIn("Quit Live and open it again", outdated)
