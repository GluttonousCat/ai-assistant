# -*- coding: utf-8 -*-
"""pptgen MCP 内部协议与 LibreOffice 路径发现（系统 Python / LO Python 双端共用，纯 stdlib）。

进程结构:
    server.py (系统 venv Python, fastmcp)
        |-- Popen --> uno_worker.py (LibreOffice 自带 Python, uno/pyuno)
                        |-- Popen --> soffice.bin --headless (独立 profile, 命名管道桥)
    通信: client<->worker 走 stdin/stdout 各一行一个 JSON (UTF-8);
          worker<->soffice 走 UNO 命名管道 (pipe name=pptgen_pipe, 本机实测 socket 不通管道通)

请求:  {"id": <int>, "op": "<op名>", "args": {<op参数>}}
响应:  {"id": <int>, "ok": true,  "result": <任意JSON>}
       {"id": <int>, "ok": false, "error": "<消息>", "traceback": "<可选>"}
"""
import os
import shutil

# ---- op 名集合（worker 端 dispatch 表与此对齐）----
OPS = [
    "status",
    "probe",            # 探测枚举成员/页面属性名（开发与排障用）
    "new_deck",
    "open_deck",
    "save",
    "close",
    "add_slide",
    "list_slides",
    "add_textbox",
    "set_text",
    "add_shape",
    "add_image",
    "add_table",
    "render_slide",
    "set_transition",
    "add_animation",
    "list_animations",
    "remove_animation",
    "shutdown",
]

DEFAULT_UNO_PORT = 2002
DEFAULT_PAGE_W_MM = 338.7   # 13.333 in
DEFAULT_PAGE_H_MM = 190.5   # 7.5 in


def find_lo_program():
    """定位 LibreOffice program 目录。优先级: env PPTGEN_LO_PROGRAM > 常规安装路径 > PATH 上的 soffice。

    返回 (program_dir, soffice_exe, lo_python)；找不到返回 (None, None, None)。
    """
    env_dir = os.environ.get("PPTGEN_LO_PROGRAM")
    candidates = []
    if env_dir:
        candidates.append(env_dir)
    candidates.append(r"C:\Program Files\LibreOffice\program")
    candidates.append(r"C:\Program Files (x86)\LibreOffice\program")
    for d in candidates:
        python_exe = os.path.join(d, "python.exe")
        soffice = os.path.join(d, "soffice.exe")
        if os.path.isfile(python_exe) and os.path.isfile(soffice):
            return d, soffice, python_exe
    # 兜底: PATH 上的 soffice（Windows 上通常是 soffice.com / soffice.exe）
    for name in ("soffice.exe", "soffice.com"):
        found = shutil.which(name)
        if found:
            d = os.path.dirname(os.path.abspath(found))
            python_exe = os.path.join(d, "python.exe")
            soffice = os.path.join(d, "soffice.exe")
            if os.path.isfile(python_exe) and os.path.isfile(soffice):
                return d, soffice, python_exe
    return None, None, None


def profile_dir():
    """headless soffice 的独立用户 profile 目录（避免挂到用户开着的 GUI 实例）。"""
    base = os.environ.get("PPTGEN_PROFILE_DIR")
    if base:
        return base
    return os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "pptgen", "lo_profile")


def path_to_url(path):
    """Windows 绝对路径 -> file:/// URL（正斜杠）。"""
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file:///" + p.lstrip("/")
