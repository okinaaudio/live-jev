"""テストは本物の Live や Remote Script に触らない。"""

import plugin_script

plugin_script.ping = lambda: False
plugin_script.list_plugins = lambda refresh=False: []
