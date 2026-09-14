# -*- encoding: utf-8 -*-
"""
Stdio MCP 端到端握手冒烟 (模拟外部 MCP 客户端: dsh / Claude Desktop / Cursor)

子进程拉起 `python -m core.mcp.server`, 按 MCP 规范走完整线级握手:
  initialize -> notifications/initialized -> tools/list -> tools/call
校验: 协议版本与服务名回显 / 只读清单不含写类工具 / 真实调用返回 ok 信封。
与 mcp/tests/test_server.py (进程内测 handle()) 互补, 验证的是真实 stdio 传输层
(UTF-8 编码 / 逐行 JSON-RPC / stdout 纯净性), 任何 MCP 客户端接入走的都是这条路。

用法 (项目根目录):
    .venv\\Scripts\\python.exe jobs/smoke_mcp_stdio.py
    .venv\\Scripts\\python.exe jobs/smoke_mcp_stdio.py --tool get_schema --args-json '{\"keyword\": \"估值\"}'
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _rpc(proc: subprocess.Popen, req: Dict[str, Any],
         timeout: float = 60.0) -> Dict[str, Any]:
    """发一条 JSON-RPC 请求并等对应 id 的响应 (notification 无回包, 单独发)"""
    import threading
    box: Dict[str, Any] = {}
    # 兜底超时: readline 阻塞读无法 interrupt, 用后台线程 + join(timeout)
    def _read():
        for line in proc.stdout:            # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                raise RuntimeError(f"stdout 出现非 JSON-RPC 行 (被污染): {line[:120]}")
            if resp.get("id") == req.get("id"):
                box["resp"] = resp
                return

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    t.join(timeout)
    if "resp" not in box:
        raise RuntimeError(f"请求无响应 (>{timeout}s): method={req.get('method')}")
    return box["resp"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", default="resolve_stock",
                        help="握手后试调的工具名 (默认 resolve_stock)")
    parser.add_argument("--args-json", default='{"text": "贵州茅台和宁德时代"}',
                        help="工具参数 JSON 串")
    args = parser.parse_args()

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "ALPHA_MCP_READ_ONLY": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "core.mcp.server"], cwd=str(ROOT),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, text=True, encoding="utf-8")
    print(f"[smoke] spawn: {sys.executable} -m mcp.server (cwd={ROOT})")
    try:
        # 1) initialize
        r = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05",
                                   "capabilities": {},
                                   "clientInfo": {"name": "smoke-stdio",
                                                  "version": "0"}}})
        info = r["result"]["serverInfo"]
        assert "alpha-finance-radar" in info["name"], info
        print(f"[smoke] initialize ok: {info['name']} v{info['version']} "
              f"(protocol {r['result']['protocolVersion']})")

        # 2) notifications/initialized (无回包, 不炸即通过)
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        # 3) tools/list — 只读闸门生效
        r = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in r["result"]["tools"]]
        assert "query_financials" in names, "核心工具缺失"
        assert "forge_chain" not in names, "写类工具泄漏进只读清单"
        assert all("description" in t for t in r["result"]["tools"])
        print(f"[smoke] tools/list ok: {len(names)} 只读工具 "
              f"(forge_chain 等写类已隐藏)")

        # 4) tools/call — 真实信封
        r = _rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": args.tool,
                                   "arguments": json.loads(args.args_json)}})
        result = r["result"]
        envelope = json.loads(result["content"][0]["text"])
        assert envelope.get("ok") is True, f"工具返回 ok=false: {envelope}"
        assert not result.get("isError")
        data_str = json.dumps(envelope.get("data"), ensure_ascii=False)
        print(f"[smoke] tools/call ok: {args.tool} -> "
              f"{data_str[:160]}{'…' if len(data_str) > 160 else ''}")

        print("[smoke] PASS — stdio 握手全链路可用, 外部 MCP 客户端可按此接入")
        return 0
    finally:
        proc.terminate()
        try:
            stderr_tail = proc.communicate(timeout=10)[1]
        except subprocess.TimeoutExpired:
            proc.kill()
            stderr_tail = ""
        tail = [l for l in (stderr_tail or "").splitlines() if l.strip()][-3:]
        for l in tail:
            print(f"[server-stderr] {l}")


if __name__ == "__main__":
    sys.exit(main())
