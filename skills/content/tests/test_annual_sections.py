# -*- encoding: utf-8 -*-
"""annual_sections 单测 — fixture 仿澜起/药明 3~7 节真实形态。"""
from __future__ import annotations

from skills.content.annual_sections import extract_chunks

_MD = """# 某公司 2025 年年度报告

## 第三节 管理层讨论与分析

### 一、报告期内公司从事的业务情况
主业A。

#### (一) 子业务结构
子业务细节。

### 二、经营情况讨论与分析
收入增长 30%。

### 三、可能面对的风险
市场风险。

## 第四节 公司治理、环境和社会

### 一、公司治理相关情况说明
治理概况 (不应入库)。

### 五、董事和高级管理人员的情况
高管 8 人。

#### (五) 员工情况
在职员工的数量合计 14,000 人。

### 十一、公司股权激励计划、员工持股计划
激励计划授予 500 万股。

### 十二、报告期内的内部控制制度建设及实施情况
内控 (不应入库)。

## 第五节 重要事项

### 一、承诺事项履行情况
承诺均已履行。

### 三、重大关联交易
关联采购 3 亿元。

### 五、重大诉讼、仲裁事项
无重大诉讼。

### 八、破产重整相关事项
不适用 (不应入库)。

## 第六节 股份变动及股东情况

报告期末普通股股东总数 45,123 户。

|股东总数|45,123|户|
|---|---|---|
|前十大股东|见下表|

### 一、股份变动情况表
总股本 11.3 亿股。

### 三、股东和实际控制人情况
控股股东 X 集团, 质押 12%。

## 第七节 债券相关情况

不适用。
"""


def test_mdna_all_subsections():
    chunks = [c for c in extract_chunks(_MD) if c["section_key"] == "mdna"]
    titles = [c["title"] for c in chunks]
    assert titles == ["报告期内公司从事的业务情况", "经营情况讨论与分析",
                      "可能面对的风险"]          # 全量, 无筛选


def test_governance_keyword_filter():
    chunks = [c for c in extract_chunks(_MD) if c["section_key"] == "governance"]
    titles = [c["title"] for c in chunks]
    assert "董事和高级管理人员的情况" in titles      # 关键词命中
    assert any("股权激励" in t for t in titles)
    assert all("治理概况" not in t and "内部控制" not in t for t in titles)
    # 澜起式: 员工情况作为董监高 L2 子节随父块带入
    mng = next(c for c in chunks if "董事和高级管理人员" in c["title"])
    assert "在职员工的数量合计 14,000 人" in mng["text"]


def test_matters_keyword_filter():
    chunks = [c for c in extract_chunks(_MD) if c["section_key"] == "matters"]
    titles = [c["title"] for c in chunks]
    assert any("承诺" in t for t in titles)
    assert any("关联交易" in t for t in titles)
    assert any("诉讼" in t for t in titles)
    assert all("破产" not in t for t in titles)      # 不在关键词, 不入库


def test_shareholders_all_with_head():
    chunks = [c for c in extract_chunks(_MD)
              if c["section_key"] == "shareholders"]
    # 节首前文 (sub_order=0): 股东总数所在
    head = next(c for c in chunks if c["sub_order"] == 0)
    assert "股东总数 45,123 户" in head["text"]
    assert len(head["tables"]) == 1                   # 节首表格也解析
    titles = [c["title"] for c in chunks if c["sub_order"] > 0]
    assert "股份变动情况表" in titles and "股东和实际控制人情况" in titles


def test_bonds_short_skipped():
    assert not [c for c in extract_chunks(_MD) if c["section_key"] == "bonds"]


def test_sub_order_sequential_l1_only():
    chunks = [c for c in extract_chunks(_MD)
              if c["section_key"] == "mdna"]
    assert [c["sub_order"] for c in chunks] == [1, 2, 3]
