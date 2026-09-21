# Installing Live Jev

Written for people and for AI coding assistants (Claude Code, Codex, Cursor and the like). Every step has a check.


## 0. Prerequisites
| You need | Check | If missing |
| --- | --- | --- |
| Apple Silicon Mac (M1 or later), macOS 14 or later | `uname -m` prints `arm64`; `sw_vers -productVersion` is 14 or higher | Not supported (Intel Macs are not supported) |
| Ableton Live 12 | Live starts | — |
| Xcode Command Line Tools | `xcode-select -p` prints a path | `xcode-select --install` |
| Homebrew `python@3.13` | `/opt/homebrew/bin/python3.13 --version` | `brew install python@3.13` (Homebrew: <https://brew.sh>) |
| A TypeSafe API key | — | Sign in at <https://console.typesafe.ai/> and create one (docs: <https://docs.typesafe.ai/>). It is a paid API; check TypeSafe’s site for pricing |

**Note for AI assistants:** ask the user to enter the API key themselves. Never write the key to a file you create, a log, or a commit. Ask the user to do step 5 in Setup.

## 1. Get the code
```bash
git clone https://github.com/okinaaudio/live-jev.git ~/live-jev
cd ~/live-jev
```
**Important:** the app runs `daemon.py` from this folder. **Do not move or delete the folder after building the app** (if you move it, repeat step 4). Decide where it should live before you continue.

Check: `/opt/homebrew/bin/python3.13 -m unittest discover -s tests` ends with `OK`.

## 2. Install the Remote Script
```bash
mkdir -p ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev
cp remote_script/LiveJev/*.py ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev/
```
If you moved your User Library, put it in `Remote Scripts/LiveJev/` under the location shown in Live’s Settings → Library.

Check: `cmp remote_script/LiveJev/LiveJev.py ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev/LiveJev.py` prints nothing and exits successfully. The current Remote Script version is 0.18.

## 3. Enable it in Live (done by the user)
Start Live → Settings → **Link, Tempo & MIDI** → **Control Surface** → choose **LiveJev** in a free slot → **restart Live**. Leave Input and Output set to None.

Check (with Live running):
```bash
LIVE_JEV_LANG=en /opt/homebrew/bin/python3.13 plugin_script.py ping     # → pong
```

## 4. Build the app
```bash
bash scripts/build-app.sh          # → ~/Applications/Live Jev.app
open ~/Applications/Live\ Jev.app
```
A waveform icon appears in the menu bar. There is no Dock icon.

The development app needs Homebrew's `python@3.13`. It looks for `LIVE_JEV_PYTHON` first, then a Python bundled in the app, `/opt/homebrew/bin/python3.13`, `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, and `/usr/bin/python3`. The development build has no bundled Python. If only the system Python 3.9 is available, the background service cannot run and the bar shows "Background service stopped." Run `brew install python@3.13`.

Check: `~/Applications/Live Jev.app` exists and opening it adds the waveform icon to the menu bar.

## 5. Set your TypeSafe API key

On first launch, Setup opens. Paste your TypeSafe API key there. Live Jev stores it in the macOS Keychain and passes it to the background service when the service starts. Setup also checks the Remote Script and the connection to Live. Reopen it from the menu bar icon → **Setup…**.

For terminal use, you can store the key in a shell profile instead:

```bash
echo 'export TYPESAFE_API_KEY="YOUR_KEY"' >> ~/.zshrc
```

The daemon reads `TYPESAFE_API_KEY` from the process environment or from an `export TYPESAFE_API_KEY=...` line in `~/.zshenv`, `~/.zprofile`, `~/.zshrc`, `~/.bash_profile`, `~/.bashrc`, or `~/.profile`.

Check with Live running. Setting `LIVE_JEV_LANG=en` makes the expected line English:

```bash
LIVE_JEV_LANG=en /opt/homebrew/bin/python3.13 cli.py status
# → one line such as "Live 12 tracks / 120 BPM"
```

## 6. Optional: add a Gemini API key

Add a Gemini API key in Setup only if you want Gemini to try phrases that neither local matching nor Jev can place. Live Jev sends the sentence, track and device names, and installed plug-in and Live device names to Google. It sends no audio or project files. Gemini API use may cost money.

Live Jev stores this key in the macOS Keychain. It does not read `GEMINI_API_KEY` from shell profile files. Developers who run `cli.py` directly can export `GEMINI_API_KEY=...` in that shell. `LIVE_JEV_LLM=0` disables Gemini even when a key is present.

Check: Setup shows **Saved in Keychain** under the Gemini field. Leave the field empty to keep Gemini off.

## 7. Use it
Bring Live to the front and press **⌘⇧Space** → type “mute” → Enter. The bar disappears at once and Live stays in front. It only comes back when it needs to ask you something. To undo the last successful command, including a success with a hidden result row, summon the bar and press ⌘Z.

Check from Terminal (mutes the selected track, then unmutes it):
```bash
/opt/homebrew/bin/python3.13 cli.py "mute" && /opt/homebrew/bin/python3.13 cli.py "unmute"
```

## Updating

After `git pull`, copy the Remote Script again with the commands in step 2, or press **Update** in Setup. Update the script first, then quit and reopen Live itself; restarting only Live Jev does not reload the script inside Live. If Swift files changed, run `bash scripts/build-app.sh` again. Setup compares the installed Remote Script with the version available from the current app or checkout and reports whether they match.

## Troubleshooting
| Symptom | Where to look |
| --- | --- |
| `plugin_script.py ping` prints `no answer` | Did you choose LiveJev in step 3 and restart Live afterwards? Live’s log (`~/Library/Preferences/Ableton/Live 12.*/Log.txt`) should contain `LiveJev: started, listening on port 9140` |
| “The Jev API key was not found.” | Add the key in Setup. If you use a shell profile instead, quit and reopen the app after adding the line from step 5 |
| `build-app.sh` says swift was not found | `xcode-select --install` |
| `build-app.sh` says `/opt/homebrew/bin/python3.13` is missing | `brew install python@3.13` |
| Nothing happens on ⌘⇧Space | Menu bar waveform icon → Show. Check that no other app uses the same shortcut |
| The app keeps saying “Starting background service…” | The cloned folder was moved or deleted (see step 1). Put it back, or repeat step 4 |
| The bar shows “Background service stopped” and only system Python 3.9 is installed | Run `brew install python@3.13` |
| “Python was not found” in `~/Library/Logs/LiveJev.log` | The app looks for Python in the order listed in step 4 |
| A plug-in name is not understood | Does the plug-in show up in Live’s browser? You can pin a nickname in `plugin_aliases.json`, for example `{"valhalla": "ValhallaVintageVerb"}` |

## Uninstall
Delete `~/Applications/Live Jev.app`, `~/Music/Ableton/User Library/Remote Scripts/LiveJev/` and the cloned folder, remove the `TYPESAFE_API_KEY` line from your shell profile, and set the Control Surface slot in Live back to None.
