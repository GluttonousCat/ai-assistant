# -*- encoding: utf-8 -*-
"""
研报 LLM Analysis (文件名元数据提取)

【设计】只把研报文件名给 LLM (deepseek-v4-flash), 一个 prompt 完成:
    title(清洗后标题) / org(机构) / target(标的) / industry(行业) / region(地区) / market(市场)
知识星球研报文件名是规范命名 (券商-标的-主题), 信息基本都在名字里,
不读正文, 每篇秒级、成本极低。file_name 保留原文件名做溯源。

表结构 (fin.report_meta):
    file_name  原文件名 (原样保留)
    title      清洗后标题 (去机构/中文版/译文等噪音, 只留内容主题)
    org_name   机构
    target     标的名 (仅具体公司/商品; 行业/宏观报告为空)
    industry   行业 (规范化)
    region     地区 (规范化)
    market     市场 (A股/美股/港股/宏观/商品/行业/其他)

调用入口:
    analyze_report_meta(report_id)   单篇
    analyze_pending(limit)           批量 (入库后由调度器自动调用)
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient

logger = get_logger(__name__)

# 模型 (config llm.models.analysis.model 可覆盖)
META_MODEL = "deepseek-v4-flash-0731"

META_EXTRACT_PROMPT = """你是研报文件名清洗与元数据提取引擎。处理下面的研报文件名, 严格只输出一个 JSON 对象 (无解释、无代码块):
{{"title": "…", "org": "…", "target": "…", "industry": "…", "region": "…", "market": "…"}}

## 字段规则

**title** — 清洗后的标题, 只保留内容主题:
1. 去掉开头的机构名及其分隔符: "伯恩斯坦-", "【高盛】", "中文版-高盛-", "摩根士丹利-" 等
2. 去掉尾部噪音: "-译文", "译文", ".pdf", ".mp3", ".txt" 等后缀
3. 保留标的名+主题内容本身 (如 "滴滴出行：国际业务亏损风险消退，但现金转化仍限制重估")
4. 不要改写、缩写或翻译内容, 只做删减

**org** — 机构: 券商/投行名 (高盛/摩根大通/摩根士丹利/伯恩斯坦/野村/德意志银行/美银证券/中信证券…); 文件名里没有给 null

**target** — 标的, 必须是"具体的公司名或商品名", 严格规则:
1. 仅当文件名明确指向某家公司 (通常带代码, 如 滴滴出行/沃尔玛（WMT.US）/奕瑞科技（688301）/蒙牛乳业（2319.HK）) 时给公司名 (纯名字, 不带括号代码)
2. 商品报告给商品名 (黄金/原油/铜…)
3. 行业综述/宏观/策略报告给 null —— 不要把行业词 (如"日本电子元件"、"消费速递") 当成标的!
4. 多个公司给第一个

**industry** — 行业, 从这个词表里选最贴近的 1 个 (小写):
半导体 / 电子元件 / 消费电子 / 互联网 / 软件 / 银行 / 保险 / 券商 / 地产 / 建筑 / 食品饮料 / 白酒 / 农业 / 医药 / 汽车 / 机械 / 电力设备 / 新能源 / 有色金属 / 贵金属 / 钢铁 / 煤炭 / 石油石化 / 化工 / 交运 / 航空 / 物流 / 零售 / 纺织服装 / 传媒 / 教育 / 通信 / 计算机 / 军工 / 环保 / 公用事业 / 综合
实在都不贴近才可自造 2-4 字行业词

**region** — 主要地区市场, 从这个词表里选 1 个:
中国大陆 / 中国香港 / 中国台湾 / 日本 / 韩国 / 美国 / 欧洲 / 东南亚 / 印度 / 澳大利亚 / 亚太 / 新兴市场 / 全球
(中国大陆的研报必须写 "中国大陆" 而非 "中国")

**market** — 按标的所属交易所选 1 个:
A股 (代码 6开头.SH / 0或3开头.SZ / 68开头.SH)
H股 (港股, 代码 XXXX.HK)
台股 (XXXX.TW / XXXX.TWO)
日股 (XXXX.T)
韩股 (XXXX.KS / XXXX.KQ)
美股 (XX.US / XXX.N / XXX.O)
欧洲股 (伦敦 .L / 法兰克福 .DE / 巴黎 .PA 等)
东南亚股 (吉隆坡 .KL / 新加坡 .SI / 雅加达 .JK)
商品 (黄金/原油/铜等大宗商品主题) / 宏观 (经济/利率/策略) / 其他 (行业综述等)
禁止填行业名 (如"行业/半导体") 或地区名 (如"亚太/欧洲") —— 那不是市场!

文件名: {filename}"""

# 新列 DDL (幂等; 独立连接立即提交, 避免事务内 DDL 锁表)
DDL_META_COLS = [
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS target VARCHAR(64)",
     "col", "target"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS industry VARCHAR(64)",
     "col", "industry"),
    ("ALTER TABLE fin.report_meta ADD COLUMN IF NOT EXISTS region VARCHAR(64)",
     "col", "region"),
    ("CREATE INDEX IF NOT EXISTS idx_report_meta_industry ON fin.report_meta (industry)",
     "idx", "idx_report_meta_industry"),
    ("CREATE INDEX IF NOT EXISTS idx_report_meta_region ON fin.report_meta (region)",
     "idx", "idx_report_meta_region"),
]

_MARKETS = {"A股", "H股", "台股", "日股", "韩股", "美股", "欧洲股", "东南亚股",
            "商品", "宏观", "其他"}
# 旧值归一化 (行业/地区不是市场)
_MARKET_REMAP = {"行业": "其他", "港股": "H股"}

_cols_ensured = False


def ensure_meta_columns() -> None:
    """新列 DDL: 独立连接立即提交, 缺才执行 (进程内只跑一次)"""
    global _cols_ensured
    if _cols_ensured:
        return
    import psycopg2
    cfg = get_config().pg_config
    conn = psycopg2.connect(
        host=cfg["host"], port=cfg["port"], user=cfg["user"],
        password=cfg["password"], dbname=cfg["database"], connect_timeout=10,
        options="-c search_path=stock,fin,public")
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='fin' AND table_name='report_meta'")
        cols = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='fin' AND tablename='report_meta'")
        idx = {r[0] for r in cur.fetchall()}
        for stmt, kind, key in DDL_META_COLS:
            exists = (key in idx) if kind == "idx" else (key in cols)
            if not exists:
                cur.execute(stmt)
        cur.close()
        _cols_ensured = True
    finally:
        conn.close()


def _get_llm():
    """qwen-flash 小模型客户端 (config llm.models.analysis.model 覆盖)"""
    from openai import OpenAI
    cfg = get_config()
    model = cfg.get("llm.models.analysis.model") or META_MODEL
    return OpenAI(api_key=cfg.openai_api_key, base_url=cfg.openai_base_url,
                  timeout=30.0), model


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").strip()
        if t.startswith("json"):
            t = t[4:]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def analyze_report_meta(report_id: int, pg: Optional[PgClient] = None,
                        force: bool = False) -> bool:
    """
    单篇: 用文件名提取机构/标的/行业/地区/市场 并入库。
    已提取过 (org+industry 有值) 且非 force -> 跳过。返回是否执行。
    """
    ensure_meta_columns()
    if not get_config().openai_api_key:
        return False

    def _run(pgc: PgClient) -> bool:
        row = pgc.fetch_one(
            "SELECT report_id, file_name, title, org_name, industry "
            "FROM fin.report_meta WHERE report_id=%s", (report_id,))
        if not row:
            return False
        if not force and row.get("org_name") and row.get("industry")                 and row.get("title") != (row.get("file_name") or ""):
            return False
        filename = (row.get("file_name") or row.get("title") or "").strip()
        if not filename:
            return False

        client, model = _get_llm()
        # deepseek-v4 为思考模型: 必须关思考, 否则思考吃光 token 且 content 为空
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user",
                       "content": META_EXTRACT_PROMPT.format(filename=filename)}],
            temperature=0.0,
            extra_body={"enable_thinking": False})
        data = _parse_json(resp.choices[0].message.content or "")
        if not data:
            logger.warning(f"#{report_id} LLM Analysis 解析失败: {filename[:40]}")
            return False

        org = (str(data.get("org") or "").strip())[:64] or None
        target = (str(data.get("target") or "").strip())[:64] or None
        industry = (str(data.get("industry") or "").strip())[:64] or None
        region = (str(data.get("region") or "").strip())[:64] or None
        market = str(data.get("market") or "").strip()
        market = _MARKET_REMAP.get(market, market)
        if market not in _MARKETS:
            market = None
        # 清洗后标题: 空则回退原文件名 (去后缀)
        title = (str(data.get("title") or "").strip())[:200]
        if not title:
            title = re.sub(r"\.(pdf|mp3|txt|docx?)$", "", filename, flags=re.I)[:200]

        pgc.execute(
            "UPDATE fin.report_meta SET title=%s, org_name=%s, target=%s, industry=%s, "
            "region=%s, market=COALESCE(%s, market) WHERE report_id=%s",
            (title, org, target, industry, region, market, report_id))
        return True

    if pg is not None:
        return _run(pg)
    with PgClient() as pgc:
        return _run(pgc)


def analyze_pending(limit: int = 50, force: bool = False,
                    offset: int = 0) -> int:
    """批量: 处理缺元数据的研报 (force=True 按 offset 全量重提取)"""
    ensure_meta_columns()
    with PgClient() as pg:
        if force:
            ids = [r["report_id"] for r in pg.fetch_all(
                "SELECT report_id FROM fin.report_meta ORDER BY report_id "
                "LIMIT %s OFFSET %s", (limit, offset))]
        else:
            ids = [r["report_id"] for r in pg.fetch_all(
                "SELECT report_id FROM fin.report_meta "
                "WHERE (org_name IS NULL OR org_name = '' OR industry IS NULL OR industry = '') "
                "ORDER BY report_id LIMIT %s", (limit,))]
    if not ids:
        return 0
    n = 0
    for rid in ids:
        try:
            if analyze_report_meta(rid, force=force):
                n += 1
        except Exception as e:
            logger.warning(f"#{rid} LLM Analysis 失败: {e}")
    return n
