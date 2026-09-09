# -*- encoding: utf-8 -*-
"""
巨潮资讯网 (cninfo) 定期报告爬取模块

结构 (分层: client 纯网络 / filters 纯函数 / downloader 编排):
- client.py     HTTP 客户端: orgId 解析 / 公告检索 / PDF 下载 (直连+重试+魔数校验)
- filters.py    公告筛选纯函数: 年报全文识别 / 报告年度解析 / 干扰版排除
- downloader.py 编排+CLI: 股票解析→查询→筛选→元数据入库→下载 (存在性幂等)
                python -m tools.cninfo.downloader --stock 中际旭创 --years 2024

存储: fin.cninfo_announcement (DDL 见 storage/pg_schema.py)
文件: output/cninfo/downloads/<ts_code>/<year>_<category>.pdf
用途: 年报原文交叉核验 (extract_document 可直接解析本地 PDF)
"""
from tools.cninfo.client import CninfoClient
from tools.cninfo.downloader import CninfoDownloader

__all__ = ["CninfoClient", "CninfoDownloader"]
