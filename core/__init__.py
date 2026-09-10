"""
基础设施层
- config:  统一配置 (config.yaml + .env)
- logger:  日志管理 (按顶层包聚合, 见 core/logger.py)
- llm:     模型路由工厂 (core/llm)
- lifespan: FastAPI 生命周期
"""
from core.config import get_config, Config
from core.logger import get_logger, LogManager

__all__ = ["get_config", "Config", "get_logger", "LogManager"]
