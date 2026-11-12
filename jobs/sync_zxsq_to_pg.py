# -*- encoding: utf-8 -*-
"""
Zhixingqiu crawler data → PG research-report warehouse sync script.

Data flow:
  output/{group_id}/topics_{group_id}.db (sqlite)
    ├── topics table        → fin.report_meta.title/text      (topic text serves as the report body)
    └── topic_files table   → fin.report_meta (file_path = locally downloaded file, extract body)
  output/{group_id}/downloads/*   → fin.report_meta (attachment extraction; supports pdf/docx/txt/md)

Usage:
  python -m scripts.sync_zxsq_to_pg [--group <group_id>] [--only-args] [--limit N]

Features:
- Checkpoint resume: fin.report_sync_meta records last_topic_id; re-runs only process
  newly added topic attachments
- Idempotent: report_meta is deduplicated by (source='zsxq', topic_id); repeated
  syncs only update
- Text extraction: when the local file is missing, degrade to the topic's text;
  extraction_status='pending' marks it for later backfill
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logger import get_logger
logger = get_logger(__name__)

from core.config import get_config
from storage.pg import PgClient
from storage.pg_schema import (
    init_report_schema,
    T_REPORT_META,
    T_REPORT_FORECAST,
    T_REPORT_SYNC_META,
)
from storage.sqlite.files import FilesDatabase
from storage.sqlite.topics import TopicsDatabase
from tools.finance.report_extractor import get_extractor
from core.helpers import strip_zsxq_tags, is_chinese_translated

# Idempotency check: exists in report_meta OR was deleted by dedup-merge with an
# audit-trail row (audited ones are never re-inserted, preventing the
# ingest → merge-delete → full-sync re-insert → re-analyze ping-pong loop)
_EXISTS_SQL = (
    f"SELECT 1 FROM fin.report_meta WHERE file_id=%s "
    f"UNION ALL SELECT 1 FROM fin.report_meta_merged WHERE file_id=%s LIMIT 1")
from core.paths import PathManager

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
    def __init__(self, group_id: str, limit: int = None):
        self.group_id = str(group_id)
        self.limit = limit
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
        this run (for the download pipeline to run per-report LLM analysis on them
        immediately).
        """
        if not os.path.exists(self.db_path) and self.files_db is None:
            logger.error(f"话题库/文件库均不存在: {self.db_path}")
            print("   请先运行爬虫: python -m cli.interactive 或 API /crawl/*")
            return []

        # Deploy the PG tables
        with PgClient() as pg:
            init_report_schema(pg)

        self._new_ids = []
        if os.path.exists(self.db_path):
            self._sync_topics()
            self._sync_files()
        if self.files_db is not None:
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
                    f"SELECT 1 FROM {T_REPORT_META} WHERE source='zsxq_topic' AND topic_id=%s",
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

    # ---------- Attachment sync ----------
    def _sync_files(self):
        """topic_files table of the topics DB → report_meta (source='zsxq'),
        including text extraction."""
        with PgClient() as pg:
            last_file_id = self._get_sync_marker(pg, "last_file_id")
            rows = self.db.get_topic_files_after(last_file_id, self.limit)

            total = 0
            max_file_id = last_file_id
            skipped_cn = 0
            for row in rows:
                file_id = row["file_id"]
                max_file_id = max(max_file_id, file_id)
                name = _nn(row.get("name") or "")

                # Chinese-translated versions are not ingested (keep the English
                # original; the download layer already skips them — this is a
                # fallback for files already on disk)
                if is_chinese_translated(name):
                    skipped_cn += 1
                    continue

                # Already in DB / previously merged-and-deleted? Idempotent skip
                exists = pg.fetch_one(_EXISTS_SQL, (file_id, file_id))
                if exists:
                    total += 1
                    continue

                file_path = self._resolve_local_path(row)
                content, chars, status = self._extract(file_path, row)
                content = _nn(content)

                if not content:
                    status = "failed"

                inserted = pg.fetch_all(
                    f"""INSERT INTO {T_REPORT_META}
                        (topic_id, file_id, title, file_name, file_path, file_size,
                         content_text, content_chars, source, extraction_status,
                         publish_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'zsxq', %s, %s)
                        RETURNING report_id""",
                    (
                        row.get("topic_id"),
                        file_id,
                        self._derive_title(name),
                        name,
                        file_path,
                        row.get("size"),
                        content,
                        chars,
                        status,
                        self._parse_date(row.get("create_time")),
                    ),
                )
                if inserted:
                    self._new_ids.append(inserted[0]["report_id"])
                total += 1

            self._set_sync_marker(pg, "last_file_id", value_int=max_file_id)
            logger.info(f"📎 附件已同步 (本批次 {total} 个"
                  + (f", 跳过中文版 {skipped_cn} 个" if skipped_cn else "") + ")")

            # Backfill: at first sync the file was not yet downloaded (pending);
            # now that it has arrived -> extract body text and update
            backfilled = 0
            pendings = pg.fetch_all(
                f"SELECT report_id, file_path FROM {T_REPORT_META} "
                f"WHERE source='zsxq' AND extraction_status='pending' LIMIT 100")
            for pr in pendings:
                fp = pr.get("file_path")
                if not fp or not os.path.exists(fp):
                    continue
                text = _nn(self.extractor.extract(fp))
                if text:
                    pg.execute(
                        f"UPDATE {T_REPORT_META} SET content_text=%s, content_chars=%s, "
                        f"extraction_status='extracted' WHERE report_id=%s",
                        (text[:MAX_TEXT_CHARS], len(text), pr["report_id"]))
                    backfilled += 1
            if backfilled:
                logger.info(f"📎 附件正文回填 {backfilled} 个 (此前文件未就绪)")

    # ---------- Group-file sync (files DB; file-centric groups like Maoge's
    #      research-report circle) ----------
    def _sync_group_files(self):
        """files table of the files DB → report_meta (source='zsxq_file'); extracts
        text of already-downloaded files."""
        from core.helpers import sanitize_filename
        if self.files_db is None:
            return

        # Sync watermark: group files are tracked by file_id (independent of
        # topic_files' last_file_id)
        with PgClient() as pg:
            marker_key = f"group_file_id_{self.group_id}"
            last_file_id = self._get_sync_marker(pg, marker_key)
            # Audio files are not ingested into the report warehouse
            # (mp3/m4a/wav/aac/flac)
            _audio_exts = ('.mp3', '.m4a', '.wav', '.aac', '.flac')
            # Fetch ALL completed files (no watermark here: download completion
            # order is unrelated to file_id, so an older file that completes after
            # the watermark has passed would be missed forever); idempotency comes
            # from the exists check below
            rows = [r for r in self.files_db.get_completed_files()
                    if not (r.get("name") or "").lower().endswith(_audio_exts)]

            total = 0
            skipped_cn = 0
            max_file_id = last_file_id
            for row in rows:
                file_id = row["file_id"]
                max_file_id = max(max_file_id, file_id)

                name = _nn(row.get("name") or "")

                # Chinese-translated versions are not ingested (keep the English
                # original)
                if is_chinese_translated(name):
                    skipped_cn += 1
                    continue

                # Idempotent: skip if the file is already in the DB or was
                # previously merged-and-deleted
                exists = pg.fetch_one(_EXISTS_SQL, (file_id, file_id))
                if exists:
                    continue
                file_path = self._resolve_local_path(row)
                content, chars, status = self._extract(file_path, row)
                content = _nn(content)

                # Attach the topic text (if PDF extraction came up empty, fall
                # back to the topic text)
                if (not content or chars < MIN_REPORT_CHARS) and row.get("topic_id"):
                    topic = self.db.get_topic(row["topic_id"]) if os.path.exists(self.db_path) else None
                    if topic:
                        topic_text = strip_zsxq_tags(topic.get("text") or "")
                        if len(topic_text) >= MIN_REPORT_CHARS:
                            content, chars, status = topic_text, len(topic_text), "pending"

                if not content:
                    status = "failed"

                inserted = pg.fetch_all(
                    f"""INSERT INTO {T_REPORT_META}
                        (topic_id, file_id, title, file_name, file_path, file_size,
                         content_text, content_chars, source, extraction_status,
                         publish_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'zsxq_file', %s, %s)
                        RETURNING report_id""",
                    (
                        row.get("topic_id"),
                        file_id,
                        self._derive_title(name),
                        name,
                        file_path,
                        row.get("size"),
                        (content or "")[:MAX_TEXT_CHARS],
                        chars,
                        status,
                        self._parse_date(row.get("create_time")),
                    ),
                )
                if inserted:
                    self._new_ids.append(inserted[0]["report_id"])
                total += 1

            self._set_sync_marker(pg, marker_key, value_int=max_file_id)
            logger.info(f"📁 群文件已同步 (本批次 {total} 个"
                  + (f", 跳过中文版 {skipped_cn} 个" if skipped_cn else "") + ")")

    # ---------- Helper methods ----------
    def _extract(self, file_path, row) -> tuple:
        """Returns (text, chars, status)."""
        if file_path and os.path.exists(file_path):
            text = self.extractor.extract(file_path)
            if text:
                return text, len(text), "extracted"
            return None, 0, "failed"

        # File not downloaded: degrade to the topic text (if it is long enough to
        # look like a report body)
        topic = self.db.get_topic(row.get("topic_id")) if row.get("topic_id") else None
        if topic:
            text = strip_zsxq_tags(topic.get("text") or "")
            if len(text) >= 200:
                return text, len(text), "pending"
        return None, 0, "failed"

    def _resolve_local_path(self, row) -> str:
        """Local path of an attachment: first check local_path, then guess by the
        sanitized filename, finally the raw filename."""
        from core.helpers import sanitize_filename
        local = row.get("local_path")
        if local and os.path.exists(local):
            return local

        # Files on disk are stored under their sanitized name (by the downloader)
        name = row.get("name") or ""
        guess = os.path.join(self.download_dir, sanitize_filename(name))
        if os.path.exists(guess):
            return guess

        # Last-resort fallback: the raw filename
        raw_guess = os.path.join(self.download_dir, name)
        return raw_guess if os.path.exists(raw_guess) else None

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