"""
工具注册表 (Tool Registry)

管理所有可供 Agent/Skill 调用的工具
每个工具包含: 名称、描述、函数引用、参数schema
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel


class ToolSpec(BaseModel):
    """工具规格说明"""
    name: str
    description: str
    func: Optional[Callable] = None
    parameters: Dict[str, Any] = {}
    returns: str = ""


class ToolRegistry:
    """工具注册表"""

    _instance: Optional["ToolRegistry"] = None
    _tools: Dict[str, ToolSpec]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools = {}
        return cls._instance

    def register(self, spec: ToolSpec):
        """注册工具"""
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        """获取工具"""
        return self._tools.get(name)

    def list_all(self) -> List[ToolSpec]:
        """列出所有工具"""
        return list(self._tools.values())

    def describe_all(self) -> str:
        """生成所有工具的描述文本 (供 LLM 使用)"""
        lines = []
        for spec in self._tools.values():
            params_str = ", ".join(
                f"{k}: {v.get('type', 'any')}" for k, v in spec.parameters.items()
            )
            lines.append(f"- {spec.name}({params_str}): {spec.description}")
        return "\n".join(lines)

    def call(self, name: str, **kwargs) -> Any:
        """调用工具"""
        spec = self.get(name)
        if not spec or not spec.func:
            raise ValueError(f"工具不存在: {name}")
        return spec.func(**kwargs)


def get_tool_registry() -> ToolRegistry:
    return ToolRegistry()


# ---------- 注册内置工具 ----------
def _register_builtin_tools():
    """注册内置工具"""
    registry = get_tool_registry()

    # Tushare 行情
    try:
        from tools.market.tushare_client import TushareClient

        def fetch_daily(trade_date: str):
            """获取指定日期的A股日线数据"""
            client = TushareClient()
            return client.fetch_daily(trade_date)

        def fetch_stock_daily(ts_code: str, start_date: str, end_date: str):
            """获取个股区间日线数据"""
            client = TushareClient()
            return client.fetch_stock_daily(ts_code, start_date, end_date)

        registry.register(ToolSpec(
            name="fetch_daily",
            description="获取指定日期的A股日线数据",
            func=fetch_daily,
            parameters={"trade_date": {"type": "string", "description": "交易日期 YYYYMMDD"}},
        ))

        registry.register(ToolSpec(
            name="fetch_stock_daily",
            description="获取个股区间日线数据",
            func=fetch_stock_daily,
            parameters={
                "ts_code": {"type": "string", "description": "股票代码 000001.SZ"},
                "start_date": {"type": "string", "description": "开始日期"},
                "end_date": {"type": "string", "description": "结束日期"},
            },
        ))
    except ImportError:
        pass

    # K线指标
    try:
        from tools.kline.indicators import (
            calculate_adx,
            calculate_rolling_poc,
            generate_wyckoff_signals_adx_vp,
        )

        registry.register(ToolSpec(
            name="calculate_adx",
            description="计算ADX趋势强度指标",
            func=calculate_adx,
            parameters={
                "df": {"type": "DataFrame", "description": "K线数据"},
                "period": {"type": "integer", "description": "周期"},
            },
        ))

        registry.register(ToolSpec(
            name="calculate_rolling_poc",
            description="计算滚动POC控制点",
            func=calculate_rolling_poc,
            parameters={
                "df": {"type": "DataFrame", "description": "K线数据"},
                "window": {"type": "integer", "description": "滚动窗口"},
            },
        ))
    except ImportError:
        pass

    # 知识星球爬虫
    try:
        from tools.zsxq.crawler import ZSXQCrawler

        def crawl_zsxq(group_id: str, mode: str = "latest", count: int = 20):
            """爬取知识星球话题"""
            # 需要cookie, 这里只提供接口定义
            pass

        registry.register(ToolSpec(
            name="crawl_zsxq",
            description="爬取知识星球话题数据",
            func=crawl_zsxq,
            parameters={
                "group_id": {"type": "string", "description": "群组ID"},
                "mode": {"type": "string", "description": "爬取模式 latest/historical/incremental"},
                "count": {"type": "integer", "description": "数量"},
            },
        ))
    except ImportError:
        pass


# 自动注册内置工具
_register_builtin_tools()
