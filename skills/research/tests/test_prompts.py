# -*- encoding: utf-8 -*-
"""T05 十章模板与提示词库测试 — 纯离线 (不连 LLM/DB)。

分组:
  test_chapters   十章齐全/六字段非空/wants 闭集/股票无关
  test_style      全局文风纪律 prompt 含三层分离关键词
  test_prompt     chapter_prompt 组装: 注入/占位/查表/假设 JSON/花括号保全
  test_imports    静态检查不 import 重模块 (禁止 harness/tools/storage/core)
"""
import ast
from pathlib import Path

import pytest

from skills.research.prompts_chapters import (
    CHAPTERS,
    EVIDENCE_CATEGORIES,
    HYPOTHESES_CHAPTER_NO,
    chapter_prompt,
)
from skills.research.prompts_style import STYLE_PROMPT

_RESEARCH_DIR = Path(__file__).resolve().parent.parent

# 模板必须股票无关 — 指南示例公司与首案例公司都不许出现 (T05 禁止条款)
_BANNED_COMPANY_TOKENS = ("600519", "贵州茅台", "茅台", "中芯国际", "光芯片")


class TestChapters:
    def test_ten_chapters_in_order(self):
        assert [c["no"] for c in CHAPTERS] == list(range(1, 11))

    def test_titles_unique(self):
        assert len({c["title"] for c in CHAPTERS}) == 10

    @pytest.mark.parametrize("c", CHAPTERS, ids=lambda c: f"ch{c['no']}")
    def test_fields_nonempty(self, c):
        for key in ("no", "title", "question", "wants", "structure", "length_hint"):
            assert c.get(key), f"第{c['no']}章字段 {key} 为空"
        assert isinstance(c["structure"], list) and len(c["structure"]) >= 3
        assert isinstance(c["wants"], list) and c["wants"]

    @pytest.mark.parametrize("c", CHAPTERS, ids=lambda c: f"ch{c['no']}")
    def test_wants_in_closed_vocab(self, c):
        unknown = set(c["wants"]) - set(EVIDENCE_CATEGORIES)
        assert not unknown, f"第{c['no']}章 wants 越界: {unknown}"

    def test_every_category_used(self):
        used = {w for c in CHAPTERS for w in c["wants"]}
        assert used == set(EVIDENCE_CATEGORIES)

    @pytest.mark.parametrize("c", CHAPTERS, ids=lambda c: f"ch{c['no']}")
    def test_stock_agnostic(self, c):
        joined = "".join([c["title"], c["question"], *c["structure"], c["length_hint"]])
        for token in _BANNED_COMPANY_TOKENS:
            assert token not in joined, f"第{c['no']}章写死了公司数据: {token}"


class TestStylePrompt:
    def test_three_layer_keywords(self):
        for kw in ("[E:", "[V:", "待验证", "机构", "验证指标"):
            assert kw in STYLE_PROMPT, f"STYLE_PROMPT 缺三层分离关键词: {kw}"

    def test_layers_named(self):
        for kw in ("事实", "观点", "判断"):
            assert kw in STYLE_PROMPT

    def test_four_mechanisms(self):
        assert "机制" in STYLE_PROMPT
        for kw in ("经营", "竞争", "变化", "验证"):
            assert kw in STYLE_PROMPT

    def test_no_marketing_tone_and_no_advice(self):
        assert "营销腔" in STYLE_PROMPT
        assert "不做投资建议" in STYLE_PROMPT

    def test_units_and_language(self):
        for kw in ("亿元", "万元", "%", "简体中文"):
            assert kw in STYLE_PROMPT

    def test_citation_rendering_note(self):
        assert "脚注" in STYLE_PROMPT

    def test_stock_agnostic(self):
        for token in _BANNED_COMPANY_TOKENS:
            assert token not in STYLE_PROMPT


class TestChapterPrompt:
    FACTS = "[T:profile.mainbiz.2025A] 主营构成: A 产品收入占比 60%"
    VIEWS = "[R:12.1] 高盛(2026-01-05)认为行业需求将回升"

    def test_injects_all_sections(self):
        ch = CHAPTERS[0]
        prompt = chapter_prompt(ch, self.FACTS, self.VIEWS)
        assert STYLE_PROMPT in prompt                    # 全局文风前缀
        assert ch["question"] in prompt
        assert ch["length_hint"] in prompt
        for item in ch["structure"]:
            assert item in prompt
        assert self.FACTS in prompt and self.VIEWS in prompt

    def test_empty_digests_get_placeholder(self):
        prompt = chapter_prompt(CHAPTERS[3])
        assert "待验证" in prompt
        assert "机构观点" in prompt

    def test_accepts_int_chapter_no(self):
        assert chapter_prompt(1) == chapter_prompt(CHAPTERS[0])
        with pytest.raises(KeyError):
            chapter_prompt(99)

    def test_hypotheses_request_only_on_configured_chapter(self):
        for c in CHAPTERS:
            prompt = chapter_prompt(c)
            if c["no"] == HYPOTHESES_CHAPTER_NO:
                assert "```json" in prompt
                assert '"statement"' in prompt
                assert '"indicators"' in prompt
                assert '"related_eids"' in prompt
            else:
                assert "```json" not in prompt

    def test_json_braces_survive(self):
        """JSON 示例花括号原样保留 (AGENTS 坑: .format 花括号曾被吃掉)"""
        prompt = chapter_prompt(CHAPTERS[HYPOTHESES_CHAPTER_NO - 1])
        assert '[{"statement": "假设陈述(一句话)", "indicators": ' in prompt

    def test_stock_agnostic(self):
        for c in CHAPTERS:
            for token in _BANNED_COMPANY_TOKENS:
                assert token not in chapter_prompt(c)


class TestImports:
    """禁止 import harness/tools/storage/core (T05 禁止条款的静态检查)"""

    @pytest.mark.parametrize("mod", ["prompts_chapters", "prompts_style"])
    def test_no_heavy_imports(self, mod):
        tree = ast.parse(
            (_RESEARCH_DIR / f"{mod}.py").read_text(encoding="utf-8"))
        names = [a.name for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for a in node.names]
        names += [node.module for node in ast.walk(tree)
                  if isinstance(node, ast.ImportFrom) and node.module]
        for name in names:
            root = name.split(".")[0]
            assert root not in ("harness", "tools", "storage", "core"), \
                f"{mod}.py 引入了禁用模块: {name}"
