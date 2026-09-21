from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from unittest import mock

from bridge_client import Ack, make_bridge_client
from script_bridge_client import ScriptBridgeClient, acks_from_results, translate_arguments


def _loopback_tcp_available() -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return True
    except PermissionError:
        return False
    finally:
        probe.close()


class TranslationTests(unittest.TestCase):
    def test_ping_timeout_returns_false(self) -> None:
        client = ScriptBridgeClient()
        with mock.patch.object(client, "_request", side_effect=TimeoutError("slow")):
            self.assertFalse(client.ping())

    def test_ping_records_remote_script_version(self) -> None:
        client = ScriptBridgeClient()
        with mock.patch.object(client, "_request", return_value={"ok": True, "version": "0.18"}):
            self.assertTrue(client.ping())
        self.assertEqual(client.script_version, "0.18")

    def test_ping_without_version_records_incompatible_missing_version(self) -> None:
        client = ScriptBridgeClient()
        with mock.patch.object(client, "_request", return_value={"ok": True}):
            self.assertTrue(client.ping())
        self.assertIsNone(client.script_version)

    def test_reconnect_refreshes_version_before_the_requested_read(self) -> None:
        client = ScriptBridgeClient()
        client._socket = object()
        client._pinged = True
        client.script_version = "0.18"

        def discard_closed():
            client._socket = None

        def connect():
            client._socket = object()

        responses = {
            "ping": {"ok": True, "version": "0.18"},
            "snapshot": {"ok": True, "snapshot": {}},
        }
        with mock.patch.object(client, "_discard_if_closed", side_effect=discard_closed), mock.patch.object(
            client, "_connect", side_effect=connect
        ), mock.patch.object(
            client, "_request_on_connection", side_effect=lambda payload: responses[payload["action"]]
        ) as request:
            response = client._request({"action": "snapshot"})

        self.assertEqual(response, responses["snapshot"])
        self.assertEqual(client.script_version, "0.18")
        self.assertEqual(
            [call.args[0]["action"] for call in request.call_args_list],
            ["ping", "snapshot"],
        )

    def test_translates_allowed_arguments_and_keeps_ack_metadata_local(self) -> None:
        commands = translate_arguments([
            "--api-get", "live_set tracks 1", "mute", "get-1",
            "--api-mixer-status", "master", "mix-1",
            "--api-device-parameters", "live_set tracks 1 devices 0", "dev-1",
        ])
        self.assertEqual([command.wire["op"] for command in commands], ["lom_get", "mixer_status", "device_parameters"])
        self.assertNotIn("request_id", commands[0].wire)
        self.assertEqual(commands[0].request_id, "get-1")
        acks = acks_from_results(commands, [
            {"ok": True, "value": 1},
            {"ok": True, "value": {"parameters": {}}},
            {"ok": True, "value": {"parameters": []}},
        ])
        self.assertEqual(acks[0], Ack("api_get", "get-1", 1, "live_set tracks 1", "mute"))
        self.assertEqual(acks[1].path, "live_set master_track")

    def test_translates_structural_modifiers_in_either_order(self) -> None:
        add = translate_arguments(["--write", "--midi-name", "Pads", "--add-midi-tracks", "1"])
        rename = translate_arguments(["--write", "--rename-track-name", "Bass", "--rename-track-index", "2"])
        self.assertEqual(add[0].wire, {"op": "add_track", "kind": "midi", "name": "Pads"})
        self.assertEqual(rename[0].wire, {"op": "rename_track", "index": 2, "name": "Bass"})

    def test_forbidden_arguments_fail_before_translation(self) -> None:
        with self.assertRaises(ValueError):
            translate_arguments(["--write", "--api-call", "live_set", "delete_track", "[0]", "x"])


class FakeScriptServer:
    def __init__(self, *, close_after_response: bool = False, reply: bool = True) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(5)
        self.socket.settimeout(0.05)
        self.port = self.socket.getsockname()[1]
        self.close_after_response = close_after_response
        self.reply = reply
        self.accepts = 0
        self.requests = []
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=1)
        self.socket.close()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                connection, _sender = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepts += 1
            connection.settimeout(0.05)
            data = b""
            try:
                while not self.stopped.is_set():
                    try:
                        chunk = connection.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    data += chunk
                    while b"\n" in data:
                        line, data = data.split(b"\n", 1)
                        request = json.loads(line)
                        self.requests.append(request)
                        if not self.reply:
                            continue
                        if request["action"] == "ping":
                            response = {"ok": True, "version": "0.14"}
                        else:
                            results = [{"ok": True, "value": 1} for _op in request["ops"]]
                            response = {"ok": True, "request": request["request"], "results": results}
                        connection.sendall(json.dumps(response).encode() + b"\n")
                        if self.close_after_response:
                            connection.close()
                            raise OSError
            except OSError:
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass


class SocketPairProtocolTests(unittest.TestCase):
    def test_persistent_json_lines_flow_from_arguments_to_acks(self) -> None:
        client_socket, server_socket = socket.socketpair()
        requests = []

        def serve() -> None:
            data = b""
            try:
                while len(requests) < 2:
                    data += server_socket.recv(4096)
                    while b"\n" in data and len(requests) < 2:
                        line, data = data.split(b"\n", 1)
                        request = json.loads(line)
                        requests.append(request)
                        response = {
                            "ok": True,
                            "request": request["request"],
                            "results": [{"ok": True, "value": len(requests)}],
                        }
                        server_socket.sendall(json.dumps(response).encode() + b"\n")
            finally:
                server_socket.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with mock.patch("script_bridge_client.socket.create_connection", return_value=client_socket):
            client = ScriptBridgeClient(timeout=0.2)
            self.addCleanup(client.close)
            first = client.run(["--api-get", "live_set", "tempo", "one"])
            second = client.run(["--api-get", "live_set tracks 0", "mute", "two"])
        thread.join(timeout=1)
        self.assertEqual(first.acks[0], Ack("api_get", "one", 1, "live_set", "tempo"))
        self.assertEqual(second.acks[0], Ack("api_get", "two", 2, "live_set tracks 0", "mute"))
        self.assertEqual(len(requests), 2)

    def test_timeout_sends_a_write_once(self) -> None:
        client_socket, server_socket = socket.socketpair()
        received = []

        def serve() -> None:
            try:
                received.append(server_socket.recv(4096))
                time.sleep(0.1)
            finally:
                server_socket.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with mock.patch("script_bridge_client.socket.create_connection", return_value=client_socket):
            client = ScriptBridgeClient(timeout=0.02)
            self.addCleanup(client.close)
            result = client.run(["--write", "--api-set", "live_set", "loop", "1", "set-1"])
        thread.join(timeout=1)
        self.assertTrue(result.timed_out)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].count(b"\n"), 1)

    def test_snapshot_response_shape_is_read_by_the_client(self) -> None:
        client_socket, server_socket = socket.socketpair()
        requests = []

        def serve() -> None:
            try:
                requests.append(json.loads(server_socket.recv(4096)))
                response = {
                    "ok": True,
                    "snapshot": {
                        "schema": 1,
                        "song": {"tempo": 123, "is_playing": False},
                        "tracks": [],
                        "master": {"volume": {"value": 0.75, "display": "-3.0 dB"}},
                        "scenes": [],
                        "returns": [],
                    },
                }
                server_socket.sendall(json.dumps(response).encode() + b"\n")
            finally:
                server_socket.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        with mock.patch("script_bridge_client.socket.create_connection", return_value=client_socket):
            client = ScriptBridgeClient(timeout=0.2)
            self.addCleanup(client.close)
            snapshot, _elapsed = client.read_snapshot()
        thread.join(timeout=1)
        self.assertEqual(requests, [{"action": "snapshot"}])
        self.assertEqual(snapshot.tempo, 123)
        self.assertEqual(snapshot.master_display, "-3.0 dB")


@unittest.skipUnless(_loopback_tcp_available(), "実行環境がloopback TCP bindを禁止しています")
class ScriptBridgeClientTests(unittest.TestCase):
    def test_reuses_one_connection(self) -> None:
        server = FakeScriptServer()
        self.addCleanup(server.close)
        client = ScriptBridgeClient(port=server.port, timeout=0.2)
        self.addCleanup(client.close)
        first = client.run(["--api-get", "live_set", "tempo", "one"])
        second = client.run(["--api-get", "live_set", "tempo", "two"])
        self.assertEqual(first.acks[0].payload, 1)
        self.assertEqual(second.acks[0].request_id, "two")
        self.assertEqual(server.accepts, 1)

    def test_reconnects_on_the_call_after_a_clean_server_close(self) -> None:
        server = FakeScriptServer(close_after_response=True)
        self.addCleanup(server.close)
        client = ScriptBridgeClient(port=server.port, timeout=0.2)
        self.addCleanup(client.close)
        client.run(["--api-get", "live_set", "tempo", "one"])
        time.sleep(0.02)
        second = client.run(["--api-get", "live_set", "tempo", "two"])
        self.assertEqual(second.acks[0].request_id, "two")
        self.assertEqual(server.accepts, 2)

    def test_timeout_does_not_resend(self) -> None:
        server = FakeScriptServer(reply=False)
        self.addCleanup(server.close)
        client = ScriptBridgeClient(port=server.port, timeout=0.05)
        self.addCleanup(client.close)
        result = client.run(["--write", "--api-set", "live_set", "loop", "1", "set-1"])
        self.assertTrue(result.timed_out)
        self.assertEqual(len(server.requests), 1)


class TransportSelectionTests(unittest.TestCase):
    def test_default_is_udp_and_explicit_script_is_selected(self) -> None:
        udp = object()
        script = object()
        with mock.patch("bridge_client.PersistentBridgeClient", return_value=udp), mock.patch("script_bridge_client.ScriptBridgeClient", return_value=script):
            self.assertIs(make_bridge_client("udp"), udp)
            self.assertIs(make_bridge_client("script"), script)

    def test_auto_falls_back_to_udp(self) -> None:
        udp = object()
        script = mock.Mock()
        script.ping.return_value = False
        from pathlib import Path
        with mock.patch("bridge_client.PersistentBridgeClient", return_value=udp), mock.patch("script_bridge_client.ScriptBridgeClient", return_value=script), \
             mock.patch("bridge_client.UPSTREAM_PY", Path(__file__)):
            self.assertIs(make_bridge_client("auto"), udp)
        script.close.assert_called_once_with()

    def test_auto_keeps_script_when_the_udp_bridge_is_not_installed(self) -> None:
        from pathlib import Path
        script = mock.Mock()
        script.ping.return_value = False
        with mock.patch("script_bridge_client.ScriptBridgeClient", return_value=script), \
             mock.patch("bridge_client.UPSTREAM_PY", Path("/nonexistent/ableton_udp_bridge.py")):
            self.assertIs(make_bridge_client("auto"), script)
        script.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
