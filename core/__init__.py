"""
基础设施层 (引擎与领域之外的地基; 引擎在顶层 harness/)
- config:   统一配置 (config.yaml + .env)
- logger:   日志管理 (按顶层包聚合, 见 core/logger.py)
- security: JWT + bcrypt 认证
- lifespan: FastAPI 生命周期 (调度器启停)
"""
from core.config import get_config, Config
from core.logger import get_logger

__all__ = ["get_config", "Config", "get_logger"]
