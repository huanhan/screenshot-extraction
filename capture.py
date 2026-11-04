# -*- coding: utf-8 -*-
"""截图：全屏、窗口列表、指定窗口截图。不依赖界面。"""
import ctypes
import ctypes.wintypes as wt
import os
import time
from collections import namedtuple

from PIL import ImageGrab

from app_config import SHOT_DIR

user32 = ctypes.windll.user32

WindowInfo = namedtuple("WindowInfo", "hwnd title rect")   # rect = (left, top, right, bottom)
EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
DWMWA_EXTENDED_FRAME_BOUNDS = 9


def capture_screen():
    img = ImageGrab.grab(all_screens=True).convert("RGB")
    return save_shot(img, "shot")


def save_shot(img, prefix):
    os.makedirs(SHOT_DIR, exist_ok=True)
    name = time.strftime(f"{prefix}_%Y%m%d_%H%M%S_") + f"{time.time_ns() % 1000000:06d}.png"
    path = os.path.join(SHOT_DIR, name)
    img.save(path, "PNG")
    return path


def list_windows(own_hwnd=None):
    """列出可见的、有标题的顶层窗口，排除本程序自己的窗口。"""
    result = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value.strip()
        if not title or hwnd == own_hwnd:
            return True
        result.append(WindowInfo(hwnd, title, window_rect(hwnd)))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return [w for w in result if w.rect and w.rect[2] - w.rect[0] > 50 and w.rect[3] - w.rect[1] > 50]


def window_rect(hwnd):
    """优先用 DWM 真实边界（不含阴影边框），失败退回 GetWindowRect。"""
    r = wt.RECT()
    try:
        ok = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) == 0
    except OSError:
        ok = False
    if not ok and not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def capture_window(info):
    hwnd = info.hwnd
    if not user32.IsWindow(hwnd):
        raise RuntimeError("窗口已经关闭")
    if user32.IsIconic(hwnd):
        raise RuntimeError("窗口已最小化，请先还原它")
    user32.SetForegroundWindow(hwnd)     # 不调用 ShowWindow，避免改动用户窗口的大小状态
    time.sleep(0.35)
    rect = window_rect(hwnd)
    if not rect:
        raise RuntimeError("无法获取窗口位置")
    left, top, right, bottom = rect
    img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
    return save_shot(img, "win")


def window_alive(info):
    """窗口是否仍然存在。"""
    return bool(user32.IsWindow(info.hwnd))
