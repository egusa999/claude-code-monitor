"""アイコン(claude_monitor.ico / claude_monitor_20.png / claude_monitor_32.png、PWA用のdocs/icon-*.png)を生成する。デザインはPWAのdocs/icon.svgと同じ
(暗色の角丸正方形・紫のリング・赤い点)。色を変えたらこのスクリプトを再実行する: python make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw

BG, RING, DOT = "#171a26", "#8b6cf6", "#ff4d4f"
MASTER_SIZE = 1024  # 縮小時に滑らかにするため大きく描いてから縮める
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
PNG_SIZES = (20, 32)  # 20=タイトルバー用、32=ウィンドウ(iconphoto)用
PWA_SIZES = (192, 512)  # Android等でPWAをインストールするときのアイコン(docs/icon-<size>.png)
MASKABLE_SIZE = 512     # Androidの丸・角丸マスク用(docs/icon-maskable.png)。絵は中央80%の安全領域に収める
MASKABLE_SCALE = 0.8

# 64単位のSVG座標(docs/icon.svg)を MASTER_SIZE へ拡大して描く
UNIT = MASTER_SIZE / 64


def draw_master(maskable: bool = False) -> Image.Image:
    """maskable=Trueなら背景を全面塗り(角丸なし)にし、絵を中央の安全領域へ縮める。"""
    img = Image.new("RGBA", (MASTER_SIZE, MASTER_SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if maskable:
        d.rectangle([0, 0, MASTER_SIZE - 1, MASTER_SIZE - 1], fill=BG)
    else:
        d.rounded_rectangle([0, 0, MASTER_SIZE - 1, MASTER_SIZE - 1], radius=14 * UNIT, fill=BG)
    unit = UNIT * (MASKABLE_SCALE if maskable else 1)
    ring_outer, ring_inner = 21 * unit, 15 * unit  # 半径18・線幅6(中心線18の外側21・内側15)
    c = MASTER_SIZE / 2
    d.ellipse([c - ring_outer, c - ring_outer, c + ring_outer, c + ring_outer], fill=RING)
    d.ellipse([c - ring_inner, c - ring_inner, c + ring_inner, c + ring_inner], fill=BG)
    dot = 7 * unit
    d.ellipse([c - dot, c - dot, c + dot, c + dot], fill=DOT)
    return img


def main() -> None:
    here = Path(__file__).resolve().parent
    master = draw_master()
    master.save(here / "claude_monitor.ico", sizes=ICO_SIZES)
    for size in PNG_SIZES:
        master.resize((size, size), Image.LANCZOS).save(here / f"claude_monitor_{size}.png")
    for size in PWA_SIZES:
        master.resize((size, size), Image.LANCZOS).save(here / "docs" / f"icon-{size}.png")
    draw_master(maskable=True).resize((MASKABLE_SIZE, MASKABLE_SIZE), Image.LANCZOS).save(
        here / "docs" / "icon-maskable.png")


if __name__ == "__main__":
    main()
