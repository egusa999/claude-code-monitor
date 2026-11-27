"""PWAの「agent」タブ(RPA実機エージェントの稼働一覧)のテスト。RPA側のモジュールは偽物を一時フォルダに置く。

実データでの確認は環境変数 CLAUDE_MONITOR_RPA_AGENT_DIR を設定したときだけ行う(内部のパスはリポジトリに書かない)。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_monitor_web as web  # noqa: E402
import rpa_agents  # noqa: E402

NOW = datetime(2026, 11, 27, 15, 0, 0)
FAKE_GUI = textwrap.dedent('''
    import time
    from dataclasses import dataclass
    from pathlib import Path
    from types import SimpleNamespace

    AC = SimpleNamespace(STATUS_FRESH_SEC=120)
    AGENT_HOME = Path(__file__).parent
    COLUMNS = [("label", "名前", 1), ("updated", "最終更新", 1), ("state", "状態", 1), ("hope", "HOPE", 1),
               ("sender", "送信元", 1), ("lock", "占有", 1), ("command", "実行中の命令", 1), ("hours", "使用時間", 1),
               ("pc", "PC", 1)]
    AGENTS = []      # テストから (pc, updated_at, foreground) を入れる
    DELAY = 0.0
    FAIL = None

    @dataclass
    class Info:
        pc_name: str
        updated_at: str
        agent_dir: Path

    def _list(agent_home, now=None):
        if FAIL:
            raise OSError(FAIL)
        time.sleep(DELAY)
        return [Info(pc, upd, Path(pc)) for pc, upd, _fg in AGENTS]

    agent_client = SimpleNamespace(list_agents=_list)

    def _read_extra_status(agent_dir):
        for pc, _upd, fg in AGENTS:
            if Path(pc) == agent_dir:
                return {} if fg is None else {"foreground": fg}
        return {}

    def collect_rows(now=None):
        return [({"pc": pc, "label": "L-" + pc, "state": "稼働中(待機)", "updated": upd, "hope": "-",
                  "sender": "-", "lock": "-", "command": "-", "hours": "終日"}, "idle")
                for pc, upd in ((i.pc_name, i.updated_at) for i in _list(None, now))]
''')


class FakeGuiMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        Path(self.tmp.name, rpa_agents.GUI_MODULE + ".py").write_text(FAKE_GUI, encoding="utf-8")
        rpa_agents.configure(self.tmp.name)
        self.gui = rpa_agents._load_gui()

    def tearDown(self):
        self.gui.DELAY = 0.0
        rpa_agents.configure(None)
        if self.tmp.name in sys.path:
            sys.path.remove(self.tmp.name)
        self.tmp.cleanup()


class ForegroundClassTests(unittest.TestCase):
    def test_title_and_url_never_returned(self):
        self.assertEqual(rpa_agents.foreground_class("IEFrame | http://host/?view&ris_pat_id=12345678"), "IEFrame")
        self.assertEqual(rpa_agents.foreground_class("Windows.UI.Core.CoreWindow | スタート"), "Windows.UI.Core.CoreWindow")
        self.assertEqual(rpa_agents.foreground_class("ThunderRT6FormDC|利用者認証"), "ThunderRT6FormDC")

    def test_unsafe_or_empty_is_dash(self):
        for value in (None, "", " | title", "クラス | x", "a b | x", "x" * 65, "http://h/?id=1"):
            self.assertEqual(rpa_agents.foreground_class(value), "-", value)


class ForegroundLabelTests(unittest.TestCase):
    def test_known_screens(self):
        cases = {
            "ThunderRT6FormDC | 利用者認証": "HOPE 利用者認証",
            "ThunderRT6Main | ExBSecurityManagerI": "HOPE 認証(セキュリティマネージャ)",
            "ThunderRT6FormDC | エラー - ExBSecurityManager001": "HOPE 認証エラー",
            "ThunderRT6FormDC | パスワード変更": "HOPE パスワード変更",
            "ThunderRT6FormDC | HOPE/EGMAIN-GX 患者 山田太郎 12345678": "HOPE 電子カルテ",
            "IEFrame | http://host/?view&ris_pat_id=12345678": "RIS(IE)",
            "IEFrame | 何かのページ": "Internet Explorer",
            "TFMain | 壁カレ4 [C:\\Users\\someone\\AppData]": "壁カレ",
            "Windows.UI.Core.CoreWindow | スタート": "スタートメニュー",
            "ApplicationFrameWindow | 設定": "Windowsアプリ",
        }
        for value, label in cases.items():
            self.assertEqual(rpa_agents.foreground_label(value), label, value)

    def test_unknown_falls_back_to_class_only(self):
        self.assertEqual(rpa_agents.foreground_label("Static | "), "Static")
        self.assertEqual(rpa_agents.foreground_label("ThunderRT6FormDC | 患者 山田太郎"), "ThunderRT6FormDC")
        self.assertEqual(rpa_agents.foreground_label("クラス | x"), "-")
        self.assertEqual(rpa_agents.foreground_label(None), "-")
        self.assertEqual(rpa_agents.foreground_label(""), "-")

    def test_title_never_leaks(self):
        secret = "患者 山田太郎 12345678"
        for cls in ("ThunderRT6FormDC", "IEFrame", "TFMain", "Unknown", "Windows.UI.Core.CoreWindow"):
            label = rpa_agents.foreground_label(f"{cls} | {secret}")
            self.assertNotIn("山田", label)
            self.assertNotIn("12345678", label)


class StaleTests(unittest.TestCase):
    def test_boundaries(self):
        iso = lambda sec: (NOW - timedelta(seconds=sec)).isoformat()
        self.assertFalse(rpa_agents.is_stale(iso(119), NOW, 120))
        self.assertTrue(rpa_agents.is_stale(iso(120), NOW, 120))
        self.assertTrue(rpa_agents.is_stale(iso(3600), NOW, 120))
        self.assertFalse(rpa_agents.is_stale(iso(-30), NOW, 120))  # 実機の時計が進んでいる
        for bad in (None, "", "not-a-date"):
            self.assertTrue(rpa_agents.is_stale(bad, NOW, 120))


class FetchTests(FakeGuiMixin, unittest.TestCase):
    def test_disabled(self):
        rpa_agents.configure(None)
        self.assertEqual(rpa_agents.fetch_agents(NOW), {"enabled": False})
        rpa_agents.configure("   ")
        self.assertFalse(rpa_agents.is_enabled())

    def test_rows_stale_and_foreground(self):
        self.gui.AGENTS[:] = [("PC-A", (NOW - timedelta(seconds=5)).isoformat(), "IEFrame | http://x/?ris_pat_id=999"),
                              ("PC-B", (NOW - timedelta(minutes=20)).isoformat(), None)]
        data = rpa_agents.fetch_agents(NOW)
        self.assertTrue(data["enabled"])
        self.assertEqual(data["fresh_sec"], 120)
        self.assertEqual([c["key"] for c in data["columns"]][:2], ["label", "updated"])
        a, b = data["rows"]
        self.assertEqual((a["pc"], a["stale"], a["foreground"], a["tag"]), ("PC-A", False, "RIS(IE)", "idle"))
        self.assertEqual((b["pc"], b["stale"], b["foreground"]), ("PC-B", True, "-"))
        self.assertNotIn("ris_pat_id", json.dumps(data, ensure_ascii=False))

    def test_empty_agent_list(self):
        self.gui.AGENTS[:] = []
        data = rpa_agents.fetch_agents(NOW)
        self.assertEqual((data["rows"], "error" in data), ([], False))

    def test_error_keeps_previous_rows(self):
        self.gui.AGENTS[:] = [("PC-A", NOW.isoformat(), None)]
        self.assertEqual(len(rpa_agents.fetch_agents(NOW)["rows"]), 1)
        self.gui.FAIL = "share unavailable"
        try:
            data = rpa_agents.fetch_agents(NOW)
        finally:
            self.gui.FAIL = None
        self.assertIn("share unavailable", data["error"])
        self.assertEqual(len(data["rows"]), 1)

    def test_timeout_returns_previous_then_recovers(self):
        self.gui.AGENTS[:] = [("PC-A", NOW.isoformat(), None)]
        rpa_agents.fetch_agents(NOW)
        old = rpa_agents.READ_TIMEOUT_SEC
        rpa_agents.READ_TIMEOUT_SEC = 0.05
        self.gui.DELAY = 0.3
        try:
            t = time.monotonic()
            data = rpa_agents.fetch_agents(NOW)
            self.assertLess(time.monotonic() - t, 0.25)
            self.assertIn("遅れて", data["error"])
            self.assertEqual(len(data["rows"]), 1)
            rpa_agents.fetch_agents(NOW)  # 読み取り中は新しく積まない(同じ処理を待つ)
        finally:
            rpa_agents.READ_TIMEOUT_SEC = old
        time.sleep(0.4)
        self.gui.DELAY = 0.0
        self.assertNotIn("error", rpa_agents.fetch_agents(NOW))

    def test_missing_module(self):
        rpa_agents.configure(str(Path(self.tmp.name) / "nowhere"))
        data = rpa_agents.fetch_agents(NOW)
        self.assertTrue(data["enabled"])
        self.assertIn("見つかりません", data["error"])


class HttpTests(FakeGuiMixin, unittest.TestCase):
    TOKEN = "a" * 40

    def setUp(self):
        super().setUp()
        web.MonitorHandler.token = self.TOKEN
        self.server = web.MonitorServer(("127.0.0.1", 0), web.MonitorHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, token):
        r = urllib.request.Request(f"http://127.0.0.1:{self.server.server_address[1]}/api/agents")
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_auth_required(self):
        self.assertEqual(self.get(None)[0], 401)
        self.assertEqual(self.get("wrong" * 10)[0], 401)

    def test_ok_and_disabled(self):
        self.gui.AGENTS[:] = [("PC-A", datetime.now().isoformat(), "Unknown | secret title")]
        status, body = self.get(self.TOKEN)
        self.assertEqual((status, body["rows"][0]["foreground"], body["rows"][0]["stale"]), (200, "Unknown", False))
        self.assertNotIn("secret", json.dumps(body))
        rpa_agents.configure(None)
        self.assertEqual(self.get(self.TOKEN), (200, {"enabled": False}))

    def test_unreadable_is_503(self):
        rpa_agents.configure(str(Path(self.tmp.name) / "nowhere"))
        status, body = self.get(self.TOKEN)
        self.assertEqual(status, 503)
        self.assertIn("error", body)


@unittest.skipUnless(os.environ.get(rpa_agents.ENV_AGENT_DIR), "実データの確認は環境変数を設定したときだけ")
class RealDataTests(unittest.TestCase):
    def test_real_rows_have_no_titles(self):
        rpa_agents.configure(os.environ[rpa_agents.ENV_AGENT_DIR])
        try:
            data = rpa_agents.fetch_agents()
        finally:
            rpa_agents.configure(None)
        self.assertNotIn("error", data)
        self.assertTrue(data["rows"])
        for row in data["rows"]:
            self.assertNotIn("|", row["foreground"])
            labels = {label for _c, _m, label in rpa_agents.FOREGROUND_RULES}
            self.assertTrue(row["foreground"] in labels or row["foreground"] == "-"
                            or rpa_agents.FOREGROUND_CLASS_PATTERN.match(row["foreground"]), row["foreground"])
        self.assertNotIn("http", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
