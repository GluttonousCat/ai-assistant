"""
Layered Intent Classifier.

Design:
- L1 rule layer: keyword + confidence scoring (deterministic, zero cost, instant response)
- L2 LLM layer: call LLM which outputs JSON (handles complex / colloquial phrasing)
- Decision: high-confidence L1 returns directly; otherwise L2; low-confidence L2 returns unknown

Confidence semantics:
- L1: core keyword hit 0.9, general keyword hit 0.7, combination-weighted
- L2: the model's own reported confidence (0-1)

Usage:
    from harness.intent import LayeredIntentClassifier
    clf = LayeredIntentClassifier()
    intent, conf, slots = clf.classify("看看贵州茅台近三年的毛利率")
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# Rule definitions (L1)
# ============================================================

# Core keywords: a hit yields high confidence
CORE_KEYWORDS: Dict[str, List[str]] = {
    "query": ["营收", "营业收入", "净利润", "净利率", "毛利率", "毛利", "roe", "净资产收益率",
              "回报率", "市盈率", "市净率", "股价", "收盘价", "最新价", "市值", "涨跌幅",
              "涨幅", "换手率", "资产负债率", "流动比率", "速动比率", "净资产", "每股收益",
              "营业利润", "利润总额", "经营现金流", "总资产", "总负债", "商誉", "eps",
              "成交额", "成交量"],
    "detect": ["财务风险", "异常检测", "预警", "排雷", "财务造假", "风险排查"],
    "verify": ["研报校验", "预测校验", "一致性验证", "研报验证", "验证", "核对", "校验"],
    "report": ["分析报告", "深度报告", "生成报告", "完整报告", "研究报告",
               "研报", "解读", "研报解读", "评级", "目标价", "盈利预测", "观点",
               "黄金", "原油", "大宗商品", "贵金属"],
    "compare": ["对比", "比较", "谁高", "谁更高", "vs", "哪个更好", "孰强", "对比一下"],
    "chain": ["产业链", "链条", "环节", "上游", "下游", "受益环节", "板块挖掘"],
    "alpha": ["预期差", "分歧度", "拐点", "预期在哪里"],
}

# General keywords: a hit yields a 0.6 baseline
GENERAL_KEYWORDS: Dict[str, List[str]] = {
    "query": ["查询", "查一下", "查查", "多少", "收入", "利润", "排名", "筛选",
              "大于", "小于", "同比", "看", "看看", "看下",
              "收盘", "市盈", "市净", "换手", "基本面", "赚了", "多少钱"],
    "detect": ["风险", "异常", "检测"],
    "verify": ["校验", "验证", "研报", "一致性"],
    "report": ["报告", "生成", "写一份", "总结"],
    # compare colloquial patterns: "谁毛利率高"/"哪个更高"/"谁的营收更高" (broad words get 0.6, resolved by priority)
    "compare": ["谁", "哪个", "更高", "更优", "孰"],
}

# Intent priority (on ties, this order wins; verify before report prevents "验证XX的研报" being hijacked by report)
INTENT_PRIORITY = ["verify", "report", "detect", "chain", "alpha", "compare", "query"]

# L1 high-confidence threshold (>= means return directly, skip the LLM)
HIGH_CONFIDENCE_THRESHOLD = 0.75


@dataclass
class IntentResult:
    """Intent classification result."""
    intent: str
    confidence: float
    source: str  # 'rule' / 'llm' / 'fallback'
    slots: Dict[str, Any] = field(default_factory=dict)  # slots (entities)


class LayeredIntentClassifier:
    """Layered intent classifier."""

    def __init__(self, llm: Optional[Any] = None, enable_llm: bool = True):
        self.config = get_config()
        self._llm = llm
        # Enable the LLM only with a non-empty API key (empty string / placeholder do not count)
        self._enable_llm = (
            enable_llm and bool(self.config.openai_api_key)
        )

    @property
    def llm(self) -> Optional[Any]:
        """Lazy-load the LLM."""
        if self._llm is None and self._enable_llm:
            from harness.llm import LLMClient
            self._llm = LLMClient()
        return self._llm

    # ---------- Main entry ----------
    def classify(self, user_input: str) -> IntentResult:
        text = (user_input or "").strip()
        if not text:
            return IntentResult("unknown", 0.0, "fallback")

        # L1: rules
        result = self._rule_classify(text)
        if result.intent != "unknown" and result.confidence >= HIGH_CONFIDENCE_THRESHOLD:
            return result

        # L2: LLM (when a key exists)
        if self.llm:
            llm_result = self._llm_classify(text)
            if llm_result is not None and llm_result.confidence >= HIGH_CONFIDENCE_THRESHOLD:
                return llm_result
            # LLM result exists but with low confidence: pick the higher of rule vs LLM result
            if llm_result is not None and result.confidence < llm_result.confidence:
                return llm_result

        return result

    # ---------- L1 rule layer ----------
    def _rule_classify(self, text: str) -> IntentResult:
        text_l = text.lower()
        scores: Dict[str, float] = {}

        for intent in INTENT_PRIORITY:
            score = 0.0
            # Core keywords
            for kw in CORE_KEYWORDS.get(intent, []):
                if kw in text_l:
                    score = max(score, 0.9)
                    break
            # General keywords
            for kw in GENERAL_KEYWORDS.get(intent, []):
                if kw in text_l:
                    score = max(score, 0.6)
                    break
            if score > 0:
                scores[intent] = score

        # compare composite signal: metric word + comparison indicator word (谁/哪个/更高/vs) co-occurring -> strong compare signal
        # (otherwise "谁毛利率更高" would be overwhelmed by query's 0.9 metric core keyword)
        if scores.get("query") and any(
            w in text_l for w in ("谁", "哪个", "更高", "更优", "vs", "孰")
        ):
            scores["compare"] = max(scores.get("compare", 0.0), 0.95)

        # compare strong signal: text contains >= 2 stock entities ("X 和 Y 的指标")
        if scores.get("query") and self._count_stocks(text) >= 2:
            scores["compare"] = max(scores.get("compare", 0.0), 0.95)

        if not scores:
            # Entity fallback: contains both a stock + a metric entity (telegraphic style "茅台 毛利率") -> query
            if self._has_stock_and_metric(text):
                slots = self._extract_slots_rule(text)
                return IntentResult("query", 0.7, "rule", slots=slots)
            return IntentResult("unknown", 0.0, "rule")

        # Pick the highest-scoring intent
        best = max(scores, key=scores.get)
        conf = scores[best]
        slots = self._extract_slots_rule(text)
        return IntentResult(best, conf, "rule", slots=slots)

    @staticmethod
    def _count_stocks(text: str) -> int:
        """Count stock entities in text (dedup + interval exclusion, prevents "平安银行" also matching "平安")."""
        try:
            from tools.finance.stock_kb import get_stock_kb
            return get_stock_kb().count_stocks_in_text(text)
        except Exception:
            return 0

    @staticmethod
    def _has_stock_and_metric(text: str) -> bool:
        """Text hits both the stock dictionary and the metric dictionary (lightweight, millisecond-level)."""
        try:
            from tools.finance.stock_kb import get_stock_kb
            kb = get_stock_kb()
            return bool(kb.match_stock(text) and kb.match_metric(text))
        except Exception:
            return False

    # ---------- Rule-based slot extraction (stock/metric/time) ----------
    def _extract_slots_rule(self, text: str) -> Dict[str, Any]:
        """Lightweight slot extraction: only extracts core metric words (excludes action words)."""
        slots: Dict[str, Any] = {"metrics": []}

        # Only core metric words (CORE_KEYWORDS); exclude action words in GENERAL_KEYWORDS
        metric_kws = set(CORE_KEYWORDS["query"])
        for kw in metric_kws:
            if kw in text:
                slots["metrics"].append(kw)
        return slots

    # ---------- L2 LLM layer ----------
    def _llm_classify(self, text: str) -> Optional[IntentResult]:
        """Call the LLM to output: {"intent", "confidence", "stocks", "metrics", "time"}."""
        if not self.llm:
            return None
        try:
            fallback_intent = self._rule_classify(text).intent
            prompt = (
                "你是一个金融查询意图分类器。\n"
                "可分类别: query(数据查询) compare(对比) detect(异常检测) "
                "verify(研报校验) report(报告/研报解读) "
                "chain(产业链/环节挖掘, 如'AI算力链有哪些环节') "
                "alpha(个股预期差, 如'XX股票的分歧度/预期差/基本面拐点') "
                "unknown(无关)\n\n"
                "提取槽位: stocks(股票名列表) metrics(指标列表) "
                "time(时间描述,如'2023年'/'最近三年')\n\n"
                "用户输入: {text}\n\n"
                "严格只输出 JSON, 格式: "
                '{{"intent":"...","confidence":0-1,"stocks":[],"metrics":[],"time":"..."}}'
            ).format(text=text)

            resp = self.llm.invoke(prompt)
            parsed = self._parse_llm_json(resp)
            if not parsed or "intent" not in parsed:
                return None

            intent = parsed["intent"].lower()
            if intent not in INTENT_PRIORITY + ["unknown", "compare"]:
                intent = fallback_intent  # illegal category falls back to rules

            conf = float(parsed.get("confidence", 0.5))
            conf = max(0.0, min(1.0, conf))
            slots = {
                "stocks": parsed.get("stocks", []),
                "metrics": parsed.get("metrics", []),
                "time": parsed.get("time", ""),
            }
            return IntentResult(intent, conf, "llm", slots=slots)
        except Exception as e:
            logger.warning(f"LLM 意图识别失败: {e}")
            return None

    @staticmethod
    def _parse_llm_json(text: str) -> Optional[Dict[str, Any]]:
        if not text:
            return None
        t = text.strip()
        # Strip ```json ``` fences
        if t.startswith("```"):
            t = t.strip("`")
            if t.startswith("json"):
                t = t[4:]
            t = t.strip()
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            import re
            m = re.search(r"\{.*\}", t, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    return None
        return None


# ============================================================
# Convenience functions
# ============================================================

_classifier: Optional[LayeredIntentClassifier] = None


def get_intent_classifier() -> LayeredIntentClassifier:
    global _classifier
    if _classifier is None:
        _classifier = LayeredIntentClassifier()
    return _classifier