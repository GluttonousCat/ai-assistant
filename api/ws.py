#!/usr/bin/env python3
"""
Agent WebSocket 会话服务
挂载到主 app: app.include_router(ws_router)
"""
import asyncio
import json
from typing import Dict, Any
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.logger import get_logger
from agent.state import AgentState
from agent.graph import create_crawler_agent, invoke_agent

ws_router = APIRouter(tags=["agent-ws"])

# 存储会话状态
sessions: Dict[str, AgentState] = {}


class ConnectionManager:
    """WebSocket连接管理"""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, session_id: str):
        await websocket.accept()
        self.active_connections[session_id] = websocket
        # 初始化会话状态
        if session_id not in sessions:
            sessions[session_id] = AgentState()

    def disconnect(self, session_id: str):
        if session_id in self.active_connections:
            del self.active_connections[session_id]

    async def send_message(self, session_id: str, message: str):
        if session_id in self.active_connections:
            await self.active_connections[session_id].send_text(message)

    async def send_json(self, session_id: str, data: Dict):
        if session_id in self.active_connections:
            await self.active_connections[session_id].send_json(data)


manager = ConnectionManager()


@ws_router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """
    WebSocket接口
    实时交互式Agent
    """
    await manager.connect(websocket, session_id)
    state = sessions.get(session_id, AgentState())

    try:
        while True:
            # 接收用户输入
            data = await websocket.receive_text()
            message = json.loads(data)
            user_input = message.get("text", "")

            # 更新状态
            state.user_input = user_input
            state.log(f"用户输入: {user_input}")

            # 发送处理中状态
            await manager.send_json(session_id, {
                "type": "status",
                "message": "处理中..."
            })

            # 执行Agent
            agent = create_crawler_agent()
            result = await asyncio.to_thread(invoke_agent, agent, state)

            # 更新会话状态
            sessions[session_id] = result

            # 发送响应
            await manager.send_json(session_id, {
                "type": "response",
                "text": result.response,
                "log": result.execution_log[-10:] if result.execution_log else [],
                "timestamp": datetime.now().isoformat()
            })

    except WebSocketDisconnect:
        manager.disconnect(session_id)
        get_logger(__name__).info(f"ws 会话 {session_id} 断开")


@ws_router.post("/chat/{session_id}")
async def chat_endpoint(session_id: str, message: Dict[str, Any]):
    """
    HTTP API接口
    非实时交互
    """
    user_input = message.get("text", "")

    # 获取或创建状态
    if session_id not in sessions:
        sessions[session_id] = AgentState()

    state = sessions[session_id]
    state.user_input = user_input

    # 执行Agent
    agent = create_crawler_agent()
    result = invoke_agent(agent, state)

    # 更新状态
    sessions[session_id] = result

    return {
        "session_id": session_id,
        "response": result.response,
        "log": result.execution_log,
        "tasks": [
            {
                "id": t.task_id,
                "type": t.task_type,
                "status": t.status,
                "result": t.result
            }
            for t in result.tasks
        ]
    }


@ws_router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """获取会话状态"""
    if session_id not in sessions:
        return {"error": "会话不存在"}

    state = sessions[session_id]
    return {
        "session_id": session_id,
        "cookies_count": len(state.cookies),
        "tasks_count": len(state.tasks),
        "current_task": state.current_task.task_id if state.current_task else None,
        "log": state.execution_log
    }


@ws_router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    """停止当前任务"""
    if session_id in sessions:
        sessions[session_id].should_stop = True
        return {"message": "已发送停止信号"}
    return {"error": "会话不存在"}


@ws_router.delete("/sessions/{session_id}")
async def clear_session(session_id: str):
    """清除会话"""
    if session_id in sessions:
        del sessions[session_id]
        return {"message": "会话已清除"}
    return {"error": "会话不存在"}

