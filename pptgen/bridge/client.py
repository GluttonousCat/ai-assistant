# -*- coding: utf-8 -*-
"""WorkerClient — 从系统 Python 拉起 uno_worker (LibreOffice 自带 Python) 并按协议通信。

被 server.py (MCP) / cli.py / tests / examples 共用。
"""
import json
import os
import queue
import subprocess
import threading

try:
    from .protocol import find_lo_program
except ImportError:  # 直接以文件方式运行时
    from protocol import find_lo_program

STARTUP_TIMEOUT = 330   # 含首次拉起 soffice 冷启动 (新 profile 完整初始化可达数分钟)
OP_TIMEOUT = 60


class PptgenError(RuntimeError):
    pass


class WorkerClient(object):
    def __init__(self, op_timeout=OP_TIMEOUT):
        self.op_timeout = op_timeout
        self._proc = None
        self._reader = None
        self._queue = None
        self._id = 0
        self._lock = threading.Lock()

    # ---- 生命周期 ----

    def ensure(self):
        """拉起 worker 并等待就绪 (线程安全, 可重入)。"""
        with self._lock:
            self._ensure_locked()

    def _ensure_locked(self):
        """调用方必须已持有 self._lock (内部不重复加锁, 防自锁)。"""
        if self._proc is not None and self._proc.poll() is None:
            return
        prog, _soffice, lo_python = find_lo_program()
        if not lo_python:
            raise PptgenError("未找到 LibreOffice 自带 Python (可用 env PPTGEN_LO_PROGRAM 指定)")
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uno_worker.py")
        # 独立进程组: MCP server 重启不连坐; CREATE_NO_WINDOW 防止弹控制台
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [lo_python, worker, "--lo-program", prog],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=creationflags)
        self._queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # 等 worker 就绪 (直接走无锁请求)
        self._request_locked("status", STARTUP_TIMEOUT, {})

    def _read_loop(self):
        try:
            for raw in iter(self._proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    self._queue.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            self._queue.put({"__eof__": True})

    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    def shutdown(self):
        if self.alive():
            try:
                self.request("shutdown", timeout=15)
            except Exception:  # noqa: BLE001
                pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    # ---- 请求 ----

    def request(self, op, timeout=None, **args):
        with self._lock:
            self._ensure_locked()
            return self._request_locked(op, timeout, args)

    def _request_locked(self, op, timeout, args):
        """调用方必须已持有 self._lock。"""
        self._id += 1
        rid = self._id
        req = {"id": rid, "op": op, "args": args}
        self._proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        self._proc.stdin.flush()
        deadline_timeout = timeout or self.op_timeout
        try:
            resp = self._queue.get(timeout=deadline_timeout)
        except queue.Empty:
            self._kill()
            raise PptgenError("op %r 超时 (%ss), worker 已被重置" % (op, deadline_timeout))
        if resp.get("__eof__"):
            self._kill()
            stderr_tail = self._stderr_tail()
            raise PptgenError("worker 已退出: %s" % stderr_tail)
        if resp.get("id") != rid:
            raise PptgenError("响应 id 错位: %r != %r" % (resp.get("id"), rid))
        if not resp.get("ok"):
            tb = (resp.get("traceback") or "").strip().splitlines()
            tail = "\n".join(tb[-4:]) if tb else ""
            raise PptgenError((resp.get("error") or "unknown error") + ("\n" + tail if tail else ""))
        return resp.get("result")

    def _kill(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _stderr_tail(self):
        try:
            if self._proc is not None and self._proc.stderr is not None:
                data = self._proc.stderr.read(4000) if self._proc.poll() is not None else b""
                return data.decode("utf-8", "replace")[-2000:]
        except Exception:  # noqa: BLE001
            pass
        return "(无 stderr)"

    # ---- 便捷封装 (与 MCP 工具一一对应) ----

    def status(self):
        return self.request("status")

    def new_deck(self, width_mm=None, height_mm=None):
        kw = {}
        if width_mm:
            kw["width_mm"] = width_mm
        if height_mm:
            kw["height_mm"] = height_mm
        return self.request("new_deck", **kw)

    def open_deck(self, path):
        return self.request("open_deck", path=path)

    def save(self, path, fmt="pptx"):
        return self.request("save", path=path, format=fmt)

    def close(self):
        return self.request("close")

    def add_slide(self):
        return self.request("add_slide")

    def list_slides(self):
        return self.request("list_slides")

    def add_textbox(self, slide, text, x=None, y=None, w=None, h=None, font=None,
                    font_size=None, bold=None, color=None, align=None, name=None):
        return self.request("add_textbox", slide=slide, text=text, x=x, y=y, w=w, h=h,
                            font=font, font_size=font_size, bold=bold, color=color,
                            align=align, name=name)

    def set_text(self, slide, shape, text, font=None, font_size=None, bold=None,
                 color=None, align=None):
        return self.request("set_text", slide=slide, shape=shape, text=text, font=font,
                            font_size=font_size, bold=bold, color=color, align=align)

    def add_shape(self, slide, kind="rectangle", x=None, y=None, w=None, h=None,
                  fill=None, line_color=None, line_width=None, text=None, font=None,
                  font_size=None, bold=None, color=None, align=None, name=None):
        return self.request("add_shape", slide=slide, kind=kind, x=x, y=y, w=w, h=h,
                            fill=fill, line_color=line_color, line_width=line_width,
                            text=text, font=font, font_size=font_size, bold=bold,
                            color=color, align=align, name=name)

    def add_image(self, slide, path, x=None, y=None, w=None, h=None, name=None):
        return self.request("add_image", slide=slide, path=path, x=x, y=y, w=w, h=h, name=name)

    def add_table(self, slide, rows, x=None, y=None, w=None, h=None, font=None,
                  font_size=None, header_bold=True, header_fill=None, border=True,
                  border_color=None, name=None):
        return self.request("add_table", slide=slide, rows=rows, x=x, y=y, w=w, h=h,
                            font=font, font_size=font_size, header_bold=header_bold,
                            header_fill=header_fill, border=border,
                            border_color=border_color, name=name)

    def render_slide(self, slide, width_px=1280):
        return self.request("render_slide", slide=slide, width_px=width_px)

    def set_transition(self, slide, effect, duration=None):
        return self.request("set_transition", slide=slide, effect=effect, duration=duration)

    def add_animation(self, slide, shape, effect, trigger="onclick", duration=None, delay=0.0):
        kw = {"slide": slide, "shape": shape, "effect": effect, "trigger": trigger,
              "delay": delay}
        if duration is not None:
            kw["duration"] = duration
        return self.request("add_animation", **kw)

    def list_animations(self, slide):
        return self.request("list_animations", slide=slide)

    def remove_animation(self, slide, shape):
        return self.request("remove_animation", slide=slide, shape=shape)

    def probe(self):
        return self.request("probe", timeout=90)
