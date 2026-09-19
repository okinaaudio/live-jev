# Live Jev

**Control Ableton Live with one short sentence.**

Press **⌘⇧Space** while you work in Live and a small bar appears on top of it. Type (or dictate) something like “turn it down 3 dB”, “Serum 2 on a new track”, or “quantize to 1/16”, then hit Enter. The bar disappears instantly, Live stays in front, and the change is applied. The bar only comes back when it needs to ask you something.

English and Japanese are both supported, with many ways to say the same thing.

> **Status: early (0.1x).** It works end to end, but there is no installer yet — setup takes a few terminal commands. 1.00 will be the first packaged release.

## What it can do
- **Mixer** — volume, pan, mute, solo, arm, monitoring, sends (“mute”, “down by 3 dB”, “pan left 20”, “send A up a bit”). If you don’t name a track, it acts on the selected one.
- **Transport** — play, stop, record, loop, metronome, tempo, jump to bar, undo/redo, capture MIDI.
- **Clips and scenes** — launch and stop, loop, warp, pitch, gain.
- **Notes** — quantize (1/4 to 1/32, triplets, strength), legato, transpose by octaves or semitones, velocity, double the loop.
- **Devices** — insert plug-ins and Live’s own devices (“new track with Omnisphere”, “add EQ Eight”), turn devices on and off. Candidates come from *your* Live browser, so whatever you own just works. New tracks land where Live would put them and get Live’s default names.
- **Tracks** — add and rename.
- Everything can be **undone** (the arrow in the bar, or ⌘Z).

## How it works
- Meaning is decided by **Jev**, a small, fast model from [TypeSafe](https://typesafe.ai) that picks from a list of choices. Fixed phrases are answered locally without calling Jev at all. No LLM is involved; when Live Jev is unsure, it asks.
- Live is driven through one small Python **Remote Script** (`LiveJev`) that runs inside Live. No Max for Live required. A command takes about 20 ms; reading the whole set takes about 10 ms.
- The bar is Swift (AppKit). The background service is Python 3.13, standard library only.

## Getting started

**Before you start**
- Live Jev needs **your own API key for TypeSafe’s Jev model**. Sign in at <https://console.typesafe.ai/> to create one; the docs are at <https://docs.typesafe.ai/>. It is a paid API — TypeSafe’s site listed $42 per billion input tokens in September 2026 (one command is a few hundred tokens). Check their site for current pricing.
- Live needs a one-time setup: copy a small **Remote Script** and select it in Live’s settings. Nothing needs to be added to your sets.
- There is no installer yet, so you will type a few commands in Terminal.

**The step-by-step guide, with a check for every step, is in [INSTALL.md](INSTALL.md).** You can also hand that file to an AI coding assistant and ask it to set things up for you.

### Requirements
- Apple Silicon Mac (M1 or later), macOS 14 or later, Ableton Live 12 (Suite not required)
- [Homebrew](https://brew.sh) and the Xcode Command Line Tools (`xcode-select --install`)
- A TypeSafe API key

### Quick start
```bash
brew install python@3.13
git clone https://github.com/okinaaudio/live-jev.git ~/live-jev
cd ~/live-jev

# the Remote Script that runs inside Live
mkdir -p ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev
cp remote_script/LiveJev/*.py ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev/

# your key (replace YOUR_KEY)
echo 'export TYPESAFE_API_KEY="YOUR_KEY"' >> ~/.zshrc

# build the app → ~/Applications/Live Jev.app
bash scripts/build-app.sh
```
**Do not move or delete the cloned folder after building** — the app runs `daemon.py` from it.

Then, in Live: Settings → **Link, Tempo & MIDI** → **Control Surface** → choose **LiveJev** in a free slot → restart Live. Open `~/Applications/Live Jev.app` (a waveform icon appears in the menu bar; there is no Dock icon), bring Live to the front, and press **⌘⇧Space**.

The menu bar icon lets you switch the language (Automatic / Japanese / English) and launch at login.

### About your plug-ins
- **There is nothing to import.** Live Jev reads the plug-in list from your own Live browser (Plug-ins, Instruments, Audio Effects, MIDI Effects) the first time you ask for a plug-in, in under a second, and remembers it. Only what you own becomes a candidate.
- The plug-in has to show up in Live’s browser (VST3/AU enabled and scanned in Live’s plug-in settings).
- Full names (`Serum 2`), partial names (`serum`) and nicknames all work. To pin a nickname, add it to `plugin_aliases.json`, for example `{"valhalla": "ValhallaVintageVerb"}`.
- When a plug-in is installed in several formats, Live Jev loads **VST3 first, then AU, then VST2**. Change the order with `LIVE_JEV_PLUGIN_FORMATS`, for example `au,vst3,vst`.
- Generic words such as “reverb”, “compressor” or “EQ” never insert anything; Live Jev suggests names instead.
- After installing a new plug-in, restart Live and reopen Live Jev.
- The author’s plug-in list is not in this repository. Product names in the tests are examples.

### What leaves your Mac
- The TypeSafe API receives **the sentence you typed** and the **names in the set you have open** that are needed to understand it (track, device, parameter, clip and scene names; plug-in names from your Live browser when you ask for a plug-in by name). No audio, no audio files, no project data.
- There is no other network traffic. Everything between Live Jev and Live stays on your Mac (127.0.0.1).

## Safety
- Only an allow-list of operations can be sent to Live. Deleting tracks or clips and free-form note writing are not possible.
- “Don’t …” is never executed. If you name a track that does not exist, nothing is written — it never falls back to another track. If you name no track, the selected track is used.
- Two requests in one sentence (“mute Pad and solo Bass”) are declined rather than half-executed. Send them one at a time.
- A question from Live Jev (“Which track?”) expires after about 20 seconds, so an old question can never swallow your next command.
- Your API key is read from the environment (or your shell profile) at run time and is never written to a file.

## Development
```bash
/opt/homebrew/bin/python3.13 -m unittest discover -s tests
bash scripts/build-app.sh
```
An optional LLM rewrite lane (`llm_rewrite.py`) is still in the code but off by default (`LIVE_JEV_LLM=1` to try it).

## License
[MIT License](LICENSE) © 2026 Okina Audio
