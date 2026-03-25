# -*- encoding: utf-8 -*-
"""
@author: GluttonousCat
@description:
"""
from fastapi import FastAPI

from core.lifespan import lifespan
from api import router

app = FastAPI(lifespan=lifespan)

app.include_router(router)

