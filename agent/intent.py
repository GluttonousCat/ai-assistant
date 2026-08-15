"""
分层意图识别器 (Layered Intent Classifier)

设计:
- L1 规则层: 关键词+置信度评分 (确定性, 零成本, 秒响应)
- L2 LLM 层: 调用 LLM 输出 JSON (处理复杂/口语化表达)
- 决策: L1 高置信直接返回; 否则 L2; L2 低置信返回 unknown

置信度语义:
- L1: 命中核心词 0.9, 命中一般词 0.7, 组合加权
- L2: 模型自身输出的 confidence (0-1)

用法:
    from agent.intent import LayeredIntentClassifier
    clf = LayeredIntentClassifier()
    intent, conf, slots = clf.classify("看看贵州茅台近三年的毛利率")
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.config import get_config
from core.logger import get_logger

logger = get_logger(__name__)


# ============================================================
# 规则定义 (L1)
# ============================================================

# 核心词: 命中即高置信
CORE_KEYWORDS: Dict[str, List[str]] = {
    "query": ["营收", "净利润", "净利率", "毛利率", "roe", "净资产收益率",
              "市盈率", "市净率", "股价", "收盘价", "市值", "涨跌幅", "换手率",
              "资产负债率", "流动比率", "净资产", "每股收益", "营业利润",
              "利润总额", "经营现金流", "总资产", "总负债", "商誉"],
    "detect": ["财务风险", "异常检测", "预警", "排雷", "财务造假", "风险排查"],
    "verify": ["研报校验", "预测校验", "一致性验证", "研报验证"],
    "report": ["分析报告", "深度报告", "生成报告", "完整报告"],
    "compare": ["对比", "比较", "谁高", "谁更高", "vs", "哪个更好", "孰强"],
}

# 一般词: 命中给 0.7 基线
GENERAL_KEYWORDS: Dict[str, List[str]] = {
    "query": ["查询", "查一下", "多少", "收入", "利润", "排名", "筛选",
              "大于", "小于", "同比", "看", "看看", "股价",
              "收盘", "市盈", "市净", "换手", "基本面"],
    "detect": ["风险", "异常", "检测"],
    "verify": ["校验", "验证", "研报", "一致性"],
    "report": ["报告", "生成", "写一份", "总结"],
}

# 意图优先级 (同分时按此顺序)
INTENT_PRIORITY = ["report", "detect", "verify", "compare", "query"]

# L1 高置信阈值 (>= 则直接返回, 不调 LLM)
HIGH_CONFIDENCE_THRESHOLD = 0.75


@dataclass
class IntentResult:
    """意图识别结果"""
    intent: str
    confidence: float
    source: str  # 'rule' / 'llm' / 'fallback'
    slots: Dict[str, Any] = field(default_factory=dict)  # 槽位 (实体)

    def to_state_dict(self) -> Dict[str, Any]:
        """转为 AgentState.parsed_intent 可存结构"""
        return {
            "type": self.intent,
            "confidence": self.confidence,
            "source": self.source,
            "slots": self.slots,
        }


class LayeredIntentClassifier:
    """分层意图分类器"""

    def __init__(self, llm: Optional[Any] = None, enable_llm: bool = True):
        self.config = get_config()
        self._llm = llm
        # API key 非空才启用 LLM (空串/占位都不算)
        self._enable_llm = (
            enable_llm and bool(self.config.openai_api_key)
        )

    @property
    def llm(self) -> Optional[Any]:
        """懒加载 LLM"""
        if self._llm is None and self._enable_llm:
            from llm.client import LLMClient
            self._llm = LLMClient()
        return self._llm

    # ---------- 主入口 ----------
    def classify(self, user_input: str) -> IntentResult:
        text = (user_input or "").strip()
        if not text:
            return IntentResult("unknown", 0.0, "fallback")

        # L1: 规则
        result = self._rule_classify(text)
        if result.intent != "unknown" and result.confidence >= HIGH_CONFIDENCE_THRESHOLD:
            return result

        # L2: LLM (有 key 时)
        if self.llm:
            llm_result = self._llm_classify(text)
            if llm_result is not None and llm_result.confidence >= HIGH_CONFIDENCE_THRESHOLD:
                return llm_result
            # LLM 结果存在但低置信, 与规则结果取高者
            if llm_result is not None and result.confidence < llm_result.confidence:
                return llm_result

        return result

    # ---------- L1 规则层 ----------
    def _rule_classify(self, text: str) -> IntentResult:
        text_l = text.lower()
        scores: Dict[str, float] = {}

        for intent in INTENT_PRIORITY:
            score = 0.0
            # 核心词
            for kw in CORE_KEYWORDS.get(intent, []):
                if kw in text_l:
                    score = max(score, 0.9)
                    break
            # 一般词
            for kw in GENERAL_KEYWORDS.get(intent, []):
                if kw in text_l:
                    score = max(score, 0.6)
                    break
            if score > 0:
                scores[intent] = score

        if not scores:
            return IntentResult("unknown", 0.0, "rule")

        # 取最高分意图
        best = max(scores, key=scores.get)
        conf = scores[best]
        slots = self._extract_slots_rule(text)
        return IntentResult(best, conf, "rule", slots=slots)

    # ---------- 规则槽位提取 (股票/指标/时间) ----------
    def _extract_slots_rule(self, text: str) -> Dict[str, Any]:
        """轻量槽位提取: 只提取核心指标词 (排除动作词)"""
        slots: Dict[str, Any] = {"metrics": []}

        # 只取核心指标词 (CORE_KEYWORDS), 排除 GENERAL_KEYWORDS 里的动作词
        metric_kws = set(CORE_KEYWORDS["query"])
        for kw in metric_kws:
            if kw in text:
                slots["metrics"].append(kw)
        return slots

    # ---------- L2 LLM 层 ----------
    def _llm_classify(self, text: str) -> Optional[IntentResult]:
        """调用 LLM 输出: {"intent", "confidence", "stocks", "metrics", "time"}"""
        if not self.llm:
            return None
        try:
            from skills.fin_query.prompts import INTENT_ROUTER_PROMPT
            fallback_intent = self._rule_classify(text).intent
            prompt = (
                "你是一个金融查询意图分类器。\n"
                "可分类别: query(数据查询) compare(对比) detect(异常检测) "
                "verify(研报校验) report(报告生成) unknown(无关)\n\n"
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
                intent = fallback_intent  # 非法类别回退规则

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
        # 去 ```json ``` 包裹
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
# 快捷函数
# ============================================================

_classifier: Optional[LayeredIntentClassifier] = None


def get_intent_classifier() -> LayeredIntentClassifier:
    global _classifier
    if _classifier is None:
        _classifier = LayeredIntentClassifier()
    return _classifier


def classify_intent(user_input: str) -> Tuple[str, float, Dict[str, Any]]:
    """便捷分类: 返回 (intent, confidence, slots)"""
    result = get_intent_classifier().classify(user_input)
    return result.intent, result.confidence, result.slots