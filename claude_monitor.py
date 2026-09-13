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

import json
import os
import time
import tkinter as tk
from dataclasses import dataclass
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
WORKING_THRESHOLD_SEC = 6.0         # この秒数以内にトランスクリプト更新があれば「作業中」
AWAITING_PROMPT_MAX_AGE_SEC = 180.0  # 新規プロンプト/ツール結果を「考え中」とみなす最大経過時間
POLL_INTERVAL_MS = 1500             # セッション一覧・使用量の再取得間隔
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
class SessionInfo:
    pid: int
    session_id: str
    name: str
    cwd: str
    started_at: float
    context_pct: float = 0.0
    context_tokens: int = 0
    working: bool = False
    last_active: float = 0.0


# セッションごとに直近判明したコンテキストトークン数を保持するキャッシュ。
# SessionInfoは毎ポーリングで作り直されるため、この値はモジュールレベルで持つ。
_last_known_tokens: dict[str, int] = {}


def _is_pid_alive(pid: int) -> bool:
    if os.name == "posix":
        return Path(f"/proc/{pid}").exists()
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _load_active_sessions() -> list[SessionInfo]:
    """~/.claude/sessions/*.json から、プロセスが生存中のセッションのみ返す。"""
    sessions: dict[str, SessionInfo] = {}
    if not SESSIONS_DIR.exists():
        return []

    for meta_file in SESSIONS_DIR.glob("*.json"):
        try:
            pid = int(meta_file.stem)
        except ValueError:
            continue
        if not _is_pid_alive(pid):
            continue
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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
        )
        # 同一 sessionId の重複(古い meta ファイル残存等)は新しい方を優先
        existing = sessions.get(session_id)
        if existing is None or started_at >= existing.started_at:
            sessions[session_id] = info

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
            except json.JSONDecodeError:
                continue

        if tail_size >= size or tail_size >= TRANSCRIPT_MAX_SCAN_BYTES:
            return
        tail_size = min(tail_size * 4, TRANSCRIPT_MAX_SCAN_BYTES, size)


def _read_last_usage(transcript: Path) -> dict | None:
    """トランスクリプト末尾から、メインチェーンの最後の usage オブジェクトを取得する。

    Task(サブエージェント)呼び出しは isSidechain: true として同じファイルに
    記録され、メインの会話とは無関係な(別のコンテキストウィンドウの)トークン数
    を持つため、必ず除外する。これを除外しないと /context の表示と一致しなくなる。
    """
    for obj in _iter_recent_entries(transcript):
        if obj.get("isSidechain"):
            continue
        message = obj.get("message") or {}
        if message.get("model") == "<synthetic>":
            # 実際のAPI呼び出しを伴わない合成メッセージ(中断時の後処理等)で、
            # usageが全項目0のダミー値になっている。実際のコンテキスト量とは
            # 無関係なので無視し、その前の本物のassistantメッセージを見る。
            continue
        usage = message.get("usage")
        if usage:
            return usage
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
        info.working = False
        info.last_active = info.started_at
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

    usage = _read_last_usage(transcript)
    if usage:
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

    info.context_tokens = tokens
    info.context_pct = min(100.0, tokens / CONTEXT_TOKEN_LIMIT * 100.0)


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

    def __init__(self, master: tk.Widget, bg: str):
        super().__init__(master, bg=bg, height=self.ROW_HEIGHT)
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
            f"({format_tokens(info.context_tokens)}/{format_tokens(CONTEXT_TOKEN_LIMIT)})"
        )
        c.create_text(track_w + 10, (track_y0 + track_y1) // 2,
                       text=label, fill=COLOR_TEXT, anchor="w",
                       font=("Segoe UI", 9, "bold"))

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
    def __init__(self, root: tk.Tk):
        self.root = root
        self.rows: dict[str, SessionRow] = {}

        root.overrideredirect(True)
        root.configure(bg=COLOR_BG)
        root.attributes("-topmost", True)
        self.window_width = (
            14 + SessionRow.NAME_WIDTH + 10 + SessionRow.BAR_WIDTH + 24
            + SessionRow.LAMP_SIZE + 8 + 28
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
        sessions = get_sessions()
        seen_ids = {s.session_id for s in sessions}

        # 消えたセッションの行を削除
        for sid in list(self.rows.keys()):
            if sid not in seen_ids:
                self.rows.pop(sid).destroy()

        if not sessions:
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
                row = SessionRow(self.body, row_bg)
                self.rows[info.session_id] = row
            row.pack(fill="x")
            row.update_bg(row_bg)
            row.update_data(info)

        row_count = len(sessions) if sessions else 1
        new_height = 34 + 24 + row_count * SessionRow.ROW_HEIGHT + (0 if sessions else 20)
        self.root.geometry(f"{self.window_width}x{new_height}")

        self.root.after(POLL_INTERVAL_MS, self.refresh)

    def _blink(self) -> None:
        for row in self.rows.values():
            row.toggle_blink()
        self.root.after(BLINK_INTERVAL_MS, self._blink)


def main() -> None:
    root = tk.Tk()
    ClaudeMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
