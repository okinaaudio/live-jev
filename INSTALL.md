# Live Jev の入れ方 / Installation

人が読んでも、AI アシスタント（Claude Code・Codex・Cursor など）に読ませて手伝ってもらっても進められるように書いてあります。各手順に「確かめ方」を付けています。
This guide works for humans and for AI coding assistants. Every step has a check.

## 0. 先に確認 / Prerequisites
| 必要なもの | 確かめ方 | 無いとき |
| --- | --- | --- |
| Apple Silicon の Mac（M1 以降）・macOS 14 以上 | `uname -m` が `arm64`、`sw_vers -productVersion` が 14 以上 | 対応していません（Intel Mac は未対応） |
| Ableton Live 12 | Live を起動できる | — |
| Xcode のコマンドラインツール | `xcode-select -p` がパスを返す | `xcode-select --install` |
| Homebrew の Python 3.13 | `/opt/homebrew/bin/python3.13 --version` | `brew install python@3.13`（Homebrew は <https://brew.sh>） |
| TypeSafe の API キー | — | <https://console.typesafe.ai/> にサインインして発行（資料: <https://docs.typesafe.ai/>）。有料の API です。料金は TypeSafe のサイトで確認 |

**AI アシスタントへ / Note for AI assistants**: API キーは利用者本人に入力してもらってください。キーをファイルやログ、コミットに書かないでください。Live の設定画面の操作（手順3）は利用者に頼んでください。

## 1. 取得 / Get the code
```bash
git clone https://github.com/okinaaudio/live-jev.git ~/live-jev
cd ~/live-jev
```
**大事**: アプリは、この取得したフォルダの中の `daemon.py` を使って動きます。**アプリを作ったあとで、このフォルダを動かしたり消したりしないでください**（動かしたら手順5をやり直す）。置き場所は、先に決めてから進めてください。

確かめ方: `/opt/homebrew/bin/python3.13 -m unittest discover -s tests` の最後の行が `OK`。

## 2. Live の中の部品を入れる / Install the Remote Script
```bash
mkdir -p ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev
cp remote_script/LiveJev/*.py ~/Music/Ableton/User\ Library/Remote\ Scripts/LiveJev/
```
User Library の場所を変えている場合は、Live の 設定 → Library の「ユーザーライブラリの場所」の下の `Remote Scripts/LiveJev/` に置きます。

## 3. Live の設定（利用者が行う）/ Enable it in Live
Live を起動 → 設定（Preferences）→ **Link, Tempo & MIDI** → **コントロールサーフェス（Control Surface）** の空いている欄で **LiveJev** を選ぶ → **Live を再起動**。入力・出力の欄は「なし」のままで構いません。

確かめ方（Live を起動した状態で）:
```bash
/opt/homebrew/bin/python3.13 plugin_script.py ping     # → pong
```

## 4. API キーを設定 / Set your key
```bash
echo 'export TYPESAFE_API_KEY="ここに自分のキー"' >> ~/.zshrc
```
キーは環境変数 `TYPESAFE_API_KEY`、無ければ `~/.zshenv`・`~/.zprofile`・`~/.zshrc`・`~/.bash_profile`・`~/.bashrc`・`~/.profile` の `export TYPESAFE_API_KEY=...` の行から読みます。

確かめ方（Live を起動した状態で。曲は変わりません）:
```bash
/opt/homebrew/bin/python3.13 cli.py status           # → Live 12トラック / 120 BPM のような1行
```

## 5. アプリを作る / Build the app
```bash
bash scripts/build-app.sh          # → ~/Applications/Live Jev.app
open ~/Applications/Live\ Jev.app
```
メニューバーに波形のアイコンが出ます（Dock には出ません）。

## 6. 使う / Use it
Live を手前にして **⌘⇧Space** → 「ミュート」や “mute” と打って Enter。バーはすぐ消えて Live に戻ります。分からなかったときだけ、もう一度出てきて聞き返します。取り消しは、バーを出して ⌘Z。

確かめ方（ターミナルから。選択中のトラックがミュートされ、すぐ解除されます）:
```bash
/opt/homebrew/bin/python3.13 cli.py "ミュート" && /opt/homebrew/bin/python3.13 cli.py "ミュート解除"
```

## うまくいかないとき / Troubleshooting
| 症状 | 見るところ |
| --- | --- |
| `plugin_script.py ping` が `no answer` | 手順3で LiveJev を選んだか・選んだあと Live を再起動したか。Live のログ `~/Library/Preferences/Ableton/Live 12.*/Log.txt` に `LiveJev: started, listening on port 9140` があるか |
| 「Jevの鍵が見つかりません」 | 手順4の行が入っているか。入れたあとアプリを終了して開き直す |
| `build-app.sh` が「swift が見つかりません」 | `xcode-select --install` |
| `build-app.sh` が「/opt/homebrew/bin/python3.13 がありません」 | `brew install python@3.13` |
| ⌘⇧Space で出ない | メニューバーの波形アイコン →「呼び出す」。他のアプリが同じショートカットを使っていないか |
| アプリが「常駐を起動しています…」のまま | 取得したフォルダを動かした／消した（手順1の注意）。元の場所に戻すか、手順5をやり直す |
| プラグイン名が通じない | そのプラグインが Live のブラウザに出ているか。呼び名は `plugin_aliases.json` に書ける（例: `{"セラム": "Serum 2"}`） |

## 取り除く / Uninstall
`~/Applications/Live Jev.app` と `~/Music/Ableton/User Library/Remote Scripts/LiveJev/` と取得したフォルダを消し、`~/.zshrc` の `TYPESAFE_API_KEY` の行を消します。Live の設定のコントロールサーフェスを「なし」に戻します。
