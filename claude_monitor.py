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
import re
import shlex
import shutil
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
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
BAR_COL_WIDTH = 190
STATUS_COL_WIDTH = 60
BAR_TRACK_WIDTH = 70
NAME_COL_WIDTH = WINDOW_WIDTH - ROW_PADDING_X * 2 - BAR_COL_WIDTH - STATUS_COL_WIDTH
UI_PREFS_PATH = Path.home() / ".claude_monitor" / "ui.json"  # 画面設定(最前面ピン)
HIDDEN_PATH = Path.home() / ".claude_monitor" / "hidden.json"  # 「非表示」にしたセッションID
KICKOFF_STATUS_LABELS = {"success": "成功", "error": "エラー", "skipped": "スキップ"}
POLL_INTERVAL_MS = 1500             # セッション一覧・使用量の再取得間隔
AGENT_VISIBLE_SEC = 120.0           # サブエージェントのトランスクリプトが、この秒数以内に更新されていれば一覧に出す
SUBAGENTS_DIRNAME = "subagents"     # ~/.claude/projects/*/<sessionId>/subagents/agent-<id>.jsonl
AGENT_FILE_GLOB = "agent-*.jsonl"
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


def load_topmost() -> bool:
    """最前面に固定するか。既定はオフ(他のウィンドウの邪魔をしない)。"""
    try:
        return bool(json.loads(UI_PREFS_PATH.read_text(encoding="utf-8")).get("topmost"))
    except (OSError, ValueError, AttributeError):
        return False


def save_topmost(value: bool) -> None:
    try:
        UI_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        UI_PREFS_PATH.write_text(json.dumps({"topmost": value}), encoding="utf-8")
    except OSError as e:
        logging.error("ui prefs save failed: %s", e)


def load_hidden() -> set[str]:
    try:
        data = json.loads(HIDDEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {x for x in data if isinstance(x, str)} if isinstance(data, list) else set()


def save_hidden(hidden: set[str]) -> None:
    try:
        HIDDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        HIDDEN_PATH.write_text(json.dumps(sorted(hidden)), encoding="utf-8")
    except OSError as e:
        logging.error("hidden save failed: %s", e)


def format_kickoff_time(iso: str | None) -> str:
    """Hello送信の前回実行時刻(ISO8601)をローカル時刻の短い表記にする。"""
    if not iso:
        return "(まだ実行なし)"
    try:
        d = datetime.fromisoformat(iso).astimezone()
        return f"{d.year}/{d.month}/{d.day} {d:%H:%M:%S}"
    except ValueError:
        return iso


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
    return {k: str(data.get(k, "")) for k in ("server", "token", "ssh_host")}


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
        self._error = tk.StringVar()

        rows = (("サーバーURL", self._server, False), ("トークン", self._token, True),
                ("SSH接続先(端末ボタン用)", self._ssh, False))
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
                       font=("Segoe UI", 8)).grid(row=3, column=1, sticky="w", padx=12)
        tk.Label(self, textvariable=self._error, bg=COLOR_BG, fg=COLOR_ERROR, anchor="w",
                 font=("Segoe UI", 9)).grid(row=4, column=0, columnspan=2, sticky="w", padx=12)
        buttons = tk.Frame(self, bg=COLOR_BG)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", padx=12, pady=10)
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
        self.result = {"server": server, "token": token, "ssh_host": ssh_host}
        self.destroy()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class Chip(tk.Label):
    """PWAの小さな枠付きボタン(UUID/非表示/端末など)。"""

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

    def __init__(self, master: tk.Widget, bg: str, command):
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


class SessionRow(tk.Frame):
    """1セッション分の行ウィジェット(PWAの行と同じ構成: 名前+チップ/パス、バー、ランプ)。"""

    LAMP_SIZE = 16

    def __init__(self, master: tk.Widget, bg: str, open_terminal_fn, on_hide, on_uuid):
        super().__init__(master, bg=bg, height=ROW_HEIGHT_COMPACT)
        self.pack_propagate(False)
        self._bg = bg
        self._blink_on = True
        self._working = False
        self._attach_cmd = ""
        self._session_id = ""
        self._open_terminal_fn = open_terminal_fn
        self._on_hide = on_hide
        self._on_uuid = on_uuid
        self._name_font = tkfont.Font(family="Segoe UI", size=11, weight="bold")

        self.name_label = tk.Label(self, text="", bg=bg, fg=COLOR_TEXT, font=self._name_font, anchor="w")
        self.cwd_label = tk.Label(self, text="", bg=bg, fg=COLOR_TEXT_DIM, font=("Segoe UI", 8), anchor="w")
        self.uuid_chip = Chip(self, "UUID", lambda: self._on_uuid(self._session_id), bg)
        self.hide_chip = Chip(self, "非表示", lambda: self._on_hide(self._session_id), bg)
        self.term_chip = Chip(self, "端末", self._on_terminal, bg)

        self.bar_canvas = tk.Canvas(self, width=BAR_COL_WIDTH, height=36, bg=bg, highlightthickness=0)
        self.bar_canvas.place(x=NAME_COL_WIDTH + ROW_PADDING_X, rely=0.5, anchor="w")
        self.lamp_canvas = tk.Canvas(self, width=self.LAMP_SIZE + 8, height=self.LAMP_SIZE + 8,
                                     bg=bg, highlightthickness=0)
        self.lamp_canvas.place(x=WINDOW_WIDTH - ROW_PADDING_X, rely=0.5, anchor="e")

    def _on_terminal(self) -> None:
        if not self._open_terminal_fn(self._attach_cmd):
            self.term_chip.config(text="失敗")
            self.after(1500, lambda: self.term_chip.config(text="端末"))

    def update_bg(self, bg: str) -> None:
        """最近アクティブ順の並べ替えで行の位置(縞模様)が変わったときに背景色を追従させる。"""
        if bg == self._bg:
            return
        self._bg = bg
        for w in (self, self.name_label, self.cwd_label, self.uuid_chip, self.hide_chip,
                  self.term_chip, self.bar_canvas, self.lamp_canvas):
            w.config(bg=bg)

    def update_data(self, info: SessionInfo, revealed: bool) -> None:
        self._session_id = info.session_id
        self._attach_cmd = info.attach_cmd
        self._working = info.working
        self.name_label.config(text=info.name)
        self.cwd_label.config(text=info.cwd)
        self.uuid_chip.config(text=info.session_id if revealed else "UUID")
        self._draw_bar(info)
        self._draw_lamp()
        self._layout(info)

    def _layout(self, info: SessionInfo) -> None:
        """名前の右にチップを並べ、列幅に収まらなければ次の行へ折り返す(PWAと同じ挙動)。"""
        name_w = self._name_font.measure(info.name)
        chips = [self.uuid_chip, self.hide_chip]
        if info.attach_cmd:
            chips.append(self.term_chip)
        else:
            self.term_chip.place_forget()
        max_x = NAME_COL_WIDTH - 4
        x, y = ROW_PADDING_X + name_w + CHIP_GAP * 2, ROW_NAME_Y
        wrapped = False
        self.name_label.place(x=ROW_PADDING_X, y=ROW_NAME_Y, width=name_w + 4, height=NAME_LINE_HEIGHT)
        for chip in chips:
            w = chip.winfo_reqwidth()
            if x + w > max_x and not wrapped:
                wrapped, x, y = True, ROW_PADDING_X, ROW_NAME_Y + NAME_LINE_HEIGHT + 2
            elif x + w > max_x:
                x, y = ROW_PADDING_X, y + CHIP_LINE_HEIGHT
            chip.place(x=x, y=y, height=CHIP_LINE_HEIGHT - 2)
            x += w + CHIP_GAP
        last_y = y if wrapped else ROW_NAME_Y
        cwd_y = last_y + (CHIP_LINE_HEIGHT if wrapped else NAME_LINE_HEIGHT) + 2
        self.cwd_label.place(x=ROW_PADDING_X, y=cwd_y, width=NAME_COL_WIDTH - ROW_PADDING_X, height=14)
        self.config(height=cwd_y + 14 + ROW_PADDING_Y)

    def _draw_bar(self, info: SessionInfo) -> None:
        c = self.bar_canvas
        c.delete("all")
        mid = 18
        track_y0, track_y1 = mid - 4, mid + 4
        c.create_rectangle(0, track_y0, BAR_TRACK_WIDTH, track_y1, fill=COLOR_TRACK, outline="")
        fill_w = int(BAR_TRACK_WIDTH * min(info.context_pct, 100.0) / 100.0)
        if fill_w > 0:
            c.create_rectangle(0, track_y0, fill_w, track_y1, fill=COLOR_ACCENT, outline="")
        label = (f"{info.context_pct:.0f}% "
                 f"({format_tokens(info.context_tokens)}/{format_tokens(info.context_limit)})")
        c.create_text(BAR_TRACK_WIDTH + 8, mid + 7, text=label, fill=COLOR_TEXT, anchor="w",
                      font=("Segoe UI", 8))
        if info.model:
            c.create_text(BAR_TRACK_WIDTH + 8, mid - 8, text=info.model, fill=COLOR_TEXT_DIM,
                          anchor="w", font=("Segoe UI", 7))

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
        self._hidden: set[str] = load_hidden()
        self._revealed: set[str] = set()
        self._last_sessions: list[SessionInfo] = []
        self._tray_key: tuple = ()

        root.overrideredirect(True)
        root.configure(bg=COLOR_BG, highlightthickness=1, highlightbackground=COLOR_BORDER)
        self._topmost = load_topmost()
        root.attributes("-topmost", self._topmost)
        root.geometry(f"{WINDOW_WIDTH}x60+80+80")

        self._build_titlebar()
        self._kickoff_syncing = False
        self._kickoff_result: tuple[bool, bool] | None = None
        if self._remote is not None:
            self._build_kickoff_panel()
        self._build_header()
        self.body = tk.Frame(root, bg=COLOR_BG)
        self.body.pack(fill="x")

        self.empty_label = tk.Label(
            self.body, text="アクティブなセッションはありません",
            bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=("Segoe UI", 9),
        )
        self.tray = tk.Frame(root, bg=COLOR_BG, highlightthickness=0)
        self.tray_inner = tk.Frame(self.tray, bg=COLOR_BG)

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

        refresh_btn = tk.Label(
            bar, text="↻", bg=COLOR_TITLEBAR, fg=COLOR_TEXT_DIM,
            font=("Segoe UI", 10), cursor="hand2",
        )
        refresh_btn.pack(side="right", padx=4)
        refresh_btn.bind("<Button-1>", lambda e: self.refresh())

        # ウィンドウドラッグ
        for widget in (bar,):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._on_move)

    def _toggle_topmost(self) -> None:
        """最前面固定のオン/オフ。枠なしウィンドウはタスクバーに出ないため、切り替え手段を残す。"""
        self._topmost = not self._topmost
        self.root.attributes("-topmost", self._topmost)
        self._pin_btn.config(fg=COLOR_ACCENT if self._topmost else COLOR_TEXT_DIM)
        save_topmost(self._topmost)

    def _build_kickoff_panel(self) -> None:
        """毎朝4時(JST)のHello送信のカード(PWAと同じ: タイトル+状態3行+スイッチ)。"""
        outer = tk.Frame(self.root, bg=COLOR_BG)
        outer.pack(fill="x", padx=12, pady=(12, 4))
        card = tk.Frame(outer, bg=COLOR_BG, highlightthickness=1, highlightbackground=COLOR_BORDER)
        card.pack(fill="x")
        tk.Label(card, text="毎朝4時(JST) Hello送信", bg=COLOR_TITLEBAR, fg=COLOR_TEXT,
                 font=("Segoe UI", 10, "bold"), anchor="w", padx=14, pady=8).pack(fill="x")
        body = tk.Frame(card, bg=COLOR_BG)
        body.pack(fill="x", padx=14, pady=10)
        self._kickoff_switch = ToggleSwitch(body, COLOR_BG, self._on_kickoff_toggle)
        self._kickoff_switch.pack(side="right")
        info = tk.Frame(body, bg=COLOR_BG)
        info.pack(side="left", fill="x", expand=True)
        font = ("Segoe UI", 9)
        self._kickoff_status = tk.Label(info, text="読み込み中...", bg=COLOR_BG, fg=COLOR_TEXT_DIM,
                                        font=font, anchor="w")
        self._kickoff_status.pack(fill="x")
        self._kickoff_next = tk.Label(info, text="", bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=font, anchor="w")
        self._kickoff_next.pack(fill="x")
        last = tk.Frame(info, bg=COLOR_BG)
        last.pack(fill="x")
        self._kickoff_last = tk.Label(last, text="", bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=font, anchor="w")
        self._kickoff_last.pack(side="left")
        self._kickoff_result_label = tk.Label(last, text="", bg=COLOR_BG, fg=COLOR_GREEN, font=font)
        self._kickoff_result_label.pack(side="left", padx=(4, 0))

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
        self._kickoff_status.config(text=f"状態: {'有効' if state.get('enabled') else '無効'}")
        self._kickoff_next.config(text=f"次回実行(予定): {state.get('next_run_local') or '-'}")
        self._kickoff_last.config(text=f"前回実行: {format_kickoff_time(state.get('last_run'))}")
        self._kickoff_result_label.config(
            text=KICKOFF_STATUS_LABELS.get(status, status or "-"),
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

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=COLOR_BG, height=HEADER_HEIGHT)
        header.pack(fill="x")
        header.pack_propagate(False)
        font = ("Segoe UI", 8)
        tk.Label(header, text="SESSION", bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=font).place(
            x=ROW_PADDING_X, y=8)
        tk.Label(header, text="CONTEXT USED", bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=font).place(
            x=NAME_COL_WIDTH + ROW_PADDING_X, y=8)
        tk.Label(header, text="STATUS", bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=font).place(
            x=WINDOW_WIDTH - ROW_PADDING_X, y=8, anchor="ne")

    def _start_move(self, event: tk.Event) -> None:
        self._drag_x, self._drag_y = event.x, event.y

    def _on_move(self, event: tk.Event) -> None:
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    # -- 非表示・UUID表示(PWAと同じ。非表示は保存して次回起動時にも保持) ----------

    def _hide_session(self, session_id: str) -> None:
        self._hidden.add(session_id)
        save_hidden(self._hidden)
        self._render(self._last_sessions, self._last_error)

    def _unhide_session(self, session_id: str) -> None:
        self._hidden.discard(session_id)
        save_hidden(self._hidden)
        self._render(self._last_sessions, self._last_error)

    def _unhide_all(self) -> None:
        self._hidden.clear()
        save_hidden(self._hidden)
        self._render(self._last_sessions, self._last_error)

    def _toggle_uuid(self, session_id: str) -> None:
        self._revealed.symmetric_difference_update({session_id})
        self._render(self._last_sessions, self._last_error)

    def _render_tray(self, hidden_sessions: list[SessionInfo]) -> None:
        key = tuple(s.session_id for s in hidden_sessions)
        if key == self._tray_key:
            return
        self._tray_key = key
        self.tray.pack_forget()
        for w in self.tray_inner.winfo_children():
            w.destroy()
        if not hidden_sessions:
            return
        self.tray.pack(fill="x", padx=ROW_PADDING_X, pady=(6, 8))
        self.tray_inner.pack(fill="x")
        head = tk.Frame(self.tray_inner, bg=COLOR_BG)
        head.pack(fill="x")
        tk.Label(head, text=f"非表示中 ({len(hidden_sessions)})", bg=COLOR_BG, fg=COLOR_TEXT_DIM,
                 font=("Segoe UI", 8)).pack(side="left")
        Chip(head, "すべて戻す", self._unhide_all, COLOR_BG).pack(side="right")
        chips = tk.Frame(self.tray_inner, bg=COLOR_BG)
        chips.pack(fill="x", pady=(4, 0))
        for s in hidden_sessions:
            Chip(chips, f"{s.name}  ✕", lambda sid=s.session_id: self._unhide_session(sid),
                 COLOR_TRACK).pack(side="left", padx=(0, 4), pady=2)

    # -- データ更新 -----------------------------------------------------

    def refresh(self) -> None:
        if self._remote is not None:
            sessions, error = self._remote.get()
        else:
            sessions, error = get_sessions(), None
        self._update_kickoff_panel()
        self._render(sessions, error)
        self.root.after(POLL_INTERVAL_MS, self.refresh)

    def _render(self, sessions: list[SessionInfo], error: str | None) -> None:
        self._last_sessions, self._last_error = sessions, error
        visible = [s for s in sessions if s.session_id not in self._hidden]
        if error:
            empty_text, empty_fg = error, COLOR_ERROR
        elif not sessions:
            empty_text, empty_fg = "アクティブなセッションはありません", COLOR_TEXT_DIM
        else:
            empty_text, empty_fg = "表示中のセッションはありません(下の「非表示中」欄から戻せます)", COLOR_TEXT_DIM
        self.empty_label.config(text=empty_text, fg=empty_fg)
        self._render_tray([s for s in sessions if s.session_id in self._hidden])

        seen_ids = {s.session_id for s in visible}
        # 消えた(または非表示にした)セッションの行を削除
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
                                 self._hide_session, self._toggle_uuid)
                self.rows[info.session_id] = row
            row.pack(fill="x")
            row.update_bg(row_bg)
            row.update_data(info, info.session_id in self._revealed)

        self.root.update_idletasks()
        self.root.geometry(f"{WINDOW_WIDTH}x{self.root.winfo_reqheight()}")

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
