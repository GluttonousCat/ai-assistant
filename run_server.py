# -*- encoding: utf-8 -*-
"""独立服务启动入口 (供后台常驻)"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8208, log_level="info")
