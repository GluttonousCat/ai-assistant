# -*- encoding: utf-8 -*-
"""
巨潮资讯网 (cninfo) 定期报告爬取模块 — 自包含, 与宿主项目零耦合
(整个目录可独立拷出使用, 运行依赖仅 requests; 批量 --index 模式另需 tushare)

结构 (分层: client 纯网络 / filters 纯函数 / store 文件存储 / downloader 编排):
- client.py     HTTP 客户端: orgId 解析 / 公告检索 / 全市场清单 / PDF 下载
                (直连+重试+断点续传+魔数校验)
- filters.py    公告筛选纯函数: 年报全文识别 / 报告年度解析 / 代码归一化
- store.py      文件存储: 公告元数据 / orgId 映射 / 断点状态 (output/cninfo/meta/)
- downloader.py 编排+CLI: 代码解析→查询→筛选→落盘→下载 (存在性幂等)
                python -m tools.cninfo.downloader --stock 300308 --years 2024
- batch.py      全市场批量: 限流熔断/断点续传/时间预算/--index 指数成分

输入只认证券代码 (6 位或 ts_code) — 股票名称解析 (如 '中际旭创') 是调用方
(agent 词表) 的职责。历史版本曾写 PG 表 fin.cninfo_* (2026-10 起改文件产物,
存量表保留未迁移)。

文件产物: output/cninfo/downloads/<ts_code>/<year>_<category>.pdf
"""
from .client import CninfoClient
from .downloader import CninfoDownloader

__all__ = ["CninfoClient", "CninfoDownloader"]
