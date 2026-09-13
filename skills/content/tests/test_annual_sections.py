# -*- encoding: utf-8 -*-
"""annual_sections 单测 — fixture 仿澜起/药明 3~7 节真实形态。"""
from __future__ import annotations

from skills.content.annual_sections import (extract_chunks,
                                        extract_sec2, parse_md_tables)

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



# 第二节节选 (真实形态: L1 子节 + L2 子小节 + 两段表头块 + <br> 折行 + 尾部说明)
_MD_SEC2 = """# 澜起科技股份有限公司 2025 年年度报告

## 第二节 公司简介和主要财务指标

### 一、公司信息

公司名称: 澜起科技股份有限公司。

### 六、近三年主要会计数据和财务指标

#### (一) 主要会计数据

单位：元 币种：人民币

|主要会计数据|2025年|2024年|本期比上年<br>同期增减(%)|2023年|
|---|---|---|---|---|
|营业收入|5,456,316,783.63|3,638,911,068.29|49.94|2,285,738,498.23|
|归属于上市公司股<br>东的净利润|2,235,569,970.18|1,411,778,923.59|58.35|450,909,813.13|
||2025年末|2024年末|本期末比上<br>年同期末增<br>减（%）|2023年末|
|归属于上市公司股<br>东的净资产|12,923,722,372.46|11,403,438,067.08|13.33|10,191,406,155.95|

2025 年度，公司利润总额较上年度增长 64.27%。相关财务数据大幅增长主要原因包括：
（1）营业收入较上年度增长 49.94%；（2）毛利率较上年度提升 4.10 个百分点。

#### (二) 主要财务指标

|主要财务指标|2025年|2024年|本期比上年同期增减(%)|2023年|
|---|---|---|---|---|
|基本每股收益（元／股）|1.96|1.25|56.80|0.40|

### 九、非经常性损益项目和金额

|非经常性损益项目|2025年金额|2024年金额|2023年金额|
|---|---|---|---|
|非流动资产处置损益|-1,234.56|-2,345.67|-3,456.78|
|政府补助（与公司正常经营业务密切相关、符合国家政策规定、按照一定标准定额或定量持续享受的政府补助除外）|83,611,983.94|51,272,343.87|38,401,145.32|
|合计|102,203,306.94|76,953,402.31|57,364,238.29|

报告期非经常性损益总额较上年增长 32.81%，主要系政府补助增加所致。

### 十、其他
"""


# ---------- 表格解析 ----------

def test_parse_md_tables_basic_block():
    tables = parse_md_tables(
        "|主要会计数据|2025年|2024年|\n|---|---|---|\n"
        "|营业收入|100|80|\n|利润总额|60|40|")
    assert len(tables) == 1
    assert tables[0]["header"] == ["主要会计数据", "2025年", "2024年"]
    assert tables[0]["rows"][0] == ["营业收入", "100", "80"]


def test_parse_md_tables_two_header_blocks():
    """近三年表实为两段表头块 (年度数据+年末数据), 各自成表"""
    tables = parse_md_tables(
        "|指标|2025年|2024年|\n|---|---|---|\n|营业收入|100|80|\n"
        "|指标|2025年末|2024年末|\n|---|---|---|\n|净资产|900|800|")
    assert len(tables) == 2
    assert tables[1]["header"][1] == "2025年末"
    assert tables[1]["rows"][0] == ["净资产", "900", "800"]


def test_parse_md_tables_br_cell_join():
    """<br> 折行去除后跨行词接合 (实证: '归属于上市公司股<br>东的净利润')"""
    tables = parse_md_tables(
        "|项目|金额|\n|---|---|\n|归属于上市公司股<br>东的净利润|1,000|")
    assert tables[0]["rows"][0][0] == "归属于上市公司股东的净利润"


def test_parse_md_tables_skips_empty_rows():
    tables = parse_md_tables(
        "|项目|金额|\n|---|---|\n|||\n|营业收入|100|")
    assert len(tables[0]["rows"]) == 1


# ---------- 子章节提取 ----------

def test_extract_sec2_both_targets():
    out = extract_sec2(_MD_SEC2)
    assert set(out) == {"kpi3y", "nonrecurring"}

    k = out["kpi3y"]
    assert k["title"] == "近三年主要会计数据和财务指标"  # 编号在切分时剥离
    # (一)主要会计数据=1表(含内嵌口径行) + (二)主要财务指标=1表
    assert len(k["tables"]) == 2
    assert k["tables"][0]["rows"][2][1] == "2025年末"   # 内嵌口径切换行在
    assert k["tables"][0]["rows"][0][0] == "营业收入"
    # 指标说明文字随父小节全文保留
    assert "毛利率较上年度提升 4.10 个百分点" in k["text"]

    n = out["nonrecurring"]
    assert len(n["tables"]) == 1
    row0 = n["tables"][0]["rows"][0]
    assert row0[0] == "非流动资产处置损益"
    assert "政府补助增加所致" in n["text"]


def test_extract_sec2_by_name_not_number():
    """编号无关: 药明式 '七、/十、' 与澜起式 '六、/九、' 同样命中 (关键词匹配)"""
    md_wuxi = _MD_SEC2.replace("### 六、近三年主要会计数据", "### 七、近三年主要会计数据")
    out = extract_sec2(md_wuxi)
    assert "kpi3y" in out


def test_extract_sec2_miss_tolerant():
    out = extract_sec2("# 无目标内容的年报\n\n## 第二节\n\n### 一、公司信息\n无表格。")
    assert out == {}


# ---------- 主题式方言 (A+H/央企, 中石化600028实证: 全篇无「第X节」) ----------

_MD_TOPIC = """# 中国石化 2025 年年度报告

## 目录

目录内容。

## 公司简介

公司基本情况简介。

## 主要财务数据及指标

|主要会计数据|2025年|2024年|
|---|---|---|
|营业收入|30,000亿|28,000亿|

非经常性损益合计 120 亿元, 主要为政府补助。

## 经营业绩回顾及展望

油气当量产量增长, 炼化毛利改善。

## 公司治理

员工总数 38 万人。

## 重要事项

无重大事项。

## 股东情况

股东总数 60 万户。

## 财务报表

附注。
"""


def test_topic_dialect_sections_and_chunks():
    """主题式: ## 主题名即节, 整节成块 (无「一、二、」小节)"""
    from skills.content.annual_core import split_sections_titled
    keys = [k for k, _, _ in split_sections_titled(_MD_TOPIC)]
    assert "summary" in keys and "mdna" in keys and "governance" in keys
    assert "definitions" not in keys          # 目录/未映射主题跳过

    chunks = extract_chunks(_MD_TOPIC)
    by = {c["section_key"]: c for c in chunks}
    assert by["mdna"]["title"] == "经营业绩回顾及展望"     # 整节单块
    assert "油气当量产量" in by["mdna"]["text"]
    assert "38 万人" in by["governance"]["text"]


def test_topic_dialect_sec2():
    """主题式 sec2: 主要财务数据主题整块即 kpi3y; 非经常损益随主题文本入库"""
    out = extract_sec2(_MD_TOPIC)
    assert "kpi3y" in out
    assert "营业收入" in out["kpi3y"]["text"]
