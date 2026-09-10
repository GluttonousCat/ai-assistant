"""forge 结构校验纯函数测试 (无 DB / 无 LLM)"""
from __future__ import annotations

from skills.beta_alpha.forge import validate_chain_dict


def _ok_chain():
    return {
        "chain_id": "probe_chain",
        "name": "探针链",
        "nodes": [
            {"id": "node_a", "name": "环节A", "keywords": ["甲产品"]},
            {"id": "node_b", "name": "环节B", "keywords": ["乙产品", "B Widget"]},
            {"id": "node_c", "name": "环节C", "keywords": ["丙材料"], "sw_l2": ["元件"]},
        ],
        "edges": [["node_a", "node_b"], ["node_b", "node_c"]],
    }


def test_valid_chain_passes():
    ok, errors = validate_chain_dict(_ok_chain())
    assert ok and not errors


def test_bad_chain_id_and_missing_name():
    c = _ok_chain()
    c["chain_id"] = "Bad-ID"
    c.pop("name")
    ok, errors = validate_chain_dict(c)
    assert not ok
    assert any("chain_id" in e for e in errors)
    assert any("name" in e for e in errors)


def test_edge_to_undefined_node():
    c = _ok_chain()
    c["edges"].append(["node_a", "ghost"])
    ok, errors = validate_chain_dict(c)
    assert not ok and any("未定义" in e for e in errors)


def test_duplicate_node_and_too_few_nodes():
    c = _ok_chain()
    c["nodes"][1]["id"] = "node_a"   # 与节点0重复
    ok, errors = validate_chain_dict(c)
    assert not ok and any("重复" in e for e in errors)
    ok2, _ = validate_chain_dict({"chain_id": "x_y", "name": "n",
                                  "nodes": _ok_chain()["nodes"][:2]})
    assert not ok2


def test_generic_bare_keyword_rejected():
    c = _ok_chain()
    c["nodes"][0]["keywords"] = ["整机"]
    ok, errors = validate_chain_dict(c)
    assert not ok and any("泛词" in e for e in errors)
    # 带限定的可以通过
    c["nodes"][0]["keywords"] = ["半导体设备"]
    assert validate_chain_dict(c)[0]


def test_non_mapping_rejected():
    ok, errors = validate_chain_dict(["not", "a", "dict"])
    assert not ok and errors
