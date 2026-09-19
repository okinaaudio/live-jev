# LiveJev (the summon bar)

A small input bar for macOS 14 or later. The Swift side never talks to Jev or Live directly: it starts `daemon.py` (Python 3.13) from the parent folder and exchanges one JSON object per line with it.

```sh
swift build -c release --scratch-path ~/dev/live-jev-build
~/dev/live-jev-build/release/LiveJev
```

Set `LIVE_JEV_DAEMON=/absolute/path/to/daemon.py` to use a different `daemon.py`. For everyday use, build the `.app` with `scripts/build-app.sh`.

- `⌘⇧Space` summon / hide · `Enter` send and return to Live at once · `Esc` hide · `↑` / `↓` input history · `⌘Z` (with an empty field) undo
- Menu bar waveform icon: Show, Launch at Login, Language (Automatic / Japanese / English), Show Details, Quit
