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
    NATIVE_FRAME = False  # Windows以外の既定(枠なし+自前のタイトルバー)
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
        self.app = cm.ClaudeMonitorApp(self.root, native_frame=self.NATIVE_FRAME)
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

    def _labels_texts(self):
        texts = []
        stack = [self.root]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if isinstance(w, tk.Label):
                texts.append(w.cget("text"))
        return texts

    def test_frame_mode(self):
        self.assertEqual(self.root.title(), cm.WINDOW_TITLE)
        borderless = bool(self.root.overrideredirect())
        self.assertEqual(borderless, not self.NATIVE_FRAME)
        texts = self._labels_texts()
        # 標準の枠ではアイコン・題名・✕はOS側にあるので帯に出さない。ボタン(↻・📌)は残す
        self.assertEqual("✕" in texts, not self.NATIVE_FRAME)
        self.assertEqual(cm.WINDOW_TITLE in texts, not self.NATIVE_FRAME)
        self.assertIn("↻", texts)
        self.assertIn("📌", texts)


class NativeFrameTest(ExternalResizeTest):
    """Windowsの既定(標準のタイトルバー・枠)。外部リサイズの扱いは同じであること。"""
    NATIVE_FRAME = True

    def test_own_resize_handle_still_clamps(self):
        self.skipTest("標準の枠ではリサイズハンドルを作らない(OSの枠で行う)")


class ColorrefTest(unittest.TestCase):
    def test_converts_to_bgr(self):
        self.assertEqual(cm.colorref("#12141d"), 0x1D1412)
        self.assertEqual(cm.colorref("#ff0000"), 0x0000FF)
        self.assertEqual(cm.colorref("#000000"), 0)

    def test_rejects_bad_values(self):
        for bad in ("", "#fff", "#12345", "red"):
            with self.assertRaises(ValueError):
                cm.colorref(bad)


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display (use xvfb-run)")
class Win32ChromeTest(unittest.TestCase):
    """Windowsの枠なし+自前の—□✕。Win32呼び出しは差し替えて、アプリ側の振る舞いだけ確かめる。"""

    def setUp(self):
        self.applied = []
        self.apply_result = True
        self.minimized = 0
        self.saved = []
        patches = [
            mock.patch.object(cm, "load_ui_prefs", lambda: {}),
            mock.patch.object(cm, "save_ui_prefs", lambda p: self.saved.append(dict(p))),
            mock.patch.object(cm, "get_sessions", lambda: []),
            mock.patch.object(cm, "fetch_plan_usage", lambda: {}),
            mock.patch.object(cm, "win32_apply_style", self._apply),
            mock.patch.object(cm, "win32_minimize", self._minimize),
            mock.patch.object(cm, "win32_work_area", lambda root: (0, 0, 900, 700)),
            mock.patch.object(cm, "win32_start_move", lambda root: True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.root = tk.Tk()
        self.app = cm.ClaudeMonitorApp(self.root, native_frame=False, win32_chrome=True)
        for _ in range(5):
            self.root.update()

    def tearDown(self):
        self.root.destroy()
        del self.app, self.root
        gc.collect()

    def _apply(self, root):
        self.applied.append(1)
        return self.apply_result

    def _minimize(self, root):
        self.minimized += 1

    def caption_texts_left_to_right(self):
        bar = self.app._maximize_btn.master
        labels = [w for w in bar.winfo_children() if isinstance(w, tk.Label)
                  and w.cget("text") in (cm.GLYPH_MINIMIZE, cm.GLYPH_MAXIMIZE, cm.GLYPH_RESTORE, cm.GLYPH_CLOSE)]
        return [w.cget("text") for w in sorted(labels, key=lambda w: w.winfo_x())]

    def test_caption_buttons_are_rightmost_in_order(self):
        self.assertEqual(self.caption_texts_left_to_right(),
                         [cm.GLYPH_MINIMIZE, cm.GLYPH_MAXIMIZE, cm.GLYPH_CLOSE])
        bar = self.app._maximize_btn.master
        others = [w.winfo_x() for w in bar.winfo_children() if isinstance(w, tk.Label)
                  and w.cget("text") == "📌"]
        self.assertTrue(others and others[0] < self.app._maximize_btn.winfo_x())  # 📌は—より左

    def test_maximize_and_restore(self):
        before = (self.app._width, self.app._height)
        self.app._toggle_maximize()
        for _ in range(5):
            self.root.update()
        self.assertEqual((self.root.winfo_width(), self.root.winfo_height()), (900, 700))
        self.assertEqual(self.app._maximize_btn.cget("text"), cm.GLYPH_RESTORE)
        self.app._render([], None)  # 自動の高さ調整で縮めない
        self.root.update()
        self.assertEqual(self.root.winfo_height(), 700)
        self.assertFalse(self.app._user_sized)  # 最大化は外部リサイズとみなさない
        self.app._toggle_maximize()
        for _ in range(5):
            self.root.update()
        self.assertEqual(self.app._width, before[0])
        self.assertEqual(self.app._maximize_btn.cget("text"), cm.GLYPH_MAXIMIZE)

    def test_maximize_without_work_area_does_nothing(self):
        with mock.patch.object(cm, "win32_work_area", lambda root: None):
            self.app._toggle_maximize()
        self.assertIsNone(self.app._maximized_from)

    def test_minimize_uses_win32(self):
        self.app._minimize()
        self.assertEqual(self.minimized, 1)

    def test_style_is_applied_only_once(self):
        # 起動時の1回だけ。定期更新・最前面切替では付け直さない(繰り返すとエクスプローラーが暴走した)
        self.root.after(cm.WIN_STYLE_DELAY_MS + 50, lambda: None)
        import time
        end = time.time() + (cm.WIN_STYLE_DELAY_MS + 300) / 1000
        while time.time() < end:
            self.root.update()
        self.assertEqual(len(self.applied), 1)
        for _ in range(5):
            self.app._ensure_win_style()
            self.app.refresh()
        self.app._toggle_topmost()
        self.app._toggle_topmost()
        self.assertEqual(len(self.applied), 1)

    def test_native_move_skips_manual_move(self):
        ev = mock.Mock(x_root=100, y_root=100)
        self.app._start_move(ev)
        x0 = self.root.winfo_x()
        self.app._on_move(mock.Mock(x_root=300, y_root=300))
        self.root.update()
        self.assertEqual(self.root.winfo_x(), x0)


class Win32StyleTest(unittest.TestCase):
    def test_target_style(self):
        popup = cm.WS_POPUP | 0x02000000  # WS_POPUP|WS_CLIPCHILDREN(枠なしTkの既定)
        style, ex = cm.win32_style_target(popup, cm.WS_EX_TOOLWINDOW)
        self.assertFalse(style & cm.WS_POPUP)
        self.assertTrue(style & cm.WS_MINIMIZEBOX)
        self.assertTrue(style & 0x02000000)  # 他のビットは残す
        self.assertFalse(ex & cm.WS_EX_TOOLWINDOW)
        self.assertTrue(ex & cm.WS_EX_APPWINDOW)
        # 符号付きで返ってきた値(GetWindowLongW)でも同じ結果
        self.assertEqual(cm.win32_style_target(popup - 0x100000000, cm.WS_EX_TOOLWINDOW), (style, ex))
        # 既に目的のスタイルなら変わらない
        self.assertEqual(cm.win32_style_target(style, ex), (style, ex))

    def test_as_long(self):
        self.assertEqual(cm._as_long(0x80000000), -0x80000000)
        self.assertEqual(cm._as_long(0x7FFFFFFF), 0x7FFFFFFF)
        self.assertEqual(cm._as_long(0), 0)


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display (use xvfb-run)")
class ManualMoveTest(ExternalResizeTest):
    def test_drag_keeps_grab_offset(self):
        self.root.geometry("+100+100")
        self.root.update()
        self.app._start_move(mock.Mock(x_root=150, y_root=110))  # 左上から(50,10)の位置を掴む
        self.app._on_move(mock.Mock(x_root=400, y_root=300))
        self.root.update()
        self.assertEqual((self.root.winfo_x(), self.root.winfo_y()), (350, 290))


if __name__ == "__main__":
    unittest.main()
