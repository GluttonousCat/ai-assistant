"""
个股 Alpha 挖掘 Skill (v1) — 预期差四象限

框架: 市场预期端(券商盈利预测分歧/方向) x 基本面端(财务动量) x 定价端(估值分位) x 观点端(研报)
纯存量数据 (report_forecast / fina_indicator / daily_basic / report_meta), 不依赖年报全文.

用法(命令行验证):
    python -m agent.beta_alpha.skills.alpha 中际旭创
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core.logger import get_logger
from core.llm.client import get_agent_llm
from agent.skills.base import BaseSkill, SkillContext
from storage.pg import PgClient
from agent.beta_alpha.prompts import ALPHA_SUMMARY_PROMPT

logger = get_logger(__name__)

# 盈利预测单位 -> 元 归一因子 (增速%/倍 不换算); 未知单位按原值 (保守)
_UNIT_FACTOR = {
    "亿元": 1e8, "万亿元": 1e12, "万元": 1e4, "百万元": 1e6,
    "亿元/年": 1e8, "元": 1.0, "%": 1.0, "倍": 1.0, "倍数": 1.0,
}


def to_yuan(value: float, unit: Optional[str]) -> float:
    """盈利预测值按单位归一到元 (可测纯函数)"""
    return float(value) * _UNIT_FACTOR.get((unit or "").strip(), 1.0)


def classify_drift(first: Optional[float], last: Optional[float],
                   tol: float = 3.0) -> Optional[str]:
    """前后半段预测均值对比 -> 上修/下修/持平 (±tol% 内算持平; 数据不足 None)"""
    if first is None or last is None or first == 0:
        return None
    drift = (last - first) / abs(first) * 100
    if drift > tol:
        return "上修"
    if drift < -tol:
        return "下修"
    return "持平"


def edge_trend(latest: Optional[float], prev: Optional[float],
               diff_pp: float = 5.0) -> Optional[str]:
    """
    相邻报告期同比的边际方向 (纯函数, 供 tests 验证)。
    信号带增速水平语境, 避免高增速被"降速"标签误导:
    - 高位 (>=50%): 高位提速 / 高位降速 / 高位平稳
    - 负增长 (<0%): 负增收窄 / 负增扩大 / 负增持平
    - 其余: 提速 / 降速 / 平稳
    """
    if latest is None or prev is None:
        return None
    diff = latest - prev
    if latest < 0:
        if diff >= diff_pp:
            return "负增收窄"
        if diff <= -diff_pp:
            return "负增扩大"
        return "负增持平"
    base = "提速" if diff >= diff_pp else ("降速" if diff <= -diff_pp else "平稳")
    return f"高位{base}" if latest >= 50 else base


def _resolve_stock(text: str) -> Optional[Tuple[str, str]]:
    """(名称, ts_code); 优先股票词典 (match_stock 返回 (ts_code, 别名)), 再尝试 6 位代码"""
    code: Optional[str] = None
    try:
        from tools.finance.stock_kb import get_stock_kb
        kb = get_stock_kb()
        kb.load()
        hit = kb.match_stock(text)
        if hit:
            code = hit[0]
    except Exception as e:
        logger.warning(f"股票词典匹配失败: {e}")
    if not code:
        m = re.search(r"(\d{6})\.(SZ|SH|BJ)", text.upper())
        if m:
            code = f"{m.group(1)}.{m.group(2)}"
    if not code:
        return None
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT name FROM stock.stock_basic WHERE ts_code=%s", (code,))
    if not row:
        return None
    return row["name"], code


def _load_forecast_divergence(pg, ts_code: str) -> List[Dict]:
    """象限1: 盈利预测分歧度与方向 (同指标同期间内按单位归一后统计)"""
    rows = pg.fetch_all(
        "SELECT rf.forecast_type, rf.forecast_period, rf.forecast_value, "
        "       rf.forecast_unit, rm.publish_date, rm.org_name "
        "FROM fin.report_forecast rf "
        "LEFT JOIN fin.report_meta rm ON rm.report_id = rf.report_id "
        "WHERE rf.ts_code = %s AND rf.forecast_value IS NOT NULL "
        "ORDER BY rf.forecast_period DESC, rm.publish_date",
        (ts_code,),
    )
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df["value"] = df.apply(
        lambda r: to_yuan(r["forecast_value"], r["forecast_unit"]), axis=1)
    out = []
    for (ftype, period), g in df.groupby(["forecast_type", "forecast_period"]):
        if len(g) < 2:
            continue
        vals = g["value"]
        mean = float(vals.mean())
        cv = float(vals.std(ddof=1) / abs(mean)) if mean and len(vals) >= 2 else None
        # 方向: 后半段均值 vs 前半段 (按发布时间)
        half = max(1, len(g) // 2)
        first, last = float(vals.iloc[:half].mean()), float(vals.iloc[-half:].mean())
        direction = classify_drift(first, last)
        out.append({
            "forecast_type": ftype, "forecast_period": str(period),
            "n_forecasts": int(len(g)), "n_orgs": int(g["org_name"].nunique()),
            "mean": mean, "cv": round(cv, 3) if cv is not None else None,
            "direction": direction,
            "drift_pct": round((last - first) / abs(first) * 100, 1) if first else None,
        })
    out.sort(key=lambda x: x["forecast_period"], reverse=True)
    return out[:6]


def _load_momentum(pg, ts_code: str) -> List[Dict]:
    """象限2: 财务动量 (近 8 报告期) + 边际方向标记"""
    rows = pg.fetch_all(
        "SELECT end_date, or_yoy, netprofit_yoy, grossprofit_margin, q_sales_yoy "
        "FROM fin.fina_indicator "
        "WHERE ts_code = %s AND report_type = '1' "
        "ORDER BY end_date DESC LIMIT 8", (ts_code,))
    if not rows:
        return []
    df = pd.DataFrame(rows)
    for c in ("or_yoy", "netprofit_yoy", "grossprofit_margin", "q_sales_yoy"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("end_date")
    recs = df.to_dict("records")
    if len(recs) >= 2:
        last, prev = recs[-1], recs[-2]
        trend = {
            "or_yoy_trend": edge_trend(last.get("or_yoy"), prev.get("or_yoy")),
            "netprofit_yoy_trend": edge_trend(last.get("netprofit_yoy"),
                                              prev.get("netprofit_yoy")),
            "q_sales_yoy_trend": edge_trend(last.get("q_sales_yoy"),
                                            prev.get("q_sales_yoy")),
        }
        gm = df["grossprofit_margin"].dropna()
        trend["gm_trend"] = ("上行" if len(gm) >= 4 and gm.iloc[-2:].mean() > gm.iloc[:-2].mean()
                             else ("下行" if len(gm) >= 4 else None))
        recs[-1]["trend"] = trend
    for r in recs:
        r["end_date"] = str(r["end_date"])
        for k, v in list(r.items()):
            if isinstance(v, float) and pd.notna(v):
                r[k] = round(v, 2)
    return recs


def _load_valuation(pg, ts_code: str) -> Dict:
    """象限3: PE/PB 近 3 年分位 (0=最低 100=最高)"""
    rows = pg.fetch_all(
        "SELECT pe_ttm, pb FROM stock.daily_basic "
        "WHERE ts_code = %s AND trade_date >= current_date - 1095 "
        "ORDER BY trade_date", (ts_code,))
    if not rows:
        return {}

    def _pct(vals: List, key: str) -> Optional[Dict]:
        arr = [float(r[key]) for r in vals
               if r.get(key) is not None and float(r[key]) > 0]
        if len(arr) < 60:
            return None
        latest = arr[-1]
        pct = sum(1 for v in arr if v < latest) / len(arr) * 100
        return {"latest": round(latest, 2), "pct_rank": round(pct, 1)}

    return {"pe_ttm": _pct(rows, "pe_ttm"), "pb": _pct(rows, "pb"),
            "n_days": len(rows)}


def _load_views(pg, ts_code: str, name: str) -> List[Dict]:
    """象限4: 近期研报观点 (标的绑定/标题/tags_targets 三路命中)"""
    rows = pg.fetch_all(
        "SELECT title, org_name, publish_date, analysis_json "
        "FROM fin.report_meta "
        "WHERE (ts_code = %s OR title ILIKE %s OR tags_targets ILIKE %s) "
        "  AND publish_date IS NOT NULL "
        "ORDER BY publish_date DESC LIMIT 5",
        (ts_code, f"%{name}%", f"%{name}%"),
    )
    out = []
    for r in rows:
        rating = core_view = None
        try:
            aj = json.loads(r["analysis_json"] or "{}")
            rating = aj.get("rating")
            cv = aj.get("core_view") or aj.get("summary")
            core_view = (str(cv)[:120] + "…") if cv and len(str(cv)) > 120 else cv
        except Exception:
            pass
        out.append({"title": r["title"], "org": r["org_name"],
                    "date": str(r["publish_date"]), "rating": rating,
                    "core_view": core_view})
    return out


class AlphaSkill(BaseSkill):
    """个股预期差挖掘: 预测分歧 x 财务动量 x 估值分位 x 研报观点"""

    name = "alpha"
    description = ("个股预期差分析: 券商盈利预测分歧度/调整方向、财务边际动量、"
                   "估值分位、研报观点四象限综合解读")
    intent_keywords = ["预期差", "分歧", "拐点", "alpha"]

    def __init__(self, llm: Optional[Any] = None):
        self.llm = llm or get_agent_llm()

    def analyze(self, name: str, ts_code: str) -> Dict:
        with PgClient() as pg:
            forecast = _load_forecast_divergence(pg, ts_code)
            momentum = _load_momentum(pg, ts_code)
            valuation = _load_valuation(pg, ts_code)
            views = _load_views(pg, ts_code, name)
        return {
            "ts_code": ts_code, "name": name,
            "forecast": forecast, "momentum": momentum,
            "valuation": valuation, "views": views,
        }

    def build_summary_prompt(self, res: Dict) -> str:
        return ALPHA_SUMMARY_PROMPT.format(
            stock_name=res["name"], ts_code=res["ts_code"],
            forecast=json.dumps(res["forecast"], ensure_ascii=False, default=str),
            momentum=json.dumps(res["momentum"], ensure_ascii=False, default=str),
            valuation=json.dumps(res["valuation"], ensure_ascii=False, default=str),
            views=json.dumps(res["views"], ensure_ascii=False, default=str),
        )

    @staticmethod
    def build_table(res: Dict) -> Tuple[List[str], List[Dict]]:
        """四象限结果 -> 前端 data 事件平表 (run 与 SSE 共用)"""
        f = res["forecast"][0] if res["forecast"] else None
        m = res["momentum"][-1] if res["momentum"] else {}
        tr = m.get("trend") or {}
        pe, pb = res["valuation"].get("pe_ttm"), res["valuation"].get("pb")
        cols = ["metric", "value", "signal"]
        data = [
            {"metric": "预测覆盖", "value": f"{f['n_forecasts']}份/{f['n_orgs']}家"
                if f else "无覆盖", "signal": f["forecast_period"] if f else "—"},
            {"metric": "预测分歧度CV", "value": f["cv"] if f and f.get("cv") is not None else "—",
             "signal": ("分歧大" if f and f.get("cv") and f["cv"] > 0.3
                        else ("分歧小" if f and f.get("cv") is not None else "—"))},
            {"metric": "预测方向", "value": f["direction"] or "—" if f else "—",
             "signal": f"{f['drift_pct']}%" if f and f.get("drift_pct") is not None else "—"},
            {"metric": "营收同比(累计)", "value": m.get("or_yoy"), "signal": tr.get("or_yoy_trend") or "—"},
            {"metric": "单季营收同比", "value": m.get("q_sales_yoy"),
             "signal": tr.get("q_sales_yoy_trend") or "—"},
            {"metric": "净利同比(累计)", "value": m.get("netprofit_yoy"),
             "signal": tr.get("netprofit_yoy_trend") or "—"},
            {"metric": "毛利率", "value": m.get("grossprofit_margin"),
             "signal": tr.get("gm_trend") or "—"},
            {"metric": "PE(TTM)分位", "value": pe["pct_rank"] if pe else None,
             "signal": f"现值{pe['latest']}" if pe else "数据不足"},
            {"metric": "PB分位", "value": pb["pct_rank"] if pb else None,
             "signal": f"现值{pb['latest']}" if pb else "数据不足"},
        ]
        return cols, data

    def run(self, context: SkillContext) -> SkillContext:
        hit = _resolve_stock(context.user_input)
        if not hit:
            context.error = ("未识别到股票。请带上股票名或代码, "
                             "例如: 中际旭创的预期差 / 300308 的分歧度")
            return context
        name, ts_code = hit
        try:
            res = self.analyze(name, ts_code)
        except Exception as e:
            logger.error(f"AlphaSkill 分析失败: {e}")
            context.error = f"预期差分析失败: {e}"
            return context

        cols, data = self.build_table(res)
        context.result = {
            "ts_code": ts_code, "name": name, "quadrants": res,
            "data": data, "columns": cols, "rows": len(data),
        }
        try:
            context.result["summary"] = self.llm.invoke(
                self.build_summary_prompt(res))
        except Exception as e:
            logger.warning(f"AlphaSkill 综述生成失败: {e}")
            context.result["summary"] = "四象限数据已生成, 但综述生成失败。"
        return context


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "中际旭创的预期差"
    ctx = AlphaSkill()(SkillContext(user_input=text))
    if ctx.error:
        print("ERROR:", ctx.error)
    else:
        import pandas as pd
        print(pd.DataFrame(ctx.result["data"],
                           columns=ctx.result["columns"]).to_string(index=False))
        print("\n---- 综述 ----\n", ctx.result.get("summary", "")[:800])
