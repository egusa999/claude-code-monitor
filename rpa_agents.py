"""RPA実機エージェントの稼働一覧(PWAの「agent」タブ用、読み取り専用)。

判定・行の組み立ては外部のRPAプロジェクト(`agent_monitor_gui.collect_rows` と
`agent_client.list_agents`)をそのまま import して使い、ここでは二重実装しない。
RPAプロジェクトのフォルダはサーバー起動時に `--rpa-agent-dir`(または環境変数
`CLAUDE_MONITOR_RPA_AGENT_DIR`)で渡す。未設定なら機能ごと無効(`enabled: false`)。

PHI対策: 状態ファイルの `foreground` 欄は「クラス名 | 窓タイトル」で、タイトルに
URL・患者IDが入りうる。ここではクラス名だけを取り出し、さらに英数字等の安全な文字だけで
できている場合に限って返す(それ以外は "-")。タイトルは絶対に返さない。
"""
from __future__ import annotations

import concurrent.futures
import importlib
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

ENV_AGENT_DIR = "CLAUDE_MONITOR_RPA_AGENT_DIR"
GUI_MODULE = "agent_monitor_gui"
READ_TIMEOUT_SEC = 10  # 共有フォルダの読み取りが詰まってもAPIを長く待たせない
FOREGROUND_SEPARATOR = " | "
FOREGROUND_CLASS_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
NO_VALUE = "-"
DEFAULT_FRESH_SEC = 120  # 取り込めなかったときの表示用(RPA側の STATUS_FRESH_SEC と同じ値)

_lock = threading.Lock()
_agent_dir: Path | None = None
_gui = None  # import済みの agent_monitor_gui モジュール
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="rpa-agents")
_pending: concurrent.futures.Future | None = None
_last_payload: dict | None = None


def configure(agent_dir: str | None) -> None:
    """RPAプロジェクトのフォルダ(agent_monitor_gui.py がある所)を設定する。空なら無効。"""
    global _agent_dir, _gui, _last_payload
    with _lock:
        _agent_dir = Path(agent_dir).expanduser() if agent_dir and agent_dir.strip() else None
        _gui = None
        _last_payload = None
        sys.modules.pop(GUI_MODULE, None)  # 別のフォルダを設定し直したときに古い import を使わない


def is_enabled() -> bool:
    return _agent_dir is not None


def foreground_class(value: str | None) -> str:
    """「クラス名 | タイトル」からクラス名だけを返す。安全な文字だけでなければ "-"。"""
    if not value:
        return NO_VALUE
    cls = value.split(FOREGROUND_SEPARATOR, 1)[0].split("|", 1)[0].strip()
    return cls if FOREGROUND_CLASS_PATTERN.match(cls) else NO_VALUE


def is_stale(updated_at: str | None, now: datetime, fresh_sec: float) -> bool:
    """更新が fresh_sec 秒より古い(または読めない)なら True。時計が進んで未来になる場合は新しい扱い。"""
    try:
        t = datetime.fromisoformat((updated_at or "").strip())
    except ValueError:
        return True
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return (now - t).total_seconds() >= fresh_sec


def _load_gui():
    global _gui
    if _gui is None:
        if _agent_dir is None or not (_agent_dir / f"{GUI_MODULE}.py").is_file():
            raise RuntimeError("RPAエージェントのフォルダが見つかりません")
        path = str(_agent_dir)
        if path not in sys.path:
            sys.path.append(path)  # 末尾に足す(このサーバー自身のモジュールを上書きしない)
        _gui = importlib.import_module(GUI_MODULE)
    return _gui


def _collect(now: datetime) -> dict:
    gui = _load_gui()
    fresh_sec = getattr(gui.AC, "STATUS_FRESH_SEC", DEFAULT_FRESH_SEC)
    extra_by_pc: dict[str, dict] = {}
    for info in gui.agent_client.list_agents(gui.AGENT_HOME, now):
        extra = gui._read_extra_status(info.agent_dir)
        extra_by_pc[info.pc_name] = {
            "stale": is_stale(info.updated_at, now, fresh_sec),
            "foreground": foreground_class(extra.get("foreground")),
        }
    rows = []
    for values, tag in gui.collect_rows(now):
        pc = values.get("pc", "")
        extra = extra_by_pc.get(pc, {"stale": True, "foreground": NO_VALUE})
        row = {key: str(values.get(key, NO_VALUE)) for key, _title, _width in gui.COLUMNS}
        row.update(tag=tag, stale=extra["stale"], foreground=extra["foreground"])
        rows.append(row)
    columns = [{"key": key, "title": title} for key, title, _width in gui.COLUMNS]
    return {"enabled": True, "fresh_sec": fresh_sec, "columns": columns, "rows": rows,
            "checked_at": now.isoformat(timespec="seconds")}


def fetch_agents(now: datetime | None = None) -> dict:
    """APIの応答を作る。読み取りが READ_TIMEOUT_SEC を超えたら前回値+エラーを返す(処理自体は裏で続く)。"""
    global _pending, _last_payload
    if not is_enabled():
        return {"enabled": False}
    with _lock:
        if _pending is None or _pending.done():
            _pending = _executor.submit(_collect, now or datetime.now())
        future = _pending
    try:
        payload = future.result(timeout=READ_TIMEOUT_SEC)
    except concurrent.futures.TimeoutError:
        return _with_error("共有フォルダの読み取りが遅れています(前回値)")
    except Exception as e:  # 外部プロジェクトのimport失敗・状態ファイル破損など
        return _with_error(f"エージェント一覧を読めません: {type(e).__name__}: {e}")
    with _lock:
        _last_payload = payload
    return payload


def _with_error(message: str) -> dict:
    with _lock:
        base = dict(_last_payload) if _last_payload else {"enabled": True, "rows": [], "columns": []}
    base["error"] = message
    return base
