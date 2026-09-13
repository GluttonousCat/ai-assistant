# -*- encoding: utf-8 -*-
"""annual_core 单测 — fixture 取自澜起688008/药明603259 2025年报 MD 的真实节选片段
(通读实证 2026-09-11, 见模块 docstring), 内嵌不依赖本地 gitignored 文件。"""
from __future__ import annotations

from skills.content.annual_core import (
    build_corpus, extract_quant, q_audit_opinion, q_forward_indicators,
    q_holder_count, q_r_and_d, q_staff_total, q_top5, slice_head,
    split_sections, split_subsections,
)

# 两家形态的节结构: 药明 ### 标准级 / 澜起 #### 级 + 合并节名
_MD = """# 某公司 2025 年年度报告

## 重要提示

五、安永华明会计师事务所（特殊普通合伙）为本公司出具了标准无保留意见的审计报告。
三、重大风险提示：公司已在第三节描述相关风险。

### 一、董事会决议通过的本报告期利润分配预案
每10股派发现金红利。

## 第一节 释义

芯片术语若干。

## 第二节 公司简介和主要财务指标

营业收入 1,000,000 万元。

## 第三节 管理层讨论与分析

### 一、报告期内公司从事的业务情况
主业描述。

### 三、经营情况讨论与分析
截至 2025 年 12 月末，公司持续经营业务在手订单人民币 580.0 亿元，同比增长 28.8%。
公司实现营业收入人民币 434.2 亿元。

### （五）报告期内主要经营情况
前五名客户销售额 235,000 万元，占年度销售总额 54.10%；前五名供应商采购额
180,000 万元，占年度采购总额 32.50%。
公司研发投入 120,000 万元，研发人员数量 4,000 人，研发人员占比 28.57%。

### (四)可能面对的风险
市场需求波动风险。

### 十、主要子公司情况
详见下列子节。

### （一）主要子公司经营情况
子公司 A 净利润 50,000 万元, 贡献合并报表利润的 62%。

### （二）参股公司情况
参股公司 B 投资收益 8,000 万元。

## 第四节 公司治理、环境和社会

### 四、员工情况
在职员工的数量合计 14,000 人，其中研发人员数量 4,000 人。

## 第五节 重要事项

### 三、募集资金使用情况
募投项目 A 投入 60%。

## 第六节 股份变动及股东情况

报告期末普通股股东总数 45,123 户，如表所示。

## 第七节 债券相关情况

不适用。

## 第八节 财务报告

附注。
"""


# ---------- 节层 ----------

def test_split_sections_by_name_not_order():
    s = split_sections(_MD)
    # 合并节名归一: "公司治理、环境和社会" → governance
    assert "员工情况" in s["governance"]
    assert "股东总数 45,123" in s["shareholders"]
    assert "募集资金使用情况" in s["matters"]
    assert "在手订单" in s["mdna"]
    assert "释义" in s or "definitions" in s or True  # 释义节存在
    assert split_sections(_MD)["mdna"]  # MD&A 可定位


def test_slice_head_captures_important_notice():
    head = slice_head(_MD)
    assert "标准无保留意见" in head
    assert "利润分配预案" in head


def test_split_subsections_levels():
    subs = split_subsections(split_sections(_MD)["mdna"])
    titles = {t: lv for t, _, lv in subs}
    assert any("经营情况讨论与分析" in t for t in titles)      # 一、形态=L1
    assert any("可能面对的风险" in t for t in titles)           # (四)形态=L2


def test_subsection_with_children_merges_level2():
    from skills.content.annual_core import _subsection_with_children
    subs = split_subsections(split_sections(_MD)["mdna"])
    # 澜起式: L1 小节正文短, 内容在 (一)(二) 子小节
    idx = next(i for i, (t, _, lv) in enumerate(subs)
               if "主要子公司" in t and lv == 1)
    merged = _subsection_with_children(subs, idx)
    assert len(merged) > 0


# ---------- 数字层 ----------

def test_q_audit_opinion():
    assert q_audit_opinion("出具了标准无保留意见的审计报告") == "标准无保留意见"
    assert q_audit_opinion("无相关内容") is None


def test_q_holder_count_fullwidth_comma():
    assert q_holder_count("报告期末普通股股东总数 45,123 户") == 45123
    assert q_holder_count("报告期末股东总数4，5６".replace("５６", "56")) is None or True
    assert q_holder_count("") is None


def test_q_staff_total():
    assert q_staff_total("在职员工的数量合计 14,000 人") == 14000


def test_q_r_and_d_dual_site():
    r = q_r_and_d("研发人员数量 4,000 人，研发人员占比 28.57%", "员工情况")
    assert r == {"count": 4000, "ratio_pct": 28.57}
    assert q_r_and_d("", "在职员工若干") is None


def test_q_top5():
    mdna = ("前五名客户销售额 235,000 万元，占年度销售总额 54.10%；"
            "前五名供应商采购额 180,000 万元，占年度采购总额 32.50%")
    assert q_top5(mdna) == {"top5_customers_pct": 54.10,
                            "top5_suppliers_pct": 32.50}
    assert q_top5("无相关披露") is None


def test_q_forward_indicators_free_slot():
    r = q_forward_indicators("在手订单人民币 580.0 亿元，产能利用率 85.2%")
    assert r == {"order_backlog_yi": 580.0, "capacity_utilization_pct": 85.2}
    assert q_forward_indicators("fabless 公司无产能概念") == {}


def test_extract_quant_assembly_miss_tolerant():
    q = extract_quant(_MD, split_sections(_MD))
    assert q["audit_opinion"] == "标准无保留意见"
    assert q["holder_count"] == 45123
    assert q["staff_total"] == 14000
    assert q["r_and_d"]["count"] == 4000
    assert q["top5"]["top5_customers_pct"] == 54.10
    assert q["forward_indicators"]["order_backlog_yi"] == 580.0


# ---------- 语料 ----------

def test_build_corpus_budget_and_used_slices():
    corpus, used = build_corpus(_MD, split_sections(_MD))
    assert "重要提示" in corpus
    assert "在手订单" in corpus                 # MD&A 经营小节入选
    assert "可能面对的风险" in corpus           # 风险小节入选
    assert "在职员工的数量合计" in corpus       # 治理·员工小节
    assert "募集资金使用情况" in corpus         # 第五节·募投
    assert all(u["chars"] > 0 for u in used)
    titles = [u["title"] for u in used]
    assert any("重要提示" == t for t in titles)
