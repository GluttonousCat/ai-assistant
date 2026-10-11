# -*- encoding: utf-8 -*-
"""巨潮文件存储测试 (tmp 目录, 无网络/无 DB)"""
from __future__ import annotations

from tools.cninfo import store


def test_stock_meta_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "META_ROOT", tmp_path)
    # 不存在 -> 空骨架
    meta = store.load_stock_meta("600519.SH")
    assert meta == {"announcements": {}, "state": {}}

    # upsert 两条公告 (同 aid 二次写入保留下载回填字段)
    ann = {"announcementTitle": "2024年年度报告", "announcementId": "a1",
           "announcementTime": 1700000000000, "adjunctUrl": "finalpage/a1.PDF",
           "secCode": "600519", "secName": "贵州茅台"}
    meta["announcements"]["a1"] = {
        "title": ann["announcementTitle"], "category": "ndbg",
        "report_year": 2024, "announce_ts": 1700000000000,
        "adjunct_url": ann["adjunctUrl"], "download_status": "done",
        "file_path": "x/2024_ndbg.pdf", "file_size": 99}
    store.save_stock_meta("600519.SH", meta)

    again = store.load_stock_meta("600519.SH")
    assert again["announcements"]["a1"]["download_status"] == "done"
    assert again["announcements"]["a1"]["report_year"] == 2024


def test_org_map_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "META_ROOT", tmp_path)
    assert store.load_org_map() == {}
    store.save_org_map({"600519": "gssh0600519"})
    assert store.load_org_map() == {"600519": "gssh0600519"}
