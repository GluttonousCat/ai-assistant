# -*- coding: utf-8 -*-
"""pptgen MCP server — 把 WorkerClient 的原子操作暴露为 MCP 工具 (stdio)。

运行: 项目 .venv 的 python (依赖 fastmcp); 由 ZCode 工作区 MCP 配置拉起。
工具描述内嵌完整效果目录 (来自 presets.py), 模型无需猜测枚举值。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastmcp import FastMCP

try:
    from .client import PptgenError, WorkerClient
    from . import presets
except ImportError:
    from client import PptgenError, WorkerClient
    import presets

mcp = FastMCP("pptgen")
_client = None


def _c() -> WorkerClient:
    global _client
    if _client is None:
        _client = WorkerClient()
    return _client


def _j(r) -> str:
    return json.dumps(r, ensure_ascii=False, default=str)


_EFFECTS_DOC = (
    "进场效果: " + ", ".join(presets.ENTRANCE_NAMES) +
    " | 出场效果: " + ", ".join(presets.EXIT_NAMES))
_TRANS_DOC = ", ".join(sorted(presets.TRANSITIONS))
_MM = "坐标/尺寸单位 mm; 16:9 页面默认 338.7 x 190.5 (先 list_slides 查 page_mm)。颜色为 RRGGBB 十六进制。"


@mcp.tool
def status() -> str:
    """查询 pptgen 管线状态 (worker/soffice/文档是否就绪, 页面尺寸)。"""
    return _j(_c().status())


@mcp.tool
def new_deck(width_mm: float = 338.7, height_mm: float = 190.5) -> str:
    """新建空白演示文稿 (会替换当前文档)。默认 16:9 (338.7 x 190.5 mm)。"""
    return _j(_c().new_deck(width_mm=width_mm, height_mm=height_mm))


@mcp.tool
def open_deck(path: str) -> str:
    """打开已有 .pptx/.odp 继续编辑 (path 为绝对路径)。"""
    return _j(_c().open_deck(path=path))


@mcp.tool
def save(path: str, fmt: str = "pptx") -> str:
    """保存文档。fmt: pptx | odp | pdf。path 为绝对路径, 返回文件大小。"""
    return _j(_c().save(path=path, fmt=fmt))


@mcp.tool
def close() -> str:
    """关闭当前文档 (不退出 soffice 常驻实例)。"""
    return _j(_c().close())


@mcp.tool
def add_slide() -> str:
    """在末尾追加一页空白幻灯片, 返回 0 起始页码。"""
    return _j(_c().add_slide())


@mcp.tool
def list_slides() -> str:
    """列出每页: 形状清单(名称/类型/位置/文字预览)、切换效果、动画清单。做任何修改前先调用它对齐状态。"""
    return _j(_c().list_slides())


@mcp.tool
def add_textbox(slide: int, text: str, x: float, y: float, w: float, h: float,
                font_size: float = None, bold: bool = None, color: str = None,
                align: str = None, font: str = None, name: str = None) -> str:
    """添加文本框。""" + _MM + """ align: left|center|right|justify。默认微软雅黑。
    多段落用 \\n 分隔。返回形状名 (后续引用它)。"""
    return _j(_c().add_textbox(slide=slide, text=text, x=x, y=y, w=w, h=h,
                               font_size=font_size, bold=bold, color=color,
                               align=align, font=font, name=name))


@mcp.tool
def set_text(slide: int, shape: str, text: str, font_size: float = None,
             bold: bool = None, color: str = None, align: str = None) -> str:
    """替换指定形状的文字并可选改样式 (shape 为 add_* 返回的名称)。"""
    return _j(_c().set_text(slide=slide, shape=shape, text=text, font_size=font_size,
                            bold=bold, color=color, align=align))


@mcp.tool
def add_shape(slide: int, kind: str, x: float, y: float, w: float, h: float,
              fill: str = None, line_color: str = None, line_width: float = None,
              text: str = None, font_size: float = None, bold: bool = None,
              color: str = None, name: str = None) -> str:
    """添加形状。kind: rectangle|rounded|ellipse|line|text。fill/line_color 为 RRGGBB 或 none。可带文字。"""
    return _j(_c().add_shape(slide=slide, kind=kind, x=x, y=y, w=w, h=h, fill=fill,
                             line_color=line_color, line_width=line_width, text=text,
                             font_size=font_size, bold=bold, color=color, name=name))


@mcp.tool
def add_image(slide: int, path: str, x: float, y: float, w: float = None,
              h: float = None, name: str = None) -> str:
    """插入图片 (png/jpg)。只给 w 或 h 之一时按原纵横比补全; 都不给按原始尺寸。"""
    return _j(_c().add_image(slide=slide, path=path, x=x, y=y, w=w, h=h, name=name))


@mcp.tool
def add_table(slide: int, rows: list, x: float, y: float, w: float, h: float,
              header_bold: bool = True, header_fill: str = None,
              font_size: float = 12, border: bool = True, border_color: str = None,
              name: str = None) -> str:
    """添加表格。rows 为二维字符串数组 (第一行表头)。header_fill 表头底色 RRGGBB; border 细边框默认开。"""
    return _j(_c().add_table(slide=slide, rows=rows, x=x, y=y, w=w, h=h,
                             header_bold=header_bold, header_fill=header_fill,
                             font_size=font_size, border=border, border_color=border_color,
                             name=name))


@mcp.tool
def render_slide(slide: int) -> str:
    """把指定页渲染为 PNG 并返回绝对路径 —— 用 Read 工具查看图片做视觉自查, 发现问题就地修复再渲。"""
    return _j(_c().render_slide(slide=slide))


@mcp.tool
def set_transition(slide: int, effect: str, duration: float = 0.6) -> str:
    """设置页面切换效果。effect 可选: """ + _TRANS_DOC + """ (或 none)。duration 秒。"""
    return _j(_c().set_transition(slide=slide, effect=effect, duration=duration))


@mcp.tool
def add_animation(slide: int, shape: str, effect: str, trigger: str = "onclick",
                  duration: float = None, delay: float = 0.0) -> str:
    """给形状加动画。""" + _EFFECTS_DOC + """
    trigger: onclick | with_previous | after_previous。duration 秒(默认0.5), delay 秒。
    生成的动画导出 pptx 后是真实 PowerPoint 预设 (presetID), PowerPoint/WPS 可正常播放编辑。"""
    return _j(_c().add_animation(slide=slide, shape=shape, effect=effect, trigger=trigger,
                                 duration=duration, delay=delay))


@mcp.tool
def list_animations(slide: int) -> str:
    """列出该页动画: 形状/触发/预设/延时。"""
    return _j(_c().list_animations(slide=slide))


@mcp.tool
def remove_animation(slide: int, shape: str) -> str:
    """移除该形状的全部动画。"""
    return _j(_c().remove_animation(slide=slide, shape=shape))


@mcp.tool
def shutdown() -> str:
    """完全停止 pptgen 管线 (关文档 + 退 soffice)。排障后手动调用。"""
    global _client
    if _client is not None:
        _client.shutdown()
        _client = None
    return _j({"stopped": True})


if __name__ == "__main__":
    mcp.run()
