"""GUIが外部(FancyZones・スナップ等)から変えられたサイズを採用し、自動調整で上書きしないことの確認。
画面(DISPLAY)が無い環境ではスキップする。Xvfbなら: xvfb-run -a python3 -m unittest tests.test_gui_resize"""
import gc
import os
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import claude_monitor as cm


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display (use xvfb-run)")
class ExternalResizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        prefs = Path(self.tmp.name) / "ui.json"
        patches = [
            mock.patch.object(cm, "load_ui_prefs", lambda: {}),
            mock.patch.object(cm, "save_ui_prefs", self._save),
            mock.patch.object(cm, "get_sessions", lambda: []),
            mock.patch.object(cm, "fetch_plan_usage", lambda: {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.saved = []
        self.root = tk.Tk()
        self.app = cm.ClaudeMonitorApp(self.root)
        self.pump()

    def tearDown(self):
        self.root.destroy()
        # Tkのオブジェクトはメインスレッドで解放する(他テストのスレッドでGCされるとTcl_AsyncDeleteで異常終了する)
        del self.app, self.root
        gc.collect()
        self.tmp.cleanup()

    def _save(self, prefs):
        self.saved.append(dict(prefs))

    def pump(self):
        for _ in range(5):
            self.root.update()

    def external_resize(self, w, h):
        self.root.tk.call("wm", "geometry", ".", f"{w}x{h}")  # _set_sizeを通さない=外部からの変更
        self.pump()

    def test_auto_height_does_not_mark_user_sized(self):
        self.assertFalse(self.app._user_sized)
        self.app._fit_scroll_area()
        self.pump()
        self.assertFalse(self.app._user_sized)
        self.assertEqual(self.saved, [])

    def test_external_resize_is_kept_after_rerender(self):
        self.external_resize(700, 500)
        self.assertTrue(self.app._user_sized)
        self.assertEqual((self.app._width, self.app._height), (700, 500))
        self.app._render([], None)  # 再描画しても高さを内容に戻さない
        self.pump()
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (700, 500))
        self.assertEqual(self.saved[-1].get("width"), 700)
        self.assertEqual(self.saved[-1].get("height"), 500)

    def test_zone_smaller_than_minimum_fits(self):
        # ゾーンが最小サイズ(420x200)より小さくても、その大きさに収まる
        self.external_resize(300, 150)
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (300, 150))
        self.assertEqual((self.app._width, self.app._height), (300, 150))

    def test_own_resize_handle_still_clamps(self):
        self.app._rs_start = (0, 0, 500, 400)
        ev = mock.Mock(x_root=-400, y_root=-400)
        self.app._on_resize(ev, True, True)
        self.pump()
        self.assertEqual((self.app._width, self.app._height), (cm.MIN_WINDOW_WIDTH, cm.MIN_WINDOW_HEIGHT))

    def test_child_configure_is_ignored(self):
        before = (self.app._width, self.app._height, self.app._user_sized)
        ev = mock.Mock(widget=self.app.body, width=10, height=10)
        self.app._on_root_configure(ev)
        self.assertEqual((self.app._width, self.app._height, self.app._user_sized), before)

    def test_zero_size_is_ignored(self):
        before = (self.app._width, self.app._height, self.app._user_sized)
        self.app._on_root_configure(mock.Mock(widget=self.root, width=1, height=1))
        self.assertEqual((self.app._width, self.app._height, self.app._user_sized), before)


if __name__ == "__main__":
    unittest.main()
