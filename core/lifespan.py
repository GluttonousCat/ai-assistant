# -*- encoding: utf-8 -*-
"""
@date: 2026/03/23
@file: lifespan.py
@author: GluttonousCat
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    pass
