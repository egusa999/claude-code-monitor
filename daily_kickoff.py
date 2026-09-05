#!/usr/bin/env python3
"""
毎朝4時(JST)に Claude Code へ "Hello" プロンプトを送り、新規セッションを
1つ開始するスクリプト。

- `claude -p`(print/非対話モード)で実行する。応答が返り次第プロセスは終了する
  ため、Claude Code Monitor のアクティブセッション一覧や `claude agents` に
  古いセッションが溜まり続けることはない(`--bg` は使わない)。
  会話自体は通常のセッションと同様に履歴へ保存される。
- systemd タイマー (claude-daily-kickoff.timer) から実行される想定。
- 有効/無効は kickoff_state.json の "enabled" フラグで制御する。
  このフラグは同じフォルダの claude_monitor_web.py (Claude Code Monitor Web版)の
  画面からトグルできる(このスクリプトを --set-enabled true|false で呼び出すだけ)。
- 実行結果(成功/スキップ/エラー)は kickoff_state.json に記録し、
  Monitor 側の「稼働状態」表示に使う。

依存: 標準ライブラリのみ。claude CLI が PATH 上にあること。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "kickoff_state.json"
LOG_FILE = SCRIPT_DIR / "kickoff.log"
SESSION_CWD = SCRIPT_DIR
SESSION_NAME = "daily-4am-hello"
PROMPT_TEXT = "Hello"

DEFAULT_STATE = {
    "enabled": True,
    "last_run": None,       # ISO8601 (UTC)
    "last_status": None,    # "success" | "skipped" | "error"
    "last_message": None,
}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return {**DEFAULT_STATE, **data}
        except (OSError, json.JSONDecodeError):
            pass
    return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def set_enabled(enabled: bool) -> dict:
    state = load_state()
    state["enabled"] = enabled
    save_state(state)
    _log(f"enabled -> {enabled}")
    return state


def _log(message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def run_kickoff() -> int:
    state = load_state()
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not state.get("enabled", True):
        state["last_run"] = now_iso
        state["last_status"] = "skipped"
        state["last_message"] = "無効化されていたためスキップしました"
        save_state(state)
        _log("skipped (disabled)")
        return 0

    # -p (print) で実行: 応答が返り次第プロセスは終了する。--bg は使わない
    # (--bg は明示的に stop/rm するまでバックグラウンドで動き続け、Monitor の
    #  アクティブセッション一覧や `claude agents` に日々溜まっていってしまうため)。
    cmd = ["claude", "-p", "-n", SESSION_NAME, PROMPT_TEXT]
    try:
        result = subprocess.run(
            cmd, cwd=str(SESSION_CWD), capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        state["last_run"] = now_iso
        state["last_status"] = "error"
        state["last_message"] = str(exc)
        save_state(state)
        _log(f"error: {exc}")
        return 1

    state["last_run"] = now_iso
    if result.returncode == 0:
        state["last_status"] = "success"
        state["last_message"] = result.stdout.strip()[:500]
        _log(f"success: {state['last_message']}")
    else:
        state["last_status"] = "error"
        state["last_message"] = (result.stderr or result.stdout).strip()[:500]
        _log(f"error (exit {result.returncode}): {state['last_message']}")

    save_state(state)
    return result.returncode


def main() -> None:
    args = sys.argv[1:]

    if args[:1] == ["--set-enabled"]:
        if len(args) < 2 or args[1] not in ("true", "false"):
            print("usage: daily_kickoff.py --set-enabled true|false", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(set_enabled(args[1] == "true"), ensure_ascii=False))
        return

    if args[:1] == ["--status"]:
        print(json.dumps(load_state(), ensure_ascii=False))
        return

    sys.exit(run_kickoff())


if __name__ == "__main__":
    main()
