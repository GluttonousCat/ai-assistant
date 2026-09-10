# -*- encoding: utf-8 -*-
"""
内容域工具 (2个):
- build_company_profile  上市公司画像 (八大板块, 纯数据只读)
- write_article          公众号文章生成 (LLM 成文, 写文件, 默认不进对话沙盒)
"""
from __future__ import annotations

from typing import Any, Dict

from mcp.registry import REGISTRY
from mcp.spec import ToolError, obj_schema, param


@REGISTRY.tool(
    name="build_company_profile",
    domain="report",
    description=(
        "上市公司画像 (纯数据, 不成文): 一次组装八大板块——公司概况/主营构成/"
        "近5年核心财务/估值分位/盈利预测分歧/预测兑现/风险信号/近期研报观点/量价形态。"
        "用户要'全面了解一家公司'/'做个画像'/'体检'时调用; 画像可作为 write_article "
        "(公众号文章) 与 PPT 生成的素材底座。单板块缺数据自动降级不报错。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "years": param("财务回看年数", "integer", default=5),
    }, ["stock"]),
    examples=["给中际旭创做个画像", "全面看一下宁德时代"],
)
def build_company_profile(stock: str, years: int = 5) -> Dict[str, Any]:
    from skills.content.profile import build_company_profile as _build
    try:
        return _build(stock, years)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"画像组装失败: {e}")


@REGISTRY.tool(
    name="write_article",
    domain="report",
    description=(
        "生成微信公众号文章 (markdown, 存 output/articles/): 两种模式——"
        "stock=股票画像解读 (数据+研报观点成文); topic=研报主题综述 (综合多篇观点对比)。"
        "合规内置: 券商观点仅转述并标注机构、数据标注来源、文末免责声明。"
        "**LLM 生成耗时 30~90s, 且写文件 (写类工具)**, 只读沙盒下不可用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码 (画像模式)", "string"),
        "topic": param("主题关键词 (综述模式, 如 '光模块')", "string"),
        "years": param("画像模式财务回看年数", "integer", default=5),
    }),
    examples=["给中际旭创写篇公众号文章", "写一篇光模块研报观点综述"],
    read_only=False,
    notes="产物 output/articles/YYYYMMDD_*.md; 发布公众号为人工操作 (草稿箱API后续可选)",
)
def write_article(stock: str = "", topic: str = "", years: int = 5) -> Dict[str, Any]:
    from skills.content.article import write_stock_article, write_topic_article
    try:
        if topic:
            return write_topic_article(topic)
        if stock:
            return write_stock_article(stock, years)
        raise ToolError("stock 与 topic 至少提供一个")
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"文章生成失败: {e}")
