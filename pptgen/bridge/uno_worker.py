# -*- coding: utf-8 -*-
"""uno_worker.py — LibreOffice UNO 执行器。

运行环境: LibreOffice 自带 Python (pyuno 与 CPython 版本强绑定, 禁止换系统 Python 跑本文件)。
职责:
  1. 拉起/复用 headless soffice (独立 profile, UNO 命名管道桥, 见 ensure_soffice 注释)
  2. 维护文档/页面/形状/动画/导出等原子操作, 按 protocol.py 的 JSON-lines 协议响应
注意: 本文件 stdout 只允许输出协议 JSON, 日志一律走 stderr。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from .protocol import (DEFAULT_PAGE_H_MM, DEFAULT_PAGE_W_MM,
                           find_lo_program, path_to_url, profile_dir)
    from . import presets
except ImportError:  # 直接以文件方式运行时
    from protocol import (DEFAULT_PAGE_H_MM, DEFAULT_PAGE_W_MM,
                          find_lo_program, path_to_url, profile_dir)
    import presets

# ---------------- uno bootstrap ----------------
LO_PROGRAM = None  # 命令行 --lo-program 可覆盖


def _import_uno():
    global LO_PROGRAM, uno, PropertyValue, NamedValue, systemPathToFileUrl
    prog = LO_PROGRAM or os.path.dirname(os.path.abspath(sys.executable))
    if prog not in sys.path:
        sys.path.insert(0, prog)
    _log("importing uno from %s" % prog)
    import uno as _uno
    uno = _uno
    from com.sun.star.beans import PropertyValue as _PV
    PropertyValue = _PV
    from com.sun.star.beans import NamedValue as _NV
    NamedValue = _NV
    systemPathToFileUrl = _uno.systemPathToFileUrl
    _log("uno imported")


def _log(msg):
    try:
        sys.stderr.write("[uno_worker] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def pv(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def mm(v):
    return int(round(float(v) * 100))  # mm -> 1/100 mm


def hex_color(s):
    return int(str(s).lstrip("#"), 16)


# ---------------- soffice 生命周期 ----------------
# 本机实测 (2026-09-05, LO 25.2.2.2 / Win10):
#   1. 经 soffice.exe 启动器拉起时 --accept 声明的 socket/pipe 均不建立 → 必须直接跑 soffice.bin
#   2. 全新 profile 的 headless 首启会卡死 (registrymodifications.xcu 永不生成) →
#      用用户已初始化的 GUI profile 播种专用 profile
#   3. socket acceptor 在本机不监听, 命名管道正常 → 一律用 pipe 桥接
PIPE_NAME = "pptgen_pipe"


def pipe_url():
    return "uno:pipe,name=%s;urp;StarOffice.ComponentContext" % PIPE_NAME


def _probe_resolver():
    local = uno.getComponentContext()
    return local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local)


def _resolve_with_timeout(resolver, url, timeout):
    """resolve() 在 office 忙于初始化时会无限阻塞, 用看门狗线程包一层。

    返回 (ctx, error, blocked); blocked=True 表示该次调用超时未返回 (线程弃置)。
    """
    box = {}

    def run():
        try:
            box["ctx"] = resolver.resolve(url)
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("ctx"), box.get("err"), t.is_alive()


def _seed_profile(profile):
    """首启卡死的规避: 无 registrymodifications.xcu 时从用户 GUI profile 播种。"""
    marker = os.path.join(profile, "user", "registrymodifications.xcu")
    if os.path.exists(marker):
        return False
    src = os.path.join(os.environ.get("APPDATA", ""), "LibreOffice", "4", "user")
    if not os.path.exists(os.path.join(src, "registrymodifications.xcu")):
        return False  # 无源可播, 只能裸启 (若卡死会在连接超时处报错)
    dst = os.path.join(profile, "user")
    os.makedirs(dst, exist_ok=True)
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".lock", ".~lock.*", "cache",
                                                       "backup", "temp"))
        _log("profile seeded from GUI profile")
        return True
    except Exception as e:  # noqa: BLE001
        _log("profile seed failed: %r" % (e,))
        return False


def _soffice_procs():
    """挂在本专用 profile 上的 soffice.bin PID 列表 (排障/清场用)。"""
    marker = path_to_url(profile_dir())
    script = ("Get-CimInstance Win32_Process -Filter \"Name='soffice.bin'\" | "
              "Where-Object { $_.CommandLine -like '*%s*' } | "
              "Select-Object -ExpandProperty ProcessId" % marker)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=20)
        return [int(x) for x in r.stdout.split() if x.strip().isdigit()]
    except Exception as e:  # noqa: BLE001
        _log("soffice proc scan failed: %r" % (e,))
        return []


def _kill_procs(pids):
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        except Exception:  # noqa: BLE001
            pass


def _wait_soffice_gone(timeout, kill_after=None):
    """等待本 profile 的 soffice 全部退出; kill_after 秒后强杀。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        procs = _soffice_procs()
        if not procs:
            return True
        if kill_after is not None and time.time() - t0 > kill_after:
            _log("force killing lingering soffice: %r" % (procs,))
            _kill_procs(procs)
            kill_after = None  # 只强杀一轮
        time.sleep(1.0)
    return not _soffice_procs()


def _cleanup_soffice(grace=15):
    """优雅终止 + 限时强杀, 确保 profile 释放 (防下次冷启动锁竞争)。"""
    try:
        ST.desktop.terminate()
    except Exception:  # noqa: BLE001
        pass
    _wait_soffice_gone(grace + 15, kill_after=grace)


def ensure_soffice():
    """确保 headless soffice.bin 的管道桥可用, 返回 (ctx, spawned)。"""
    resolver = _probe_resolver()
    _log("probing existing pipe bridge")
    attach_deadline = time.time() + 120
    while time.time() < attach_deadline:
        ctx, _err, blocked = _resolve_with_timeout(resolver, pipe_url(), 10.0)
        if ctx is not None:
            _log("attached to existing soffice")
            return ctx, False  # 已有常驻实例, 直接复用
        if not blocked:
            break  # 快速失败 = 没有实例在跑 → 去拉起
        _log("pipe exists but office busy, waiting...")
    _log("no live bridge, spawning soffice.bin")
    # 清场: 同 profile 的旧实例(退出中/僵死)会造成锁竞争 → 冷启动卡死
    stale = _soffice_procs()
    if stale:
        _log("stale soffice with our profile: %r -> killing" % (stale,))
        _kill_procs(stale)
        _wait_soffice_gone(20)
    prog = LO_PROGRAM or os.path.dirname(os.path.abspath(sys.executable))
    soffice = os.path.join(prog, "soffice.bin")
    if not os.path.isfile(soffice):
        soffice = os.path.join(prog, "soffice.exe")
    if not os.path.isfile(soffice):
        raise RuntimeError("未找到 LibreOffice soffice.bin (可用 env PPTGEN_LO_PROGRAM 指定 program 目录)")
    profile = profile_dir()
    os.makedirs(profile, exist_ok=True)
    _seed_profile(profile)
    lock = os.path.join(profile, ".lock")
    if os.path.exists(lock):
        try:
            os.remove(lock)  # 上次强杀的残留锁
        except OSError:
            pass
    cmd = [
        soffice,
        "-env:UserInstallation=%s" % path_to_url(profile),  # 独立 profile, 防挂到 GUI 实例; 必须是第一个参数
        "--headless", "--norestore", "--nologo", "--nodefault",
        "--accept=pipe,name=%s;urp;" % PIPE_NAME,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(cmd, creationflags=creationflags,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 300  # 冷启动(新播种 profile 首次初始化)可能要几分钟
    last = None
    while time.time() < deadline:
        ctx, err, blocked = _resolve_with_timeout(resolver, pipe_url(), 15.0)
        if ctx is not None:
            return ctx, True
        if not blocked:
            last = err  # 快速失败 → 正常重试; blocked → office 正在初始化, 继续等
        time.sleep(1.0)
    raise RuntimeError("soffice 管道桥建立失败 (300s): %r (可能卡在首启, 检查 profile 播种)" % (last,))


class State(object):
    ctx = None
    smgr = None
    desktop = None
    doc = None
    render_dir = None
    spawned = False  # 本 worker 是否亲手拉起了 soffice (EOF 时只有亲爹才清理)


ST = State()


def connect():
    ST.ctx, ST.spawned = ensure_soffice()
    ST.smgr = ST.ctx.ServiceManager
    ST.desktop = ST.smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ST.ctx)
    _adopt_or_close()
    ST.render_dir = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")),
                                 "pptgen_renders")
    os.makedirs(ST.render_dir, exist_ok=True)


def _adopt_or_close():
    """新 worker 接入: 接管常驻 soffice 里已打开的 Impress 文档 (deck 状态活在 daemon),
    多余的关掉; 没有 impressions 则保持无文档状态。"""
    impress_docs = []
    try:
        comps = ST.desktop.Components.createEnumeration()
        while comps.hasMoreElements():
            d = comps.nextElement()
            try:
                if d.supportsService("com.sun.star.presentation.PresentationDocument"):
                    impress_docs.append(d)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    if impress_docs:
        ST.doc = impress_docs[-1]
        for d in impress_docs[:-1]:
            try:
                d.close(False)
            except Exception:  # noqa: BLE001
                pass


# ---------------- 文档/页面/形状 基础 ----------------

def _pages():
    if ST.doc is None:
        raise RuntimeError("没有打开的文档 (先 new_deck / open_deck)")
    return ST.doc.getDrawPages()


def _page(idx):
    pages = _pages()
    idx = int(idx)
    if idx < 0 or idx >= pages.Count:
        raise ValueError("页码越界: %d (共 %d 页)" % (idx, pages.Count))
    return pages.getByIndex(idx)


def _find_shape(page, ref):
    """按 Name 或 '#'索引 找形状。"""
    for i in range(page.Count):
        sh = page.getByIndex(i)
        name = ""
        try:
            name = str(sh.Name or "")
        except Exception:  # noqa: BLE001
            pass
        if ref == name or ref == "#%d" % i or ref == str(i):
            return sh
    raise ValueError("形状不存在: %r" % ref)


def _apply_shape_text_align(sh, align):
    """形状级水平对齐 (Draw 特性: 文本块锚定由 TextHorizontalAdjust 决定,
    段落 ParaAdjust 仅在 BLOCK 锚定下生效, 见 _style_text 修复说明)。"""
    if not align:
        return
    m = {"left": "LEFT", "center": "CENTER", "right": "RIGHT", "justify": "BLOCK"}
    try:
        sh.setPropertyValue(
            "TextHorizontalAdjust",
            uno.Enum("com.sun.star.drawing.TextHorizontalAdjust", m[align]))
    except Exception:  # noqa: BLE001
        pass


def _style_text(text_or_cursor, font, font_size, bold, color, align):
    """文本属性统一入口。

    历史坑 (2026-09-05 实测修复):
      1. 只设 CharHeight 时中文按 CharHeightAsian 默认 18pt 渲染 → 字号三系同设;
      2. ParaAdjust 设在 XText 根对象上不生效(段落级属性) → 逐段落设置, 失败再退回根对象;
      3. CharWeight 同理补 Asian/Complex 两系。
    """
    p = text_or_cursor
    if font:
        p.setPropertyValue("CharFontName", font)
        try:
            p.setPropertyValue("CharFontNameAsian", font)
        except Exception:  # noqa: BLE001
            pass
    if font_size:
        for prop in ("CharHeight", "CharHeightAsian", "CharHeightComplex"):
            try:
                p.setPropertyValue(prop, float(font_size))
            except Exception:  # noqa: BLE001
                pass
    if bold is not None:
        for prop in ("CharWeight", "CharWeightAsian", "CharWeightComplex"):
            try:
                p.setPropertyValue(prop, 200.0 if bold else 100.0)
            except Exception:  # noqa: BLE001
                pass
    if color:
        p.setPropertyValue("CharColor", hex_color(color))
    if align:
        val = {"left": 0, "right": 1, "center": 2, "justify": 3}[align]
        applied = False
        try:  # 段落级设置 (XText/Cell 枚举段落)
            enum = p.createEnumeration()
            while enum.hasMoreElements():
                enum.nextElement().setPropertyValue("ParaAdjust", val)
            applied = True
        except Exception:  # noqa: BLE001
            pass
        if not applied:  # 退回: 直接设根对象 (对 cursor/已选范围有效)
            try:
                p.setPropertyValue("ParaAdjust", val)
            except Exception:  # noqa: BLE001
                pass


def _set_pos_size(sh, x, y, w, h):
    if x is not None and y is not None:
        sh.setPosition(uno.createUnoStruct("com.sun.star.awt.Point", mm(x), mm(y)))
    if w is not None and h is not None:
        sh.setSize(uno.createUnoStruct("com.sun.star.awt.Size", mm(w), mm(h)))


def _new_shape_name(page, kind):
    n = page.Count + 1
    return "%s_%d_%d" % (kind, _page_index(page), n)


def _page_index(page):
    pages = _pages()
    for i in range(pages.Count):
        if pages.getByIndex(i) == page:
            return i
    return -1


# ---------------- 动画 (SMIL 节点树工具) ----------------

def _anim_root(page):
    try:
        return page.getAnimationNode()  # XAnimationNodeSupplier
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("页面不支持动画节点: %r" % (e,))


def _children(node):
    """XTimeContainer 子节点, 兼容多种访问方式。"""
    out = []
    try:
        count = node.getCount()
        for i in range(count):
            out.append(node.getByIndex(i))
        return out
    except Exception:  # noqa: BLE001
        pass
    try:
        en = node.createEnumeration()
        while en.hasMoreElements():
            out.append(en.nextElement())
    except Exception:  # noqa: BLE001
        pass
    return out


def _append_child(parent, child):
    parent.appendChild(child)


def _remove_child(parent, child):
    try:
        return parent.removeChild(child)
    except Exception:  # noqa: BLE001
        for i in range(parent.getCount()):
            if parent.getByIndex(i) == child:
                parent.removeByIndex(i)
                return True
    return False


def _userdata(node):
    """UserData ([ ]PropertyValue) -> dict"""
    out = {}
    try:
        for p in node.UserData or ():
            out[str(p.Name)] = p.Value
    except Exception:  # noqa: BLE001
        pass
    return out


def _set_userdata(node, key, value):
    """XAnimationNode.UserData 是 sequence<NamedValue> (实测, 用 PropertyValue 会类型拒绝)。"""
    pvs = list(node.UserData or ())
    for p in pvs:
        if str(p.Name) == key:
            p.Value = value
            break
    else:
        nv = NamedValue()
        nv.Name = key
        nv.Value = value
        pvs.append(nv)
    node.UserData = tuple(pvs)


def _node_type_int(node):
    try:
        return int(_userdata(node).get("node-type", -1))
    except (TypeError, ValueError):
        return -1


def _is_main_sequence(node):
    v = _userdata(node).get("node-type", "")
    if _node_type_int(node) == 4:
        return True
    return isinstance(v, str) and v.endswith("MAIN_SEQUENCE")


def _main_sequence(page):
    root = _anim_root(page)
    for c in _children(root):
        if _is_main_sequence(c):
            return c
    if _is_main_sequence(root):  # 新页面的 getAnimationNode() 直接返回主序列本身
        return root
    return None


def _target_shape_name(page, node):
    """效果节点的 Target -> 形状名。"""
    try:
        target = node.Target
    except Exception:  # noqa: BLE001
        return None
    sh = target
    try:
        sh = target.Shape  # ParagraphTarget 包装
    except Exception:  # noqa: BLE001
        pass
    if sh is None:
        return None
    for i in range(page.Count):
        p = page.getByIndex(i)
        if p == sh:
            try:
                return str(sh.Name or "") or "#%d" % i
            except Exception:  # noqa: BLE001
                return "#%d" % i
    return None


# ---------------- op 实现 ----------------

def op_status(args):
    ver = "?"
    try:
        from importlib import metadata  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "python": sys.version.split()[0],
        "lo_program": LO_PROGRAM or os.path.dirname(os.path.abspath(sys.executable)),
        "doc_open": ST.doc is not None,
        "slides": (_pages().Count if ST.doc is not None else 0),
        "render_dir": ST.render_dir,
    }


def op_probe_effect(args):
    """在真实办公端 setPropertyValue("Effect", enum) 验证成员 (本地 pyuno 注册表可能不一致)。"""
    member = args["member"]
    try:
        e = uno.Enum("com.sun.star.presentation.AnimationEffect", member)
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "stage": "local-enum", "error": str(ex)}
    sh = ST.doc.createInstance("com.sun.star.drawing.TextShape")
    try:
        sh.setPropertyValue("Effect", e)
        return {"ok": True}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "stage": "office-set", "error": str(ex)}


def op_probe(args):
    """属性名/动画树探测 (开发排障用)。"""
    out = {"page_props": [], "shape_props": [], "anim_tree": None}
    # 页面/形状属性名
    try:
        info = _page(0).getPropertySetInfo()
        out["page_props"] = sorted(str(p.Name) for p in info.Properties)
    except Exception as e:  # noqa: BLE001
        out["page_props_error"] = repr(e)
    try:
        if _pages().Count and _page(0).Count:
            info = _page(0).getByIndex(0).getPropertySetInfo()
            out["shape_props"] = sorted(str(p.Name) for p in info.Properties)
    except Exception as e:  # noqa: BLE001
        out["shape_props_error"] = repr(e)
    # 当前页动画树
    try:
        out["anim_tree"] = _dump_anim(_page(0))
    except Exception as e:  # noqa: BLE001
        out["anim_tree_error"] = repr(e)
    return out


def _dump_anim(page):
    def dump(node, depth=0):
        ud = _userdata(node)
        d = {"type": str(getattr(node, "getType", lambda: "")()) if depth else "root",
             "user_data": {k: str(v) for k, v in ud.items()},
             "children": []}
        for k in ("Begin", "Duration", "Fill", "Restart", "PresetClass", "PresetID"):
            try:
                d[k.lower()] = str(node.getPropertyValue(k))
            except Exception:  # noqa: BLE001
                pass
        for c in _children(node):
            d["children"].append(dump(c, depth + 1))
        return d
    root = _anim_root(page)
    return dump(root) if root is not None else None


def _close_all_impress():
    """关掉 daemon 里所有 Impress 文档 (new_deck / open_deck 前清场)。"""
    docs = []
    try:
        comps = ST.desktop.Components.createEnumeration()
        while comps.hasMoreElements():
            d = comps.nextElement()
            try:
                if d.supportsService("com.sun.star.presentation.PresentationDocument"):
                    docs.append(d)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    for d in docs:
        try:
            d.setModified(False)
            d.close(False)
        except Exception:  # noqa: BLE001
            pass
    ST.doc = None


def op_new_deck(args):
    _close_all_impress()
    ST.doc = ST.desktop.loadComponentFromURL(
        "private:factory/simpress", "_blank", 0, (pv("Hidden", True),))
    w = float(args.get("width_mm", DEFAULT_PAGE_W_MM))
    h = float(args.get("height_mm", DEFAULT_PAGE_H_MM))
    pages = _pages()
    for i in range(pages.Count):
        pages.getByIndex(i).setPropertyValue("Width", mm(w))
        pages.getByIndex(i).setPropertyValue("Height", mm(h))
    ST._page_w = w
    ST._page_h = h
    return {"slides": pages.Count, "page_mm": [w, h]}


def op_open_deck(args):
    _close_all_impress()
    path = args["path"]
    if not os.path.isfile(path):
        raise ValueError("文件不存在: %s" % path)
    ST.doc = ST.desktop.loadComponentFromURL(
        systemPathToFileUrl(path), "_blank", 0, (pv("Hidden", True), pv("ReadOnly", False)))
    if ST.doc is None:
        raise RuntimeError("打开失败(格式不支持?): %s" % path)
    pages = _pages()
    if pages.Count:
        p0 = pages.getByIndex(0)
        ST._page_w = p0.Width / 100.0
        ST._page_h = p0.Height / 100.0
    return {"slides": pages.Count, "page_mm": [ST._page_w, ST._page_h]}


def op_save(args):
    if ST.doc is None:
        raise RuntimeError("没有打开的文档")
    path = os.path.abspath(args["path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fmt = args.get("format", "pptx")
    filters = {"pptx": "Impress MS PowerPoint 2007 XML",
               "odp": "impress8",
               "pdf": "impress_pdf_Export"}
    if fmt not in filters:
        raise ValueError("不支持的格式: %s (可选 %s)" % (fmt, sorted(filters)))
    ST.doc.storeToURL(systemPathToFileUrl(path), (pv("FilterName", filters[fmt]),))
    return {"path": path, "format": fmt, "size": os.path.getsize(path)}


def _close_doc():
    if ST.doc is not None:
        try:
            ST.doc.setModified(False)
            ST.doc.close(False)
        except Exception:  # noqa: BLE001
            pass
        ST.doc = None


def op_close(args):
    _close_doc()
    return {"closed": True}


def op_add_slide(args):
    pages = _pages()
    page = pages.insertNewByIndex(pages.Count)
    try:
        page.setPropertyValue("Width", mm(getattr(ST, "_page_w", DEFAULT_PAGE_W_MM)))
        page.setPropertyValue("Height", mm(getattr(ST, "_page_h", DEFAULT_PAGE_H_MM)))
    except Exception:  # noqa: BLE001
        pass
    return {"slide": pages.Count - 1}


def _shape_info(page, i, sh):
    pos = sh.getPosition()
    size = sh.getSize()
    info = {
        "index": i,
        "name": "",
        "type": "",
        "x_mm": round(pos.X / 100.0, 1), "y_mm": round(pos.Y / 100.0, 1),
        "w_mm": round(size.Width / 100.0, 1), "h_mm": round(size.Height / 100.0, 1),
        "text": "",
    }
    try:
        info["name"] = str(sh.Name or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        info["type"] = sh.getShapeType()
    except Exception:  # noqa: BLE001
        pass
    try:
        info["text"] = sh.getText().getString()[:80]
    except Exception:  # noqa: BLE001
        pass
    return info


def op_list_slides(args):
    pages = _pages()
    out = []
    for i in range(pages.Count):
        page = pages.getByIndex(i)
        tr = {}
        for k in ("TransitionType", "TransitionSubtype", "TransitionDirection", "TransitionDuration"):
            try:
                tr[k] = page.getPropertyValue(k)
            except Exception:  # noqa: BLE001
                pass
        shapes = [_shape_info(page, j, page.getByIndex(j)) for j in range(page.Count)]
        out.append({"index": i, "transition": tr, "shapes": shapes,
                    "animations": _list_page_animations(page)})
    return {"page_mm": [getattr(ST, "_page_w", None), getattr(ST, "_page_h", None)],
            "slides": out}


def op_add_textbox(args):
    page = _page(args["slide"])
    sh = ST.doc.createInstance("com.sun.star.drawing.TextShape")
    _set_pos_size(sh, args.get("x"), args.get("y"), args.get("w"), args.get("h"))
    page.add(sh)
    t = sh.getText()
    t.setString(str(args.get("text", "")))
    _style_text(t, args.get("font") or "Microsoft YaHei", args.get("font_size"),
                args.get("bold"), args.get("color"), args.get("align"))
    _apply_shape_text_align(sh, args.get("align"))
    try:
        sh.setPropertyValue("TextAutoGrowHeight", False)
        sh.setPropertyValue("TextWordWrap", True)
    except Exception:  # noqa: BLE001
        pass
    name = args.get("name") or _new_shape_name(page, "tb")
    try:
        sh.Name = name
    except Exception:  # noqa: BLE001
        pass
    return {"slide": args["slide"], "name": name}


def op_set_text(args):
    page = _page(args["slide"])
    sh = _find_shape(page, args["shape"])
    t = sh.getText()
    t.setString(str(args.get("text", "")))
    _style_text(t, args.get("font"), args.get("font_size"), args.get("bold"),
                args.get("color"), args.get("align"))
    return {"ok": True}


def op_add_shape(args):
    page = _page(args["slide"])
    kind = args.get("kind", "rectangle")
    services = {"rectangle": "com.sun.star.drawing.RectangleShape",
                "rounded": "com.sun.star.drawing.RectangleShape",
                "ellipse": "com.sun.star.drawing.EllipseShape",
                "line": "com.sun.star.drawing.LineShape",
                "text": "com.sun.star.drawing.TextShape"}
    if kind not in services:
        raise ValueError("未知形状: %s (可选 %s)" % (kind, sorted(services)))
    sh = ST.doc.createInstance(services[kind])
    _set_pos_size(sh, args.get("x"), args.get("y"), args.get("w"), args.get("h"))
    if kind == "rounded":
        try:
            sh.setPropertyValue("CornerRadius", 300)
        except Exception:  # noqa: BLE001
            pass
    page.add(sh)
    fill = args.get("fill")
    try:
        if fill in (None, "", "none"):
            sh.setPropertyValue("FillStyle", uno.Enum("com.sun.star.drawing.FillStyle", "NONE"))
        else:
            sh.setPropertyValue("FillStyle", uno.Enum("com.sun.star.drawing.FillStyle", "SOLID"))
            sh.setPropertyValue("FillColor", hex_color(fill))
    except Exception:  # noqa: BLE001
        pass
    line_color = args.get("line_color")
    try:
        if line_color in (None, "", "none"):
            sh.setPropertyValue("LineStyle", uno.Enum("com.sun.star.drawing.LineStyle", "NONE"))
        else:
            sh.setPropertyValue("LineStyle", uno.Enum("com.sun.star.drawing.LineStyle", "SOLID"))
            sh.setPropertyValue("LineColor", hex_color(line_color))
            sh.setPropertyValue("LineWidth", mm(args.get("line_width", 0.5)))
    except Exception:  # noqa: BLE001
        pass
    if args.get("text"):
        t = sh.getText()
        t.setString(str(args["text"]))
        _style_text(t, args.get("font") or "Microsoft YaHei", args.get("font_size"),
                    args.get("bold"), args.get("color"), args.get("align") or "center")
        _apply_shape_text_align(sh, args.get("align") or "center")
    name = args.get("name") or _new_shape_name(page, kind)
    try:
        sh.Name = name
    except Exception:  # noqa: BLE001
        pass
    return {"slide": args["slide"], "name": name}


def op_add_image(args):
    page = _page(args["slide"])
    path = os.path.abspath(args["path"])
    if not os.path.isfile(path):
        raise ValueError("图片不存在: %s" % path)
    sh = ST.doc.createInstance("com.sun.star.drawing.GraphicObjectShape")
    _set_pos_size(sh, args.get("x"), args.get("y"), args.get("w") or 10, args.get("h") or 10)
    page.add(sh)
    url = systemPathToFileUrl(path)
    graphic = None
    try:
        provider = ST.smgr.createInstanceWithContext("com.sun.star.graphic.GraphicProvider", ST.ctx)
        graphic = provider.queryGraphic((pv("URL", url),))
        sh.setPropertyValue("Graphic", graphic)
    except Exception as e:  # noqa: BLE001
        _log("GraphicProvider 失败, 退回 GraphicURL: %r" % (e,))
        sh.setPropertyValue("GraphicURL", url)
    # 宽高缺省时按图形原始纵横比补全 (纯比例, 避免单位混算)
    try:
        if graphic is not None and (args.get("w") is None or args.get("h") is None):
            g = sh.getPropertyValue("Graphic")
            try:
                s = g.Size100thMM
                nat_w, nat_h = s.Width, s.Height
            except Exception:  # noqa: BLE001
                sp = g.SizePixel
                nat_w, nat_h = sp.Width, sp.Height
            if nat_w > 0 and nat_h > 0:
                if args.get("w") and not args.get("h"):
                    _set_pos_size(sh, None, None, None,
                                  float(args["w"]) * nat_h / float(nat_w))
                elif args.get("h") and not args.get("w"):
                    _set_pos_size(sh, None, None,
                                  float(args["h"]) * nat_w / float(nat_h), None)
                else:
                    _set_pos_size(sh, None, None, nat_w / 100.0, nat_h / 100.0)
    except Exception as e:  # noqa: BLE001
        _log("原始尺寸适配失败: %r" % (e,))
    name = args.get("name") or _new_shape_name(page, "img")
    try:
        sh.Name = name
    except Exception:  # noqa: BLE001
        pass
    return {"slide": args["slide"], "name": name}


def _cell_border(color=0xBBBBBB, width=26):
    b = uno.createUnoStruct("com.sun.star.table.BorderLine2")
    b.Color = color
    b.LineWidth = width  # 1/100 mm, 26 ≈ 细线
    b.LineStyle = 0      # SOLID
    return b


def op_add_table(args):
    page = _page(args["slide"])
    rows = args.get("rows") or []
    if not rows:
        raise ValueError("rows 不能为空")
    sh = ST.doc.createInstance("com.sun.star.drawing.TableShape")
    _set_pos_size(sh, args.get("x"), args.get("y"), args.get("w"), args.get("h"))
    page.add(sh)
    table = None
    for attr in ("Model", "Table"):
        try:
            table = getattr(sh, attr)
            if table is not None:
                break
        except Exception:  # noqa: BLE001
            continue
    if table is None:
        raise RuntimeError("无法获取 Table 对象 (TableShape 接口变化?)")
    n_rows, n_cols = len(rows), max(len(r) for r in rows)
    try:
        table.initialize(n_rows, n_cols)
    except Exception:  # noqa: BLE001
        pass
    font = args.get("font") or "Microsoft YaHei"
    fs = args.get("font_size", 12)
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            if c >= table.ColumnCount or r >= table.RowCount:
                break
            cell = table.getCellByPosition(c, r)
            cell.setString(str(val))
            _style_text(cell, font, fs, True if (r == 0 and args.get("header_bold", True)) else None,
                        None, None)
    if args.get("header_fill"):
        try:
            hexv = hex_color(args["header_fill"])
        except ValueError:
            hexv = None
        if hexv is not None:
            for c in range(min(n_cols, table.ColumnCount)):
                cell = table.getCellByPosition(c, 0)
                for prop in ("CellBackColor", "CellBackgroundColour", "FillColor"):
                    try:
                        cell.setPropertyValue(prop, hexv)
                        break
                    except Exception:  # noqa: BLE001
                        continue
    if args.get("border", True):
        try:
            bl = _cell_border(hex_color(args.get("border_color") or "BBBBBB"))
        except ValueError:
            bl = _cell_border()
        for r_ in range(table.RowCount):
            for c_ in range(table.ColumnCount):
                cell = table.getCellByPosition(c_, r_)
                for side in ("LeftBorder", "RightBorder", "TopBorder", "BottomBorder"):
                    try:
                        cell.setPropertyValue(side, bl)
                    except Exception:  # noqa: BLE001
                        pass
    name = args.get("name") or _new_shape_name(page, "tbl")
    try:
        sh.Name = name
    except Exception:  # noqa: BLE001
        pass
    return {"slide": args["slide"], "name": name, "rows": n_rows, "cols": n_cols}


def _has_prop(obj, name):
    try:
        return obj.getPropertySetInfo().hasPropertyByName(name)
    except Exception:  # noqa: BLE001
        return False


def op_render_slide(args):
    page = _page(args["slide"])
    width_px = int(args.get("width_px", 1280))
    pw, ph = page.Width, page.Height
    height_px = int(width_px * ph / float(pw))
    out = os.path.join(ST.render_dir, "slide_%02d.png" % int(args["slide"]))
    if os.path.exists(out):
        os.remove(out)
    exporter = ST.smgr.createInstanceWithContext(
        "com.sun.star.drawing.GraphicExportFilter", ST.ctx)
    exporter.setSourceDocument(page)
    # Logical 尺寸必须=整页(1/100mm), 否则 PixelWidth 会被忽略 (实测只出 682px)
    fd = uno.Any("[]com.sun.star.beans.PropertyValue", (
        pv("PixelWidth", width_px), pv("PixelHeight", height_px),
        pv("LogicalWidth", pw), pv("LogicalHeight", ph),
    ))
    ok = exporter.filter((
        pv("FilterName", "impress_png_Export"),
        pv("URL", systemPathToFileUrl(out)),
        pv("FilterData", fd),
    ))
    if not os.path.exists(out):
        raise RuntimeError("渲染失败 filter=%r" % (ok,))
    return {"slide": args["slide"], "png": out}


# ---------------- 切换与动画 op ----------------

def op_set_transition(args):
    page = _page(args["slide"])
    effect = args["effect"]
    if effect in (None, "", "none"):
        for k in ("TransitionType", "TransitionSubtype"):
            try:
                page.setPropertyValue(k, 0)
            except Exception:  # noqa: BLE001
                pass
        return {"slide": args["slide"], "effect": "none"}
    if effect not in presets.TRANSITIONS:
        raise ValueError("未知切换效果: %s (可选 %s)" % (effect, sorted(presets.TRANSITIONS)))
    t, sub = presets.TRANSITIONS[effect]
    page.setPropertyValue("TransitionType", t)
    page.setPropertyValue("TransitionSubtype", sub)
    try:
        page.setPropertyValue("TransitionDirection", bool(args.get("direction", True)))
    except Exception:  # noqa: BLE001
        pass
    dur = args.get("duration")
    applied = None
    if dur is not None:
        for k in ("TransitionDuration", "Duration"):
            try:
                page.setPropertyValue(k, float(dur))
                applied = k
                break
            except Exception:  # noqa: BLE001
                continue
    return {"slide": args["slide"], "effect": effect, "type": t, "subtype": sub,
            "duration_prop": applied}


# ---------------- 动画 op (纯 SMIL 节点树构建, 见 presets.py 结构约定) ----------------

ANIM_PAR = "com.sun.star.animations.ParallelTimeContainer"
ANIM_SET = "com.sun.star.animations.AnimateSet"
_TRIGGER_INT = {"onclick": 1, "with_previous": 2, "after_previous": 3}


def _make_anim_node(service):
    return ST.smgr.createInstanceWithContext(service, ST.ctx)


def _effect_nodes(page):
    """[(outer, inner, node)]: main_seq → outer par → inner par → 效果节点。"""
    out = []
    main = _main_sequence(page)
    if main is None:
        return out
    for outer in _children(main):
        for inner in _children(outer):
            for node in _children(inner):
                out.append((outer, inner, node))
    return out


def op_add_animation(args):
    page = _page(args["slide"])
    sh = _find_shape(page, args["shape"])
    effect = args["effect"]
    spec = presets.ANIMATIONS.get(effect)
    if spec is None:
        raise ValueError("未知效果: %s (可选 %s)" % (effect, sorted(presets.ANIMATIONS)))
    trigger = args.get("trigger", "onclick")
    if trigger not in presets.TRIGGERS:
        raise ValueError("未知触发: %s (可选 %s)" % (trigger, presets.TRIGGERS))
    delay = float(args.get("delay") or 0.0)
    duration = float(args.get("duration") or presets.DEFAULT_DURATION)

    main = _main_sequence(page)
    if main is None:
        # 新页面 root(TIMING_ROOT) 下没有主序列 → 创建 (导出为 <p:seq nodeType="mainSeq">)
        main = _make_anim_node("com.sun.star.animations.SequenceTimeContainer")
        _set_userdata(main, "node-type", 4)
        _append_child(_anim_root(page), main)

    outer_groups = [g for g in _children(main) if _children(g)]
    if trigger in ("onclick", "after_previous") or not outer_groups:
        outer = _make_anim_node(ANIM_PAR)
        inner = _make_anim_node(ANIM_PAR)
        inner.Begin = 0
        _append_child(outer, inner)
        _append_child(main, outer)
        outer.Begin = "indefinite" if trigger == "onclick" else delay
        effect_begin = 0.0
    else:  # with_previous: 复用最后一组
        outer = outer_groups[-1]
        inner = _children(outer)[-1]
        effect_begin = delay

    node = _make_anim_node(ANIM_SET)
    node.Target = sh
    node.AttributeName = "Visibility"
    node.To = spec["cls"] != "exit"
    node.Begin = effect_begin
    try:
        node.Duration = duration
    except Exception:  # noqa: BLE001
        pass
    try:
        node.Fill = 3  # com.sun.star.animations.TimingFill.HOLD (short 常量)
    except Exception:  # noqa: BLE001
        pass
    _set_userdata(node, "node-type", _TRIGGER_INT[trigger])
    _set_userdata(node, "preset-class", 1 if spec["cls"] == "entrance" else 2)
    _set_userdata(node, "preset-id", spec["preset_id"])
    if spec.get("subtype"):
        _set_userdata(node, "preset-sub-type", spec["subtype"])
    _append_child(inner, node)
    return {"slide": args["slide"], "shape": args["shape"], "effect": effect,
            "trigger": trigger, "delay": delay, "duration": duration}


def op_list_animations(args):
    page = _page(args["slide"])
    return {"slide": args["slide"], "animations": _list_page_animations(page)}


def _list_page_animations(page):
    out = []
    for _outer, _inner, node in _effect_nodes(page):
        ud = _userdata(node)
        begin = dur = None
        try:
            begin = node.Begin
        except Exception:  # noqa: BLE001
            pass
        try:
            dur = node.Duration
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "shape": _target_shape_name(page, node),
            "trigger": {v: k for k, v in _TRIGGER_INT.items()}.get(ud.get("node-type"),
                                                                    str(ud.get("node-type"))),
            "preset_id": str(ud.get("preset-id", "")),
            "preset_class": str(ud.get("preset-class", "")),
            "subtype": str(ud.get("preset-sub-type", "")),
            "begin": begin if isinstance(begin, (int, float, str)) else str(begin),
            "duration": dur if isinstance(dur, (int, float)) else str(dur),
        })
    return out


def op_remove_animation(args):
    page = _page(args["slide"])
    sh = _find_shape(page, args["shape"])
    removed = 0
    for outer, inner, node in _effect_nodes(page):
        try:
            t = node.Target
        except Exception:  # noqa: BLE001
            t = None
        tsh = getattr(t, "Shape", t)
        if tsh is not None and tsh == sh:
            if _remove_child(inner, node):
                removed += 1
            if not _children(inner):
                _remove_child(outer, inner)
    return {"slide": args["slide"], "shape": args["shape"], "removed": removed}


def op_shutdown(args):
    try:
        _close_doc()
    finally:
        ST._cleaned = True
        _cleanup_soffice()
    return {"bye": True}


OPS_TABLE = {
    "status": op_status,
    "probe": op_probe,
    "probe_effect": op_probe_effect,
    "new_deck": op_new_deck,
    "open_deck": op_open_deck,
    "save": op_save,
    "close": op_close,
    "add_slide": op_add_slide,
    "list_slides": op_list_slides,
    "add_textbox": op_add_textbox,
    "set_text": op_set_text,
    "add_shape": op_add_shape,
    "add_image": op_add_image,
    "add_table": op_add_table,
    "render_slide": op_render_slide,
    "set_transition": op_set_transition,
    "add_animation": op_add_animation,
    "list_animations": op_list_animations,
    "remove_animation": op_remove_animation,
    "shutdown": op_shutdown,
}


def main():
    global LO_PROGRAM
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--lo-program":
            LO_PROGRAM = argv[i + 1]; i += 2
        else:
            i += 1
    _import_uno()
    connect()
    _log("connected via pipe %s" % PIPE_NAME)

    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
    stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({"id": None, "ok": False, "error": "bad json"},
                                    ensure_ascii=False) + "\n")
            continue
        rid = req.get("id")
        op = req.get("op")
        args = req.get("args") or {}
        if op not in OPS_TABLE:
            resp = {"id": rid, "ok": False, "error": "unknown op: %r" % (op,)}
        else:
            resp = None
            for attempt in (1, 2):
                try:
                    result = OPS_TABLE[op](args)
                    resp = {"id": rid, "ok": True, "result": result}
                    break
                except Exception as e:  # noqa: BLE001
                    # 桥被释放(上个客户端清理竞态/soffice 重启) → 重连后重试一次
                    if attempt == 1 and ("Disposed" in type(e).__name__ or "disposed" in str(e)):
                        _log("bridge disposed, reconnecting: %r" % (e,))
                        try:
                            ST.doc = None
                            ST.spawned = False
                            connect()
                            continue
                        except Exception as re:  # noqa: BLE001
                            _log("reconnect failed: %r" % (re,))
                    _log("op %s failed: %s" % (op, traceback.format_exc()))
                    resp = {"id": rid, "ok": False,
                            "error": "%s: %s" % (type(e).__name__, e),
                            "traceback": traceback.format_exc(limit=5)}
                    break
        stdout.write(json.dumps(resp, ensure_ascii=False, default=str) + "\n")
        if op == "shutdown":
            try:
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass
            break
    # stdin EOF: worker 退出; 只有亲手拉起 soffice 的 worker 才带走它, 避免 orphan;
    # 后续接入的 worker 复用常驻实例, 不影响
    if ST.spawned and not getattr(ST, "_cleaned", False):
        ST._cleaned = True
        _cleanup_soffice()


if __name__ == "__main__":
    main()
