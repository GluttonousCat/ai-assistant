# -*- encoding: utf-8 -*-
"""
ai-assistant 统一入口
运行: uvicorn app:app --reload --port 8208
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.lifespan import lifespan
from api.middleware import AuthMiddleware
from api import router
from api.ws import ws_router
from api.finance import fin_router
from api.range_api import range_router
from api.auth_api import auth_router
from api.reports_api import reports_router

app = FastAPI(
    title="ai-assistant",
    description="智能投研助手 - 知识星球爬虫 + Tushare数据分析 + Agent ChatBI",
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
app.include_router(ws_router)
app.include_router(fin_router)
app.include_router(range_router)
app.include_router(auth_router)
app.include_router(reports_router)

# ---------- 前端静态托管 (平台单页应用) ----------
_WEB_DIST = Path(__file__).parent / "web" / "dist"
if _WEB_DIST.exists():
    app.mount("/assets", StaticFiles(directory=_WEB_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def index():
        """平台首页 (登录 + 控制台, React 单页应用)"""
        return FileResponse(_WEB_DIST / "index.html")

    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse(_WEB_DIST / "index.html")

    @app.get("/range", include_in_schema=False)
    async def range_dashboard():
        """区间扫描看板 (同一单页应用)"""
        return FileResponse(_WEB_DIST / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8208)
