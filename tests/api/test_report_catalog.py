import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
import pytest

from api.deps import get_current_user
from api.reports_api import reports_router
from storage.repositories import reports as repo


def client():
    app = FastAPI()
    app.include_router(reports_router)
    app.dependency_overrides[get_current_user] = lambda: object()
    return TestClient(app)


def test_report_reads_execute_outside_event_loop_and_preserve_filter_contract(
    monkeypatch,
):
    seen = []

    def lookup(**params):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        seen.append(params)
        return {
            "total": 0,
            "page": params["page"],
            "page_size": params["page_size"],
            "items": [],
        }

    monkeypatch.setattr("api.reports_api.catalog.list_reports", lookup)
    response = client().get("/api/reports?keyword=test&market=A股&page=2&page_size=15")
    assert response.status_code == 200 and response.json()["page"] == 2
    assert seen[0]["keyword"] == "test" and seen[0]["market"] == "A股"


def test_detail_not_found_and_original_download_contract(tmp_path, monkeypatch):
    def missing(**kwargs):
        raise LookupError("研报不存在")

    monkeypatch.setattr("api.reports_api.catalog.report_detail", missing)
    assert client().get("/api/reports/7").status_code == 404
    report = tmp_path / "原文.pdf"
    report.write_bytes(b"original report")
    monkeypatch.setattr(
        "api.reports_api.catalog.report_file",
        lambda _: {"file_name": "原文.pdf", "file_path": str(report)},
    )
    response = client().get("/api/reports/7/download")
    assert response.content == b"original report"
    assert (
        "filename*=UTF-8''%E5%8E%9F%E6%96%87.pdf"
        in response.headers["content-disposition"]
    )


def test_catalog_retains_pagination_date_format_and_parameterized_filters(monkeypatch):
    pg = MagicMock()
    pg.__enter__.return_value = pg
    pg.fetch_one.return_value = {"n": 1}
    pg.fetch_all.return_value = [
        {
            "report_id": 7,
            "publish_date": date(2026, 11, 16),
            "created_at": datetime(2026, 11, 16, 9, 12),
        }
    ]
    factory = MagicMock(return_value=pg)
    monkeypatch.setattr(repo, "PgClient", factory)
    keyword = "x' OR 1=1 --"
    result = repo.list_reports(
        keyword=keyword, page=-1, page_size=200, analysis="pending", industry="软件"
    )
    assert result["page"] == 1 and result["page_size"] == 100
    assert result["items"][0]["publish_date"] == "2026-11-16"
    assert result["items"][0]["created_at"] == "2026-11-16 09:12"
    sql, args = pg.fetch_all.call_args.args
    assert keyword not in sql and f"%{keyword}%" in args
    assert "pending_ocr" in sql and "NOT IN ('done','failed')" in sql
    assert args[-2:] == (100, 0)
    factory.assert_called_once_with(read_only=True)


def test_catalog_translation_preference_and_forecasts_remain_unchanged(monkeypatch):
    pg = MagicMock()
    pg.__enter__.return_value = pg
    pg.fetch_one.return_value = {
        "report_id": 7,
        "content_text": "original",
        "content_text_cn": "中文" * 5000,
    }
    pg.fetch_all.return_value = [{"forecast_type": "eps", "forecast_value": 1.2}]
    monkeypatch.setattr(repo, "PgClient", lambda **kwargs: pg)
    result = repo.report_detail(7)
    assert len(result["content_text"]) == 8000 and result["content_text"].startswith(
        "中文"
    )
    assert (
        "content_text_cn" not in result
        and result["forecasts"][0]["forecast_value"] == 1.2
    )


def test_crawler_closes_after_a_failed_background_operation(monkeypatch):
    from importlib import import_module

    module = import_module("api.router")
    crawler = MagicMock()
    crawler.crawl_latest.side_effect = RuntimeError("upstream failed")
    monkeypatch.setattr(module, "get_crawler", lambda _: crawler)

    async def run():
        tasks = BackgroundTasks()
        await module.crawl_latest(SimpleNamespace(group_id="test", per_page=5), tasks)
        with pytest.raises(RuntimeError, match="upstream failed"):
            await tasks()

    asyncio.run(run())
    crawler.close.assert_called_once()


def test_pending_attachment_does_not_expose_topic_commentary_as_original(monkeypatch):
    pg = MagicMock()
    pg.__enter__.return_value = pg
    old = {"report_id": 7, "source": "zsxq_file", "extraction_status": "pending",
           "file_path": "scan.pdf", "content_text": "话题文字", "content_text_cn": "话题译文"}
    pg.fetch_one.side_effect = lambda *_: old.copy()
    pg.fetch_all.return_value = []
    monkeypatch.setattr(repo, "PgClient", lambda **kwargs: pg)
    assert repo.report_detail(7)["content_text"] == ""
    assert repo.report_body(7)["content_text"] == ""
    assert "NOT IN ('zsxq_topic','zsxq_topic_pdf_absent')" in pg.fetch_one.call_args.args[0]
    old.update(file_path=None, extraction_status="extracted", content_text="[[PDF_PAGE:1]]\n已识别原文")
    assert "已识别原文" in repo.report_body(7)["content_text"]
