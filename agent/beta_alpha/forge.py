"""
种子链锻造 (forge) — 双通道建链:

1. LLM 生成: 主题 -> 草稿(喂真实披露语料样本) -> 结构校验 -> 关键词命中率校验
   (vs 74.5万行主营构成) -> 命中不足自动修正一轮 -> 落盘
2. 用户自建: 手写/编辑 YAML -> save_chain 结构校验后落盘; --check 出命中率报告

用法(命令行):
    python -m agent.beta_alpha.forge "苹果代工产业链"            # 生成并保存
    python -m agent.beta_alpha.forge "磷化工" --no-save          # 只看草稿
    python -m agent.beta_alpha.forge --check ai_compute          # 校验已有链
"""
from __future__ import annotations

import random
import re
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

import yaml

from core.logger import get_logger
from agent.beta_alpha.analysis import chain_analysis as ca
from agent.beta_alpha.analysis.chain_analysis import (
    CHAINS_DIR,
    GENERIC_KEYWORD_BLACKLIST,
    _load_latest_mainbz,
    get_chain,
    load_chains,
)
from agent.beta_alpha.prompts import CHAIN_FORGE_PROMPT, CHAIN_REFINE_PROMPT

logger = get_logger(__name__)

_CHAIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_CORPUS_SAMPLE_N = 120
_MAX_YAML_CHARS = 20000


# ============================================================
# 结构校验 (纯函数, tests 验证)
# ============================================================

def validate_chain_dict(data) -> Tuple[bool, List[str]]:
    """链模板结构校验: 字段/格式/引用完整性/泛词纪律"""
    errors: List[str] = []
    if not isinstance(data, dict):
        return False, ["顶层必须是 YAML mapping"]

    cid = str(data.get("chain_id") or "")
    if not _CHAIN_ID_RE.match(cid):
        errors.append("chain_id 须为小写英文+下划线, 2-41 字符")
    if not data.get("name"):
        errors.append("缺 name (中文链名)")

    nodes = data.get("nodes")
    if not isinstance(nodes, list) or len(nodes) < 3:
        errors.append("nodes 至少 3 个环节")
        nodes = nodes if isinstance(nodes, list) else []

    ids = []
    for i, n in enumerate(nodes, 1):
        if not isinstance(n, dict):
            errors.append(f"节点{i} 不是 mapping")
            continue
        nid = str(n.get("id") or "")
        if not _CHAIN_ID_RE.match(nid):
            errors.append(f"节点{i} id 非法: {nid!r}")
        if nid in ids:
            errors.append(f"节点 id 重复: {nid}")
        ids.append(nid)
        if not n.get("name"):
            errors.append(f"节点{nid or i} 缺 name")
        kws = n.get("keywords")
        if not isinstance(kws, list) or not kws:
            errors.append(f"节点{nid or i} 缺 keywords")
        else:
            for kw in kws:
                if str(kw).strip() in GENERIC_KEYWORD_BLACKLIST:
                    errors.append(
                        f"节点{nid or i} 裸泛词 '{kw}' (须带限定, 如 电子树脂/半导体设备)")

    edges = data.get("edges") or []
    if not isinstance(edges, list):
        errors.append("edges 须为列表")
        edges = []
    for e in edges:
        if not (isinstance(e, (list, tuple)) and len(e) == 2):
            errors.append(f"edge 应为 [上游, 下游]: {e!r}")
            continue
        if e[0] not in ids or e[1] not in ids:
            errors.append(f"edge 指向未定义节点: {list(e)}")
    return (not errors), errors


# ============================================================
# 关键词命中率 (vs 最新报告期主营构成, 内存匹配, 复用素材缓存)
# ============================================================

def _corpus_index() -> List[Tuple[str, str, List[Tuple[str, str, float]]]]:
    """[(ts_code, blob, [(item_lower, item, share>=5)])]"""
    idx = []
    for code, d in _load_latest_mainbz().items():
        if d["total"] <= 0:
            continue
        items = [(it.lower(), it, sales / d["total"] * 100)
                 for it, sales, _sub in d["items"]]
        items = [t for t in items if t[2] >= 5]
        if not items:
            continue
        blob = " | ".join(t[0] for t in items)
        idx.append((code, blob, items))
    return idx


def keyword_hit_report(chain: Dict) -> Dict:
    """每环节每关键词的命中股票数与披露样例 (生成闭环与 --check 共用)"""
    idx = _corpus_index()
    nodes_report = []
    for node in chain.get("nodes", []):
        kw_rows = []
        node_codes = set()
        for kw in [str(k) for k in node.get("keywords", [])]:
            k = kw.lower()
            stocks = 0
            samples: List[str] = []
            for code, blob, items in idx:
                if k not in blob:
                    continue
                stocks += 1
                node_codes.add(code)
                if len(samples) < 4:
                    samples.append(next(o for l, o, _s in items if k in l)[:24])
            kw_rows.append({"keyword": kw, "stocks": stocks, "samples": samples})
        nodes_report.append({
            "id": node.get("id"), "name": node.get("name", node.get("id")),
            "keywords": kw_rows, "total_stocks": len(node_codes),
        })
    return {"nodes": nodes_report}


def hit_report_text(report: Dict) -> str:
    """命中率报告的纯文本形态 (喂给 LLM 修正 / CLI 打印)"""
    lines = []
    for n in report.get("nodes", []):
        parts = []
        for kw in n["keywords"]:
            mark = "✗" if kw["stocks"] == 0 else "✓"
            parts.append(f"{kw['keyword']}={kw['stocks']}只{mark}")
        lines.append(f"- {n['name']}: {'; '.join(parts)} (环节合计 {n['total_stocks']} 只)")
    return "\n".join(lines) or "(无)"


def _corpus_sample_text() -> str:
    items = set()
    for _code, _blob, rows in _corpus_index():
        for _l, o, _s in rows:
            items.add(o)
    pool = sorted(items)
    random.seed(42)
    sample = random.sample(pool, min(_CORPUS_SAMPLE_N, len(pool)))
    return "、".join(sample)


# ============================================================
# LLM 生成闭环
# ============================================================

def _parse_yaml_llm(text: str) -> Optional[Dict]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("yaml"):
            t = t[4:]
        t = t.strip()
    try:
        data = yaml.safe_load(t)
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def forge_chain(theme: str, llm=None,
                on_stage: Optional[Callable[[str], None]] = None) -> Dict:
    """
    主题 -> 种子链 (草稿 -> 结构校验 -> 命中率 -> 弱则修正一轮)。
    返回 {chain, report, rounds, errors}; chain 可能为 None (生成失败)。
    """
    from core.llm.client import get_agent_llm
    llm = llm or get_agent_llm()

    def _stage(msg):
        if on_stage:
            on_stage(msg)

    _stage("LLM 草稿生成中 (约 1 分钟)…")
    prompt = CHAIN_FORGE_PROMPT.format(
        corpus_sample=_corpus_sample_text(), theme=theme)
    chain = None
    errors: List[str] = []
    for attempt in (1, 2):   # 结构不合法带错误重试一次
        raw = llm.invoke(prompt)
        chain = _parse_yaml_llm(raw)
        ok, errors = validate_chain_dict(chain)
        if ok:
            break
        _stage(f"结构校验未过 ({len(errors)} 项), 重试…")
        prompt = (f"你上次输出的 YAML 存在以下问题:\n"
                  + "\n".join(f"- {e}" for e in errors)
                  + f"\n\n请修正后重新输出完整 YAML。\n\n主题: {theme}")
        chain = None
    if chain is None:
        return {"chain": None, "report": None, "rounds": attempt, "errors": errors}

    _stage("关键词命中率校验 (vs 74.5 万行主营构成)…")
    report = keyword_hit_report(chain)
    weak_kws = sum(1 for n in report["nodes"] for kw in n["keywords"]
                   if kw["stocks"] == 0)
    weak_nodes = sum(1 for n in report["nodes"] if n["total_stocks"] == 0)

    rounds = 1
    if weak_kws > 0 or weak_nodes > 0:
        _stage(f"命中不足 (零命中关键词 {weak_kws} 个/零命中环节 {weak_nodes} 个), LLM 修正一轮…")
        refined = None
        for attempt in (1, 2):   # 修正轮重试一次 (长 prompt 偶发超时)
            try:
                refined = _parse_yaml_llm(llm.invoke(CHAIN_REFINE_PROMPT.format(
                    hit_report=hit_report_text(report),
                    chain_yaml=yaml.safe_dump(chain, allow_unicode=False, sort_keys=False))))
                if refined:
                    break
            except Exception as e:
                logger.warning(f"forge 修正轮第{attempt}次失败: {e}")
                refined = None
        if refined:
            ok2, errors2 = validate_chain_dict(refined)
            report2 = keyword_hit_report(refined) if ok2 else None
            weak2 = sum(1 for n in (report2 or {"nodes": []})["nodes"]
                        for kw in n["keywords"] if kw["stocks"] == 0)
            if ok2 and weak2 <= weak_kws:
                chain, report, rounds = refined, report2, 2

    return {"chain": chain, "report": report, "rounds": rounds, "errors": []}


# ============================================================
# 落盘 (LLM 生成 / 用户自建共用)
# ============================================================

def chain_to_yaml(chain: Dict) -> str:
    body = yaml.safe_dump(chain, allow_unicode=True, sort_keys=False, width=100)
    return (f"# source: forge 保存于 {date.today().isoformat()}\n"
            f"# 命中率复查: python -m agent.beta_alpha.forge --check {chain.get('chain_id')}\n"
            + body)


def save_chain_yaml(yaml_text: str, source: str = "manual") -> Tuple[bool, List[str], Optional[str]]:
    """校验 + 落盘 beta_alpha/chains/<chain_id>.yaml; 成功返回 (True, [], path)"""
    if not yaml_text or len(yaml_text) > _MAX_YAML_CHARS:
        return False, ["YAML 内容为空或超过 20KB 上限"], None
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return False, [f"YAML 解析失败: {e}"], None
    ok, errors = validate_chain_dict(data)
    if not ok:
        return False, errors, None
    cid = str(data["chain_id"])
    if not _CHAIN_ID_RE.match(cid):
        return False, [f"chain_id 非法: {cid}"], None
    if not data.get("source"):
        data["source"] = source
    path = CHAINS_DIR / f"{cid}.yaml"
    path.write_text(chain_to_yaml(data), encoding="utf-8")
    ca._chains_cache = None   # 清链模板缓存, 新链即刻可被 match/get 发现
    ca._result_cache.clear()  # 同 id 重存(编辑)时避免读到旧分析结果
    logger.info(f"种子链已保存: {path.name} (source={source})")
    return True, [], str(path)


# ============================================================
# CLI
# ============================================================

def _print_report(report: Dict) -> None:
    print(hit_report_text(report))
    zero = [kw["keyword"] for n in report["nodes"] for kw in n["keywords"]
            if kw["stocks"] == 0]
    if zero:
        print(f"\n⚠ 主营构成零命中关键词 {len(zero)} 个: {', '.join(zero[:10])}"
              f"{'…' if len(zero) > 10 else ''}"
              f"\n  (仅指主营构成披露不匹配; 英文缩写类如 GPU/HBM 常只命中研报tags, 环节已有"
              f"其他关键词命中时无需处理; 环节整体零命中才必须改)")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="种子链锻造/校验")
    parser.add_argument("theme", nargs="?", help="产业链主题 (如 '苹果代工产业链')")
    parser.add_argument("--check", metavar="CHAIN_ID", help="校验已有链的命中率")
    parser.add_argument("--no-save", action="store_true", help="只生成不落盘")
    args = parser.parse_args()

    if args.check:
        chain = get_chain(args.check) or _load_chain_file(args.check)
        if not chain:
            print(f"链不存在: {args.check}")
            raise SystemExit(1)
        ok, errors = validate_chain_dict(chain)
        print(f"结构校验: {'通过' if ok else '未通过'}")
        for e in errors:
            print("  -", e)
        _print_report(keyword_hit_report(chain))
        return

    if not args.theme:
        parser.error("需要主题参数, 或使用 --check CHAIN_ID")
    res = forge_chain(args.theme, on_stage=lambda m: print(f"[forge] {m}"))
    if res["chain"] is None:
        print("生成失败:", *res["errors"], sep="\n  - ")
        raise SystemExit(1)
    print(f"\n==== {res['chain']['name']} ({res['chain']['chain_id']}) "
          f"rounds={res['rounds']} ====")
    print(chain_to_yaml(res["chain"]))
    _print_report(res["report"])
    if not args.no_save:
        ok, errors, path = save_chain_yaml(chain_to_yaml(res["chain"]), source="llm")
        print("\n已保存:" if ok else "\n保存失败:", path if ok else errors)
        print(f"预览: python -m agent.beta_alpha.analysis.chain_analysis {res['chain']['chain_id']}")


def _load_chain_file(chain_id: str) -> Optional[Dict]:
    fp = CHAINS_DIR / f"{chain_id}.yaml"
    if not fp.exists():
        return None
    with open(fp, encoding="utf-8") as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    main()
