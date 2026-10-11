# -*- encoding: utf-8 -*-
"""
每日研报晨报 (daily digest) — 早间爬虫轮末生成, 供用户每日阅读

数据窗口: 昨日 06:00 以来入库的研报 (覆盖昨日 23:00 夜批 + 今晨 07:00 早批)
输出: output/daily_digest/YYYY-MM-DD.md
  今日概览 -> LLM 综述 -> 行业分组清单(标题/机构/标的/评级) -> 盈利预测要点 -> 管线积压

挂载: zsxq_scheduler._run_round 当日首个 run_hour 轮末 (失败不影响主轮结果)
CLI: python -m jobs.daily_digest            # 昨日 06:00 以来的窗口
     python -m jobs.daily_digest --days 3   # 手动回看更大窗口
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)

OUT_DIR = "output/daily_digest"
MAX_LIST_ROWS = 80          # 清单最多列出条数 (超出截断提示)
MAX_LLM_LINES = 40          # LLM 综述输入最多条数 (控 token)


def _window_start(days: int) -> datetime:
    """数据窗口起点: N 天前的 06:00 (默认 1 = 昨日晨批之前, 覆盖夜批+今晨批)"""
    n = datetime.now()
    return (n - timedelta(days=days - 1)).replace(hour=6, minute=0,
                                                  second=0, microsecond=0)


def fetch_reports(since: datetime) -> List[Dict[str, Any]]:
    """窗口内入库的研报 + 关联盈利预测 (按入库时间升序)"""
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT report_id, title, org_name, ts_code, target, industry, "
            "       market, publish_date, analysis_status, analysis_json, "
            "       file_name, extraction_status "
            "FROM fin.report_meta "
            "WHERE created_at >= %s "
            "ORDER BY created_at", (since,))
        forecasts = pg.fetch_all(
            "SELECT report_id, forecast_type, forecast_period, "
            "       forecast_value, forecast_unit "
            "FROM fin.report_forecast WHERE report_id = ANY(%s)",
            ([r["report_id"] for r in rows],)) if rows else []
    fby: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for f in forecasts:
        fby[f["report_id"]].append(f)
    out = []
    for r in rows:
        d = dict(r)
        aj = {}
        try:
            aj = json.loads(r["analysis_json"] or "{}")
        except (ValueError, TypeError):
            pass
        d["rating"] = aj.get("rating") or ""
        d["core_view"] = str(aj.get("core_view") or "")[:120]
        d["forecasts"] = fby.get(r["report_id"], [])
        out.append(d)
    return out


def _target_price(r: Dict[str, Any]) -> Optional[str]:
    for f in r["forecasts"]:
        if f["forecast_type"] == "target_price" and f["forecast_value"]:
            return f"{float(f['forecast_value']):g}{f['forecast_unit'] or ''}"
    return None


def _llm_summary(reports: List[Dict[str, Any]]) -> str:
    """LLM 综述: 每行一条(标题|机构|标的|评级|核心观点), 输出 3 段内中文综述"""
    lines = []
    for r in reports[:MAX_LLM_LINES]:
        parts = [f"《{r['title'] or r['file_name']}》",
                 r["org_name"] or "未知机构",
                 (r["target"] or r["ts_code"] or "无标的"),
                 f"评级:{r['rating']}"
                 if r["rating"] and r["rating"] != "无" else "未评级"]
        if r["core_view"]:
            parts.append(r["core_view"])
        lines.append(" | ".join(str(p) for p in parts))
    if not lines:
        return "本期无新增研报。"
    prompt = (
        "你是投研晨报编辑。以下是最近一个窗口内新入库的券商研报清单, "
        "每行格式: 标题 | 机构 | 标的 | 评级 | 核心观点(节选):\n\n"
        + "\n".join(lines)
        + "\n\n请写「今日研报综述」, 要求:\n"
          "1) 共 2~3 段、总计 300~500 字, 简体中文;\n"
          "2) 第一段概括今日覆盖的行业与主题脉络;\n"
          "3) 第二段点名值得关注的个股观点与预测变化(带机构名);\n"
          "4) 若信息不足以支撑某点, 直接略过, 严禁编造清单外内容;\n"
          "5) 直接输出综述正文, 不要任何前后缀或标题。"
    )
    from harness.llm import get_agent_llm
    for attempt, timeout in enumerate((120.0, 180.0), 1):
        try:
            text = (get_agent_llm(timeout=timeout).invoke(prompt) or "").strip()
            if text:
                return text
            logger.warning(f"晨报综述 LLM 返回空 ({attempt}/2)")
        except Exception as e:
            logger.warning(f"晨报综述 LLM 调用失败 ({attempt}/2): {e}")
    return "（LLM 综述暂不可用, 请直接阅读下方清单; 完整清单与预测不受影响。）"


def generate(days: int = 1) -> Path:
    """生成晨报 MD 并落盘, 返回文件路径"""
    get_config()   # 灌 .env 进程环境 (storage 直读 env; 未先调会连默认地址)
    since = _window_start(days)
    reports = fetch_reports(since)
    now = datetime.now()
    root = Path(get_config().root_dir)

    # ---- 统计 ----
    orgs = {r["org_name"] for r in reports if r["org_name"]}
    targets = {r["ts_code"] or r["target"] for r in reports
               if r["ts_code"] or r["target"]}
    markets = Counter(r["market"] or "未分类" for r in reports)
    industries = Counter(r["industry"] or "未分类" for r in reports)
    analyzed = sum(1 for r in reports if r["analysis_status"] == "done")

    # ---- LLM 综述 ----
    summary = _llm_summary(reports) if reports else "本期无新增研报。"

    # ---- MD 组装 ----
    md: List[str] = []
    md.append(f"# 研报晨报 · {now:%Y-%m-%d}\n")
    md.append(f"> 生成于 {now:%H:%M} | 覆盖 {since:%Y-%m-%d %H:%M} 以来入库研报 "
              f"(含昨夜批+今晨批)\n")

    md.append("## 今日概览\n")
    md.append(f"- 新增研报 **{len(reports)}** 篇 | 覆盖机构 **{len(orgs)}** 家 | "
              f"涉及标的 **{len(targets)}** 只 | 深度提取完成 **{analyzed}** 篇")
    if reports:
        md.append("- 市场分布: " + " / ".join(
            f"{k} {v}" for k, v in markets.most_common()))
        md.append("- 行业分布: " + " / ".join(
            f"{k} {v}" for k, v in industries.most_common(8)))
    md.append("")

    md.append("## 今日综述\n")
    md.append(summary + "\n")

    # ---- 行业分组清单 ----
    md.append("## 研报清单 (按行业)\n")
    if not reports:
        md.append("本窗口无新增研报。\n")
    else:
        by_ind: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in reports:
            by_ind[r["industry"] or "未分类"].append(r)
        shown = 0
        for ind in sorted(by_ind, key=lambda k: -len(by_ind[k])):
            if shown >= MAX_LIST_ROWS:
                break
            md.append(f"### {ind} ({len(by_ind[ind])})\n")
            for r in by_ind[ind]:
                if shown >= MAX_LIST_ROWS:
                    break
                shown += 1
                bits = [f"**《{r['title'] or r['file_name']}》**"]
                seg = r["org_name"] or "未知机构"
                if r["target"] or r["ts_code"]:
                    seg += f" | {r['target'] or r['ts_code']}"
                if r["rating"] and r["rating"] != "无":
                    seg += f" | 评级: {r['rating']}"
                tp = _target_price(r)
                if tp:
                    seg += f" | 目标价: {tp}"
                md.append(f"- {' — '.join(bits + [seg])}")
            md.append("")
        if len(reports) > shown:
            md.append(f"…(其余 {len(reports) - shown} 篇见研报中心)\n")

    # ---- 盈利预测要点 ----
    md.append("## 盈利预测要点\n")
    fc_rows = []
    for r in reports:
        for f in r["forecasts"]:
            if f["forecast_type"] in ("target_price",) or not f["forecast_value"]:
                continue
            fc_rows.append(f"- **{r['target'] or r['ts_code'] or r['title'][:20]}**: "
                           f"{r['org_name'] or '未知'} {f['forecast_period']} "
                           f"{f['forecast_type']} "
                           f"{float(f['forecast_value']):g}{f['forecast_unit'] or ''}")
    md.extend(fc_rows[:30] or ["本窗口无新增盈利预测。"])
    if len(fc_rows) > 30:
        md.append(f"…(共 {len(fc_rows)} 条)")
    md.append("")

    # ---- 管线积压 ----
    from storage.pg import PgClient
    with PgClient() as pg:
        backlog = {b["extraction_status"]: b["c"] for b in pg.fetch_all(
            "SELECT extraction_status, count(*) AS c FROM fin.report_meta "
            "GROUP BY 1")}
    md.append("## 管线积压 (待消化)\n")
    md.append(f"- pending {backlog.get('pending', 0)} | "
              f"pending_ocr {backlog.get('pending_ocr', 0)} | "
              f"failed {backlog.get('failed', 0)}\n")

    out_dir = root / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{now:%Y-%m-%d}.md"
    path.write_text("\n".join(md), encoding="utf-8")
    logger.info(f"研报晨报已生成: {path} ({len(reports)} 篇)")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="每日研报晨报生成")
    parser.add_argument("--days", type=int, default=1,
                        help="回看窗口天数 (默认 1 = 昨日 06:00 以来)")
    args = parser.parse_args()
    p = generate(days=args.days)
    print(f"研报晨报: {p}")
