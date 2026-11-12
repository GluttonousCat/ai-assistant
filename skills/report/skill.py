# -*- encoding: utf-8 -*-
"""
Report Skill - report analysis (M4: zsxq report interpretation, multi-market)

Three working modes (decided by user_input / params):
1. extract  Given a report_id (or auto-pick unanalyzed reports), LLM structured
            extraction: backfills report_meta.ts_code / market / report_type,
            forecasts go into fin.report_forecast
            (A-shares link to Tushare via ts_code; non-A-share targets such as
            US/HK stocks and commodities are also stored and kept)
2. analyze  Given a target (A-share name/code/US ticker/commodity name), aggregate
            all its extracted reports and generate a synthesized interpretation
3. qa       Given report_id + question, answer based on a single report's content

Returns an explicit error when OPENAI_API_KEY is missing (this skill depends on
the LLM, no rule-based degradation).
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Dict, Iterator, List, Optional

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from storage.pg_schema import ensure_report_analysis_columns
from core.helpers import normalize_org_name
from skills.base import BaseSkill, SkillContext
from skills.report.prompts import (
    REPORT_EXTRACT_PROMPT, REPORT_QA_PROMPT, REPORT_SUMMARY_PROMPT,
)

logger = get_logger(__name__)

# Max characters of a single report sent to the LLM (controls token cost)
_MAX_TEXT_CHARS = 12000

# market whitelist
MARKETS = {"A股", "H股", "台股", "日股", "韩股", "美股", "欧洲股", "东南亚股", "宏观", "商品", "行业", "其他"}


# The single source of truth for the report-table incremental column DDL
# (analysis_json/market/tags_*/forecast.symbol etc.) lives in
# storage.pg_schema.DDL_REPORT_ANALYSIS_STATEMENTS; columns are added uniformly
# by ensure_report_analysis_columns() (analysis_status: none/done/failed/no_llm).

# Vision OCR concurrency cap: backlog files already on disk breeze through the
# download loop (no network request, no sleep), so instantly spawning a dozen
# OCR threads risks overwhelming the qwen gateway — a semaphore throttles them,
# the excess queue up (while queued the status is already pending_ocr, and the
# idempotent guard prevents duplicate submission)
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

    # ---------- Entry ----------
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
        from harness.llm import get_agent_llm
        return get_agent_llm()

    @staticmethod
    def _extract_llm():
        """Use the extract-purpose model for structured extraction"""
        if not get_config().openai_api_key:
            return None
        from harness.llm import get_extract_llm
        return get_extract_llm()

    @staticmethod
    def _vision_backfill(row: Dict[str, Any]) -> bool:
        """
        Vision OCR backfill for image-type PDFs: when a local file exists but
        content_chars=0, render pages -> recognize via the vision model ->
        write content_text back.
        Returns whether text was successfully obtained.
        """
        path = row.get("file_path")
        if not path or row.get("content_chars"):
            return bool(row.get("content_chars"))
        if row.get("extraction_status") == "pending_ocr":
            # an OCR task is already running/queued: do not spawn a duplicate thread (idempotent)
            return False
        # Vision OCR is slow (2-5 minutes per report): run in a background thread,
        # do not block the current extraction batch.
        # Status becomes pending_ocr; the next extraction/analysis round picks it up naturally.
        import threading
        rid, fname = row["report_id"], row.get("file_name") or path
        from storage.pg import PgClient as _Pg
        with _Pg() as _pg:
            _pg.execute(
                "UPDATE fin.report_meta SET extraction_status='pending_ocr' "
                "WHERE report_id=%s", (rid,))

        def _job():
            from tools.pdf import ocr_pdf
            from harness.llm import get_vision_llm
            from skills.report.prompts import VISION_OCR_PROMPT
            try:
                with _OCR_SEMAPHORE:  # concurrency throttle: at most 4 reports OCR'd at once
                    text = ocr_pdf(path, prompt=VISION_OCR_PROMPT, max_pages=10,
                                   vision_llm=get_vision_llm())
                from storage.pg import PgClient as _Pg
                ok = False
                with _Pg() as _pg:
                    if text:
                        n = _pg.execute(
                            "UPDATE fin.report_meta SET content_text=%s, content_chars=%s, "
                            "extraction_status='extracted' WHERE report_id=%s",
                            (text, len(text), rid))
                        if n == 0:
                            # record was deleted by merge dedup during OCR — discard, do not continue extraction
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
                # OCR done -> immediately continue with single-report deep extraction
                # (previously it waited for the next scheduler round, up to 12h;
                # at this point content_chars>0 so the vision branch cannot re-enter, no recursion risk)
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
        """When no explicit mode, guess from the input: question-like -> qa, else analyze"""
        t = (text or "").strip()
        if not t:
            return "analyze"
        if t.endswith("?") or t.endswith("？") or any(
            w in t for w in ("是什么", "怎么样", "怎么看", "为什么", "吗", "呢")
        ):
            return "qa"
        return "analyze"

    # ---------- Mode 1: structured extraction ----------
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
                # auto-pick unanalyzed reports (judged by body text; extraction_status
                # may not be backfilled); image-type PDFs with a file but no text are
                # included too (vision OCR backfill happens before extraction)
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
                    # Image-type PDF: vision-model OCR goes to a background thread
                    # (2-5 minutes), the next round picks it up automatically.
                    # Only "no file to OCR" is marked failed; queued OCR keeps status
                    # none, otherwise it would never be extracted again.
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

    # ---------- Mode 1b: single-report AI analysis (streaming, for the SSE endpoint) ----------
    def extract_stream(self, report_id: int) -> Iterator[Dict[str, Any]]:
        """
        Single-report AI analysis, yielding one event per Agent-flow stage
        (for /api/reports/{id}/analyze/stream):
          {type: stage, stage, message}   pipeline stage (load body / LLM extract / parse & normalize / persist)
          {type: delta, text}             raw LLM output chunk by chunk (multiple, streamed into the dialog)
          {type: data,  data}             structured result summary (same as what is persisted)
          {type: error, message}          fatal stop (nothing yielded afterwards; the API layer sends done)
          {type: done}                    normal end
        Synchronous generator: the API layer runs it in a worker thread, events go
        to SSE through a queue.
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
                # Image-type PDF: same as the batch pipeline, hand off to background OCR
                # instead of holding the SSE connection for long
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
                # The thinking switch is injected uniformly by LLMClient per config
                # (reasoning blocks are skipped automatically, only body text streams out)
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
                # the record was dedup-merged away during extraction
                yield _ev("error", message="提取完成, 但该研报在处理期间被去重合并移除 "
                                           "(同话题保留了另一条记录), 本次结果已丢弃")
                return
            yield _ev("stage", stage="save",
                      message=f"结果入库: fin.report_meta + fin.report_forecast "
                              f"(盈利预测 {summary['forecast_count']} 条)")
            self._spawn_chain_extract(row["report_id"])
            from skills.report.translate import spawn_translate
            spawn_translate(row["report_id"])
            yield _ev("data", data=summary)

        yield _ev("done")

    def _extract_one(self, pg: PgClient, row: Dict[str, Any]) -> None:
        text = (row.get("content_text") or "")[:_MAX_TEXT_CHARS]
        if not text.strip():
            pg.execute(
                "UPDATE fin.report_meta SET analysis_status='failed' WHERE report_id=%s",
                (row["report_id"],))
            return

        # The thinking switch and max_tokens are injected uniformly by LLMClient
        # per config (currently the strongest thinking mode is globally on)
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
        # background translation of the English-original body -> content_text_cn
        # (Chinese bodies are skipped idempotently; failure does not affect the main flow)
        from skills.report.translate import spawn_translate
        spawn_translate(row["report_id"])

    @staticmethod
    def _spawn_chain_extract(report_id: int) -> None:
        """After deep extraction completes, extract industry-chain links in a
        background thread (beta_alpha module; failure does not affect the main flow)"""
        try:
            from skills.beta_alpha.analysis.chain_extract import spawn_chain_extract
            spawn_chain_extract(report_id)
        except Exception as e:
            logger.warning(f"链抽取调度失败 report_id={report_id}: {e}")

    def _apply_extract_result(self, pg: PgClient, row: Dict[str, Any],
                              data: Dict[str, Any]) -> Dict[str, Any]:
        """
        LLM-extracted JSON -> normalize -> write back to report_meta + report_forecast.
        (shared by both the extract / extract_stream paths; returns the structured
        summary for the frontend)
        """

        # normalize
        market = str(data.get("market") or "其他").strip()
        if market not in MARKETS:
            market = "其他"
        ts_codes = self._normalize_codes(data.get("ts_codes") or [])
        symbols = self._normalize_symbols(data.get("symbols") or [])
        commodities = [str(c)[:32] for c in (data.get("commodities") or [])][:10]
        # multi-dimensional classification tags (stored comma-separated in columns;
        # the full structure is kept in analysis_json)
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

        # backfill metadata (write only when the existing value is empty)
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
        # publish_date fallback: prefer the zsxq announce time (written by sync);
        # only when missing, backfill the date the LLM extracted from the report body
        # — never use the crawl/ingest time
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
        # display title: broker + original file name (keep the original name, no re-splitting)
        # for the broker prefer the already-normalized Chinese org_name in the DB
        # (LLM Analysis vocabulary), otherwise normalize the LLM-extracted value
        # — otherwise English originals extract "J.P. Morgan" etc. into the title
        # (this once polluted titles with English)
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
            # the record was deleted as a merge duplicate while OCR/deep extraction
            # was running — discard the result and never continue inserting
            # forecasts (would violate the foreign key; once caused "OCR 后自动提取失败")
            logger.info(f"#{row['report_id']} 提取完成但记录已被去重删除, 结果丢弃")
            return None

        # forecast data: delete old rows first, then insert (idempotent rerun)
        # (this block was once dead code sitting after _display_title's return —
        # forecasts never got persisted; it has been moved back)
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

    # ---------- Display title: broker + original file name ----------
    @staticmethod
    def _join_tags(items: Any, limit: int = 6) -> str:
        """Tag list -> comma-separated string (dedupe/drop empties/truncate)"""
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
        Build the 'broker-original file name' display title.
        - the broker comes from the LLM-extracted org_name (missing -> leave the title alone);
        - the original file name is kept as-is (minus any .pdf suffix);
        - if the file name already starts with the broker, do not prepend it again.
        """
        org = (str(org or "")).strip()
        name = (orig_name or "").strip()
        if name.lower().endswith(".pdf"):
            name = name[:-4]
        if not org or not name:
            return (name or orig_name or "")[:200]
        # file name already carries the broker prefix -> just strip ".pdf"
        if name.startswith(org) or name.startswith(f"中文版-{org}"):
            return name[:200]
        return f"{org}-{name}"[:200]

    # ---------- Mode 2: synthesized interpretation ----------
    def _run_analyze(self, context: SkillContext, params: Dict[str, Any]) -> None:
        # explicit params first; then infer from the input (A-share name/code/US
        # ticker/commodity keyword); on failure use the whole sentence as a keyword
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

        # prefer the already-extracted analysis_json; extract on the spot when missing
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

    # ---------- Mode 3: single-report Q&A ----------
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

    # ---------- Target matching (A-share/US/HK stocks/commodities/macro themes) ----------
    def _match_reports(self, pg: PgClient, target: str):
        """Returns (where_sql, args); (None, None) when nothing matches"""
        t = target.strip()

        # A-share: 6-digit code or exact name match
        ts_code = self._resolve_a_share(pg, t)
        if ts_code:
            return "ts_code = %s", [ts_code]

        # US/HK ticker (e.g. NVDA.O / 0981.HK) or name: symbols LIKE match
        if re.match(r"^[A-Za-z0-9.\-]+$", t) and ("." in t or len(t) <= 6):
            if re.match(r"^[A-Za-z]{1,6}(\.[A-Z]{1,2})?$", t.upper()) or re.match(r"^\d{4,5}\.HK$", t):
                return "symbols ILIKE %s", [f"%{t}%"]

        # commodity/theme keyword: match commodities/key_points in title or analysis_json
        return ("(title ILIKE %s OR analysis_json ILIKE %s)",
                [f"%{t}%", f"%{t}%"])

    def _resolve_a_share(self, pg: PgClient, target: str) -> Optional[str]:
        """Stock name/code -> A-share ts_code (A-share universe only)"""
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
        """When dictionary/regex both miss: strip common command words and use the
        remainder as a title keyword (2 chars minimum)"""
        t = (text or "").strip()
        for w in ("的研报", "研报", "解读", "分析", "观点", "一下", "帮我", "看看",
                  "报告", "怎么样", "吗", "?", "？"):
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

    # ---------- Utilities ----------
    @staticmethod
    def _ensure_columns(pg: PgClient) -> None:
        """Ensure extension columns like tags/market exist (DDL single source of truth: storage.pg_schema)"""
        ensure_report_analysis_columns()

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
        """Non-A-share ticker symbols (NVDA.O / 0981.HK / BABA etc.)"""
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
                # A-share ts_code
                ts_code, symbol = symbol, None
            elif not symbol:
                if ts_codes:
                    ts_code = ts_codes[0]
                elif market != "A股":
                    symbol = ""  # targetless macro/commodity forecast
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
