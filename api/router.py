# -*- encoding: utf-8 -*-
"""
@date:2026/03/24
@file: router.py
@author: GluttonousCat
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/", tags=["router"])

@router.get("/")
def get_root():
    pass

@router.get("/wkf")
def get_wkf():
    pass

__all__ = ["router"]
