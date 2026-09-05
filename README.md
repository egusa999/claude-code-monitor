# Claude Code Monitor 説明書

Claude Code の稼働中セッションを一覧表示するモニタリングツールです。
「セッション名(右の「UUID」ボタンをクリックするとセッションIDを表示/非表示)」
「コンテキスト使用量(% + トークン数)」「状態ランプ(作業中=赤点滅 / 待機中=緑点灯)」を表示します。
(WebブラウザUIのみ。デスクトップGUI版はセッションIDを表示しません)

同梱ファイルは以下の通りです。モニター本体は `claude_monitor.py` / `claude_monitor_web.py` の
**どちらか一方だけ使えば動きます**。`daily_kickoff.py` 以下は「毎朝4時 Hello送信」機能用です。

| ファイル | 用途 |
|---|---|
| `claude_monitor.py` | デスクトップGUI版(tkinter)。GUIのある環境、またはX11転送で使用 |
| `claude_monitor_web.py` | Webブラウザ版。**Proxmox LXC等のヘッドレス環境ではこちらを推奨** |
| `daily_kickoff.py` | 毎朝4時(JST)に Claude へ "Hello" を送るスクリプト本体(詳細は後述) |
| `kickoff_state.json` | `daily_kickoff.py` の状態(有効/無効・前回実行結果)。実行時に自動生成 |
| `kickoff.log` | `daily_kickoff.py` の簡易ログ。実行時に自動生成 |
| `systemd/claude-daily-kickoff.service` / `.timer` | `daily_kickoff.py` を毎朝4時JSTに起動するsystemdユニット |

どちらも Python 標準ライブラリのみで動作します(追加インストール不要。GUI版のみ環境によって `tkinter` の追加インストールが必要な場合があります)。

---

## 前提: このツールが読みに行くデータ

以下は Claude Code が自動生成するファイルで、**このツールを実行するホスト上に存在している必要があります**(=SSH先のリモートに置くのではなく、Claude Codeセッションが実際に動いているマシン/コンテナ上で実行してください)。

- `~/.claude/sessions/*.json` … 起動中セッションのメタ情報(PID, セッションID, 作業ディレクトリ等)
- `~/.claude/projects/*/<セッションID>.jsonl` … 会話トランスクリプト(コンテキスト使用量の算出に使用)

上記が存在しない/読めない場合、セッションは「アクティブなセッションはありません」と表示されます。

---

## 配置方法

1. このZIP内の2ファイルを、Claude Codeを実行しているホスト(Proxmox LXCコンテナ内)の任意のフォルダに展開する
   ```bash
   mkdir -p ~/claude_monitor
   # zipを展開して2ファイルをここに置く
   ```
2. Python3が入っていることを確認
   ```bash
   python3 --version
   ```

---

## 使い方A: Webブラウザ版(推奨・ヘッドレス環境向け)

### 1. LXCコンテナ側でサーバーを起動
```bash
cd ~/claude_monitor
python3 claude_monitor_web.py --port 8765
```
デフォルトで `127.0.0.1` にのみバインドします(LAN上に公開されません)。
`Ctrl+C` で停止します。

LAN内から直接(SSHトンネルなしで)アクセスしたい場合は `--host 0.0.0.0` を指定してください。
Tailscale等のVPNに参加させている場合は、その仮想IPからも同じポートで届きます。
```bash
python3 claude_monitor_web.py --host 0.0.0.0 --port 8765
```
⚠️ `0.0.0.0`で公開すると**認証なしで誰でも閲覧できます**(セッション名・作業ディレクトリ・
コンテキスト使用率が見える)。LANやVPN内に信頼できない端末がいる環境では`127.0.0.1`のまま
SSHトンネル経由での利用を推奨します。

### 2a. SSHトンネル経由で見る場合
```bash
ssh -L 8765:localhost:8765 <ユーザー名>@<LXCのIPまたはホスト名>
```
このSSH接続を維持したまま、ブラウザで `http://localhost:8765` を開く。

### 2b. LAN/Tailscale直接アクセスの場合
```
http://<LXCのLAN IPまたはTailscale IP>:8765
```

1.5秒ごとに自動更新されます。作業中のセッションは赤いランプが点滅します。

### インストールして独立ウィンドウ化(PWA)
PWA(manifest/アイコン/Service Worker)に対応しているため、Chrome/Edgeの「インストール」機能で
タブ・アドレスバーのない独立ウィンドウとして開けます。

⚠️ **ブラウザの仕様上、インストール機能は「セキュアなコンテキスト」(HTTPS、または`localhost`)
でしか使えません。** `http://<LAN IP>:8765` のような素のHTTPアクセスではインストールボタンが
出ません。確実にインストールしたい場合は上記2a(SSHトンネル→`localhost:8765`)を使ってください。

### 常時起動しておきたい場合(任意)
`screen` や `tmux` で常駐させるか、systemdサービス化してください。

例(systemdサービス化。LAN/Tailscaleへの直接公開ありの場合):
```ini
# /etc/systemd/system/claude-monitor.service
[Unit]
Description=Claude Code Monitor (Web)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /root/claude_monitor/claude_monitor_web.py --host 0.0.0.0 --port 8765
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
```
```bash
systemctl daemon-reload
systemctl enable --now claude-monitor
```
実運用(claudecode LXCコンテナ)では上記の設定で`claude-monitor.service`として常駐化済み。

---

## 使い方B: デスクトップGUI版

GUIが直接使える環境(コンテナにデスクトップ環境がある、またはSSHのX11フォワーディングを使う)向けです。

### ローカルにGUI環境がある場合
```bash
python3 claude_monitor.py
```
`tkinter` が入っていないと `ModuleNotFoundError: No module named 'tkinter'` になります。Debian/Ubuntu系なら:
```bash
apt install python3-tk
```

### SSH経由でGUIを手元に転送する場合(X11フォワーディング)
- リモート(LXC)側: `sshd_config` に `X11Forwarding yes`、`xauth` パッケージが入っていること
- 手元側: Xサーバーが必要(Windows: VcXsrv / X410、Mac: XQuartz、Linuxは標準で可のことが多い)

```bash
ssh -X <ユーザー名>@<LXCのIPまたはホスト名>
python3 claude_monitor.py
```

---

## 設定値の変更(任意)

### コンテキスト上限(重要・環境によって必ず確認)

コンテキスト使用率は `使用トークン数 ÷ CONTEXT_TOKEN_LIMIT` で計算しています。この上限値は
契約/モデルによって **200,000** だったり **1,000,000(拡張コンテキスト)** だったりするため、
既定値のままだと Claude Code の `/context` コマンドの表示(%)とズレます。

`/context` を実行すると `Tokens: 96.6k / 1m (10%)` のように出るので、右側の分母
(この例では `1m` = 1,000,000)を確認し、以下のいずれかの方法で合わせてください。

- 環境変数で指定(両ファイル共通、恒久的にしたい場合は `.bashrc` 等に追記):
  ```bash
  export CLAUDE_CONTEXT_TOKEN_LIMIT=1000000
  python3 claude_monitor_web.py --port 8765
  ```
- Web版はコマンドライン引数でも指定可能:
  ```bash
  python3 claude_monitor_web.py --port 8765 --context-limit 1000000
  ```
- 何も指定しない場合の既定値は `1,000,000` です。200,000トークン契約の場合は明示的に
  `200000` を指定してください。

### その他の定数

両ファイル冒頭に定数が並んでいます。必要に応じて書き換えてください。

- `WORKING_THRESHOLD_SEC`(既定 6秒): トランスクリプトがこの秒数以内に更新されていれば「作業中(赤)」と判定
- `POLL_INTERVAL_MS`(既定 1500ms): 一覧の再取得間隔
- `BLINK_INTERVAL_MS`(GUI版のみ、既定 500ms): 赤ランプの点滅間隔

Web版はブラウザ側のポーリング間隔(`POLL_INTERVAL_MS`)を変更した場合、サーバー再起動が必要です(HTML生成時に埋め込まれるため)。

---

## 「毎朝4時 Hello送信」機能(daily_kickoff.py)

毎朝4時(JST)に `claude -p -n daily-4am-hello "Hello"` を実行し、新規セッションを1つ
開始する常駐スケジューラです。`-p`(print/非対話モード)を使っているため応答が返れば
プロセスは即終了し、`--bg` のようにバックグラウンドエージェントとして残り続けて
セッション一覧や `claude agents` に溜まっていくことはありません。会話自体は通常の
セッションと同様に履歴へ保存されます。

Web版モニターのトップに稼働状態パネルが表示され、有効/無効をトグルできます。
`claude_monitor_web.py` は同じフォルダの `daily_kickoff.py` を import して、
状態の読み書き(`/api/kickoff`, `/api/kickoff/toggle`)に使います。

### 常駐化(systemdタイマー)

```bash
cp systemd/claude-daily-kickoff.service systemd/claude-daily-kickoff.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now claude-daily-kickoff.timer
```

`claude-daily-kickoff.service` の `ExecStart` は本番配置場所
(`/root/claude_monitor/daily_kickoff.py`)を指しているため、`claude_monitor_web.py` 同様
デプロイ手順(下記)でコピーしておくこと。

タイマーは `OnCalendar=*-*-* 04:00:00 Asia/Tokyo` で毎朝4時JST(=系統によっては前日19:00 UTC)
に発火。`enable`済みなのでLXC再起動後も自動的に有効なまま。`Persistent=true` のため、
LXCが4時をまたいで停止していた場合も起動後に1回分を実行します。

次回実行予定の確認:
```bash
systemctl list-timers claude-daily-kickoff.timer
```

### 有効/無効の切り替え

Web版パネルのトグルから操作するのが基本だが、CLIからも操作可能:

```bash
python3 daily_kickoff.py --set-enabled false   # 無効化(次回はスキップされる)
python3 daily_kickoff.py --set-enabled true    # 有効化
python3 daily_kickoff.py --status              # 現在の状態を表示
```

無効化してもsystemdタイマー自体は動き続けるが、`daily_kickoff.py` 側で
「スキップ」として記録するだけで `claude` は起動しません。

### 手動実行(動作確認用)

```bash
python3 daily_kickoff.py
```

実際に `claude -p` が実行され、Claude API/セッション利用が発生する点に注意。

---

## デプロイ手順(本番反映)

`claude_monitor.py` / `claude_monitor_web.py` / `daily_kickoff.py` / `kickoff_state.json` を
編集したら、本番配置場所 `/root/claude_monitor/` へコピーして関連サービスを再起動する。

```bash
cp claude_monitor_web.py claude_monitor.py daily_kickoff.py /root/claude_monitor/
systemctl restart claude-monitor.service
```

`systemd/*.service` / `*.timer` を編集した場合は `/etc/systemd/system/` へコピーして
`systemctl daemon-reload` すること。

---

## トラブルシューティング

| 症状 | 確認事項 |
|---|---|
| セッションが1件も出ない | `~/.claude/sessions/` と `~/.claude/projects/` がこのホスト上に存在するか確認。別ホスト/別コンテナのファイルは見えません |
| `ModuleNotFoundError: No module named 'tkinter'` | GUI版のみで発生。`apt install python3-tk` などでインストール、またはWeb版を使う |
| Webブラウザで開けない | SSHトンネル(`ssh -L 8765:localhost:8765 ...`)を張ったまま接続しているか確認。サーバーが起動しているか(`python3 claude_monitor_web.py`実行中か)を確認 |
| ランプがずっと緑のまま | 実際に該当セッションでClaudeが処理中か確認。処理直後は最大 `WORKING_THRESHOLD_SEC` 秒ほど反映に遅れが出ます |
| コンテキスト%が `/context` と合わない | 上記「コンテキスト上限」の設定を `/context` の分母(200k or 1m)に合わせてください。それでもズレる場合はサブエージェント(Task)実行中の可能性があるので、少し待ってから再確認してください |
| ブラウザにインストールボタンが出ない | セキュアコンテキスト(HTTPSまたは`localhost`)でしかインストールできない仕様。素のLAN/Tailscale IPへのHTTPアクセスでは出ない。SSHトンネル経由の`localhost:8765`で開いてください |
| `--host 0.0.0.0`にしたらセッション情報が誰でも見えるようになった | 仕様通り(認証なし)。信頼できない端末がいるネットワークでは`127.0.0.1`+SSHトンネル運用に戻してください |
