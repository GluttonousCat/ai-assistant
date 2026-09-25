# -*- encoding: utf-8 -*-
"""T06 草稿生成器 — Stage 2 核心: 证据包 → 十章草稿 + 假设清单 + 引用校验 flags。

流程 (docs/research/tasks/T06_草稿生成器与引用校验.md):
    load_pack 载入证据分片 → 按 T05 CHAPTERS 逐章:
      wants 路由筛选证据子集 → 摘要化 (每条一行, ≤80 条, >30k 字符截断并 flag)
      → get_agent_llm().invoke 非流式生成 (失败重试 1 次, 仍败跳过并 flag)
      → 正文行内引用提取回填 used_eids
    假设 JSON 由 T05 模板在假设章 (HYPOTHESES_CHAPTER_NO=8, 情景分析; 任务书"第10章"
    系章序笔误, 以 T05 落库常量为准) 的 prompt 中追加要求; 生成器对每章输出宽容解析,
    命中"statement 数组"即剥离正文回填 Draft.hypotheses
    → citation_check 全稿校验 + T01 validate_draft 契约校验 → save_draft。

CLI:
    python -m skills.research.compose.generator 贵州茅台
    python -m skills.research.compose.generator 600519.SH --years 5

提示词库直接 import T05 (prompts_chapters.CHAPTERS/chapter_prompt/HYPOTHESES_CHAPTER_NO,
chapter_prompt 已内嵌 STYLE_PROMPT)。
测试与离线场景通过 llm= 参数注入桩, 不触真实模型。
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.logger import get_logger
from core.paths import PathManager
from skills.research.compose.citation_check import check_draft, extract_citations
from skills.research.contracts import (
    Chapter,
    Draft,
    Fact,
    Hypothesis,
    View,
    load_pack,
    load_shard,
    research_dir as default_research_dir,
    save_draft,
    validate_draft,
)
from skills.research.prompts_chapters import (
    CHAPTERS,
    HYPOTHESES_CHAPTER_NO,
    chapter_prompt,
)

logger = get_logger(__name__)

# ---------- 摘要预算 (任务书: 每条一行 ≤80 条; 超 30k 字符截断) ----------
TOTAL_ITEM_CAP = 80        # 单章证据行总数上限
FACT_ITEM_CAP = 60         # 事实/观点并存时的事实份额 (观点保底 20 条, 防被挤出)
VIEW_ITEM_CAP = 20
DIGEST_CHAR_CAP = 30000

RETRY_TIMES = 2                     # 失败章重试 1 次 (共 2 次尝试)

_TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.I)
_JSON_ARRAY_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.S)
_FENCE_WRAP_RE = re.compile(r"^```[a-zA-Z]*[ \t]*\n(.*?)\n?```\s*$", re.S)

# 假设章兜底指令 — 仅当模板 prompt 自身未含假设 JSON 要求时追加
# (注意: 本模块不用 .format 拼此串, JSON 花括号无需转义)
_HYPOTHESES_INSTRUCTION = """
## 附加要求: 假设清单 (本章独有)
正文完成后, 另起一段用 ```json 围栏输出 3~6 条研究假设, 严格按下述结构 (数组):
[{"statement": "可证伪的假设陈述 (一句)",
  "indicators": ["验证指标 (写明看什么数据/什么条件算验证或证伪)"],
  "related_eids": ["支撑该假设的证据编号, 只用上文证据摘要中出现过的 eid/vid"]}]
围栏外不要再输出任何解释。"""

_RETRY_NUDGE = "\n\n(注意: 上次调用失败或输出为空, 请严格按输出约定重新生成本章正文。)"


# ---------- 股票/目录解析 ----------

def _shard_info(d: Path) -> Optional[Tuple[str, str]]:
    """读证据目录首个合法分片 → (ts_code, 名称); 无分片返回 None。"""
    for p in sorted(d.glob("*.json")):
        try:
            shard = load_shard(p)
        except Exception:
            continue
        if not getattr(shard, "kind", ""):
            continue
        name = str(shard.meta.get("name") or shard.meta.get("stock")
                   or shard.meta.get("stock_name") or "")
        return (shard.ts_code or d.name), name
    return None


def resolve_stock(stock: str, root: Path,
                  rdir: Optional[Path] = None) -> Tuple[str, str, Path]:
    """把「股票名或代码」解析为 (ts_code, name, 证据目录)。

    代码 (600519.SH) 直接定位; 名称则在已有证据目录的分片 meta 中匹配
    (Stage 1 分片 meta 记录了股票名); 显式给了目录且目录非空则采信该目录,
    最后退到「唯一证据目录」; 均失败时提示直接传代码。
    """
    stock = (stock or "").strip()
    if not stock:
        raise ValueError("stock 不能为空")
    if _TS_CODE_RE.match(stock):
        ts = stock.upper()
        d = rdir or default_research_dir(root, ts)
        info = _shard_info(d) if d.is_dir() else None
        return ts, (info[1] if info and info[1] else ts), d

    base = root / "research"
    candidates = [rdir] if rdir is not None else (
        sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else [])
    infos = {d: _shard_info(d) for d in candidates if d.is_dir()}
    for d, info in infos.items():
        if info and info[1] == stock:
            return info[0], info[1], d
    if rdir is not None:
        info = infos.get(rdir)
        if info:
            return info[0], info[1] or stock, rdir
    non_empty = [d for d, info in infos.items() if info]
    if rdir is None and len(non_empty) == 1:
        d = non_empty[0]
        return infos[d][0], infos[d][1] or stock, d
    raise ValueError(f"无法把 {stock!r} 解析为 ts_code: 无匹配证据目录, "
                     f"请直接传 6 位代码 (如 600519.SH) 或 --dir 指定证据目录")


# ---------- 证据路由与摘要 ----------

_VIEW_WANTS = {"views", "view", "reports", "report"}

# T05 EVIDENCE_CATEGORIES 闭集的匹配规则 — eid 前缀 (mainbiz ⊂ profile, 同时列出无害)
_WANT_PREFIX_RULES = {
    "profile": ("T:profile.",),
    "mainbiz": ("T:profile.mainbiz.",),
    "financials": ("T:tushare.",),
    "tushare": ("T:tushare.",),            # financials 的历史别名
    "annual": ("A:",),
}


def _select_for_want(want: str, facts: List[Fact],
                     views: List[View]) -> Tuple[List[Fact], List[View]]:
    """按证据类别路由 (T05 EVIDENCE_CATEGORIES 闭集 + 任务书别名)。

    views/reports → 全部观点; 其余按 eid 前缀匹配 (profile/mainbiz/financials/annual,
    规则取自 T05 落库模板的词汇表); 闭集外的 want 退化为 T: 域/指标 token 匹配。
    """
    w = want.strip().lower()
    if w in _VIEW_WANTS:
        return [], list(views)
    prefixes = _WANT_PREFIX_RULES.get(w)
    if prefixes:
        return [f for f in facts if f.eid.startswith(prefixes)], []
    return [f for f in facts
            if f.eid.startswith("T:") and w in f.eid[2:].split(".")], []


def select_chapter_evidence(wants: List[str], facts: List[Fact],
                            views: List[View]) -> Tuple[List[Fact], List[View], List[str]]:
    """汇总一章所需证据子集 (跨类别去重保序); 全部未命中时回退全量并记 flag。"""
    sel_f: List[Fact] = []
    sel_v: List[View] = []
    seen_e: set = set()
    seen_v: set = set()
    for w in wants or []:
        sf, sv = _select_for_want(w, facts, views)
        for f in sf:
            if f.eid not in seen_e:
                seen_e.add(f.eid)
                sel_f.append(f)
        for v in sv:
            if v.vid not in seen_v:
                seen_v.add(v.vid)
                sel_v.append(v)
    flags: List[str] = []
    if not sel_f and not sel_v and (facts or views):
        flags.append(f"evidence-route-fallback: wants={wants} 未命中任何证据, 已回退全量证据")
        return list(facts), list(views), flags
    return sel_f, sel_v, flags


def _period_year(period: str) -> Optional[int]:
    m = re.match(r"((?:19|20)\d{2})", (period or "").strip())
    return int(m.group(1)) if m else None


def _fact_year(f: Fact) -> Optional[int]:
    return _period_year(f.period)


def _view_year(v: View) -> Optional[int]:
    return _period_year(v.period) or _period_year(v.publish_date)


def build_digest(ch_no: int, facts: List[Fact], views: List[View],
                 char_cap: int = DIGEST_CHAR_CAP) -> Tuple[str, str, List[str]]:
    """证据子集 → (facts_digest, views_digest, flags), 逐行摘要, 新期间在前。

    行格式: 事实 `- [E:eid] content｜来源: source｜期间`; 观点 `- [V:vid] 机构(日期): statement`。
    条数上限 80: 两类并存时 60 事实 + 20 观点 (观点不被挤出), 单类独享 80;
    两摘要合计超 char_cap 时先裁事实摘要 (保观点完整), 记 flag。
    """
    flags: List[str] = []
    fs = sorted(facts, key=lambda f: -(_fact_year(f) or 0))       # 稳定排序: 同年保留证据包原序
    vs = sorted(views, key=lambda v: -(_view_year(v) or 0))
    fact_cap = FACT_ITEM_CAP if vs else TOTAL_ITEM_CAP
    view_cap = VIEW_ITEM_CAP if fs else TOTAL_ITEM_CAP
    if len(fs) > fact_cap:
        flags.append(f"digest-truncated: 第{ch_no}章 事实 {len(fs)} 条截断至 {fact_cap} (按期间新旧保留)")
        fs = fs[:fact_cap]
    if len(vs) > view_cap:
        flags.append(f"digest-truncated: 第{ch_no}章 观点 {len(vs)} 条截断至 {view_cap}")
        vs = vs[:view_cap]
    ftext = "\n".join(f"- [E:{f.eid}] {f.content}｜来源: {f.source}" +
                      (f"｜期间: {f.period}" if f.period else "") for f in fs)
    vtext = "\n".join(f"- [V:{v.vid}] {v.org}({v.publish_date}): {v.statement}" for v in vs)
    over = len(ftext) + len(vtext) - char_cap
    if over > 0:
        keep_f = len(ftext) - over
        if keep_f >= 0:
            cut = ftext.rfind("\n", 0, keep_f)
            ftext = ftext[: cut if cut > 0 else keep_f]
        else:                                  # 观点摘要自身超限, 罕见但兜底
            cut = vtext.rfind("\n", 0, char_cap)
            vtext = vtext[: cut if cut > 0 else char_cap]
            ftext = ""
        flags.append(f"digest-too-long: 第{ch_no}章 证据摘要超 {char_cap} 字符已截断")
    return ftext, vtext, flags


# ---------- 章节生成 ----------

def _generate_chapter_body(ch: Dict[str, Any], facts_digest: str,
                           views_digest: str, llm: Any) -> Tuple[str, str]:
    """单章 LLM 调用 (非流式): 成功返回 (body, ""); 两次尝试均失败返回 ("", 原因)。

    T05 的 chapter_prompt 已内嵌 STYLE_PROMPT 与假设章 JSON 指令;
    若模板日后缺失假设 JSON 要求, 追加兜底指令 (正常不触发)。
    """
    prompt = chapter_prompt(ch, facts_digest, views_digest)
    if int(ch["no"]) == HYPOTHESES_CHAPTER_NO and ("假设" not in prompt or "```json" not in prompt):
        prompt += _HYPOTHESES_INSTRUCTION
    last_err = ""
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            resp = (llm.invoke(prompt + ("" if attempt == 1 else _RETRY_NUDGE)) or "").strip()
            if resp:
                m = _FENCE_WRAP_RE.match(resp)          # 模型习惯性整段围栏, 剥掉
                return (m.group(1).strip() if m else resp), ""
            last_err = "空响应"
        except Exception as e:                           # 网络/限流/超时等, 记原因后重试
            last_err = f"{type(e).__name__}: {e}"
        logger.warning("第%s章<%s> 第 %s/%s 次尝试失败: %s",
                       ch["no"], ch["title"], attempt, RETRY_TIMES, last_err)
    return "", last_err


def _iter_json_array_candidates(text: str):
    """按 '[' 出现序产出括号配平的候选 JSON 数组串 (外层在前, 嵌套数组不误截)。"""
    for start, ch in enumerate(text):
        if ch != "[":
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    yield text[start:i + 1]
                    break


def _parse_hypotheses(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """从假设章输出中剥离 ```json 围栏的假设数组 → (正文, 假设 dict 列表)。

    宽容解析 (AGENTS 纪律: 剥围栏): 先取围栏块; 全失败再做裸数组括号配平
    (候选须解析为含非空 statement 的 dict 数组才算命中)。正文 = 剔除命中的
    JSON 块后的剩余文本。
    """
    items: List[Dict[str, Any]] = []
    for block in _JSON_ARRAY_FENCE_RE.findall(text):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            items += [d for d in data if isinstance(d, dict) and str(d.get("statement", "")).strip()]
    body = _JSON_ARRAY_FENCE_RE.sub("", text).strip()
    if not items:
        for cand in _iter_json_array_candidates(body):
            try:
                data = json.loads(cand)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, list):
                continue
            cand_items = [d for d in data
                          if isinstance(d, dict) and str(d.get("statement", "")).strip()]
            if cand_items:
                items = cand_items
                body = body.replace(cand, "", 1).strip()
                break
    return body, items


def _build_hypotheses(items: List[Dict[str, Any]], start_index: int = 0) -> List[Hypothesis]:
    """dict → Hypothesis (hid 从 start_index+1 起顺编 H1..; 指标/引用洗成非空 str 列表)。"""
    out: List[Hypothesis] = []
    for i, d in enumerate(items, start_index + 1):
        out.append(Hypothesis(
            hid=f"H{i}",
            statement=str(d.get("statement", "")).strip(),
            indicators=[str(x).strip() for x in d.get("indicators", []) if str(x).strip()],
            related_eids=[str(x).strip() for x in d.get("related_eids", []) if str(x).strip()],
        ))
    return out


def _dedup_flags(flags: List[str]) -> List[str]:
    seen: set = set()
    return [f for f in flags if not (f in seen or seen.add(f))]


# ---------- 主入口 ----------

def generate_draft(stock: str, years: int = 5,
                   research_dir: Optional[Path] = None,
                   llm: Any = None) -> Draft:
    """逐章生成十章草稿并落盘 draft.json, 返回 Draft。

    stock: 股票名或 ts_code; years: 证据回看年数 (窗口外证据剔除, 仅记日志);
    research_dir: 证据目录 (默认 output/research/<ts_code>);
    llm: LLM 客户端桩 (须有 .invoke(prompt)->str), 默认懒加载 get_agent_llm()。
    """
    root = Path(PathManager().output_dir)
    ts_code, name, rdir = resolve_stock(stock, root, research_dir)
    if not rdir.is_dir() or not list(rdir.glob("*.json")):
        raise FileNotFoundError(f"证据目录为空: {rdir} — 请先运行 Stage 1 (T02/T03/T04) 生成分片")
    facts, views = load_pack(rdir)
    logger.info("载入证据: %d facts / %d views (%s)", len(facts), len(views), rdir)

    now_year = datetime.now().year
    floor = now_year - years + 1
    facts = [f for f in facts if (_fact_year(f) or floor) >= floor]
    views = [v for v in views if (_view_year(v) or floor) >= floor]
    logger.info("回看 %d 年 (>= %d): 剩余 %d facts / %d views", years, floor, len(facts), len(views))
    if not facts and not views:
        raise ValueError(f"{rdir} 内无 {floor} 年后的证据, 拒绝生成空稿 (可调大 years)")

    if llm is None:
        from harness.llm import get_agent_llm     # 懒加载: 离线测试不初始化 OpenAI 客户端
        llm = get_agent_llm(timeout=300.0)        # 长文+思考生成, 默认 120s 偏紧

    flags: List[str] = []
    chapters: List[Chapter] = []
    hypotheses: List[Hypothesis] = []
    for ch in CHAPTERS:
        no = int(ch["no"])
        sel_f, sel_v, sel_flags = select_chapter_evidence(ch.get("wants", []), facts, views)
        flags += sel_flags
        facts_digest, views_digest, dflags = build_digest(no, sel_f, sel_v)
        flags += dflags
        body, err = _generate_chapter_body(ch, facts_digest, views_digest, llm)
        if err:
            flags.append(f"chapter-failed: 第{no}章<{ch['title']}> 跳过 ({err})")
            continue
        # 假设 JSON 解析对所有章兜底 (只有假设章 prompt 会要求, 其余章命中即属意外收获)
        body, hitems = _parse_hypotheses(body)
        if hitems:
            hypotheses += _build_hypotheses(hitems, start_index=len(hypotheses))
        used: List[str] = []
        for _, ref in extract_citations(body):
            if ref not in used:
                used.append(ref)
        chapters.append(Chapter(no=no, title=str(ch["title"]), body_md=body, used_eids=used))
        logger.info("第%s章<%s>完成: %d 字, 引用 %d 处", no, ch["title"], len(body), len(used))
    if not hypotheses:
        flags.append(f"hypotheses-missing: 第{HYPOTHESES_CHAPTER_NO}章(假设章) "
                     f"未解析出假设 JSON 数组")

    draft = Draft(ts_code=ts_code, name=name,
                  generated_at=datetime.now().isoformat(timespec="seconds"),
                  chapters=chapters, hypotheses=hypotheses, flags=[])
    flags += check_draft(draft, facts, views)
    flags += [f"contract-invalid: {e}" for e in validate_draft(draft, facts, views)]
    draft.flags = _dedup_flags(flags)
    save_draft(draft, rdir / "draft.json")
    logger.info("草稿完成: %d/%d 章, %d 假设, %d flags → %s",
                len(chapters), len(CHAPTERS), len(hypotheses), len(draft.flags),
                rdir / "draft.json")
    return draft


# ---------- CLI ----------

def _cli(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m skills.research.compose.generator",
        description="研究生产线 Stage 2: 证据包 → 十章草稿 draft.json (LLM 逐章生成)")
    ap.add_argument("stock", help="股票名 (如 贵州茅台) 或 ts_code (如 600519.SH)")
    ap.add_argument("--years", type=int, default=5, help="证据回看年数 (默认 5)")
    ap.add_argument("--dir", type=Path, default=None,
                    help="证据目录 (默认 output/research/<ts_code>)")
    args = ap.parse_args(argv)
    draft = generate_draft(args.stock, years=args.years, research_dir=args.dir)
    print(f"草稿已生成: {draft.ts_code} {draft.name} — "
          f"{len(draft.chapters)} 章, {len(draft.hypotheses)} 条假设, {len(draft.flags)} 条 flags")
    for f in draft.flags[:20]:
        print(" -", f)
    if len(draft.flags) > 20:
        print(f"  …其余 {len(draft.flags) - 20} 条见 draft.json")


if __name__ == "__main__":
    _cli()
