# LiveJev (the summon bar)

A small input bar for macOS 14 or later. The Swift side never talks to Jev or Live directly: it starts `daemon.py` (Python 3.13) from the parent folder and exchanges one JSON object per line with it.

```sh
swift build -c release --scratch-path ~/dev/live-jev-build
~/dev/live-jev-build/release/LiveJev
```

`~/dev/live-jev-build` is the default from `scripts/build-app.sh`. Set `LIVE_JEV_BUILD_DIR` to use another build directory. Set `LIVE_JEV_DAEMON=/absolute/path/to/daemon.py` to use a different `daemon.py`. For everyday use, build the `.app` with `scripts/build-app.sh`.

- `⌘⇧Space` summon / hide · `Enter` send and return to Live at once · `Esc` hide · `↑` / `↓` input history · `⌘Z` (with an empty field) undo
- Menu bar waveform icon: Show, Launch at Login, Language (Automatic / Japanese / English), Show Details, Quit

Setup opens on first launch and from **Setup…** above **Launch at Login**. Choose a User Library if it is not in `~/Music/Ableton/User Library`. Installation stages and atomically exchanges only the `Remote Scripts/LiveJev` folder. The current Remote Script version is 0.18. Setup reads versions from the script text without executing Python. It compares the installed version with the source available from the current app or checkout and reports whether they match. Select LiveJev in Live's Control Surface settings and restart Live after installation. Setup polls status every two seconds while open and stops when closed or minimized. Done and Finish later both complete first-run setup; the menu can reopen it.

Enter the TypeSafe key in Setup. This is the recommended method. Setup uses the macOS generic-password Keychain item with service `com.okinaaudio.livejev` and account `TYPESAFE_API_KEY`. Saving or removing a key restarts the daemon through its existing retry lifecycle. A saved key is passed only in the child's `TYPESAFE_API_KEY` environment variable. A key from the process environment or a shell profile also satisfies Setup. For terminal use, add `export TYPESAFE_API_KEY="YOUR_KEY"` to a shell profile.

The optional Gemini key uses the same Keychain service with account `GEMINI_API_KEY`. The app passes it to the child through the environment. The daemon does not read a Gemini key from shell profile files, but developers can export it in the shell that runs `cli.py`. When enabled, Gemini receives the sentence, track and device names, and installed plug-in and Live device names. It receives no audio or project files. Gemini API use may cost money.

Daemon selection: `LIVE_JEV_DAEMON`, then `~/Library/Application Support/LiveJev/config.json` (`daemon_path`), then bundled `Contents/Resources/daemon/daemon.py`, then the development path compiled into the app. Script installation uses bundled `Contents/Resources/remote_script/LiveJev`, falling back to `remote_script/LiveJev` beside the resolved daemon.

Python selection: `LIVE_JEV_PYTHON`, bundled `Contents/Resources/python/bin/python3`, `/opt/homebrew/bin/python3.13`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, then `/usr/bin/python3`. The selected executable path is logged. Development builds require Homebrew's `python@3.13`. If only the system Python 3.9 is available, the daemon cannot run and the bar shows "Background service stopped." Run `brew install python@3.13`. The child receives `PYTHONDONTWRITEBYTECODE=1` to preserve the signed bundle. Development builds use the source files beside the resolved daemon instead of bundled daemon and Remote Script resources.

After `git pull`, copy `remote_script/LiveJev/*.py` into the User Library again, or press **Update** in Setup. Restart Live. If Swift files changed, run `bash scripts/build-app.sh` again.
