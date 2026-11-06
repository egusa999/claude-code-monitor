#!/usr/bin/env python3
"""
Claude Code Monitor
====================
現在アクティブな Claude Code セッションを一覧表示するデスクトップウィジェット。

表示内容(1セッション=1行):
    [セッション名]   [コンテキスト使用量バー + %]   [状態ランプ]
        - 状態ランプ: 作業中(直近数秒以内にトランスクリプトが更新) => 赤点滅
                      待機中(それ以外)                             => 緑点灯

データソース:
    ~/.claude/sessions/<pid>.json         起動中セッションのメタ情報(pid, sessionId, cwd, name)
    ~/.claude/projects/*/<sessionId>.jsonl  各セッションの会話トランスクリプト(usage情報を含む)

依存: 標準ライブラリのみ(tkinter)。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from tkinter import messagebox
import urllib.error
import urllib.request
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

HOME = Path.home()
SESSIONS_DIR = HOME / ".claude" / "sessions"
PROJECTS_DIR = HOME / ".claude" / "projects"


# コンテキストウィンドウの上限(トークン)。契約/モデルによって 200,000 だったり
# 1,000,000(拡張コンテキスト)だったりするため、環境変数 CLAUDE_CONTEXT_TOKEN_LIMIT
# で上書きできるようにしている。/context コマンドの表示(例: "96.6k / 1m")の
# 右側の数字に合わせて設定すること。
CONTEXT_TOKEN_LIMIT = int(os.environ.get("CLAUDE_CONTEXT_TOKEN_LIMIT", "1000000"))
# モデルIDの接頭辞 → コンテキストウィンドウ(トークン)。トランスクリプトには上限値そのものが
# 記録されないため、各行の message.model から推定する。長い接頭辞を優先して照合する。
# 表に無いモデルは上記 CONTEXT_TOKEN_LIMIT(全体の既定値)にフォールバックする。
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "claude-fable-5-1": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
WORKING_THRESHOLD_SEC = 6.0         # この秒数以内にトランスクリプト更新があれば「作業中」
AWAITING_PROMPT_MAX_AGE_SEC = 180.0  # 新規プロンプト/ツール結果を「考え中」とみなす最大経過時間
DEFAULT_SSH_HOST = "root@claudecode"  # リモートモードで端末を開くときのSSH接続先
REMOTE_FETCH_TIMEOUT_SEC = 5          # サーバーAPIの応答待ち上限
TOKEN_ENV_VAR = "CLAUDE_MONITOR_TOKEN"
COLOR_ERROR = "#ff4d4d"
CONFIG_PATH = Path.home() / ".claude_monitor" / "config.json"  # リモートモードの接続設定(server/token/ssh_host)
# ssh先は先頭が英数字のみ許可(`-o...`のようなオプション注入を防ぐ)
SSH_HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")
LOG_PATH = Path.home() / ".claude_monitor" / "app.log"  # pyw起動だとコンソールが無いため、エラーはここに残す
# リモートで実行を許す接続コマンド。サーバー応答由来の文字列をsshに渡すため、形式を厳格に限定する。
ATTACH_CMD_PATTERN = re.compile(r"^(tmux attach -t =|claude attach )[A-Za-z0-9._-]+$")
# --- フォルダからのセッション新規作成(リモートモード) ---
# サーバー側ヘルパー(ClaudeSessionManagerと共用)。引数はJSONを標準入力で渡し、日本語パスをコマンドラインに載せない。
SESSION_CTL_COMMAND = "claude-session-ctl"
SESSION_CTL_TIMEOUT_SEC = 60          # 作成はclaude-startの完了待ちを含むため長めにとる
SESSION_CTL_START = "start"
SESSION_CTL_LISTDIR = "listdir"
# セッション名はフォルダ名そのもの。claude-session-ctlと同じ規則で、満たさないフォルダからは作れない
SESSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# 作成元フォルダの既定(claude-session-ctlのALLOWED_BASESと同じ)。⚙の「追加の作成元」はこの後ろに足される
DEFAULT_CREATE_BASES: tuple[str, ...] = ("/root/onedrive/ClaudeCode", "/root/onedrive/連絡用/RPA")
CREATE_BASES_SEP = ";"
SESSION_CTL_STOP = "stop"
SESSION_CTL_CAPTURE = "capture"
SESSION_CTL_SEND_TEXT = "send_text"
SESSION_CTL_SEND_KEY = "send_key"
# --- 「終了」ボタン: /session-end を送り、完了(待機中が続く)を確認してからセッションを削除する ---
SESSION_END_COMMAND = "/session-end"
TMUX_ATTACH_PATTERN = re.compile(r"^tmux attach -t =([A-Za-z0-9_-]{1,64})$")  # 終了はtmuxのセッションだけ対象
END_POLL_SEC = 1.0                    # 状態確認の間隔
END_ENTER_DELAY_SEC = 0.5             # 文字列送信からEnter送信までの間隔
END_START_TIMEOUT_SEC = 60.0          # この間に「作業中」にならなければ失敗(削除しない)
END_ENTER_RESEND_AFTER_SEC = 8.0      # 作業中にならず入力欄にコマンドが残っていればEnterを1回だけ再送
END_IDLE_STABLE_SEC = 10.0            # 「待機中」がこの秒数続いたら完了とみなす
END_TOTAL_TIMEOUT_SEC = 30 * 60.0     # /session-end 全体の上限(超えたら削除しない)
PANE_PROMPT_CHAR = "❯"
PANE_BORDER_CHAR = "─"
ANSI_SGR_PATTERN = re.compile(r"\x1b\[([0-9;:]*)m")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b(?:\[[0-9;:?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
SGR_DIM_ON, SGR_DIM_OFF, SGR_RESET_CODES = "2", "22", frozenset({"", "0"})
BACKGROUND_POLL_MS = 100              # 別スレッドの処理結果をメインスレッドで確認する間隔
CREATE_LIST_HEIGHT = 14               # 作成ダイアログのフォルダ一覧の行数
# --- 画面レイアウト(PWAの見た目に合わせた寸法) ---
WINDOW_WIDTH = 560
ROW_PADDING_X = 14
ROW_PADDING_Y = 8
ROW_NAME_Y = 8
ROW_HEIGHT_COMPACT = 52
NAME_LINE_HEIGHT = 22
CHIP_LINE_HEIGHT = 22
CHIP_GAP = 4
CHIP_FONT = ("Segoe UI", 8)
HEADER_HEIGHT = 28
# コンテキスト列はステータス(ランプ)の直前に置く(ステータス列を詰めた分をセッション名の列に回す)
BAR_COL_WIDTH = 190
STATUS_COL_WIDTH = 32
BAR_TRACK_WIDTH = 70
MODEL_FONT_SIZE = 9       # コンテキスト列のモデル名
CONTEXT_FONT_SIZE = 10    # コンテキスト列の「% (使用/上限)」
BAR_CANVAS_HEIGHT = 46    # 上記2行が収まる高さ
BAR_TEXT_OFFSET = 11      # 中央からモデル名・数値の行までの距離
MIN_WINDOW_WIDTH = 420
MIN_WINDOW_HEIGHT = 200
SCROLLBAR_WIDTH_PX = 8
SCROLLBAR_STYLE = "Monitor.Vertical.TScrollbar"
# プラン使用量パネル(Claude Usageアプリと同じ表。スクロール領域の外に固定)
USAGE_PANEL_HEIGHT = 76
USAGE_HEADER_Y = 10
USAGE_FIRST_ROW_Y = 32
USAGE_ROW_HEIGHT = 26
USAGE_PCT_WIDTH = 44          # バー右の "100%" の幅
USAGE_RING_COL_WIDTH = 56
USAGE_IN_COL_WIDTH = 64
USAGE_AT_COL_WIDTH = 76
USAGE_AT_SAMPLE = "12/31 23:59"  # RESETS AT列の最長表記(列幅の実測用)
USAGE_COL_GAP = 8
USAGE_MIN_BAR_WIDTH = 30
USAGE_BAR_HALF = 3            # バーの太さの半分
USAGE_RING_RADIUS = 8
USAGE_RING_WIDTH = 3
USAGE_SESSION_COLOR = "#8b6cf6"  # Claude Usageアプリ同様、セッション=紫・週=青
USAGE_WEEKLY_COLOR = "#4f8ff7"
TITLE_SWITCH_SIZE = (30, 16, 12)  # タイトルバーのHelloスイッチ(幅, 高さ, つまみ)
RESIZE_EDGE_PX = 5      # 右端・下端のドラッグ可能な帯の太さ
SCREEN_MARGIN_PX = 80   # 自動で高さを合わせるとき、画面の高さから残す余白


def name_col_width(window_width: int) -> int:
    """名前列の幅(残りをバー列とステータス列が占める)。"""
    return window_width - ROW_PADDING_X * 2 - BAR_COL_WIDTH - STATUS_COL_WIDTH
UI_PREFS_PATH = Path.home() / ".claude_monitor" / "ui.json"  # 画面設定(最前面ピン)
POLL_INTERVAL_MS = 1500             # セッション一覧・使用量の再取得間隔
AGENT_VISIBLE_SEC = 120.0           # サブエージェントのトランスクリプトが、この秒数以内に更新されていれば一覧に出す
SUBAGENTS_DIRNAME = "subagents"     # ~/.claude/projects/*/<sessionId>/subagents/agent-<id>.jsonl
AGENT_FILE_GLOB = "agent-*.jsonl"
RELAYOUT_DELAY_MS = 80              # リサイズ中の再配置の間引き間隔
CORNER_CURSOR = "size_nw_se" if os.name == "nt" else "bottom_right_corner"  # Tkのカーソル名はOSで異なる
RESIZE_GRIP_PX = 14                 # 右下のリサイズ角の大きさ
BLINK_INTERVAL_MS = 500             # 赤ランプの点滅間隔
TRANSCRIPT_TAIL_BYTES = 65536        # トランスクリプト末尾読み取りの初期サイズ
TRANSCRIPT_MAX_SCAN_BYTES = 8 * 1024 * 1024  # 見つからない場合に拡大する上限(8MB)

# 配色(添付スクリーンショットの "Claude Usage" ウィンドウに寄せたダークテーマ)
COLOR_BG = "#171a26"
COLOR_TITLEBAR = "#12141d"
COLOR_ROW_BG = "#1c2032"
COLOR_ROW_ALT_BG = "#20243a"
COLOR_BORDER = "#2a2f45"
COLOR_TEXT = "#e7e9f5"
COLOR_TEXT_DIM = "#8a8fa8"
COLOR_ACCENT = "#8b6cf6"
COLOR_TRACK = "#2a2f45"
COLOR_RED_ON = "#ff4d4f"
COLOR_RED_OFF = "#5a2a2c"
COLOR_GREEN = "#3ddc84"


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------

@dataclass
class AgentInfo:
    """親セッションの中で動くサブエージェント(fork・Explore・general-purpose等)。
    別プロセスではないため sessions/*.json は無く、親セッション配下の subagents/ に記録される。"""
    agent_id: str
    agent_type: str = ""
    description: str = ""
    is_fork: bool = False
    model: str = ""
    context_tokens: int = 0
    context_limit: int = 0
    context_pct: float = 0.0
    working: bool = False


@dataclass
class SessionInfo:
    pid: int
    session_id: str
    name: str
    cwd: str
    started_at: float
    context_pct: float = 0.0
    context_tokens: int = 0
    context_limit: int = 0
    model: str = ""
    working: bool = False
    last_active: float = 0.0
    agents: list[AgentInfo] = field(default_factory=list)
    attach_cmd: str = ""  # 端末で実行する接続コマンド(コピー用)。接続手段が無ければ空


# セッションごとに直近判明したコンテキストトークン数を保持するキャッシュ。
# SessionInfoは毎ポーリングで作り直されるため、この値はモジュールレベルで持つ。
_last_known_tokens: dict[str, int] = {}
_last_known_limit: dict[str, int] = {}
_last_known_model: dict[str, str] = {}


def _is_pid_alive(pid: int) -> bool:
    if os.name == "posix":
        status_path = Path(f"/proc/{pid}/status")
        try:
            for line in status_path.read_text().splitlines():
                if line.startswith("Tgid:"):
                    return int(line.split()[1]) == pid
        except (FileNotFoundError, ProcessLookupError, ValueError):
            return False
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# /proc/<pid>/stat のフィールド番号(man proc(5)参照)。フィールド2の comm
# (プロセス名)は空白や閉じ括弧を含みうるため、最後の ")" より後ろをフィールド3
# (state)起点として数える必要がある。
_STAT_STARTTIME_FIELD_NUMBER = 22
_STAT_FIELD_NUMBER_AFTER_COMM_START = 3


def _process_start_ticks(pid: int) -> int | None:
    """/proc/<pid>/stat の starttime(システム起動からのクロックtick数)を返す。
    取得できなければNone。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    parts = raw.rsplit(")", 1)
    if len(parts) != 2:
        return None
    fields_after_comm = parts[1].split()
    index = _STAT_STARTTIME_FIELD_NUMBER - _STAT_FIELD_NUMBER_AFTER_COMM_START
    try:
        return int(fields_after_comm[index])
    except (IndexError, ValueError):
        return None


def _is_same_process_instance(pid: int, recorded_start_ticks: int | None) -> bool:
    """PIDが生存しているだけでなく、~/.claude/sessions/<pid>.jsonに記録された
    起動時刻(procStart)と、現在そのPIDで動いているプロセスの実際の起動時刻が
    一致する、同一プロセスインスタンスかどうかを確認する。

    理由: プロセスが終了した後、OSが同じPID番号を全く無関係な別プロセスへ
    再利用することがある。生死判定(_is_pid_alive)だけでは、既に終了した
    セッションの残骸ファイルを、たまたまPID番号を引き継いだ無関係な新しい
    プロセスのせいで「生存中」と誤判定してしまう不具合が実際に起きた
    (2026-09-28、実データで再現確認)。recorded_start_ticksが取得できない
    (procStartフィールドを持たない古い形式のセッションファイル等)場合は、
    この照合をスキップし_is_pid_aliveの結果のみで判定する(後方互換)。
    """
    if not _is_pid_alive(pid):
        return False
    if recorded_start_ticks is None:
        return True
    actual_start_ticks = _process_start_ticks(pid)
    if actual_start_ticks is None:
        return True
    return actual_start_ticks == recorded_start_ticks


def _build_attach_cmd(data: dict) -> str:
    """セッションに端末から接続するコマンドを作る。bgは`claude attach <jobId>`
    (`claude agents`のセッションマネージャーと同じ手段)、tmux上の対話セッションは
    `tmux attach -t <セッション名>`。値はJSON由来なのでshlex.quoteで必ず無害化する。"""
    if data.get("kind") == "bg" and data.get("jobId"):
        return f"claude attach {shlex.quote(str(data['jobId']))}"
    tmux_target = str(data.get("tmux") or "")
    tmux_session = tmux_target.split(":", 1)[0]
    if tmux_session:
        # `=`付きで完全一致指定にする(無いと前方一致で別セッションに入る恐れがある)
        return f"tmux attach -t {shlex.quote('=' + tmux_session)}"
    return ""


def _load_active_sessions() -> list[SessionInfo]:
    """~/.claude/sessions/*.json から、プロセスが生存中のセッションのみ返す。"""
    sessions: dict[str, SessionInfo] = {}
    parked_job_ids: dict[str, str] = {}  # session_id -> parkedJobId(退避中の親)
    live_bg_job_ids: set[str] = set()    # 生存中のbgセッションのjobId
    if not SESSIONS_DIR.exists():
        return []

    for meta_file in SESSIONS_DIR.glob("*.json"):
        try:
            pid = int(meta_file.stem)
        except ValueError:
            continue
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        recorded_start_ticks: int | None = None
        raw_proc_start = data.get("procStart")
        if raw_proc_start is not None:
            try:
                recorded_start_ticks = int(raw_proc_start)
            except (TypeError, ValueError):
                recorded_start_ticks = None

        if not _is_same_process_instance(pid, recorded_start_ticks):
            continue

        session_id = data.get("sessionId")
        if not session_id:
            continue

        cwd = data.get("cwd", "")
        name = data.get("name") or Path(cwd).name or session_id[:8]
        started_at = (data.get("startedAt") or 0) / 1000.0

        info = SessionInfo(
            pid=pid,
            session_id=session_id,
            name=name,
            cwd=cwd,
            started_at=started_at,
            attach_cmd=_build_attach_cmd(data),
        )
        # 同一 sessionId の重複(古い meta ファイル残存等)は新しい方を優先
        existing = sessions.get(session_id)
        if existing is None or started_at >= existing.started_at:
            sessions[session_id] = info
            if data.get("parkedJobId"):
                parked_job_ids[session_id] = str(data["parkedJobId"])
            if data.get("kind") == "bg" and data.get("jobId"):
                live_bg_job_ids.add(str(data["jobId"]))

    # 親セッションがbgジョブへ退避(park)され、そのbgが生存中なら、親は隠してbg側の
    # 1行だけ残す(Claude本体のセッションマネージャーと同じ見え方)。bgが終了すれば
    # live_bg_job_idsに入らないので、親は通常どおり再表示される。
    for sid, job_id in parked_job_ids.items():
        if job_id in live_bg_job_ids:
            sessions.pop(sid, None)

    return sorted(sessions.values(), key=lambda s: s.started_at)


def _find_transcript(session_id: str) -> Path | None:
    matches = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def _iter_recent_entries(transcript: Path):
    """トランスクリプト末尾から新しい順にJSONオブジェクトを返すジェネレータ。

    直近の行がコマンド出力・画像等で非常に大きいと、その1行だけで初期の読み取り
    範囲(TRANSCRIPT_TAIL_BYTES)を超えてしまい、それより前の行が範囲外になって
    見つからないことがある(セッションが活発なほど起きやすい)。呼び出し側が
    欲しい行を見つけられずジェネレータを最後まで消費した場合は、読み取り範囲を
    段階的に広げて(最大 TRANSCRIPT_MAX_SCAN_BYTES まで)再試行する。
    """
    try:
        size = transcript.stat().st_size
    except OSError:
        return

    tail_size = min(TRANSCRIPT_TAIL_BYTES, size)
    while True:
        try:
            with transcript.open("rb") as fh:
                if tail_size < size:
                    fh.seek(-tail_size, os.SEEK_END)
                data = fh.read()
        except OSError:
            return

        for line in reversed(data.split(b"\n")):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

        if tail_size >= size or tail_size >= TRANSCRIPT_MAX_SCAN_BYTES:
            return
        tail_size = min(tail_size * 4, TRANSCRIPT_MAX_SCAN_BYTES, size)


def _context_limit_for_model(model: str | None) -> int:
    if model:
        for prefix in sorted(MODEL_CONTEXT_LIMITS, key=len, reverse=True):
            if model.startswith(prefix):
                return MODEL_CONTEXT_LIMITS[prefix]
    return CONTEXT_TOKEN_LIMIT


def _read_last_usage(transcript: Path, include_sidechain: bool = False) -> tuple[dict, str | None] | None:
    """トランスクリプト末尾から、メインチェーンの最後の (usage, model) を取得する。

    Task(サブエージェント)呼び出しは isSidechain: true として同じファイルに
    記録され、メインの会話とは無関係な(別のコンテキストウィンドウの)トークン数
    を持つため、必ず除外する。これを除外しないと /context の表示と一致しなくなる。
    サブエージェント自身のトランスクリプト(全行が isSidechain: true)を読むときだけ
    include_sidechain=True にする。
    """
    for obj in _iter_recent_entries(transcript):
        if obj.get("isSidechain") and not include_sidechain:
            continue
        message = obj.get("message") or {}
        if message.get("model") == "<synthetic>":
            # 実際のAPI呼び出しを伴わない合成メッセージ(中断時の後処理等)で、
            # usageが全項目0のダミー値になっている。実際のコンテキスト量とは
            # 無関係なので無視し、その前の本物のassistantメッセージを見る。
            continue
        usage = message.get("usage")
        if usage:
            return usage, message.get("model")
    return None


def _entry_age_sec(obj: dict) -> float | None:
    """エントリの`timestamp`(ISO8601)から、現在までの経過秒数を返す。
    タイムスタンプが無い/壊れている場合はNoneを返す。
    """
    ts = obj.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _is_awaiting_response(transcript: Path) -> bool:
    """トランスクリプトの最後の『メッセージ』が、まだアシスタントの応答の
    書き込みが完了していない状態(=作業中とみなすべき状態)かどうか。

    Claude Code Desktop連携時は、実際の会話(user/assistant)以外にも
    `bridge-session`/`last-prompt`/`atis-latch`等のブックキーピング専用の
    エントリが同じファイルに書き込まれる。これらにはtimestampが無いため
    読み飛ばし、直近の実際のuser/assistantメッセージを見る。

    - 直近の実メッセージがuser側(人間の新規プロンプト、またはツール実行結果)
      なら、アシスタントがまだ考え始めていない・思考中の可能性がある。最初の
      thinkingブロックが完成するまではファイルへの新規書き込みが一切発生
      しないため、mtimeの新しさだけでは「プロンプト送信直後の考え中」を
      作業中と判定できない(実際にこれが原因で、送信直後にランプが赤に
      ならない不具合が起きた)。ただし、そのuser発言自体が
      `AWAITING_PROMPT_MAX_AGE_SEC`より古い場合は「考え中」とはみなさない。
      理由: 会話が実質終了して久しいセッションでも、上記ブックキーピング
      エントリだけが書き込まれ続けることで、読み飛ばした先にある古いuser
      発言を「今まさに考え中」と誤判定し、ランプが永久に赤点滅し続ける
      不具合が実際に起きた。
    - 直近の実メッセージがassistant側でstop_reasonが"tool_use"の場合は、
      ツール実行結果待ち(コマンド実行・サブエージェント呼び出し等、結果が
      返るまで新規書き込みが止まる)。こちらは実行時間の上限がないため
      年齢での打ち切りはしない。いずれもサブエージェント呼び出し中を
      「本体は作業中」の意味で含めたいため、isSidechainでは絞り込まない。
    """
    for obj in _iter_recent_entries(transcript):
        entry_type = obj.get("type")
        if entry_type not in ("user", "assistant"):
            continue
        if entry_type == "user":
            age = _entry_age_sec(obj)
            return age is not None and age < AWAITING_PROMPT_MAX_AGE_SEC
        message = obj.get("message") or {}
        return message.get("stop_reason") == "tool_use"
    return False


def _update_usage_and_activity(info: SessionInfo) -> None:
    transcript = _find_transcript(info.session_id)
    if transcript is None:
        info.context_pct = 0.0
        info.context_tokens = 0
        info.context_limit = CONTEXT_TOKEN_LIMIT
        info.working = False
        info.last_active = info.started_at
        info.agents = []
        return

    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        mtime = 0.0
    mtime_fresh = (time.time() - mtime) < WORKING_THRESHOLD_SEC
    info.working = mtime_fresh or _is_awaiting_response(transcript)
    # 一覧の並び替えに使う「最終アクティブ時刻」。稼働中は、ツール実行に
    # 時間がかかってmtimeの更新が一時止まっていても「今まさにアクティブ」
    # として常に最上位に来てほしいため現在時刻を使い、待機中は実際の
    # トランスクリプト最終更新時刻(mtime)をそのまま使う。
    info.last_active = time.time() if info.working else mtime

    result = _read_last_usage(transcript)
    if result:
        usage, model = result
        _last_known_limit[info.session_id] = _context_limit_for_model(model)
        if model:
            _last_known_model[info.session_id] = model
        tokens = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        _last_known_tokens[info.session_id] = tokens
    else:
        # 拡大読み取りでも見つからなかった場合(書き込み中の行が一時的に不完全、等)。
        # 0に落とすとアクティブ中に瞬間的な0表示が起きるため、直近の既知値を保持する。
        tokens = _last_known_tokens.get(info.session_id, 0)

    limit = _last_known_limit.get(info.session_id, CONTEXT_TOKEN_LIMIT)
    info.context_tokens = tokens
    info.context_limit = limit
    info.model = _last_known_model.get(info.session_id, "")
    info.context_pct = min(100.0, tokens / limit * 100.0)
    info.agents = _load_agents(transcript)


def _read_agent_meta(agent_file: Path) -> dict:
    try:
        return json.loads(agent_file.with_suffix(".meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _load_agents(parent_transcript: Path) -> list[AgentInfo]:
    """親セッション配下のサブエージェントのうち、作業中または直近に更新があったものを返す。

    種類(fork / Explore / general-purpose等)を問わず全て対象。完了後もファイルは残り続けるため、
    AGENT_VISIBLE_SEC を過ぎたものは出さない。コンテキスト量は各エージェント自身のトランスクリプトの
    最後のusageから求める(親とは別のコンテキストウィンドウ。forkは親の会話を引き継いで始まる)。
    """
    agents_dir = parent_transcript.parent / parent_transcript.stem / SUBAGENTS_DIRNAME
    agents: list[AgentInfo] = []
    now = time.time()
    for agent_file in agents_dir.glob(AGENT_FILE_GLOB):
        try:
            age = now - agent_file.stat().st_mtime
        except OSError:
            continue
        working = age < WORKING_THRESHOLD_SEC or (age < AGENT_VISIBLE_SEC and _is_awaiting_response(agent_file))
        if not working and age >= AGENT_VISIBLE_SEC:
            continue
        meta = _read_agent_meta(agent_file)
        agent_id = agent_file.stem.removeprefix("agent-")
        result = _read_last_usage(agent_file, include_sidechain=True)
        if result:
            usage, model = result
            tokens = (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                      + usage.get("cache_read_input_tokens", 0))
            _last_known_tokens[agent_id] = tokens
            _last_known_limit[agent_id] = _context_limit_for_model(model)
            if model:
                _last_known_model[agent_id] = model
        tokens = _last_known_tokens.get(agent_id, 0)
        limit = _last_known_limit.get(agent_id, CONTEXT_TOKEN_LIMIT)
        agents.append(AgentInfo(
            agent_id=agent_id,
            agent_type=meta.get("agentType", ""),
            description=meta.get("description", ""),
            is_fork=bool(meta.get("isFork")),
            model=_last_known_model.get(agent_id, ""),
            context_tokens=tokens,
            context_limit=limit,
            context_pct=min(100.0, tokens / limit * 100.0),
            working=working,
        ))
    return sorted(agents, key=lambda a: (not a.working, a.agent_id))


def format_tokens(n: int) -> str:
    """/context コマンドと同じ表記(例: 96.6k, 1m)に揃えるための整形。"""
    if n >= 1_000_000:
        value, suffix = n / 1_000_000, "m"
    elif n >= 1_000:
        value, suffix = n / 1_000, "k"
    else:
        return str(n)
    text = f"{value:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text + suffix


def get_sessions() -> list[SessionInfo]:
    sessions = _load_active_sessions()
    for s in sessions:
        _update_usage_and_activity(s)
    # 最近アクティブな順(直近に更新があったセッションが常に上)に並べ替える。
    return sorted(sessions, key=lambda s: s.last_active, reverse=True)


# --- プラン使用量(5時間セッション枠・週の上限)。Claude Usageアプリと同じ値 ---------------
# Claude Codeがログインに使うOAuthトークン(~/.claude/.credentials.json)で取得する。
# トークンの更新(refresh)はClaude Code本体に任せ、ここでは読むだけにする
# (こちらでrefreshするとrefreshTokenが回転し、Claude Code側のログインを壊しうるため)。
CREDENTIALS_PATH = HOME / ".claude" / ".credentials.json"
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_API_BETA = "oauth-2025-04-20"
USAGE_CACHE_SEC = 60.0          # API呼び出しの最短間隔(レート制限への配慮。表示の残り時間は手元で毎秒計算する)
USAGE_FETCH_TIMEOUT_SEC = 10
# (APIのキー, 表示用キー, 枠の長さ[秒]) 経過率(ELAPSED)の計算に使う
USAGE_WINDOWS: tuple[tuple[str, str, int], ...] = (
    ("five_hour", "session", 5 * 3600),
    ("seven_day", "weekly", 7 * 24 * 3600),
)
_usage_lock = threading.Lock()
_usage_cache: dict = {}
_usage_cache_at = 0.0


def _fetch_plan_usage_uncached() -> dict:
    try:
        oauth = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))["claudeAiOauth"]
        token = oauth["accessToken"]
        expires_ms = float(oauth.get("expiresAt") or 0)
    except (OSError, ValueError, KeyError, TypeError):
        return {"error": "Claude Codeのログイン情報がありません"}
    if expires_ms and expires_ms / 1000.0 < time.time():
        return {"error": "ログイン情報の期限切れ(Claude Code起動で更新)"}
    req = urllib.request.Request(USAGE_API_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": USAGE_API_BETA,
        "User-Agent": "claude-code-monitor"})
    try:
        with urllib.request.urlopen(req, timeout=USAGE_FETCH_TIMEOUT_SEC) as res:
            raw = json.load(res)
    except urllib.error.HTTPError as e:
        return {"error": f"使用量API HTTP {e.code}"}
    except (OSError, ValueError):
        return {"error": "使用量APIに接続できません"}
    result: dict = {}
    for api_key, key, window_sec in USAGE_WINDOWS:
        item = raw.get(api_key) if isinstance(raw, dict) else None
        if not isinstance(item, dict) or item.get("utilization") is None:
            continue
        result[key] = {"pct": float(item["utilization"]), "resets_at": item.get("resets_at"),
                       "window_sec": window_sec}
    if not result:
        return {"error": "使用量データがありません"}
    return result


def fetch_plan_usage() -> dict:
    """{"session"|"weekly": {"pct", "resets_at"(ISO8601), "window_sec"}} を返す。
    失敗時は"error"を付け、前回取得できた値があればそれも残す(表示を消さない)。"""
    global _usage_cache, _usage_cache_at
    with _usage_lock:
        if _usage_cache and time.time() - _usage_cache_at < USAGE_CACHE_SEC:
            return dict(_usage_cache)
        fresh = _fetch_plan_usage_uncached()
        if "error" in fresh:
            previous = {k: v for k, v in _usage_cache.items() if k != "error"}
            fresh = {**previous, **fresh}
        _usage_cache, _usage_cache_at = fresh, time.time()
        return dict(fresh)


def parse_iso_utc(value) -> datetime | None:
    """ISO8601文字列をaware datetimeにする(不正ならNone)。"""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def format_remaining(seconds: float) -> str:
    """残り時間をClaude Usageアプリと同じ粒度で表す(例: 4h 19m / 5d 2h / 12m)。"""
    minutes = max(int(seconds // 60), 0)
    days, rem = divmod(minutes, 24 * 60)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


# 接続コマンドを新しい端末ウィンドウで実行する。上から順に、最初に見つかったものを使う。
# (端末エミュレータ名, コマンドの前に付ける引数)
_POSIX_TERMINALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("x-terminal-emulator", ("-e",)),
    ("gnome-terminal", ("--",)),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-x",)),
    ("xterm", ("-e",)),
)


def open_terminal(attach_cmd: str, ssh_host: str | None = None) -> bool:
    """attach_cmd(`tmux attach -t =名` / `claude attach <id>`)を新しい端末で実行する。
    ssh_host指定時(リモートモード)は`ssh -t <host> <attach_cmd>`として手元の端末で開く。
    起動できる端末が無い・コマンド形式が不正・起動に失敗したときは False を返す。"""
    if not attach_cmd:
        return False
    if ssh_host:
        if not ATTACH_CMD_PATTERN.match(attach_cmd) or not SSH_HOST_PATTERN.match(ssh_host):
            return False
        argv = ["ssh", "-t", ssh_host, attach_cmd]
    else:
        argv = shlex.split(attach_cmd)
    try:
        if os.name == "nt":
            terminal = ["wt.exe"] if shutil.which("wt.exe") else ["cmd.exe", "/c", "start", ""]
            subprocess.Popen([*terminal, *argv])
            return True
        for exe, prefix in _POSIX_TERMINALS:
            if shutil.which(exe):
                subprocess.Popen([exe, *prefix, *argv])
                return True
    except OSError:
        return False
    return False


class SessionCtlError(Exception):
    """claude-session-ctl呼び出しの失敗。メッセージはそのままユーザーに見せる。"""


def parse_create_bases(raw: str) -> list[str]:
    """「;」区切りの作成元フォルダを、絶対パスのみ・重複なしのリストにする。"""
    bases: list[str] = []
    for part in (raw or "").split(CREATE_BASES_SEP):
        path = part.strip().rstrip("/") or part.strip()
        if path.startswith("/") and path not in bases:
            bases.append(path)
    return bases


def effective_create_bases(extra_raw: str) -> list[str]:
    """既定の作成元 + ⚙で追加した作成元(重複なし)。"""
    return parse_create_bases(CREATE_BASES_SEP.join((*DEFAULT_CREATE_BASES, extra_raw or "")))


def base_labels(bases: list[str]) -> list[str]:
    """作成元ボタンの表示名。末尾のフォルダ名(ClaudeCode / RPA)、重なるときはフルパス。"""
    short = [posixpath.basename(b) or b for b in bases]
    return [s if short.count(s) == 1 else b for s, b in zip(short, bases)]

def call_session_ctl(ssh_host: str, cmd: str, runner=subprocess.run, **params) -> object:
    """`ssh <host> claude-session-ctl` にJSONリクエストを渡し、resultを返す。失敗はSessionCtlError。"""
    if not SSH_HOST_PATTERN.match(ssh_host or ""):
        raise SessionCtlError("SSH接続先が不正です(⚙で設定してください)")
    payload = json.dumps({"cmd": cmd, **params}, ensure_ascii=True)
    # pyw起動でsshのコンソール窓を出さない
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        proc = runner(["ssh", "-o", "BatchMode=yes", ssh_host, SESSION_CTL_COMMAND],
                      input=payload, capture_output=True, text=True, encoding="utf-8",
                      errors="replace", timeout=SESSION_CTL_TIMEOUT_SEC, **extra)
    except subprocess.TimeoutExpired as e:
        raise SessionCtlError(
            f"サーバーが{SESSION_CTL_TIMEOUT_SEC}秒以内に応答しませんでした。Tailscaleの再認証が必要な場合は、"
            f"端末で一度 ssh {ssh_host} を実行してください") from e
    except OSError as e:
        raise SessionCtlError(f"sshを起動できません: {e}") from e
    try:
        response = json.loads(proc.stdout)
    except ValueError:
        detail = (proc.stderr or "").strip() or f"終了コード {proc.returncode}"
        raise SessionCtlError(f"サーバーの操作に失敗しました: {detail}") from None
    if not isinstance(response, dict):
        raise SessionCtlError("サーバーの応答形式が不正です")
    if not response.get("ok"):
        raise SessionCtlError(str(response.get("error") or "不明なエラー"))
    return response.get("result")


def run_in_background(widget: tk.Misc, work, on_done) -> None:
    """work()を別スレッドで実行し、完了後にメインスレッドで on_done(result, error) を呼ぶ。
    tkのウィジェットはメインスレッド以外から触らないため、スレッドは結果を置くだけにする。"""
    box: dict = {}

    def target() -> None:
        try:
            box["result"] = work()
        except SessionCtlError as e:
            box["error"] = str(e)
        except Exception as e:  # 想定外もログに残してダイアログで知らせる
            logging.exception("background task failed")
            box["error"] = f"{type(e).__name__}: {e}"
        box["done"] = True

    def poll() -> None:
        try:
            if not box.get("done"):
                widget.after(BACKGROUND_POLL_MS, poll)
                return
            on_done(box.get("result"), box.get("error"))
        except tk.TclError:
            pass  # 完了前にダイアログが閉じられた

    threading.Thread(target=target, daemon=True).start()
    widget.after(BACKGROUND_POLL_MS, poll)


def _non_dim_text(raw: str) -> str:
    """capture-pane -e の1行から、薄字(SGR 2)区間とエスケープを除いた表示文字列を返す。
    入力欄の薄字はプレースホルダ・提案文で、実際の入力ではないため。"""
    out: list[str] = []
    dim, pos = False, 0
    for m in ANSI_SGR_PATTERN.finditer(raw):
        if not dim:
            out.append(ANSI_ESCAPE_PATTERN.sub("", raw[pos:m.start()]))
        for code in m.group(1).replace(":", ";").split(";"):
            if code in SGR_RESET_CODES or code == SGR_DIM_OFF:
                dim = False
            elif code == SGR_DIM_ON:
                dim = True
        pos = m.end()
    if not dim:
        out.append(ANSI_ESCAPE_PATTERN.sub("", raw[pos:]))
    return "".join(out)


def pane_input_text(pane: str) -> str | None:
    """Claude Codeの入力欄(罫線の直下の「❯ ...」行)に入っている文字列。入力欄が見つからなければNone。"""
    lines = [ln for ln in pane.splitlines() if ANSI_ESCAPE_PATTERN.sub("", ln).strip()]
    for i in range(len(lines) - 1, 0, -1):
        plain = ANSI_ESCAPE_PATTERN.sub("", lines[i]).strip()
        above = ANSI_ESCAPE_PATTERN.sub("", lines[i - 1]).strip()
        if plain.startswith(PANE_PROMPT_CHAR) and above.startswith(PANE_BORDER_CHAR):
            text = _non_dim_text(lines[i]).strip()
            return text[len(PANE_PROMPT_CHAR):].strip() if text.startswith(PANE_PROMPT_CHAR) else ""
    return None


def end_session_workflow(name: str, session_id: str, ctl, get_session,
                         sleep=time.sleep, clock=time.monotonic) -> None:
    """tmuxセッション name へ /session-end を送り、完了してからセッションを削除する(claude-stop。会話履歴は残る)。
    完了は、このモニターの「作業中」判定(トランスクリプト基準)で、作業中を一度見た後に待機中が
    END_IDLE_STABLE_SEC 続いたことで判断する。途中で失敗・タイムアウトしたら削除はしない。
    ctl(cmd, **params) はclaude-session-ctl呼び出し、get_session(session_id) は最新のSessionInfo(無ければNone)。"""
    info = get_session(session_id)
    if info is None:
        raise SessionCtlError("セッションが一覧に見つかりません")
    if info.working:
        raise SessionCtlError("作業中のため送信できません。待機中(緑)になってから押してください")
    current = pane_input_text(str(ctl(SESSION_CTL_CAPTURE, name=name)))
    if current is None:
        raise SessionCtlError("Claudeの入力欄が見つかりません(確認画面などが出ていないか端末で確認してください)")
    if current:
        raise SessionCtlError(f"入力欄に未送信の文字があります: {current!r}")
    ctl(SESSION_CTL_SEND_TEXT, name=name, text=SESSION_END_COMMAND)
    sleep(END_ENTER_DELAY_SEC)
    ctl(SESSION_CTL_SEND_KEY, name=name, key="Enter")

    started = clock()
    resent = False
    while True:  # 作業中になるのを待つ(=コマンドが受け付けられた)
        info = get_session(session_id)
        if info is None:
            raise SessionCtlError("セッションが途中で終了しました")
        if info.working:
            break
        elapsed = clock() - started
        if elapsed >= END_START_TIMEOUT_SEC:
            raise SessionCtlError(f"{SESSION_END_COMMAND} が始まりませんでした(セッションは削除していません)")
        if not resent and elapsed >= END_ENTER_RESEND_AFTER_SEC:
            resent = True
            if pane_input_text(str(ctl(SESSION_CTL_CAPTURE, name=name))) == SESSION_END_COMMAND:
                logging.warning("'%s': Enter was dropped; resending", name)
                ctl(SESSION_CTL_SEND_KEY, name=name, key="Enter")
        sleep(END_POLL_SEC)

    idle_since: float | None = None
    while True:  # 待機中が続くのを待つ(質問への回答待ちは「作業中」扱いなので、答えるまで削除しない)
        now = clock()
        if now - started >= END_TOTAL_TIMEOUT_SEC:
            raise SessionCtlError(f"{SESSION_END_COMMAND} が時間内に終わりませんでした(セッションは削除していません)")
        info = get_session(session_id)
        if info is None:
            raise SessionCtlError("セッションが途中で終了しました")
        if info.working:
            idle_since = None
        elif idle_since is None:
            idle_since = now
        elif now - idle_since >= END_IDLE_STABLE_SEC:
            break
        sleep(END_POLL_SEC)
    ctl(SESSION_CTL_STOP, name=name)


class RemoteSessionSource:
    """サーバーのWeb版(`/api/sessions`)からセッション一覧を取得する。
    ネットワーク待ちでGUIを固めないよう、取得は別スレッドで周期実行し、get()は最新結果を返す。"""

    def __init__(self, base_url: str = "", token: str = ""):
        self._lock = threading.Lock()
        self._base = ""
        self._token = ""
        self._kickoff: dict | None = None
        self._sessions: list[SessionInfo] = []
        self._error: str | None = "設定してください(右上の ⚙)"
        self.configure(base_url, token)
        threading.Thread(target=self._loop, daemon=True).start()

    def configure(self, base_url: str, token: str) -> None:
        """接続先・トークンを(再)設定する。空なら未設定として取得しない。"""
        with self._lock:
            self._base = base_url.rstrip("/")
            self._token = token
            self._sessions = []
            self._kickoff = None
            self._error = "接続中..." if (base_url and token) else "設定してください(右上の ⚙)"

    def _request(self, path: str, body: dict | None = None) -> dict:
        """サーバーAPIを呼ぶ(bodyがあればPOST)。失敗時はurllib/OSError/ValueErrorを送出する。"""
        with self._lock:
            base, token = self._base, self._token
        headers = {"Authorization": f"Bearer {token}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=REMOTE_FETCH_TIMEOUT_SEC) as res:
            return json.load(res)

    def _fetch(self) -> list[SessionInfo]:
        payload = self._request("/api/sessions")
        names = {f.name for f in fields(SessionInfo)} - {"agents"}
        return [SessionInfo(**{k: v for k, v in item.items() if k in names})
                for item in payload["sessions"]]

    def _loop(self) -> None:
        while True:
            with self._lock:
                configured = bool(self._base and self._token)
            if not configured:
                time.sleep(POLL_INTERVAL_MS / 1000.0)
                continue
            try:
                sessions, error = self._fetch(), None
            except urllib.error.HTTPError as e:
                sessions, error = None, "トークンが無効です" if e.code == 401 else f"HTTP {e.code}"
            except (OSError, ValueError, KeyError, TypeError):
                sessions, error = None, "サーバーに接続できません"
            kickoff = None
            if error is None:
                try:
                    kickoff = self._request("/api/kickoff")
                except (OSError, ValueError):
                    kickoff = None  # Hello状態が取れなくても一覧表示は止めない(前回値を保持)
            with self._lock:
                if sessions is not None:
                    self._sessions = sessions
                if kickoff is not None:
                    self._kickoff = kickoff
                self._error = error
            time.sleep(POLL_INTERVAL_MS / 1000.0)

    def get_kickoff(self) -> dict | None:
        """毎朝4時Hello送信の最新状態(未取得ならNone)。"""
        with self._lock:
            return dict(self._kickoff) if self._kickoff else None

    def set_kickoff(self, enabled: bool) -> bool:
        """Hello送信の有効/無効を切り替える(呼び出し元のスレッドでブロックする)。成否を返す。"""
        try:
            state = self._request("/api/kickoff/toggle", {"enabled": enabled})
        except (OSError, ValueError):
            return False
        with self._lock:
            self._kickoff = state
        return True

    def get(self) -> tuple[list[SessionInfo], str | None]:
        """(直近に取得できた一覧, 接続エラー文言またはNone)。"""
        with self._lock:
            return list(self._sessions), self._error

    def fetch_usage(self) -> dict:
        """サーバーの`/api/usage`(プラン使用量)を取得する(呼び出し元のスレッドでブロックする)。"""
        with self._lock:
            configured = bool(self._base and self._token)
        if not configured:
            return {"error": "未設定"}
        try:
            return self._request("/api/usage")
        except urllib.error.HTTPError as e:
            if e.code == HTTP_UNAUTHORIZED:
                return {"error": "トークンが無効です"}
            try:
                return json.load(e)  # 503でも本文にエラー理由が入っている
            except (OSError, ValueError):
                return {"error": f"HTTP {e.code}"}
        except (OSError, ValueError):
            return {"error": "サーバーに接続できません"}


HTTP_UNAUTHORIZED = 401
USAGE_POLL_SEC = 60.0  # GUIがプラン使用量を取り直す間隔(サーバー側でも同じ間隔でキャッシュする)


class UsageSource:
    """プラン使用量を別スレッドで周期取得する(ネットワーク待ちでGUIを固めない)。get()は最新結果を返す。"""

    def __init__(self, fetch_fn):
        self._fetch_fn = fetch_fn
        self._lock = threading.Lock()
        self._usage: dict = {}
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def refresh_now(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while True:
            try:
                usage = self._fetch_fn()
            except Exception as e:  # 取得側のどんな失敗でも周期取得を止めない
                logging.warning("usage fetch failed: %s", e)
                usage = {"error": "使用量を取得できません"}
            with self._lock:
                if "error" in usage and set(usage) == {"error"}:
                    # 失敗しても前回値は残し、エラー表示だけ更新する
                    self._usage = {**{k: v for k, v in self._usage.items() if k != "error"}, **usage}
                else:
                    self._usage = usage
            self._wake.wait(USAGE_POLL_SEC)
            self._wake.clear()

    def get(self) -> dict:
        with self._lock:
            return dict(self._usage)


def load_ui_prefs() -> dict:
    """画面設定(topmost / width / height)。無い・壊れているときは空。"""
    try:
        data = json.loads(UI_PREFS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_ui_prefs(prefs: dict) -> None:
    try:
        UI_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        UI_PREFS_PATH.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError as e:
        logging.error("ui prefs save failed: %s", e)


def normalize_server_url(raw: str) -> str:
    """入力されたサーバーURLを整える。スキーム省略時はhttps(実運用はTLS)。不正ならValueError。"""
    url = raw.strip().rstrip("/")
    if not url:
        raise ValueError("サーバーURLを入力してください")
    if "://" not in url:
        url = "https://" + url
    if not re.match(r"^https?://[^\s/]+$", url):
        raise ValueError("サーバーURLは https://ホスト名:ポート の形式で入力してください")
    return url


def load_config() -> dict[str, str]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(data.get(k, "")) for k in ("server", "token", "ssh_host", "create_bases")}


def save_config(cfg: dict[str, str]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)  # トークン入りなので本人のみ読めるようにする(Windowsでは実質無効)
    except OSError:
        pass


class SettingsDialog(tk.Toplevel):
    """サーバーURL・トークン・SSH先を入力・保存するダイアログ。保存されたら self.result に辞書が入る。"""

    def __init__(self, master: tk.Tk, current: dict[str, str]):
        super().__init__(master, bg=COLOR_BG)
        self.title("接続設定")
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.result: dict[str, str] | None = None

        self._server = tk.StringVar(value=current.get("server", ""))
        self._token = tk.StringVar(value=current.get("token", ""))
        self._ssh = tk.StringVar(value=current.get("ssh_host") or DEFAULT_SSH_HOST)
        # 既定(ClaudeCode・RPA)は常に使えるので、ここには追加分だけを表示・保存する
        extra = [b for b in parse_create_bases(current.get("create_bases", ""))
                 if b not in DEFAULT_CREATE_BASES]
        self._bases = tk.StringVar(value=CREATE_BASES_SEP.join(extra))
        self._error = tk.StringVar()

        rows = (("サーバーURL", self._server, False), ("トークン", self._token, True),
                ("SSH接続先(端末・作成用)", self._ssh, False),
                (f"追加の作成元(任意・{CREATE_BASES_SEP}区切り)", self._bases, False))
        for r, (label, var, secret) in enumerate(rows):
            tk.Label(self, text=label, bg=COLOR_BG, fg=COLOR_TEXT, anchor="w",
                     font=("Segoe UI", 9)).grid(row=r, column=0, sticky="w", padx=12, pady=(10, 0))
            entry = tk.Entry(self, textvariable=var, width=46, show="*" if secret else "",
                             bg=COLOR_ROW_BG, fg=COLOR_TEXT, insertbackground=COLOR_TEXT,
                             relief="flat", font=("Consolas", 10))
            entry.grid(row=r, column=1, padx=12, pady=(10, 0), ipady=3)
            if secret:
                self._token_entry = entry
        self._show = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="トークンを表示", variable=self._show, command=self._toggle_show,
                       bg=COLOR_BG, fg=COLOR_TEXT_DIM, selectcolor=COLOR_ROW_BG,
                       activebackground=COLOR_BG, activeforeground=COLOR_TEXT,
                       font=("Segoe UI", 8)).grid(row=len(rows), column=1, sticky="w", padx=12)
        tk.Label(self, textvariable=self._error, bg=COLOR_BG, fg=COLOR_ERROR, anchor="w",
                 font=("Segoe UI", 9)).grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", padx=12)
        buttons = tk.Frame(self, bg=COLOR_BG)
        buttons.grid(row=len(rows) + 2, column=0, columnspan=2, sticky="e", padx=12, pady=10)
        tk.Button(buttons, text="保存", command=self._save, width=8).pack(side="right", padx=(6, 0))
        tk.Button(buttons, text="キャンセル", command=self.destroy, width=10).pack(side="right")

        self.bind("<Return>", lambda e: self._save())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.focus_force()

    def _toggle_show(self) -> None:
        self._token_entry.config(show="" if self._show.get() else "*")

    def _save(self) -> None:
        try:
            server = normalize_server_url(self._server.get())
        except ValueError as e:
            self._error.set(str(e))
            return
        token = self._token.get().strip()
        ssh_host = self._ssh.get().strip() or DEFAULT_SSH_HOST
        if not token:
            self._error.set("トークンを入力してください")
            return
        if not SSH_HOST_PATTERN.match(ssh_host):
            self._error.set("SSH接続先は root@claudecode のような形式で入力してください")
            return
        raw_bases = self._bases.get().strip()
        bases = [b for b in parse_create_bases(raw_bases) if b not in DEFAULT_CREATE_BASES]
        if raw_bases and not parse_create_bases(raw_bases):
            self._error.set("追加の作成元は / から始まるサーバー上のパスで入力してください")
            return
        self.result = {"server": server, "token": token, "ssh_host": ssh_host,
                       "create_bases": CREATE_BASES_SEP.join(bases)}
        self.destroy()


class CreateSessionDialog(tk.Toplevel):
    """サーバー上のフォルダを選び、そのフォルダ名をセッション名にして新規作成するダイアログ。
    フォルダ一覧の取得はsshで行うため別スレッド。「作成」が押されたら self.result に (名前, フルパス) が入る。"""

    def __init__(self, master: tk.Tk, ssh_host: str, bases: list[str], existing_names: set[str],
                 initial_base: str = ""):
        super().__init__(master, bg=COLOR_BG)
        self.title("フォルダからセッションを作成")
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.result: tuple[str, str] | None = None
        self._ssh_host = ssh_host
        self._existing = existing_names
        self._folders: list[str] = []
        self._load_seq = 0  # 作成元を素早く切り替えたとき、古い取得結果を捨てるための通し番号

        self._base = tk.StringVar(value=initial_base if initial_base in bases else bases[0])
        # 作成元はワンクリックで切り替えられるよう、ボタンを横に並べる(選択中はアクセント色)
        switcher = tk.Frame(self, bg=COLOR_BG)
        switcher.grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 0))
        tk.Label(switcher, text="作成元", bg=COLOR_BG, fg=COLOR_TEXT, font=("Segoe UI", 9)).pack(
            side="left", padx=(0, 10))
        self._base_buttons: dict[str, tk.Label] = {}
        for base, label in zip(bases, base_labels(bases)):
            btn = tk.Label(switcher, text=label, bg=COLOR_ROW_BG, fg=COLOR_TEXT, font=("Segoe UI", 9, "bold"),
                           padx=12, pady=3, cursor="hand2", highlightthickness=1,
                           highlightbackground=COLOR_BORDER)
            btn.pack(side="left", padx=(0, 6))
            btn.bind("<Button-1>", lambda e, b=base: self._select_base(b))
            self._base_buttons[base] = btn
        self._paint_base_buttons()

        self._list = tk.Listbox(self, height=CREATE_LIST_HEIGHT, width=52, bg=COLOR_ROW_BG, fg=COLOR_TEXT,
                                selectbackground=COLOR_ACCENT, highlightthickness=0, relief="flat",
                                font=("Segoe UI", 10), activestyle="none", exportselection=False)
        self._list.grid(row=1, column=0, columnspan=2, padx=12, pady=(8, 0))
        self._list.bind("<<ListboxSelect>>", lambda e: self._on_select())
        self._list.bind("<Double-Button-1>", lambda e: self._create())

        self._message = tk.StringVar(value="")
        self._message_label = tk.Label(self, textvariable=self._message, bg=COLOR_BG, fg=COLOR_TEXT_DIM,
                                       anchor="w", justify="left", wraplength=420, font=("Segoe UI", 9))
        self._message_label.grid(row=2, column=0, columnspan=2, sticky="w", padx=12, pady=(6, 0))
        buttons = tk.Frame(self, bg=COLOR_BG)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", padx=12, pady=10)
        self._create_btn = tk.Button(buttons, text="作成", command=self._create, width=8, state="disabled")
        self._create_btn.pack(side="right", padx=(6, 0))
        tk.Button(buttons, text="キャンセル", command=self.destroy, width=10).pack(side="right")

        self.bind("<Return>", lambda e: self._create())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()
        self.focus_force()
        self._load()

    def _select_base(self, base: str) -> None:
        if base == self._base.get():
            return
        self._base.set(base)
        self._paint_base_buttons()
        self._load()

    def _paint_base_buttons(self) -> None:
        for base, btn in self._base_buttons.items():
            selected = base == self._base.get()
            btn.config(bg=COLOR_ACCENT if selected else COLOR_ROW_BG,
                       fg=COLOR_TEXT if selected else COLOR_TEXT_DIM)

    def _set_message(self, text: str, error: bool = False) -> None:
        self._message.set(text)
        self._message_label.config(fg=COLOR_ERROR if error else COLOR_TEXT_DIM)

    def _load(self) -> None:
        self._load_seq += 1
        seq, base = self._load_seq, self._base.get()
        self._list.delete(0, "end")
        self._folders = []
        self._create_btn.config(state="disabled")
        self._set_message("フォルダ一覧を取得中...")
        run_in_background(self, lambda: call_session_ctl(self._ssh_host, SESSION_CTL_LISTDIR, base=base),
                          lambda result, error: self._on_loaded(seq, result, error))

    def _on_loaded(self, seq: int, result, error: str | None) -> None:
        if seq != self._load_seq:
            return
        if error:
            self._set_message(error, error=True)
            return
        if not isinstance(result, list):
            self._set_message("フォルダ一覧の形式が不正です", error=True)
            return
        self._folders = [f for f in result if isinstance(f, str)]
        for i, folder in enumerate(self._folders):
            label = folder + ("  (起動中)" if folder in self._existing else "")
            self._list.insert("end", label)
            if self._blocked_reason(folder):
                self._list.itemconfig(i, fg=COLOR_TEXT_DIM)
        self._set_message("フォルダを選ぶと、その名前のセッションを作成します" if self._folders
                          else "フォルダがありません")

    def _blocked_reason(self, folder: str) -> str | None:
        if not SESSION_NAME_PATTERN.match(folder):
            return "セッション名に使えるのは半角英数字・「-」「_」のみ(先頭は英数字、64文字以内)のため、このフォルダからは作成できません"
        if folder in self._existing:
            return f"'{folder}' という名前のセッションが既に起動しています"
        return None

    def _selected_folder(self) -> str | None:
        sel = self._list.curselection()
        return self._folders[sel[0]] if sel and sel[0] < len(self._folders) else None

    def _on_select(self) -> None:
        folder = self._selected_folder()
        if folder is None:
            return
        reason = self._blocked_reason(folder)
        self._create_btn.config(state="disabled" if reason else "normal")
        if reason:
            self._set_message(reason, error=True)
        else:
            self._set_message(f"セッション名: {folder}\n場所: {posixpath.join(self._base.get(), folder)}")

    def _create(self) -> None:
        folder = self._selected_folder()
        if folder is None or self._blocked_reason(folder):
            return
        self.result = (folder, posixpath.join(self._base.get(), folder))  # サーバー側のパスなので常に「/」区切り
        self.destroy()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class Chip(tk.Label):
    """PWAの小さな枠付きボタン(UUID/端末/終了など)。"""

    def __init__(self, master: tk.Widget, text: str, command, bg: str = COLOR_TRACK):
        super().__init__(
            master, text=text, bg=bg, fg=COLOR_TEXT_DIM, font=CHIP_FONT, padx=5, pady=1,
            highlightthickness=1, highlightbackground=COLOR_BORDER, cursor="hand2")
        self._command = command
        self.bind("<Button-1>", lambda e: self._command())
        self.bind("<Enter>", lambda e: self.config(fg=COLOR_TEXT, highlightbackground=COLOR_ACCENT))
        self.bind("<Leave>", lambda e: self.config(fg=COLOR_TEXT_DIM, highlightbackground=COLOR_BORDER))


class ToggleSwitch(tk.Canvas):
    """PWAのスイッチ(46x26の丸角、ON=アクセント色)。"""

    WIDTH, HEIGHT, KNOB = 46, 26, 20

    def __init__(self, master: tk.Widget, bg: str, command, size: tuple[int, int, int] | None = None):
        if size is not None:
            self.WIDTH, self.HEIGHT, self.KNOB = size  # タイトルバー用の小型版など
        super().__init__(master, width=self.WIDTH, height=self.HEIGHT, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._command = command
        self._value = False
        self._enabled = False
        self.bind("<Button-1>", self._on_click)
        self._draw()

    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        self._value = value
        self._draw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._draw()

    def _on_click(self, _event: tk.Event) -> None:
        if not self._enabled:
            return
        self.set(not self._value)
        self._command()

    def _draw(self) -> None:
        self.delete("all")
        h = self.HEIGHT
        track = COLOR_ACCENT if self._value else COLOR_TRACK
        if not self._enabled:
            track = COLOR_TRACK
        self.create_oval(0, 0, h, h, fill=track, outline=track)
        self.create_oval(self.WIDTH - h, 0, self.WIDTH, h, fill=track, outline=track)
        self.create_rectangle(h // 2, 0, self.WIDTH - h // 2, h, fill=track, outline=track)
        pad = (h - self.KNOB) // 2
        x0 = self.WIDTH - pad - self.KNOB if self._value else pad
        self.create_oval(x0, pad, x0 + self.KNOB, pad + self.KNOB,
                         fill="#ffffff" if self._value else COLOR_TEXT_DIM, outline="")


class UsagePanel(tk.Canvas):
    """Claude Usageアプリと同じ表: 行=CURRENT SESSION/WEEKLY LIMIT、
    列=SESSION USED(バー+%)/ELAPSED(枠の経過率リング)/RESETS IN/RESETS AT。
    スクロール領域の外(タイトルバー直下)に固定で置く。"""

    # (キー, 通常の見出し, 幅が狭いときの見出し, 色)
    ROWS = (("session", "CURRENT SESSION", "SESSION", USAGE_SESSION_COLOR),
            ("weekly", "WEEKLY LIMIT", "WEEKLY", USAGE_WEEKLY_COLOR))

    def __init__(self, master: tk.Widget):
        super().__init__(master, bg=COLOR_BG, highlightthickness=0, height=USAGE_PANEL_HEIGHT)
        self._usage: dict = {}
        # フォントはOSで実際の字幅が違う(代替フォントだと広い)ため、列幅は実測で決める
        self._label_font = tkfont.Font(family="Segoe UI", size=9, weight="bold")
        self._at_font = tkfont.Font(family="Segoe UI", size=9)
        self._label_cols = tuple(  # (通常, 狭いとき)の見出し列幅
            max(self._label_font.measure(row[i]) for row in self.ROWS) + USAGE_COL_GAP * 2 for i in (1, 2))
        self._at_col = max(USAGE_AT_COL_WIDTH, self._at_font.measure(USAGE_AT_SAMPLE) + USAGE_COL_GAP)
        self.bind("<Configure>", lambda e: self._draw())

    def update_usage(self, usage: dict) -> None:
        self._usage = usage
        self._draw()

    def _columns(self, width: int) -> dict[str, int]:
        """各列の基準x座標(右端の列から順に右寄せで確保し、残りをバーに充てる)。
        バーが最小幅を割るほど狭いときは短い見出しに切り替える(compact=True)。"""
        at_right = width - ROW_PADDING_X
        in_right = at_right - self._at_col
        ring_center = in_right - USAGE_IN_COL_WIDTH - USAGE_RING_COL_WIDTH // 2
        pct_right = ring_center - USAGE_RING_COL_WIDTH // 2 - USAGE_COL_GAP
        bar_x1 = pct_right - USAGE_PCT_WIDTH
        compact = bar_x1 - (ROW_PADDING_X + self._label_cols[0]) < USAGE_MIN_BAR_WIDTH
        bar_x0 = ROW_PADDING_X + self._label_cols[1 if compact else 0]
        bar_x1 = max(bar_x1, bar_x0 + 4)
        return dict(at_right=at_right, in_right=in_right, ring=ring_center, compact=compact,
                    bar_x0=bar_x0, bar_x1=bar_x1, pct_right=pct_right)

    def _draw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        if width <= 1:
            return
        col = self._columns(width)
        dim, head_font = COLOR_TEXT_DIM, ("Segoe UI", 7)
        y = USAGE_HEADER_Y
        self.create_text((col["bar_x0"] + col["pct_right"]) // 2, y, text="USED" if col["compact"] else "SESSION USED", fill=dim, font=head_font)
        self.create_text(col["ring"], y, text="ELAPSED", fill=dim, font=head_font)
        self.create_text(col["in_right"] - USAGE_IN_COL_WIDTH // 2, y, text="RESETS IN", fill=dim, font=head_font)
        self.create_text(col["at_right"] - self._at_col // 2, y, text="RESETS AT", fill=dim, font=head_font)
        error = self._usage.get("error")
        if error:
            self.create_text(ROW_PADDING_X, y, text=error, fill=COLOR_ERROR, font=head_font, anchor="w")
        now = datetime.now(timezone.utc)
        for idx, (key, label, short_label, color) in enumerate(self.ROWS):
            cy = USAGE_FIRST_ROW_Y + idx * USAGE_ROW_HEIGHT
            self.create_text(ROW_PADDING_X, cy, text=short_label if col["compact"] else label, fill=COLOR_TEXT_DIM,
                             font=self._label_font, anchor="w")
            self._draw_row(self._usage.get(key), cy, col, color, now, key == "session")

    def _draw_row(self, item, cy: int, col: dict[str, int], color: str, now: datetime,
                  is_session: bool) -> None:
        x0, x1 = col["bar_x0"], col["bar_x1"]
        self.create_rectangle(x0, cy - USAGE_BAR_HALF, x1, cy + USAGE_BAR_HALF, fill=COLOR_TRACK, outline="")
        r = USAGE_RING_RADIUS
        rc = col["ring"]
        self.create_oval(rc - r, cy - r, rc + r, cy + r, outline=COLOR_TRACK, width=USAGE_RING_WIDTH)
        resets = parse_iso_utc(item.get("resets_at")) if isinstance(item, dict) else None
        # リセット時刻を過ぎた枠は終了済み(次に使うまで新しい枠は始まらない)。APIが古い値を返す間も
        # 経過率が100%に張り付いたり古い使用率が残ったりしないよう、未使用(0%・空リング・「—」)として描く。
        if isinstance(item, dict) and resets is not None and resets <= now:
            item = {"pct": 0.0}
            resets = None
        if not isinstance(item, dict):
            for x in (col["pct_right"], col["in_right"], col["at_right"]):
                self.create_text(x, cy, text="—", fill=COLOR_TEXT_DIM, font=("Segoe UI", 9), anchor="e")
            return
        pct = min(max(float(item.get("pct") or 0.0), 0.0), 100.0)
        fill_x = x0 + int((x1 - x0) * pct / 100.0)
        if fill_x > x0:
            self.create_rectangle(x0, cy - USAGE_BAR_HALF, fill_x, cy + USAGE_BAR_HALF, fill=color, outline="")
        self.create_text(col["pct_right"], cy, text=f"{pct:.0f}%", fill=COLOR_TEXT,
                         font=("Segoe UI", 10, "bold"), anchor="e")
        if resets is None:
            for x in (col["in_right"], col["at_right"]):
                self.create_text(x, cy, text="—", fill=COLOR_TEXT_DIM, font=("Segoe UI", 9), anchor="e")
            return
        remaining = (resets - now).total_seconds()
        window = float(item.get("window_sec") or 0)
        if window > 0:
            elapsed = min(max(1.0 - remaining / window, 0.0), 1.0)
            if elapsed > 0:
                self.create_arc(rc - r, cy - r, rc + r, cy + r, start=90, extent=-359.9 * elapsed,
                                style="arc", outline=color, width=USAGE_RING_WIDTH)
        self.create_text(col["in_right"], cy, text=format_remaining(remaining) if remaining > 0 else "—",
                         fill=COLOR_TEXT, font=("Segoe UI", 9, "bold"), anchor="e")
        local = resets.astimezone()
        at_text = local.strftime("%H:%M") if is_session else f"{local.month}/{local.day} {local:%H:%M}"
        self.create_text(col["at_right"], cy, text=at_text, fill=COLOR_TEXT_DIM,
                         font=self._at_font, anchor="e")


class SessionRow(tk.Frame):
    """1セッション分の行ウィジェット(PWAの行と同じ構成: 名前+チップ/パス、バー、ランプ)。"""

    LAMP_SIZE = 16

    def __init__(self, master: tk.Widget, bg: str, open_terminal_fn, on_uuid,
                 on_end=None, is_ending=None):
        super().__init__(master, bg=bg, height=ROW_HEIGHT_COMPACT)
        self.pack_propagate(False)
        self._bg = bg
        self._blink_on = True
        self._working = False
        self._attach_cmd = ""
        self._session_id = ""
        self._open_terminal_fn = open_terminal_fn
        self._on_uuid = on_uuid
        self._name_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")

        self.name_label = tk.Label(self, text="", bg=bg, fg=COLOR_TEXT, font=self._name_font, anchor="w")
        self.cwd_label = tk.Label(self, text="", bg=bg, fg=COLOR_TEXT_DIM, font=("Segoe UI", 8), anchor="w")
        self.uuid_chip = Chip(self, "UUID", lambda: self._on_uuid(self._session_id), bg)
        self.term_chip = Chip(self, "端末", self._on_terminal, bg)
        # 終了(/session-end→削除)はリモートモードかつtmuxのセッションだけ
        self._on_end = on_end
        self._is_ending = is_ending or (lambda _sid: False)
        self._info: SessionInfo | None = None
        self.end_chip = Chip(self, "終了", lambda: self._on_end(self._info), bg)

        self.bar_canvas = tk.Canvas(self, width=BAR_COL_WIDTH, height=BAR_CANVAS_HEIGHT, bg=bg, highlightthickness=0)
        self.bar_canvas.place(relx=1.0, x=-(ROW_PADDING_X + STATUS_COL_WIDTH), rely=0.5, anchor="e")
        self.lamp_canvas = tk.Canvas(self, width=self.LAMP_SIZE + 8, height=self.LAMP_SIZE + 8,
                                     bg=bg, highlightthickness=0)
        self.lamp_canvas.place(relx=1.0, x=-ROW_PADDING_X, rely=0.5, anchor="e")

    def _on_terminal(self) -> None:
        if not self._open_terminal_fn(self._attach_cmd):
            self.term_chip.config(text="失敗")
            self.after(1500, lambda: self.term_chip.config(text="端末"))

    def update_bg(self, bg: str) -> None:
        """最近アクティブ順の並べ替えで行の位置(縞模様)が変わったときに背景色を追従させる。"""
        if bg == self._bg:
            return
        self._bg = bg
        for w in (self, self.name_label, self.cwd_label, self.uuid_chip,
                  self.term_chip, self.end_chip, self.bar_canvas, self.lamp_canvas):
            w.config(bg=bg)

    def update_data(self, info: SessionInfo, revealed: bool, name_col: int) -> None:
        self._session_id = info.session_id
        self._attach_cmd = info.attach_cmd
        self._working = info.working
        self._info = info
        self.end_chip.config(text="終了中…" if self._is_ending(info.session_id) else "終了")
        self.name_label.config(text=info.name)
        self.cwd_label.config(text=info.cwd)
        self.uuid_chip.config(text=info.session_id if revealed else "UUID")
        self._draw_bar(info)
        self._draw_lamp()
        self._layout(info, name_col)

    def _ellipsize(self, text: str, max_width: int) -> str:
        if self._name_font.measure(text) <= max_width:
            return text
        while text and self._name_font.measure(text + "…") > max_width:
            text = text[:-1]
        return text + "…"

    def _layout(self, info: SessionInfo, name_col: int) -> None:
        """名前の右にチップを並べ、列幅に収まらなければ次の行へ折り返す(PWAと同じ挙動)。"""
        # 名前が列幅を超えるとバー(右隣の列)に隠れるため、収まらない分は「…」で省略する
        shown = self._ellipsize(info.name, max(name_col - ROW_PADDING_X - 4, 40))
        self.name_label.config(text=shown)
        name_w = self._name_font.measure(shown)
        # フォントによって行の高さが違うため、ラベル自身が要求する高さ以上を確保する(切れ防止)
        name_line = max(NAME_LINE_HEIGHT, self.name_label.winfo_reqheight())
        chips = [self.uuid_chip]
        if info.attach_cmd:
            chips.append(self.term_chip)
        else:
            self.term_chip.place_forget()
        if self._on_end is not None and TMUX_ATTACH_PATTERN.match(info.attach_cmd):
            chips.append(self.end_chip)
        else:
            self.end_chip.place_forget()
        max_x = name_col - 4
        x, y = ROW_PADDING_X + name_w + CHIP_GAP * 2, ROW_NAME_Y
        wrapped = False
        self.name_label.place(x=ROW_PADDING_X, y=ROW_NAME_Y, width=name_w + 4, height=name_line)
        for chip in chips:
            w = chip.winfo_reqwidth()
            if x + w > max_x and not wrapped:
                wrapped, x, y = True, ROW_PADDING_X, ROW_NAME_Y + name_line + 2
            elif x + w > max_x:
                x, y = ROW_PADDING_X, y + CHIP_LINE_HEIGHT
            chip.place(x=x, y=y, height=CHIP_LINE_HEIGHT - 2)
            x += w + CHIP_GAP
        last_y = y if wrapped else ROW_NAME_Y
        cwd_y = last_y + (CHIP_LINE_HEIGHT if wrapped else name_line) + 2
        self.cwd_label.place(x=ROW_PADDING_X, y=cwd_y, width=max(name_col - ROW_PADDING_X, 40), height=14)
        self.config(height=cwd_y + 14 + ROW_PADDING_Y)

    def _draw_bar(self, info: SessionInfo) -> None:
        c = self.bar_canvas
        c.delete("all")
        mid = BAR_CANVAS_HEIGHT // 2
        track_y0, track_y1 = mid - 4, mid + 4
        c.create_rectangle(0, track_y0, BAR_TRACK_WIDTH, track_y1, fill=COLOR_TRACK, outline="")
        fill_w = int(BAR_TRACK_WIDTH * min(info.context_pct, 100.0) / 100.0)
        if fill_w > 0:
            c.create_rectangle(0, track_y0, fill_w, track_y1, fill=COLOR_ACCENT, outline="")
        label = (f"{info.context_pct:.0f}% "
                 f"({format_tokens(info.context_tokens)}/{format_tokens(info.context_limit)})")
        c.create_text(BAR_TRACK_WIDTH + 8, mid + BAR_TEXT_OFFSET, text=label, fill=COLOR_TEXT, anchor="w",
                      font=("Segoe UI", CONTEXT_FONT_SIZE))
        if info.model:
            c.create_text(BAR_TRACK_WIDTH + 8, mid - BAR_TEXT_OFFSET, text=info.model, fill=COLOR_TEXT_DIM,
                          anchor="w", font=("Segoe UI", MODEL_FONT_SIZE))

    def _draw_lamp(self) -> None:
        c = self.lamp_canvas
        c.delete("all")
        size = self.LAMP_SIZE + 8
        cx = cy = size // 2
        r = self.LAMP_SIZE // 2
        if self._working:
            color = COLOR_RED_ON if self._blink_on else COLOR_RED_OFF
        else:
            color = COLOR_GREEN
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")

    def toggle_blink(self) -> None:
        self._blink_on = not self._blink_on
        if self._working:
            self._draw_lamp()


class ClaudeMonitorApp:
    def __init__(self, root: tk.Tk, remote: RemoteSessionSource | None = None,
                 ssh_host: str | None = None):
        self.root = root
        self._remote = remote
        self._ssh_host = ssh_host
        self._open_terminal_fn = lambda cmd: open_terminal(cmd, self._ssh_host)
        self.rows: dict[str, SessionRow] = {}
        self._revealed: set[str] = set()
        self._last_sessions: list[SessionInfo] = []

        root.overrideredirect(True)
        root.configure(bg=COLOR_BG, highlightthickness=1, highlightbackground=COLOR_BORDER)
        self._prefs = load_ui_prefs()
        self._topmost = bool(self._prefs.get("topmost"))
        root.attributes("-topmost", self._topmost)
        self._width = max(int(self._prefs.get("width") or WINDOW_WIDTH), MIN_WINDOW_WIDTH)
        saved_height = int(self._prefs.get("height") or 0)
        # 一度でも手動でリサイズした(高さが保存されている)ら、以後は高さを自動調整しない
        self._user_sized = saved_height >= MIN_WINDOW_HEIGHT
        self._height = saved_height if self._user_sized else 60
        root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        root.geometry(f"{self._width}x{self._height}+80+80")
        self._relayout_job: str | None = None

        self._kickoff_syncing = False
        self._kickoff_result: tuple[bool, bool] | None = None
        self._creating = False  # セッション作成の通信中(二重実行を防ぐ)
        self._ending: set[str] = set()  # 「終了」処理中のsession_id(二重実行を防ぐ)
        self._build_titlebar()
        self._usage_source = UsageSource(
            self._remote.fetch_usage if self._remote is not None else fetch_plan_usage)
        self._usage_panel = UsagePanel(self.root)
        self._usage_panel.pack(fill="x", pady=(4, 0))
        tk.Frame(self.root, bg=COLOR_BORDER, height=1).pack(fill="x", padx=12)
        self._build_header()
        self._build_scroll_area()

        self.empty_label = tk.Label(
            self.body, text="アクティブなセッションはありません",
            bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=("Segoe UI", 9),
        )
        self._build_resize_handles()

        self.refresh()
        self._blink()

    # -- UI構築 -------------------------------------------------------

    def _build_titlebar(self) -> None:
        bar = tk.Frame(self.root, bg=COLOR_TITLEBAR, height=34)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        tk.Label(
            bar, text="●", bg=COLOR_TITLEBAR, fg=COLOR_ACCENT, font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(14, 0))
        tk.Label(
            bar, text="Claude Code Monitor", bg=COLOR_TITLEBAR,
            fg=COLOR_TEXT, font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=(6, 0))

        close_btn = tk.Label(
            bar, text="✕", bg=COLOR_TITLEBAR, fg=COLOR_TEXT_DIM,
            font=("Segoe UI", 10), cursor="hand2",
        )
        close_btn.pack(side="right", padx=12)
        close_btn.bind("<Button-1>", lambda e: self.root.destroy())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=COLOR_RED_ON))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=COLOR_TEXT_DIM))

        self._pin_btn = tk.Label(
            bar, text="📌", bg=COLOR_TITLEBAR, fg=COLOR_ACCENT if self._topmost else COLOR_TEXT_DIM,
            font=("Segoe UI Emoji", 9), cursor="hand2",
        )
        self._pin_btn.pack(side="right", padx=4)
        self._pin_btn.bind("<Button-1>", lambda e: self._toggle_topmost())

        if self._remote is not None:
            settings_btn = tk.Label(
                bar, text="⚙", bg=COLOR_TITLEBAR, fg=COLOR_TEXT_DIM,
                font=("Segoe UI", 10), cursor="hand2",
            )
            settings_btn.pack(side="right", padx=4)
            settings_btn.bind("<Button-1>", lambda e: self.open_settings())

            self._create_btn = tk.Label(
                bar, text="＋", bg=COLOR_TITLEBAR, fg=COLOR_TEXT_DIM,
                font=("Segoe UI", 10, "bold"), cursor="hand2",
            )
            self._create_btn.pack(side="right", padx=4)
            self._create_btn.bind("<Button-1>", lambda e: self.open_create_dialog())
            self._create_btn.bind("<Enter>", lambda e: self._create_btn.config(fg=COLOR_TEXT))
            self._create_btn.bind("<Leave>", lambda e: self._create_btn.config(
                fg=COLOR_ACCENT if self._creating else COLOR_TEXT_DIM))

        refresh_btn = tk.Label(
            bar, text="↻", bg=COLOR_TITLEBAR, fg=COLOR_TEXT_DIM,
            font=("Segoe UI", 10), cursor="hand2",
        )
        refresh_btn.pack(side="right", padx=4)
        refresh_btn.bind("<Button-1>", lambda e: self._manual_refresh())

        if self._remote is not None:
            self._build_kickoff_toggle(bar)

        # ウィンドウドラッグ
        for widget in (bar,):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._on_move)

    def _toggle_topmost(self) -> None:
        """最前面固定のオン/オフ。枠なしウィンドウはタスクバーに出ないため、切り替え手段を残す。"""
        self._topmost = not self._topmost
        self.root.attributes("-topmost", self._topmost)
        self._pin_btn.config(fg=COLOR_ACCENT if self._topmost else COLOR_TEXT_DIM)
        self._prefs["topmost"] = self._topmost
        save_ui_prefs(self._prefs)

    def _build_kickoff_toggle(self, bar: tk.Frame) -> None:
        """毎朝4時(JST)のHello送信のスイッチをタイトルバーに置く。前回の結果は「Hello」の文字色で示す
        (成功=緑/エラー=赤)。"""
        self._kickoff_switch = ToggleSwitch(bar, COLOR_TITLEBAR, self._on_kickoff_toggle,
                                            size=TITLE_SWITCH_SIZE)
        self._kickoff_switch.pack(side="right", padx=(2, 8))
        self._kickoff_result_label = tk.Label(bar, text="Hello", bg=COLOR_TITLEBAR, fg=COLOR_TEXT_DIM,
                                              font=("Segoe UI", 8, "bold"))
        self._kickoff_result_label.pack(side="right")

    def _manual_refresh(self) -> None:
        self._usage_source.refresh_now()
        self._render(*self._current_sessions())

    def _on_kickoff_toggle(self) -> None:
        enabled = self._kickoff_switch.get()
        self._kickoff_syncing = True
        self._kickoff_switch.set_enabled(False)

        def work() -> None:
            # tkのウィジェットはメインスレッド以外から触らない。結果だけ置き、次のrefreshで反映する
            self._kickoff_result = (self._remote.set_kickoff(enabled), enabled)
        threading.Thread(target=work, daemon=True).start()

    def _update_kickoff_panel(self) -> None:
        if self._remote is None:
            return
        if self._kickoff_syncing:
            if self._kickoff_result is None:
                return  # 切り替え通信の完了待ち
            ok, enabled = self._kickoff_result
            self._kickoff_result = None
            self._kickoff_syncing = False
            if not ok:
                self._kickoff_switch.set(not enabled)  # 失敗したので表示を元に戻す
        state = self._remote.get_kickoff()
        if state is None:
            return
        self._kickoff_switch.set(bool(state.get("enabled")))
        self._kickoff_switch.set_enabled(True)
        status = state.get("last_status")
        self._kickoff_result_label.config(
            fg={"success": COLOR_GREEN, "error": COLOR_RED_ON}.get(status, COLOR_TEXT_DIM))

    def open_settings(self) -> None:
        """接続設定ダイアログを開き、保存されたら即座に接続し直す。"""
        if self._remote is None:
            return
        dialog = SettingsDialog(self.root, load_config())
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        try:
            save_config(dialog.result)
        except OSError as e:
            logging.error("config save failed: %s", e)
            messagebox.showerror("Claude Code Monitor", f"設定を保存できませんでした: {CONFIG_PATH}\n({e})")
        logging.info("settings updated: server=%s ssh_host=%s",
                     dialog.result["server"], dialog.result["ssh_host"])
        self._ssh_host = dialog.result["ssh_host"]
        self._remote.configure(dialog.result["server"], dialog.result["token"])

    def open_create_dialog(self) -> None:
        """フォルダを選んで、フォルダ名をセッション名にした新しいセッションをサーバー上に作る。"""
        if self._remote is None or self._creating:
            return
        bases = effective_create_bases(load_config().get("create_bases", ""))
        if not self._ssh_host:
            self.open_settings()
            return
        existing = {s.name for s in self._last_sessions}
        dialog = CreateSessionDialog(self.root, self._ssh_host, bases, existing,
                                     initial_base=str(self._prefs.get("create_base") or ""))
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        name, directory = dialog.result
        self._prefs["create_base"] = posixpath.dirname(directory)  # 次回は前回と同じ作成元を開く
        save_ui_prefs(self._prefs)
        ssh_host = self._ssh_host
        self._set_creating(True)
        logging.info("create session: name=%s dir=%s", name, directory)
        run_in_background(
            self.root,
            lambda: call_session_ctl(ssh_host, SESSION_CTL_START, name=name, dir=directory),
            lambda _result, error: self._on_created(name, ssh_host, error))

    def end_session(self, info: SessionInfo | None) -> None:
        """「終了」: /session-end を送り、完了を確認してからセッションを削除する(会話履歴は残る)。"""
        if self._remote is None or info is None or info.session_id in self._ending:
            return
        m = TMUX_ATTACH_PATTERN.match(info.attach_cmd)
        if not m:
            return
        name, session_id, ssh_host = m.group(1), info.session_id, self._ssh_host
        if info.working:
            messagebox.showinfo("Claude Code Monitor", f"'{name}' は作業中です。待機中(緑)になってから押してください。")
            return
        if not messagebox.askyesno(
                "Claude Code Monitor",
                f"'{name}' に {SESSION_END_COMMAND} を送り、完了したらセッションを削除します。\n"
                "(会話履歴は残ります。途中で質問が出たら端末で答えてください)\n\nよろしいですか?"):
            return
        self._ending.add(session_id)
        self._render(*self._current_sessions())
        logging.info("end session: name=%s id=%s", name, session_id)

        def get_session(sid: str) -> SessionInfo | None:
            sessions, _error = self._remote.get()  # 取得失敗中は前回値(スレッドセーフ)
            return next((s for s in sessions if s.session_id == sid), None)

        run_in_background(
            self.root,
            lambda: end_session_workflow(
                name, session_id, lambda cmd, **p: call_session_ctl(ssh_host, cmd, **p), get_session),
            lambda _result, error: self._on_ended(name, session_id, error))

    def _on_ended(self, name: str, session_id: str, error: str | None) -> None:
        self._ending.discard(session_id)
        self._render(*self._current_sessions())
        if error:
            logging.error("end session failed: %s: %s", name, error)
            messagebox.showerror("Claude Code Monitor", f"'{name}' の終了処理を中断しました。\n\n{error}")
            return
        logging.info("end session done: %s", name)

    def _set_creating(self, creating: bool) -> None:
        self._creating = creating
        self._create_btn.config(text="…" if creating else "＋",
                                fg=COLOR_ACCENT if creating else COLOR_TEXT_DIM)

    def _on_created(self, name: str, ssh_host: str, error: str | None) -> None:
        self._set_creating(False)
        if error:
            logging.error("create session failed: %s", error)
            messagebox.showerror("Claude Code Monitor", f"'{name}' を作成できませんでした。\n\n{error}")
            return
        # 初めてのフォルダではClaudeが信頼確認画面で止まるので、アプリでは承認せず端末で本人に答えてもらう
        if messagebox.askyesno(
                "Claude Code Monitor",
                f"'{name}' を作成しました。\n端末で開きますか?\n\n"
                "(初めてのフォルダでは、フォルダを信頼するかの確認が表示されます)"):
            if not open_terminal(f"tmux attach -t ={name}", ssh_host):
                messagebox.showerror("Claude Code Monitor", "端末を開けませんでした")

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=COLOR_BG, height=HEADER_HEIGHT)
        self._header = header
        header.pack(fill="x")
        header.pack_propagate(False)
        font = ("Segoe UI", 8)
        tk.Label(header, text="SESSION", bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=font).place(
            x=ROW_PADDING_X, y=8)
        tk.Label(header, text="CONTEXT USED", bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=font).place(
            relx=1.0, x=-(ROW_PADDING_X + STATUS_COL_WIDTH + BAR_COL_WIDTH), y=8, anchor="nw")
        tk.Label(header, text="STATUS", bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=font).place(
            relx=1.0, x=-ROW_PADDING_X, y=8, anchor="ne")

    def _start_move(self, event: tk.Event) -> None:
        self._drag_x, self._drag_y = event.x, event.y

    def _on_move(self, event: tk.Event) -> None:
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    # -- UUID表示 -------------------------------------------------------

    def _toggle_uuid(self, session_id: str) -> None:
        self._revealed.symmetric_difference_update({session_id})
        self._render(self._last_sessions, self._last_error)

    # -- データ更新 -----------------------------------------------------

    def _current_sessions(self) -> tuple[list[SessionInfo], str | None]:
        if self._remote is not None:
            return self._remote.get()
        return get_sessions(), None

    def refresh(self) -> None:
        sessions, error = self._current_sessions()
        self._update_kickoff_panel()
        self._usage_panel.update_usage(self._usage_source.get())  # 残り時間・経過率は毎回手元で再計算
        self._render(sessions, error)
        self.root.after(POLL_INTERVAL_MS, self.refresh)

    def _render(self, sessions: list[SessionInfo], error: str | None) -> None:
        self._last_sessions, self._last_error = sessions, error
        visible = sessions
        if error:
            empty_text, empty_fg = error, COLOR_ERROR
        else:
            empty_text, empty_fg = "アクティブなセッションはありません", COLOR_TEXT_DIM
        self.empty_label.config(text=empty_text, fg=empty_fg)

        seen_ids = {s.session_id for s in visible}
        # 消えたセッションの行を削除
        for sid in list(self.rows.keys()):
            if sid not in seen_ids:
                self.rows.pop(sid).destroy()

        if not visible or error:
            self.empty_label.pack(fill="x", pady=12)
        else:
            self.empty_label.pack_forget()

        # pack()は既存ウィジェットを再度呼んでも並び順を変えないため、いったん全行を
        # 配置解除してから新しい順序で詰め直す(rows dict自体は使い回し、再生成はしない)。
        for row in self.rows.values():
            row.pack_forget()

        for idx, info in enumerate(visible):
            row_bg = COLOR_ROW_BG if idx % 2 == 0 else COLOR_ROW_ALT_BG
            row = self.rows.get(info.session_id)
            if row is None:
                row = SessionRow(self.body, row_bg, self._open_terminal_fn,
                                 self._toggle_uuid,
                                 on_end=self.end_session if self._remote is not None else None,
                                 is_ending=lambda sid: sid in self._ending)
                self.rows[info.session_id] = row
            row.pack(fill="x")
            row.update_bg(row_bg)
            row.update_data(info, info.session_id in self._revealed, self._name_col())

        self._fit_scroll_area()

    # -- リサイズ・スクロール ---------------------------------------------

    def _build_scroll_area(self) -> None:
        """セッション一覧をスクロール可能なCanvasに載せる(高さが足りないとき用)。"""
        holder = tk.Frame(self.root, bg=COLOR_BG)
        holder.pack(fill="both", expand=True)
        # 矢印なしのttkスクロールバーは自身では幅が決まらないため、固定幅の枠に入れる
        self._scrollbar_box = tk.Frame(holder, width=SCROLLBAR_WIDTH_PX, bg=COLOR_BG)
        self._scrollbar_box.pack_propagate(False)
        self._scrollbar = ttk.Scrollbar(self._scrollbar_box, orient="vertical",
                                        style=self._scrollbar_style())
        self._scrollbar.pack(fill="both", expand=True)
        self._scroll = tk.Canvas(holder, bg=COLOR_BG, highlightthickness=0, height=1,
                                 yscrollcommand=self._scrollbar.set)
        self._scrollbar.config(command=self._scroll.yview)
        self._scroll.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self._scroll, bg=COLOR_BG)
        self._body_window = self._scroll.create_window((0, 0), window=self.body, anchor="nw")
        self._scroll.bind("<Configure>", self._on_scroll_area_configure)
        self.body.bind("<Configure>", lambda e: self._scroll.configure(
            scrollregion=self._scroll.bbox("all")))
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        self.root.bind_all("<Button-4>", lambda e: self._scroll.yview_scroll(-3, "units"))
        self.root.bind_all("<Button-5>", lambda e: self._scroll.yview_scroll(3, "units"))

    @staticmethod
    def _scrollbar_style() -> str:
        """標準のtk.ScrollbarはWindowsで色を変えられないため、ttk(clam)でアプリの配色に揃える。
        矢印は省き、細いつまみだけにする。"""
        style = ttk.Style()
        style.theme_use("clam")
        style.layout(SCROLLBAR_STYLE, [("Vertical.Scrollbar.trough", {
            "sticky": "ns",
            "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure(SCROLLBAR_STYLE, troughcolor=COLOR_BG, background=COLOR_BORDER,
                        bordercolor=COLOR_BG, lightcolor=COLOR_BORDER, darkcolor=COLOR_BORDER,
                        relief="flat", borderwidth=0, width=SCROLLBAR_WIDTH_PX,
                        arrowsize=SCROLLBAR_WIDTH_PX)
        style.map(SCROLLBAR_STYLE, background=[("active", COLOR_ACCENT), ("pressed", COLOR_ACCENT)],
                  lightcolor=[("active", COLOR_ACCENT), ("pressed", COLOR_ACCENT)],
                  darkcolor=[("active", COLOR_ACCENT), ("pressed", COLOR_ACCENT)])
        return SCROLLBAR_STYLE

    def _on_wheel(self, event: tk.Event) -> None:
        step = -1 if event.delta > 0 else 1
        self._scroll.yview_scroll(step * 3, "units")

    def _on_scroll_area_configure(self, event: tk.Event) -> None:
        self._scroll.itemconfigure(self._body_window, width=event.width)
        self._schedule_relayout()

    def _name_col(self) -> int:
        width = self._scroll.winfo_width()
        return name_col_width(width if width > 1 else self._width)

    def _schedule_relayout(self) -> None:
        """幅が変わったら行の配置を計算し直す(ドラッグ中に連打されないよう間引く)。"""
        if self._relayout_job is not None:
            self.root.after_cancel(self._relayout_job)
        self._relayout_job = self.root.after(RELAYOUT_DELAY_MS, self._relayout)

    def _relayout(self) -> None:
        self._relayout_job = None
        self._render(self._last_sessions, self._last_error)

    def _fit_scroll_area(self) -> None:
        """未リサイズなら高さを内容に合わせる(画面内に収まる範囲)。手動リサイズ後は高さを維持し、
        はみ出す分はスクロールバーで見る。"""
        self.root.update_idletasks()
        content_h = self.body.winfo_reqheight()
        view_h = self._scroll.winfo_height()
        if not self._user_sized:
            chrome = self.root.winfo_reqheight() - self._scroll.winfo_reqheight()
            limit = self.root.winfo_screenheight() - SCREEN_MARGIN_PX
            self._height = min(chrome + content_h, limit)
            self.root.geometry(f"{self._width}x{self._height}")
            self.root.update_idletasks()
            view_h = self._scroll.winfo_height()
        overflow = content_h > view_h > 1
        if overflow and not self._scrollbar_box.winfo_ismapped():
            self._scrollbar_box.pack(side="right", fill="y")
        elif not overflow and self._scrollbar_box.winfo_ismapped():
            self._scrollbar_box.pack_forget()
            self._scroll.yview_moveto(0)

    def _build_resize_handles(self) -> None:
        """枠なしウィンドウ用のリサイズ手段: 右端・下端の帯と右下の角(PWAのような自動折り返しに追従)。"""
        specs = (
            ("sb_h_double_arrow", dict(relx=1.0, x=-RESIZE_EDGE_PX, y=0, relheight=1.0,
                                       width=RESIZE_EDGE_PX), True, False),
            ("sb_v_double_arrow", dict(rely=1.0, y=-RESIZE_EDGE_PX, x=0, relwidth=1.0,
                                       height=RESIZE_EDGE_PX), False, True),
            (CORNER_CURSOR, dict(relx=1.0, rely=1.0, x=-RESIZE_GRIP_PX, y=-RESIZE_GRIP_PX,
                                width=RESIZE_GRIP_PX, height=RESIZE_GRIP_PX), True, True),
        )
        for cursor, place, resize_w, resize_h in specs:
            handle = tk.Frame(self.root, cursor=cursor, bg=COLOR_BG)
            handle.place(**place)
            handle.lift()
            handle.bind("<ButtonPress-1>", self._start_resize)
            handle.bind("<B1-Motion>", lambda e, w=resize_w, h=resize_h: self._on_resize(e, w, h))
            handle.bind("<ButtonRelease-1>", lambda e: self._end_resize())

    def _start_resize(self, event: tk.Event) -> None:
        self._rs_start = (event.x_root, event.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _on_resize(self, event: tk.Event, resize_w: bool, resize_h: bool) -> None:
        x0, y0, w0, h0 = self._rs_start
        width = max(MIN_WINDOW_WIDTH, w0 + (event.x_root - x0)) if resize_w else w0
        height = max(MIN_WINDOW_HEIGHT, h0 + (event.y_root - y0)) if resize_h else h0
        self._width, self._height = width, height
        if resize_h:
            self._user_sized = True
        self.root.geometry(f"{width}x{height}")

    def _end_resize(self) -> None:
        self._prefs.update(width=self._width)
        if self._user_sized:
            self._prefs["height"] = self._height
        save_ui_prefs(self._prefs)

    def _blink(self) -> None:
        for row in self.rows.values():
            row.toggle_blink()
        self.root.after(BLINK_INTERVAL_MS, self._blink)


class StartupError(Exception):
    """起動条件の不備(トークン未設定など)。メッセージはそのままユーザーに見せる。"""


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH, level=logging.INFO, encoding="utf-8",
        format="%(asctime)s %(levelname)s %(message)s")


def _read_token(args: argparse.Namespace) -> str:
    if args.token_file:
        path = Path(args.token_file).expanduser()
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise StartupError(f"トークンファイルを読めません: {path}\n({e})") from e
    return os.environ.get(TOKEN_ENV_VAR, "").strip()


def main() -> None:
    _setup_logging()
    try:
        _run()
    except StartupError as e:
        logging.error("startup error: %s", e)
        _show_fatal(str(e))
    except Exception:
        logging.exception("fatal error")
        _show_fatal(f"予期しないエラーで終了しました。詳細: {LOG_PATH}")


def _show_fatal(message: str) -> None:
    """コンソールの無いpyw起動でも気づけるよう、ダイアログで知らせる。"""
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Claude Code Monitor", message)
        root.destroy()
    except tk.TclError:
        pass  # GUIが出せない環境ではログのみ


def _run() -> None:
    parser = argparse.ArgumentParser(description="Claude Code Monitor (GUI)")
    parser.add_argument("--remote", action="store_true",
                        help="リモートモード。接続設定はGUIの ⚙ で入力・保存する(config.json)")
    parser.add_argument("--server", help="リモートモード: Web版のURL(保存済み設定より優先)")
    parser.add_argument("--token-file", help=f"Bearerトークンのファイル(保存済み設定より優先。環境変数{TOKEN_ENV_VAR}も可)")
    parser.add_argument("--ssh-host", help=f"「端末」ボタンで接続するSSH先(既定 {DEFAULT_SSH_HOST})")
    args = parser.parse_args()

    remote = None
    ssh_host = None
    if args.remote or args.server:
        cfg = load_config()
        try:
            server = normalize_server_url(args.server) if args.server else cfg.get("server", "")
        except ValueError as e:
            raise StartupError(str(e)) from e
        token = _read_token(args) or cfg.get("token", "")
        ssh_host = args.ssh_host or cfg.get("ssh_host") or DEFAULT_SSH_HOST
        remote = RemoteSessionSource(server, token)
        logging.info("remote mode: server=%s ssh_host=%s", server or "(unset)", ssh_host)
    root = tk.Tk()
    app = ClaudeMonitorApp(root, remote=remote, ssh_host=ssh_host)
    if remote is not None and not (server and token):
        root.after(200, app.open_settings)  # 未設定なら、起動直後に設定ダイアログを出す
    root.mainloop()


if __name__ == "__main__":
    main()
