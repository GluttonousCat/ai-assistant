# -*- encoding: utf-8 -*-
"""
知识星球爬虫数据 → PG 研报仓库 同步脚本

数据流:
  output/{group_id}/topics_{group_id}.db (sqlite)
    ├── topics 表         → fin.report_meta.title/text       (话题文本即研报正文)
    └── topic_files 表    → fin.report_meta (file_path=本地下载文件, 提取正文)
  output/{group_id}/downloads/*   → fin.report_meta (附件提取, 支持 pdf/docx/txt/md)

用法:
  python -m scripts.sync_zxsq_to_pg [--group <group_id>] [--only-args] [--limit N]

特性:
- 断点续传: fin.report_sync_meta 记录 last_topic_id, 重复执行只处理新增话题附件
- 幂等: report_meta 按 (source='zsxq', topic_id) 去重, 重复同步只更新
- 文本提取: 本地文件缺失时降级用话题 text 文本, extraction_status='pending' 待补
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
    init_report_views,
    T_REPORT_META,
    T_REPORT_FORECAST,
    T_REPORT_SYNC_META,
)
from storage.sqlite.files import FilesDatabase
from storage.sqlite.topics import TopicsDatabase
from tools.finance.report_extractor import get_extractor
from core.helpers import strip_zsxq_tags, is_chinese_translated

# 幂等检查: 现存 OR 已被去重合并删除留痕 (留痕过的永不重插, 防
# 入库→merge删除→全量sync重插→再分析 的乒乓循环)
_EXISTS_SQL = (
    f"SELECT 1 FROM fin.report_meta WHERE file_id=%s "
    f"UNION ALL SELECT 1 FROM fin.report_meta_merged WHERE file_id=%s LIMIT 1")
from core.paths import PathManager

# 研报文本提取的最大长度 (避免超大文件占满 PG 字段, 只保留头部核心内容)
MAX_TEXT_CHARS = 200_000
# 研报正文最小字数下限 (低于此值视为非研报内容, 如纯提问/闲聊)
MIN_REPORT_CHARS = 30


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
        # 本次同步新入库的 report_id (run() 返回给下载流水线做单篇处理)
        self._new_ids: list = []

    # ---------- 同步主入口 ----------
    def run(self) -> list:
        """
        增量同步 sqlite -> PG. 返回本次新入库的 report_id 列表
        (供下载流水线对刚入库的研报立即做单篇 LLM 分析).
        """
        if not os.path.exists(self.db_path) and self.files_db is None:
            logger.error(f"话题库/文件库均不存在: {self.db_path}")
            print("   请先运行爬虫: python -m cli.interactive 或 API /crawl/*")
            return []

        # 部署 PG 表
        with PgClient() as pg:
            init_report_schema(pg)
            init_report_views(pg)

        self._new_ids = []
        if os.path.exists(self.db_path):
            self._sync_topics()
            self._sync_files()
        if self.files_db is not None:
            self._sync_group_files()
        self._print_summary()
        return self._new_ids

    # ---------- 话题文本同步 ----------
    def _sync_topics(self):
        """话题库中 topics 表 → report_meta (source='zsxq_topic')"""
        with PgClient() as pg:
            last_topic_id = self._get_sync_marker(pg, "last_topic_id")
            total = 0
            skipped = 0
            max_topic_id = last_topic_id

            for row in self.db.fetch_topics_after(last_topic_id, self.limit):
                topic_id = row["topic_id"]
                max_topic_id = max(max_topic_id, topic_id)
                text = strip_zsxq_tags(row.get("text") or "")
                title = (row.get("title") or "").strip()
                create_time = row.get("create_time") or ""

                # 研报准入: 空文本且无标题, 或文本过短 (非研报内容)
                if (not text and not title) or len(text) < MIN_REPORT_CHARS:
                    skipped += 1
                    continue

                # 幂等: 已同步过则跳过
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

    # ---------- 附件同步 ----------
    def _sync_files(self):
        """话题库中 topic_files 表 → report_meta (source='zsxq'), 含文本提取"""
        with PgClient() as pg:
            last_file_id = self._get_sync_marker(pg, "last_file_id")
            rows = self.db.get_topic_files_after(last_file_id, self.limit)

            total = 0
            max_file_id = last_file_id
            skipped_cn = 0
            for row in rows:
                file_id = row["file_id"]
                max_file_id = max(max_file_id, file_id)
                name = row.get("name") or ""

                # 中文翻译版不入库 (保留英文原版; 下载层已跳过, 此处兜底本地已有文件)
                if is_chinese_translated(name):
                    skipped_cn += 1
                    continue

                # 已在库中/曾被合并删除? 幂等跳过
                exists = pg.fetch_one(_EXISTS_SQL, (file_id, file_id))
                if exists:
                    total += 1
                    continue

                file_path = self._resolve_local_path(row)
                content, chars, status = self._extract(file_path, row)

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

            # 回填: 先同步时文件未下载 (pending), 现在文件已到 -> 提取正文更新
            backfilled = 0
            pendings = pg.fetch_all(
                f"SELECT report_id, file_path FROM {T_REPORT_META} "
                f"WHERE source='zsxq' AND extraction_status='pending' LIMIT 100")
            for pr in pendings:
                fp = pr.get("file_path")
                if not fp or not os.path.exists(fp):
                    continue
                text = self.extractor.extract(fp)
                if text:
                    pg.execute(
                        f"UPDATE {T_REPORT_META} SET content_text=%s, content_chars=%s, "
                        f"extraction_status='extracted' WHERE report_id=%s",
                        (text[:MAX_TEXT_CHARS], len(text), pr["report_id"]))
                    backfilled += 1
            if backfilled:
                logger.info(f"📎 附件正文回填 {backfilled} 个 (此前文件未就绪)")

    # ---------- 群文件同步 (files 库, 猫哥研报圈等以文件为主的群) ----------
    def _sync_group_files(self):
        """文件库中 files 表 → report_meta (source='zsxq_file'), 提取已下载文件文本"""
        from core.helpers import sanitize_filename
        if self.files_db is None:
            return

        # 同步水位: 群文件用 file_id 记录 (独立于 topic_files 的 last_file_id)
        with PgClient() as pg:
            marker_key = f"group_file_id_{self.group_id}"
            last_file_id = self._get_sync_marker(pg, marker_key)
            # 音频文件不入研报库 (mp3/m4a/wav/aac/flac)
            _audio_exts = ('.mp3', '.m4a', '.wav', '.aac', '.flac')
            # 全量取 completed (不用水位线: 下载完成顺序与 file_id 无关,
            # 水位线已越过的老文件完成后会被永久漏掉); 幂等靠下方 exists 检查
            rows = [r for r in self.files_db.get_completed_files()
                    if not (r.get("name") or "").lower().endswith(_audio_exts)]

            total = 0
            skipped_cn = 0
            max_file_id = last_file_id
            for row in rows:
                file_id = row["file_id"]
                max_file_id = max(max_file_id, file_id)

                name = row.get("name") or ""

                # 中文翻译版不入库 (保留英文原版)
                if is_chinese_translated(name):
                    skipped_cn += 1
                    continue

                # 幂等: 文件已在库中/曾被合并删除则跳过
                exists = pg.fetch_one(_EXISTS_SQL, (file_id, file_id))
                if exists:
                    continue
                file_path = self._resolve_local_path(row)
                content, chars, status = self._extract(file_path, row)

                # 关联话题文本 (若 PDF 提取为空, 用话题文本兜底)
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

    # ---------- 工具方法 ----------
    def _extract(self, file_path, row) -> tuple:
        """返回 (text, chars, status)"""
        if file_path and os.path.exists(file_path):
            text = self.extractor.extract(file_path)
            if text:
                return text, len(text), "extracted"
            return None, 0, "failed"

        # 文件未下载: 降级用话题文本 (若话题文本较长且像研报正文)
        topic = self.db.get_topic(row.get("topic_id")) if row.get("topic_id") else None
        if topic:
            text = strip_zsxq_tags(topic.get("text") or "")
            if len(text) >= 200:
                return text, len(text), "pending"
        return None, 0, "failed"

    def _resolve_local_path(self, row) -> str:
        """附件本地路径: 先查 local_path, 再按 sanitize 后文件名猜, 最后原始名"""
        from core.helpers import sanitize_filename
        local = row.get("local_path")
        if local and os.path.exists(local):
            return local

        # 磁盘上的文件名是 sanitize 后的 (下载器存储)
        name = row.get("name") or ""
        guess = os.path.join(self.download_dir, sanitize_filename(name))
        if os.path.exists(guess):
            return guess

        # 最后一个兜底: 原始名
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

    # ---------- 解析辅助 ----------
    @staticmethod
    def _parse_date(s) -> str:
        """'2026-08-16T10:00:00+08:00' → '2026-08-16', 失败返回 None"""
        if not s:
            return None
        return s[:10] if len(s) >= 10 else s

    @staticmethod
    def _derive_filename(topic_id, title) -> str:
        return f"topic_{topic_id}_{(title or '')[:40]}.txt"

    @staticmethod
    def _derive_title(name_or_text) -> str:
        """从文件名或正文首行推导标题"""
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