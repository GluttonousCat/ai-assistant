# -*- encoding: utf-8 -*-
"""
巨潮元数据/进度存储 (纯文件, 无数据库)

组织 (output/cninfo/):
- meta/org_map.json     sec_code -> orgId 持久映射 (每股只真正解析一次,
                        免重复打限流敏感的 topSearch)
- meta/<ts_code>.json   单股公告元数据 + 批量爬取断点状态
- downloads/<ts_code>/  PDF 产物 (文件在盘即已下载)

条目字段沿用历史 PG 表 fin.cninfo_announcement (2026-10 前版本, DDL 曾在
storage/pg_schema.py); 存量表保留历史数据未迁移, 文件为增量事实源。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict

META_ROOT = Path("output/cninfo/meta")


def _atomic_write(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)   # 原子替换 (Windows 上 rename 目标存在会抛错)


# ---------- orgId 映射 ----------

def load_org_map() -> Dict[str, str]:
    try:
        return json.loads((META_ROOT / "org_map.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_org_map(m: Dict[str, str]) -> None:
    _atomic_write(META_ROOT / "org_map.json", m)


# ---------- 单股元数据 + 断点状态 ----------

def stock_meta_path(ts_code: str) -> Path:
    return META_ROOT / f"{ts_code}.json"


def load_stock_meta(ts_code: str) -> Dict:
    """{"announcements": {aid: 条目}, "state": {"meta": ..., "dl": ..., "error": ...}}

    announcements 条目: announcement 原始字段的清洗投影 (title/category/
    report_year/announce_date/announce_ts/adjunct_url/sec_code/sec_name)
    加下载回填字段 (file_path/file_size/download_status/downloaded_at)。
    state 由批量爬取 (batch.py) 维护, 单股流程 (downloader.sync_stock) 不碰。
    """
    try:
        meta = json.loads(stock_meta_path(ts_code).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    meta.setdefault("announcements", {})
    meta.setdefault("state", {})
    return meta


def save_stock_meta(ts_code: str, meta: Dict) -> None:
    _atomic_write(stock_meta_path(ts_code), meta)
