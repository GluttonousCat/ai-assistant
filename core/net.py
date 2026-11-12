"""
Direct network connection configuration
Force all requests to bypass the system proxy and hit the Tushare API directly
(an unstable proxy caused timeouts/slowness)

How it works: the requests library reads http_proxy/https_proxy/NO_PROXY env
     vars; setting NO_PROXY='*' makes every request go direct.
Usage: call before importing tushare, or from the main entrypoint.
"""
from __future__ import annotations

import os


def force_direct_connection() -> None:
    """Force direct connection bypassing proxies (takes effect for the global requests session)"""
    os.environ["http_proxy"] = ""
    os.environ["https_proxy"] = ""
    os.environ["HTTP_PROXY"] = ""
    os.environ["HTTPS_PROXY"] = ""
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    # Alternative: requests also supports session.trust_env=False, but env vars are the most universal


# Runs at import time (applies uniformly to sync_tushare / sync_financial etc.)
force_direct_connection()