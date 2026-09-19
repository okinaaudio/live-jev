# Installing Live Jev

Written for people and for AI coding assistants (Claude Code, Codex, Cursor and the like). Every step has a check.

## 0. Prerequisites
| You need | Check | If missing |
| --- | --- | --- |
| Apple Silicon Mac (M1 or later), macOS 14 or later | `uname -m` prints `arm64`; `sw_vers -productVersion` is 14 or higher | Not supported (Intel Macs are not supported) |
| Ableton Live 12 | Live starts | — |
| Xcode Command Line Tools | `xcode-select -p` prints a path | `xcode-select --install` |
| Homebrew Python 3.13 | `/opt/homebrew/bin/python3.13 --version` | `brew install python@3.13` (Homebrew: <https://brew.sh>) |
| A TypeSafe API key | — | Sign in at <https://console.typesafe.ai/> and create one (docs: <https://docs.typesafe.ai/>). It is a paid API; check TypeSafe’s site for pricing |

**Note for AI assistants:** ask the user to enter the API key themselves. Never write the key to a file you create, a log, or a commit. Ask the user to do step 3 (it happens in Live’s settings window).

## 1. Get the code
```bash
git clone https://github.com/okinaaudio/live-jev.git ~/live-jev
cd ~/live-jev
```
**Important:** the app runs `daemon.py` from this folder. **Do not move or delete the folder after building the app** (if you move it, repeat step 5). Decide where it should live before you continue.

Check: `/opt/homebrew/bin/python3.13 -m unittest discover -s tests` ends with `OK`.

## 2. Install the Remote Script
```bash
mkdir -p ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev
cp remote_script/LiveJev/*.py ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev/
```
If you moved your User Library, put it in `Remote Scripts/LiveJev/` under the location shown in Live’s Settings → Library.

## 3. Enable it in Live (done by the user)
Start Live → Settings → **Link, Tempo & MIDI** → **Control Surface** → choose **LiveJev** in a free slot → **restart Live**. Leave Input and Output set to None.

Check (with Live running):
```bash
/opt/homebrew/bin/python3.13 plugin_script.py ping     # → pong
```

## 4. Set your API key
```bash
echo 'export TYPESAFE_API_KEY="YOUR_KEY"' >> ~/.zshrc
```
The key is read from the `TYPESAFE_API_KEY` environment variable, or from an `export TYPESAFE_API_KEY=...` line in `~/.zshenv`, `~/.zprofile`, `~/.zshrc`, `~/.bash_profile`, `~/.bashrc` or `~/.profile`.

Check (with Live running; this does not change your set):
```bash
/opt/homebrew/bin/python3.13 cli.py status           # → one line such as "Live 12 tracks / 120 BPM"
```

## 5. Build the app
```bash
bash scripts/build-app.sh          # → ~/Applications/Live Jev.app
open ~/Applications/Live\ Jev.app
```
A waveform icon appears in the menu bar. There is no Dock icon.

## 6. Use it
Bring Live to the front and press **⌘⇧Space** → type “mute” → Enter. The bar disappears at once and Live stays in front. It only comes back when it needs to ask you something. To undo the last successful command, including a success with a hidden result row, summon the bar and press ⌘Z.

Check from Terminal (mutes the selected track, then unmutes it):
```bash
/opt/homebrew/bin/python3.13 cli.py "mute" && /opt/homebrew/bin/python3.13 cli.py "unmute"
```

## Troubleshooting
| Symptom | Where to look |
| --- | --- |
| `plugin_script.py ping` prints `no answer` | Did you choose LiveJev in step 3 and restart Live afterwards? Live’s log (`~/Library/Preferences/Ableton/Live 12.*/Log.txt`) should contain `LiveJev: started, listening on port 9140` |
| “The Jev API key was not found.” | Is the line from step 4 in your shell profile? Quit and reopen the app after adding it |
| `build-app.sh` says swift was not found | `xcode-select --install` |
| `build-app.sh` says `/opt/homebrew/bin/python3.13` is missing | `brew install python@3.13` |
| Nothing happens on ⌘⇧Space | Menu bar waveform icon → Show. Check that no other app uses the same shortcut |
| The app keeps saying “Starting background service…” | The cloned folder was moved or deleted (see step 1). Put it back, or repeat step 5 |
| A plug-in name is not understood | Does the plug-in show up in Live’s browser? You can pin a nickname in `plugin_aliases.json`, for example `{"valhalla": "ValhallaVintageVerb"}` |

## Uninstall
Delete `~/Applications/Live Jev.app`, `~/Music/Ableton/User Library/Remote Scripts/LiveJev/` and the cloned folder, remove the `TYPESAFE_API_KEY` line from your shell profile, and set the Control Surface slot in Live back to None.
