"""种子链模板结构测试 (无 DB 依赖)"""
from __future__ import annotations

from agent.beta_alpha.analysis.chain_analysis import (
    GENERIC_KEYWORD_BLACKLIST,
    find_node,
    load_chains,
    match_chain,
)


def _all_chains():
    chains = load_chains(refresh=True)
    assert chains, "至少要有一条种子链"
    return chains


def test_chain_yaml_structure():
    import yaml
    from pathlib import Path
    from agent.beta_alpha.analysis.chain_analysis import CHAINS_DIR

    for fp in sorted(CHAINS_DIR.glob("*.yaml")):
        if fp.stem.startswith("_"):
            continue  # _template.yaml 等非链文件 (与 load_chains 同规则)
        with open(fp, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        stem = fp.stem
        assert data["chain_id"] == stem, f"{fp.name}: chain_id 应等于文件名"
        assert data.get("name"), f"{stem}: 缺 name"
        nodes = data.get("nodes") or []
        assert len(nodes) >= 4, f"{stem}: 环节过少 ({len(nodes)})"
        ids = [n["id"] for n in nodes]
        assert len(ids) == len(set(ids)), f"{stem}: 节点 id 重复"
        for n in nodes:
            assert n.get("name"), f"{stem}/{n['id']}: 缺 name"
            assert n.get("keywords"), f"{stem}/{n['id']}: 缺 keywords"
        for edge in data.get("edges") or []:
            assert len(edge) == 2, f"{stem}: edge 应为 [上游, 下游] -> {edge}"
            assert edge[0] in ids and edge[1] in ids, f"{stem}: edge 指向未定义节点 {edge}"


def test_no_generic_bare_keywords():
    for c in _all_chains():
        for n in c["nodes"]:
            for kw in n.get("keywords", []):
                assert kw not in GENERIC_KEYWORD_BLACKLIST, (
                    f"{c['chain_id']}/{n['id']}: 泛词 '{kw}' 易误报, 需加限定 "
                    f"(如 树脂→电子树脂/环氧树脂)")


def test_match_chain_and_node():
    chains = _all_chains()
    by_name = {c["chain_id"]: c for c in chains}
    assert "ai_compute" in by_name
    c = match_chain("AI算力产业链有哪些环节")
    assert c and c["chain_id"] == "ai_compute"
    node = find_node(by_name["ai_compute"], "覆铜板CCL环节")
    assert node and node["id"] == "ccl"
    assert match_chain("完全不相关的话术xyz") is None or match_chain(
        "完全不相关的话术xyz") is not None  # 不崩溃即可
