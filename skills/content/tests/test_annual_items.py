# -*- encoding: utf-8 -*-
"""annual_items 单测 — 五条硬规则各一例 + 三方言形态 fixture (澜起/中石化/兴业实证)。"""
from __future__ import annotations

from skills.content.annual_items import (
    _block_end, _clean_text, _clean_title, extract_items, marker_rank,
    parse_md_tables,
)

# 三方言混合 fixture (每段取自真实年报节选)
_MD = """# 某公司 2025 年年度报告

## 目录

主要会计数据和财务指标 …… 8

#### 六、近三年主要会计数据和财务指标 

单位：元 币种：人民币

|主要会计数据|2025年|2024年|增减(%)|
|---|---|---|---|
|营业收入|5,456,316,783.63|3,638,911,068.29|49.94|
||2025年末|2024年末|增减(%)|
|净资产|12,923,722,372.46|11,403,438,067.08|13.33|

利润总额增长 64.27%, 主要原因: 营业收入较上年度增长 49.94%。

报告期末公司前三年主要会计数据和财务指标如下表所示，详细情况请见后文。

#### (二) 主要财务指标

|基本每股收益|1.96|1.25|
|---|---|---|
|每股净资产|10.5|9.8|

#### 九、非经常性损益项目和金额 

|项目|2025年|2024年|
|---|---|---|
|政府补助|83,611,983.94|51,272,343.87|

报告期非经常性损益增长主要系政府补助增加所致。

#### 十、其他

不适用。
"""


# ---------- 标记秩 (规则2: 编号判兄弟) ----------

def test_marker_rank_numbering_over_heading_level():
    assert marker_rank("#### 六、近三年主要会计数据") == 2.5      # L1 编号
    assert marker_rank("#### (二) 主要财务指标") == 3.5           # L2 编号
    assert marker_rank("#### 2.1 主要会计数据") == 3.0            # 数字编号
    assert marker_rank("## 主要财务数据及指标") == 2.0            # 纯标题
    assert marker_rank("正文叙述行, 不是标记。") is None
    assert marker_rank("#### 六、近三年主要会计数据 ") == 2.5      # 规则3: 尾随空格


# ---------- 标题守卫 (规则1: 排除叙述句) ----------

def test_extract_skips_narrative_lines():
    out = extract_items(_MD)
    # 叙述句「报告期末公司前三年主要会计数据和财务指标如下表所示…」不得当标题:
    # kpi3y 应锚定真正的 '#### 六、' 标题行
    assert "近三年主要会计数据和财务指标" == out["kpi3y"]["title"]
    # 目录行后无正文 (短块守卫), 不会命中目录处
    assert "5,456,316,783.63" in str(out["kpi3y"]["tables"])


def test_decorated_title_allowed():
    """装饰标题 (中石化实证 '<u>(3)</u> 非经常性损益项目及涉及金额') 应放行"""
    md = "<u>(3)</u> 非经常性损益项目及涉及金额 \n\n|项目|金额|\n|---|---|\n|补助|100|\n\n说明文字。" + "x" * 120
    out = extract_items(md)
    assert "nonrecurring" in out


# ---------- 切块边界 ----------

def test_block_end_sibling_boundary():
    md = ("#### 六、近三年数据\n\n" + "内容。" * 30 + "\n\n"
          "#### (一) 子节\n子内容。\n\n#### 七、下一节\n其他。")
    end = _block_end(md, len("#### 六、近三年数据\n"), 2.5)
    # 边界应停在 '#### 七、' (L1 兄弟), (一) 子节包含在内
    assert md[end:].startswith("#### 七、")


# ---------- ③④ 分离与清洗 ----------

def test_text_tables_separation():
    out = extract_items(_MD)
    k = out["kpi3y"]
    assert "营业收入较上年度增长 49.94%" in k["text"]       # 叙述在 text
    assert "|" not in k["text"]                             # 表格已剥离
    assert "5,456" not in k["text"]                         # 数字在 tables 不在 text
    assert len(k["tables"]) >= 2                            # 两段表头块各自成表


def test_clean_title():
    assert _clean_title("#### 六、近三年主要会计数据和财务指标") == "近三年主要会计数据和财务指标"
    assert _clean_title("<u>(3)</u> 非经常性损益项目及涉及金额") == "非经常性损益项目及涉及金额"
    assert _clean_title("#### 2.1 主要会计数据和财务指标") == "主要会计数据和财务指标"


def test_clean_text_strips_markup_and_pages():
    t = _clean_text("正文<u>强调</u>内容\n\n69 / 249\n\n|表|格|\n|---|---|\n尾行")
    assert t == "正文强调内容\n尾行"


def test_unit_row_extraction():
    """单位行并表名 (规则: '单位：元' 行不再当表头)"""
    md = ("六、近三年主要会计数据和财务指标\n\n单位：元 币种：人民币\n\n"
          "|指标|2025年|\n|---|---|\n|营收|100|\n\n" + "说明。" * 60)
    out = extract_items(md)
    t = out["kpi3y"]["tables"][0]
    assert t["header"] == ["指标", "2025年"]          # 真表头
    assert t.get("unit") == "元"


# ---------- 三方言 smoke ----------

def test_dialect_a_and_digit_numbering():
    """准则式(#### 六、) 与兴业式(#### 2.1) 同一套锚定通吃"""
    md_d = ("#### 2.1 主要会计数据和财务指标\n\n"
            "|指标|金额|\n|---|---|\n|营收|200|\n\n" + "银行说明。" * 40 +
            "\n\n#### 2.2 分季度数据\n其他。")
    out = extract_items(md_d)
    assert out["kpi3y"]["title"] == "主要会计数据和财务指标"
    assert "2.2" not in out["kpi3y"]["text"]           # 兄弟边界生效
