"""Preview or upgrade a bounded set of previously analyzed report attachments."""

import argparse
from pathlib import Path
import threading

from core.config import get_config
from core.logger import get_logger
from skills.base import SkillContext
from skills.report.extraction import ANALYSIS_VERSION, saved_analysis_version
from skills.report.skill import ReportSkill
from tools.finance import report_analysis

logger = get_logger(__name__)


def candidates(limit, before_id=None, report_id=None):
    """Return eligible older analyses and the exclusive cursor to continue from."""
    if not 1 <= limit <= 50:
        raise ValueError("limit 必须在 1 到 50 之间")
    selected = []
    cursor = before_id
    while True:
        page = report_analysis.completed_attachment_page(50, cursor, report_id)
        for row in page:
            cursor = row["report_id"]
            version = saved_analysis_version(row["analysis_json"])
            if version >= ANALYSIS_VERSION:
                continue
            if not row["has_body"] and not (row["file_path"] and Path(row["file_path"]).is_file()):
                continue
            selected.append((cursor, row["title"], version))
            if len(selected) == limit:
                return selected, None if report_id else cursor
        if report_id or len(page) < 50:
            return selected, None


def upgrade(report_id):
    context = ReportSkill()(SkillContext(params={
        "mode": "extract", "report_id": report_id, "upgrade_only": True,
    }))
    # The service keeps these jobs alive after returning. A standalone invocation
    # must wait for its OCR continuation, followed by the post-commit workers.
    for prefix in ("ocr", "chain-extract", "translate"):
        for worker in threading.enumerate():
            if worker.name == f"{prefix}-{report_id}":
                worker.join()
    if context.error:
        raise RuntimeError(context.error)
    result = context.result or {}
    if report_id in result.get("processed", []):
        return "已升级"
    if report_id in result.get("skipped", []):
        return "已跳过（版本或状态已更新）"
    if report_id in result.get("queued_ocr", []):
        latest = report_analysis.load_report(report_id) or {}
        if saved_analysis_version(latest.get("analysis_json")) >= ANALYSIS_VERSION:
            return "已升级（OCR）"
    raise RuntimeError(result.get("summary") or "未保存新版本")


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"历史研报升级到 v{ANALYSIS_VERSION}，默认只读预览")
    parser.add_argument("--limit", type=int, default=5, help="最多选择的研报篇数（1–50，默认 5）")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--before-id", type=int, help="按 ID 降序，从该 ID 之前续查")
    scope.add_argument("--report-id", type=int, help="仅预览或重试该篇历史研报")
    parser.add_argument("--apply", action="store_true", help="逐篇调用模型并保存新版分析")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 50 or any(x is not None and x <= 0 for x in (args.report_id, args.before_id)):
        parser.error("limit 须为 1–50，ID 须为正整数")
    get_config()
    selected, cursor = candidates(args.limit, args.before_id, args.report_id)
    print(f"{'执行升级' if args.apply else '只读预览'} · 候选 {len(selected)} 篇")
    failed = []
    for rid, title, version in selected:
        print(f"#{rid} · {'早期版本' if not version else f'v{version}'} → v{ANALYSIS_VERSION} · {title}", flush=True)
        if args.apply:
            try:
                print(f"#{rid} {upgrade(rid)}", flush=True)
            except Exception as exc:
                logger.exception("历史研报 #%s 升级失败", rid)
                print(f"#{rid} 升级失败 · {exc} · 可用 --report-id {rid} --apply 重试", flush=True)
                failed.append(rid)
    if cursor:
        print(f"继续向前查询使用 --before-id {cursor}")
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
