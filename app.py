# -*- encoding: utf-8 -*-
"""
fin-agent unified entrypoint
Run: uvicorn app:app --reload --port 8208
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from workflows.runtime.lifecycle import lifespan
from api.middleware import AuthMiddleware
from api import router
from api.finance import fin_router
from api.market_api import market_router
from api.quant_api import quant_router
from api.auth_api import auth_router
from api.reports_api import reports_router
from api.monitor_api import monitor_router

app = FastAPI(
    title="ai-assistant",
    description="智能财务助手 - 知识星球爬虫 + Tushare数据分析 + Agent ChatBI",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(AuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(fin_router)
app.include_router(market_router)
app.include_router(quant_router)
app.include_router(auth_router)
app.include_router(reports_router)
app.include_router(monitor_router)

# ---------- Frontend static hosting (platform SPA) ----------
_WEB_DIST = Path(__file__).parent / "web" / "dist"
if _WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=_WEB_DIST / "assets"), name="assets")

    @app.get("/login", name="login_page", include_in_schema=False)
    @app.get("/", include_in_schema=False)
    async def index():
        """Platform index (login + console, React SPA)"""
        return FileResponse(_WEB_DIST / "index.html")

if __name__ == "__main__":
    from run_server import main

    main(app)
