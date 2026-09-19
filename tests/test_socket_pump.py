from __future__ import annotations

from collections import deque
import socket
import unittest

from remote_script.LiveJev.socket_pump import SocketPump


class FakeListener:
    def __init__(self, connections):
        self.connections = deque(connections)
        self.closed = False

    def accept(self):
        if not self.connections:
            raise BlockingIOError
        return self.connections.popleft(), ("local", 0)

    def close(self):
        self.closed = True


class LimitedSendSocket:
    def __init__(self, wrapped, limit):
        self.wrapped = wrapped
        self.limit = limit
        self.may_send = True

    def setblocking(self, value):
        self.wrapped.setblocking(value)

    def recv(self, size):
        return self.wrapped.recv(size)

    def send(self, data):
        if not self.may_send:
            raise BlockingIOError
        self.may_send = False
        return self.wrapped.send(data[:self.limit])

    def allow_send(self):
        self.may_send = True

    def close(self):
        self.wrapped.close()


class SocketPumpTests(unittest.TestCase):
    def setUp(self):
        self.clients = []
        self.pumps = []

    def tearDown(self):
        for pump in self.pumps:
            pump.close()
        for client in self.clients:
            client.close()

    def pair(self):
        client, server = socket.socketpair()
        client.setblocking(False)
        self.clients.append(client)
        return client, server

    def pump(self, listener, handler=lambda line: line.upper()):
        pump = SocketPump(lambda: listener, handler)
        self.pumps.append(pump)
        return pump

    def read_available(self, client):
        chunks = []
        while True:
            try:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            except BlockingIOError:
                break
        return b"".join(chunks)

    def test_accepts_all_connections_and_keeps_json_lines_separate(self):
        first_client, first_server = self.pair()
        second_client, second_server = self.pair()
        pump = self.pump(FakeListener([first_server, second_server]))
        first_client.sendall(b"one\ntwo\n")
        second_client.sendall(b"three\n")

        pump.poll()

        self.assertEqual(self.read_available(first_client), b"ONE\nTWO\n")
        self.assertEqual(self.read_available(second_client), b"THREE\n")

    def test_keeps_partial_input_until_a_later_poll(self):
        client, server = self.pair()
        pump = self.pump(FakeListener([server]))
        client.sendall(b"par")
        pump.poll()
        self.assertEqual(self.read_available(client), b"")

        client.sendall(b"tial\n")
        pump.poll()

        self.assertEqual(self.read_available(client), b"PARTIAL\n")

    def test_keeps_unsent_output_until_a_later_poll(self):
        client, raw_server = self.pair()
        server = LimitedSendSocket(raw_server, 4)
        pump = self.pump(FakeListener([server]))
        client.sendall(b"abcdefgh\n")

        pump.poll()
        self.assertEqual(self.read_available(client), b"ABCD")

        server.allow_send()
        pump.poll()
        self.assertEqual(self.read_available(client), b"EFGH")

        server.allow_send()
        pump.poll()
        self.assertEqual(self.read_available(client), b"\n")

    def test_defers_complete_lines_after_the_command_limit(self):
        client, server = self.pair()
        listener = FakeListener([server])
        pump = SocketPump(lambda: listener, lambda line: line, max_commands=1)
        self.pumps.append(pump)
        client.sendall(b"one\ntwo\n")

        pump.poll()
        self.assertEqual(self.read_available(client), b"one\n")
        pump.poll()
        self.assertEqual(self.read_available(client), b"two\n")

    def test_defers_commands_after_the_time_limit(self):
        client, server = self.pair()
        listener = FakeListener([server])
        now = [0.0]
        handled = []

        def handle(line):
            handled.append(line)
            now[0] += 0.009
            return line

        pump = SocketPump(lambda: listener, handle, clock=lambda: now[0])
        self.pumps.append(pump)
        client.sendall(b"one\ntwo\n")

        pump.poll()
        self.assertEqual(handled, ["one"])
        pump.poll()
        self.assertEqual(handled, ["one", "two"])
        pump.poll()
        self.assertEqual(self.read_available(client), b"one\ntwo\n")

    def test_closes_only_the_connection_whose_handler_raises(self):
        bad_client, bad_server = self.pair()
        good_client, good_server = self.pair()

        def handle(line):
            if line == "boom":
                raise RuntimeError("broken command")
            return "ok"

        pump = self.pump(FakeListener([bad_server, good_server]), handle)
        bad_client.sendall(b"boom\n")
        good_client.sendall(b"safe\n")

        pump.poll()

        self.assertEqual(self.read_available(bad_client), b"")
        self.assertEqual(bad_client.recv(1), b"")
        self.assertEqual(self.read_available(good_client), b"ok\n")

    def test_retries_listener_creation_on_the_next_poll(self):
        client, server = self.pair()
        listener = FakeListener([server])
        attempts = []

        def create_listener():
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError(48, "Address already in use")
            return listener

        pump = SocketPump(create_listener, lambda line: line)
        self.pumps.append(pump)
        pump.poll()
        client.sendall(b"ready\n")
        pump.poll()

        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.read_available(client), b"ready\n")


if __name__ == "__main__":
    unittest.main()
