# -*- encoding: utf-8 -*-
"""
skills/research.sources — Stage 1 事实源模块

  tushare_source  T02 · Tushare/画像事实源 → facts_tushare.json
  annual_source   T03 · 年报细粒度证据 → facts_annual_<年份>.json (并行任务)
  report_source   T04 · 研报观点抽取 → views_reports.json (并行任务)

本 __init__ 保持空壳 (T02 维护): 各源独立 import, 避免 Wave 1 并行任务互相牵连。
"""
