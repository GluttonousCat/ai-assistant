from unittest.mock import MagicMock

import pytest

from storage.repositories.report_duplicates import _duplicate_rows
from storage.repositories import report_ingestion
from workflows.reports.sync import ZSXQ2PGSync


def test_topic_preserves_unrelated_documents_and_unpaired_translations():
    def row(rid, file_id, name, status="done"):
        return {"report_id": rid, "file_id": file_id, "file_name": name, "analysis_status": status}

    rows = [
        row(1, 10, "公司A.pdf", "none"),
        row(2, 10, "公司A.pdf"),
        row(3, 11, "公司B.docx"),
        row(4, 12, "【中文版】公司A.pdf"),
        row(5, 13, "中文版-公司C.pdf"),
        row(6, 14, "周报.pdf"),
        row(7, 15, "周报.pdf"),
        row(8, 16, "中文版-周报.pdf"),
    ]
    assert {r["report_id"] for r in _duplicate_rows(rows)} == {1, 4}


def test_group_and_topic_sources_restore_late_download_without_using_commentary(tmp_path, monkeypatch):
    enqueue = MagicMock()
    monkeypatch.setattr("workflows.reports.sync.enqueue", enqueue)
    path = tmp_path / "report.pdf"
    sync = object.__new__(ZSXQ2PGSync)
    sync.download_dir = str(tmp_path)
    sync.ready_limit = None
    sync._new_ids = []
    sync.db = MagicMock()
    sync.extractor = MagicMock()
    sync.extractor.extract.return_value = "原始报告"
    states = {}
    writes = []

    def save(row, source, local_path, text, length, status, *, previous_path=None):
        writes.append((source, text, length, status))
        states[row["file_id"]] = {"file_path": local_path, "extraction_status": status}
        return 22 if local_path else None

    monkeypatch.setattr(report_ingestion, "attachment_states", lambda ids: states)
    monkeypatch.setattr(report_ingestion, "save_attachment", save)
    meta = {"file_id": 17, "name": path.name, "topic_id": 3}
    sync._sync_attachments([meta], "zsxq")
    assert writes == [("zsxq", "", 0, "pending")]
    assert not sync._new_ids
    enqueue.assert_not_called()
    path.touch()
    sync._sync_attachments([{**meta, "local_path": str(path)}], "zsxq_file")
    sync._sync_attachments([meta], "zsxq")
    assert writes[1:] == [("zsxq_file", "原始报告", 4, "extracted")]
    assert sync._new_ids == [22]
    enqueue.assert_called_once_with("report_extract", report_id=22)
    sync.db.get_topic.assert_not_called()
    sync.extractor.extract.assert_called_once_with(str(path))


def test_tombstones_and_completed_entries_skip_new_extraction(monkeypatch):
    pg = MagicMock()
    pg.__enter__.return_value = pg
    monkeypatch.setattr(report_ingestion, "PgClient", lambda: pg)
    pg.fetch_one.return_value = {"exists": 1}
    args = ({"file_id": 12, "name": "A.pdf"}, "zsxq_file", "A.pdf", "原文", 2, "extracted")
    assert report_ingestion.save_attachment(*args) is None
    pg.fetch_one.side_effect = [None, {"report_id": 22, "file_path": "A.pdf", "extraction_status": "extracted"}]
    assert report_ingestion.save_attachment(*args) is None
    for call in pg.execute.call_args_list:
        assert "pg_advisory_xact_lock" in call.args[0]
    pg.fetch_one.side_effect = [None, {"report_id": 22, "file_path": None, "extraction_status": "pending"}]
    assert report_ingestion.save_attachment(*args) == 22
    sql, bindings = pg.execute.call_args_list[-2].args
    assert "analysis_json=NULL" in sql and "analysis_status='none'" in sql
    assert bindings == ("A.pdf", "原文", 2, "extracted", 22)
    assert pg.execute.call_args.args == ("DELETE FROM fin.report_forecast WHERE report_id=%s", (22,))


def test_download_callback_budgets_ready_files_and_leaves_legacy_work_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr("workflows.reports.sync.enqueue", MagicMock())
    sync = object.__new__(ZSXQ2PGSync)
    sync.download_dir = str(tmp_path)
    sync._new_ids, sync.ready_limit = [], 1
    sync.extractor = MagicMock()
    sync.extractor.extract.return_value = "就绪"
    rows = []
    for fid in range(3):
        path = tmp_path / f"{fid}.pdf"
        path.touch()
        rows.append({"file_id": fid, "name": path.name, "local_path": str(path)})
    states = {0: {"file_path": rows[0]["local_path"], "extraction_status": "pending"}}
    monkeypatch.setattr(report_ingestion, "attachment_states", lambda ids: states)
    save = MagicMock(return_value=45)
    monkeypatch.setattr(report_ingestion, "save_attachment", save)
    sync._sync_attachments(rows, "zsxq_file")
    assert save.call_count == 1
    assert save.call_args.args[0]["file_id"] == 1
    assert sync._new_ids == [45]

@pytest.mark.integration
def test_attachment_queries_in_isolated_postgres(monkeypatch):
    """Exercise the actual dedup/upsert SQL only against transaction-local temp tables."""
    from contextlib import nullcontext
    from core.config import get_config
    from storage.pg import PgClient
    from storage.repositories import report_duplicates

    get_config()
    with PgClient() as pg:
        pg.execute("""CREATE TEMP TABLE report_meta (
            report_id serial PRIMARY KEY, file_id bigint, topic_id bigint, file_name text, file_path text,
            file_size int, title text, source text, publish_date date, content_text text, content_chars int,
            extraction_status text, analysis_json text, analysis_status text, content_text_cn text,
            tags_industries text, tags_regions text, tags_commodities text, tags_themes text,
            tags_targets text, symbols text, ts_code text) ON COMMIT DROP""")
        pg.execute("""CREATE TEMP TABLE report_meta_merged (
            file_id bigint PRIMARY KEY, report_id integer, topic_id bigint, file_name text) ON COMMIT DROP""")
        pg.execute("CREATE TEMP TABLE report_forecast (report_id int) ON COMMIT DROP")

        class TempDatabase:
            def execute(self, sql, params=None):
                return pg.execute(sql.replace("fin.", "pg_temp."), params)

            def fetch_one(self, sql, params=None):
                return pg.fetch_one(sql.replace("fin.", "pg_temp."), params)

            def fetch_all(self, sql, params=None):
                return pg.fetch_all(sql.replace("fin.", "pg_temp."), params)

        def use_temp(**kwargs):
            return nullcontext(TempDatabase())

        monkeypatch.setattr(report_ingestion, "PgClient", use_temp)
        monkeypatch.setattr(report_duplicates, "PgClient", use_temp)
        monkeypatch.setattr(report_duplicates, "ddl_cursor", lambda: nullcontext(MagicMock()))
        pg.execute("""
            INSERT INTO pg_temp.report_meta(file_id, topic_id, file_name, source, analysis_status)
            VALUES (NULL,1,'topic.txt','zsxq_topic',NULL), (31,1,'A.pdf','zsxq','done'),
                   (32,1,'B.pdf','zsxq_file',NULL), (33,1,'中文版-A.pdf','zsxq',NULL),
                   (31,1,'A.pdf','zsxq_file',NULL), (34,1,'notes.docx','zsxq',NULL)""")
        assert report_duplicates.merge()["deleted"] == 2
        assert report_duplicates.merge()["deleted"] == 0
        assert [r["file_name"] for r in pg.fetch_all(
            "SELECT file_name FROM pg_temp.report_meta ORDER BY report_id"
        )] == ["topic.txt", "A.pdf", "B.pdf", "notes.docx"]
        assert pg.fetch_all("SELECT file_id FROM pg_temp.report_meta_merged") == [{"file_id": 33}]
        meta = {"file_id": 32, "topic_id": 1, "name": "B.pdf"}
        pg.execute("UPDATE pg_temp.report_meta SET analysis_json='old', content_text_cn='old' WHERE file_id=32")
        pg.execute("INSERT INTO pg_temp.report_forecast VALUES (3)")
        assert report_ingestion.save_attachment(meta, "zsxq_file", "B.pdf", "真实正文", 4, "extracted") == 3
        assert report_ingestion.save_attachment(meta, "zsxq_file", "B.pdf", "真实正文", 4, "extracted") is None
        assert pg.fetch_one(
            "SELECT content_text, content_text_cn, analysis_json FROM pg_temp.report_meta WHERE file_id=32"
        ) == {"content_text": "真实正文", "content_text_cn": None, "analysis_json": None}
        assert not pg.fetch_all("SELECT * FROM pg_temp.report_forecast")
