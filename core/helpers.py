#!/usr/bin/env python3
"""
Common utility functions
"""
import re
from datetime import datetime, timedelta


def clean_cookie(cookie: str) -> str:
    """Clean up a cookie string"""
    if not cookie:
        return ""

    # Strip surrounding whitespace and quotes
    cookie = cookie.strip().strip('"\'')

    # Handle bytes input
    if isinstance(cookie, bytes):
        cookie = cookie.decode('utf-8')

    # Strip a b' or b" prefix
    if cookie.startswith("b'") and cookie.endswith("'"):
        cookie = cookie[2:-1]
    elif cookie.startswith('b"') and cookie.endswith('"'):
        cookie = cookie[2:-1]

    # Unescape escaped characters
    cookie = cookie.replace('\\n', '').replace('\\"', '"').replace("\\'", "'")

    # Ensure a space after every semicolon
    cookie = '; '.join(part.strip() for part in cookie.split(';'))

    return cookie


def decrement_time(iso_time: str, milliseconds: int = 1) -> str:
    """Subtract the given milliseconds from an ISO time"""
    dt = datetime.fromisoformat(iso_time.replace('+0800', '+08:00'))
    dt = dt - timedelta(milliseconds=milliseconds)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + '+0800'


def sanitize_filename(filename: str) -> str:
    """Sanitize a filename, removing illegal characters"""
    safe = "".join(c for c in filename if c.isalnum() or c in '._-（）()[]{} ')
    return safe or "unnamed"


def is_chinese_translated(name: str) -> bool:
    """
    Detect a Chinese-translated zsxq file (e.g. '中文版-高盛-xxx.pdf').
    Platform policy: keep only English/original reports; Chinese translations
    are filtered at the download/ingest layer.
    Match: starts with 「中文版」 (including 【中文版】/(中文版) variants), or a
    delimiter-wrapped -中文版- / a 中文版.pdf suffix.
    """
    n = (name or "").strip()
    if not n:
        return False
    if n.startswith(("中文版", "【中文版", "（中文版", "(中文版")):
        return True
    return bool(re.search(r"[-_－—·]中文版([-_.．]|$)", n))


# Common investment banks/brokers: English name -> common Chinese name
# (org field and display titles use Chinese uniformly; org names extracted
#  by AI from English originals are normalized through this map)
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
    Normalize an org name: English name -> common Chinese name
    (case/extra-whitespace insensitive). Unmapped input is returned as-is;
    empty input returns an empty string.
    """
    n = (name or "").strip()
    if not n:
        return ""
    key = re.sub(r"\s+", " ", n).lower().rstrip(".")
    return ORG_CN_MAP.get(key, n)


def strip_zsxq_tags(text: str) -> str:
    """Strip rich-text tags from zsxq topic text
    Example: <e type="web" href="https%3A%2F%2F...">百度网盘</e> -> 百度网盘(link)
    """
    if not text:
        return text

    # <e type="web" href="url">label</e> -> label(link: url)
    def _replace_web(m):
        label = m.group(2) or ""
        url = m.group(1) or ""
        return f"{label}(link:{url})" if url else label

    text = re.sub(r'<e\s+type="web"\s+href="([^"]*)"[^>]*>(.*?)</e>', _replace_web, text, flags=re.S)

    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()
