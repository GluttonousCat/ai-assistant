"""Bounded retrieval for cross-report questions in the interactive agent."""

import asyncio
import re

from core import metrics
from core.json_response import parse_json_response
from core.logger import get_logger
from harness.execution import execute_batch
from skills.report.answers import answer_from_sources, render_citations
from skills.report.document import Segment


logger = get_logger(__name__)
_PLAN = """为用户的跨篇研报综述准备一次目录检索。根据本轮问题及对话中明确的对象，仅返回 JSON：
{"topics":[{"keyword":"用户重点关注的概念","alternate":"同一主题更容易命中标题的另一个简短检索词"}],"days":14}。
将明确的重点排在第一，保留用户同时请求的其他范围。通常一至三个简短主题词就够，最多六个。
同义词或重点的细分品种可作备用词。用户指定时间范围时按其要求；“最近”默认近14天，不限时间用0。
材料将由程序统一查找和阅读。"""


def is_report_overview(text, domain):
    if domain not in ("report", None):
        return False
    if re.search(r"(?:这|那|该|本)[篇份]|单篇|(?:研报|报告)\s*(?:#\s*)?\d+|#\s*\d+|回测|查行情|仅列标题|只列标题", text):
        return False
    report = domain == "report" or re.search(r"研报|券商|卖方", text)
    if not report:
        return False
    return bool(re.search(
        r"综述|综览|(?:总结|梳理|汇总).{0,16}(?:研报|研究报告)|观点.{0,8}(?:有哪些|都有什么|汇总|整理|总结|分歧|对比)|"
        r"(?:哪些|总结|盘点|汇总|梳理|整理|近期|最近|多家|各家).{0,24}(?:观点|看法)|"
        r"(?:各家|多家|不同|几家)(?:机构|券商).{0,10}(?:怎么看|判断|分歧)", text))


def _plan(llm, messages):
    prompt = [*messages, {"role": "system", "content": _PLAN}]
    value = parse_json_response(llm.invoke(prompt))
    if not isinstance(value, dict) or not isinstance(value.get("topics"), list):
        raise ValueError("未生成有效的研报检索主题")
    terms = {}
    for topic in value["topics"]:
        if not isinstance(topic, dict):
            continue
        term = topic.get("keyword")
        if isinstance(term, str) and 0 < len(term.strip()) <= 64:
            alt = topic.get("alternate")
            terms.setdefault(term.strip(), alt.strip() if isinstance(alt, str) and len(alt) <= 64 else "")
    days = value.get("days", 14)
    if not 1 <= len(terms) <= 6 or type(days) is not int or not 0 <= days <= 36500:
        raise ValueError("研报检索计划缺少有效主题或时间范围")
    return terms, days


def _identities(row):
    return {(key, row[key]) for key in ("report_id", "file_id", "copy_key") if row.get(key) is not None}


def _select(pools, topics, seen, limit):
    selected = []
    orgs = set()
    # Each topic receives a seat before the focus topic gets an extra one.
    rotation = [*topics, topics[0]] if len(topics) > 1 else topics
    while len(selected) < limit:
        before = len(selected)
        for topic in rotation:
            candidates = [r for r in pools[topic] if not seen.intersection(_identities(r))]
            if not candidates:
                continue
            row = min(candidates, key=lambda r: (
                r["topic_rank"], r.get("material_type") != "original",
                r.get("org") in orgs, not r.get("lead_evidence"),
            ))
            seen.update(_identities(row))
            orgs.add(row.get("org"))
            selected.append(row)
            if len(selected) == limit:
                break
        if len(selected) == before:
            break
    return selected


async def _batch(calls, batch, holder, sse, results):
    for name, args in calls:
        label = "、".join(args["keywords"]) if name == "search_reports" else f"研报 #{args['report_id']}"
        yield sse("stage", {"stage": "tool", "batch": batch, "batch_size": len(calls),
                            "message": f"{name} · {label}"})
    results.extend(await execute_batch(calls))
    for (name, args), env in zip(calls, results, strict=True):
        data = env.get("data") or {}
        info = {"ok": env["ok"], "elapsed_ms": env.get("elapsed_ms", 0), "rows": data.get("count", 0)}
        holder["tool_events"].append({"name": name, "args": args, **info})
        metrics.record("tool", tool=name, session_id=holder.get("session_id"),
                       ok=info["ok"], ms=info["elapsed_ms"])
        yield sse("tool_call", {"tool": name, "args": args, "batch": batch, **info})


async def run_overview(llm, messages, text, holder, sse):
    holder["tool_events"] = []
    yield sse("stage", {"stage": "agent", "message": "研报综述 · 确定检索主题"})
    metrics.record("session_round", session_id=holder.get("session_id"), round_no=1)
    terms, days = await asyncio.to_thread(_plan, llm, messages)
    pools = {t: [] for t in terms}
    findings = {t: [] for t in terms}
    read_states = {t: [] for t in terms}
    missing = list(terms)
    seen = set()
    catalog = {}
    batch = searches = reads = 0
    model_calls = 1
    for attempt in range(2):
        lookup = {}
        for topic in missing:
            if attempt and any(not seen.intersection(_identities(r)) for r in pools[topic]):
                continue
            word = terms[topic] if attempt else topic
            if word:
                if word in catalog:
                    pools[topic].extend(catalog[word])
                else:
                    lookup.setdefault(word, []).append(topic)
        if lookup:
            batch += 1
            searches += 1
            args = {"keywords": list(lookup), "days": days, "limit": 12}
            result = []
            async for event in _batch([("search_reports", args)], batch, holder, sse, result):
                yield event
            if not result[0]["ok"]:
                if not attempt:
                    raise RuntimeError(result[0].get("error") or "研报目录查询失败")
                yield sse("stage", {"stage": "agent", "message": "补充目录查询失败，保留首批已经核对的资料"})
            else:
                for word, topics in lookup.items():
                    catalog[word] = [r for r in result[0]["data"]["reports"] if r["matched_keyword"] == word]
                    for topic in topics:
                        pools[topic].extend(catalog[word])
        picked = _select(pools, missing, seen, len(missing) if attempt else 6)
        if not picked:
            continue
        batch += 1
        reads += 1
        # One attachment may cover several requested topics. Read it once,
        # then let each verified claim carry the topic it actually answers.
        report_topics = {}
        for row in picked:
            keys = _identities(row)
            report_topics[row["report_id"]] = [
                t for t in missing if any(keys & _identities(candidate) for candidate in pools[t])]
        calls = [("read_report", {"report_id": rid, "focus": f"{'、'.join(topics)}。{text}"})
                 for rid, topics in report_topics.items()]
        results = []
        async for event in _batch(calls, batch, holder, sse, results):
            yield event
        materials = []
        title_quotes = []
        for rid, env in zip(report_topics, results, strict=True):
            data = env.get("data") or {}
            for topic in report_topics[rid]:
                read_states[topic].append(data.get("body_status") if env["ok"] else "error")
            if not env["ok"] or not data.get("passages"):
                continue
            meta = {k: data.get(k) for k in (
                "report_id", "title", "org", "publish_date", "material_type", "page_count", "pages_with_text")}
            materials.append(({**meta, "topics": report_topics[rid], "source_basis": "document"},
                              [Segment(**part) for part in data["passages"]]))
            if data.get("indexed_quote"):
                for topic in report_topics[rid]:
                    if topic.casefold() in (meta["title"] or "").casefold():
                        title_quotes.append({**meta, "topic": topic, "source": data["indexed_quote"],
                                             "statement": "", "attribution": "unknown", "stance": "unknown"})
                        break
        if materials:
            yield sse("stage", {"stage": "agent", "message": "核对研报原句、发言人和适用条件"})
            model_calls += 1
            metrics.record("session_round", session_id=holder.get("session_id"), round_no=model_calls)
            question = (f"{text}\n本批需核对的主题：{'、'.join(missing)}。只选与各篇标记主题直接相关的论据。"
                        "同机构的原件和二手解读涉及相同判断时，优先引用原件。")
            try:
                answer = await asyncio.to_thread(answer_from_sources, llm, materials, question, compare=True)
            except Exception:
                if not attempt or not any(findings.values()):
                    raise
                logger.exception("补读研报的引文核对失败，保留首批结果")
                yield sse("stage", {"stage": "agent", "message": "补读引文核对失败，已保留首批结果"})
                break
            for cite in answer["citations"]:
                findings[cite["topic"]].append(cite)
            for quote in title_quotes:
                entries = findings[quote["topic"]]
                if not any(c["report_id"] == quote["report_id"] for c in entries):
                    entries.append(quote)
        missing = [t for t in missing if not any(c["statement"] for c in findings[t])]
        if not missing:
            break

    sections = []
    for topic, citations in findings.items():
        if citations:
            body = render_citations(citations)
        elif not pools[topic]:
            body = "本轮目录检索未取得匹配材料。"
        elif read_states[topic] and all(s == "error" for s in read_states[topic]):
            body = "本轮未成功读取入选材料，正文情况待核实。"
        else:
            body = "所查材料中尚未找到可核对的观点。"
        partial = {c["report_id"]: f"#{c['report_id']} 已识别 {c['pages_with_text']}/{c['page_count']} 页"
                   for c in citations if c.get("page_count") and c.get("pages_with_text") != c["page_count"]}
        if partial:
            body += "\n\n原件覆盖范围：" + "，".join(partial.values()) + "。"
        sections.append(f"### {topic}\n\n{body}")
    holder["final_text"] = "\n\n".join(sections)
    yield sse("stage", {"stage": "report_coverage", "search_calls": searches, "read_batches": reads,
                        "model_calls": model_calls, "uncovered_topics": missing,
                        "message": f"研报综述完成 · {searches} 次检索，{reads} 批阅读"})
