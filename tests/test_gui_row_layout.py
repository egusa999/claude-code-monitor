"""GUIの行がPWAと同じ並び(1段目=ランプ+名前、2段目=ボタン、3段目=パス、右端=コンテキスト列)になることの確認。
画面(DISPLAY)が無い環境ではスキップする。Xvfbなら: xvfb-run -a python3 -m unittest tests.test_gui_row_layout"""
import gc
import os
import tkinter as tk
import unittest

import claude_monitor as cm


def geom(w):
    return w.winfo_x(), w.winfo_y(), w.winfo_width(), w.winfo_height()


@unittest.skipUnless(os.environ.get("DISPLAY"), "needs a display (use xvfb-run)")
class RowLayoutTest(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.geometry(f"{cm.WINDOW_WIDTH}x300")
        self.busy = {}

    def tearDown(self):
        self.root.destroy()
        del self.root
        gc.collect()

    def make_row(self, info, width=cm.WINDOW_WIDTH, with_end=True):
        self.root.geometry(f"{width}x300")
        row = cm.SessionRow(self.root, cm.COLOR_ROW_BG, lambda cmd: True,
                            on_end=(lambda i: None) if with_end else None,
                            on_clear=(lambda i: None) if with_end else None, busy_kind=self.busy.get)
        row.pack(fill="x")
        row.update_data(info, cm.name_col_width(width))
        for _ in range(3):
            self.root.update()
        return row

    def info(self, name="Claudecodemonitor", attach="tmux attach -t =Claudecodemonitor", **kw):
        return cm.SessionInfo(pid=1, session_id="sid-1", name=name, cwd="/root/onedrive/ClaudeCode/x",
                              started_at=0, context_pct=12.0, context_tokens=120000, context_limit=1000000,
                              model="claude-opus-5-5", attach_cmd=attach, **kw)

    def assert_inside_name_col(self, row, widget, width):
        x, _, w, _ = geom(widget)
        bar_left = width - cm.ROW_PADDING_X - cm.BAR_COL_WIDTH
        self.assertLessEqual(x + w, bar_left, f"{widget} overlaps the context column")

    def test_lamp_name_then_chips_then_path(self):
        row = self.make_row(self.info())
        lx, ly, lw, lh = geom(row.lamp_canvas)
        nx, ny, nw, nh = geom(row.name_label)
        # ランプは名前の左で、名前の行の中に縦中央で収まる
        self.assertEqual(lx, cm.ROW_PADDING_X)
        self.assertLessEqual(lx + lw, nx)
        self.assertGreaterEqual(ly, ny)
        self.assertLessEqual(ly + lh, ny + nh)
        # ボタンはすべて名前の下の段で、名前の頭に字下げを揃える
        chips = [row.term_chip, row.clear_chip, row.end_chip]
        for chip in chips:
            self.assertTrue(chip.winfo_ismapped())
            self.assertGreaterEqual(chip.winfo_y(), ny + nh)
        self.assertEqual(row.term_chip.winfo_x(), nx)
        # 並びは 端末・クリア・終了(UUIDは無い)
        xs = [c.winfo_x() for c in chips]
        self.assertEqual(xs, sorted(xs))
        self.assertEqual([c.cget("text") for c in chips], ["端末", "クリア", "終了"])
        self.assertFalse(hasattr(row, "uuid_chip"))
        # パスはボタンの下、同じ字下げ
        cx, cy, _, _ = geom(row.cwd_label)
        self.assertEqual(cx, nx)
        self.assertGreaterEqual(cy, max(c.winfo_y() + c.winfo_height() for c in chips))
        # ステータス列は無く、コンテキスト列が右端にある
        bx, _, bw, _ = geom(row.bar_canvas)
        self.assertEqual(bx + bw, cm.WINDOW_WIDTH - cm.ROW_PADDING_X)

    def test_no_chips_without_attach(self):
        row = self.make_row(self.info(attach=""))
        for chip in (row.term_chip, row.clear_chip, row.end_chip):
            self.assertFalse(chip.winfo_ismapped())
        # ボタンの段を詰め、パスは名前のすぐ下
        self.assertLess(row.cwd_label.winfo_y(), row.name_label.winfo_y() + row.name_label.winfo_height() + 6)

    def test_bg_job_has_terminal_only(self):
        row = self.make_row(self.info(attach="claude attach j1"))
        self.assertTrue(row.term_chip.winfo_ismapped())
        self.assertFalse(row.clear_chip.winfo_ismapped())
        self.assertFalse(row.end_chip.winfo_ismapped())

    def test_local_mode_has_terminal_only(self):
        row = self.make_row(self.info(), with_end=False)
        self.assertTrue(row.term_chip.winfo_ismapped())
        self.assertFalse(row.clear_chip.winfo_ismapped())
        self.assertFalse(row.end_chip.winfo_ismapped())

    def test_busy_labels(self):
        self.busy["sid-1"] = "clear"
        row = self.make_row(self.info())
        self.assertEqual((row.clear_chip.cget("text"), row.end_chip.cget("text")), ("クリア中…", "終了"))
        self.busy["sid-1"] = "end"
        row.update_data(self.info(), cm.name_col_width(cm.WINDOW_WIDTH))
        self.assertEqual((row.clear_chip.cget("text"), row.end_chip.cget("text")), ("クリア", "終了中…"))

    def test_long_name_is_ellipsized_within_name_col(self):
        row = self.make_row(self.info(name="VeryLongSessionName" * 6))
        self.assertTrue(row.name_label.cget("text").endswith("…"))
        self.assert_inside_name_col(row, row.name_label, cm.WINDOW_WIDTH)

    def test_chips_wrap_at_minimum_width(self):
        width = cm.MIN_WINDOW_WIDTH
        row = self.make_row(self.info(), width=width)
        for w in (row.name_label, row.term_chip, row.clear_chip, row.end_chip, row.cwd_label):
            self.assert_inside_name_col(row, w, width)
        # 行の高さはパスの下端まで確保される(次の行に重ならない)
        self.assertGreaterEqual(row.winfo_height(), row.cwd_label.winfo_y() + row.cwd_label.winfo_height())

    def test_working_lamp_blinks(self):
        row = self.make_row(self.info(working=True))
        colors = []
        for _ in range(2):
            colors.append(row.lamp_canvas.itemcget(row.lamp_canvas.find_all()[0], "fill"))
            row.toggle_blink()
        self.assertEqual(set(colors), {cm.COLOR_RED_ON, cm.COLOR_RED_OFF})


if __name__ == "__main__":
    unittest.main()
