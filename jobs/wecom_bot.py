# -*- encoding: utf-8 -*-
"""
企业微信「智能机器人」长连接桥接 — 把本地 Agent 问答接入企微会话

方向: 本进程主动外拨企微 wss (无需公网 IP/域名/回调URL), 企微把 @机器人 的消息
从长连接推下来, 进程内直调 harness.loop.run_agent_stream 生成回答, 以「内容
刷新式」流式回复逐帧写回同一条企微消息。

协议依据: 企微官方 @wecom/aibot-node-sdk (src/ws.ts / types/api.ts):
  认证帧   {"cmd":"aibot_subscribe","headers":{"req_id"},"body":{"bot_id","secret"}}
  心跳帧   {"cmd":"ping","headers":{"req_id"}}   每30s
  收消息   {"cmd":"aibot_msg_callback","body":{"msgid","chattype","chatid?",
            "from":{"userid"},"msgtype","text":{"content"}}}
  事件     {"cmd":"aibot_event_callback",...}    (enter_chat 等, 忽略)
  流式回复 {"cmd":"aibot_respond_msg","headers":{"req_id":<透传收到的req_id>},
            "body":{"msgtype":"stream","stream":{"id","finish","content"}}}
  约束: content≤20480字节/帧; 流式最长6分钟(超时强制收口); 单bot单连接(新顶旧);
        同一用户并发交互≤3条(本桥按会话串行化)

企微网关两处非常规行为 (2026-10-27 实测, 故用 aiohttp 而非 websockets 库):
  ① 其应答帧带 MASK 位 (RFC6455 禁止服务器帧掩码; websockets 严格解析即 1002 断连)
  ② 不响应 WebSocket 协议层 ping, 只认上述 JSON 心跳 (websockets 默认保活 50s 超时)

会话: session_id="wecom-<chatid或userid>" — 每个企微群/私聊各自独立多轮记忆;
      历史在**本进程内存**(harness.session, TTL 30min), 与 web 端会话互不相通。

用法:
    .venv/Scripts/python.exe -m jobs.wecom_bot     # 前台常驻, Ctrl+C 退出
    .env 配置: WECOM_BOT_ID / WECOM_BOT_SECRET
    (企业微信 → 智能机器人 → API配置 → 使用长连接 页面获取)
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
HEARTBEAT_S = 30           # 官方心跳间隔
DEAD_AFTER_S = 100         # 100s 无任何入帧判连接死亡(>3个心跳周期)
STREAM_DEADLINE_S = 330    # 企微流式上限6分钟, 留余量强制收口
PUSH_INTERVAL_S = 1.5      # delta 阶段刷新节流 (流式为全量刷新, 无需高频)
MAX_CONTENT_BYTES = 20000  # 官方单帧 20480 字节, 留余量
RETRY_MAX_S = 30           # 断线重连退避上限


def _req_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def _acquire_singleton_lock() -> bool:
    """单实例守卫: 企微单bot单连接(新顶旧), 双实例会互踢断流 (2026-10-27 实踩)"""
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
            pass   # PID 已死/文件损坏 → 锁失效, 可抢
    lock.write_text(str(os.getpid()))
    return True


def _clip(text: str) -> str:
    """按 utf-8 字节截到单帧上限内"""
    if len(text.encode("utf-8")) <= MAX_CONTENT_BYTES:
        return text
    n = len(text)
    while n > 0 and len(text[:n].encode("utf-8")) > MAX_CONTENT_BYTES - 100:
        n = n * 9 // 10
    return text[:n] + "\n\n…(内容过长, 已截断)"


def _md_cells(row: str) -> List[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _wecom_md(text: str) -> str:
    """企微气泡 Markdown 不支持表格(受限子集): 转"加粗表头+竖线行", 两列表转 key: value"""
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
                if len(header) == 2:   # 两列表 → key: value 行
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
        self._send_lock = asyncio.Lock()               # 帧串行发(回复有序)
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._seen_msgids: deque = deque(maxlen=256)    # 企微重推去重
        self._last_recv = 0.0
        self._authed = False

    # ---------- 发送原语 ----------
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

    # ---------- 消息处理: 收问题 → 本地 Agent → 流式回复 ----------
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
            text = re.sub(r"^@\S+\s*", "", text)   # 群聊剥 @机器人 前缀
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
        """跑一轮 Agent: 阶段状态即时刷, 最终回答节流刷, 结束必发 finish=True"""
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
                # data(表格)不外发; delta 阶段按时间节流刷新
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

    # ---------- 连接与心跳 ----------
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
                if msg.type != aiohttp.WSMsgType.TEXT:   # CLOSE/CLOSING/ERROR → 结束迭代
                    logger.warning("非文本帧/关闭: %s", msg.type.name)
                    break
                self._last_recv = time.monotonic()
                frame = json.loads(msg.data)
                cmd = frame.get("cmd") or ""
                if not cmd:   # 认证/心跳 ack (errcode 随帧返回)
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
        except Exception as e:  # noqa: BLE001 单条消息失败不拖垮连接
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
            except Exception as e:  # noqa: BLE001 网络抖动一律退避重连
                logger.warning("连接异常: %s; %ss 后重连", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RETRY_MAX_S)


def main() -> None:
    if not _acquire_singleton_lock():
        sys.exit(1)
    get_config()  # 先加载 .env (OPENAI/PG 等)
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
