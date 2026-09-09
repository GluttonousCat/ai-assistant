# -*- encoding: utf-8 -*-
"""
实体/知识域工具 (3个):
- resolve_stock       股票实体解析 (名称/代码/别名 -> ts_code + 行业档案)
- get_schema          数据字典 (可查哪些表哪些字段)
- trading_calendar    A股交易日历 (最近交易日/T+N/区间/期末)
"""
from __future__ import annotations

from typing import Any, Dict

from mcp.registry import REGISTRY
from mcp.spec import ToolError, obj_schema, param
from mcp.tools._common import resolve_stocks, stock_profile


# ============================================================
# 1. resolve_stock
# ============================================================

@REGISTRY.tool(
    name="resolve_stock",
    domain="entity",
    description=(
        "股票身份与档案查询: 把简称/别名/6位代码解析为标准 ts_code, 返回名称、申万行业、"
        "上市信息。适用场景: ①用户问公司身份/行业归属 (如'宁德时代是哪个行业的'); "
        "②一句话提到多只股票需批量定位; ③别名拿不准需要确认。"
        "注意: 其他股票类工具都直接接受股票名并内部解析, **不要**把本工具当它们的前置步骤。"
        "只做实体定位, 不返回任何行情数据。"),
    params_schema=obj_schema({
        "text": param("包含股票称呼的原文片段, 如 '宁德时代和茅台的最新研报'", "string"),
    }, ["text"]),
    examples=["宁德时代是哪个行业的", "茅台和中际旭创", "600519 是什么公司"],
)
def resolve_stock(text: str) -> Dict[str, Any]:
    hits = resolve_stocks(text)
    if not hits:
        raise ToolError(f"未识别到股票实体: 「{text}」")
    out = []
    for h in hits:
        prof = stock_profile(h["ts_code"])
        out.append({**h, "industry": prof.get("industry"),
                    "industry_l1": prof.get("industry_l1"),
                    "industry_l2": prof.get("industry_l2"),
                    "market": prof.get("market")})
    return {"count": len(out), "stocks": out}


# ============================================================
# 2. get_schema
# ============================================================

@REGISTRY.tool(
    name="get_schema",
    domain="entity",
    description=(
        "查询本平台数据字典: 有哪些表、每个表有哪些字段、中文含义。"
        "当用户问题涉及数据范围确认 (如'你们有没有股东数据')、或需要为 SQL 查询确认字段名时调用。"
        "可传关键词过滤 (如表名片段或业务词, 如 'income'/'估值'/'研报')。"),
    params_schema=obj_schema({
        "keyword": param("过滤关键词, 可选; 匹配表名或字段描述", "string"),
    }),
    examples=["你们有什么数据", "财务三表有哪些字段", "估值数据在哪个表"],
)
def get_schema(keyword: str = "") -> Dict[str, Any]:
    from tools.finance.schema_info import get_schema_info
    text = get_schema_info().to_prompt_text()
    if keyword:
        lines = text.splitlines()
        kept, hit = [], False
        for ln in lines:
            if ln.startswith(("##", "表")) or "表:" in ln:
                hit = keyword.lower() in ln.lower()
            if not keyword or hit:
                kept.append(ln)
        text = "\n".join(kept) if kept else text
    return {"schema_text": text[:12000], "truncated": len(text) > 12000}


# ============================================================
# 3. trading_calendar
# ============================================================

@REGISTRY.tool(
    name="trading_calendar",
    domain="entity",
    description=(
        "A股交易日历: 最近交易日、T+N/T-N 偏移、日期区间内的交易日列表、各期期末交易日。"
        "涉及'昨天/上一交易日/最近一个月/季末'等时间表述需要换算成具体日期时调用。"
        "注意只处理交易日历, 不含财务报告期口径 (年报/季报截止日请用财务工具)。"),
    params_schema=obj_schema({
        "action": param("动作", "string", enum=["last", "offset", "range", "period_ends"]),
        "ref_date": param("基准日 YYYY-MM-DD, 缺省今天", "string"),
        "offset": param("offset 动作的交易日偏移量 (正=未来, 负=过去)", "integer"),
        "start": param("range/period_ends 动作的开始日 YYYY-MM-DD", "string"),
        "end": param("range/period_ends 动作的结束日 YYYY-MM-DD", "string"),
        "freq": param("period_ends 的频率: ME=月末 QE=季末 YE=年末", "string",
                      enum=["ME", "QE", "YE"], default="ME"),
    }, ["action"]),
    examples=["上一个交易日是哪天", "今天往后5个交易日", "上个月有几个交易日"],
)
def trading_calendar(action: str = "last", ref_date: str = "",
                     offset: int = 0, start: str = "", end: str = "",
                     freq: str = "ME") -> Dict[str, Any]:
    from tools.kline.calendar import (
        get_last_trading_day, get_offset_trading_day, get_period_ends,
        get_trading_days,
    )
    if action == "last":
        d = get_last_trading_day(ref_date or None)
        return {"action": action, "last_trading_day": d.isoformat()}
    if action == "offset":
        if not offset:
            raise ToolError("offset 动作必须提供非零 offset 参数")
        d = get_offset_trading_day(ref_date, int(offset))
        return {"action": action, "ref": ref_date, "offset": offset,
                "result": d.date().isoformat()}
    if action == "range":
        if not (start and end):
            raise ToolError("range 动作必须提供 start/end")
        df = get_trading_days(start, end)
        days = [d.date().isoformat() for d in df["date"]]
        return {"action": action, "count": len(days), "days": days[:250],
                "truncated": len(days) > 250}
    if action == "period_ends":
        if not (start and end):
            raise ToolError("period_ends 动作必须提供 start/end")
        ends = [d.date().isoformat() for d in get_period_ends(start, end, freq)]
        return {"action": action, "freq": freq, "ends": ends}
    raise ToolError(f"未知 action: {action}")
