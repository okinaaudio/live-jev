"""Tests never access a real Live instance or Remote Script."""

import plugin_script

plugin_script.ping = lambda: False
plugin_script.list_plugins = lambda refresh=False: []


import socket

import script_bridge_client

_real_connect = socket.socket.connect


def _refuse_live_port(self, address):
    # A service built without an injected bridge would otherwise reach the user's open Live set during a test run.
    if isinstance(address, tuple) and len(address) >= 2 and address[1] == script_bridge_client.PORT:
        raise ConnectionRefusedError("tests must not connect to a real Live instance")
    return _real_connect(self, address)


socket.socket.connect = _refuse_live_port
