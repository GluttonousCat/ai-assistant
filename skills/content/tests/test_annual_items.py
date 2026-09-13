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


def test_marker_rank_segment_depth():
    """N.M vs N.M.K 段数分秩 — 兴业 '4.1.6'/'4.1.6.1' 与 '4.1' 同秩曾误切边界"""
    assert marker_rank("#### 4.1 治理护航") == 3.0
    assert marker_rank("##### **4.1.6** 董事、监事和高级管理人员情况") == 3.1
    assert marker_rank("#### 4.1.6.1 现任及离任董事情况") == 3.2
    # 顿号后空格曾是根因: 澜起 '#### 二、 经营情况讨论与分析' 被判 4.0 深层
    assert marker_rank("#### 二、 经营情况讨论与分析") == 2.5


# ---------- 节层: 双方言 + §3 五节规则 ----------

from skills.content.annual_items import (  # noqa: E402
    _alias_hit, extract_sections, split_sections_titled,
)

_MD_A = """# 某公司 2025 年年度报告 (准则式, 仿澜起)

## 目录

第三节 管理层讨论与分析 …… 27

## 第一节 释义

释义内容。

## 第三节 管理层讨论与分析

#### 一、报告期内公司所从事的主要业务

业务说明。

#### 二、 经营情况讨论与分析

收入增长 30%。

#### 三、风险因素

市场风险。

#### (一) 市场波动风险

波动细节。

## 第四节 公司治理

#### 一、公司治理相关情况说明

治理概况 (不入)。

#### 五、董事和高级管理人员的情况

高管 8 人。

#### (五) 员工情况

在职员工的数量合计 14,000 人。

## 第五节 重要事项

#### 一、承诺事项履行情况

承诺均已履行。

#### 八、破产重整相关事项

不适用 (不入)。

## 第六节 股份变动及股东情况

报告期末普通股股东总数 45,123 户。

#### 一、股份变动情况表

总股本 11.3 亿股。

## 第七节 债券相关情况

不适用。

## 第八节 财务报告

报表正文。
"""


def test_sections_dialect_a_rules():
    """准则式 §3 全规则: mdna 全量含子节/治理与事项关键词/六节前文/七节跳短"""
    audit: dict = {}
    secs = extract_sections(_MD_A, audit=audit)
    assert [s["title"] for s in secs["mdna"]["subsections"]] == [
        "报告期内公司所从事的主要业务", "经营情况讨论与分析", "风险因素"]
    # (一) 子节并入父块, 标题转 [标题] 行保留
    risk = secs["mdna"]["subsections"][2]
    assert "[市场波动风险]" in risk["text"] and "波动细节" in risk["text"]
    # 治理关键词: 只收 董监高 块 (员工情况是其 L2 子节随之带入)
    gov = secs["governance"]["subsections"]
    assert [s["title"] for s in gov] == ["董事和高级管理人员的情况"]
    assert "14,000" in gov[0]["text"]
    assert [s["title"] for s in secs["matters"]["subsections"]] == ["承诺事项履行情况"]
    # 六节: 节首前文 (股东总数所在) + 整节小节
    sh = secs["shareholders"]["subsections"]
    assert sh[0]["title"] == "(节首前文)" and "45,123" in sh[0]["text"]
    assert sh[1]["title"] == "股份变动情况表"
    # 七节 <500 字跳过
    assert "bonds" not in secs
    assert audit["sections"]["bonds"]["status"] == "skip_short"


_MD_C = """# 某银行 2025 年年度报告 (章式+数字编号, 仿兴业)

### **第一章 公司简介**

#### 1.1 公司概况

概况内容。

### **第二章 财务摘要**

#### 2.1 主要会计数据和财务指标

|指标|2025年|
|---|---|
|营收|100|

### **第三章 管理层讨论与分析**

#### 3.1 战略实施情况

战略叙述。

#### 3.1.1 深层小节

深层内容。

#### 3.2 风险管理

银行风险叙述。

### **第四章 公司治理、环境与社会**

#### 4.1 治护护航

ESG 内容 (无关键词命中)。

### **第五章 重要事项**

#### 5.1 承诺事项

承诺叙述。

#### 5.5 公司及董事、监事、高级管理人员受处 罚情况

处罚内容 (无关键词命中)。
"""


def test_sections_dialect_c():
    """兴业式: '### **第X章**' 粗体章锚 + N.M 小节 + N.M.K 深层 + 折行空格标题"""
    audit: dict = {}
    secs = extract_sections(_MD_C, audit=audit)
    assert [s["title"] for s in secs["mdna"]["subsections"]] == [
        "战略实施情况", "风险管理"]
    assert "[深层小节]" in secs["mdna"]["subsections"][0]["text"]
    # 治理零关键词命中 → 跳过不兜底 (用户决策「不和规范的先跳过」)
    assert "governance" not in secs
    assert audit["sections"]["governance"]["status"] == "skip_no_keyword"
    # 事项: 5.1 承诺收, 5.5 处罚 (空格标题) 不在关键词内 → 排除
    assert [s["title"] for s in secs["matters"]["subsections"]] == ["承诺事项"]
    assert extract_items(_MD_C)["kpi3y"]["tables"][0]["rows"] == [["营收", "100"]]


_MD_B = """# 某央企 2025 年年度报告 (主题式, 仿中石化, 全篇无「第X节」)

## 目录

## 公司简介

简介内容。

## 管理层讨论与分析

以下讨论与分析应与财务报表同时阅读。

#### 1 合并经营业绩

营业收入 27,836 亿元。

## 公司治理、环境和社会

治理叙述。

## 重要事项

事项叙述。

## 股份变动及主要股东持股情况

股东叙述。

## 董事长致辞

致辞 (未映射, 不入)。
"""


def test_sections_dialect_b_skips_without_numbered_anchors():
    """主题式: 节定位成立但节内无编号小节 (阿拉伯 '**1**' 式) → 跳过留审计"""
    titled = split_sections_titled(_MD_B)
    assert [k for k, _t, _x in titled] == [
        "summary", "mdna", "governance", "matters", "shareholders"]
    audit: dict = {}
    secs = extract_sections(_MD_B, audit=audit)
    assert secs == {}
    assert audit["sections"]["mdna"]["status"] == "skip_no_anchors"
    assert audit["sections"]["governance"]["status"] == "skip_no_anchors"


def test_dialect_threshold_toc_residue():
    """目录残留 1~3 个「第X节」不误判准则式 (≥4 才判, 兴业实证踩坑)"""
    md = ("## 第一节 释义 …… 3\n\n## 主要财务数据及指标\n\n数据。\n\n"
          "## 管理层讨论与分析\n\n讨论。\n\n## 公司治理\n\n治理。\n\n"
          "## 重要事项\n\n事项。\n\n## 股份变动及股东情况\n\n股东。\n")
    titled = split_sections_titled(md)
    assert "mdna" in [k for k, _t, _x in titled]      # 走主题式而非准则式


# ---------- 第二章相似度兜底 ----------

def test_alias_fuzzy_fallback():
    """措辞变体 (与/和, 非任何别名子串) → 相似度兜底命中, 审计记 fuzzy"""
    md = ("#### 一、会计数据与财务指标摘要\n\n"
          "|指标|2025年|\n|---|---|\n|营收|83|\n\n" + "说明文字。" * 40)
    audit: dict = {}
    out = extract_items(md, audit=audit)
    assert "kpi3y" in out
    assert audit["items"]["kpi3y"]["status"] == "fuzzy"
    assert audit["items"]["kpi3y"]["score"] < 1.0
    # 叙述句不入兜底: 长行叙述相似度再高也被结构守卫拦下
    assert _alias_hit("报告期末公司前三年主要会计数据和财务指标如下表所示，详见后文。",
                      ["主要会计数据和财务指标"]) is None


def test_alias_exact_still_prefers_word_list():
    md = ("#### 九、非经常性损益项目及涉及金额\n\n"
          "|项目|金额|\n|---|---|\n|补助|100|\n\n" + "说明。" * 40)
    audit: dict = {}
    extract_items(md, audit=audit)
    assert audit["items"]["nonrecurring"]["status"] == "exact"
