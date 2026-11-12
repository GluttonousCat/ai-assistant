# -*- encoding: utf-8 -*-
"""
Standalone heartbeat sentinel — WS-level health monitoring of the API service
(process-isolated from the monitored service: if it dies, the sentinel lives on).

Background: the API service once died silently for 17 days with nobody noticing
(the monitor page ran inside the API; when it died, the watchdog's voice died too).
Design:
- Independent process, periodically GETs /health; broadcasts an alert on any state
  change (up<->down);
- WebSocket feed: any client connecting to ws://127.0.0.1:8209/ws receives
    {type:"hello"}   current state, sent immediately on connect
    {type:"beat"}    heartbeat frame per check (ok/latency)
    {type:"alert"}   up<->down state flip
    {type:"restart"} record of an auto-restart action
- Auto-restart (disable-able): after fail-threshold consecutive failures, restart
  the API process; 10-minute cooldown; probes the port before restarting to avoid
  duplicate instances;
- Opening http://127.0.0.1:8209/ in a browser shows a minimal status page
  (inlined in this same file).

Start (detached):
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

    # ---- WeCom bot supervision: liveness via lock-file PID, auto-restart on death
    #      (three silent deaths observed in practice) ----
    def wecom_alive() -> bool:
        import ctypes
        import ctypes.wintypes as wt
        try:
            pid = int((ROOT / "output" / "wecom_bot.lock").read_text().strip())
        except (OSError, ValueError):
            return False
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True

    WECOM_CMD = [str(ROOT / ".venv" / "Scripts" / "python.exe"),
                 "-m", "jobs.wecom_bot"]
    state["wecom_down"] = 0

    async def wecom_guard() -> None:
        if wecom_alive():
            state["wecom_down"] = 0
            return
        state["wecom_down"] += 1
        if state["wecom_down"] < 3:      # Act only after 3 consecutive beats (avoid killing during startup gap)
            return
        if state["wecom_down"] > 3:      # Already restarted once; wait for it to write the lock (60s startup window)
            if state["wecom_down"] > 9:  # Still dead after 9 beats (~90s) → restart again
                state["wecom_down"] = 3
            return
        logger.error("[告警] wecom_bot 进程死亡, 自动拉起")
        await broadcast({"type": "alert", "event": "wecom_down", "ts": time.time()})
        (ROOT / "output" / "wecom_bot.lock").unlink(missing_ok=True)
        subprocess.Popen(  # noqa: S603 fixed command line
            WECOM_CMD, cwd=str(ROOT),
            stdout=open(ROOT / "logs" / "wecom_bot.log", "ab"),
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        state["wecom_down"] = 4

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
            if ok:   # Recovered: alert carries the downtime duration
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
            proc = subprocess.Popen(  # noqa: S603 fixed command line, no external input
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
        return {**state, "wecom_ok": wecom_alive()}

    @app.websocket("/ws")
    async def ws_ep(ws: WebSocket):
        await ws.accept()
        clients.add(ws)
        try:
            await ws.send_text(json.dumps({
                "type": "hello", "ok": state["ok"],
                "fails": state["fails"], "ts": time.time()}))
            while True:
                await ws.receive_text()      # Client ping/keepalive; content ignored
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(ws)

    @app.on_event("startup")   # noqa: deprecated-but-stable; no refactor just to silence the warning
    async def _loop():
        async def run():
            while True:
                try:
                    await tick()
                    await wecom_guard()
                except Exception as e:  # noqa: BLE001 the sentinel itself must never exit
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
