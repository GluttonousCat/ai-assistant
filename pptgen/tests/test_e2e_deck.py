# -*- coding: utf-8 -*-
"""pptgen 端到端测试 — 需要 LibreOffice (跳过条件: 找不到安装)。

运行: .venv/Scripts/python.exe -m pytest pptgen/tests -v
覆盖: 内容工具/动画(SMIL)/切换/渲染/保存 XML 校验/重开往返。
"""
import os
import re
import zipfile

import pytest

from pptgen.bridge.client import WorkerClient
from pptgen.bridge.protocol import find_lo_program

pytestmark = pytest.mark.skipif(
    find_lo_program()[0] is None, reason="未安装 LibreOffice (PPTGEN_LO_PROGRAM 可指定)")

TMP = os.environ.get("TEMP", os.path.expanduser("~"))
IMG = os.path.join(TMP, "pptgen_test_img.png")


@pytest.fixture(scope="module")
def client():
    # 测试图片
    try:
        from PIL import Image
        Image.new("RGB", (400, 300), (40, 60, 90)).save(IMG)
    except ImportError:
        pytest.skip("Pillow 不可用, 跳过 (图片用例)")
    c = WorkerClient()
    yield c
    c.shutdown()


@pytest.fixture()
def deck(client):
    client.new_deck()
    client.add_slide()
    return client


def slide_xml(pptx_path, index=0):
    with zipfile.ZipFile(pptx_path) as z:
        return z.read("ppt/slides/slide%d.xml" % (index + 1)).decode("utf-8")


def test_content_tools_and_save(deck, tmp_path):
    c = deck
    c.add_textbox(slide=0, text="标题", x=20, y=15, w=300, h=25, font_size=28,
                  bold=True, name="title")
    c.add_shape(slide=0, kind="rounded", x=20, y=50, w=60, h=20, fill="2B4C6F",
                text="圆角", font_size=12, color="FFFFFF", name="r1")
    c.add_table(slide=0, rows=[["K", "V"], ["营收", "100"]], x=20, y=90, w=100, h=30,
                header_fill="2B4C6F", name="t1")
    c.add_image(slide=0, path=IMG, x=200, y=50, w=60, name="i1")
    r = c.render_slide(slide=0)
    assert os.path.isfile(r["png"]) and os.path.getsize(r["png"]) > 1000
    out = str(tmp_path / "content.pptx")
    c.save(out)
    assert os.path.getsize(out) > 5000
    inv = c.list_slides()
    names = [s["name"] for s in inv["slides"][0]["shapes"]]
    assert {"title", "r1", "t1", "i1"} <= set(names)


def test_textbox_set_text(deck):
    c = deck
    c.add_textbox(slide=0, text="旧文本", x=20, y=20, w=100, h=15, name="tb")
    c.set_text(slide=0, shape="tb", text="新文本", color="AA0000", font_size=18)
    inv = c.list_slides()
    tb = [s for s in inv["slides"][0]["shapes"] if s["name"] == "tb"][0]
    assert tb["text"] == "新文本"


def test_animations_build_and_export(deck, tmp_path):
    c = deck
    for i, kw in enumerate([
        dict(shape="a1", effect="fly_in_left", duration=0.6),
        dict(shape="a2", effect="fade", trigger="with_previous", delay=0.3),
        dict(shape="a3", effect="zoom_in", trigger="after_previous", delay=0.2),
        dict(shape="a4", effect="fade_out", trigger="onclick"),
    ]):
        c.add_textbox(slide=0, text="t%d" % i, x=20, y=20 + i * 20, w=80, h=10,
                      name=kw["shape"])
        c.add_animation(slide=0, **kw)
    anims = c.list_animations(slide=0)["animations"]
    assert len(anims) == 4
    assert anims[0]["preset_id"] == "ooo-entrance-fly-in"
    assert anims[0]["subtype"] == "from-left"
    assert anims[1]["trigger"] == "with_previous" and anims[1]["begin"] == 0.3
    assert anims[2]["trigger"] == "after_previous"
    assert anims[3]["preset_id"] == "ooo-exit-fade-out"
    out = str(tmp_path / "anim.pptx")
    c.save(out)
    xml = slide_xml(out)
    assert "<p:timing>" in xml
    # PowerPoint 预设元数据 (导出器翻译: fly-in=presetID 2, from-left=subtype 8)
    assert re.search(r'presetID="2"[^>]*presetClass="entr"[^>]*presetSubtype="8"', xml) or \
        re.search(r'presetClass="entr"[^>]*presetID="2"[^>]*presetSubtype="8"', xml)
    for nt in ("clickEffect", "withEffect", "afterEffect"):
        assert 'nodeType="%s"' % nt in xml
    assert 'presetClass="exit"' in xml


def test_transition_roundtrip(deck):
    c = deck
    c.set_transition(slide=0, effect="push_from_left", duration=0.8)
    inv = c.list_slides()
    tr = inv["slides"][0]["transition"]
    assert tr["TransitionType"] == 35 and tr["TransitionSubtype"] == 4


def test_remove_animation(deck):
    c = deck
    c.add_textbox(slide=0, text="x", x=20, y=20, w=50, h=10, name="rx")
    c.add_animation(slide=0, shape="rx", effect="appear")
    assert len(c.list_animations(slide=0)["animations"]) == 1
    c.remove_animation(slide=0, shape="rx")
    assert len(c.list_animations(slide=0)["animations"]) == 0


def test_open_deck_roundtrip(deck, tmp_path):
    c = deck
    c.add_textbox(slide=0, text="重开测试", x=20, y=20, w=120, h=20, name="ro")
    c.add_animation(slide=0, shape="ro", effect="fly_in_right")
    out = str(tmp_path / "reopen.pptx")
    c.save(out)
    c.open_deck(out)
    inv = c.list_slides()
    names = [s["name"] for s in inv["slides"][0]["shapes"]]
    assert "ro" in names
    anims = c.list_animations(slide=0)["animations"]
    assert any(a["shape"] == "ro" and "fly-in" in a["preset_id"] for a in anims)
