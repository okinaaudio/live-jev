"""Tests never access a real Live instance or Remote Script."""

import plugin_script

plugin_script.ping = lambda: False
plugin_script.list_plugins = lambda refresh=False: []
