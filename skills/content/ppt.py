# -*- encoding: utf-8 -*-
"""
Profile -> pptgen bridge

Flow: profile digest -> LLM writes a "PPT task book" (per-slide planning, real
data) -> invoke ../pptgen in auto mode to produce the .pptx (separate venv;
LLM config injected via environment variables)

pptgen prerequisites: ../pptgen/.venv + LibreOffice; the LLM uses this
platform's .env OpenAI-compatible config (deepseek is text-only;
--no-vision for self-check degradation).
The task book is always written to disk (output/ppt/) — even if slide
generation fails, the manual two-step flow still works:
    ../pptgen/.venv/Scripts/python.exe -m pptgen.agent build 任务书.md --out xx.pptx
"""
from __future__ import annotations

import os
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Dict

from core.logger import get_logger
from skills.content.profile import build_company_profile, profile_digest
from skills.content.prompts import PPT_TASK_PROMPT

logger = get_logger(__name__)

# pptgen is a sibling project outside the repo (PycharmProjects/pptgen);
# this file sits under agent/content/, three levels up to the root
PPTGEN_DIR = Path(__file__).resolve().parents[3] / "pptgen"
PPT_ROOT = Path("output/ppt")


def _pptgen_env() -> Dict[str, str]:
    """This platform's LLM config -> pptgen environment variables"""
    from core.config import get_config
    cfg = get_config()
    env = os.environ.copy()
    if cfg.openai_base_url:
        env["PPTGEN_LLM_BASE_URL"] = cfg.openai_base_url
    if cfg.openai_api_key:
        env["PPTGEN_LLM_API_KEY"] = cfg.openai_api_key
    env["PPTGEN_LLM_MODEL"] = cfg.llm_model_for("agent")
    return env


def make_task_book(stock: str, years: int = 5) -> Dict[str, Any]:
    """Profile -> pptgen task book markdown (written to disk). The LLM only
    plans; the data comes from the profile."""
    from harness.llm import get_agent_llm
    profile = build_company_profile(stock, years)
    digest = profile_digest(profile)
    task = get_agent_llm().invoke(
        PPT_TASK_PROMPT.format(digest=digest))
    PPT_ROOT.mkdir(parents=True, exist_ok=True)
    path = PPT_ROOT / f"{date.today():%Y%m%d}_{profile.get('name', stock)}_任务书.md"
    path.write_text(task, encoding="utf-8")
    logger.info(f"PPT 任务书已生成: {path}")
    return {"task_book": str(path), "task": task,
            "name": profile.get("name", stock)}


def generate_profile_ppt(stock: str, out_path: str = "",
                         timeout_sec: int = 600) -> Dict[str, Any]:
    """Profile -> task book -> pptgen auto slide generation. Returns
    {pptx, task_book, ok, error}"""
    book = make_task_book(stock)
    py = PPTGEN_DIR / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        return {"ok": False, "task_book": book["task_book"], "pptx": None,
                "error": f"pptgen venv 不存在: {py} — 请先按 ../pptgen/README 初始化"}

    out = (Path(out_path) if out_path else
           PPT_ROOT / f"{date.today():%Y%m%d}_{book['name']}_画像.pptx")
    out.parent.mkdir(parents=True, exist_ok=True)
    # the pptgen subprocess cwd is its own project dir, so --out must be an
    # absolute path (a relative one would land under pptgen/)
    out = out.resolve()
    cmd = [str(py), "-m", "pptgen.agent", "auto", book["task"], "--no-vision",
           "--max-steps", "300", "--out", str(out)]
    logger.info(f"调用 pptgen 出片 (超时 {timeout_sec}s): {out.name}")
    try:
        r = subprocess.run(cmd, cwd=str(PPTGEN_DIR), env=_pptgen_env(),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout_sec)
        if r.returncode == 0 and out.exists():
            logger.info(f"PPT 已生成: {out} ({out.stat().st_size // 1024}KB)")
            return {"ok": True, "pptx": str(out),
                    "task_book": book["task_book"], "error": None}
        tail = (r.stderr or r.stdout or "")[-800:]
        logger.warning(f"pptgen 失败 (code={r.returncode}): {tail}")
        return {"ok": False, "task_book": book["task_book"], "pptx": None,
                "error": f"pptgen 退出码 {r.returncode}: {tail}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "task_book": book["task_book"], "pptx": None,
                "error": f"pptgen 超时 (> {timeout_sec}s); 任务书已存, 可人工两步出片"}
