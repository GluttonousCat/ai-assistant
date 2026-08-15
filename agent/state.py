#!/usr/bin/env python3
"""
Agent状态定义
"""
from typing import Dict, Any, Optional, List, Literal
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CookieStatus:
    """Cookie状态"""
    cookie: str
    is_valid: bool = True
    last_checked: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    error_count: int = 0


@dataclass
class CrawlTask:
    """爬取任务"""
    task_id: str
    task_type: Literal["latest", "historical", "incremental", "update", "files"]
    group_id: str
    params: Dict[str, Any] = field(default_factory=dict)
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    result: Optional[Dict] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


@dataclass
class AgentState:
    """Agent状态"""
    # 用户输入
    user_input: str = ""
    parsed_intent: Optional[Dict] = None

    # 财务分析结果 (Text-to-SQL)
    fin_result: Optional[Dict] = None

    # Cookie管理
    cookies: Dict[str, CookieStatus] = field(default_factory=dict)
    active_cookie: Optional[str] = None

    # 任务队列
    tasks: List[CrawlTask] = field(default_factory=list)
    current_task: Optional[CrawlTask] = None

    # 执行状态
    execution_log: List[str] = field(default_factory=list)
    should_stop: bool = False

    # 输出
    response: str = ""

    def log(self, message: str):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.execution_log.append(f"[{timestamp}] {message}")

    def get_active_cookie(self) -> Optional[str]:
        """获取有效Cookie"""
        if self.active_cookie and self.cookies.get(self.active_cookie):
            cookie_status = self.cookies[self.active_cookie]
            if cookie_status.is_valid:
                return cookie_status.cookie
        return None

    def invalidate_cookie(self, cookie_key: str):
        """标记Cookie失效"""
        if cookie_key in self.cookies:
            self.cookies[cookie_key].is_valid = False
            self.cookies[cookie_key].error_count += 1
