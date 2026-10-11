# -*- encoding: utf-8 -*-
"""
巨潮资讯网 (cninfo) 定期报告获取+解析模块 — 自包含, 与宿主项目零耦合
(整个目录可独立拷出使用; 运行依赖 requests, 解析另需 pymupdf4llm/pymupdf,
批量 --index 模式另需 tushare)

定期报告四类: AR 年报(ndbg) / H1 半年报(bndbg) / Q1 一季报(yjdbg) / Q3 三季报(sjdbg)
— 下载与元数据四类全覆盖; 解析当前覆盖 AR/H1 (同构), Q1/Q3 待后续规则。

结构 (分层: client 纯网络 / filters 纯函数 / store 文件存储 /
        downloader·batch 获取编排 / parse 解析 / evidence 证据抽取内核):
- client.py     HTTP 客户端: orgId 解析 / 公告检索 / 全市场清单 / PDF 下载
                (直连+重试+断点续传+魔数校验)
- filters.py    公告筛选纯函数: 年报全文识别 / 报告年度解析 / 代码归一化
- store.py      文件存储: 公告元数据 / orgId 映射 / 断点状态 (output/cninfo/meta/)
- downloader.py 获取编排+CLI: 代码解析→查询→筛选→落盘→下载 (存在性幂等)
                python -m tools.cninfo.downloader --stock 300308 --years 2024
- batch.py      全市场批量: 限流熔断/断点续传/时间预算/--index 指数成分
- parse.py      报告解析+CLI: PDF→MD→{items,sections} JSON (AR/H1);
                annual_corpus 确定性语料提炼 (kpi3y+mdna 经营/风险)
                python -m tools.cninfo.parse 300308 --year 2024 [--audit]
- evidence.py   年报原子证据抽取内核: 系统提示词/小节序列化/原子清洗
                (LLM 客户端由调用方注入, 四类原子 operating/segment/
                statement/risk; 编排与证据契约留调用方)

输入只认证券代码 (6 位或 ts_code) — 股票名称解析 (如 '中际旭创') 是调用方
(agent 词表) 的职责。历史版本曾写 PG 表 fin.cninfo_* (2026-10 起改文件产物,
存量表保留未迁移)。

文件产物:
- output/cninfo/downloads/<ts_code>/<year>_<category>.pdf   (downloader)
- output/cninfo/meta/<ts_code>.json + org_map.json          (store)
- output/cninfo/md/<ts_code>/<year>_<category>.md + _items.json   (parse)
"""
from .client import CninfoClient
from .downloader import CninfoDownloader

__all__ = ["CninfoClient", "CninfoDownloader"]
