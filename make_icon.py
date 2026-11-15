"""アイコン(claude_monitor.ico / claude_monitor_20.png / claude_monitor_32.png)を生成する。デザインはPWAのdocs/icon.svgと同じ
(暗色の角丸正方形・紫のリング・赤い点)。色を変えたらこのスクリプトを再実行する: python make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw

BG, RING, DOT = "#171a26", "#8b6cf6", "#ff4d4f"
MASTER_SIZE = 1024  # 縮小時に滑らかにするため大きく描いてから縮める
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
PNG_SIZES = (20, 32)  # 20=タイトルバー用、32=ウィンドウ(iconphoto)用

# 64単位のSVG座標(docs/icon.svg)を MASTER_SIZE へ拡大して描く
UNIT = MASTER_SIZE / 64


def draw_master() -> Image.Image:
    img = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, MASTER_SIZE - 1, MASTER_SIZE - 1], radius=14 * UNIT, fill=BG)
    ring_outer, ring_inner = 21 * UNIT, 15 * UNIT  # 半径18・線幅6(中心線18の外側21・内側15)
    c = MASTER_SIZE / 2
    d.ellipse([c - ring_outer, c - ring_outer, c + ring_outer, c + ring_outer], fill=RING)
    d.ellipse([c - ring_inner, c - ring_inner, c + ring_inner, c + ring_inner], fill=BG)
    dot = 7 * UNIT
    d.ellipse([c - dot, c - dot, c + dot, c + dot], fill=DOT)
    return img


def main() -> None:
    here = Path(__file__).resolve().parent
    master = draw_master()
    master.save(here / "claude_monitor.ico", sizes=ICO_SIZES)
    for size in PNG_SIZES:
        master.resize((size, size), Image.LANCZOS).save(here / f"claude_monitor_{size}.png")


if __name__ == "__main__":
    main()
