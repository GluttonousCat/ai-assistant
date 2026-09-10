#!/usr/bin/env python3
"""
通用工具函数
"""
import re
from datetime import datetime, timedelta, timezone


def clean_cookie(cookie: str) -> str:
    """清理Cookie字符串"""
    if not cookie:
        return ""

    # 去除首尾空白和引号
    cookie = cookie.strip().strip('"\'')

    # 处理bytes类型
    if isinstance(cookie, bytes):
        cookie = cookie.decode('utf-8')

    # 去除前缀b'或b"
    if cookie.startswith("b'") and cookie.endswith("'"):
        cookie = cookie[2:-1]
    elif cookie.startswith('b"') and cookie.endswith('"'):
        cookie = cookie[2:-1]

    # 处理转义字符
    cookie = cookie.replace('\\n', '').replace('\\"', '"').replace("\\'", "'")

    # 确保分号后有空格
    cookie = '; '.join(part.strip() for part in cookie.split(';'))

    return cookie


def timestamp_to_iso(timestamp_ms: int) -> str:
    """毫秒时间戳转ISO格式"""
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone(timedelta(hours=8)))
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0800'


def iso_to_timestamp(iso_time: str) -> int:
    """ISO格式转毫秒时间戳"""
    dt = datetime.fromisoformat(iso_time.replace('+0800', '+08:00'))
    return int(dt.timestamp() * 1000)


def decrement_time(iso_time: str, milliseconds: int = 1) -> str:
    """ISO时间减去指定毫秒"""
    dt = datetime.fromisoformat(iso_time.replace('+0800', '+08:00'))
    dt = dt - timedelta(milliseconds=milliseconds)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0800'


def sanitize_filename(filename: str) -> str:
    """清理文件名，移除非法字符"""
    safe = "".join(c for c in filename if c.isalnum() or c in '._-（）()[]{} ')
    return safe or "unnamed"


def is_chinese_translated(name: str) -> bool:
    """
    判定是否知识星球的中文翻译版文件 (如 '中文版-高盛-xxx.pdf').
    平台策略: 只保留英文/原版研报, 中文版在下载/入库层过滤.
    匹配: 以「中文版」开头 (含 【中文版】/(中文版) 变体), 或分隔符包裹的 -中文版- / 中文版.pdf 尾缀.
    """
    n = (name or "").strip()
    if not n:
        return False
    if n.startswith(("中文版", "【中文版", "（中文版", "(中文版")):
        return True
    return bool(re.search(r"[-_－—·]中文版([-_.．]|$)", n))


# 常见投行/券商 英文名 -> 中文通用名 (机构字段与展示标题统一用中文;
# AI 从英文原版提取出的机构名经此归一)
ORG_CN_MAP = {
    "goldman sachs": "高盛", "gs": "高盛", "gse": "高盛",
    "morgan stanley": "摩根士丹利", "ms": "摩根士丹利",
    "j.p. morgan": "摩根大通", "j.p.morgan": "摩根大通", "jpmorgan": "摩根大通",
    "jpm": "摩根大通", "jp morgan": "摩根大通",
    "ubs": "瑞银", "bernstein": "伯恩斯坦", "sanford c. bernstein": "伯恩斯坦",
    "barclays": "巴克莱", "deutsche bank": "德意志银行", "db": "德意志银行",
    "bofa securities": "美银证券", "bank of america": "美银证券", "bofa": "美银证券",
    "merrill lynch": "美银证券", "ml": "美银证券",
    "nomura": "野村", "citi": "花旗", "citigroup": "花旗", "citi research": "花旗",
    "hsbc": "汇丰", "macquarie": "麦格理", "jefferies": "杰富瑞",
    "credit suisse": "瑞信", "cs": "瑞信",
    "bnp paribas": "法国巴黎银行", "societe generale": "法国兴业银行",
    "socgen": "法国兴业银行", "daiwa": "大和", "mizuho": "瑞穗", "mufg": "三菱日联",
    "oppenheimer": "奥本海默", "piper sandler": "派杰", "stifel": "斯蒂费尔",
    "wedbush": "韦德布什", "clsa": "里昂证券", "baird": "贝尔德",
    "wells fargo": "富国银行", "rbc": "加拿大皇家银行", "cibc": "加拿大帝国商业银行",
    "bloomberg intelligence": "彭博行业研究", "s&p global": "标普全球",
    "morningstar": "晨星", "redburn": "红伯恩", "arete": "阿雷特",
}


def normalize_org_name(name: str) -> str:
    """
    机构名归一: 英文名 -> 中文通用名 (大小写/多余空格不敏感).
    未命中映射的原文返回; 空返回空串.
    """
    n = (name or "").strip()
    if not n:
        return ""
    key = re.sub(r"\s+", " ", n).lower().rstrip(".")
    return ORG_CN_MAP.get(key, n)


def strip_zsxq_tags(text: str) -> str:
    """清理知识星球话题文本中的富文本标签
    例: <e type="web" href="https%3A%2F%2F...">百度网盘</e> → 百度网盘(链接)
    """
    if not text:
        return text

    # <e type="web" href="url">显示文本</e> → 显示文本(链接: url)
    def _replace_web(m):
        label = m.group(2) or ""
        url = m.group(1) or ""
        return f"{label}(link:{url})" if url else label

    text = re.sub(r'<e\s+type="web"\s+href="([^"]*)"[^>]*>(.*?)</e>', _replace_web, text, flags=re.S)

    # 移除其余所有标签
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()
