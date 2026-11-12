#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
mcb.py — Minecraft 嬗变台 自动刷（视觉识别版）

你给一张目标物品的图，它在游戏画面上实时比对，看到就点，刷到就停。

热键（全局，任何时候都有效）
    F9    暂停 / 继续
    F10   停止（结束本次刷取）
    F6    录制循环开始        F11  打点（结果出现那一刻）      F7  录制结束

常用命令
    python mcb.py quick        一条龙向导：框槽位 → 给图 → 录一遍 → 开刷
    python mcb.py add 钻石.png  把任意来源的图片加为目标（自动适配缩放）
    python mcb.py run          直接开刷
    python mcb.py watch        实时看匹配分数（调阈值用）
    python mcb.py selftest     自检
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
from functools import lru_cache
import glob
import json
import os
import shutil
import struct
import sys
import time

import numpy as np
from PIL import Image
from hotkeys import ChordTracker, InputListener, key_identity, key_token

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else RESOURCE_DIR
APP_VERSION = "1.1.0"
APP_NAME = "嬗变台自动刷取"
REPO_URL = "https://github.com/MyRemme/mc-transmute-bot"
BILIBILI_URL = "https://space.bilibili.com/1746890498"
CFG_PATH = os.path.join(HERE, "config.json")
TARGET_DIR = os.path.join(HERE, "targets")
MACRO_DIR = os.path.join(HERE, "macros")

# Minecraft GUI 缩放 1/1.5/2/3/4 对应的图标像素尺寸
ICON_SCALES = (16, 24, 32, 48, 64)

DEFAULT_CFG = {
    "window_title": "Minecraft",
    "watch": [0, 0, 0, 0],
    "threshold": 0.90,
    "settle": 0.15,
    "scan_tries": 2,
    "scan_interval": 0.06,
    "loop_delay": 0.05,
    "speed": 1.3,
    "stop_on_hit": True,
    "success_sound": "assets/success.wav",
    "guard": None,
    "guard_threshold": 0.85,
    "hotkeys": {
        "start": "f8",
        "pause": "f9",        # 暂停 / 继续
        "stop": "f10",        # 停止
        "record_start": "f6",
        "record_mark": "f11",
        "record_stop": "f7",
        "mark": "space",      # 标定槽位时打点
    },
}


def hk(cfg, name: str) -> str:
    """取配置里的单键或组合快捷键。"""
    h = (cfg or {}).get("hotkeys") or {}
    return str(h.get(name) or DEFAULT_CFG["hotkeys"][name]).lower()


def success_sound(cfg=None):
    """Play the configured WAV asynchronously after a successful selection."""
    import winsound
    cfg = cfg or load_cfg()
    path = cfg.get("success_sound", DEFAULT_CFG["success_sound"])
    if not os.path.isabs(path):
        local_path = os.path.join(HERE, path)
        path = local_path if os.path.exists(local_path) else os.path.join(RESOURCE_DIR, path)
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except RuntimeError as error:
        log(f"提示音播放失败: {error}")


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------
def log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + str(msg), flush=True)


def dpi_aware() -> None:
    """必须在截图/取坐标之前调用，否则 125%/150% 缩放下坐标全错。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


dpi_aware()


def load_cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    cfg["hotkeys"] = dict(DEFAULT_CFG["hotkeys"])
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
            cfg["hotkeys"] = {**DEFAULT_CFG["hotkeys"], **(cfg.get("hotkeys") or {})}
        except Exception as e:
            log(f"配置读取失败，用默认值: {e}")
    return cfg


def save_cfg(cfg: dict) -> None:
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp",
                                     dir=os.path.dirname(os.path.abspath(CFG_PATH)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(cfg, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, CFG_PATH)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def grab(bbox=None):
    """截屏 -> (float32 灰度, PIL 图)。bbox 为物理像素 (x1,y1,x2,y2)。"""
    img, _origin = capture_game(bbox)
    return np.asarray(img.convert("L"), dtype=np.float32), img


def is_blank(gray: np.ndarray, thr: float = 2.0) -> bool:
    """空槽是纯色背景，直接跳过：省算力，也避免误报。"""
    return float(gray.std()) < thr


def _down(g, f: int):
    if f <= 1:
        return g
    im = Image.fromarray(g.astype(np.uint8))
    return np.asarray(im.resize((max(1, im.size[0] // f), max(1, im.size[1] // f)),
                                Image.BOX), dtype=np.float32)


@lru_cache(maxsize=128)
def _template_spectra(shape, template_shape, template_bytes, mask_bytes):
    """Cache template transforms by pixel content, so replaced images cannot reuse stale data."""
    template = np.frombuffer(template_bytes, dtype=np.float32).reshape(template_shape)
    h, w, channels = template_shape
    weights = np.frombuffer(mask_bytes, dtype=np.bool_).reshape(h, w).astype(np.float32)
    count = float(weights.sum()) * channels
    if count == 0:
        return None, None, 0.0, count
    mean = float((template * weights[:, :, None]).sum()) / count
    centered = (template - mean) * weights[:, :, None]
    norm = float(np.linalg.norm(centered))
    template_fft = np.fft.rfft2(centered.transpose(2, 0, 1), s=shape).conj()
    mask_fft = np.fft.rfft2(weights, s=shape).conj()
    return template_fft, mask_fft, norm, count


def _match_exact(gray: np.ndarray, tpl: np.ndarray, mask=None, *, cache=None):
    """Masked NCC over grayscale or RGB pixels, including color when supplied."""
    H, W = gray.shape[:2]
    h, w = tpl.shape[:2]
    if h > H or w > W or h < 4 or w < 4:
        return 0.0, (0, 0)
    image = np.asarray(gray, dtype=np.float32)
    template = np.asarray(tpl, dtype=np.float32)
    if image.ndim == 2:
        image = image[:, :, None]
        template = template[:, :, None]
    weights = np.ones((h, w), dtype=np.bool_) if mask is None else np.asarray(mask, dtype=np.bool_)
    # Valid correlation positions never wrap, so padding to the image size is sufficient.
    shape = (1 << (H - 1).bit_length(), 1 << (W - 1).bit_length())
    template_fft, mask_fft, norm, count = _template_spectra(
        shape, template.shape, template.tobytes(), weights.tobytes())
    if norm < 1e-6:
        return 0.0, (0, 0)

    spectra = cache.get(shape) if cache is not None else None
    if spectra is None:
        image_fft = np.fft.rfft2(image.transpose(2, 0, 1), s=shape)
        energy_fft = np.fft.rfft2((image * image).sum(axis=2), s=shape)
        spectra = (image_fft, image_fft.sum(axis=0), energy_fft)
        if cache is not None:
            cache[shape] = spectra
    image_fft, sum_fft, energy_fft = spectra
    correlations = np.stack(((image_fft * template_fft).sum(axis=0),
                             sum_fft * mask_fft, energy_fft * mask_fft))
    values = np.fft.irfft2(correlations, s=shape)[:, :H - h + 1, :W - w + 1]
    variance = np.maximum(0.0, values[2] - values[1] ** 2 / count)
    denom = norm * np.sqrt(variance)
    scores = np.divide(values[0], denom, out=np.zeros_like(denom), where=denom > 1e-6)
    y, x = np.unravel_index(int(np.argmax(scores)), scores.shape)
    return float(np.clip(scores[y, x], -1.0, 1.0)), (int(x), int(y))


def match(gray: np.ndarray, tpl: np.ndarray, mask=None, *, cache=None):
    """
    带遮罩时通过 FFT 做精确归一化互相关。
    无遮罩的大图先用金字塔粗定位，再回原图精修。
    """
    H, W = gray.shape[:2]
    h, w = tpl.shape[:2]
    if h > H or w > W or h < 4 or w < 4:
        return 0.0, (0, 0)

    if mask is not None:
        return _match_exact(gray, tpl, mask, cache=cache)

    npos = (H - h + 1) * (W - w + 1)
    cost = npos * h * w
    if cost <= 4_000_000:
        return _match_exact(gray, tpl)

    f = max(2, min(8, int(np.ceil(np.sqrt(cost / 4_000_000)))))
    gs, ts = _down(gray, f), _down(tpl, f)
    if ts.shape[0] < 4 or ts.shape[1] < 4:
        return _match_exact(gray, tpl)

    _s, (cx, cy) = _match_exact(gs, ts)
    px, py = cx * f, cy * f
    pad = f + 3
    x0, y0 = max(0, px - pad), max(0, py - pad)
    x1, y1 = min(W, px + pad + w), min(H, py + pad + h)
    sub = gray[y0:y1, x0:x1]
    if sub.shape[0] < h or sub.shape[1] < w:
        return _match_exact(gray, tpl)
    s2, (ox, oy) = _match_exact(sub, tpl)
    return s2, (x0 + ox, y0 + oy)


def autotrim(img: Image.Image, tol: int = 18) -> Image.Image:
    """把框选时多带的槽位背景裁掉，只留物品本身。"""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    h, w, _ = a.shape
    if h < 8 or w < 8:
        return img
    corners = np.array([a[0, 0], a[0, w - 1], a[h - 1, 0], a[h - 1, w - 1]])
    bg = np.median(corners, axis=0)
    mask = np.abs(a - bg).sum(axis=2) > tol
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return img
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    if (y1 - y0) < 4 or (x1 - x0) < 4:
        return img
    if (y1 - y0) >= h - 1 and (x1 - x0) >= w - 1:
        return img
    return img.crop((x0, y0, x1, y1))


def list_windows(sub: str = "") -> list[str]:
    """枚举可见窗口标题，可按关键字过滤。"""
    u = ctypes.windll.user32
    out: list[str] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        t = buf.value
        if not sub or sub.lower() in t.lower():
            out.append(t)
        return True

    try:
        u.EnumWindows(cb, 0)
    except Exception:
        pass
    return out


def _win_process(hwnd) -> tuple[str, int]:
    """Read the executable and creation time to distinguish recycled process IDs."""
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    hp = k.OpenProcess(0x1000, False, pid.value)
    if not hp:
        return "", 0
    try:
        buf = ctypes.create_unicode_buffer(512)
        n = wt.DWORD(512)
        exe = ""
        if k.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(n)):
            exe = os.path.basename(buf.value).lower()
        created, exited, kernel, user = (wt.FILETIME() for _ in range(4))
        started = 0
        if k.GetProcessTimes(hp, ctypes.byref(created), ctypes.byref(exited),
                             ctypes.byref(kernel), ctypes.byref(user)):
            started = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return exe, started
    finally:
        k.CloseHandle(hp)


def _win_exe(hwnd) -> str:
    return _win_process(hwnd)[0]


def _decl() -> None:
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.OpenProcess.restype = wt.HANDLE
    k.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                          ctypes.POINTER(wt.DWORD)]
    k.CloseHandle.argtypes = [wt.HANDLE]
    k.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.POINTER(wt.FILETIME)] * 4
    u.WindowFromPoint.argtypes = [wt.POINT]
    u.WindowFromPoint.restype = wt.HWND
    u.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
    u.GetAncestor.restype = wt.HWND
    u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    u.IsWindow.argtypes = [wt.HWND]
    u.IsWindowVisible.argtypes = [wt.HWND]
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.SetForegroundWindow.argtypes = [wt.HWND]
    u.BringWindowToTop.argtypes = [wt.HWND]
    u.IsIconic.argtypes = [wt.HWND]
    u.IsZoomed.argtypes = [wt.HWND]
    u.GetForegroundWindow.restype = wt.HWND
    u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u.GetWindowTextLengthW.argtypes = [wt.HWND]
    u.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]


_WINDOW_CACHE = {}
_WINDOW_SELECTION = None
_WINDOW_CONFIG_LOADED = False


def window_details(hwnd) -> dict:
    _decl()
    u = ctypes.windll.user32
    if not u.IsWindow(hwnd):
        return {}
    title = ctypes.create_unicode_buffer(u.GetWindowTextLengthW(hwnd) + 1)
    u.GetWindowTextW(hwnd, title, len(title))
    classname = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, classname, len(classname))
    pid = wt.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(rect))
    exe, started = _win_process(hwnd)
    return {"hwnd": int(hwnd), "pid": pid.value, "started": started,
            "title": title.value, "exe": exe, "class": classname.value,
            "width": rect.right - rect.left, "height": rect.bottom - rect.top}


def available_game_windows(title_sub: str = "") -> list[dict]:
    """List Java/LWJGL game candidates, ranked by class and title evidence."""
    _decl()
    u = ctypes.windll.user32
    candidates = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not u.IsWindowVisible(hwnd):
            return True
        info = window_details(hwnd)
        if not info or not info["title"]:
            return True
        if not u.IsIconic(hwnd) and (info["width"] < 300 or info["height"] < 200):
            return True
        java = info["exe"] in ("java.exe", "javaw.exe")
        lwjgl = info["class"].lower() in ("glfw30", "lwjgl")
        if java or lwjgl:
            title = info["title"].lower()
            info["score"] = (10 * java + 10 * lwjgl + 5 * ("minecraft" in title)
                             + 5 * bool(title_sub and title_sub.lower() in title))
            candidates.append(info)
        return True

    try:
        u.EnumWindows(cb, 0)
    except Exception:
        return []
    candidates.sort(key=lambda item: (item["score"], item["width"] * item["height"]),
                    reverse=True)
    return candidates


def set_window_selection(selection):
    global _WINDOW_SELECTION, _WINDOW_CONFIG_LOADED
    _WINDOW_SELECTION = dict(selection) if selection else None
    _WINDOW_CONFIG_LOADED = True
    _WINDOW_CACHE.clear()


def _selected_window_handle(selection) -> int:
    """Require the original window and process, including its creation time."""
    info = window_details(selection.get("hwnd", 0))
    keys = ("pid", "started", "exe", "class")
    if info and all(info[key] == selection.get(key) for key in keys):
        return info["hwnd"]
    return 0


def find_game_window(title_sub: str = "") -> int:
    """Resolve the bound window, or use automatic detection before selection."""
    global _WINDOW_CONFIG_LOADED
    _decl()
    if not _WINDOW_CONFIG_LOADED:
        cfg = load_cfg()
        set_window_selection(cfg.get("selected_window"))
    if _WINDOW_SELECTION:
        return _selected_window_handle(_WINDOW_SELECTION)
    u = ctypes.windll.user32
    cached = _WINDOW_CACHE.get(title_sub)
    if cached and time.monotonic() < cached[1] and u.IsWindow(cached[0]):
        return cached[0]
    candidates = available_game_windows(title_sub)
    if not candidates:
        return 0
    hwnd = candidates[0]["hwnd"]
    _WINDOW_CACHE[title_sub] = (hwnd, time.monotonic() + 1.0)
    return hwnd


def beep(times: int = 1) -> None:
    try:
        import winsound
        for _ in range(times):
            winsound.Beep(1200, 180)
    except Exception:
        pass


def client_box(hwnd=None):
    """游戏窗口客户区：(left, top, width, height)，屏幕物理像素。"""
    _decl()
    hwnd = hwnd or find_game_window("")
    if not hwnd:
        return None
    u = ctypes.windll.user32
    u.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    u.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    rc = wt.RECT()
    if not u.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    pt = wt.POINT(0, 0)
    u.ClientToScreen(hwnd, ctypes.byref(pt))
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return None
    return (pt.x, pt.y, w, h)


def to_norm_rect(box, cb):
    x, y, w, h = cb
    return [round((box[0] - x) / w, 4), round((box[1] - y) / h, 4),
            round((box[2] - x) / w, 4), round((box[3] - y) / h, 4)]


def from_norm_rect(n, cb):
    x, y, w, h = cb
    return [int(x + n[0] * w), int(y + n[1] * h), int(x + n[2] * w), int(y + n[3] * h)]


def to_norm_pts(pts, cb):
    x, y, w, h = cb
    return [[round((p[0] - x) / w, 4), round((p[1] - y) / h, 4)] for p in pts]


def from_norm_pts(norm, cb):
    x, y, w, h = cb
    return [[int(x + p[0] * w), int(y + p[1] * h)] for p in norm]


def game_ready() -> tuple[bool, str]:
    """开刷前的硬性检查：游戏必须在屏幕上、没有被挡住。否则截图和点击都会落到别的窗口。"""
    _decl()
    u = ctypes.windll.user32
    hwnd = find_game_window("")
    if not hwnd:
        return False, "目标窗口不可用，请在“标定与设置”中自动识别或手动选择游戏窗口"
    if u.IsIconic(hwnd):
        return False, "游戏窗口被最小化了 —— 请先点开它"
    cb = client_box(hwnd)
    if not cb:
        return False, "拿不到游戏窗口客户区"
    cx, cy = cb[0] + cb[2] // 2, cb[1] + cb[3] // 2
    h = u.WindowFromPoint(wt.POINT(cx, cy))
    u.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
    root = u.GetAncestor(h, 2)  # GA_ROOT
    if root != hwnd:
        return False, "游戏窗口不在最前面（被别的窗口挡住了），截图和点击都会打到那个窗口上"
    return True, ""


def capture_game(bbox=None):
    """Capture only the Minecraft client DC, returning image and screen origin."""
    _decl()
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = find_game_window("")
    if not hwnd or u.IsIconic(hwnd):
        raise RuntimeError("目标窗口不可用或已最小化，请打开游戏并重新选择窗口")
    cb = client_box(hwnd)
    if not cb:
        raise RuntimeError("无法获取游戏窗口客户区")
    left, top, width, height = cb
    rect = tuple(map(int, bbox)) if bbox is not None else (left, top, left + width, top + height)
    x0, y0, x1, y1 = rect
    if not (left <= x0 < x1 <= left + width and top <= y0 < y1 <= top + height):
        raise RuntimeError("截图区域超出 Minecraft 游戏窗口，请重新标定")
    for x, y in ((x0, y0), (x1 - 1, y0), (x0, y1 - 1), (x1 - 1, y1 - 1),
                 ((x0 + x1) // 2, (y0 + y1) // 2)):
        under = u.WindowFromPoint(wt.POINT(x, y))
        if u.GetAncestor(under, 2) != hwnd:
            raise RuntimeError("截图区域被其他窗口遮挡，请将游戏切到前台")

    u.GetDC.argtypes = [wt.HWND]
    u.GetDC.restype = wt.HDC
    u.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    g.CreateCompatibleDC.argtypes = [wt.HDC]
    g.CreateCompatibleDC.restype = wt.HDC
    g.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    g.CreateCompatibleBitmap.restype = wt.HBITMAP
    g.SelectObject.argtypes = [wt.HDC, wt.HANDLE]
    g.SelectObject.restype = wt.HANDLE
    g.BitBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
    g.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                           ctypes.c_void_p, wt.UINT]
    g.DeleteObject.argtypes = [wt.HANDLE]
    g.DeleteDC.argtypes = [wt.HDC]
    w, h = x1 - x0, y1 - y0
    dc = u.GetDC(hwnd)
    memory = g.CreateCompatibleDC(dc)
    bitmap = g.CreateCompatibleBitmap(dc, w, h)
    previous = g.SelectObject(memory, bitmap) if bitmap else None
    try:
        if not dc or not memory or not bitmap or not previous:
            raise RuntimeError("无法创建游戏窗口截图")
        if not g.BitBlt(memory, 0, 0, w, h, dc, x0 - left, y0 - top, 0x00CC0020):
            raise RuntimeError("读取游戏窗口截图失败")
        g.SelectObject(memory, previous)
        previous = None
        header = ctypes.create_string_buffer(40)
        struct.pack_into("<IiiHHIIiiII", header, 0, 40, w, -h, 1, 32, 0, w * h * 4,
                         0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(w * h * 4)
        if g.GetDIBits(memory, bitmap, 0, h, pixels, header, 0) != h:
            raise RuntimeError("读取游戏窗口像素失败")
        return Image.frombytes("RGB", (w, h), pixels.raw, "raw", "BGRX"), (x0, y0)
    finally:
        if previous:
            g.SelectObject(memory, previous)
        if bitmap:
            g.DeleteObject(bitmap)
        if memory:
            g.DeleteDC(memory)
        if dc:
            u.ReleaseDC(hwnd, dc)


def get_watch(cfg) -> list:
    """优先用相对坐标换算，窗口挪了/改了大小也不会偏。"""
    cb = client_box()
    n = cfg.get("watch_norm")
    if n and cb:
        return from_norm_rect(n, cb)
    return list(cfg.get("watch", [0, 0, 0, 0]))


def get_slots(cfg) -> list:
    cb = client_box()
    n = cfg.get("slots_norm")
    if n and cb:
        return from_norm_pts(n[:3], cb)
    # Older configurations may include the bottom input slot as a fourth result.
    return list(cfg.get("slots") or [])[:3]


def save_watch(cfg, box) -> None:
    cfg["watch"] = list(box)
    cb = client_box()
    if cb:
        cfg["watch_norm"] = to_norm_rect(box, cb)
        cfg["client_at_mark"] = list(cb)
    save_cfg(cfg)


def get_refill(cfg):
    cb = client_box()
    n = cfg.get("refill_norm")
    if n and cb:
        return [int(cb[0] + n[0] * cb[2]), int(cb[1] + n[1] * cb[3])]
    return cfg.get("refill")


def save_refill(cfg, pt) -> None:
    cfg["refill"] = [int(pt[0]), int(pt[1])]
    cb = client_box()
    if cb:
        cfg["refill_norm"] = [round((pt[0] - cb[0]) / cb[2], 4),
                              round((pt[1] - cb[1]) / cb[3], 4)]
    save_cfg(cfg)


def save_slots(cfg, pts) -> None:
    cfg["slots"] = [list(p) for p in pts]
    cb = client_box()
    if cb:
        cfg["slots_norm"] = to_norm_pts(pts, cb)
    save_cfg(cfg)


def focus_window(title_sub: str, tries: int = 3) -> bool:
    """
    把游戏切到前台。只调 SetForegroundWindow（不会改窗口大小），
    但 Windows 默认禁止后台进程抢前台，所以用「按一下 ALT」解锁，并重试几次。
    """
    _decl()
    u = ctypes.windll.user32
    hwnd = find_game_window(title_sub)
    if not hwnd:
        return False
    if u.IsIconic(hwnd):
        log("游戏窗口是最小化的 —— 请手动点开（我不会替你还原，免得改窗口大小）")
        return False

    for i in range(max(1, tries)):
        if u.GetForegroundWindow() == hwnd:
            return True
        u.BringWindowToTop(hwnd)
        u.keybd_event(0x12, 0, 0, 0)        # ALT down：解除前台锁定
        u.keybd_event(0x12, 0, 0x0002, 0)   # ALT up
        u.SetForegroundWindow(hwnd)
        time.sleep(0.18)
        if u.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.2)
    return u.GetForegroundWindow() == hwnd


# --------------------------------------------------------------------------
# 目标模板（支持任意来源的图，自动适配 GUI 缩放）
# --------------------------------------------------------------------------
def _to_gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float32)


def build_mask(img_rgb, tol: int = 34):
    """
    算出「物品本身」的像素遮罩：把占比最大的颜色当背景剔除。
    这样同一物品不管是灰底、绿底（高亮）还是别的背景都能比对。
    """
    if "A" in img_rgb.getbands():
        alpha = np.asarray(img_rgb.getchannel("A"))
        if bool((alpha < 255).any()):
            return alpha >= 128
    rgb = np.asarray(img_rgb.convert("RGB")).astype(np.float32)
    h, w, _ = rgb.shape
    q = rgb // 8 * 8
    uniq, cnt = np.unique(q.reshape(-1, 3), axis=0, return_counts=True)
    bg = uniq[cnt.argmax()].astype(np.float32)
    m = np.abs(rgb - bg).sum(axis=2) > tol
    # Cropped slots can include several background colors and a beveled border.
    edges = np.concatenate((q[0], q[-1], q[1:-1, 0], q[1:-1, -1]))
    edge_colors, edge_counts = np.unique(edges, axis=0, return_counts=True)
    for color in edge_colors[edge_counts >= max(4, min(h, w) * 0.2)]:
        m &= np.abs(rgb - color).sum(axis=2) > tol
    frac = float(m.mean())
    if frac < 0.03 or frac > 0.97:
        return np.ones((h, w), bool)
    return m


def build_variants(img: Image.Image, auto_scale: bool = True):
    """
    生成不同尺寸的模板变体：[(h, w, 灰度, 遮罩, RGB), ...]
    - 方形图标额外试 16/24/32/48/64（GUI 缩放 1/1.5/2/3/4）
    - 所有模板再试 0.5/0.65/0.8/1.25/1.5 倍
    每个变体都带遮罩，只比物品像素，忽略背景。
    """
    source = img.convert("RGBA")
    source.putalpha(Image.fromarray(build_mask(source).astype(np.uint8) * 255))
    w, h = source.size
    out = []

    def push(W, H):
        im = source if (W, H) == (w, h) else source.resize(
            (W, H), Image.NEAREST if W > w else Image.BOX)
        out.append((H, W, _to_gray(im), build_mask(im),
                    np.asarray(im.convert("RGB"), dtype=np.float32)))

    push(w, h)
    if not auto_scale:
        return out

    cand: list[tuple[int, int]] = []
    if abs(h - w) <= 2:
        cand += [(s, s) for s in ICON_SCALES]
    for f in (0.5, 0.65, 0.8, 1.25, 1.5):
        cand.append((max(4, int(round(w * f))), max(4, int(round(h * f)))))

    seen = {(w, h)}
    for (W, H) in cand:
        if (W, H) in seen:
            continue
        seen.add((W, H))
        push(W, H)
    return out


def load_targets(only: list[str] | None = None, auto_scale: bool = True):
    """返回 [(文件名, 原尺寸变体, 全部变体), ...]。"""
    if not os.path.isdir(TARGET_DIR):
        os.makedirs(TARGET_DIR, exist_ok=True)
        return []
    out = []
    for p in sorted(glob.glob(os.path.join(TARGET_DIR, "*"))):
        fn = os.path.basename(p)
        if not fn.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
            continue
        if only and fn not in only:
            continue
        try:
            with Image.open(p) as source:
                img = source.convert("RGBA")
        except Exception:
            continue
        var = build_variants(img, auto_scale)
        dup = any(np.array_equal(nat[3], var[0][3])
                  and np.array_equal(nat[4], var[0][4]) for _n, nat, _v in out)
        if dup:
            continue
        out.append((fn, var[0], var))
    return out


def scan(gray: np.ndarray, targets, thr: float, rgb=None):
    """
    两级扫描：先用原尺寸（快路径），没命中再试缩放变体（兼容不同 GUI 缩放）。
    返回 (score, name, (x,y), (h,w))
    """
    best = (-1.0, "", (0, 0), (0, 0))
    image = gray if rgb is None else rgb
    channel = 2 if rgb is None else 4
    cache = {}
    for name, native, _var in targets:
        if native[0] > image.shape[0] or native[1] > image.shape[1]:
            continue
        s, pos = match(image, native[channel], native[3], cache=cache)
        if s > best[0]:
            best = (s, name, pos, (native[0], native[1]))
    if best[0] >= thr:
        return best
    for name, _native, var in targets:
        for variant in var[1:]:
            h, w = variant[:2]
            if h > image.shape[0] or w > image.shape[1]:
                continue
            s, pos = match(image, variant[channel], variant[3], cache=cache)
            if s > best[0]:
                best = (s, name, pos, (h, w))
            if s >= thr:
                return best
    return best


# --------------------------------------------------------------------------
# 框选 / 裁剪
# --------------------------------------------------------------------------
def pick_on_screen(hint: str = "", countdown: int = 2) -> list[int]:
    """全屏半透明遮罩，拖矩形。返回物理像素 bbox。"""
    import tkinter as tk

    if countdown:
        for i in range(countdown, 0, -1):
            print(f"\r  {i} 秒后打开框选窗口，请切到游戏界面 ...", end="", flush=True)
            time.sleep(1)
        print()

    root = tk.Tk()
    root.title("框选区域")
    root.attributes("-topmost", True)
    try:
        root.attributes("-fullscreen", True)
    except Exception:
        root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")
    try:
        root.attributes("-alpha", 0.30)  # 太透会看不见，太实会挡游戏
    except Exception:
        pass
    root.configure(bg="#101010", cursor="crosshair")

    cv = tk.Canvas(root, bg="#101010", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    cv.create_text(sw // 2, 40, anchor="n", fill="#00ff88", font=("Microsoft YaHei", 22),
                   text=hint or "拖一个矩形框住目标区域")
    cv.create_text(sw // 2, 84, anchor="n", fill="#cccccc", font=("Microsoft YaHei", 13),
                   text="按住左键拖拽 · 松开确认 · 按 Esc 取消")

    # 有些系统上全屏 + 透明窗口会被压在游戏后面，强制拉到最前
    def _raise():
        try:
            root.lift()
            root.focus_force()
            root.attributes("-topmost", True)
        except Exception:
            pass

    root.after(50, _raise)
    root.after(400, _raise)

    st = {"x0": 0, "y0": 0, "rect": None, "box": None}

    def down(e):
        st["x0"], st["y0"] = e.x, e.y
        st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ff88", width=2)

    def move(e):
        if st["rect"]:
            cv.coords(st["rect"], st["x0"], st["y0"], e.x, e.y)

    def up(e):
        x0, y0, x1, y1 = st["x0"], st["y0"], e.x, e.y
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        st["box"] = (x0, y0, x1, y1)
        root.quit()

    cv.bind("<Button-1>", down)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", up)
    root.bind("<Escape>", lambda e: (st.update(box=None), root.quit()))
    root.mainloop()
    root.destroy()

    if not st["box"]:
        return []
    x0, y0, x1, y1 = st["box"]
    if x1 - x0 < 4 or y1 - y0 < 4:
        return []
    return [int(x0), int(y0), int(x1), int(y1)]


def pick_by_screenshot(hint: str = "", countdown: int = 2) -> list[int]:
    """
    截取游戏客户区，在普通窗口里框选，再换算回屏幕坐标。
    """
    if countdown:
        for i in range(countdown, 0, -1):
            print(f"\r  {i} 秒后截屏，请切到游戏界面 ...", end="", flush=True)
            time.sleep(1)
        print()
    img, (ox, oy) = capture_game()
    tmp = os.path.join(HERE, "_pick_tmp.png")
    img.save(tmp)
    log(f"已截屏 {img.size[0]}x{img.size[1]}，在弹出的窗口里拖框")
    box = pick_on_image(tmp, hint)
    return [box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy] if box else []


def pick_on_image(path: str, hint: str = "") -> list[int]:
    import tkinter as tk
    from PIL import ImageTk

    img = Image.open(path).convert("RGB")
    W, H = img.size
    s = min(1.0, 1500 / W, 850 / H)
    disp = img.resize((int(W * s), int(H * s)), Image.LANCZOS) if s < 1 else img

    root = tk.Tk()
    root.title(f"{os.path.basename(path)}  {W}x{H}  缩放{s:.2f}")
    tkimg = ImageTk.PhotoImage(disp)
    cv = tk.Canvas(root, width=disp.size[0], height=disp.size[1],
                   highlightthickness=0, cursor="crosshair")
    cv.pack()
    cv.create_image(0, 0, anchor="nw", image=tkimg)
    cv.create_text(8, 8, anchor="nw", fill="#00ff88", font=("Consolas", 11),
                   text=hint or "拖矩形框住单个物品图标 → Enter 确认 / Esc 取消")
    root.lift()
    root.focus_force()
    root.attributes("-topmost", True)
    st = {"x0": 0, "y0": 0, "rect": None, "box": None}

    def down(e):
        st["x0"], st["y0"] = e.x, e.y
        st["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3366", width=2)

    def move(e):
        if st["rect"]:
            cv.coords(st["rect"], st["x0"], st["y0"], e.x, e.y)

    def up(e):
        x0, y0, x1, y1 = st["x0"], st["y0"], e.x, e.y
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        st["box"] = (x0, y0, x1, y1)

    cv.bind("<Button-1>", down)
    cv.bind("<B1-Motion>", move)
    cv.bind("<ButtonRelease-1>", up)
    root.bind("<Return>", lambda e: root.quit())
    root.bind("<Escape>", lambda e: (st.update(box=None), root.quit()))
    root.mainloop()
    root.destroy()
    if not st["box"]:
        return []
    x0, y0, x1, y1 = st["box"]
    return [int(x0 / s), int(y0 / s), int(x1 / s), int(y1 / s)]


# --------------------------------------------------------------------------
# GUI 指纹（防点穿）
# --------------------------------------------------------------------------
def cmd_guard(args) -> None:
    cfg = load_cfg()
    log("拖一个矩形，框住 GUI 上「只在嬗变台界面里才有」的一小块（例如边框一角，约 20x20）")
    if getattr(args, "shot", False):
        box = pick_by_screenshot("框住 GUI 特征区域（约 20x20）")
    else:
        box = pick_on_screen("框住 GUI 特征区域（约 20x20）")
    if not box:
        log("取消")
        return
    _, img = grab(tuple(box))
    img.convert("L").save(os.path.join(HERE, "guard.png"))
    cfg["guard"] = box
    save_cfg(cfg)
    log(f"GUI 指纹已保存 {box}")
    log("跑的时候每轮先核对它：界面关了/被挡住就立刻停，不会点穿到世界里")


def check_guard(cfg: dict) -> bool:
    b = cfg.get("guard")
    if not b or b[2] <= b[0]:
        return True
    p = os.path.join(HERE, "guard.png")
    if not os.path.exists(p):
        return True
    tpl = _to_gray(Image.open(p))
    gray, _ = grab(tuple(b))
    if gray.shape != tpl.shape:
        return False
    s, _ = match(gray, tpl)
    return s >= float(cfg.get("guard_threshold", 0.85))


# --------------------------------------------------------------------------
# 录制 / 回放
# --------------------------------------------------------------------------
def cmd_record(args) -> None:
    os.makedirs(MACRO_DIR, exist_ok=True)
    events, mark = record_events(load_cfg())
    if not events:
        log("没录到事件，放弃")
        return
    a = [e for e in events if mark is None or e[1] <= mark]
    b = [e for e in events if mark is not None and e[1] > mark]
    if b:
        offset = b[0][1]
        for event in b:
            event[1] = round(event[1] - offset, 4)
    path = os.path.join(MACRO_DIR, f"{args.name}.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"spin": a, "clear": b, "has_marker": mark is not None}, stream)
    log(f"已保存 {path}")


def record_events(cfg, on_log=log, cancel=None):
    from pynput import keyboard, mouse
    import threading
    cancel = cancel or threading.Event()
    events: list[list] = []
    st = {"rec": False, "t0": 0.0, "mark": None, "done": False}
    tracker = ChordTracker({name: hk(cfg, name)
                            for name in ("record_start", "record_mark", "record_stop")})
    lock = threading.RLock()
    pending = {}
    ignored = set()

    def control(identity, token, pressed):
        with lock:
            actions = tracker.update(identity, token, pressed)
            for action in actions:
                for held_identity, held_token in tracker.held.items():
                    if held_token in tracker.bindings[action]:
                        ignored.add(held_identity)
                        previous = pending.pop(held_identity, None)
                        if previous is not None:
                            events.remove(previous)
                if action == "record_start" and not st["rec"]:
                    st["rec"] = True
                    st["t0"] = time.perf_counter()
                    on_log("开始录制")
                elif action == "record_mark" and st["rec"] and st["mark"] is None:
                    st["mark"] = ts()
                    on_log(f"结果打点 {st['mark']:.2f}s")
                elif action == "record_stop":
                    st["rec"] = False
                    st["done"] = True
            return bool(actions)

    def record_button(identity, event, pressed):
        if identity in ignored:
            if not pressed:
                ignored.discard(identity)
            return
        if not st["rec"]:
            return
        if pressed:
            pending[identity] = event
            events.append(event)
        elif identity in pending:
            pending.pop(identity)
            events.append(event)

    def ts():
        return round(time.perf_counter() - st["t0"], 4)

    def on_move(x, y):
        if st["rec"]:
            events.append(["m", ts(), int(x), int(y)])

    def on_click(x, y, button, pressed):
        with lock:
            identity = ("mouse", button)
            control(identity, f"mouse_{button.name}", pressed)
            record_button(identity, ["c", ts(), int(x), int(y), button.name, bool(pressed)], pressed)

    def on_scroll(x, y, dx, dy):
        if st["rec"]:
            events.append(["s", ts(), int(x), int(y), int(dx), int(dy)])

    def keyname(key):
        try:
            return key.name, "k"
        except AttributeError:
            char = getattr(key, "char", None)
            if char and ord(char) >= 32:
                return char, "c"
            return getattr(key, "vk", None), "v"

    def on_press(key):
        with lock:
            name, kind = keyname(key)
            identity = key_identity(key)
            control(identity, key_token(key), True)
            record_button(identity, ["k", ts(), name, kind, True], True)

    def on_release(key):
        with lock:
            identity = key_identity(key)
            control(identity, key_token(key), False)
            name, kind = keyname(key)
            record_button(identity, ["k", ts(), name, kind, False], False)

    on_log(f"等待 {hk(cfg, 'record_start').upper()} 开始录制")
    with mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll) as ml, \
            keyboard.Listener(on_press=on_press, on_release=on_release) as kl:
        while not st["done"] and not cancel.is_set():
            time.sleep(0.05)
    return events, st["mark"]


def load_macro(name: str):
    p = os.path.join(MACRO_DIR, f"{name}.json")
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def play_events(events, state, speed: float = 1.0) -> bool:
    """回放。返回 False 表示被停止。state: {"stop":bool,"paused":bool}"""
    from pynput import keyboard, mouse

    m, k = mouse.Controller(), keyboard.Controller()
    held = []
    t_prev = 0.0
    try:
        for e in events:
            if state["stop"]:
                return False
            while state["paused"]:
                if state["stop"]:
                    return False
                time.sleep(0.05)
            wait = (e[1] - t_prev) / max(speed, 1e-6)
            if wait > 0:
                end = time.perf_counter() + wait
                while time.perf_counter() < end:
                    if state["stop"]:
                        return False
                    time.sleep(min(0.01, max(0.0, end - time.perf_counter())))
            t_prev = e[1]
            kind = e[0]
            if kind == "m":
                m.position = (e[2], e[3])
            elif kind == "c":
                m.position = (e[2], e[3])
                btn = getattr(mouse.Button, e[4], mouse.Button.left)
                (m.press if e[5] else m.release)(btn)
            elif kind == "s":
                m.position = (e[2], e[3])
                m.scroll(e[4], e[5])
            elif kind == "k":
                if e[3] == "c":
                    key = keyboard.KeyCode.from_char(e[2])
                elif e[3] == "v":
                    key = keyboard.KeyCode.from_vk(e[2])
                else:
                    key = getattr(keyboard.Key, e[2])
                if e[4]:
                    k.press(key)
                    held.append(key)
                else:
                    k.release(key)
                    if key in held:
                        held.remove(key)
    finally:
        for key in held:
            try:
                k.release(key)
            except Exception:
                pass
    return True


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
def _hotkey_listener(state: dict, cfg=None):
    cfg = cfg or load_cfg()
    def action(name):
        if name == "pause":
            state["paused"] = not state["paused"]
            log("已暂停" if state["paused"] else "继续")
        elif name == "stop":
            state["stop"] = True
    return InputListener({name: hk(cfg, name) for name in ("pause", "stop")}, action).start()


def click_at(x: int, y: int, shift: bool = False) -> None:
    from pynput import keyboard, mouse
    m, k = mouse.Controller(), keyboard.Controller()
    m.position = (int(x), int(y))
    try:
        if shift:
            k.press(keyboard.Key.shift)
            time.sleep(0.03)
        m.press(mouse.Button.left)
        time.sleep(0.05)
    finally:
        m.release(mouse.Button.left)
        if shift:
            k.release(keyboard.Key.shift)


def grab_patch(cx: int, cy: int, half: int = 36):
    """以屏幕坐标 (cx,cy) 为中心，只读取游戏窗口内的小块。"""
    x0, y0 = int(cx - half), int(cy - half)
    return capture_game((x0, y0, x0 + 2 * half, y0 + 2 * half))


def patch_changed(a_img, b_img, thr: float = 4.0) -> bool:
    """两张小块图是否明显不同（判断刷新有没有生效）。"""
    a = np.asarray(a_img.convert("L"), dtype=np.float32)
    b = np.asarray(b_img.convert("L"), dtype=np.float32)
    if a.shape != b.shape:
        return True
    return float(np.abs(a - b).mean()) > thr


def rgb_score_at(rgb, tpl_rgb, mask, x: int, y: int) -> float:
    """在已定位的 (x,y) 处，用 RGB 三通道算相似度 —— 颜色是区分物品的关键。"""
    h, w = mask.shape
    sub = rgb[y:y + h, x:x + w]
    if sub.shape[0] != h or sub.shape[1] != w:
        return 0.0
    idx = None
    if mask is not None and not bool(mask.all()):
        flat = np.flatnonzero(mask.ravel())
        if len(flat) >= 20:
            idx = (flat[:, None] * 3 + np.arange(3)).ravel()
    a = tpl_rgb.ravel().astype(np.float32)
    b = sub.ravel().astype(np.float32)
    if idx is not None:
        a, b = a[idx], b[idx]
    a = a - a.mean()
    b = b - b.mean()
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return float((a * b).sum() / (na * nb))


def scan_at_slots(targets, slots, thr: float, half: int = 48, *, frames=None):
    """
    每个槽位只截一次，用 RGB 同时定位和打分，原尺寸未命中再试全部可用尺寸。
    """
    best = (-1.0, "", (0, 0), (0, 0))
    if frames is None:
        frames = (grab_patch(cx, cy, half) for cx, cy in slots)
    for img, (ox, oy) in frames:
        gray = np.asarray(img.convert("L"), dtype=np.float32)
        rgb = np.asarray(img, dtype=np.float32)
        score, name, pos, size = scan(gray, targets, thr, rgb=rgb)
        if score > best[0]:
            best = (score, name, (ox + pos[0], oy + pos[1]), size)
        if score >= thr:
            return best
    return best


def capture_slots(n: int = 3, on_log=log, key_name: str = "space") -> list:
    """
    悬停 + 按打点键，从上到下记录左侧三个候选物品中心。
    """
    from pynput import mouse
    pos: list = []
    done = {"v": False}

    def action(name):
        if name == "cancel":
            done["v"] = True
            pos.clear()
            return
        p = mouse.Controller().position
        pos.append([int(p[0]), int(p[1])])
        on_log(f"  ✓ 第 {len(pos)} 个：({int(p[0])}, {int(p[1])}) —— 继续点下一个，或按 Enter 结束")
        if n and len(pos) >= n:
            done["v"] = True

    on_log(f"从上到下把鼠标移到左侧三个候选物品上按 {key_name.upper()}，Esc 取消")
    bindings = {"mark": key_name}
    if key_name.lower() != "esc":
        bindings["cancel"] = "esc"
    with InputListener(bindings, action):
        while not done["v"]:
            time.sleep(0.05)
    return pos


def run_auto(cfg, targets, state, opts, on_log=log, on_progress=None) -> dict:
    """
    接手已经投料、刷新过的界面，先检查左侧三个候选。
    命中就左键选择并停止，否则选择循环候选以转化并重抽。
    """
    slots = get_slots(cfg)
    thr = float(opts.get("threshold", cfg["threshold"]))
    passes = int(opts.get("passes", 0))
    loop_slot = int(opts.get("loop_slot", 0))
    settle = max(0.05, float(cfg.get("settle", 0.15)))
    dry = bool(opts.get("dry", False))
    if len(slots) != 3:
        on_log("请先从上到下标定左侧三个候选物品")
        return {"rounds": 0, "hits": 0, "elapsed": 0, "stopped": True}

    i = hits = 0
    stagnant = 0
    before = None
    t0 = time.time()
    while not state["stop"] and (passes <= 0 or i < passes):
        i += 1
        while state["paused"] and not state["stop"]:
            before = None
            time.sleep(0.05)
        if state["stop"]:
            break
        ok, why = game_ready()
        if not ok:
            on_log(f"！{why} → 立即停止（避免点到别的窗口）")
            break
        if not check_guard(cfg):
            on_log("！GUI 指纹对不上（界面关了/被挡住）→ 停止")
            break

        current_slots = get_slots(cfg)
        if current_slots != slots:
            before = None
        slots = current_slots
        if before is None:
            before = [grab_patch(cx, cy, 48) for cx, cy in slots]
        r = scan_at_slots(targets, slots, thr, frames=before)
        if state["stop"] or state["paused"]:
            before = None
            continue
        ok, why = game_ready()
        if not ok:
            on_log(f"！{why} → 停止")
            break

        if r[0] >= thr:
            h, w = r[3]
            cx = r[2][0] + w // 2
            cy = r[2][1] + h // 2
            on_log(f"[{i}] ★ 命中 {r[1]} score={r[0]:.3f} → 点它")
            if not dry:
                click_at(cx, cy)
            hits += 1
            if on_progress:
                on_progress(i, hits, 0.0, r[0], r[1])
            if not dry:
                success_sound(cfg)
            break

        if on_progress:
            el = time.time() - t0
            on_progress(i, hits, i / max(el, 1e-6), r[0], r[1])
        if dry:
            before = None
            time.sleep(settle)
            continue
        if passes > 0 and i >= passes:
            break

        # Selecting a candidate replaces the input stack and rerolls all three results.
        sx, sy = get_refill(cfg) or slots[loop_slot % 3]
        click_at(sx, sy)
        time.sleep(settle)

        changed = False
        deadline = time.time() + max(0.6, settle * 4)
        while time.time() < deadline and not state["stop"] and not state["paused"]:
            now = [grab_patch(cx, cy, 48) for cx, cy in slots]
            if any(patch_changed(old[0], new[0]) for old, new in zip(before, now)):
                changed = True
                break
            time.sleep(0.04)

        if changed:
            stagnant = 0
            before = now
            continue

        before = None
        stagnant += 1
        if stagnant >= 4:
            on_log("！连续几轮画面都没变化 → 停止")
            on_log("  常见原因：① 经验不足（每次转化要 3 级）"
                   " ② 刷新按钮没标对 ③ 点到了别的地方")
            break

    return {"rounds": i, "hits": hits, "elapsed": time.time() - t0, "stopped": bool(state["stop"])}


def cmd_slots(args) -> None:
    cfg = load_cfg()
    log("标定结果槽：把鼠标移到每个槽位中心，按一下空格。")
    pos = capture_slots(args.count)
    if len(pos) != 3:
        log("标定已取消，保留原来的三个候选坐标")
        return
    save_slots(cfg, pos)
    log(f"✓ 已保存 {len(pos)} 个槽位")


def run_loop(cfg, targets, macro, state, opts, on_log=log, on_progress=None) -> dict:
    """
    核心循环，CLI 和 GUI 共用。
    state: {"stop": bool, "paused": bool}
    opts : threshold / speed / passes / want / keep_going / dry
    """
    watch = tuple(get_watch(cfg))
    thr = float(opts.get("threshold", cfg["threshold"]))
    speed = float(opts.get("speed", cfg.get("speed", 1.3)))
    passes = int(opts.get("passes", 0))
    want = int(opts.get("want", 0))
    keep = bool(opts.get("keep_going", False))
    dry = bool(opts.get("dry", False))

    i = hits = 0
    t0 = time.time()
    while not state["stop"] and (passes <= 0 or i < passes):
        i += 1
        while state["paused"] and not state["stop"]:
            time.sleep(0.05)
        if state["stop"]:
            break

        ok, why = game_ready()
        if not ok:
            on_log(f"！{why} → 立即停止（避免点到别的窗口）")
            break
        if not check_guard(cfg):
            on_log("！GUI 指纹对不上（界面关了/被挡住）→ 停止")
            break

        if not play_events(macro["spin"], state, speed):
            break

        time.sleep(float(cfg.get("settle", 0.15)))
        best = (-1.0, "", (0, 0), (0, 0))
        for _ in range(int(cfg.get("scan_tries", 2))):
            gray, img = grab(watch)
            if not is_blank(gray):
                r = scan(gray, targets, thr,
                         rgb=np.asarray(img.convert("RGB"), dtype=np.float32))
                if r[0] > best[0]:
                    best = r
            if best[0] >= thr:
                break
            time.sleep(float(cfg.get("scan_interval", 0.06)))

        if best[0] >= thr:
            hits += 1
            on_log(f"[{i}] ★ 命中 {best[1]}  score={best[0]:.3f}")
            if not dry:
                _take(cfg, watch, best)
                success_sound(cfg)
            if on_progress:
                on_progress(i, hits, 0.0, best[0], best[1])
            if want and hits >= want:
                on_log(f"已刷到 {hits} 个，收工")
                break
            if cfg.get("stop_on_hit", True) and not keep:
                on_log("刷取成功 → 自动停止")
                break
            time.sleep(0.35)
            continue

        el = time.time() - t0
        if on_progress:
            on_progress(i, hits, i / max(el, 1e-6), best[0], best[1])
        if macro.get("clear"):
            if not play_events(macro["clear"], state, speed):
                break
        time.sleep(float(cfg.get("loop_delay", 0.05)))

    return {"rounds": i, "hits": hits, "elapsed": time.time() - t0,
            "stopped": bool(state["stop"])}


def cmd_run(args) -> None:
    cfg = load_cfg()
    for k in ("threshold", "settle", "speed", "scan_interval", "scan_tries", "loop_delay"):
        v = getattr(args, k, None)
        if v is not None:
            cfg[k] = v

    targets = load_targets(args.targets, auto_scale=not args.no_autoscale)
    if not targets:
        log("targets/ 里没有图片。先：python mcb.py add 你的图.png")
        return
    b = get_watch(cfg)
    auto = bool(getattr(args, "auto", False)) and bool(cfg.get("slots"))
    if not auto and (b[2] <= b[0] or b[3] <= b[1]):
        log("还没框槽位。先：python mcb.py region")
        return
    macro = load_macro("loop")
    if not auto and (not macro or not macro.get("spin")):
        log("还没录循环。先：python mcb.py record --name loop（或用 --auto 自动模式）")
        return

    if not args.no_focus and not focus_window(cfg.get("window_title", "")):
        log("没找到 java 游戏窗口，3 秒后继续")
        time.sleep(3)

    state = {"stop": False, "paused": False}
    kl = _hotkey_listener(state, cfg)
    log(f"开始 | 阈值 {cfg['threshold']:.2f} | 倍速 {cfg.get('speed', 1.3)} | "
        f"模板 {[t[0] for t in targets]} | {hk(cfg, 'pause').upper()} 暂停 / "
        f"{hk(cfg, 'stop').upper()} 停止")

    def prog(i, hits, rate, score, name):
        print(f"  第 {i} 轮 | {rate:.2f} 轮/秒 | 最高分 {score:.3f} {name[:16]}", flush=True)


    try:
        if auto:
            log(f"自动模式 | 槽位 {cfg['slots']} | 阈值 {cfg['threshold']:.2f}")
            res = run_auto(cfg, targets, state, {
                "threshold": cfg["threshold"], "passes": args.passes,
                "dry": args.dry, "loop_slot": getattr(args, "loop_slot", 0),
            }, on_progress=prog)
        else:
            res = run_loop(cfg, targets, macro, state, {
                "threshold": cfg["threshold"], "speed": cfg.get("speed", 1.3),
                "passes": args.passes, "want": args.want,
                "keep_going": args.keep_going, "dry": args.dry,
            }, on_progress=prog)
    except KeyboardInterrupt:
        log("Ctrl+C 中断")
        res = {"rounds": 0, "hits": 0, "elapsed": 0}
    finally:
        kl.stop()
        try:
            from pynput import keyboard as kb
            kc = kb.Controller()
            for key in (kb.Key.shift, kb.Key.ctrl, kb.Key.alt):
                kc.release(key)
        except Exception:
            pass
        print()
    log(f"结束：{res['rounds']} 轮 / 命中 {res['hits']} 次 / 用时 {res['elapsed']:.1f}s")


def _take(cfg, b, best) -> None:
    """收走命中的物品：默认按住 shift 左键（整组进背包）。"""
    from pynput import keyboard, mouse

    _, name, (x, y), (h, w) = best
    sx = b[0] + x + w // 2
    sy = b[1] + y + h // 2
    mc, kc = mouse.Controller(), keyboard.Controller()
    mc.position = (sx, sy)
    kc.press(keyboard.Key.shift)
    time.sleep(0.04)
    mc.click(mouse.Button.left)
    time.sleep(0.04)
    kc.release(keyboard.Key.shift)


# --------------------------------------------------------------------------
# 一条龙向导
# --------------------------------------------------------------------------
def cmd_quick(args) -> None:
    cfg = load_cfg()
    log("=" * 56)
    log(f" {APP_NAME} · 向导")
    log("=" * 56)

    if not args.skip_region or cfg["watch"][2] <= cfg["watch"][0]:
        log("步骤 1/3：把嬗变台界面打开，然后拖一个框，只框住「掉落槽」那几格")
        box = (pick_by_screenshot if args.shot else pick_on_screen)("框住掉落槽区域（1~3 格）")
        if not box:
            log("取消")
            return
        cfg["watch"] = box
        save_cfg(cfg)
        log(f"  ✓ 槽位区域 {box}")

    if not args.skip_image:
        log("步骤 2/3：现在截一张槽位图，然后从里面裁出目标物品")
        _, img = grab(tuple(cfg["watch"]))
        tmp = os.path.join(HERE, "debug_watch.png")
        img.save(tmp)
        log(f"  已截图 → {tmp}")
        log("  在弹出窗口里拖一个矩形，只框住那颗物品图标，然后按 Enter")
        box = pick_on_image(tmp)
        if box:
            name = args.name or "target"
            if not name.lower().endswith(".png"):
                name += ".png"
            os.makedirs(TARGET_DIR, exist_ok=True)
            Image.open(tmp).convert("RGB").crop(tuple(box)).save(os.path.join(TARGET_DIR, name))
            log(f"  ✓ 目标模板 targets/{name}")
        else:
            log("  （跳过了裁图，用已有的 targets/）")

    if not load_macro("loop") or args.re_record:
        log("步骤 3/3：录一遍循环")
        log(f"  按 {hk(cfg, 'record_start').upper()} 开始 → 做一遍"
            f"（放物品/转/结果出现时按 {hk(cfg, 'record_mark').upper()}）"
            f"→ 按 {hk(cfg, 'record_stop').upper()} 结束")
        ns = argparse.Namespace(name="loop")
        cmd_record(ns)
    else:
        log("步骤 3/3：已有循环宏，跳过（要重录加 --re-record）")

    if not cfg.get("guard") and args.with_guard:
        log("附加：框一块 GUI 特征区域防点穿")
        cmd_guard(None)

    log(f"准备就绪，3 秒后开刷（{hk(cfg, 'pause').upper()} 暂停 / "
        f"{hk(cfg, 'stop').upper()} 停止）")
    time.sleep(3)
    run_args = argparse.Namespace(
        passes=args.passes, threshold=args.threshold, settle=None, speed=args.speed,
        scan_interval=None, scan_tries=None, loop_delay=None,
        targets=None, no_autoscale=False, keep_going=False, dry=False,
        no_focus=False, yes=True, want=args.want,
    )
    cmd_run(run_args)


# --------------------------------------------------------------------------
def cmd_doctor(args) -> None:
    """体检：把环境、依赖、状态全打出来，出问题直接看这里。"""
    lines: list[str] = []

    def p(s: str = "") -> None:
        print(s)
        lines.append(s)

    p("=" * 60)
    p(f" {APP_NAME} · 环境体检")
    p("=" * 60)
    p(f"Python      : {sys.executable}")
    p(f"版本        : {sys.version.split()[0]}  ({'64' if sys.maxsize > 2**32 else '32'} 位)")
    p(f"脚本目录    : {HERE}")

    try:
        import numpy
        p(f"numpy       : {numpy.__version__}  ✓")
    except Exception as e:
        p(f"numpy       : ✗ {e}")
    try:
        import PIL
        p(f"pillow      : {PIL.__version__}  ✓")
    except Exception as e:
        p(f"pillow      : ✗ {e}")
    try:
        import pynput
        p(f"pynput      : {getattr(pynput, '__version__', 'ok')}  ✓")
    except Exception as e:
        p(f"pynput      : ✗ {e}   ← pip install pynput")
    try:
        import tkinter
        p(f"tkinter     : {tkinter.TkVersion}  ✓")
    except Exception as e:
        p(f"tkinter     : ✗ {e}   ← 重装 python 时勾选 tcl/tk")

    try:
        u = ctypes.windll.user32
        p(f"屏幕        : {u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)} (系统)")
    except Exception as e:
        p(f"屏幕        : ✗ {e}")

    cfg = load_cfg()
    p("")
    p("—— 状态 ——")
    w = cfg.get("watch", [0, 0, 0, 0])
    if w[2] > w[0]:
        p(f"槽位区域    : {w}  ({w[2]-w[0]}x{w[3]-w[1]})  ✓")
    else:
        p("槽位区域    : 未设置  ← 跑 region 或 quick")
    tg = load_targets()
    if tg:
        p(f"目标模板    : {len(tg)} 个 → {', '.join(t[0] for t in tg)}  ✓")
    else:
        p("目标模板    : 空  ← 跑 add 或 crop")
    m = load_macro("loop")
    if m and m.get("spin"):
        p(f"循环宏      : ✓  spin {len(m['spin'])} 事件 / clear {len(m.get('clear', []))} 事件")
    else:
        p("循环宏      : 无  ← 跑 record --name loop")
    p(f"GUI 指纹    : {'✓ 已设置' if cfg.get('guard') else '未设置（可选，但建议做）'}")

    p("")
    p("—— 游戏窗口 ——")
    wins = [t for t in list_windows() if t.strip()]
    hwnd = find_game_window(cfg.get("window_title", ""))
    if hwnd:
        r = wt.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        b = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, b, n + 1)
        p(f"  ✓ 游戏窗口: {b.value[:50]}")
        p(f"    进程 {_win_exe(hwnd)}  位置 ({r.left},{r.top})  {r.right-r.left}x{r.bottom-r.top}")
    else:
        p("  ✗ 没找到 java 游戏窗口（游戏没开？）")
        for t in wins[:6]:
            p(f"     - {t[:60]}")
    if any("magpie" in t.lower() for t in wins):
        p("  ！检测到 Magpie（窗口放大工具）。它会盖住全屏遮罩，")
        p("    框选时如果什么都看不见，请改用：python mcb.py region --shot")

    p("")
    p("=" * 60)
    p(" 结论")
    p("=" * 60)
    if not tg:
        p(" 下一步：python mcb.py quick")
    elif w[2] <= w[0]:
        p(" 下一步：python mcb.py region")
    elif not m:
        p(" 下一步：python mcb.py record --name loop")
    else:
        p(" 配置齐全，可以直接：python mcb.py run --dry 先空跑看看")

    out = os.path.join(HERE, "doctor.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    p(f"\n体检报告已存到 {out}（出问题把这个文件发我）")


def cmd_snap(args) -> None:
    """游戏客户区截图存盘，供标定/排查用。"""
    img, _origin = capture_game()
    p = os.path.join(HERE, args.out or "screen.png")
    img.save(p)
    log(f"✓ {p}  ({img.size[0]}x{img.size[1]})")


def cmd_setregion(args) -> None:
    """直接写死槽位坐标，跳过框选。"""
    cfg = load_cfg()
    cfg["watch"] = [args.x1, args.y1, args.x2, args.y2]
    save_cfg(cfg)
    w, h = args.x2 - args.x1, args.y2 - args.y1
    log(f"✓ 槽位区域 = {cfg['watch']}  ({w}x{h})")


def cmd_region(args) -> None:
    cfg = load_cfg()
    log("拖一个矩形，框住「结果/掉落槽」那一小块")
    if getattr(args, "shot", False):
        box = pick_by_screenshot("框住掉落槽区域")
    else:
        box = pick_on_screen("框住掉落槽区域")
    if not box:
        log("取消")
        return
    save_watch(cfg, box)
    w, h = box[2] - box[0], box[3] - box[1]
    log(f"✓ watch 区域 = {box}  ({w}x{h})")


def cmd_shot(args) -> None:
    cfg = load_cfg()
    if args.full:
        img, _origin = capture_game()
        out = os.path.join(HERE, "debug_full.png")
    else:
        b = cfg["watch"]
        if b[2] <= b[0]:
            log("还没设 watch 区域，先跑 region")
            return
        _, img = grab(tuple(b))
        out = os.path.join(HERE, "debug_watch.png")
    img.save(out)
    log(f"✓ {out}  ({img.size[0]}x{img.size[1]})")


def cmd_add(args) -> None:
    os.makedirs(TARGET_DIR, exist_ok=True)
    src = args.image
    if not os.path.exists(src):
        log(f"找不到 {src}")
        return
    name = args.name or os.path.splitext(os.path.basename(src))[0]
    if not name.lower().endswith(".png"):
        name += ".png"
    dst = os.path.join(TARGET_DIR, name)
    shutil.copyfile(src, dst)
    img = Image.open(dst)
    log(f"✓ 已加入 targets/{name}  ({img.size[0]}x{img.size[1]})")
    log(f"  会自动适配 GUI 缩放（{', '.join(str(s) for s in ICON_SCALES)} px）")


def cmd_crop(args) -> None:
    src = args.image
    if not os.path.exists(src):
        log(f"找不到 {src}")
        return
    box = pick_on_image(src)
    if not box:
        log("取消")
        return
    img = autotrim(Image.open(src).convert("RGB").crop(tuple(box)))
    os.makedirs(TARGET_DIR, exist_ok=True)
    name = args.name or (os.path.splitext(os.path.basename(src))[0] + "_target")
    if not name.lower().endswith(".png"):
        name += ".png"
    img.save(os.path.join(TARGET_DIR, name))
    log(f"✓ targets/{name}  ({img.size[0]}x{img.size[1]})")


def cmd_watch(args) -> None:
    cfg = load_cfg()
    targets = load_targets()
    if not targets:
        log("targets/ 里没有图片，先 add 或 crop")
        return
    b = get_watch(cfg)
    if b[2] <= b[0]:
        log("先跑 region")
        return
    state = {"stop": False}

    def action(name):
        state["stop"] = True

    kl = InputListener({"stop": hk(cfg, "stop")}, action).start()
    log(f"实时匹配（{len(targets)} 个模板，阈值 {cfg['threshold']}），"
        f"{hk(cfg, 'stop').upper()} 退出")
    try:
        while not state["stop"]:
            gray, img = grab(tuple(b))
            if is_blank(gray):
                txt = "（空槽）"
            else:
                s, name, _, _ = scan(gray, targets, cfg["threshold"],
                                     rgb=np.asarray(img.convert("RGB"), dtype=np.float32))
                txt = f"{name}: {s:.3f}"
            print("\r" + txt.ljust(50), end="", flush=True)
            time.sleep(0.1)
    finally:
        kl.stop()
        print()


def cmd_selftest(args) -> None:
    log("合成槽位图，验证匹配算法...")
    rng = np.random.default_rng(0)
    patch = np.full((64, 64, 3), 0x8B, dtype=np.uint8)

    def icon(seed, ox, oy):
        r = np.random.default_rng(seed)
        box = r.integers(0, 255, size=(16, 16, 3)).astype(np.uint8)
        patch[oy:oy + 16, ox:ox + 16] = box
        return box

    icon(1, 8, 8)
    icon(2, 40, 40)

    g = _to_gray(Image.fromarray(patch))
    tpl = _to_gray(Image.fromarray(patch[8:24, 8:24]))
    s_hit, pos = match(g, tpl)
    log(f"  同图自匹配 : {s_hit:.3f} @ {pos}   (期望 ≈1.0)")

    other = _to_gray(Image.fromarray(rng.integers(0, 255, (16, 16, 3)).astype(np.uint8)))
    s_miss, _ = match(g, other)
    log(f"  不存在的图 : {s_miss:.3f}          (期望 <0.9)")

    noise = g + rng.normal(0, 6, g.shape).astype(np.float32)
    s_noise, _ = match(noise, tpl)
    log(f"  加噪       : {s_noise:.3f}          (期望 >0.9)")

    ok = abs(s_hit - 1.0) < 0.02 and s_miss < 0.9 and s_noise > 0.9
    log("自检通过 ✓" if ok else "自检失败 ✗")
    if not ok:
        sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description="MC 嬗变台 视觉自动刷",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)

    # 直接双击 mcb.py 时会走到这里（无参数）——给个提示并停住，别一闪而过
    if len(sys.argv) == 1:
        print("=" * 56)
        print(f" {APP_NAME}")
        print("=" * 56)
        print(" 第一次用，直接双击「启动.bat」或「run.bat」就行。")
        print("")
        print(" 命令行用法：")
        print("   python mcb.py quick        一条龙向导（推荐）")
        print("   python mcb.py doctor       环境体检")
        print("   python mcb.py run          开始刷")
        print("   python mcb.py --help       全部命令")
        print("=" * 56)
        try:
            input("按回车退出...")
        except Exception:
            pass
        return
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("quick", help="一条龙：框槽位→给图→录循环→开刷")
    p.add_argument("--name", default="target")
    p.add_argument("--passes", type=int, default=0)
    p.add_argument("--want", type=int, default=0)
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--speed", type=float, default=None)
    p.add_argument("--skip-region", action="store_true")
    p.add_argument("--skip-image", action="store_true")
    p.add_argument("--re-record", action="store_true")
    p.add_argument("--with-guard", action="store_true")
    p.add_argument("--shot", action="store_true", help="用截图方式框选（遮罩看不见时用）")

    p = sub.add_parser("snap", help="游戏窗口截图存盘（不弹窗口）")
    p.add_argument("--out", default="screen.png")
    p = sub.add_parser("setregion", help="直接写坐标，跳过框选")
    p.add_argument("x1", type=int); p.add_argument("y1", type=int)
    p.add_argument("x2", type=int); p.add_argument("y2", type=int)
    p = sub.add_parser("region", help="框出掉落槽区域")
    p.add_argument("--shot", action="store_true", help="用截图方式框选")
    p = sub.add_parser("slots", help="标定左侧三个候选物品中心")
    p.add_argument("--count", type=int, choices=[3], default=3)
    p = sub.add_parser("shot", help="截取 watch 区域")
    p.add_argument("--full", action="store_true")
    p = sub.add_parser("crop", help="从图片裁模板")
    p.add_argument("image")
    p.add_argument("--name", default=None)
    p = sub.add_parser("add", help="直接加入一张图片当目标")
    p.add_argument("image")
    p.add_argument("--name", default=None)
    p = sub.add_parser("record", help="录制宏")
    p.add_argument("--name", default="loop")
    p = sub.add_parser("guard", help="框 GUI 特征区域，防点穿")
    p.add_argument("--shot", action="store_true", help="用截图方式框选")
    sub.add_parser("doctor", help="环境体检（出问题先跑这个）")
    sub.add_parser("watch", help="实时匹配分数")
    sub.add_parser("selftest", help="自检")

    p = sub.add_parser("run", help="开始刷")
    p.add_argument("--passes", type=int, default=0)
    p.add_argument("--want", type=int, default=0, help="刷到 N 个就停")
    p.add_argument("--threshold", type=float, default=None)
    p.add_argument("--speed", type=float, default=None, help="回放倍速，默认 1.3")
    p.add_argument("--settle", type=float, default=None)
    p.add_argument("--scan-interval", dest="scan_interval", type=float, default=None)
    p.add_argument("--scan-tries", dest="scan_tries", type=int, default=None)
    p.add_argument("--loop-delay", dest="loop_delay", type=float, default=None)
    p.add_argument("--targets", nargs="*", default=None)
    p.add_argument("--no-autoscale", action="store_true", help="不做缩放自适应")
    p.add_argument("--keep-going", action="store_true", help="命中后不停")
    p.add_argument("--dry", action="store_true", help="命中也不点击")
    p.add_argument("--no-focus", action="store_true")
    p.add_argument("--auto", action="store_true", help="自动模式（需先标定槽位）")
    p.add_argument("--loop-slot", dest="loop_slot", type=int, default=0,
                   help="循环时点第几个槽（0 起）")
    p.add_argument("--yes", action="store_true")

    args = ap.parse_args()
    fn = {
        "quick": cmd_quick, "region": cmd_region, "shot": cmd_shot,
        "crop": cmd_crop, "add": cmd_add, "record": cmd_record,
        "snap": cmd_snap, "setregion": cmd_setregion,
        "slots": cmd_slots, "guard": cmd_guard, "doctor": cmd_doctor, "watch": cmd_watch, "run": cmd_run,
        "selftest": cmd_selftest,
    }[args.cmd]

    try:
        fn(args)
    except Exception:
        import traceback
        print()
        traceback.print_exc()
        print("\n出错了。把这段红字或 doctor.txt 发我。")
        try:
            input("按回车退出...")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
