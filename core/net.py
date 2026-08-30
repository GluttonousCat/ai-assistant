"""
网络直连配置
强制绕过系统代理直连 Tushare API (代理不稳定导致超时/慢)

原理: requests 库读取 http_proxy/https_proxy/NO_PROXY 环境变量,
     设置 NO_PROXY='*' 让所有请求直连.
使用: 在 import tushare 之前调用, 或在主入口调用.
"""
from __future__ import annotations

import os


def force_direct_connection() -> None:
    """强制绕过代理直连 (对全局 requests 会话生效)"""
    os.environ["http_proxy"] = ""
    os.environ["https_proxy"] = ""
    os.environ["HTTP_PROXY"] = ""
    os.environ["HTTPS_PROXY"] = ""
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    # 备选: requests 也支持 session.trust_env=False, 但环境变量最通用


# 模块导入即生效 (对 sync_tushare / sync_financial 等统一生效)
force_direct_connection()