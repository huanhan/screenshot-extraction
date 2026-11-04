# -*- coding: utf-8 -*-
"""全局快捷键（Win32 RegisterHotKey）与粘贴时的按键模拟。不依赖界面。"""
import ctypes
import ctypes.wintypes as wt
import re
import threading
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN, VK_V = 0x11, 0x12, 0x10, 0x5B, 0x5C, 0x56
_SPECIAL_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
}

# ---------------------------------------------------------------- 快捷键

_KEYSYM_TO_NAME = {
    "space": "space", "Tab": "tab", "Return": "enter", "KP_Enter": "enter", "Escape": "esc",
    "Insert": "insert", "Delete": "delete", "Home": "home", "End": "end",
    "Prior": "pageup", "Next": "pagedown",
}


_MODIFIER_KEYSYMS = {
    "Control_L": "ctrl", "Control_R": "ctrl",
    "Shift_L": "shift", "Shift_R": "shift",
    "Alt_L": "alt", "Alt_R": "alt",
    "Win_L": "win", "Win_R": "win", "Meta_L": "win", "Meta_R": "win", "Super_L": "win", "Super_R": "win",
}
_MOD_ORDER = ["ctrl", "alt", "shift", "win"]


def hotkey_from_event(event, held):
    """把 tkinter 按键事件转成 parse_hotkey 认识的文本，如 'ctrl+shift+r'。

    修饰键不看 event.state 的位（Windows 上 tk 的位定义不可靠，会把 Ctrl 误报成 Alt），
    而是用 held：一个集合，由录制框根据修饰键的按下/松开事件维护。
    只有修饰键、或无法识别的键时返回 None（表示还没录到有效组合）。"""
    sym = event.keysym
    if sym in _MODIFIER_KEYSYMS:
        return None
    if re.fullmatch(r"[a-zA-Z0-9]", sym):
        key = sym.lower()
    elif re.fullmatch(r"F([1-9]|1[0-2])", sym):
        key = sym.lower()
    elif sym in _KEYSYM_TO_NAME:
        key = _KEYSYM_TO_NAME[sym]
    else:
        return None
    mods = [m for m in _MOD_ORDER if m in held]
    if not mods:
        return None
    return "+".join(mods + [key])


def parse_hotkey(text):
    """'ctrl+shift+r' -> (修饰键位掩码, 虚拟键码)。格式不对抛 ValueError。"""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError("快捷键不能为空")
    mods, key = 0, None
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in ("win", "windows"):
            mods |= MOD_WIN
        elif key is not None:
            raise ValueError(f"快捷键只能包含一个普通键：{text}")
        elif len(p) == 1 and p.isalnum():
            key = ord(p.upper())
        elif re.fullmatch(r"f([1-9]|1[0-2])", p):
            key = 0x70 + int(p[1:]) - 1
        elif p in _SPECIAL_KEYS:
            key = _SPECIAL_KEYS[p]
        else:
            raise ValueError(f"不认识的按键：{p}")
    if key is None:
        raise ValueError(f"快捷键需要一个普通键，例如 ctrl+shift+r：{text}")
    if mods == 0:
        raise ValueError(f"快捷键需要至少一个修饰键（Ctrl/Alt/Shift/Win）：{text}")
    return mods, key


class HotkeyThread(threading.Thread):
    """独立线程注册全局热键并跑消息循环，触发时把事件放进队列。"""

    def __init__(self, bindings, events):
        super().__init__(daemon=True)
        self.bindings = bindings
        self.events = events
        self.thread_id = None
        self.error = ""
        self.ready = threading.Event()

    def stop(self):
        """停止消息循环（线程收到 WM_QUIT 后注销热键并退出）。"""
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.join(2)

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        ids = {}
        for i, (name, (mods, key)) in enumerate(self.bindings.items(), start=1):
            if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, key):
                ids[i] = name
            else:
                err = kernel32.GetLastError()
                why = "已被其他程序占用" if err == 1409 else f"错误码 {err}"
                self.error += f"快捷键 {name} 无法注册（{why}），请在设置里换一个。  "
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                self.events.put(("hotkey_" + ids[msg.wParam],))
        for i in ids:
            user32.UnregisterHotKey(None, i)


def wait_modifiers_released(timeout=2.0):
    vks = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)
    end = time.time() + timeout
    while time.time() < end:
        if not any(user32.GetAsyncKeyState(v) & 0x8000 for v in vks):
            return True
        time.sleep(0.02)
    return False


def send_ctrl_v():
    # 超时仍按着 Alt/Shift/Win 时先强制松开，保证发出的是普通 Ctrl+V
    for vk in (VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN):
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

