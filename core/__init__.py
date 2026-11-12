"""
Infrastructure layer (foundation shared by engine and domains; the engine lives in top-level harness/)
- config:   unified configuration (config.yaml + .env)
- logger:   logging management (aggregated by top-level package, see core/logger.py)
- security: JWT + bcrypt authentication
- lifespan: FastAPI lifecycle (scheduler start/stop)
"""
from core.config import get_config, Config
from core.logger import get_logger

__all__ = ["get_config", "Config", "get_logger"]
