# -*- coding: utf-8 -*-
"""demo_deck.py — 生成一份带动画的三页演示 PPT (不经 MCP, 直连 worker, 兼排障示例)。

运行: .venv/Scripts/python.exe -m pptgen.examples.demo_deck [输出路径]
"""
import os
import sys

from pptgen.bridge.client import WorkerClient

W, H = 338.7, 190.5  # 16:9 mm
INK, ACCENT, MUTED = "1A2B3C", "C9A66B", "667788"


def build(c: WorkerClient, out_path: str):
    c.new_deck(W, H)
    # --- 封面 (new_deck 自带第 0 页) ---
    c.add_shape(slide=0, kind="rectangle", x=0, y=0, w=W, h=H, fill="1A2B3C", name="bg")
    c.add_textbox(slide=0, text="PPT Generate Agent", x=30, y=60, w=278, h=30,
                  font_size=40, bold=True, color="FFFFFF", align="center", name="cover_t")
    c.add_textbox(slide=0, text="LibreOffice MCP · 动效演示", x=30, y=100, w=278, h=18,
                  font_size=16, color="C9A66B", align="center", name="cover_s")
    c.add_animation(slide=0, shape="cover_t", effect="fly_in_left", duration=0.8)
    c.add_animation(slide=0, shape="cover_s", effect="fade", trigger="after_previous",
                    delay=0.2)
    c.set_transition(slide=0, effect="fade", duration=0.5)

    # --- 内容页 ---
    c.add_slide()
    c.add_textbox(slide=1, text="核心能力", x=25, y=15, w=200, h=22, font_size=26,
                  bold=True, color=INK, name="h1")
    c.add_shape(slide=1, kind="line", x=25, y=42, w=288, h=0, line_color="C9A66B",
                line_width=1.2, name="hr1")
    bullets = ["真实 PowerPoint 动画: presetID 元数据直出", "渲染自查闭环: render_slide → 看图 → 修正",
               "全部原子操作: 文本/形状/表格/图片/切换"]
    for i, b in enumerate(bullets):
        c.add_textbox(slide=1, text="• " + b, x=30, y=58 + i * 18, w=280, h=14,
                      font_size=14, color=INK, name="b%d" % i)
        c.add_animation(slide=1, shape="b%d" % i, effect="peek_in_left",
                        trigger="after_previous" if i else "onclick", delay=0.1)
    c.add_table(slide=1, rows=[["效果", "兼容性"], ["fly_in / fade / zoom", "PowerPoint ✓"],
                               ["wipe / peek / blinds", "PowerPoint ✓"]],
                x=30, y=125, w=180, h=40, header_fill="2B4C6F", font_size=11, name="tbl")
    c.add_animation(slide=1, shape="tbl", effect="fade", trigger="after_previous")
    c.set_transition(slide=1, effect="push_from_left", duration=0.6)

    # --- 结尾 ---
    c.add_slide()
    c.add_textbox(slide=2, text="谢谢观看", x=30, y=75, w=278, h=30, font_size=32,
                  bold=True, color=INK, align="center", name="end")
    c.add_animation(slide=2, shape="end", effect="zoom_in", duration=0.8)
    c.set_transition(slide=2, effect="dissolve")

    r = c.save(out_path)
    print("已生成:", r["path"], "(%.1f KB)" % (r["size"] / 1024))
    for i in range(3):
        png = c.render_slide(slide=i)
        print("渲染: slide %d -> %s" % (i, png["png"]))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "demo.pptx")
    c = WorkerClient()
    try:
        build(c, out)
    finally:
        c.shutdown()


if __name__ == "__main__":
    main()
