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
LOG_PATH = Path.home() / ".claude_monitor" / "app.log"  # pyw起動だとコンソールが無いため、エラーはここに残す
# リモートで実行を許す接続コマンド。サーバー応答由来の文字列をsshに渡すため、形式を厳格に限定する。
ATTACH_CMD_PATTERN = re.compile(r"^(tmux attach -t =|claude attach )[A-Za-z0-9._-]+$")
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
        if not ATTACH_CMD_PATTERN.match(attach_cmd):
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

    def __init__(self, base_url: str, token: str):
        self._url = base_url.rstrip("/") + "/api/sessions"
        self._token = token
        self._lock = threading.Lock()
        self._sessions: list[SessionInfo] = []
        self._error: str | None = "接続中..."
        threading.Thread(target=self._loop, daemon=True).start()

    def _fetch(self) -> list[SessionInfo]:
        req = urllib.request.Request(
            self._url, headers={"Authorization": f"Bearer {self._token}"})
        with urllib.request.urlopen(req, timeout=REMOTE_FETCH_TIMEOUT_SEC) as res:
            payload = json.load(res)
        names = {f.name for f in fields(SessionInfo)} - {"agents"}
        return [SessionInfo(**{k: v for k, v in item.items() if k in names})
                for item in payload["sessions"]]

    def _loop(self) -> None:
        while True:
            try:
                sessions, error = self._fetch(), None
            except urllib.error.HTTPError as e:
                sessions, error = None, "トークンが無効です" if e.code == 401 else f"HTTP {e.code}"
            except (OSError, ValueError, KeyError, TypeError):
                sessions, error = None, "サーバーに接続できません"
            with self._lock:
                if sessions is not None:
                    self._sessions = sessions
                self._error = error
            time.sleep(POLL_INTERVAL_MS / 1000.0)

    def get(self) -> tuple[list[SessionInfo], str | None]:
        """(直近に取得できた一覧, 接続エラー文言またはNone)。"""
        with self._lock:
            return list(self._sessions), self._error


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class SessionRow(tk.Frame):
    """1セッション分の行ウィジェット。"""

    ROW_HEIGHT = 40
    NAME_WIDTH = 160
    BAR_WIDTH = 220
    BAR_TRACK_WIDTH = 70
    LAMP_SIZE = 16
    TERM_BUTTON_WIDTH = 34

    def __init__(self, master: tk.Widget, bg: str, open_terminal_fn=None):
        super().__init__(master, bg=bg, height=self.ROW_HEIGHT)
        self._open_terminal_fn = open_terminal_fn or open_terminal
        self.pack_propagate(False)
        self._bg = bg
        self._blink_on = True

        self.name_label = tk.Label(
            self, text="", bg=bg, fg=COLOR_TEXT,
            font=("Segoe UI", 10, "bold"), anchor="w",
        )
        self.name_label.place(x=14, y=0, width=self.NAME_WIDTH, height=self.ROW_HEIGHT)

        bar_x = 14 + self.NAME_WIDTH + 10
        self.bar_canvas = tk.Canvas(
            self, width=self.BAR_WIDTH, height=self.ROW_HEIGHT,
            bg=bg, highlightthickness=0,
        )
        self.bar_canvas.place(x=bar_x, y=0)

        lamp_x = bar_x + self.BAR_WIDTH + 24
        self.lamp_canvas = tk.Canvas(
            self, width=self.LAMP_SIZE + 8, height=self.ROW_HEIGHT,
            bg=bg, highlightthickness=0,
        )
        self.lamp_canvas.place(x=lamp_x, y=0)

        self._attach_cmd = ""
        self.term_button = tk.Button(
            self, text="端末", command=self._on_terminal, bd=0, padx=4, pady=0,
            bg=COLOR_TRACK, fg=COLOR_TEXT_DIM, activebackground=COLOR_TRACK,
            activeforeground=COLOR_TEXT, font=("Segoe UI", 8), cursor="hand2",
        )
        self._term_button_x = lamp_x + self.LAMP_SIZE + 8 + 6

    def _on_terminal(self) -> None:
        if not self._open_terminal_fn(self._attach_cmd):
            self.term_button.config(text="失敗")
            self.after(1500, lambda: self.term_button.config(text="端末"))

    def update_bg(self, bg: str) -> None:
        """最近アクティブ順の並べ替えで行の位置(縞模様)が変わったときに、
        その行と子ウィジェットの背景色を追従させる。"""
        if bg == self._bg:
            return
        self._bg = bg
        self.config(bg=bg)
        self.name_label.config(bg=bg)
        self.bar_canvas.config(bg=bg)
        self.lamp_canvas.config(bg=bg)

    def update_data(self, info: SessionInfo) -> None:
        self.name_label.config(text=info.name)
        self._draw_bar(info)
        self._working = info.working
        self._draw_lamp()
        self._attach_cmd = info.attach_cmd
        if info.attach_cmd:
            self.term_button.place(x=self._term_button_x, y=(self.ROW_HEIGHT - 20) // 2,
                                   width=self.TERM_BUTTON_WIDTH, height=20)
        else:
            self.term_button.place_forget()

    def _draw_bar(self, info: SessionInfo) -> None:
        c = self.bar_canvas
        c.delete("all")
        track_y0, track_y1 = 16, 24
        track_w = self.BAR_TRACK_WIDTH
        c.create_rectangle(0, track_y0, track_w, track_y1,
                            fill=COLOR_TRACK, outline="")
        fill_w = int(track_w * min(info.context_pct, 100.0) / 100.0)
        if fill_w > 0:
            c.create_rectangle(0, track_y0, fill_w, track_y1,
                                fill=COLOR_ACCENT, outline="")

        label = (
            f"{info.context_pct:.0f}% "
            f"({format_tokens(info.context_tokens)}/{format_tokens(info.context_limit)})"
        )
        c.create_text(track_w + 10, (track_y0 + track_y1) // 2 + 6,
                       text=label, fill=COLOR_TEXT, anchor="w",
                       font=("Segoe UI", 9, "bold"))
        if info.model:
            c.create_text(track_w + 10, (track_y0 + track_y1) // 2 - 8,
                           text=info.model, fill=COLOR_TEXT_DIM, anchor="w",
                           font=("Segoe UI", 8))

    def _draw_lamp(self) -> None:
        c = self.lamp_canvas
        c.delete("all")
        cx, cy, r = (self.LAMP_SIZE + 8) // 2, self.ROW_HEIGHT // 2, self.LAMP_SIZE // 2
        if self._working:
            color = COLOR_RED_ON if self._blink_on else COLOR_RED_OFF
        else:
            color = COLOR_GREEN
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline="")

    def toggle_blink(self) -> None:
        self._blink_on = not self._blink_on
        if getattr(self, "_working", False):
            self._draw_lamp()


class ClaudeMonitorApp:
    def __init__(self, root: tk.Tk, remote: RemoteSessionSource | None = None,
                 ssh_host: str | None = None):
        self.root = root
        self._remote = remote
        self._open_terminal_fn = lambda cmd: open_terminal(cmd, ssh_host)
        self.rows: dict[str, SessionRow] = {}

        root.overrideredirect(True)
        root.configure(bg=COLOR_BG)
        root.attributes("-topmost", True)
        self.window_width = (
            14 + SessionRow.NAME_WIDTH + 10 + SessionRow.BAR_WIDTH + 24
            + SessionRow.LAMP_SIZE + 8 + 6 + SessionRow.TERM_BUTTON_WIDTH + 14
        )
        root.geometry(f"{self.window_width}x60+80+80")

        self._build_titlebar()
        self._build_header()
        self.body = tk.Frame(root, bg=COLOR_BG)
        self.body.pack(fill="both", expand=True)

        self.empty_label = tk.Label(
            self.body, text="アクティブなセッションはありません",
            bg=COLOR_BG, fg=COLOR_TEXT_DIM, font=("Segoe UI", 9),
        )

        self.refresh()
        self._blink()

    # -- UI構築 -------------------------------------------------------

    def _build_titlebar(self) -> None:
        bar = tk.Frame(self.root, bg=COLOR_TITLEBAR, height=34)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        tk.Label(
            bar, text="● Claude Code Monitor", bg=COLOR_TITLEBAR,
            fg=COLOR_TEXT, font=("Segoe UI", 10, "bold"),
        ).pack(side="left", padx=12)

        close_btn = tk.Label(
            bar, text="✕", bg=COLOR_TITLEBAR, fg=COLOR_TEXT_DIM,
            font=("Segoe UI", 10), cursor="hand2",
        )
        close_btn.pack(side="right", padx=12)
        close_btn.bind("<Button-1>", lambda e: self.root.destroy())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=COLOR_RED_ON))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=COLOR_TEXT_DIM))

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

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=COLOR_BG, height=24)
        header.pack(fill="x")
        header.pack_propagate(False)
        bar_x = 14 + SessionRow.NAME_WIDTH + 10
        lamp_x = bar_x + SessionRow.BAR_WIDTH + 24
        tk.Label(header, text="SESSION", bg=COLOR_BG, fg=COLOR_TEXT_DIM,
                  font=("Segoe UI", 8)).place(x=14, y=4)
        tk.Label(header, text="CONTEXT USED", bg=COLOR_BG, fg=COLOR_TEXT_DIM,
                  font=("Segoe UI", 8)).place(x=bar_x, y=4)
        tk.Label(header, text="STATUS", bg=COLOR_BG, fg=COLOR_TEXT_DIM,
                  font=("Segoe UI", 8)).place(x=lamp_x - 20, y=4)

    def _start_move(self, event: tk.Event) -> None:
        self._drag_x, self._drag_y = event.x, event.y

    def _on_move(self, event: tk.Event) -> None:
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    # -- データ更新 -----------------------------------------------------

    def refresh(self) -> None:
        if self._remote is not None:
            sessions, error = self._remote.get()
        else:
            sessions, error = get_sessions(), None
        self.empty_label.config(
            text=error or "アクティブなセッションはありません",
            fg=COLOR_ERROR if error else COLOR_TEXT_DIM)
        seen_ids = {s.session_id for s in sessions}

        # 消えたセッションの行を削除
        for sid in list(self.rows.keys()):
            if sid not in seen_ids:
                self.rows.pop(sid).destroy()

        if not sessions or error:
            self.empty_label.pack(fill="x", pady=10)
        else:
            self.empty_label.pack_forget()

        # get_sessions()は最近アクティブ順に並んでいるが、pack()は既存ウィジェットを
        # 再度呼んでも並び順を変えないため、いったん全行を配置解除してから
        # 新しい順序で詰め直す(rows dict自体は使い回し、再生成はしない)。
        for row in self.rows.values():
            row.pack_forget()

        for idx, info in enumerate(sessions):
            row_bg = COLOR_ROW_BG if idx % 2 == 0 else COLOR_ROW_ALT_BG
            row = self.rows.get(info.session_id)
            if row is None:
                row = SessionRow(self.body, row_bg, self._open_terminal_fn)
                self.rows[info.session_id] = row
            row.pack(fill="x")
            row.update_bg(row_bg)
            row.update_data(info)

        show_label = not sessions or bool(error)
        new_height = (34 + 24 + len(sessions) * SessionRow.ROW_HEIGHT
                      + (SessionRow.ROW_HEIGHT if show_label and not sessions else 0)
                      + (20 if show_label else 0))
        self.root.geometry(f"{self.window_width}x{new_height}")

        self.root.after(POLL_INTERVAL_MS, self.refresh)

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
    parser.add_argument("--server", help="リモートモード: Web版のURL(例 https://claudecode.example.ts.net:8765)。"
                                         "省略時はこのホストのセッションを直接読む")
    parser.add_argument("--token-file", help=f"Bearerトークンを書いたファイル(省略時は環境変数{TOKEN_ENV_VAR})")
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST, help="「端末」ボタンで接続するSSH先")
    args = parser.parse_args()

    remote = None
    if args.server:
        token = _read_token(args)
        if not token:
            raise StartupError(f"--server にはトークンが必要です(--token-file か環境変数{TOKEN_ENV_VAR})")
        logging.info("remote mode: server=%s ssh_host=%s", args.server, args.ssh_host)
        remote = RemoteSessionSource(args.server, token)
    root = tk.Tk()
    ClaudeMonitorApp(root, remote=remote, ssh_host=args.ssh_host if remote else None)
    root.mainloop()


if __name__ == "__main__":
    main()
