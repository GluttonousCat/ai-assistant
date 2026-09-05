# -*- coding: utf-8 -*-
"""动效/页面切换预设表（纯数据，LO Python 3.10 可导入）。

## 动画 (v2: 纯 SMIL 节点树构建)
- 旧版形状 Effect 属性已被实测否决: 导出 pptx 不生成 <p:timing>。
- 正确路线: 在页面动画主序列下手工构建 ParallelTimeContainer×2 + AnimateSet,
  UserData 写 node-type(int)/preset-class(int)/preset-id(str)/preset-sub-type(str),
  导出器据此写出 PowerPoint 的 presetID/presetClass/presetSubType —— PP/WPS 按预设引擎播放。
- preset-id 目录为回灌实验实测收割 (注入 PP presetID 1-45 → 读回 LO 命名, 2026-09-05, LO 25.2.2.2)。
- preset-class 整数: 1=entrance, 2=exit (实测)。
- preset-sub-type 写方向字符串 ("from-left" 等), 导出时由 LO 翻译成 PP 数字。

## 页面切换
- 页面属性 TransitionType / TransitionSubtype / TransitionDirection / TransitionDuration (probe 实测存在)。
"""
from __future__ import annotations

# ---- 触发方式 ----
TRIGGERS = ("onclick", "with_previous", "after_previous")

DEFAULT_DURATION = 0.5  # 秒

_DIRS = {
    "left": "from-left", "right": "from-right", "top": "from-top", "bottom": "from-bottom",
    "upperleft": "from-top-left", "upperright": "from-top-right",
    "lowerright": "from-bottom-right", "lowerleft": "from-bottom-left",
}


def _mk(name, preset_id, cls, subtype=None, pp=None):
    return name, {"preset_id": preset_id, "cls": cls, "subtype": subtype, "pp": pp}


def _build_catalog():
    cats = []
    # 进场基础
    for n, pid, pp in (
        ("appear", "ooo-entrance-appear", 1),
        ("fade", "ooo-entrance-fade-in", 10),
        ("blinds", "ooo-entrance-venetian-blinds", 3),
        ("box", "ooo-entrance-box", 4),
        ("checkerboard", "ooo-entrance-checkerboard", 5),
        ("circle", "ooo-entrance-circle", 6),
        ("diamond", "ooo-entrance-diamond", 8),
        ("dissolve", "ooo-entrance-dissolve-in", 9),
        ("plus", "ooo-entrance-plus", 13),
        ("random_bars", "ooo-entrance-random-bars", 14),
        ("split", "ooo-entrance-split", 16),
        ("stretch", "ooo-entrance-stretchy", 17),
        ("swivel", "ooo-entrance-swivel", 19),
        ("wheel", "ooo-entrance-wheel", 21),
        ("zoom_in", "ooo-entrance-zoom", 23),
        ("float", "ooo-entrance-float", 30),
        ("rise_up", "ooo-entrance-rise-up", 37),
        ("ease_in", "ooo-entrance-ease-in", 29),
        ("grow_turn", "ooo-entrance-turn-and-grow", 31),
        ("crawl_in", "ooo-entrance-fly-in-slow", 7),
        ("spin_in", "ooo-entrance-pinwheel", 35),
    ):
        cats.append(_mk(n, pid, "entrance", None, pp))
    # 进场带方向: fly/wipe/peek + random_bars/blinds 朝向
    for d, sub in _DIRS.items():
        cats.append(_mk("fly_in_" + d, "ooo-entrance-fly-in", "entrance", sub, 2))
    for d in ("left", "right", "top", "bottom"):
        cats.append(_mk("wipe_in_" + d, "ooo-entrance-wipe", "entrance", _DIRS[d], 22))
        cats.append(_mk("peek_in_" + d, "ooo-entrance-peek-in", "entrance", _DIRS[d], 12))
    cats.append(_mk("random_bars_h", "ooo-entrance-random-bars", "entrance", "horizontal", 14))
    cats.append(_mk("random_bars_v", "ooo-entrance-random-bars", "entrance", "vertical", 14))
    cats.append(_mk("blinds_h", "ooo-entrance-venetian-blinds", "entrance", "horizontal", 3))
    cats.append(_mk("blinds_v", "ooo-entrance-venetian-blinds", "entrance", "vertical", 3))
    # 出场基础
    for n, pid, pp in (
        ("disappear", "ooo-exit-disappear", 1),
        ("fade_out", "ooo-exit-fade-out", 10),
        ("dissolve_out", "ooo-exit-dissolve", 9),
        ("zoom_out", "ooo-exit-zoom", 23),
        ("split_out", "ooo-exit-split", 16),
        ("collapse", "ooo-exit-collapse", 17),
        ("sink_down", "ooo-exit-sink-down", 37),
        ("swish", "ooo-exit-swish", 38),
        ("crawl_out", "ooo-exit-crawl-out", 7),
        ("wheel_out", "ooo-exit-wheel", 21),
        ("swivel_out", "ooo-exit-swivel", 19),
    ):
        cats.append(_mk(n, pid, "exit", None, pp))
    for d, sub in _DIRS.items():
        cats.append(_mk("fly_out_" + d, "ooo-exit-fly-out", "exit", sub, 2))
    for d in ("left", "right", "top", "bottom"):
        cats.append(_mk("wipe_out_" + d, "ooo-exit-wipe", "exit", _DIRS[d], 22))
        cats.append(_mk("peek_out_" + d, "ooo-exit-peek-out", "exit", _DIRS[d], 12))
    return dict(cats)


ANIMATIONS = _build_catalog()
ENTRANCE_NAMES = sorted(k for k, v in ANIMATIONS.items() if v["cls"] == "entrance")
EXIT_NAMES = sorted(k for k, v in ANIMATIONS.items() if v["cls"] == "exit")

# ---- 页面切换预设: 友好名 -> (TransitionType, TransitionSubtype) ----
# 常量见 com.sun.star.animations.TransitionType / TransitionSubType
TRANSITIONS = {
    # fade 必须用 subtype 101 (LO 内部编号, 经 <p:fade/> 反向导入实测 2026-09-05);
    # subtype 0 会被 LO 25.2 的 pptx 导出器丢弃 → 导出空 <p:transition> (切换失效)
    "fade": (37, 101),
    "push_from_top": (35, 1),
    "push_from_right": (35, 2),
    "push_from_bottom": (35, 3),
    "push_from_left": (35, 4),
    "slide_from_top": (36, 1),
    "slide_from_right": (36, 2),
    "slide_from_bottom": (36, 3),
    "slide_from_left": (36, 4),
    "wipe_from_top": (1, 1),
    "wipe_from_right": (1, 2),
    "wipe_from_bottom": (1, 3),
    "wipe_from_left": (1, 4),
    "dissolve": (40, 0),
    "random": (42, 0),
}
