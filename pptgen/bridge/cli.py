# -*- coding: utf-8 -*-
"""cli.py — 直连 worker 的调试/兜底入口 (系统 Python 运行)。

用法:
    python -m pptgen.mcp.cli <op> [--json '{"slide":0,...}']
    python -m pptgen.mcp.cli probe
    python -m pptgen.mcp.cli status
所有 op 与 MCP 工具同名, 参数见 uno_worker.py 的 OPS_TABLE。
"""
import argparse
import json
import sys

try:
    from .client import PptgenError, WorkerClient
except ImportError:  # 直接以文件方式运行时
    from client import PptgenError, WorkerClient  # noqa: F401


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("op")
    ap.add_argument("--json", default="{}", help="op 参数 JSON")
    ns = ap.parse_args()
    try:
        args = json.loads(ns.json) if ns.json else {}
    except ValueError as e:
        print("bad --json: %s" % e, file=sys.stderr)
        return 2
    c = WorkerClient()
    try:
        result = c.request(ns.op, timeout=120, **args)
    except PptgenError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    # 不主动 shutdown: soffice 常驻实例留给下一条命令/其他客户端复用
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
