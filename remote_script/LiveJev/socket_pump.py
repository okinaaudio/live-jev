"""Non-blocking JSON-lines socket pump with no Ableton Live dependency."""

from dataclasses import dataclass, field
import json
import time


@dataclass
class _Connection:
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)


class SocketPump:
    def __init__(
        self,
        create_listener,
        handle_line,
        *,
        max_commands=32,
        time_limit=0.008,
        max_input=4_000_000,
        max_output=32_000_000,
        max_connections=8,
        clock=time.monotonic,
        on_error=None,
        require_json_objects=False,
    ):
        self._create_listener = create_listener
        self._handle_line = handle_line
        self._max_commands = max_commands
        self._time_limit = time_limit
        self._max_input = max_input
        self._max_output = max_output
        self._max_connections = max_connections
        self._clock = clock
        self._on_error = on_error
        self._require_json_objects = require_json_objects
        self._listener = None
        self._connections = {}
        self._listener_failures = 0
        self._listener_retry_at = 0.0

    def poll(self):
        deadline = self._clock() + self._time_limit
        self._ensure_listener()
        self._accept_available(deadline)
        self._receive_available(deadline)
        self._run_commands(deadline)
        self._send_available(deadline)

    def close(self):
        self._close_listener()
        for connection in list(self._connections):
            self._close_connection(connection)

    def _close_listener(self):
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass

    def _ensure_listener(self):
        if self._listener is not None or self._clock() < self._listener_retry_at:
            return
        try:
            self._listener = self._create_listener()
            self._listener_failures = 0
            self._listener_retry_at = 0.0
        except Exception as error:
            self._listener_failures += 1
            if self._listener_failures >= 5:
                self._listener_retry_at = self._clock() + 5.0
                if self._listener_failures == 5:
                    self._report(RuntimeError("listener unavailable; retrying every 5 seconds"))
            self._report(error)

    def _accept_available(self, deadline):
        while self._listener is not None and self._clock() < deadline:
            try:
                connection, _address = self._listener.accept()
                connection.setblocking(False)
                if len(self._connections) >= self._max_connections:
                    connection.close()
                else:
                    self._connections[connection] = _Connection()
            except BlockingIOError:
                return
            except Exception as error:
                self._report(error)
                self._close_listener()
                return

    def _receive_available(self, deadline):
        for connection in list(self._connections):
            while self._clock() < deadline and connection in self._connections:
                try:
                    chunk = connection.recv(65536)
                    if not chunk:
                        self._close_connection(connection)
                        break
                    state = self._connections[connection]
                    state.incoming.extend(chunk)
                    if len(state.incoming) > self._max_input:
                        self._close_connection(connection)
                        break
                except BlockingIOError:
                    break
                except Exception as error:
                    self._report(error)
                    self._close_connection(connection)
                    break

    def _run_commands(self, deadline):
        count = 0
        for connection in list(self._connections):
            while count < self._max_commands and self._clock() < deadline:
                state = self._connections.get(connection)
                if state is None:
                    break
                newline = state.incoming.find(b"\n")
                if newline < 0:
                    break
                raw = bytes(state.incoming[:newline])
                del state.incoming[:newline + 1]
                count += 1
                try:
                    line = raw.decode("utf-8").strip()
                    if self._require_json_objects:
                        decoded = json.loads(line)
                        if not isinstance(decoded, dict):
                            self._close_connection(connection)
                            break
                    response = self._handle_line(line)
                    encoded = response.encode("utf-8") + b"\n"
                    if len(state.outgoing) + len(encoded) > self._max_output:
                        self._close_connection(connection)
                        break
                    state.outgoing.extend(encoded)
                except Exception as error:
                    self._report(error)
                    self._close_connection(connection)
                    break
            if count >= self._max_commands or self._clock() >= deadline:
                break

    def _send_available(self, deadline):
        for connection in list(self._connections):
            state = self._connections.get(connection)
            while state is not None and state.outgoing and self._clock() < deadline:
                try:
                    sent = connection.send(state.outgoing)
                    if sent <= 0:
                        self._close_connection(connection)
                        break
                    del state.outgoing[:sent]
                except BlockingIOError:
                    break
                except Exception as error:
                    self._report(error)
                    self._close_connection(connection)
                    break

    def _close_connection(self, connection):
        if self._connections.pop(connection, None) is None:
            return
        try:
            connection.close()
        except Exception:
            pass

    def _report(self, error):
        if self._on_error is not None:
            self._on_error(error)
