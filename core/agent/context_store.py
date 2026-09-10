"""
会话上下文存储 (进程级内存缓存)

用途: 支持「营收跟净利是降速, 不对劲吧?」这类省略主语的追问 --
当前句提取不到股票实体时, 继承本会话上一轮的股票。

设计:
- session_id -> {stocks, updated_at}, TTL 过期惰性清理
- 只存股票槽位 (ts_code, name), 不存对话历史 (轻量, 不做真正的对话记忆)
"""
from __future__ import annotations

import time
import threading
from typing import Dict, List, Optional, Tuple

_LOCK = threading.Lock()
_STORE: Dict[str, Dict] = {}
TTL_SECONDS = 30 * 60  # 30 分钟无活动则丢弃


def get_session_stocks(session_id: Optional[str]) -> List[Tuple[str, str]]:
    """取该会话最近一轮的股票 [(ts_code, name), ...]"""
    if not session_id:
        return []
    with _LOCK:
        entry = _STORE.get(session_id)
        if not entry:
            return []
        if time.time() - entry["updated_at"] > TTL_SECONDS:
            _STORE.pop(session_id, None)
            return []
        return list(entry["stocks"])


def set_session_stocks(session_id: Optional[str],
                       stocks: List[Tuple[str, str]]) -> None:
    """写入该会话的股票槽位 (空列表也写入, 表示本轮无股票 -> 追问链断开)"""
    if not session_id:
        return
    with _LOCK:
        # 惰性清理过期会话
        now = time.time()
        for k in [k for k, v in _STORE.items() if now - v["updated_at"] > TTL_SECONDS]:
            _STORE.pop(k, None)
        _STORE[session_id] = {
            "stocks": [(c, n) for c, n in stocks][:3],
            "updated_at": now,
        }


def reset() -> None:
    with _LOCK:
        _STORE.clear()


# ============================================================
# 写回辅助: 从查询结果行 / 用户输入提取股票并更新会话
# ============================================================

def update_session_from_result(session_id: Optional[str], user_input: str,
                               data_rows) -> None:
    """从本轮查询结果提取股票写回会话上下文.
    本轮提取不到股票 (继承场景) 则保留原上下文不覆盖."""
    if not session_id:
        return
    stocks: List[Tuple[str, str]] = []
    try:
        from tools.finance.stock_kb import get_stock_kb
        kb = get_stock_kb()
        # 优先: 结果行里的 ts_code (财务 SQL 有该列)
        for row in (data_rows or [])[:5]:
            if isinstance(row, dict) and row.get("ts_code"):
                code = row["ts_code"]
                if code not in [c for c, _ in stocks]:
                    stocks.append((code, kb.get_name(code) or code))
        # 兜底: 从用户输入词典匹配 (行情 SQL 无 ts_code 列)
        if not stocks:
            m = kb.match_stock(user_input)
            if m:
                stocks = [(m[0], kb.get_name(m[0]) or m[1])]
    except Exception:
        return
    if stocks:
        set_session_stocks(session_id, stocks[:3])