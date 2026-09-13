# -*- encoding: utf-8 -*-
"""
Report Skill - 研报分析 (M4: 知识星球研报解读, 多市场)

三种工作模式 (由 user_input / params 决定):
1. extract  给定 report_id (或自动挑未分析的研报), LLM 结构化提取:
            回填 report_meta.ts_code / market / report_type, 预测写入 fin.report_forecast
            (A股用 ts_code 关联 Tushare; 美股/港股/商品等非 A 股标的同样入库保留)
2. analyze  给定标的 (A股名/代码/美股代码/商品名), 汇总其全部已提取研报, 生成综合解读报告
3. qa       给定 report_id + question, 基于单篇研报内容问答

无 OPENAI_API_KEY 时返回明确错误 (本 Skill 依赖 LLM, 不做规则降级)。
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Dict, Iterator, List, Optional

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from core.helpers import normalize_org_name
from skills.base import BaseSkill, SkillContext
from skills.report.prompts import (
    REPORT_EXTRACT_PROMPT, REPORT_QA_PROMPT, REPORT_SUMMARY_PROMPT,
)

logger = get_logger(__name__)

# 单篇研报送 LLM 的最大字符数 (控制 token 成本)
_MAX_TEXT_CHARS = 12000

# market 白名单
MARKETS = {"A股", "H股", "台股", "日股", "韩股", "美股", "欧洲股", "东南亚股", "宏观", "商品", "行业", "其他"}


DDL_ANALYSIS_COLS = """
ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS analysis_json TEXT;
ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS analysis_status VARCHAR(16) DEFAULT 'none';
ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS market VARCHAR(8);
ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS symbols TEXT;
ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_industries TEXT;
ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_regions TEXT;
ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_commodities TEXT;
ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_themes TEXT;
ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_targets TEXT;
ALTER TABLE fin.report_forecast ADD COLUMN IF NOT EXISTS symbol VARCHAR(32);
ALTER TABLE fin.report_forecast ADD COLUMN IF NOT EXISTS market VARCHAR(8);
CREATE INDEX IF NOT EXISTS idx_report_meta_market ON fin.report_meta (market);
CREATE INDEX IF NOT EXISTS idx_report_forecast_symbol ON fin.report_forecast (symbol);
"""
# 旧表 ts_code 为 NOT NULL, 非 A 股标的没有 ts_code -> 放宽为可空 (幂等)
DDL_RELAX_TS_CODE = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='fin' AND table_name='report_forecast'
          AND column_name='ts_code' AND is_nullable='NO'
    ) THEN
        ALTER TABLE fin.report_forecast ALTER COLUMN ts_code DROP NOT NULL;
    END IF;
END $$;
"""

# (语句, 表, 列) 三元组: 缺列时才执行, 用于避免无谓 ALTER 锁
# 索引项的第三字段 = 完整索引名 (用于 pg_indexes 存在性判断)
_DDL_ANALYSIS_STATEMENTS = [
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS analysis_json TEXT",
     "report_meta", "analysis_json"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS analysis_status VARCHAR(16) DEFAULT 'none'",
     "report_meta", "analysis_status"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS market VARCHAR(8)",
     "report_meta", "market"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS symbols TEXT",
     "report_meta", "symbols"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_industries TEXT",
     "report_meta", "tags_industries"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_regions TEXT",
     "report_meta", "tags_regions"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_commodities TEXT",
     "report_meta", "tags_commodities"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_themes TEXT",
     "report_meta", "tags_themes"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS tags_targets TEXT",
     "report_meta", "tags_targets"),
    ("ALTER TABLE fin.report_forecast ADD COLUMN IF NOT EXISTS symbol VARCHAR(32)",
     "report_forecast", "symbol"),
    ("ALTER TABLE fin.report_forecast ADD COLUMN IF NOT EXISTS market VARCHAR(8)",
     "report_forecast", "market"),
    ("CREATE INDEX IF NOT EXISTS idx_report_meta_market ON fin.report_meta (market)",
     "report_meta", "idx_report_meta_market"),
    ("CREATE INDEX IF NOT EXISTS idx_report_forecast_symbol ON fin.report_forecast (symbol)",
     "report_forecast", "idx_report_forecast_symbol"),
]
# analysis_status: none / done / failed / no_llm

# 视觉 OCR 并发上限: 本地已有的积压文件会在下载循环里秒过 (无网络请求无睡眠),
# 瞬间起十几个 OCR 线程有打爆 qwen 网关的风险 — 信号量限流, 超出的排队等待
# (排队期间状态已是 pending_ocr, 幂等守卫保证不会被重复提交)
_OCR_SEMAPHORE = threading.BoundedSemaphore(4)

_METRIC_WHITELIST = {"revenue", "net_profit", "eps", "roe", "target_price", "gross_margin"}
_RATING_WHITELIST = {"买入", "增持", "推荐", "持有", "中性", "卖出",
                     "跑赢大盘", "跑输大盘", "无", ""}


class ReportSkill(BaseSkill):
    name = "report"
    description = ("研报分析: 解读知识星球研报 (A股/美股/港股/宏观/大宗商品), "
                   "提取标的/评级/盈利预测, 生成综合解读")
    intent_keywords = ["研报", "报告", "解读", "观点", "评级", "目标价", "盈利预测",
                       "商品", "原油", "黄金", "铜", "宏观"]

    # ---------- 入口 ----------
    def run(self, context: SkillContext) -> SkillContext:
        params = context.params or {}
        mode = params.get("mode") or self._guess_mode(context.user_input)

        if not self._llm():
            context.error = ("研报分析需要 LLM (未配置 OPENAI_API_KEY), "
                             "请在 .env 中配置后重启")
            return context

        try:
            if mode == "extract":
                self._run_extract(context, params)
            elif mode == "analyze":
                self._run_analyze(context, params)
            else:  # qa
                self._run_qa(context, params)
        except Exception as e:
            logger.exception("ReportSkill 执行失败")
            context.error = f"研报分析失败: {e}"
        return context

    @staticmethod
    def _llm():
        if not get_config().openai_api_key:
            return None
        from core.llm.client import get_agent_llm
        return get_agent_llm()

    @staticmethod
    def _extract_llm():
        """结构化提取用 extract 模型"""
        if not get_config().openai_api_key:
            return None
        from core.llm.client import get_extract_llm
        return get_extract_llm()

    @staticmethod
    def _vision_backfill(row: Dict[str, Any]) -> bool:
        """
        图片型 PDF 视觉 OCR 回填: 有本地文件但 content_chars=0 时,
        渲染页面 -> 视觉模型识别 -> 回写 content_text.
        返回是否成功取得文本。
        """
        path = row.get("file_path")
        if not path or row.get("content_chars"):
            return bool(row.get("content_chars"))
        if row.get("extraction_status") == "pending_ocr":
            # 已有 OCR 任务在跑/排队: 不重复起线程 (幂等)
            return False
        # 视觉 OCR 耗时 (每册 2-5 分钟): 放后台线程执行, 不阻塞当前提取批次.
        # 状态置 pending_ocr; 完成后由下轮提取/分析自然接上.
        import threading
        rid, fname = row["report_id"], row.get("file_name") or path
        from storage.pg import PgClient as _Pg
        with _Pg() as _pg:
            _pg.execute(
                "UPDATE fin.report_meta SET extraction_status='pending_ocr' "
                "WHERE report_id=%s", (rid,))

        def _job():
            from tools.pdf import ocr_pdf
            from skills.report.prompts import VISION_OCR_PROMPT
            try:
                with _OCR_SEMAPHORE:  # 并发限流: 同时最多 4 册 OCR
                    text = ocr_pdf(path, prompt=VISION_OCR_PROMPT, max_pages=10)
                from storage.pg import PgClient as _Pg
                ok = False
                with _Pg() as _pg:
                    if text:
                        n = _pg.execute(
                            "UPDATE fin.report_meta SET content_text=%s, content_chars=%s, "
                            "extraction_status='extracted' WHERE report_id=%s",
                            (text, len(text), rid))
                        if n == 0:
                            # OCR 期间记录被 merge 判重删除 —— 丢弃, 不接续提取
                            logger.info(f"#{rid} OCR 完成但记录已被去重删除, 结果丢弃")
                        else:
                            logger.info(f"图片型 PDF 视觉识别完成 #{rid}: {len(text)} 字")
                            ok = True
                    else:
                        _pg.execute(
                            "UPDATE fin.report_meta SET extraction_status='failed', "
                            "analysis_status='failed' WHERE report_id=%s", (rid,))
                if not ok:
                    return
                # OCR 完成 -> 立即接续单篇深度提取 (此前要等下一轮调度, 最长 12h;
                # 此刻 content_chars>0, 不会再进视觉分支, 无递归风险)
                from skills.base import SkillContext
                from skills.report.skill import ReportSkill
                ctx = ReportSkill()(SkillContext(user_input="", params={
                    "mode": "extract", "report_id": rid, "limit": 1}))
                if ctx.error:
                    logger.warning(f"OCR 后自动提取失败 #{rid}: {ctx.error}")
                else:
                    logger.info(f"OCR 后自动提取完成 #{rid}")
            except Exception as e:
                from storage.pg import PgClient as _Pg
                with _Pg() as _pg:
                    _pg.execute(
                        "UPDATE fin.report_meta SET extraction_status='failed', "
                        "analysis_status='failed' WHERE report_id=%s", (rid,))
                logger.warning(f"视觉提取失败 #{rid}: {e}")

        threading.Thread(target=_job, daemon=True, name=f"ocr-{rid}").start()
        logger.info(f"图片型 PDF 转后台视觉识别: {fname}")
        return False

    def _guess_mode(self, text: str) -> str:
        """没有显式 mode 时按输入猜: 带疑问 -> qa, 其余 -> analyze"""
        t = (text or "").strip()
        if not t:
            return "analyze"
        if t.endswith("?") or t.endswith("？") or any(
            w in t for w in ("是什么", "怎么样", "怎么看", "为什么", "吗", "呢")
        ):
            return "qa"
        return "analyze"

    # ---------- 模式 1: 结构化提取 ----------
    def _run_extract(self, context: SkillContext, params: Dict[str, Any]) -> None:
        report_id = params.get("report_id")
        limit = int(params.get("limit", 1))

        with PgClient() as pg:
            self._ensure_columns(pg)
            if report_id:
                rows = pg.fetch_all(
                    "SELECT report_id, title, content_text, content_chars, ts_code, "
                    "report_type, author, org_name, publish_date, market, symbols, file_path, "
                    "extraction_status FROM fin.report_meta WHERE report_id=%s", (int(report_id),))
            else:
                # 自动取未分析的研报 (以正文文本为准, extraction_status 可能未回填);
                # 有文件但无文本的图片型 PDF 也纳入 (提取前会做视觉 OCR 回填)
                rows = pg.fetch_all(
                    "SELECT report_id, title, content_text, content_chars, ts_code, "
                    "report_type, author, org_name, publish_date, market, symbols, file_path, "
                    "extraction_status FROM fin.report_meta "
                    "WHERE (content_chars > 200 OR file_path IS NOT NULL) "
                    "AND (analysis_status IS NULL OR analysis_status NOT IN ('done','failed')) "
                    "ORDER BY ts_code NULLS LAST, report_id LIMIT %s", (limit,))
            if not rows:
                context.result = {"summary": "没有待分析的研报 (需先爬取并同步到 PG)"}
                return

            for row in rows:
                if not row.get("content_chars"):
                    # 图片型 PDF: 视觉模型 OCR 转后台线程 (2-5 分钟), 完成后由下轮自动接上.
                    # 只有"无文件可 OCR"才标 failed; 排队 OCR 的保持 none, 否则永远不再被提取.
                    if not self._vision_backfill(row):
                        if row.get("file_path"):
                            context.log(f"研报 #{row['report_id']} 为图片型 PDF, 已转后台视觉 OCR")
                        else:
                            pg.execute(
                                "UPDATE fin.report_meta SET analysis_status='failed' "
                                "WHERE report_id=%s", (row["report_id"],))
                            context.log(f"研报 #{row['report_id']} 无文本且无源文件, 跳过")
                        continue
                self._extract_one(pg, row)
                context.log(f"研报 #{row['report_id']} 提取完成")

        context.result = {
            "summary": f"已分析 {len(rows)} 篇研报, 结构化结果已入库 (fin.report_forecast)",
            "processed": [r["report_id"] for r in rows],
        }

    # ---------- 模式 1b: 单篇 AI 分析 (流式, 供 SSE 端点) ----------
    def extract_stream(self, report_id: int) -> Iterator[Dict[str, Any]]:
        """
        单篇研报 AI 分析, 按 Agent 流程逐阶段产出事件 (供 /api/reports/{id}/analyze/stream):
          {type: stage, stage, message}   流程阶段 (读取正文 / LLM 提取 / 解析规范化 / 入库)
          {type: delta, text}             LLM 原始输出逐块 (多次, 弹窗内流式展示)
          {type: data,  data}             结构化结果摘要 (与入库内容一致)
          {type: error, message}          失败终止 (之后不再产出; API 层会补发 done)
          {type: done}                    正常结束
        同步生成器: 由 API 层放 worker 线程执行, 事件经队列转 SSE。
        """
        def _ev(t: str, **kw: Any) -> Dict[str, Any]:
            return {"type": t, **kw}

        with PgClient() as pg:
            self._ensure_columns(pg)
            yield _ev("stage", stage="load", message=f"读取研报 #{report_id}…")
            row = pg.fetch_one(
                "SELECT report_id, title, content_text, content_chars, ts_code, "
                "report_type, author, org_name, publish_date, market, symbols, "
                "file_path, extraction_status "
                "FROM fin.report_meta WHERE report_id=%s", (int(report_id),))
            if not row:
                yield _ev("error", message=f"研报 #{report_id} 不存在")
                return
            title = (row.get("title") or row.get("file_name") or f"#{report_id}").strip()

            if not row.get("content_chars"):
                # 图片型 PDF: 与批量链路一致转后台 OCR, 不长时间占住 SSE 连接
                if row.get("file_path"):
                    self._vision_backfill(row)
                    yield _ev("stage", stage="ocr",
                              message="图片型 PDF, 已提交后台视觉 OCR (每册 2-5 分钟)")
                    yield _ev("error", message="正文尚未就绪 (视觉 OCR 进行中), "
                                               "完成后会自动回到列表, 可再次点击 AI 分析")
                else:
                    pg.execute("UPDATE fin.report_meta SET analysis_status='failed' "
                               "WHERE report_id=%s", (row["report_id"],))
                    yield _ev("error", message="该研报无正文文本且无源文件, 无法分析")
                return

            yield _ev("stage", stage="load",
                      message=f"正文就绪: {row['content_chars']} 字 · {title[:40]}")

            text = (row.get("content_text") or "")[:_MAX_TEXT_CHARS]
            yield _ev("stage", stage="extract",
                      message="LLM 结构化提取中 (评级 / 标的 / 盈利预测 / tags)…")
            chunks: List[str] = []
            try:
                # 思考开关由 LLMClient 按 config 统一注入 (reasoning 块自动跳过, 只流出正文)
                for chunk in self._extract_llm().stream(
                        REPORT_EXTRACT_PROMPT.format(report_text=text)):
                    chunks.append(chunk)
                    yield _ev("delta", text=chunk)
            except Exception as e:
                yield _ev("error", message=f"LLM 调用失败: {e}")
                return
            raw = "".join(chunks)

            yield _ev("stage", stage="parse",
                      message="解析 JSON 并规范化 (market/rating 词表约束, 代码校验)…")
            data = self._parse_json(raw)
            if data is None:
                pg.execute("UPDATE fin.report_meta SET analysis_status='failed' "
                           "WHERE report_id=%s", (row["report_id"],))
                yield _ev("error", message="LLM 输出无法解析为 JSON, 本篇标记失败 (可重试)")
                return

            summary = self._apply_extract_result(pg, row, data)
            if summary is None:
                # 提取期间记录被去重合并删除
                yield _ev("error", message="提取完成, 但该研报在处理期间被去重合并移除 "
                                           "(同话题保留了另一条记录), 本次结果已丢弃")
                return
            yield _ev("stage", stage="save",
                      message=f"结果入库: fin.report_meta + fin.report_forecast "
                              f"(盈利预测 {summary['forecast_count']} 条)")
            self._spawn_chain_extract(row["report_id"])
            yield _ev("data", data=summary)

        yield _ev("done")

    def _extract_one(self, pg: PgClient, row: Dict[str, Any]) -> None:
        text = (row.get("content_text") or "")[:_MAX_TEXT_CHARS]
        if not text.strip():
            pg.execute(
                "UPDATE fin.report_meta SET analysis_status='failed' WHERE report_id=%s",
                (row["report_id"],))
            return

        # 思考开关与 max_tokens 由 LLMClient 按 config 统一注入 (当前全局开启最强思考)
        raw = self._extract_llm().invoke(
            REPORT_EXTRACT_PROMPT.format(report_text=text))
        data = self._parse_json(raw)
        if data is None:
            pg.execute(
                "UPDATE fin.report_meta SET analysis_status='failed' WHERE report_id=%s",
                (row["report_id"],))
            return

        self._apply_extract_result(pg, row, data)
        self._spawn_chain_extract(row["report_id"])

    @staticmethod
    def _spawn_chain_extract(report_id: int) -> None:
        """深度提取完成后, 后台线程抽取产业链环节 (beta_alpha 模块; 失败不影响主链路)"""
        try:
            from skills.beta_alpha.analysis.chain_extract import spawn_chain_extract
            spawn_chain_extract(report_id)
        except Exception as e:
            logger.warning(f"链抽取调度失败 report_id={report_id}: {e}")

    def _apply_extract_result(self, pg: PgClient, row: Dict[str, Any],
                              data: Dict[str, Any]) -> Dict[str, Any]:
        """
        LLM 提取 JSON -> 规范化 -> 回写 report_meta + report_forecast.
        (extract / extract_stream 两条链路共用; 返回给前端的结构化摘要)
        """

        # 规范化
        market = str(data.get("market") or "其他").strip()
        if market not in MARKETS:
            market = "其他"
        ts_codes = self._normalize_codes(data.get("ts_codes") or [])
        symbols = self._normalize_symbols(data.get("symbols") or [])
        commodities = [str(c)[:32] for c in (data.get("commodities") or [])][:10]
        # 多维度分类标签 (逗号分隔存列; analysis_json 里保留完整结构)
        tags = data.get("tags") or {}
        tag_industries = self._join_tags(tags.get("industries"))
        tag_regions = self._join_tags(tags.get("regions"))
        tag_commodities = self._join_tags(tags.get("commodities")) or (
            ",".join(commodities) if commodities else "")
        tag_themes = self._join_tags(tags.get("themes"))
        tag_targets = self._join_tags(tags.get("targets")) or (
            ",".join(str(n)[:32] for n in (data.get("company_names") or [])[:5]))
        rating = str(data.get("rating") or "无")
        if rating not in _RATING_WHITELIST:
            rating = "无"
        forecasts = self._normalize_forecasts(
            data.get("forecasts") or [], ts_codes, market)

        # 回填元数据 (原值为空才写)
        primary_symbol = (ts_codes[0] if ts_codes else (symbols[0] if symbols else None))
        sets = ["analysis_json=%s", "analysis_status='done'", "market=%s",
                "tags_industries=%s", "tags_regions=%s", "tags_commodities=%s",
                "tags_themes=%s", "tags_targets=%s"]
        args: List[Any] = [json.dumps(data, ensure_ascii=False), market,
                           tag_industries, tag_regions, tag_commodities,
                           tag_themes, tag_targets]
        if primary_symbol and not row.get("ts_code") and not row.get("symbols"):
            if ts_codes:
                sets.append("ts_code=%s")
                args.append(ts_codes[0])
        if symbols and not row.get("symbols"):
            sets.append("symbols=%s")
            args.append(",".join(symbols))
        if data.get("report_type") and not row.get("report_type"):
            sets.append("report_type=%s")
            args.append(str(data["report_type"])[:16])
        # 发布日期兜底: publish_date 优先用知识星球公布时间 (sync 已写入);
        # 缺失时才回写 LLM 从研报正文提取的日期 —— 绝不用爬取/入库时间
        pd_raw = str(data.get("publish_date") or "").strip()[:10]
        if pd_raw and not row.get("publish_date") and re.match(r"^\d{4}-\d{2}-\d{2}$", pd_raw):
            sets.append("publish_date=%s")
            args.append(pd_raw)
        if data.get("author") and not row.get("author"):
            sets.append("author=%s")
            args.append(str(data["author"])[:64])
        if data.get("org_name") and not row.get("org_name"):
            sets.append("org_name=%s")
            args.append(normalize_org_name(str(data["org_name"]))[:64])
        # 展示标题: 机构 + 原文件名 (保留原名, 不拆切)
        # 机构优先用库里已归一的中文 org_name (LLM Analysis 词表), 其次归一化 LLM 提取值
        # —— 否则英文原版会提取出 "J.P. Morgan" 等英文名拼进标题 (曾致 title 英文污染)
        display = self._display_title(
            (row.get("org_name") or "").strip() or normalize_org_name(data.get("org_name")),
            row.get("file_name") or row.get("title") or "")
        if display and display != row.get("title"):
            sets.append("title=%s")
            args.append(display[:200])
        args.append(row["report_id"])
        n_updated = pg.execute(
            f"UPDATE fin.report_meta SET {', '.join(sets)} WHERE report_id=%s", tuple(args))
        if n_updated == 0:
            # OCR/深度提取耗时期间, 该记录被 merge 判定重复删除 —— 结果丢弃,
            # 绝不能继续插 forecast (否则外键违规, 曾致 "OCR 后自动提取失败")
            logger.info(f"#{row['report_id']} 提取完成但记录已被去重删除, 结果丢弃")
            return None

        # 预测数据: 先删旧再插 (幂等重跑)
        # (此块曾在 _display_title 的 return 之后为死代码, 盈利预测从未入库, 已移回)
        if forecasts:
            pg.execute("DELETE FROM fin.report_forecast WHERE report_id=%s",
                       (row["report_id"],))
            try:
                for f in forecasts:
                    pg.execute(
                        "INSERT INTO fin.report_forecast "
                        "(report_id, ts_code, symbol, market, forecast_type, forecast_period, "
                        " forecast_value, forecast_unit, confidence, raw_text) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (row["report_id"], f["ts_code"], f["symbol"], f["market"],
                         f["metric"], f["period"], f["value"], f["unit"],
                         f["confidence"], f["raw_text"]))
            except Exception as e:
                if "report_forecast_report_id_fkey" in str(e):
                    logger.warning(f"#{row['report_id']} 记录在预测入库瞬间被删除, 丢弃")
                    return None
                raise

        return {
            "report_id": row["report_id"],
            "title": display or row.get("title"),
            "market": market,
            "rating": rating,
            "report_type": str(data.get("report_type") or "")[:16],
            "ts_code": ts_codes[0] if ts_codes else None,
            "symbols": symbols,
            "org_name": str(data.get("org_name") or row.get("org_name") or "")[:64],
            "core_view": str(data.get("core_view") or "")[:200],
            "key_points": [str(k)[:80] for k in (data.get("key_points") or [])[:6]],
            "forecast_count": len(forecasts),
            "forecasts": forecasts,
        }

    # ---------- 展示标题: 机构 + 原文件名 ----------
    @staticmethod
    def _join_tags(items: Any, limit: int = 6) -> str:
        """标签数组 -> 逗号分隔字符串 (去重/去空/截断)"""
        if not isinstance(items, list):
            return ""
        seen: List[str] = []
        for it in items:
            t = str(it).strip()[:24]
            if t and t not in seen:
                seen.append(t)
            if len(seen) >= limit:
                break
        return ",".join(seen)

    @staticmethod
    def _display_title(org: Optional[str], orig_name: str) -> str:
        """
        生成 '机构-原文件名' 展示标题.
        - 机构取 LLM 提取的 org_name (缺省不动标题);
        - 原文件名保留原文 (含 .pdf 后缀去除);
        - 若文件名本身已以该机构开头, 不重复拼接.
        """
        org = (str(org or "")).strip()
        name = (orig_name or "").strip()
        if name.lower().endswith(".pdf"):
            name = name[:-4]
        if not org or not name:
            return (name or orig_name or "")[:200]
        # 文件名已含机构前缀 -> 只去 ".pdf" 即可
        if name.startswith(org) or name.startswith(f"中文版-{org}"):
            return name[:200]
        return f"{org}-{name}"[:200]

    # ---------- 模式 2: 综合解读 ----------
    def _run_analyze(self, context: SkillContext, params: Dict[str, Any]) -> None:
        # 优先显式参数; 其次从输入推断 (A股名/代码/美股代码/商品关键词); 都失败则整句做关键词
        target = (params.get("ts_code") or params.get("stock")
                  or params.get("symbol") or params.get("topic")
                  or self._first_target(context.user_input)
                  or self._keyword_fallback(context.user_input))
        if not target:
            context.result = {"summary": "请指明标的或主题, 如: 解读贵州茅台的研报 / 黄金观点 / 英伟达研报"}
            return

        with PgClient() as pg:
            self._ensure_columns(pg)
            where_sql, args = self._match_reports(pg, target)
            if where_sql is None:
                context.result = {"summary": f"未找到与 '{target}' 相关的研报"}
                return
            reports = pg.fetch_all(
                f"SELECT report_id, title, author, org_name, publish_date, "
                f"report_type, market, analysis_json, content_text, ts_code, symbols "
                f"FROM fin.report_meta WHERE {where_sql} AND content_chars > 0 "
                f"ORDER BY publish_date DESC NULLS LAST LIMIT 10",
                tuple(args))

        if not reports:
            context.result = {
                "summary": f"'{target}' 暂无已入库研报。可先在研报中心触发结构化提取, "
                           f"或等待每日自动抓取任务入库。"}
            return

        # 优先用已提取的 analysis_json; 没有的现场补提取
        llm = self._llm()
        reports_payload: List[Dict[str, Any]] = []
        with PgClient() as pg:
            for r in reports:
                data = None
                if r.get("analysis_json"):
                    try:
                        data = json.loads(r["analysis_json"])
                    except json.JSONDecodeError:
                        data = None
                if data is None:
                    self._extract_one(pg, r)
                    r = self._reload_row(pg, r["report_id"]) or r
                    data = json.loads(r["analysis_json"]) if r.get("analysis_json") else {}
                reports_payload.append({
                    "title": r["title"],
                    "market": r.get("market") or (data.get("market") or ""),
                    "org": r.get("org_name") or (data.get("org_name") or ""),
                    "rating": data.get("rating") or "无",
                    "core_view": data.get("core_view") or "",
                    "key_points": data.get("key_points") or [],
                    "commodities": data.get("commodities") or [],
                    "forecasts": data.get("forecasts") or [],
                    "publish_date": str(r.get("publish_date") or ""),
                })

        question = params.get("question") or ""
        summary = llm.invoke(REPORT_SUMMARY_PROMPT.format(
            reports_json=json.dumps(reports_payload, ensure_ascii=False, indent=1),
            user_question=question or "(无)"))

        context.result = {
            "summary": summary,
            "target": target,
            "report_count": len(reports_payload),
            "reports": reports_payload,
        }

    # ---------- 模式 3: 单篇问答 ----------
    def _run_qa(self, context: SkillContext, params: Dict[str, Any]) -> None:
        report_id = params.get("report_id")
        question = params.get("question") or context.user_input
        if not report_id:
            context.result = {"summary": "请指明研报 (report_id), 或使用综合解读模式"}
            return
        with PgClient() as pg:
            row = pg.fetch_one(
                "SELECT report_id, title, content_text FROM fin.report_meta "
                "WHERE report_id=%s", (int(report_id),))
        if not row:
            context.result = {"summary": f"研报 #{report_id} 不存在"}
            return
        text = (row.get("content_text") or "")[:_MAX_TEXT_CHARS]
        if not text.strip():
            context.result = {"summary": "该研报无文本内容"}
            return
        answer = self._llm().invoke(REPORT_QA_PROMPT.format(
            title=row["title"], report_text=text, question=question))
        context.result = {"summary": answer, "report_id": row["report_id"]}

    # ---------- 目标匹配 (A股/美股/港股/商品/宏观主题) ----------
    def _match_reports(self, pg: PgClient, target: str):
        """返回 (where_sql, args); 无法匹配返回 (None, None)"""
        t = target.strip()

        # A股: 6位代码 或 名称精确匹配
        ts_code = self._resolve_a_share(pg, t)
        if ts_code:
            return "ts_code = %s", [ts_code]

        # 美股/港股代码 (如 NVDA.O / 0981.HK) 或名称: symbols LIKE 匹配
        if re.match(r"^[A-Za-z0-9.\-]+$", t) and ("." in t or len(t) <= 6):
            if re.match(r"^[A-Za-z]{1,6}(\.[A-Z]{1,2})?$", t.upper()) or re.match(r"^\d{4,5}\.HK$", t):
                return "symbols ILIKE %s", [f"%{t}%"]

        # 商品/主题关键词: 标题或 analysis_json 中的 commodities/key_points 匹配
        return ("(title ILIKE %s OR analysis_json ILIKE %s)",
                [f"%{t}%", f"%{t}%"])

    def _resolve_a_share(self, pg: PgClient, target: str) -> Optional[str]:
        """股票名/代码 -> A股 ts_code (仅 A 股库)"""
        t = target.strip().upper()
        if re.match(r"^\d{6}\.(SH|SZ|BJ)$", t):
            return t
        if re.match(r"^\d{6}$", t):
            suffix = "SH" if t.startswith(("6", "9")) else "SZ" if t.startswith(("0", "3")) else "BJ"
            return f"{t}.{suffix}"
        row = pg.fetch_one(
            "SELECT ts_code FROM stock.stock_basic WHERE name=%s", (target.strip(),))
        return row["ts_code"] if row else None

    def _keyword_fallback(self, text: str) -> Optional[str]:
        """词典/正则都没命中时: 去掉常见指令词, 剩余部分作为标题关键词 (最短 2 字)"""
        t = (text or "").strip()
        for w in ("的研报", "研报", "解读", "分析", "观点", "一下", "帮我", "看看",
                  "报告", "怎么样", "吗", "?", "？"):
            t = t.replace(w, "")
        t = t.strip()
        return t if len(t) >= 2 else None

    def _first_target(self, text: str) -> Optional[str]:
        """从输入中找第一个股票名/代码/商品词"""
        from tools.finance.stock_kb import get_stock_kb
        try:
            kb = get_stock_kb()
            kb.load()
            m = kb.match_stock(text or "")
            if m:
                return m[0] if isinstance(m, tuple) else m
        except Exception:
            pass
        m = re.search(r"\d{6}\.(SH|SZ|BJ)", (text or "").upper())
        if m:
            return m.group(0)
        # 美股代码 (如 "英伟达 NVDA" / "NVDA.O")
        m = re.search(r"\b[A-Z]{2,6}(\.[A-Z]{1,2})?\b", (text or ""))
        if m:
            return m.group(0)
        # 商品关键词
        for kw in ("黄金", "原油", "铜", "锂", "稀土", "gold", "oil", "copper"):
            if kw in (text or "").lower():
                return kw
        return None

    # ---------- 工具 ----------
    @staticmethod
    def _ensure_columns(pg: PgClient) -> None:
        """
        确保 tags/market 等扩展列存在.
        【关键】DDL 用独立连接立即提交: ALTER TABLE 持有表级排它锁,
        若放在外层事务里, 锁会持续到事务提交 (LLM 调用期间整表被堵).
        """
        import psycopg2
        from core.config import get_config as _gc
        cfg = _gc().pg_config
        conn = psycopg2.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"],
            password=cfg["password"], dbname=cfg["database"], connect_timeout=10,
            options="-c search_path=stock,fin,public")
        try:
            conn.autocommit = True
            cur = conn.cursor()
            # 缺哪个列才 ALTER 哪个 (幂等 + 避免无谓锁)
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='fin' AND table_name IN ('report_meta','report_forecast')")
            existing = {(r[0], r[1]) for r in cur.fetchall()}
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='fin' "
                "AND tablename IN ('report_meta','report_forecast')")
            existing_idx = {r[0] for r in cur.fetchall()}
            need = []
            for stmt, table, col in _DDL_ANALYSIS_STATEMENTS:
                if stmt.startswith("CREATE INDEX"):
                    if col not in existing_idx:
                        need.append(stmt)
                elif (table, col) not in existing:
                    need.append(stmt)
            if need:
                for stmt in need:
                    cur.execute(stmt)
                logger.info(f"研报表结构补列: {len(need)} 条 DDL")
            # ts_code NOT NULL 放宽 (一次性)
            cur.execute("SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_schema='fin' AND table_name='report_forecast' "
                        "AND column_name='ts_code'")
            row = cur.fetchone()
            if row and row[0] == "NO":
                cur.execute("ALTER TABLE fin.report_forecast ALTER COLUMN ts_code DROP NOT NULL")
            cur.close()
        finally:
            conn.close()

    @staticmethod
    def _reload_row(pg: PgClient, report_id: int) -> Optional[Dict[str, Any]]:
        return pg.fetch_one(
            "SELECT report_id, title, content_text, ts_code, report_type, author, "
            "org_name, publish_date, analysis_json, market, symbols "
            "FROM fin.report_meta WHERE report_id=%s", (report_id,))

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        t = text.strip()
        if t.startswith("```"):
            t = t.strip("`").strip()
            if t.startswith("json"):
                t = t[4:]
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", t, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
        return None

    @staticmethod
    def _normalize_codes(codes: List[Any]) -> List[str]:
        out = []
        for c in codes:
            c = str(c).strip().upper()
            if re.match(r"^\d{6}\.(SH|SZ|BJ)$", c) and c not in out:
                out.append(c)
        return out

    @staticmethod
    def _normalize_symbols(symbols: List[Any]) -> List[str]:
        """非A股标的代码 (NVDA.O / 0981.HK / BABA 等)"""
        out = []
        for s in symbols:
            s = str(s).strip().upper()
            if re.match(r"^[A-Z0-9.\-]{2,12}$", s) and s not in out:
                out.append(s)
        return out[:10]

    def _normalize_forecasts(self, forecasts: List[Any],
                             ts_codes: List[str], market: str) -> List[Dict[str, Any]]:
        out = []
        for f in forecasts:
            if not isinstance(f, dict):
                continue
            metric = str(f.get("metric") or "").strip()
            if metric not in _METRIC_WHITELIST:
                continue
            try:
                value = float(f.get("value"))
            except (TypeError, ValueError):
                continue
            symbol = str(f.get("symbol") or "").strip().upper()
            ts_code = None
            if re.match(r"^\d{6}\.(SH|SZ|BJ)$", symbol):
                # A股 ts_code
                ts_code, symbol = symbol, None
            elif not symbol:
                if ts_codes:
                    ts_code = ts_codes[0]
                elif market != "A股":
                    symbol = ""  # 无标的的宏观/商品预测
                else:
                    continue
            out.append({
                "ts_code": ts_code,
                "symbol": symbol or None,
                "market": market,
                "metric": metric,
                "period": str(f.get("period") or "")[:16],
                "value": value,
                "unit": str(f.get("unit") or "")[:16],
                "confidence": float(f.get("confidence") or 80.0),
                "raw_text": str(f.get("raw_text") or "")[:500],
            })
        return out
