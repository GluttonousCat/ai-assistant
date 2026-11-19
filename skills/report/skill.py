# -*- encoding: utf-8 -*-
"""Report extraction, synthesis and Q&A orchestration.

Field normalization and queued OCR live in their own domain modules.
"""

from __future__ import annotations

from core.json_response import parse_json_response
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterator, List, Optional

from core.config import get_config
from core.logger import get_logger
from tools.finance import report_analysis
from skills.report import normalization as normalize
from skills.report.document import load_document, qa_context
from skills.report.extraction import ANALYSIS_VERSION, InvalidExtraction, analyze_document, saved_analysis_version
from skills.report.ocr import queue_ocr

from skills.base import BaseSkill, SkillContext
from skills.report.prompts import (
    REPORT_QA_PROMPT,
    REPORT_SUMMARY_PROMPT,
)

logger = get_logger(__name__)

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
        processed, failed, queued, discarded, skipped = [], [], [], [], []
        for row in rows:
            rid = row["report_id"]
            if params.get("upgrade_only") and (
                row.get("analysis_status") != "done"
                or saved_analysis_version(row.get("analysis_json")) >= ANALYSIS_VERSION
            ):
                skipped.append(rid)
                continue
            try:
                document = load_document(row)
                if not document.segments and self._queue_missing_pdf(row):
                    queued.append(rid)
                    context.log(f"研报 #{rid} 已转后台视觉 OCR")
                    continue
                summary = self._extract_one(row, document)
            except Exception:
                failed.append(rid)
                logger.exception("研报 #%s 提取异常", rid)
                context.log(f"研报 #{rid} 提取异常")
                continue
            if summary:
                processed.append(rid)
                context.log(f"研报 #{rid} 提取完成")
            else:
                current = report_analysis.load_report(rid)
                changed = not current or any(
                    current.get(key) != row.get(key)
                    for key in ("analysis_json", "file_path", "content_text")
                )
                (discarded if changed else failed).append(rid)
                context.log(f"研报 #{rid} 未保存本次结果")
        context.result = {
            "summary": f"已分析 {len(processed)} 篇研报，OCR排队 {len(queued)} 篇，失败 {len(failed)} 篇，结果弃用 {len(discarded)} 篇",
            "processed": processed,
            "failed": failed,
            "queued_ocr": queued,
            "discarded": discarded,
            "skipped": skipped,
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

        document = load_document(row)
        if not document.segments:
            if self._queue_missing_pdf(row):
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
                report_analysis.mark_failed(row)
                yield _ev("error", message="该研报无正文文本且无源文件, 无法分析")
            return

        yield _ev("stage", stage="load", message=f"正文就绪 · {len(document.segments)} 个片段")
        try:
            for event in analyze_document(document, title, self._extract_llm(), streaming=True):
                if event["type"] == "result":
                    data = event["data"]
                else:
                    yield event
            yield _ev("stage", stage="parse", message="核对出处并规范化评级、代码和预测口径…")
            summary = self._apply_extract_result(row, data)
        except InvalidExtraction as e:
            report_analysis.mark_failed(row)
            yield _ev("error", message=str(e))
            return
        except Exception as e:
            yield _ev("error", message=f"研报分析失败: {e}")
            return
        if summary is None:
            yield _ev(
                "error",
                message="研报在处理期间已被更新或去重，本次结果未覆盖现有记录。",
            )
            return
        yield _ev(
            "stage",
            stage="save",
            message=(f"解读材料及转述预测 {summary['forecast_count']} 条已保存"
                     if summary.get("material_type") == "commentary"
                     else f"分析结果及盈利预测 {summary['forecast_count']} 条已入库"),
        )
        self._after_extract(row["report_id"])
        yield _ev("data", data=summary)

        yield _ev("done")

    def _queue_missing_pdf(self, row: Dict[str, Any]) -> bool:
        path = row.get("file_path") or ""
        if not path.lower().endswith(".pdf") or not Path(path).is_file():
            return False
        self._vision_backfill({**row, "content_chars": 0})
        return True

    def _extract_one(self, row: Dict[str, Any], document=None) -> Optional[Dict[str, Any]]:
        if document is None:
            document = load_document(row)
        title = row.get("title") or row.get("file_name") or ""
        try:
            for event in analyze_document(document, title, self._extract_llm()):
                if event["type"] == "result":
                    data = event["data"]
            summary = self._apply_extract_result(row, data)
        except InvalidExtraction:
            report_analysis.mark_failed(row)
            return None
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
        if data.get("analysis_version") and not any(
            summary.get(key) for key in ("core_view", "key_points", "risks", "evidence", "forecasts")
        ):
            raise InvalidExtraction("未提取到研究观点、可核对的证据或有效预测，原有分析已保留")
        if report_analysis.save_extraction(row, fields, forecasts):
            return summary
        logger.info("#%s 提取完成但记录已被更新或去重, 结果丢弃", row["report_id"])
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
            insights = data.get("research_insights")
            reports_payload.append(
                {
                    "report_id": r["report_id"],
                    "material_type": data.get("material_type") or "unknown",
                    "title": r["title"],
                    "market": r.get("market") or (data.get("market") or ""),
                    "org": r.get("org_name") or (data.get("org_name") or ""),
                    "rating": data.get("rating") or "无",
                    "core_view": data.get("core_view") or "",
                    "key_points": data.get("key_points") or [],
                    "commodities": data.get("commodities") or [],
                    "forecasts": data.get("forecasts") or [],
                    "risks": data.get("risks") or [],
                    "evidence": (data.get("evidence") or [])[:8],
                    "research_insights": insights if isinstance(insights, dict) else {},
                    "partial_coverage": (data.get("coverage") or {}).get("partial")
                    if isinstance(data.get("coverage"), dict) else None,
                    "publish_date": str(r.get("publish_date") or ""),
                }
            )

        question = params.get("question") or ""
        prompt_reports = []
        for report in reports_payload:
            # Keep the full forecasts in the response. Bound each report's share of
            # the synthesis prompt and retain anchors for attribution.
            comparisons = {
                kind: entries[:2] for kind, entries in report["research_insights"].items()
                if isinstance(entries, list) and entries
            }
            prompt_reports.append({
                **report, "research_insights": comparisons,
                "evidence": [] if comparisons else report["evidence"][:4],
                "forecasts": report["forecasts"][:12],
                "total_forecasts": len(report["forecasts"]),
            })
        summary = llm.invoke(
            REPORT_SUMMARY_PROMPT.format(
                reports_json=json.dumps(prompt_reports, ensure_ascii=False),
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
        analysis = self._parse_json(row.get("analysis_json") or "")
        passages = qa_context(load_document(row), question, analysis)
        if not passages:
            context.result = {"summary": "该研报无文本内容"}
            return
        answer = self._llm().invoke(
            REPORT_QA_PROMPT.format(
                title=row["title"], report_text="\n\n".join(p.render() for p in passages),
                material_type=analysis.get("material_type", "unknown") if isinstance(analysis, dict) else "unknown",
                question=question,
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
