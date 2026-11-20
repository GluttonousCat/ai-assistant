# -*- encoding: utf-8 -*-
"""
Zhixingqiu crawler data → PG research-report warehouse sync script.

Data flow:
  output/{group_id}/topics_{group_id}.db (sqlite)
    ├── topics table        → fin.report_meta.title/text      (separate topic commentary)
    └── topic_files table   → fin.report_meta (file_path = locally downloaded file, extract body)
  output/{group_id}/downloads/*   → fin.report_meta (attachment extraction; supports pdf/docx/txt/md)

Usage:
  python -m workflows.reports.sync [--group <group_id>] [--only-args] [--limit N]

Topic text is stored independently. Attachments are identified by file ID.
Missing files remain pending until the original arrives, including downloads that
complete after the ingestion watermark.
"""
from __future__ import annotations

import argparse
import os

from core.logger import get_logger
logger = get_logger(__name__)

from core.config import get_config
from storage.pg import PgClient
from storage.pg_schema import (
    ensure_report_analysis_columns,
    init_report_schema,
    T_REPORT_META,
    T_REPORT_FORECAST,
    T_REPORT_SYNC_META,
)
from storage.repositories import report_ingestion
from storage.sqlite.files import FilesDatabase
from storage.sqlite.topics import TopicsDatabase
from tools.finance.report_extractor import get_extractor
from core.helpers import strip_zsxq_tags, is_chinese_translated

from core.paths import PathManager
from core.offpeak_queue import enqueue

# Max length of extracted report text (avoids oversized files filling the PG
# column; keeps only the head with the core content)
MAX_TEXT_CHARS = 200_000
# Minimum body-length threshold (below this it is treated as non-report content,
# e.g. pure questions / chit-chat)
MIN_REPORT_CHARS = 30


def _nn(s):
    """Strip NUL chars: topic/PDF-extracted text occasionally contains \\x00, which
    psycopg2 rejects in string literals; this once rolled back an entire sync batch
    transaction (all ingestion during a 17-day outage window was stuck on this —
    verified 2026-10-30)."""
    return s.replace("\x00", "") if isinstance(s, str) else s


class ZSXQ2PGSync:
    def __init__(self, group_id: str, limit: int = None, *, ready_limit: int | None = None):
        self.group_id = str(group_id)
        self.limit = limit
        self.ready_limit = ready_limit
        self.path_manager = PathManager()
        self.db_path = self.path_manager.get_topics_db_path(self.group_id)
        self.files_db_path = self.path_manager.get_files_db_path(self.group_id)
        self.download_dir = self.path_manager.get_download_dir(self.group_id)
        self.extractor = get_extractor()
        self.db = TopicsDatabase(self.db_path)
        self.files_db = FilesDatabase(self.files_db_path) if os.path.exists(self.files_db_path) else None
        # report_ids newly ingested by this sync (returned by run() so the download
        # pipeline can process each one individually)
        self._new_ids: list = []

    # ---------- Sync main entry ----------
    def run(self) -> list:
        """
        Incremental sync sqlite -> PG. Returns the list of report_ids newly ingested
        this run. Ready documents are queued after each committed attachment.
        """
        if not os.path.exists(self.db_path) and self.files_db is None:
            logger.error(f"话题库/文件库均不存在: {self.db_path}")
            print("   请先运行爬虫: python -m cli.interactive 或 API /crawl/*")
            return []

        # Deploy the PG tables
        with PgClient() as pg:
            init_report_schema(pg)
        ensure_report_analysis_columns()

        self._new_ids = []
        if os.path.exists(self.db_path):
            self._sync_topics()
        if self.ready_limit:
            # Download callbacks look for the newest completed downloads first.
            self._sync_group_files()
        if os.path.exists(self.db_path):
            self._sync_files()
        if not self.ready_limit:
            self._sync_group_files()
        self._print_summary()
        return self._new_ids

    # ---------- Topic-text sync ----------
    def _sync_topics(self):
        """topics table of the topics DB → report_meta (source='zsxq_topic')."""
        with PgClient() as pg:
            last_topic_id = self._get_sync_marker(pg, "last_topic_id")
            total = 0
            skipped = 0
            max_topic_id = last_topic_id

            for row in self.db.fetch_topics_after(last_topic_id, self.limit):
                topic_id = row["topic_id"]
                max_topic_id = max(max_topic_id, topic_id)
                text = _nn(strip_zsxq_tags(row.get("text") or ""))
                title = _nn((row.get("title") or "").strip())
                create_time = row.get("create_time") or ""

                # Report admission: empty text with no title, or text too short
                # (non-report content)
                if (not text and not title) or len(text) < MIN_REPORT_CHARS:
                    skipped += 1
                    continue

                # Idempotent: skip if already synced
                exists = pg.fetch_one(
                    f"SELECT 1 FROM {T_REPORT_META} WHERE source IN ('zsxq_topic','zsxq_topic_pdf_absent') AND topic_id=%s",
                    (topic_id,),
                )

                if not exists:
                    pg.execute(
                        f"""INSERT INTO {T_REPORT_META}
                            (topic_id, title, content_text, content_chars, source,
                             file_name, publish_date, extraction_status)
                            VALUES (%s, %s, %s, %s, 'zsxq_topic', %s, %s, 'extracted')""",
                        (
                            topic_id,
                            title if title else self._derive_title(text),
                            text[:MAX_TEXT_CHARS],
                            len(text),
                            self._derive_filename(topic_id, title),
                            self._parse_date(create_time),
                        ),
                    )
                total += 1

            self._set_sync_marker(pg, "last_topic_id", value_int=max_topic_id)
            logger.info(f"📄 话题文本已同步 (本批次 {total} 条, 跳过 {skipped} 条短内容)")

    def _sync_files(self):
        # A previously discovered file can finish downloading at any later time.
        self._sync_attachments(self.db.get_topic_files_after(0), "zsxq", self.limit)

    def _sync_group_files(self):
        if self.files_db is not None:
            self._sync_attachments(self.files_db.get_completed_files(limit=-1), "zsxq_file")

    def _sync_attachments(self, rows, source, limit=None):
        processed = 0
        for offset in range(0, len(rows), 1000):
            batch = rows[offset:offset + 1000]
            states = report_ingestion.attachment_states([row["file_id"] for row in batch])
            for row in batch:
                if self.ready_limit and len(self._new_ids) >= self.ready_limit:
                    return
                name = _nn(row.get("name") or "")
                if is_chinese_translated(name) or name.lower().endswith((".mp3", ".m4a", ".wav", ".aac", ".flac")):
                    continue
                previous = states.get(row["file_id"])
                if previous:
                    if previous["extraction_status"] == "merged":
                        continue
                    # A legacy scanned attachment with an existing path may
                    # still have pending text. Its upgrade is explicit.
                    old_path = previous["file_path"]
                    if old_path and os.path.isfile(old_path):
                        continue
                path = self._resolve_local_path(row)
                if previous and not path:
                    continue
                text = _nn(self.extractor.extract(path)) if path else None
                status = "extracted" if text else ("failed" if path else "pending")
                report_id = report_ingestion.save_attachment(
                    {**row, "name": name}, source, path, (text or "")[:MAX_TEXT_CHARS],
                    len(text or ""), status, previous_path=previous["file_path"] if previous else None,
                )
                if report_id:
                    self._new_ids.append(report_id)
                    if path and name.lower().endswith((".pdf", ".docx", ".doc")):
                        enqueue("report_extract", report_id=report_id)
                processed += 1
                if limit and processed >= limit:
                    return

    def _resolve_local_path(self, row) -> str:
        """Local path of an attachment: first check local_path, then guess by the
        sanitized filename, finally the raw filename."""
        from core.helpers import sanitize_filename
        local = row.get("local_path")
        if local and os.path.isfile(local):
            return local

        # Files on disk are stored under their sanitized name (by the downloader)
        name = row.get("name") or ""
        guess = os.path.join(self.download_dir, sanitize_filename(name))
        if os.path.isfile(guess):
            return guess

        # Last-resort fallback: the raw filename
        raw_guess = os.path.join(self.download_dir, name)
        return raw_guess if os.path.isfile(raw_guess) else None

    def _get_sync_marker(self, pg, key: str) -> int:
        row = pg.fetch_one(
            f"SELECT value_int FROM {T_REPORT_SYNC_META} WHERE key_name=%s", (key,))
        return row["value_int"] or 0 if row else 0

    def _set_sync_marker(self, pg, key: str, value_int: int = None, value_str: str = None):
        pg.execute(
            f"""INSERT INTO {T_REPORT_SYNC_META} (key_name, value_int, value_str)
                VALUES (%s, %s, %s)
                ON CONFLICT (key_name) DO UPDATE
                SET value_int = EXCLUDED.value_int, value_str = EXCLUDED.value_str,
                    updated_at = now()""",
            (key, value_int, value_str),
        )

    def _print_summary(self):
        with PgClient() as pg:
            rows = pg.fetch_all(
                f"""SELECT source, extraction_status, count(*) AS cnt
                    FROM {T_REPORT_META} GROUP BY 1, 2 ORDER BY 1, 2""")
            print("\n" + "=" * 50)
            print("📊 研报仓库概览 (fin.report_meta)")
            print("=" * 50)
            for r in rows:
                print(f"  {r['source']:<12s} {r['extraction_status']:<10s} {r['cnt']:>6,}")

            row = pg.fetch_one(f"SELECT count(*) AS cnt FROM {T_REPORT_FORECAST}")
            print(f"  预测提取 (report_forecast): {row['cnt'] if row else 0:,}")

    # ---------- Parsing helpers ----------
    @staticmethod
    def _parse_date(s) -> str:
        """'2026-08-16T10:00:00+08:00' → '2026-08-16'; returns None on failure."""
        if not s:
            return None
        return s[:10] if len(s) >= 10 else s

    @staticmethod
    def _derive_filename(topic_id, title) -> str:
        return f"topic_{topic_id}_{(title or '')[:40]}.txt"

    @staticmethod
    def _derive_title(name_or_text) -> str:
        """Derive a title from the filename or the first line of the body."""
        s = (name_or_text or "").strip()
        if not s:
            return ""
        if "\n" in s:
            s = s.split("\n")[0]
        return s[:250]


def main():
    parser = argparse.ArgumentParser(description="知识星球 → PG 研报同步")
    parser.add_argument("--group", default=None, help="群组ID (默认取配置 ZSXQ_GROUP_ID)")
    parser.add_argument("--limit", type=int, default=None, help="每批次最多处理条数")
    args = parser.parse_args()

    group_id = args.group or get_config().zsxq_group_id
    if not group_id:
        logger.error("未指定群组ID (--group 或 .env ZSXQ_GROUP_ID)")
        sys.exit(1)

    pm = PathManager()
    topics_db = pm.get_topics_db_path(str(group_id))
    files_db = pm.get_files_db_path(str(group_id))
    if not os.path.exists(topics_db) and not os.path.exists(files_db):
        logger.error("话题库/文件库均不存在, 请先爬取 (python -m cli.interactive)")
        sys.exit(1)

    sync = ZSXQ2PGSync(group_id, args.limit)
    sync.run()


if __name__ == "__main__":
    main()
