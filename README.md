# Live Jev

**Ableton Live を、一言で。** / **Control Ableton Live with one short sentence.**

Live を触っている最中に **⌘⇧Space** を押すと、Live の上に小さなバーが出ます。「3dB下げて」「セラムを挿して」「クオンタイズして」と打って（または音声入力して）Enter。その瞬間にバーは消えて Live に戻り、操作が反映されます。分からなかったときだけ、もう一度出てきて聞き返します。

Press **⌘⇧Space** while working in Live, type (or dictate) something like “turn it down 3 dB”, “put Serum on a new track”, or “quantize to 1/16”, and hit Enter. The bar disappears instantly, Live stays in front, and the change is applied. It only comes back when it needs to ask you something. Japanese and English are both supported.

> **Status: early (0.1x).** It works end to end on the author’s machine, but there is no installer yet — setup is for people comfortable with a terminal. 1.00 will be the first packaged release.

## できること / What it can do
- **ミキサー / Mixer**: 音量・パン・ミュート・ソロ・録音待機・モニター・センド。トラックを言わなければ選択中のトラックに効きます（“mute”, “down by 3 dB”, “pan left 20”, “send A up a bit”）
- **トランスポート / Transport**: 再生・停止・録音・ループ・メトロノーム・テンポ・小節へ移動・取り消し／やり直し・キャプチャ
- **クリップとシーン / Clips & scenes**: 発射・停止、ループ・ワープ・ピッチ・ゲイン
- **ノート / Notes**: クオンタイズ（4分〜32分・3連・強さ）、レガート、オクターブ／半音の移調、ベロシティ、ループを倍
- **デバイス / Devices**: プラグインと内蔵デバイスの挿入（「新しいトラックでOmnisphere開いて」）、オン／オフ。候補はあなたの Live のブラウザから読むので、持っているプラグインがそのまま使えます。新しいトラックの位置と名前は Live の作法どおり
- **トラック / Tracks**: 追加・名前変更
- どれも **元に戻せます**（バーの矢印か ⌘Z）

## しくみ / How it works
- 意味の判定は [TypeSafe](https://typesafe.ai) の **Jev**（選択肢から選ぶ小さく速いモデル）。決まった言い回しは Jev にも聞かず手元で即答します。LLM は使いません
- Live との通信は、Live の中で動く小さな Python 部品（Remote Script `LiveJev`）1本。Max for Live は不要。1命令 約20ms、曲全体の読み取り 約10ms
- バーは Swift（AppKit）、常駐は Python 3.13（標準ライブラリのみ）

## はじめての方へ / Getting started

**先に知っておくこと / Before you start**
- Live Jev は、あなたの一言の意味を判定するために **TypeSafe 社の Jev という AI の API** を使います。使うには、あなた自身の **API キー**（合言葉）が必要です。キーは TypeSafe のコンソール（<https://console.typesafe.ai/>）にサインインして発行します。使い方の資料は <https://docs.typesafe.ai/>。料金は TypeSafe のサイトの表示では入力 10 億トークンあたり 42 ドル（2026年9月時点。1回の指示は数百トークン程度）。最新の料金と無料枠の有無は TypeSafe のサイトで確認してください
- Live 側には、**Live の中で動く小さな部品（Remote Script）を1つ入れて、Live の設定で選ぶ**作業が1回だけ必要です。曲ごとの準備は要りません
- いまはインストーラーが無いので、**ターミナルで数行のコマンドを打つ**必要があります

Live Jev needs **your own API key for TypeSafe’s Jev model** (sign in at <https://console.typesafe.ai/>, docs at <https://docs.typesafe.ai/>; pricing on their site was $42 per billion input tokens as of Sep 2026 — check the site for current pricing), plus a one-time install of a small **Remote Script** inside Live. There is no installer yet, so setup takes a few terminal commands.

### 必要なもの / Requirements
- Apple Silicon の Mac（M1 以降）・macOS 14 以上・Ableton Live 12（Suite でなくても可）
- [Homebrew](https://brew.sh) と Xcode のコマンドラインツール（`xcode-select --install`）
- TypeSafe の API キー

### 手順 / Steps
```bash
# 1. 道具を入れる / tools
brew install python@3.13

# 2. このリポジトリを取得 / get the code
git clone https://github.com/okinaaudio/live-jev.git
cd live-jev

# 3. Live の中の部品を入れる / install the Remote Script
mkdir -p ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev
cp remote_script/LiveJev/*.py ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev/

# 4. API キーを設定（「あなたのキー」を自分のキーに置き換える）/ set your key
echo 'export TYPESAFE_API_KEY="あなたのキー"' >> ~/.zshrc

# 5. アプリを作る / build the app  →  ~/Applications/Live Jev.app
bash scripts/build-app.sh
```
6. **Live の設定**: Live を起動 → 設定（Preferences）→ **Link, Tempo & MIDI** → **コントロールサーフェス（Control Surface）** の空いている欄で **LiveJev** を選ぶ → Live を再起動
7. `~/Applications/Live Jev.app` を開く（メニューバーに波形のアイコンが出ます。Dock には出ません）
8. Live を手前にして **⌘⇧Space** → 「ミュート」や “mute” と打って Enter

メニューバーの波形アイコンから、言語（自動／日本語／English）とログイン時の起動を切り替えられます。

### あなたのプラグインについて / About your plug-ins
- **読み込ませる作業は要りません。** Live Jev は、あなたの Live のブラウザ（Plug-ins・Instruments・Audio Effects・MIDI Effects）から一覧を自動で読みます。最初にプラグインを頼んだときに1回読み（1秒以内）、あとは覚えています。だから、あなたが持っているプラグインだけが候補になります
- 条件は1つ。そのプラグインが **Live のブラウザに出ていること**（Live のプラグイン設定で VST3／AU が有効で、スキャン済み）
- 呼び方は、正式名（`Serum 2`）、名前の一部（`serum`）、カタカナ（「セラム」）のどれでも通じます。カタカナや略称は Jev が一覧の中から選びます。決まった呼び方にしたいときは `plugin_aliases.json` に書きます（例: `{"バルハラ": "ValhallaVintageVerb"}`）
- 「リバーブ」「コンプ」「EQ」のような**種類の名前だけ**では挿しません（どれのことか決められないため）。候補を案内します
- 新しくプラグインを入れたら、Live を再起動して Live Jev も開き直すと候補に入ります
- このリポジトリに、作者のプラグイン一覧は入っていません。テストに出てくる製品名は例です

Nothing to import: Live Jev reads the plug-in list from **your own Live browser** the first time you ask for a plug-in, and only those become candidates. Full names, partial names, and katakana/nicknames all work; pin a nickname in `plugin_aliases.json` if you like. Generic words such as “reverb” never insert anything. After installing a new plug-in, restart Live and reopen Live Jev.

### うまくいかないとき / Troubleshooting
- **何を打っても「Liveに繋がりません」**: 手順6の設定で LiveJev が選ばれているか、選んだあと Live を再起動したかを確認
- **「Jevの鍵が見つかりません」**: 手順4のあと、アプリを一度終了して開き直す（キーは環境変数 `TYPESAFE_API_KEY`、無ければ `~/.zshrc` の `export TYPESAFE_API_KEY=...` の行から読みます）
- **⌘⇧Space で何も出ない**: メニューバーの波形アイコン →「呼び出す」。他のアプリが同じショートカットを使っていないか確認
- **プラグイン名が通じない**: `plugin_aliases.json` に呼び名を書く（例: `{"セラム": "Serum 2"}`）

### 送られる情報 / What leaves your Mac
- TypeSafe の API に送るのは、**あなたが打った一言**と、判定に必要な**いま開いている曲の名前の一覧**（トラック名・デバイス名・つまみ名・クリップ名・シーン名、プラグインを名前で探すときは Live のブラウザのプラグイン名）だけです。音声や音声ファイル、曲のデータそのものは送りません
- それ以外の通信はありません。Live とのやり取りは Mac の中（127.0.0.1）で完結します

## 安全のための決まり / Safety
- 送れる命令は許可リストで限定しています（トラックやクリップの削除、ノートの自由な書き込みはできません）
- 「〜しないで」“don’t …” は実行しません。名前を言ったのに見つからないトラックには書きません
- あなたのプラグイン一覧などはこのリポジトリに含まれません（各自の Live から毎回読みます）

## 開発 / Development
```bash
/opt/homebrew/bin/python3.13 -m unittest discover -s tests
bash scripts/build-app.sh
```
言い換え用の LLM の車線（`llm_rewrite.py`）はコードに残っていますが、既定でオフです（`LIVE_JEV_LLM=1` で試せます）。
好みの呼び名は `plugin_aliases.json`（例: `{"セラム": "Serum 2"}`）で固定できます。

## ライセンス / License
[MIT License](LICENSE) © 2026 Okina Audio
