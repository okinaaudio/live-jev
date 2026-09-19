# LiveJev (the summon bar)

A small input bar for macOS 14 or later. The Swift side never talks to Jev or Live directly: it starts `daemon.py` (Python 3.13) from the parent folder and exchanges one JSON object per line with it.

```sh
swift build -c release --scratch-path ~/dev/live-jev-build
~/dev/live-jev-build/release/LiveJev
```

Set `LIVE_JEV_DAEMON=/absolute/path/to/daemon.py` to use a different `daemon.py`. For everyday use, build the `.app` with `scripts/build-app.sh`.

- `⌘⇧Space` summon / hide · `Enter` send and return to Live at once · `Esc` hide · `↑` / `↓` input history · `⌘Z` (with an empty field) undo
- Menu bar waveform icon: Show, Launch at Login, Language (Automatic / Japanese / English), Show Details, Quit

Setup opens on first launch and from **Setup…** above **Launch at Login**. Choose a User Library if it is not in `~/Music/Ableton/User Library`. Installation stages and atomically exchanges only the `Remote Scripts/LiveJev` folder. Versions are read from the script text without executing Python. Select LiveJev in Live's Control Surface settings and restart Live after installation. Setup polls status every two seconds while open and stops when closed or minimized. Done and Finish later both complete first-run setup; the menu can reopen it.

API keys saved in Setup use the macOS generic-password Keychain item with service `com.okinaaudio.livejev` and account `TYPESAFE_API_KEY`. Saving or removing a key restarts the daemon through its existing retry lifecycle. A saved key is passed only in the child's `TYPESAFE_API_KEY` environment variable. A working daemon key also satisfies setup without saving a Keychain item.

Daemon selection: `LIVE_JEV_DAEMON`, then `~/Library/Application Support/LiveJev/config.json` (`daemon_path`), then bundled `Contents/Resources/daemon/daemon.py`, then the development path compiled into the app. Script installation uses bundled `Contents/Resources/remote_script/LiveJev`, falling back to `remote_script/LiveJev` beside the resolved daemon.

Python selection: `LIVE_JEV_PYTHON`, bundled `Contents/Resources/python/bin/python3`, `/opt/homebrew/bin/python3.13`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, then `/usr/bin/python3`. The selected executable path is logged. The child receives `PYTHONDONTWRITEBYTECODE=1` to preserve the signed bundle. Development bundles do not need bundled resources.
