#!/usr/bin/env python3
"""
Claude Code Monitor (Web版)
============================
Proxmox LXC のようなヘッドレス環境(GUI/Xサーバーなし)でも動かせるように、
claude_monitor.py と同じ検出ロジックをブラウザから見られる形にしたもの。
PWA対応(manifest/アイコン/Service Worker)により、セキュアコンテキスト
(https、またはSSHトンネル経由のlocalhost)からは独立ウィンドウとしてインストール可能。

使い方(LXCコンテナ上で実行):
    python3 claude_monitor_web.py --port 8765

手元のPCから見る(SSHポートフォワード):
    ssh -L 8765:localhost:8765 user@lxc-host
    ブラウザで http://localhost:8765 を開く

依存: 標準ライブラリのみ(http.server)。claude_monitor.py と同じフォルダに置くこと。
"""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import claude_monitor
from claude_monitor import get_sessions

# 「毎朝4時 Hello送信」スクリプト。同じフォルダに daily_kickoff.py を置くこと。
try:
    import daily_kickoff
except ImportError:
    daily_kickoff = None

POLL_INTERVAL_MS = 1500  # ブラウザ側のポーリング間隔(claude_monitor.pyと合わせる)
BLINK_INTERVAL_MS = 500

INDEX_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code Monitor</title>
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/icon.svg">
<meta name="theme-color" content="#171a26">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Claude Monitor">
<style>
  :root {
    color-scheme: dark;
    --bg: #171a26;
    --titlebar: #12141d;
    --row-bg: #1c2032;
    --row-alt-bg: #20243a;
    --border: #2a2f45;
    --text: #e7e9f5;
    --text-dim: #8a8fa8;
    --accent: #8b6cf6;
    --track: #2a2f45;
    --red-on: #ff4d4f;
    --red-off: #5a2a2c;
    --green: #3ddc84;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: "Segoe UI", "Hiragino Sans", sans-serif;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 24px 12px;
  }
  .panel {
    width: 100%;
    max-width: 560px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  .titlebar {
    background: var(--titlebar);
    padding: 10px 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 600;
    font-size: 14px;
  }
  .titlebar .dot { color: var(--accent); margin-right: 6px; }
  .header {
    display: grid;
    grid-template-columns: 1fr 190px 60px;
    padding: 8px 14px 6px;
    font-size: 11px;
    color: var(--text-dim);
    letter-spacing: 0.03em;
  }
  .row {
    display: grid;
    grid-template-columns: 1fr 190px 60px;
    align-items: center;
    padding: 10px 14px;
    font-size: 14px;
  }
  .row:nth-child(odd) { background: var(--row-bg); }
  .row:nth-child(even) { background: var(--row-alt-bg); }
  .name { font-weight: 600; }
  .uuid-btn {
    font-weight: 400;
    font-size: 10px;
    color: var(--text-dim);
    font-family: "Consolas", "SFMono-Regular", monospace;
    margin-left: 8px;
    padding: 1px 6px;
    background: var(--track);
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
    user-select: all;
  }
  .uuid-btn:hover, .hide-btn:hover { color: var(--text); border-color: var(--accent); }
  .hide-btn {
    font-weight: 400;
    font-size: 10px;
    color: var(--text-dim);
    margin-left: 4px;
    padding: 1px 6px;
    background: var(--track);
    border: 1px solid var(--border);
    border-radius: 4px;
    cursor: pointer;
  }
  .hidden-tray {
    border-top: 1px solid var(--border);
    padding: 10px 14px;
    background: var(--titlebar);
  }
  .hidden-tray-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 11px;
    color: var(--text-dim);
    letter-spacing: 0.03em;
    margin-bottom: 8px;
  }
  .hidden-tray-header button {
    background: none;
    border: 1px solid var(--border);
    color: var(--text-dim);
    border-radius: 6px;
    font-size: 11px;
    padding: 2px 8px;
    cursor: pointer;
  }
  .hidden-tray-header button:hover { color: var(--text); border-color: var(--accent); }
  .hidden-chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .hidden-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    color: var(--text-dim);
    background: var(--track);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 3px 4px 3px 10px;
  }
  .hidden-chip button {
    background: none;
    border: none;
    color: var(--text-dim);
    cursor: pointer;
    font-size: 13px;
    line-height: 1;
    padding: 2px 4px;
  }
  .hidden-chip button:hover { color: var(--text); }
  .hidden-chip .lamp {
    width: 8px;
    height: 8px;
    flex-shrink: 0;
  }
  .cwd { display: block; font-size: 11px; color: var(--text-dim); font-weight: 400; margin-top: 2px; }
  .bar-wrap { display: flex; align-items: center; gap: 8px; }
  .bar-track {
    flex: 1;
    height: 8px;
    background: var(--track);
    border-radius: 4px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 4px;
    transition: width 0.4s ease;
  }
  .pct { font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .status { display: flex; justify-content: flex-end; }
  .lamp {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: var(--green);
  }
  .lamp.working {
    background: var(--red-on);
    animation: blink 1s steps(1, end) infinite;
  }
  @keyframes blink {
    0%, 49% { background: var(--red-on); }
    50%, 100% { background: var(--red-off); }
  }
  .empty {
    padding: 24px 14px;
    text-align: center;
    color: var(--text-dim);
    font-size: 13px;
  }
  .kickoff-panel {
    width: 100%;
    max-width: 560px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 14px;
  }
  .kickoff-titlebar {
    background: var(--titlebar);
    padding: 10px 14px;
    font-weight: 600;
    font-size: 14px;
  }
  .kickoff-body {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
    gap: 12px;
  }
  .kickoff-info { font-size: 12px; color: var(--text-dim); line-height: 1.6; }
  .kickoff-info .status-success { color: var(--green); }
  .kickoff-info .status-error { color: var(--red-on); }
  .kickoff-info .status-skipped { color: var(--text-dim); }
  .switch {
    position: relative;
    display: inline-block;
    width: 46px;
    height: 26px;
    flex-shrink: 0;
  }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background: var(--track);
    border-radius: 26px;
    transition: background 0.2s ease;
  }
  .slider::before {
    content: "";
    position: absolute;
    height: 20px;
    width: 20px;
    left: 3px;
    top: 3px;
    background: var(--text-dim);
    border-radius: 50%;
    transition: transform 0.2s ease, background 0.2s ease;
  }
  .switch input:checked + .slider { background: var(--accent); }
  .switch input:checked + .slider::before { transform: translateX(20px); background: #fff; }
  .switch input:disabled + .slider { opacity: 0.5; cursor: not-allowed; }
</style>
</head>
<body>
  <div class="kickoff-panel">
    <div class="kickoff-titlebar">毎朝4時(JST) Hello送信</div>
    <div class="kickoff-body">
      <div class="kickoff-info" id="kickoff-info">読み込み中...</div>
      <label class="switch">
        <input type="checkbox" id="kickoff-toggle" disabled>
        <span class="slider"></span>
      </label>
    </div>
  </div>
  <div class="panel">
    <div class="titlebar"><span><span class="dot">&#9679;</span>Claude Code Monitor</span></div>
    <div class="header">
      <span>SESSION</span><span>CONTEXT USED</span><span style="text-align:right">STATUS</span>
    </div>
    <div id="rows"></div>
    <div id="hidden-tray" class="hidden-tray" hidden>
      <div class="hidden-tray-header">
        <span>非表示中 (<span id="hidden-count">0</span>)</span>
        <button id="show-hidden" type="button">すべて戻す</button>
      </div>
      <div id="hidden-chips" class="hidden-chips"></div>
    </div>
  </div>
<script>
function formatTokens(n) {
  if (n >= 1_000_000) return trimZero((n / 1_000_000).toFixed(1)) + 'm';
  if (n >= 1_000) return trimZero((n / 1_000).toFixed(1)) + 'k';
  return String(n);
}
function trimZero(s) {
  return s.endsWith('.0') ? s.slice(0, -2) : s;
}
const revealedUuids = new Set();
const rowsEl = document.getElementById('rows');
const HIDDEN_STORAGE_KEY = 'claudeMonitorHiddenSessions';

// 非表示にしたセッションIDはこの端末(ブラウザ)に保存し、次回起動時も維持する。
function loadHiddenSessions() {
  try {
    const raw = JSON.parse(localStorage.getItem(HIDDEN_STORAGE_KEY) || '[]');
    return new Set(Array.isArray(raw) ? raw : []);
  } catch (e) {
    return new Set();
  }
}
const hiddenSessions = loadHiddenSessions();
function saveHiddenSessions() {
  try { localStorage.setItem(HIDDEN_STORAGE_KEY, JSON.stringify([...hiddenSessions])); } catch (e) { /* ignore */ }
}
const hiddenTrayEl = document.getElementById('hidden-tray');
const hiddenCountEl = document.getElementById('hidden-count');
const hiddenChipsEl = document.getElementById('hidden-chips');
const showHiddenBtn = document.getElementById('show-hidden');

// 非表示にしたセッションは一覧から消すのではなく、パネル下部の「非表示中」
// トレイにまとめておき、個別またはまとめて元に戻せるようにする。
function renderHiddenTray(sessions) {
  const hidden = sessions.filter(s => hiddenSessions.has(s.session_id));
  if (hidden.length === 0) {
    hiddenTrayEl.hidden = true;
    return;
  }
  hiddenTrayEl.hidden = false;
  hiddenCountEl.textContent = hidden.length;
  hiddenChipsEl.innerHTML = hidden.map(s => `
    <span class="hidden-chip">${escapeHtml(s.name)}<div class="lamp ${s.working ? 'working' : ''}"></div><button type="button" class="unhide-btn" data-id="${escapeHtml(s.session_id)}" title="表示に戻す">&#10005;</button></span>
  `).join('');
}
showHiddenBtn.addEventListener('click', () => {
  hiddenSessions.clear();
  saveHiddenSessions();
  renderRows(lastLimit, lastSessions);
});
hiddenChipsEl.addEventListener('click', (e) => {
  const btn = e.target.closest('.unhide-btn');
  if (!btn) return;
  hiddenSessions.delete(btn.dataset.id);
  saveHiddenSessions();
  renderRows(lastLimit, lastSessions);
});

function renderRows(limit, sessions) {
  renderHiddenTray(sessions);
  if (sessions.length === 0) {
    rowsEl.innerHTML = '<div class="empty">アクティブなセッションはありません</div>';
    return;
  }
  const visible = sessions.filter(s => !hiddenSessions.has(s.session_id));
  if (visible.length === 0) {
    rowsEl.innerHTML = '<div class="empty">表示中のセッションはありません(下の「非表示中」欄から戻せます)</div>';
    return;
  }
  rowsEl.innerHTML = visible.map(s => {
    const id = escapeHtml(s.session_id);
    const uuidLabel = revealedUuids.has(s.session_id) ? id : 'UUID';
    return `
      <div class="row">
        <div class="name">${escapeHtml(s.name)}<button type="button" class="uuid-btn" data-id="${id}">${uuidLabel}</button><button type="button" class="hide-btn" data-id="${id}">非表示</button><span class="cwd">${escapeHtml(s.cwd)}</span></div>
        <div class="bar-wrap">
          <div class="bar-track"><div class="bar-fill" style="width:${s.context_pct}%"></div></div>
          <div class="pct">${Math.round(s.context_pct)}% (${formatTokens(s.context_tokens)}/${formatTokens(limit)})</div>
        </div>
        <div class="status"><div class="lamp ${s.working ? 'working' : ''}"></div></div>
      </div>
    `;
  }).join('');
}

let lastLimit = 0;
let lastSessions = [];

rowsEl.addEventListener('click', (e) => {
  const uuidBtn = e.target.closest('.uuid-btn');
  if (uuidBtn) {
    const id = uuidBtn.dataset.id;
    if (revealedUuids.has(id)) {
      revealedUuids.delete(id);
    } else {
      revealedUuids.add(id);
    }
    renderRows(lastLimit, lastSessions);
    return;
  }
  const hideBtn = e.target.closest('.hide-btn');
  if (hideBtn) {
    hiddenSessions.add(hideBtn.dataset.id);
    saveHiddenSessions();
    renderRows(lastLimit, lastSessions);
  }
});

async function refresh() {
  try {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    lastLimit = data.context_limit;
    lastSessions = data.sessions;
    renderRows(lastLimit, lastSessions);
  } catch (e) {
    console.error(e);
  }
}
function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

const STATUS_LABELS = { success: '成功', error: 'エラー', skipped: 'スキップ' };
const kickoffToggle = document.getElementById('kickoff-toggle');
const kickoffInfo = document.getElementById('kickoff-info');
let kickoffSyncing = false;

function formatLocal(iso) {
  if (!iso) return '(まだ実行なし)';
  return new Date(iso).toLocaleString('ja-JP');
}

function renderKickoff(state) {
  kickoffToggle.checked = !!state.enabled;
  kickoffToggle.disabled = false;
  const statusClass = state.last_status ? `status-${state.last_status}` : '';
  const statusLabel = state.last_status ? (STATUS_LABELS[state.last_status] || state.last_status) : '-';
  kickoffInfo.innerHTML = `
    状態: ${state.enabled ? '有効' : '無効'}<br>
    次回実行(予定): ${escapeHtml(state.next_run_local || '-')}<br>
    前回実行: ${formatLocal(state.last_run)} <span class="${statusClass}">${escapeHtml(statusLabel)}</span>
  `;
}

async function refreshKickoff() {
  if (kickoffSyncing) return;
  try {
    const res = await fetch('/api/kickoff');
    if (!res.ok) throw new Error('kickoff status unavailable');
    renderKickoff(await res.json());
  } catch (e) {
    kickoffInfo.textContent = '状態を取得できません(daily_kickoff.py 未検出)';
  }
}

kickoffToggle.addEventListener('change', async () => {
  kickoffSyncing = true;
  const enabled = kickoffToggle.checked;
  kickoffToggle.disabled = true;
  try {
    const res = await fetch('/api/kickoff/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (!res.ok) throw new Error('toggle failed');
    renderKickoff(await res.json());
  } catch (e) {
    kickoffToggle.checked = !enabled;
    kickoffToggle.disabled = false;
  } finally {
    kickoffSyncing = false;
  }
});

refresh();
refreshKickoff();
setInterval(refresh, __POLL_INTERVAL_MS__);
setInterval(refreshKickoff, __POLL_INTERVAL_MS__);
// タブ/ウィンドウが非表示の間はブラウザがsetIntervalを大幅に間引くため、
// 表示状態に戻ったタイミングで即座に再取得して古い表示のまま放置しない。
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { refresh(); refreshKickoff(); }
});
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
</script>
</body>
</html>
""".replace("__POLL_INTERVAL_MS__", str(POLL_INTERVAL_MS))

MANIFEST_JSON = json.dumps({
    "name": "Claude Code Monitor",
    "short_name": "CC Monitor",
    "description": "稼働中のClaude Codeセッション一覧",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#171a26",
    "theme_color": "#171a26",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"}
    ],
}, ensure_ascii=False).encode("utf-8")

ICON_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#171a26"/>
<circle cx="32" cy="32" r="18" fill="none" stroke="#8b6cf6" stroke-width="6"/>
<circle cx="32" cy="32" r="7" fill="#ff4d4f"/>
</svg>"""

SW_JS = b"""// Claude Code Monitor: minimal service worker (installability only, no offline caching)
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""


def _next_run_local() -> str | None:
    """systemd タイマーの次回発火予定時刻を人間可読な文字列で返す(取得できなければ None)。"""
    try:
        result = subprocess.run(
            ["systemctl", "show", "claude-daily-kickoff.timer", "--property=NextElapseUSecRealtime"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = result.stdout.strip()
    if not line.startswith("NextElapseUSecRealtime="):
        return None
    value = line.split("=", 1)[1].strip()
    return value or None


def _kickoff_state_payload() -> dict:
    if daily_kickoff is None:
        return {"error": "daily_kickoff module not found"}
    state = daily_kickoff.load_state()
    state["next_run_local"] = _next_run_local()
    return state


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass  # アクセスログは静かに抑制

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/sessions":
            payload = {
                "context_limit": claude_monitor.CONTEXT_TOKEN_LIMIT,
                "sessions": [asdict(s) for s in get_sessions()],
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8", cors=True)
        elif self.path == "/api/kickoff":
            payload = _kickoff_state_payload()
            status = 503 if "error" in payload else 200
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8", cors=True)
        elif self.path == "/manifest.json":
            self._send(200, MANIFEST_JSON, "application/manifest+json; charset=utf-8")
        elif self.path == "/icon.svg":
            self._send(200, ICON_SVG, "image/svg+xml")
        elif self.path == "/sw.js":
            self._send(200, SW_JS, "application/javascript; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        if self.path == "/api/kickoff/toggle":
            if daily_kickoff is None:
                self._send(503, b'{"error":"daily_kickoff module not found"}',
                            "application/json; charset=utf-8", cors=True)
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {}
            enabled = bool(payload.get("enabled", True))
            daily_kickoff.set_enabled(enabled)
            body = json.dumps(_kickoff_state_payload(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8", cors=True)
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_OPTIONS(self) -> None:
        # ブラウザのCORSプリフライト(POST /api/kickoff/toggle 用)に応答する。
        if self.path.startswith("/api/"):
            self.send_response(204)
            self._write_cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _write_cors_headers(self) -> None:
        # PWAシェル(GitHub Pages等、別オリジン)からこのAPIを呼べるようにする。
        # 公開しているのはセッション名・作業ディレクトリ・コンテキスト使用率のみで、
        # 認証トークンや機密情報は扱わないため、オリジン制限はかけていない。
        self.send_header("Access-Control-Allow-Origin", "*")

    def _send(self, status: int, body: bytes, content_type: str, cors: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cors:
            self._write_cors_headers()
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code Monitor (Web版)")
    parser.add_argument("--host", default="127.0.0.1",
                         help="バインドアドレス(既定: 127.0.0.1 = ローカルのみ。SSH -L 経由で見る想定)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cert", default=None,
                         help="TLS証明書ファイル(.crt/.pem)。指定するとHTTPSで待受(例: tailscale certで発行したもの)")
    parser.add_argument("--key", default=None,
                         help="TLS秘密鍵ファイル(.key)。--certとセットで指定")
    parser.add_argument("--context-limit", type=int, default=None,
                         help="コンテキストウィンドウの上限トークン数(既定: 環境変数 "
                              "CLAUDE_CONTEXT_TOKEN_LIMIT、未設定なら1,000,000)。"
                              "/context の表示(例: '96.6k / 1m')の右側の数字に合わせる")
    args = parser.parse_args()

    if args.context_limit is not None:
        claude_monitor.CONTEXT_TOKEN_LIMIT = args.context_limit

    server = ThreadingHTTPServer((args.host, args.port), MonitorHandler)
    scheme = "http"
    if args.cert and args.key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"Claude Code Monitor (Web) を起動しました: {scheme}://{args.host}:{args.port}")
    print("終了する場合は Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
