# -*- encoding: utf-8 -*-
"""
WeCom (Enterprise WeChat) "smart bot" long-connection bridge — plugs the local
Agent Q&A into WeCom conversations.

Direction: this process dials out to WeCom's wss (no public IP/domain/callback URL
needed); WeCom pushes @bot messages down the long connection, the process calls
harness.loop.run_agent_stream in-process to produce the answer, and writes it back
frame by frame to the same WeCom message in "content-refresh" streaming style.

Protocol per the official WeCom @wecom/aibot-node-sdk (src/ws.ts / types/api.ts):
  auth       {"cmd":"aibot_subscribe","headers":{"req_id"},"body":{"bot_id","secret"}}
  heartbeat  {"cmd":"ping","headers":{"req_id"}}   every 30s
  message    {"cmd":"aibot_msg_callback","body":{"msgid","chattype","chatid?",
             "from":{"userid"},"msgtype","text":{"content"}}}
  events     {"cmd":"aibot_event_callback",...}    (enter_chat etc., ignored)
  streaming  {"cmd":"aibot_respond_msg","headers":{"req_id":<echo the received req_id>},
             "body":{"msgtype":"stream","stream":{"id","finish","content"}}}
  Constraints: content<=20480 bytes/frame; streaming at most 6 minutes (forced
  close on timeout); one connection per bot (new replaces old); <=3 concurrent
  interactions per user (this bridge serializes per session).

Two non-standard behaviors of the WeCom gateway (measured 2026-10-27; hence
aiohttp instead of the websockets library):
  (1) Its response frames carry the MASK bit (RFC6455 forbids masking server
      frames; websockets' strict parsing drops the connection with 1002)
  (2) It does not answer WebSocket-level pings, only the JSON heartbeat above
      (websockets' default keepalive times out at 50s)

Sessions: session_id="wecom-<chatid or userid>" — each WeCom group/direct chat has
      its own independent multi-turn memory; history lives in **this process's
      memory** (harness.session, TTL 30min), disjoint from web sessions.

Usage:
    .venv/Scripts/python.exe -m jobs.wecom_bot     # run in foreground; Ctrl+C to exit
    .env settings: WECOM_BOT_ID / WECOM_BOT_SECRET
    (from WeCom → smart bot → API config → use-long-connection page)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import get_config  # noqa: E402
from core.logger import get_logger  # noqa: E402

logger = get_logger(__name__)

WSS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_S = 30           # official heartbeat interval
DEAD_AFTER_S = 100         # no inbound frame for 100s -> connection considered dead (>3 heartbeat periods)
STREAM_DEADLINE_S = 330    # WeCom streaming caps at 6 minutes; close early with headroom
PUSH_INTERVAL_S = 1.5      # refresh throttle during the delta phase (streaming is full-refresh; no need for high frequency)
MAX_CONTENT_BYTES = 20000  # official per-frame limit 20480 bytes; leave headroom
RETRY_MAX_S = 30           # reconnect backoff cap


def _req_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def _acquire_singleton_lock() -> bool:
    """Single-instance guard: WeCom allows one connection per bot (new replaces
    old); two instances kick each other and break the stream (hit in practice
    2026-10-27)."""
    import ctypes

    lock = ROOT / "output" / "wecom_bot.lock"
    lock.parent.mkdir(exist_ok=True)
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, 0, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                k32.CloseHandle(h)
                print(f"已有实例在运行 (PID {pid}); 单bot单连接, 双实例互踢, 先停旧再启新")
                return False
        except Exception:
            pass   # PID dead / lock file corrupted → lock invalid, may take over
    lock.write_text(str(os.getpid()))
    return True


def _clip(text: str) -> str:
    """Clip to the per-frame byte limit, measured in utf-8 bytes."""
    if len(text.encode("utf-8")) <= MAX_CONTENT_BYTES:
        return text
    n = len(text)
    while n > 0 and len(text[:n].encode("utf-8")) > MAX_CONTENT_BYTES - 100:
        n = n * 9 // 10
    return text[:n] + "\n\n…(内容过长, 已截断)"


def _md_cells(row: str) -> List[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _wecom_md(text: str) -> str:
    """WeCom bubble Markdown has no table support (restricted subset): convert to
    "bold header + pipe rows"; two-column tables become key: value lines."""
    lines = text.splitlines()
    out: List[str] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            block = [_cells for _cells in (_md_cells(r) for r in lines[i:j])
                     if not all(re.fullmatch(r":?-{3,}:?", c) for c in _cells if c)]
            if block:
                header, data = block[0], block[1:]
                if len(header) == 2:   # two-column table → key: value lines
                    out.extend(f"- **{h}**: {v}" for h, v in
                               ((r[0], r[1] if len(r) > 1 else "") for r in data))
                else:
                    out.append("**" + " | ".join(header) + "**")
                    out.extend("- " + " | ".join(r) for r in data[:12])
                    if len(data) > 12:
                        out.append(f"- …(其余 {len(data) - 12} 行略)")
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


class WecomBotBridge:
    def __init__(self, bot_id: str, secret: str) -> None:
        self.bot_id, self.secret = bot_id, secret
        self._send_lock = asyncio.Lock()               # frames sent serially (ordered replies)
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._seen_msgids: deque = deque(maxlen=256)    # dedupe WeCom message re-pushes
        self._last_recv = 0.0
        self._authed = False

    # ---------- Send primitives ----------
    async def _send(self, ws, frame: Dict[str, Any]) -> None:
        async with self._send_lock:
            await ws.send_str(json.dumps(frame, ensure_ascii=False))

    async def _push_stream(self, ws, req_id: str, stream_id: str,
                           content: str, finish: bool) -> None:
        await self._send(ws, {"cmd": "aibot_respond_msg",
                              "headers": {"req_id": req_id},
                              "body": {"msgtype": "stream",
                                       "stream": {"id": stream_id,
                                                  "finish": finish,
                                                  "content": _clip(_wecom_md(content))}}})

    # ---------- Message handling: incoming question → local Agent → streamed reply ----------
    async def _handle_message(self, ws, frame: Dict[str, Any]) -> None:
        body = frame.get("body") or {}
        req_id = (frame.get("headers") or {}).get("req_id", "")
        stream_id = _req_id("stream")
        msgid = str(body.get("msgid") or "")
        if msgid and msgid in self._seen_msgids:
            return
        if msgid:
            self._seen_msgids.append(msgid)

        msgtype = body.get("msgtype", "")
        if msgtype != "text":
            await self._push_stream(ws, req_id, stream_id,
                                    f"暂只支持文本提问 (收到 {msgtype})", True)
            return
        text = str((body.get("text") or {}).get("content") or "").strip()
        if body.get("chattype") == "group":
            text = re.sub(r"^@\S+\s*", "", text)   # group chats: strip the @bot prefix
        if not text:
            return

        userid = (body.get("from") or {}).get("userid", "?")
        session_id = f"wecom-{body.get('chatid') or userid}"
        logger.info("提问 [%s|%s] session=%s: %s",
                    userid, body.get("chattype"), session_id, text[:80])

        await self._push_stream(ws, req_id, stream_id, "收到, 正在思考…", False)

        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=3)
        except asyncio.TimeoutError:
            await self._push_stream(ws, req_id, stream_id,
                                    "上一问还在处理中, 请稍候再发。", True)
            return
        try:
            await self._answer(ws, req_id, stream_id, text, session_id)
        finally:
            lock.release()

    async def _answer(self, ws, req_id: str, stream_id: str,
                      text: str, session_id: str) -> None:
        """Run one Agent round: stage status pushed immediately, final answer pushed
        throttled, finish=True always sent at the end."""
        from harness.loop import run_agent_stream

        parts: List[str] = []
        status = "思考中…"
        last_push = time.monotonic()
        deadline = time.monotonic() + STREAM_DEADLINE_S

        def fmt(event: str, data: Any) -> str:
            return json.dumps([event, data], ensure_ascii=False, default=str)

        async def flush(partial: str) -> None:
            content = f"⏳ {status}\n\n{partial}" if partial else f"⏳ {status}"
            await self._push_stream(ws, req_id, stream_id, content, False)

        it = run_agent_stream(text, session_id, fmt).__aiter__()
        timed_out = False
        try:
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    timed_out = True
                    break
                try:
                    raw = await asyncio.wait_for(it.__anext__(), timeout=remain)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    timed_out = True
                    break
                event, data = json.loads(raw)
                if event == "delta":
                    parts.append(str(data.get("text") or ""))
                elif event == "stage":
                    status = str(data.get("message") or data.get("stage") or "处理中")
                    await flush("")
                elif event == "tool_call":
                    status = f"调用工具 {data.get('tool')}…"
                    await flush("")
                elif event == "error":
                    await self._push_stream(ws, req_id, stream_id,
                                            f"执行失败: {data.get('message')}", True)
                    return
                # data (tables) is not sent out; delta phase refreshes are
                # time-throttled
                if parts and time.monotonic() - last_push >= PUSH_INTERVAL_S:
                    await flush("".join(parts))
                    last_push = time.monotonic()
        finally:
            try:
                await it.aclose()
            except Exception:
                pass

        answer = "".join(parts).strip()
        if timed_out:
            answer = (answer or "处理超时, 请缩小问题范围后重试。") + \
                     "\n\n(已达企微 6 分钟流式上限, 强制收口)"
        await self._push_stream(ws, req_id, stream_id,
                                answer or "(本轮无输出, 详见 logs/jobs.log)", True)
        logger.info("回答完成 session=%s 长度=%d 超时=%s",
                    session_id, len(answer), timed_out)

    # ---------- Connection and heartbeat ----------
    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            await self._send(ws, {"cmd": "ping", "headers": {"req_id": _req_id("ping")}})
            if time.monotonic() - self._last_recv > DEAD_AFTER_S:
                raise ConnectionError(f"{DEAD_AFTER_S}s 未收到任何帧, 判定连接死亡")

    async def _serve(self, ws) -> None:
        self._last_recv = time.monotonic()
        self._authed = False
        hb = asyncio.create_task(self._heartbeat(ws))
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:   # CLOSE/CLOSING/ERROR → end iteration
                    logger.warning("非文本帧/关闭: %s", msg.type.name)
                    break
                self._last_recv = time.monotonic()
                frame = json.loads(msg.data)
                cmd = frame.get("cmd") or ""
                if not cmd:   # auth/heartbeat ack (errcode returned in-frame)
                    if frame.get("errcode"):
                        logger.warning("企微 ack 异常: errcode=%s %s",
                                       frame.get("errcode"), frame.get("errmsg"))
                    elif not self._authed:
                        self._authed = True
                        logger.info("订阅成功, 机器人已上线 (bot_id=%s…)", self.bot_id[:8])
                    continue
                if cmd == "aibot_msg_callback":
                    asyncio.create_task(self._safe_handle(ws, frame))
                elif cmd == "aibot_event_callback":
                    logger.info("事件回调: %s", str(frame.get("body"))[:120])
                else:
                    logger.debug("其他帧 cmd=%s", cmd)
        finally:
            hb.cancel()

    async def _safe_handle(self, ws, frame: Dict[str, Any]) -> None:
        try:
            await self._handle_message(ws, frame)
        except Exception as e:  # noqa: BLE001 one failed message must not kill the connection
            logger.error("消息处理失败: %s", e, exc_info=True)
            try:
                req_id = (frame.get("headers") or {}).get("req_id", "")
                await self._push_stream(ws, req_id, _req_id("stream"),
                                        f"内部错误: {e}", True)
            except Exception:
                pass

    async def run(self) -> None:
        backoff = 1
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(WSS_URL, max_msg_size=2**22,
                                                  autoping=True) as ws:
                        logger.info("已连接 %s (bot_id=%s…)", WSS_URL, self.bot_id[:8])
                        await self._send(ws, {"cmd": "aibot_subscribe",
                                              "headers": {"req_id": _req_id("sub")},
                                              "body": {"bot_id": self.bot_id,
                                                       "secret": self.secret}})
                        backoff = 1
                        await self._serve(ws)
                        logger.warning("连接结束, 准备重连")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 any network glitch -> backoff and reconnect
                logger.warning("连接异常: %s; %ss 后重连", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RETRY_MAX_S)


def main() -> None:
    if not _acquire_singleton_lock():
        sys.exit(1)
    get_config()  # load .env first (OPENAI/PG etc.)
    cfg = get_config()
    if not cfg.wecom_bot_id or not cfg.wecom_bot_secret:
        print("缺少配置: 请在项目根 .env 加入两行\n"
              "  WECOM_BOT_ID=...\n"
              "  WECOM_BOT_SECRET=...\n"
              "(企业微信 → 智能机器人 → API配置 → 使用长连接 页面获取)")
        sys.exit(1)
    print(f"企业微信机器人桥接启动 bot_id={cfg.wecom_bot_id[:8]}… "
          f"(Ctrl+C 退出; 日志: logs/jobs.log)")
    try:
        asyncio.run(WecomBotBridge(cfg.wecom_bot_id, cfg.wecom_bot_secret).run())
    except KeyboardInterrupt:
        print("\n桥接已停止")


if __name__ == "__main__":
    main()
