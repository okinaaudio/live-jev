# Live Jev

**Control Ableton Live with one short sentence.**

Press **⌘⇧Space** while you work in Live and a small bar appears on top of it. Type (or dictate) something like “turn it down 3 dB”, “Serum 2 on a new track”, or “quantize to 1/16”, then hit Enter. The bar disappears instantly, Live stays in front, and the change is applied. The bar only comes back when it needs to ask you something.

English and Japanese are both supported, with many ways to say the same thing.

> **Status: app 1.02, Remote Script 0.18.** Live Jev is distributed as source, and this repository does not plan a packaged release.

## What it can do
- **Mixer** — volume, pan, mute, solo, arm, monitoring, sends (“mute”, “down by 3 dB”, “pan left 20”, “send A up a bit”). Return tracks can be addressed by letter or number and support volume, pan, mute, and solo. Master supports volume. Master cannot be muted, soloed, armed, or renamed. Returns cannot be armed. If you don’t name a target, Live Jev acts on the selected track.
- **Transport** — play, stop, record, loop, metronome, tempo, jump to bar, Live Jev undo, capture MIDI.
- **Clips and scenes** — launch and stop, loop, warp, pitch, gain.
- **Notes** — quantize (1/4 to 1/32, triplets, strength), legato, transpose by octaves or semitones, velocity, double the loop.
- **Devices** — insert plug-ins and Live’s own devices (“new track with Omnisphere”, “add EQ Eight”, “put Valhalla on the master”, “add Echo to return A”), turn devices on and off, and change parameters. Devices on returns addressed by letter or number, and devices on Master, are targets too. Instruments cannot be inserted on a return or Master. Candidates come from *your* Live browser. New tracks land where Live would put them and get Live’s default names.
- **Tracks** — add MIDI, audio, and return tracks, and rename ordinary or return tracks.
- **Several tracks at once** — mute, solo or arm a range, everything, or everything but one (“mute tracks 3 to 6”, “unsolo all”, “mute everything except Drums”, “solo only Bass”). These forms apply only to ordinary tracks. A single command that names two tracks, such as “mute Pad and Bass”, is not executed. Say “mute Pad and mute Bass”, or use a range, all, or except form.
- **Chains** — up to four commands in one sentence, in order (“mute Pad and lower Bass by 3 dB”, “mute Pad, then solo Drums and arm Bass”). Every part is checked first; if one part is unclear, nothing runs. If a later part fails, the earlier ones are put back. Works in Japanese too.
- **Undo** — Live Jev can undo one step only when it kept a receipt for the change. Receipts cover value changes such as volume, pan, mute, solo, arm, monitoring, sends, device on/off, parameters, tempo, clip settings, song switches, jumps, and renames. Live Jev writes back the value it read just before the change. It refuses if that value has since changed by hand. Inserted devices, added tracks, and note edits need Live’s own ⌘Z.

## How it works
- Meaning is decided by **Jev**, a small, fast model from [TypeSafe](https://typesafe.ai) that picks from a list of choices. Fixed phrases are answered locally without calling Jev at all. By default, no LLM is involved. You can enable the optional Gemini path described below.
- Live is driven through one small Python **Remote Script** (`LiveJev`) that runs inside Live. No Max for Live required. A command takes about 20 ms; reading the whole set takes about 10 ms.
- The bar is Swift (AppKit). The background service is Python 3.13, standard library only.

## Getting started

**Before you start**
- Live Jev needs **your own API key for TypeSafe’s Jev model**. Sign in at <https://console.typesafe.ai/> to create one; the docs are at <https://docs.typesafe.ai/>. It is a paid API — TypeSafe’s site listed $42 per billion input tokens in September 2026 (one command is a few hundred tokens). Check their site for current pricing.
- Live needs a one-time setup: a small **Remote Script** is copied into Live’s User Library and selected in Live’s settings. Nothing needs to be added to your sets.
- Live Jev is distributed as source. You build the app on your own Mac with a few Terminal commands, so you can read and change everything it does.

### Requirements
- Apple Silicon Mac (M1 or later), macOS 14 or later, Ableton Live 12 (Suite not required)
- [Homebrew](https://brew.sh) and the Xcode Command Line Tools (`xcode-select --install`)
- A TypeSafe API key

**The step-by-step guide, with a check for every step, is in [INSTALL.md](INSTALL.md).** You can also hand that file to an AI coding assistant and ask it to set things up for you.

### Quick start
```bash
brew install python@3.13
git clone https://github.com/okinaaudio/live-jev.git ~/live-jev
cd ~/live-jev

# the Remote Script that runs inside Live
mkdir -p ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev
cp remote_script/LiveJev/*.py ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev/

# build the app → ~/Applications/Live Jev.app
bash scripts/build-app.sh
open ~/Applications/Live\ Jev.app
```
**Do not move or delete the cloned folder after building** — the app runs `daemon.py` from it.

Then, in Live: Settings → **Link, Tempo & MIDI** → **Control Surface** → choose **LiveJev** in a free slot → restart Live. Open `~/Applications/Live Jev.app` (a waveform icon appears in the menu bar; there is no Dock icon), bring Live to the front, and press **⌘⇧Space**.

The first launch opens a **Setup** window. Paste your TypeSafe API key there to store it in the macOS Keychain. Setup also checks the Remote Script and the connection to Live. The menu bar icon lets you reopen Setup, switch the language (Automatic / Japanese / English), and launch at login.

For terminal use, you can put the TypeSafe key in `~/.zshrc` instead:

```bash
echo 'export TYPESAFE_API_KEY="YOUR_KEY"' >> ~/.zshrc
```

### About your plug-ins
- **There is nothing to import.** Live Jev reads the plug-in list from your own Live browser (Plug-ins, Instruments, Audio Effects, MIDI Effects) the first time you ask for a plug-in, in under a second, and remembers it. Only what you own becomes a candidate.
- The plug-in has to show up in Live’s browser (VST3/AU enabled and scanned in Live’s plug-in settings).
- Full names (`Serum 2`), partial names (`serum`) and nicknames all work. To pin a nickname, add it to `plugin_aliases.json`, for example `{"valhalla": "ValhallaVintageVerb"}`.
- When a plug-in is installed in several formats, Live Jev loads **VST3 first, then AU, then VST2**. Change the order with `LIVE_JEV_PLUGIN_FORMATS`, for example `au,vst3,vst`.
- Generic words such as “reverb”, “compressor” or “EQ” never insert anything; Live Jev suggests names instead.
- After installing a new plug-in, restart Live and reopen Live Jev.
- The author’s plug-in list is not in this repository. Tests and examples use real product names purely as examples.

### Optional: Gemini for phrases Jev cannot place

Gemini is off unless you add a Gemini API key in Setup. The app stores the key in the macOS Keychain and passes it to the background service through its environment. Live Jev deliberately does not read `GEMINI_API_KEY` from shell profile files. If you run `cli.py` directly, export `GEMINI_API_KEY=...` in that shell. Set `LIVE_JEV_LLM=0` to turn Gemini off even when a key is present.

Live Jev calls Gemini only when neither its local phrases nor Jev can turn the sentence into a command. The model is Gemini 3.5 Flash. It rewrites the sentence as one allowed command. Live Jev then checks the result against the current tracks and the plug-in catalogue, rejects invented names, and always asks you to confirm before it runs the command. Replies that went through Gemini show a small **Gemini** label.

Gemini is limited to five calls per minute with a two-second deadline. Live Jev does not retry the same phrase for 60 seconds. A timeout or unavailable response starts a 30-second cooldown, and a quota response starts a five-minute cooldown.

Google receives the sentence, track names and their device names, and the installed plug-in and Live device names. The request is limited to 64 KB. Live Jev sends no audio or project files. Gemini API use may cost money.

### What leaves your Mac
- The TypeSafe API receives **the sentence you typed** and the **names in the set you have open** that are needed to understand it (track, device, parameter, clip and scene names; plug-in names from your Live browser when you ask for a plug-in by name). No audio, no audio files, no project data.
- There is no other network traffic unless you enable the optional Gemini path described above. Everything between Live Jev and Live stays on `127.0.0.1`.

## Safety
- Only an allow-list of operations can be sent to Live. Deleting tracks or clips and free-form note writing are not possible.
- “Don’t …” is never executed. If you name a track that does not exist, nothing is written — it never falls back to another track. If you name no track, the selected track is used.
- A sentence with several commands is never half-executed: every part is checked before anything is written, and a failure puts the earlier parts back.
- Live Jev undo restores one receipted change and refuses to overwrite a value changed by hand. Use Live’s own ⌘Z for inserted devices, added tracks, and note edits.
- The Remote Script listens only on `127.0.0.1`. It closes a connection that sends anything other than a JSON command object, so a web page cannot drive it.
- The background service checks the Remote Script version before writing. If the script inside Live is older than 0.18, Live Jev refuses the write and asks you to update it in Setup and restart Live.
- Before writing to an ordinary or return track, Live Jev reads the target name from Live again. It also reads the device name again for device changes, including devices on Master. If a checked name changed, Live Jev refuses the write.
- A question from Live Jev (“Which track?”) expires after 30 seconds, so an old question cannot consume your next command.
- The TypeSafe key comes from the Keychain through Setup, the process environment, or a shell profile. The Gemini key comes only from the Keychain through Setup or the process environment. Live Jev never writes keys to a file or logs them.

## Make it yours
Everything is plain Python and Swift, and you build it yourself, so changing it is expected.
- **Nicknames for your plug-ins** — `plugin_aliases.json` next to `daemon.py`, for example `{"valhalla": "ValhallaVintageVerb"}`.
- **Nicknames for your tracks** — `aliases.json` next to `daemon.py`, for example `{"Lead Vox": ["vocals", "the singer"]}`. Live Jev passes them to Jev together with the track names.
- **More ways to say something** — English wording lives in the `ENGLISH_PHRASES` table at the top of `intent_en.py`; Japanese wording lives in the patterns in `intent.py`. Anything the local patterns do not catch goes to Jev.
- **The text of replies** — `messages.py`, one entry per message with `ja` and `en`.
- **New operations** — add the action to `actions.py` and to the allow-list in `bridge_client.py` (and `remote_script/LiveJev/lom_protocol.py` if Live needs a new call). Only allow-listed operations can ever reach Live.
- **Check your change** — run the unit tests, then the real-Live regression below. Python changes take effect when you quit and reopen the app; Swift changes need `bash scripts/build-app.sh`; Remote Script changes need a Live restart.

## Development
```bash
/opt/homebrew/bin/python3.13 -m unittest discover -s tests
bash scripts/build-app.sh
```
- **Real-Live regression:** open an empty Live set, add a MIDI track named `LJ-TEST` plus two more tracks and a return, select one of them, then run `python3 scripts/live_regression.py --check` (read-only) and `python3 scripts/live_regression.py`. It refuses to send anything to a set without the marker track, restores every change, and compares the set before and after.
- **Your own signed build:** no binaries are published here. If you want a self-contained app for your own use or your team, `scripts/release.sh` bundles a standalone Python, signs with *your* Developer ID, scans the bundle for private paths, and notarizes a `.dmg`. See [RELEASING.md](RELEASING.md).

## Updating

After `git pull`, copy `remote_script/LiveJev/*.py` into Live’s `Remote Scripts/LiveJev` folder again, or press **Update** in Setup. Update the script first, then quit and reopen Live itself; restarting only Live Jev does not reload the script inside Live. If Swift files changed, run `bash scripts/build-app.sh` again. Setup compares the installed Remote Script with the version available from the current app or checkout and reports whether they match.

## License
[MIT License](LICENSE) © 2026 Okina Audio
