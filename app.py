# -*- encoding: utf-8 -*-
"""
ai-assistant 统一入口
运行: uvicorn app:app --reload --port 8208
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.lifespan import lifespan
from api import router
from api.ws import ws_router
from api.finance import fin_router

app = FastAPI(
    title="ai-assistant",
    description="智能投研助手 - 知识星球爬虫 + Tushare数据分析 + Agent ChatBI",
    version="2.0.0",
    lifespan=lifespan,
)

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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8208)
