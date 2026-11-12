# -*- encoding: utf-8 -*-
"""
API request/response models (Pydantic)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------- ZSXQ ----------
class CrawlRequest(BaseModel):
    group_id: str
    pages: int = Field(default=10, ge=1, le=100)
    per_page: int = Field(default=20, ge=1, le=100)


class FileDownloadRequest(BaseModel):
    group_id: str
    max_files: int = Field(default=1, ge=1, le=10)


class AccountCreateRequest(BaseModel):
    cookie: str
    name: Optional[str] = None


# ---------- Agent ----------
class AgentChatRequest(BaseModel):
    session_id: str = "default"
    text: str


class TaskInfo(BaseModel):
    id: str
    type: str
    status: str
    result: Optional[Dict[str, Any]] = None


class AgentChatResponse(BaseModel):
    session_id: str
    response: str
    log: List[str] = []
    tasks: List[TaskInfo] = []


# ---------- Common ----------
class HealthResponse(BaseModel):
    status: str
    time: datetime
