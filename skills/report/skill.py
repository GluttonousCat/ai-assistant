# -*- encoding: utf-8 -*-
"""Report extraction, synthesis and Q&A orchestration.

Field normalization and queued OCR live in their own domain modules.
"""

from __future__ import annotations

from core.json_response import parse_json_response
import json
import re
from typing import Any, Dict, Iterator, List, Optional

from core.config import get_config
from core.logger import get_logger
from tools.finance import report_analysis
from skills.report import normalization as normalize
from skills.report.ocr import queue_ocr

from skills.base import BaseSkill, SkillContext
from skills.report.prompts import (
    REPORT_EXTRACT_PROMPT,
    REPORT_QA_PROMPT,
    REPORT_SUMMARY_PROMPT,
)

logger = get_logger(__name__)

# Max characters of a single report sent to the LLM (controls token cost)
_MAX_TEXT_CHARS = 12000


class ReportSkill(BaseSkill):
    name = "report"
    description = (
        "研报分析: 解读知识星球研报 (A股/美股/港股/宏观/大宗商品), "
        "提取标的/评级/盈利预测, 生成综合解读"
    )
    intent_keywords = [
        "研报",
        "报告",
        "解读",
        "观点",
        "评级",
        "目标价",
        "盈利预测",
        "商品",
        "原油",
        "黄金",
        "铜",
        "宏观",
    ]

    def run(self, context: SkillContext) -> SkillContext:
        params = context.params or {}
        mode = params.get("mode") or self._guess_mode(context.user_input)

        if not self._llm():
            context.error = (
                "研报分析需要 LLM (未配置 OPENAI_API_KEY), 请在 .env 中配置后重启"
            )
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
        from harness.llm import get_agent_llm

        return get_agent_llm()

    @staticmethod
    def _extract_llm():
        """Use the extract-purpose model for structured extraction"""
        if not get_config().openai_api_key:
            return None
        from harness.llm import get_extract_llm

        return get_extract_llm()

    _vision_backfill = staticmethod(queue_ocr)

    def _guess_mode(self, text: str) -> str:
        """When no explicit mode, guess from the input: question-like -> qa, else analyze"""
        t = (text or "").strip()
        if not t:
            return "analyze"
        if (
            t.endswith("?")
            or t.endswith("？")
            or any(w in t for w in ("是什么", "怎么样", "怎么看", "为什么", "吗", "呢"))
        ):
            return "qa"
        return "analyze"

    def _run_extract(self, context: SkillContext, params: Dict[str, Any]) -> None:
        rows = report_analysis.extraction_candidates(
            params.get("report_id"), int(params.get("limit", 1))
        )
        if not rows:
            context.result = {"summary": "没有待分析的研报 (需先爬取并同步到 PG)"}
            return
        processed, failed, queued, discarded = [], [], [], []
        for row in rows:
            rid = row["report_id"]
            if not row.get("content_chars") and not self._vision_backfill(row):
                if row.get("file_path"):
                    queued.append(rid)
                    context.log(f"研报 #{rid} 为图片型 PDF, 已转后台视觉 OCR")
                else:
                    report_analysis.mark_failed(rid)
                    failed.append(rid)
                    context.log(f"研报 #{rid} 无文本且无源文件, 跳过")
                continue
            summary = self._extract_one(row)
            if summary:
                processed.append(rid)
                context.log(f"研报 #{rid} 提取完成")
            else:
                current = report_analysis.load_report(rid)
                (failed if current else discarded).append(rid)
                context.log(f"研报 #{rid} 未生成结果，已标记失败或被去重移除")
        context.result = {
            "summary": f"已分析 {len(processed)} 篇研报；OCR排队 {len(queued)} 篇，失败 {len(failed)} 篇，去重移除 {len(discarded)} 篇",
            "processed": processed,
            "failed": failed,
            "queued_ocr": queued,
            "discarded": discarded,
        }

    def extract_stream(self, report_id: int) -> Iterator[Dict[str, Any]]:
        """Yield load/extract/parse/save stages, model deltas and the committed result.

        The API bridges this synchronous generator to SSE in a worker thread.
        """

        def _ev(t: str, **kw: Any) -> Dict[str, Any]:
            return {"type": t, **kw}

        yield _ev("stage", stage="load", message=f"读取研报 #{report_id}…")
        row = report_analysis.load_report(report_id)
        if not row:
            yield _ev("error", message=f"研报 #{report_id} 不存在")
            return
        title = (row.get("title") or row.get("file_name") or f"#{report_id}").strip()

        if not row.get("content_chars"):
            # Image-type PDF: same as the batch pipeline, hand off to background OCR
            # instead of holding the SSE connection for long
            if row.get("file_path"):
                self._vision_backfill(row)
                yield _ev(
                    "stage",
                    stage="ocr",
                    message="图片型 PDF, 已提交后台视觉 OCR (每册 2-5 分钟)",
                )
                yield _ev(
                    "error",
                    message="正文尚未就绪 (视觉 OCR 进行中), "
                    "完成后会自动回到列表, 可再次点击 AI 分析",
                )
            else:
                report_analysis.mark_failed(row["report_id"])
                yield _ev("error", message="该研报无正文文本且无源文件, 无法分析")
            return

        yield _ev(
            "stage",
            stage="load",
            message=f"正文就绪: {row['content_chars']} 字 · {title[:40]}",
        )

        text = (row.get("content_text") or "")[:_MAX_TEXT_CHARS]
        yield _ev(
            "stage",
            stage="extract",
            message="LLM 结构化提取中 (评级 / 标的 / 盈利预测 / tags)…",
        )
        chunks: List[str] = []
        try:
            # The thinking switch is injected uniformly by LLMClient per config
            # (reasoning blocks are skipped automatically, only body text streams out)
            for chunk in self._extract_llm().stream(
                REPORT_EXTRACT_PROMPT.format(report_text=text)
            ):
                chunks.append(chunk)
                yield _ev("delta", text=chunk)
        except Exception as e:
            yield _ev("error", message=f"LLM 调用失败: {e}")
            return
        raw = "".join(chunks)

        yield _ev(
            "stage",
            stage="parse",
            message="解析 JSON 并规范化 (market/rating 词表约束, 代码校验)…",
        )
        data = self._parse_json(raw)
        if not isinstance(data, dict):
            report_analysis.mark_failed(row["report_id"])
            yield _ev("error", message="LLM 输出无法解析为 JSON, 本篇标记失败 (可重试)")
            return

        summary = self._apply_extract_result(row, data)
        if summary is None:
            # the record was dedup-merged away during extraction
            yield _ev(
                "error",
                message="提取完成, 但该研报在处理期间被去重合并移除 "
                "(同话题保留了另一条记录), 本次结果已丢弃",
            )
            return
        yield _ev(
            "stage",
            stage="save",
            message=f"结果入库: fin.report_meta + fin.report_forecast "
            f"(盈利预测 {summary['forecast_count']} 条)",
        )
        self._after_extract(row["report_id"])
        yield _ev("data", data=summary)

        yield _ev("done")

    def _extract_one(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        text = (row.get("content_text") or "")[:_MAX_TEXT_CHARS]
        if not text.strip():
            report_analysis.mark_failed(row["report_id"])
            return None
        raw = self._extract_llm().invoke(REPORT_EXTRACT_PROMPT.format(report_text=text))
        data = self._parse_json(raw)
        if not isinstance(data, dict):
            report_analysis.mark_failed(row["report_id"])
            return None
        summary = self._apply_extract_result(row, data)
        if summary:
            self._after_extract(row["report_id"])
        return summary

    def _after_extract(self, report_id: int) -> None:
        """Only start dependent work after metadata and forecasts commit."""
        self._spawn_chain_extract(report_id)
        from skills.report.translate import spawn_translate

        spawn_translate(report_id)

    @staticmethod
    def _spawn_chain_extract(report_id: int) -> None:
        """After deep extraction completes, extract industry-chain links in a
        background thread (beta_alpha module; failure does not affect the main flow)"""
        try:
            from skills.beta_alpha.analysis.chain_extract import spawn_chain_extract

            spawn_chain_extract(report_id)
        except Exception as e:
            logger.warning(f"链抽取调度失败 report_id={report_id}: {e}")

    def _apply_extract_result(
        self, row: Dict[str, Any], data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        fields, forecasts, summary = normalize.prepare_extraction(row, data)
        if report_analysis.save_extraction(row["report_id"], fields, forecasts):
            return summary
        logger.info("#%s 提取完成但记录已被去重删除, 结果丢弃", row["report_id"])
        return None

    def _run_analyze(self, context: SkillContext, params: Dict[str, Any]) -> None:
        # explicit params first; then infer from the input (A-share name/code/US
        # ticker/commodity keyword); on failure use the whole sentence as a keyword
        target = (
            params.get("ts_code")
            or params.get("stock")
            or params.get("symbol")
            or params.get("topic")
            or self._first_target(context.user_input)
            or self._keyword_fallback(context.user_input)
        )
        if not target:
            context.result = {
                "summary": "请指明标的或主题, 如: 解读贵州茅台的研报 / 黄金观点 / 英伟达研报"
            }
            return

        reports = report_analysis.matched_reports(target)

        if not reports:
            context.result = {
                "summary": f"'{target}' 暂无已入库研报。可先在研报中心触发结构化提取, "
                f"或等待每日自动抓取任务入库。"
            }
            return

        # prefer the already-extracted analysis_json; extract on the spot when missing
        llm = self._llm()
        reports_payload: List[Dict[str, Any]] = []
        for r in reports:
            data = self._parse_json(r.get("analysis_json") or "")
            if not isinstance(data, dict):
                self._extract_one(r)
                r = report_analysis.load_report(r["report_id"]) or r
                data = self._parse_json(r.get("analysis_json") or "")
                if not isinstance(data, dict):
                    data = {}
            reports_payload.append(
                {
                    "title": r["title"],
                    "market": r.get("market") or (data.get("market") or ""),
                    "org": r.get("org_name") or (data.get("org_name") or ""),
                    "rating": data.get("rating") or "无",
                    "core_view": data.get("core_view") or "",
                    "key_points": data.get("key_points") or [],
                    "commodities": data.get("commodities") or [],
                    "forecasts": data.get("forecasts") or [],
                    "publish_date": str(r.get("publish_date") or ""),
                }
            )

        question = params.get("question") or ""
        summary = llm.invoke(
            REPORT_SUMMARY_PROMPT.format(
                reports_json=json.dumps(reports_payload, ensure_ascii=False, indent=1),
                user_question=question or "(无)",
            )
        )

        context.result = {
            "summary": summary,
            "target": target,
            "report_count": len(reports_payload),
            "reports": reports_payload,
        }

    def _run_qa(self, context: SkillContext, params: Dict[str, Any]) -> None:
        report_id = params.get("report_id")
        question = params.get("question") or context.user_input
        if not report_id:
            context.result = {"summary": "请指明研报 (report_id), 或使用综合解读模式"}
            return
        row = report_analysis.load_report(report_id)
        if not row:
            context.result = {"summary": f"研报 #{report_id} 不存在"}
            return
        text = (row.get("content_text") or "")[:_MAX_TEXT_CHARS]
        if not text.strip():
            context.result = {"summary": "该研报无文本内容"}
            return
        answer = self._llm().invoke(
            REPORT_QA_PROMPT.format(
                title=row["title"], report_text=text, question=question
            )
        )
        context.result = {"summary": answer, "report_id": row["report_id"]}

    def _keyword_fallback(self, text: str) -> Optional[str]:
        """When dictionary/regex both miss: strip common command words and use the
        remainder as a title keyword (2 chars minimum)"""
        t = (text or "").strip()
        for w in (
            "的研报",
            "研报",
            "解读",
            "分析",
            "观点",
            "一下",
            "帮我",
            "看看",
            "报告",
            "怎么样",
            "吗",
            "?",
            "？",
        ):
            t = t.replace(w, "")
        t = t.strip()
        return t if len(t) >= 2 else None

    def _first_target(self, text: str) -> Optional[str]:
        """Find the first stock name/code/commodity word in the input"""
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
        # US ticker (e.g. "英伟达 NVDA" / "NVDA.O")
        m = re.search(r"\b[A-Z]{2,6}(\.[A-Z]{1,2})?\b", (text or ""))
        if m:
            return m.group(0)
        # commodity keywords
        for kw in ("黄金", "原油", "铜", "锂", "稀土", "gold", "oil", "copper"):
            if kw in (text or "").lower():
                return kw
        return None

    _parse_json = staticmethod(parse_json_response)
