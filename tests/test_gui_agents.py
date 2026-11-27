"""GUI(リモートモード)の「agent」タブ(RPA実機エージェントの稼働一覧)のテスト。
表示の部品はXvfbで: xvfb-run -a python3 -m unittest tests.test_gui_agents"""
from __future__ import annotations

import gc
import json
import os
import threading
import tkinter as tk
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import claude_monitor as cm

ROWS = [
    {"pc": "PC-A", "label": "外来", "state": "稼働中(待機)", "updated": "11/27 20:36(6秒前)", "hope": "ログアウト",
     "sender": "-", "lock": "-", "command": "-", "hours": "終日(時間内)", "foreground": "IEFrame",
     "tag": "idle", "stale": False},
    {"pc": "PC-B", "label": "-", "state": "稼働中(実行中)", "updated": "11/27 20:36(たった今)", "hope": "-",
     "sender": "nurse", "lock": "-", "command": "c1 soap", "hours": "終日(時間内)", "foreground": "-",
     "tag": "busy", "stale": False},
    {"pc": "PC-C", "label": "当直室", "state": "停止", "updated": "11/27 14:43(5時間前)", "hope": "-",
     "sender": "-", "lock": "-", "command": "-", "hours": "命令を受けない", "foreground": "-",
     "tag": "stopped", "stale": True},
]
PAYLOAD = {"enabled": True, "fresh_sec": 120, "rows": ROWS, "checked_at": "2026-11-27T20:36:22"}


class ViewTests(unittest.TestCase):
    def test_row_view(self):
        a, b, c = (cm.agent_row_view(r) for r in ROWS)
        self.assertEqual((a["name"], a["pc"], a["lamp"], a["stale"]), ("外来", "PC-A", cm.COLOR_GREEN, False))
        self.assertEqual([t for t, _ in a["fields"]], ["HOPE", "使用時間", "前面"])
        self.assertEqual((b["name"], b["pc"], b["lamp"]), ("PC-B", "", cm.COLOR_AGENT_BUSY))  # 名前なし→PC名
        self.assertIn(("実行中", "c1 soap"), b["fields"])
        self.assertEqual((c["lamp"], c["stale"]), (cm.COLOR_RED_ON, True))

    def test_row_view_missing_fields(self):
        v = cm.agent_row_view({})
        self.assertEqual((v["name"], v["state"], v["updated"], v["fields"], v["lamp"]),
                         ("-", "-", "-", [], cm.COLOR_TEXT_DIM))

    def test_summary(self):
        self.assertEqual(cm.agent_summary(PAYLOAD), ("稼働 2 / 全 3 台 ・ 更新停止 1 台 ・ 20:36:22 時点", True))
        self.assertEqual(cm.agent_summary({}), ("稼働 0 / 全 0 台", False))
        self.assertEqual(cm.agent_summary({"rows": "broken", "error": "x"}), ("稼働 0 / 全 0 台  x", True))


class FetchAgentsTests(unittest.TestCase):
    """RemoteSessionSource.fetch_agents の応答の扱い(偽サーバー)。"""

    def setUp(self):
        self.status, self.body = 200, PAYLOAD
        test = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps(test.body).encode()
                self.send_response(test.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.src = cm.RemoteSessionSource.__new__(cm.RemoteSessionSource)  # 周期取得スレッドは起動しない
        self.src._lock = threading.Lock()
        self.src.configure(f"http://127.0.0.1:{self.server.server_address[1]}", "t" * 40)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_ok(self):
        self.assertEqual(self.src.fetch_agents()["rows"][0]["pc"], "PC-A")

    def test_old_server_is_disabled(self):
        self.status, self.body = 404, {}
        self.assertEqual(self.src.fetch_agents(), {"enabled": False})

    def test_errors(self):
        self.status, self.body = 401, {"error": "unauthorized"}
        self.assertEqual(self.src.fetch_agents(), {"error": "トークンが無効です"})
        self.status, self.body = 503, {"enabled": True, "rows": [], "error": "読めません"}
        self.assertEqual(self.src.fetch_agents()["error"], "読めません")

    def test_unconfigured_and_unreachable(self):
        self.src.configure("", "")
        self.assertEqual(self.src.fetch_agents(), {"enabled": False})
        self.src.configure("http://127.0.0.1:9", "t" * 40)
        self.assertEqual(self.src.fetch_agents(), {"error": "サーバーに接続できません"})


class FakeRemote:
    def __init__(self):
        self.agents = {"enabled": False}

    def get(self):
        return [], None

    def get_kickoff(self):
        return None

    def fetch_usage(self):
        return {}

    def fetch_agents(self):
        return self.agents


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display (use xvfb-run)")
class AppTabTests(unittest.TestCase):
    def setUp(self):
        self.prefs = {}
        self.saved = []
        for p in (mock.patch.object(cm, "load_ui_prefs", lambda: dict(self.prefs)),
                  mock.patch.object(cm, "save_ui_prefs", lambda prefs: self.saved.append(dict(prefs)))):
            p.start()
            self.addCleanup(p.stop)
        self.remote = FakeRemote()

    def make_app(self):
        self.root = tk.Tk()
        self.app = cm.ClaudeMonitorApp(self.root, remote=self.remote, ssh_host="h", native_frame=False,
                                       win32_chrome=False)
        self.pump()

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()
            del self.app, self.root
        gc.collect()

    def pump(self, times=3):
        for _ in range(times):
            self.root.update()

    def load(self, payload):
        """取得スレッドを待たず、取得済みの値を差し替えて画面を更新する。"""
        self.remote.agents = payload  # 裏の取得スレッドが取り直しても同じ値になるように
        with self.app._agent_source._lock:
            self.app._agent_source._usage = payload
        self.app.refresh()
        self.pump()

    def test_tab_hidden_until_enabled(self):
        self.make_app()
        self.assertFalse(self.app._view_tabs.winfo_ismapped())
        self.load(PAYLOAD)
        self.assertTrue(self.app._view_tabs.winfo_ismapped())
        self.assertTrue(self.app._header.winfo_ismapped())  # 既定はセッション表示
        self.assertEqual(self.app._agent_rows, {})
        self.load({"enabled": False})
        self.assertFalse(self.app._view_tabs.winfo_ismapped())

    def test_switch_shows_rows_and_back(self):
        self.make_app()
        self.load(PAYLOAD)
        self.app.set_view(cm.VIEW_AGENTS)
        self.pump()
        self.assertEqual(self.saved[-1]["view"], cm.VIEW_AGENTS)
        self.assertFalse(self.app._header.winfo_ismapped())
        self.assertEqual(sorted(self.app._agent_rows), ["PC-A", "PC-B", "PC-C"])
        self.assertTrue(all(r.winfo_ismapped() for r in self.app._agent_rows.values()))
        self.assertIn("更新停止 1 台", self.app.agent_summary_label.cget("text"))
        stale = self.app._agent_rows["PC-C"]
        self.assertEqual(stale.updated_label.cget("fg"), cm.COLOR_RED_ON)
        self.assertEqual(stale.cget("bg"), cm.COLOR_AGENT_STALE_BG)
        self.assertEqual(self.app._agent_source.interval_sec, cm.AGENTS_POLL_SEC)
        # 1台減ると行も消える
        self.load({**PAYLOAD, "rows": ROWS[:2]})
        self.assertEqual(sorted(self.app._agent_rows), ["PC-A", "PC-B"])
        self.app.set_view(cm.VIEW_SESSIONS)
        self.pump()
        self.assertTrue(self.app._header.winfo_ismapped())
        self.assertFalse(any(r.winfo_ismapped() for r in self.app._agent_rows.values()))
        self.assertFalse(self.app.agent_summary_label.winfo_ismapped())
        self.assertEqual(self.app._agent_source.interval_sec, cm.AGENTS_IDLE_POLL_SEC)

    def test_saved_view_waits_for_enabled_and_error_keeps_rows(self):
        self.prefs = {"view": cm.VIEW_AGENTS}
        self.make_app()
        self.assertTrue(self.app._header.winfo_ismapped())  # まだ有効と分からないのでセッション表示
        self.load(PAYLOAD)
        self.assertFalse(self.app._header.winfo_ismapped())
        self.load({**PAYLOAD, "error": "共有フォルダの読み取りが遅れています(前回値)"})
        self.assertEqual(len(self.app._agent_rows), 3)
        self.assertEqual(self.app.agent_summary_label.cget("fg"), cm.COLOR_ERROR)

    def test_local_mode_has_no_tab(self):
        with mock.patch.object(cm, "get_sessions", lambda: []), mock.patch.object(cm, "fetch_plan_usage", lambda: {}):
            self.root = tk.Tk()
            self.app = cm.ClaudeMonitorApp(self.root, native_frame=False, win32_chrome=False)
            self.pump()
        self.assertIsNone(self.app._agent_source)
        self.app.set_view(cm.VIEW_AGENTS)
        self.pump()
        self.assertTrue(self.app._header.winfo_ismapped())
        self.assertFalse(self.app._view_tabs.winfo_ismapped())


if __name__ == "__main__":
    unittest.main()
