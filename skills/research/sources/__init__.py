# -*- encoding: utf-8 -*-
"""
skills.research.sources — Stage 1 fact-source modules

  tushare_source  T02 · Tushare/profile fact source -> facts_tushare.json
  annual_source   T03 · fine-grained annual-report evidence -> facts_annual_<year>.json (parallel task)
  report_source   T04 · research-report view extraction -> views_reports.json (parallel task)

This __init__ stays an empty shell (maintained by T02): each source is imported
independently so Wave 1 parallel tasks don't entangle each other.
"""
