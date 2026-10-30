# -*- encoding: utf-8 -*-
"""
独立心跳哨兵 — API 服务的WS级健康监控 (与被监控者进程隔离, 它死哨兵活)

背景: API 服务曾静默死亡 17 天无人发现 (监控页跑在 API 里, 哨兵死哨声也死)。
设计:
- 独立进程, 周期 GET /health; 状态变化 (up<->down) 即广播告警;
- WebSocket 出口: 任何客户端连 ws://127.0.0.1:8209/ws 即收
    {type:"hello"}   连接即给当前状态
    {type:"beat"}    每次检查的心跳帧 (ok/延迟)
    {type:"alert"}   up<->down 状态翻转
    {type:"restart"} 自动拉起动作的记录
- 自动拉起 (可关): 连续 fail-threshold 次失败后重启 API 进程, 冷却 10 分钟,
  拉起前探测端口避免重复实例;
- 浏览器打开 http://127.0.0.1:8209/ 即有一块最小状态页 (同文件内联)。

启动 (detached):
    .venv/Scripts/python.exe -m jobs.health_sentinel
CLI: --port 8209 --interval 10 --fail-threshold 3 --no-auto-restart
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Set

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from core.logger import get_logger

logger = get_logger(__name__)

ROOT = Path(__file__).resolve().parent.parent
API_URL = "http://127.0.0.1:8208/health"
RESTART_CMD = [str(ROOT / ".venv" / "Scripts" / "python.exe"), "run_server.py"]
RESTART_COOLDOWN_S = 600

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>哨兵</title>
<style>body{background:#0a0a0a;color:#ededed;font-family:monospace;padding:30px}
b{font-size:40px}.ok{color:#10b981}.bad{color:#f43f5e}p{color:#a3a3a3}</style></head>
<body><b id="s">…</b><p id="d">connecting…</p><script>
const ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws');
ws.onmessage=e=>{const m=JSON.parse(e.data);
 if(m.type==='beat'||m.type==='hello'){document.getElementById('s').textContent=m.ok?'● ALIVE':'● DOWN';
  document.getElementById('s').className=m.ok?'ok':'bad';
  document.getElementById('d').textContent=`api ${m.latency_ms??'-'}ms · ${new Date(m.ts*1000).toLocaleTimeString()}`;}
 if(m.type==='alert'){document.getElementById('d').textContent='ALERT: '+m.event;}};
</script></body></html>"""


def build_app(interval: float, fail_threshold: int, auto_restart: bool) -> FastAPI:
    app = FastAPI(title="health-sentinel", docs_url=None, redoc_url=None)
    clients: Set[WebSocket] = set()
    state: Dict = {"ok": None, "fails": 0, "t_down": None,
                   "last_restart": 0.0}

    async def broadcast(msg: Dict) -> None:
        dead = []
        for ws in clients:
            try:
                await ws.send_text(json.dumps(msg, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            clients.discard(ws)

    def port_listening() -> bool:
        import socket
        with socket.socket() as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", 8208)) == 0

    async def tick() -> None:
        t0 = time.time()
        try:
            r = await asyncio.to_thread(
                lambda: httpx.get(API_URL, timeout=5))
            ok, ms = r.status_code == 200, int((time.time() - t0) * 1000)
        except Exception:
            ok, ms = False, -1
        state["fails"] = 0 if ok else state["fails"] + 1
        prev = state["ok"]
        state["ok"] = ok
        await broadcast({"type": "beat", "ok": ok, "latency_ms": ms,
                         "ts": time.time()})
        if ok != prev and prev is not None:
            now = time.time()
            if ok:   # 恢复: 告警带宕机时长
                down_s = round(now - state["t_down"]) if state["t_down"] else None
                logger.warning(f"[告警] api_up (宕机 {down_s}s)")
                await broadcast({"type": "alert", "event": "api_up",
                                 "downtime_s": down_s, "ts": now})
                state["t_down"] = None
            else:
                state["t_down"] = now
                logger.warning("[告警] api_down")
                await broadcast({"type": "alert", "event": "api_down",
                                 "ts": now})
        if (not ok and auto_restart
                and state["fails"] >= fail_threshold
                and time.time() - state["last_restart"] > RESTART_COOLDOWN_S
                and not port_listening()):
            state["last_restart"] = time.time()
            logger.error(f"[自动拉起] API 连续 {state['fails']} 次失检, 执行重启")
            proc = subprocess.Popen(  # noqa: S603 固定命令行, 无外部输入
                RESTART_CMD, cwd=str(ROOT),
                stdout=open(ROOT / "logs" / "api-server.out.log", "ab"),
                stderr=open(ROOT / "logs" / "api-server.err.log", "ab"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            await broadcast({"type": "restart", "pid": proc.pid,
                             "ts": time.time()})

    @app.get("/")
    async def page():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(_PAGE)

    @app.get("/state")
    async def state_ep():
        return state

    @app.websocket("/ws")
    async def ws_ep(ws: WebSocket):
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_text(json.dumps({
                "type": "hello", "ok": state["ok"],
                "fails": state["fails"], "ts": time.time()}))
            while True:
                await ws.receive_text()      # 客户端 ping/保活, 内容忽略
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(ws)

    @app.on_event("startup")   # noqa: deprecated-but-stable; 不为去警告重构
    async def _loop():
        async def run():
            while True:
                try:
                    await tick()
                except Exception as e:  # noqa: BLE001 哨兵自身永不退出
                    logger.error(f"tick 异常: {e}")
                await asyncio.sleep(interval)
        asyncio.create_task(run())

    return app


if __name__ == "__main__":
    import uvicorn
    from core.config import get_config
    get_config()
    ap = argparse.ArgumentParser(description="API 独立心跳哨兵 (WS 出口)")
    ap.add_argument("--port", type=int, default=8209)
    ap.add_argument("--interval", type=float, default=10)
    ap.add_argument("--fail-threshold", type=int, default=3)
    ap.add_argument("--no-auto-restart", action="store_true")
    a = ap.parse_args()
    uvicorn.run(build_app(a.interval, a.fail_threshold,
                          not a.no_auto_restart),
                host="127.0.0.1", port=a.port, log_level="warning")
