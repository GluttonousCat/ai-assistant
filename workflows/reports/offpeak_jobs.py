"""Dispatch queued document work using the normal domain entry points."""

from pathlib import Path

from core.observability import observed, task_progress
from core.offpeak_queue import drain


def _extract_batch(limit, report_id=None):
    from skills.base import SkillContext
    from skills.report.skill import ReportSkill

    context = ReportSkill()(SkillContext(params={
        "mode": "extract", "limit": limit, "report_id": report_id,
    }))
    if context.error:
        raise RuntimeError(context.error)
    if (context.result or {}).get("failed"):
        if report_id:
            raise RuntimeError(f"研报 #{report_id} 未完成分析")
        task_progress(warning="部分研报未完成分析，详见研报提取日志")


def _extract_report(report_id):
    from tools.finance.report_analysis import load_report
    from tools.finance.report_meta_analysis import analyze_report_meta

    row = load_report(report_id)
    if (row and (row.get("file_name") or "").lower().endswith((".pdf", ".doc", ".docx"))
            and row.get("analysis_status") not in ("done", "failed")):
        analyze_report_meta(report_id)
        _extract_batch(1, report_id)


def _annual_repair(path, attempts, dry_run):
    from harness.llm import get_extract_llm
    from tools.cninfo.repair import repair_file

    task_progress(f"年报结构修复 {Path(path).parent.name}/{Path(path).name}")
    result = repair_file(Path(path), get_extract_llm(timeout=120), attempts, dry_run)
    if result.get("error"):
        raise RuntimeError(result["error"])
    if result.get("fails_after"):
        task_progress(warning="部分披露项未通过结构复检")


@observed("offpeak_documents", "闲时文档任务")
def process_pending():
    from skills.beta_alpha.analysis.chain_extract import extract_chain
    from skills.beta_alpha.prompts import CHAIN_EXTRACT_PROMPT
    from workflows.reports.digest import generate
    from workflows.reports.reanalyze import upgrade
    from tools.finance.report_meta_analysis import analyze_pending
    from skills.report.ocr import process_ocr
    from skills.report.translate import translate_report

    completed = drain({
        "report_extract": _extract_report,
        "report_batch": _extract_batch,
        "report_ocr": process_ocr,
        "report_chain": lambda report_id: extract_chain(report_id, CHAIN_EXTRACT_PROMPT, retry_errors=True),
        "report_translate": translate_report,
        "report_metadata": analyze_pending,
        "report_digest": generate,
        "report_upgrade": upgrade,
        "annual_repair": _annual_repair,
    })
    task_progress("闲时任务批次结束", completed=completed)
    return completed
