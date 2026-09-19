# LiveJev（呼び出し型の入力バー）

macOS 14 以降向けの小さな入力バーです。Swift 側は Jev や Live に直接つながず、同じフォルダの親にある `daemon.py`（Python 3.13）を起動して、1行1JSONでやり取りします。

```sh
swift build -c release --scratch-path ~/dev/live-jev-build
~/dev/live-jev-build/release/LiveJev
```

`daemon.py` の場所を変えるときは `LIVE_JEV_DAEMON=/absolute/path/to/daemon.py` を指定します。普段は `scripts/build-app.sh` で `.app` にして使います。

- `⌘⇧Space`: 呼び出す／隠す　`Enter`: 送ってすぐ Live に戻る　`Esc`: 隠す　`↑`/`↓`: 入力履歴　`⌘Z`（入力が空のとき）: 元に戻す
- メニューバーの波形アイコン: 呼び出す・ログイン時に起動・言語（自動／日本語／English）・詳細を表示・終了
