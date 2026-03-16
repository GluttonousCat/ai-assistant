#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import logging
import threading
import time
import traceback
from pathlib import Path

import websockets

from .config import HTTP_PORT, WS_PORT, config
from .audio_client import AudioClient
from .http_server import create_http_server


# 配置日志
logger = logging.getLogger(__name__)

# 日志文件路径
_DEBUG_LOG_PATH = Path(__file__).resolve().parent.parent / 'logs' / 'websocket_debug.log'
try:
    _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


def _log(msg: str):
    """记录日志"""
    try:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        try:
            with open(_DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + "\n")
        except Exception:
            pass
    except Exception:
        pass


async def websocket_server(websocket, path):
    """本地 WebSocket 服务器（可选）"""
    try:
        async for message in websocket:
            await websocket.send("Echo")
    except:
        pass


async def run_server():
    """启动服务器"""
    # 1. 创建客户端
    client = AudioClient()
    client.loop = asyncio.get_running_loop()

    # 2. 加载提示音
    try:
        client._load_dong_sound()
    except Exception:
        pass

    _log(f"boot ws_url={getattr(config, 'ws_url', '')} api_url={getattr(config, 'api_url', '')}")

    # 3. 初始化音频
    try:
        await client.initialize_audio()
    except Exception as e:
        _log(f"initialize_audio failed: {type(e).__name__}:{e!r}")
        _log(traceback.format_exc())
        raise

    # 4. 预取预设音频
    try:
        client.prefetch_preset_audios()
    except Exception:
        pass

    try:
        client.warmup_preset_pcm_cache_sync(config.preset_warmup_sync_count)
    except Exception:
        pass

    # 5. 启动本地 WebSocket Server
    _log(f"Starting Local WS Server on {WS_PORT}")
    try:
        ws_server = await websockets.serve(websocket_server, "0.0.0.0", WS_PORT)
    except Exception as e:
        _log(f"websockets.serve failed: {type(e).__name__}:{e!r}")
        _log(traceback.format_exc())
        raise

    # 6. 启动 HTTP Server (线程)
    def run_http_server():
        try:
            http_server = create_http_server(client, HTTP_PORT)
            _log(f"HTTP server started on port {HTTP_PORT}")
            http_server.serve_forever()
        except Exception as e:
            _log(f"HTTP server failed: {type(e).__name__}:{e!r}")
            _log(traceback.format_exc())
            raise

    threading.Thread(target=run_http_server, daemon=True).start()

    # 7. 初始连接
    client.handing_message_task = asyncio.create_task(client.reconnect())

    # 8. 主循环
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        if client.input_stream:
            client.input_stream.stop()
        if client.output_stream:
            client.output_stream.stop()


def run():
    """入口函数"""
    asyncio.run(run_server())


if __name__ == "__main__":
    run()
