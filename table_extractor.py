# -*- coding: utf-8 -*-
"""
截图表格提取工具（Windows）

数据模型：每张截图是一条记录，识别结果挂在来源截图上。
- 「截图」页：全屏/指定窗口截图，列表预览，双击放大查看。
- 「识别结果」页：结果表每行记录来源截图；点击行显示对应截图，同一张图的行用颜色成组；
  重复内容打标识。
- 识别方式：手动「识别待处理」/「重新识别」，或开启「自动识别」（攒够 N 张或每分钟检查一次）。
- 复制模式：粘贴快捷键按行优先顺序粘贴下一个未填单元格；已粘贴/已跳过/空单元格自动跳过。

发送给模型的图片先转 WebP。依赖：pip install pillow（tkinter 随 Python 自带）。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import queue
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import namedtuple
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from PIL import Image, ImageGrab, ImageTk

APP_NAME = "截图表格提取"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "ScreenshotExtract")
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
SHOT_DIR = os.path.join(APP_DIR, "shots")
LOG_PATH = os.path.join(APP_DIR, "log.txt")

REQUEST_TIMEOUT = 120      # 单次请求最长等待秒数
MAX_IMAGE_WIDTH = 2048     # 发送前缩放到的最大宽度
AUTO_INTERVAL_SEC = 60     # 自动识别：有待处理截图时，每隔这么久检查一次
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CGW_SITE = "https://img.cgwimg.com"          # cgwimg 站点（授权页与接口都在这里）
CGW_BASE_URL = CGW_SITE + "/api/v1"          # OpenAI 兼容接口地址
CGW_APP_ID = "screenshot-extraction"         # 在 cgwimg 后台登记的应用 ID
CGW_MODEL = "gpt-6.1-sol"                    # cgwimg 方式固定使用的模型
PROVIDER_CUSTOM, PROVIDER_CGW = "custom", "cgwimg"
PROVIDER_LABELS = {PROVIDER_CUSTOM: "自定义（OpenAI 兼容接口）",
                   PROVIDER_CGW: "cgwimg（授权登录，无需 Key）"}

DEFAULT_SETTINGS = {
    "recognize_hotkey": "ctrl+shift+r",   # 截图快捷键
    "paste_hotkey": "ctrl+shift+v",       # 复制模式下粘贴下一个单元格
    "provider": PROVIDER_CGW,     # custom: 自填 Base URL + API Key；cgwimg: 授权登录获取 cgw_key
    "cgw_key": "",                # cgwimg 授权后拿到的 Key（只在本机保存）
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "gpt-4o",
    "columns": "",          # 提取格式：列名，逗号分隔；留空则按截图表头
    "extra_prompt": "",     # 额外的提取要求
    "batch_size": 3,        # 每次请求最多合并几张图
    "webp_quality": 85,
}

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

# 同一张图里的行用这些颜色成组（浅色底，便于看清）
GROUP_COLORS = ["#e8f1ff", "#fff4e0", "#e9f8ee", "#f6e9ff", "#ffe9ea", "#e6f7f9"]


def log(msg):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def clamp_int(v, lo, hi, default):
    try:
        return min(max(int(v), lo), hi)
    except (TypeError, ValueError):
        return default


def one_line(text):
    """把多行文本压成一行显示，换行处用「↵」标出。"""
    return re.sub(r"[ \t]*(\r\n|\r|\n)[ \t]*", " ↵ ", str(text or "")).strip()


def parse_columns(text):
    return [c.strip() for c in re.split(r"[,，、\n]", text or "") if c.strip()]


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


# ---------------------------------------------------------------- 截图

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


# ---------------------------------------------------------------- 识别

def image_to_webp_b64(path, quality):
    with Image.open(path) as im:
        img = im.convert("RGB")
    if img.width > MAX_IMAGE_WIDTH:
        h = round(img.height * MAX_IMAGE_WIDTH / img.width)
        img = img.resize((MAX_IMAGE_WIDTH, h), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=quality, method=4)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build_prompt(columns, extra, n_images):
    if columns:
        fmt = f"每一行输出一个对象，键必须严格为这些列名（按此顺序）：{json.dumps(columns, ensure_ascii=False)}。"
    else:
        fmt = "每一行输出一个对象，键为截图表头中的列名。"
    p = (f"你是表格提取助手。下面共 {n_images} 张图片，请识别其中的表格并提取全部数据行。"
         "如果是同一张表的不同部分，相邻图片可能有重叠的行：每张图的行都要完整输出，不要去重（重复由程序标注）。"
         "每一行必须额外包含字段 \"_img\"，值为该行所在图片的编号（整数，从 1 开始）。"
         "按图片顺序和表内顺序输出。"
         + fmt +
         "单元格没有内容时填空字符串。保持原文，不要编造，不要遗漏行。"
         "只输出 JSON，格式为 {\"rows\": [ {...}, {...} ]}，不要输出其他文字或代码块标记。")
    if extra.strip():
        p += "\n额外要求：" + extra.strip()
    return p


def http_error_hint(code, body):
    body = body[:300]
    if code == 401:
        hint = "API Key 无效或已过期，请检查设置里的 API Key。"
    elif code == 403:
        hint = ("被服务端拒绝（403）。可能是：Key 没有该模型的权限、账户余额不足、"
                "或网关拦截了请求。请核对模型名称和 Key 权限。")
    elif code == 404:
        hint = "接口或模型不存在（404），请检查 Base URL 和模型名称。"
    elif code == 429:
        hint = "请求太频繁或额度用完（429），稍后再试，或减少每次发送张数。"
    else:
        hint = "接口返回错误。"
    return f"{hint}（HTTP {code}）服务端返回：{body}"


def call_vision_model(settings, images_b64):
    base, api_key = endpoint(settings)
    url = base.rstrip("/") + "/chat/completions"
    content = [{"type": "text", "text": build_prompt(parse_columns(settings["columns"]),
                                                    settings["extra_prompt"], len(images_b64))}]
    for b in images_b64:
        content.append({"type": "image_url", "image_url": {"url": "data:image/webp;base64," + b}})
    body = {"model": settings["model"].strip(), "temperature": 0,
            "messages": [{"role": "user", "content": content}]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + api_key,
                 "User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(http_error_hint(e.code, e.read().decode("utf-8", "replace")))
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络请求失败：{e.reason}")
    except TimeoutError:
        raise RuntimeError(f"请求超过 {REQUEST_TIMEOUT} 秒没有响应")
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("接口返回格式不认识：" + str(data)[:300])


def parse_table(text, columns):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("模型返回的内容不是 JSON：" + text[:200])
    try:
        rows_raw = json.loads(m.group(0)).get("rows", [])
    except ValueError:
        raise RuntimeError("模型返回的 JSON 无法解析：" + text[:200])
    cols = list(columns)
    if not cols:
        for r in rows_raw:
            for k in r:
                if k != "_img" and k not in cols:
                    cols.append(k)
    rows = []
    for r in rows_raw:
        if not isinstance(r, dict):
            continue
        try:
            img = int(r.get("_img", 1))
        except (TypeError, ValueError):
            img = 0                       # 没标注来源，稍后归到第一张图
        data = {c: "" if r.get(c) is None else str(r.get(c)) for c in cols}
        rows.append((img, data))
    return cols, rows


def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


class BatchWorker(threading.Thread):
    """依次处理若干批。每批是若干张截图，一次请求。结果通过队列交回主线程。"""

    def __init__(self, settings, batches, events, cancel):
        super().__init__(daemon=True)
        self.settings = settings
        self.batches = batches        # [[shot_record, ...], ...]
        self.events = events
        self.cancel = cancel

    def run(self):
        try:
            for shots in self.batches:
                if self.cancel.is_set():
                    break
                ids = [s["id"] for s in shots]
                self.events.put(("start", ids))
                t0 = time.time()
                try:
                    imgs = [image_to_webp_b64(s["path"], self.settings["webp_quality"]) for s in shots]
                    text = call_vision_model(self.settings, imgs)
                    cols, rows = parse_table(text, parse_columns(self.settings["columns"]))
                    if not rows:
                        raise RuntimeError("模型没有返回数据行。返回内容：" + text[:200])
                    self.events.put(("ok", ids, cols, rows, time.time() - t0))
                except Exception as e:
                    log("batch failed:\n" + traceback.format_exc())
                    self.events.put(("err", ids, str(e), time.time() - t0))
        finally:
            self.events.put(("finished",))


# ---------------------------------------------------------------- 授权登录（cgwimg）

class AuthThread(threading.Thread):
    """按 app-auth-integration 流程：创建授权请求 → 交给主线程打开浏览器 → 轮询直到拿到 Key。"""

    def __init__(self, events, cancel):
        super().__init__(daemon=True)
        self.events = events
        self.cancel = cancel

    @staticmethod
    def _call(method, url, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": BROWSER_UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8", "replace")).get("error", {}).get("message")
            except ValueError:
                msg = None
            raise RuntimeError(msg or f"HTTP {e.code}")
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error", {}).get("message", "未知错误"))
        return payload["data"]

    def run(self):
        try:
            grant = self._call("POST", CGW_SITE + "/api/authorize/create", {"appId": CGW_APP_ID})
            self.events.put(("auth_url", grant["authUrl"]))
            deadline = time.time() + int(grant.get("expiresInSec", 600))
            query = (f"{CGW_SITE}/api/authorize/query?id={urllib.parse.quote(grant['authId'])}"
                     f"&token={urllib.parse.quote(grant['pollToken'])}")
            while time.time() < deadline:
                if self.cancel.is_set():
                    self.events.put(("auth_fail", "已取消授权"))
                    return
                time.sleep(2.5)
                try:
                    d = self._call("GET", query)
                except (RuntimeError, OSError) as e:      # 网络抖动时继续轮询，直到超时
                    log("auth poll error: " + str(e))
                    continue
                if d.get("status") == "approved" and d.get("key"):
                    self.events.put(("auth_ok", d["key"]))
                    return
                if d.get("status") == "denied":
                    self.events.put(("auth_fail", "你拒绝了授权"))
                    return
                if d.get("status") == "expired":
                    break
            self.events.put(("auth_fail", "授权已超时，请重新授权"))
        except Exception as e:
            log("auth failed:\n" + traceback.format_exc())
            self.events.put(("auth_fail", str(e)))


# ---------------------------------------------------------------- 设置

def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    s["batch_size"] = clamp_int(s.get("batch_size"), 1, 20, 3)
    s["webp_quality"] = clamp_int(s.get("webp_quality"), 1, 100, 85)
    return s


def save_settings(s):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def endpoint(s):
    """实际使用的 (base_url, api_key)：cgwimg 方式用固定地址和授权得到的 Key。"""
    if s.get("provider") == PROVIDER_CGW:
        return CGW_BASE_URL, str(s.get("cgw_key", "")).strip()
    return str(s.get("base_url", "")).strip(), str(s.get("api_key", "")).strip()


def is_configured(s):
    base, key = endpoint(s)
    return bool(base and key and str(s.get("model", "")).strip())


# ---------------------------------------------------------------- 界面

class App:
    """
    数据模型
      self.shots: 截图记录列表，每条 {"id", "path", "time", "size", "state", "rows", "cols", "msg"}
                  state: 待识别 / 识别中 / 已识别 / 失败；rows 为该图的表格行（dict 列表）
      self.result_rows: 结果表的行，按截图顺序展开，每行 {"shot_id", "data"}
    """

    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.q = queue.Queue()
        self.hk = None
        self.shots = []
        self._next_id = 1
        self.worker = None
        self.cancel = threading.Event()
        self.copy_on = False
        self.done = set()           # {(截图id, 行号, 列名)} 已粘贴或已跳过
        self.col_enabled = {}       # {列名: bool} 列开关，默认开启；关闭的列粘贴时整列跳过
        self.copy_menu = None
        self.columns = []
        self.result_rows = []
        self.target = None          # 指定窗口时截图只截该窗口
        self.auto_on = False
        self.auto_last_check = 0.0
        self._photo = None
        self._zoom_photo = None
        self.auth_thread = None
        self.auth_cancel = threading.Event()
        self.auth_status = None     # 设置窗口里「授权状态」的 StringVar，窗口打开时才有

        root.title(APP_NAME)
        root.geometry("1120x700")
        root.minsize(900, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(root, padding=(8, 6))
        top.pack(fill="x")
        ttk.Button(top, text="设置", command=self.open_settings).pack(side="right")
        self.lbl_hotkeys = ttk.Label(top, foreground="#666")
        self.lbl_hotkeys.pack(side="left")

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.tab_shots = ttk.Frame(self.nb, padding=8)
        self.tab_run = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_shots, text="  1. 截图  ")
        self.nb.add(self.tab_run, text="  2. 识别结果  ")
        self.build_shots_tab()
        self.build_run_tab()

        self.lbl_status = ttk.Label(root, padding=(10, 4))
        self.lbl_status.pack(fill="x")

        self.update_hotkey_label()
        self.update_mode()
        self.auto_on = self.var_auto.get()      # 与复选框默认状态保持一致
        self.auto_last_check = time.time()
        self.start_hotkeys()
        self.root.after(200, self.poll)
        if not is_configured(self.settings):
            self.root.after(300, self.open_settings)

    # ---------- 布局：截图页
    def build_shots_tab(self):
        bar = ttk.Frame(self.tab_shots)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="选择窗口…", command=self.pick_window).pack(side="left")
        # 截图范围：默认全屏；选了窗口后显示窗口名，右侧 ✕ 点击即恢复全屏
        scope = ttk.Frame(bar)
        scope.pack(side="left", padx=(8, 0))
        self.lbl_mode = ttk.Label(scope)
        self.lbl_mode.pack(side="left")
        self.btn_clear_target = tk.Button(scope, text="✕", relief="flat", borderwidth=0, padx=4, pady=0,
                                          foreground="#a33", cursor="hand2", command=self.clear_target)
        self.btn_clear_target.pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="截图", command=self.capture).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="导入图片…", command=self.import_files).pack(side="left", padx=6)
        ttk.Button(bar, text="粘贴图片", command=self.paste_images).pack(side="left")
        ttk.Button(bar, text="删除选中", command=self.delete_selected).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=self.clear_shots).pack(side="left")

        body = ttk.Frame(self.tab_shots)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        self.shot_tree = ttk.Treeview(left, columns=("no", "time", "size", "state"), show="headings",
                                      selectmode="extended", height=20)
        for cid, text, w in (("no", "序号", 50), ("time", "时间", 80), ("size", "尺寸", 110),
                             ("state", "状态", 80)):
            self.shot_tree.heading(cid, text=text)
            self.shot_tree.column(cid, width=w, stretch=False)
        self.shot_tree.pack(fill="y")
        self.shot_tree.bind("<<TreeviewSelect>>", self.on_shot_select)
        self.shot_tree.bind("<Double-1>", self.on_shot_double)
        # Ctrl+V 在截图页且剪贴板有图片时导入；没有图片就不拦截，文本粘贴照常工作
        self.root.bind_all("<Control-v>", self.on_ctrl_v, add="+")

        self.preview = ttk.Label(body, anchor="center", text="选中截图预览；双击放大查看",
                                 foreground="#888", cursor="hand2")
        self.preview.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.preview.bind("<Double-1>", self.on_shot_double)

    # ---------- 布局：识别结果页
    def build_run_tab(self):
        bar = ttk.Frame(self.tab_run)
        bar.pack(fill="x", pady=(0, 6))
        self.btn_run_pending = ttk.Button(bar, text="识别待处理", command=self.run_pending)
        self.btn_run_pending.pack(side="left")
        self.btn_rerun = ttk.Button(bar, text="重新识别全部", command=self.rerun_all)
        self.btn_rerun.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(bar, text="取消", command=self.cancel_run, state="disabled")
        self.btn_cancel.pack(side="left")
        ttk.Label(bar, text="每次请求张数").pack(side="left", padx=(16, 4))
        self.var_batch = tk.StringVar(value=str(self.settings["batch_size"]))
        ttk.Spinbox(bar, from_=1, to=20, width=4, textvariable=self.var_batch).pack(side="left")
        self.var_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动识别", variable=self.var_auto,
                        command=self.toggle_auto).pack(side="left", padx=(16, 0))
        self.lbl_progress = ttk.Label(bar, text="")
        self.lbl_progress.pack(side="right")

        ops = ttk.Frame(self.tab_run)
        ops.pack(fill="x", pady=(0, 6))
        self.btn_copy = ttk.Button(ops, text="复制模式：关", command=self.toggle_copy)
        self.btn_copy.pack(side="left")
        ttk.Button(ops, text="跳过下一个", command=self.skip_next).pack(side="left", padx=6)
        ttk.Button(ops, text="重置进度", command=self.reset_progress).pack(side="left")
        ttk.Button(ops, text="清空结果", command=self.clear_results).pack(side="left", padx=6)
        ttk.Label(ops, text="点击行查看来源截图；双击单元格可修改", foreground="#888").pack(side="right")

        pane = ttk.PanedWindow(self.tab_run, orient="horizontal")
        pane.pack(fill="both", expand=True)
        rf = ttk.Frame(pane)
        pane.add(rf, weight=3)
        self.res_tree = ttk.Treeview(rf, show="headings", selectmode="browse")
        vsb = ttk.Scrollbar(rf, orient="vertical", command=self.res_tree.yview)
        hsb = ttk.Scrollbar(rf, orient="horizontal", command=self.res_tree.xview)
        self.res_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.res_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        rf.rowconfigure(0, weight=1)
        rf.columnconfigure(0, weight=1)
        self.res_tree.bind("<<TreeviewSelect>>", self.on_result_select)
        self.res_tree.bind("<Double-1>", self.on_double_click)
        self.res_tree.bind("<Button-1>", self.on_result_click)
        self.res_tree.bind("<Button-3>", self.on_result_right_click)

        sf = ttk.Frame(pane, padding=(8, 0, 0, 0))
        pane.add(sf, weight=2)
        ttk.Label(sf, text="来源截图").pack(anchor="w")
        self.src_label = ttk.Label(sf, anchor="center", text="点击左侧结果行查看来源截图",
                                   foreground="#888", cursor="hand2")
        self.src_label.pack(fill="both", expand=True)
        self.src_label.bind("<Double-1>", self.on_source_double)

        ttk.Label(sf, text="详细").pack(anchor="w", pady=(8, 2))
        df = ttk.Frame(sf)
        df.pack(fill="both", expand=True)
        self.detail = tk.Text(df, height=10, wrap="word", state="disabled",
                              background="#fafafa", relief="solid", borderwidth=1, padx=6, pady=4)
        dsb = ttk.Scrollbar(df, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dsb.set)
        self.detail.pack(side="left", fill="both", expand=True)
        dsb.pack(side="left", fill="y")
        self.detail.tag_configure("key", foreground="#555", font=("Microsoft YaHei UI", 9, "bold"))
        self.detail.tag_configure("val", font=("Microsoft YaHei UI", 10))
        self.detail.tag_configure("meta", foreground="#888")

        self.src_shot_id = None
        self.show_detail(None)

    # ---------- 状态与热键
    def say(self, text):
        self.lbl_status.config(text=text)

    def _auth_msg(self, text):
        if self.auth_status is not None:
            try:
                self.auth_status.set(text)
            except tk.TclError:
                pass
        self.say(text)

    def update_hotkey_label(self):
        self.lbl_hotkeys.config(text=f"截图：{self.settings['recognize_hotkey']}    "
                                     f"粘贴：{self.settings['paste_hotkey']}")

    def start_hotkeys(self):
        self.stop_hotkeys()
        try:
            bindings = {"capture": parse_hotkey(self.settings["recognize_hotkey"]),
                        "paste": parse_hotkey(self.settings["paste_hotkey"])}
        except ValueError as e:
            self.say(f"快捷键设置有误：{e}")
            return
        self.hk = HotkeyThread(bindings, self.q)
        self.hk.start()
        self.hk.ready.wait(2)
        if self.hk.error:
            self.say(self.hk.error.strip())

    def stop_hotkeys(self):
        if self.hk is not None:
            if self.hk.thread_id:
                user32.PostThreadMessageW(self.hk.thread_id, WM_QUIT, 0, 0)
            self.hk.join(2)
            self.hk = None

    # ---------- 事件循环（主线程）
    def poll(self):
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            log("poll error:\n" + traceback.format_exc())
            self.say("内部错误，详情见 " + LOG_PATH)
        self.auto_tick()
        self.root.after(300, self.poll)

    def handle(self, ev):
        kind = ev[0]
        if kind == "hotkey_capture":
            self.capture()
        elif kind == "hotkey_paste":
            self.on_paste_hotkey()
        elif kind == "start":
            for s in self.find_shots(ev[1]):
                s["state"] = "识别中"
            self.refresh_shots()
            self.say(f"正在识别 {len(ev[1])} 张截图…")
        elif kind == "ok":
            self.apply_batch(ev[1], ev[2], ev[3], ev[4])
            self.refresh_shots()
            self.refresh_results()
        elif kind == "err":
            for s in self.find_shots(ev[1]):
                s["state"], s["msg"] = "失败", ev[2]
            self.refresh_shots()
            self.say("识别失败：" + ev[2][:200])
        elif kind == "auth_url":
            webbrowser.open(ev[1])
            self._auth_msg("已打开浏览器，请登录并点「同意授权」，等待确认…")
        elif kind == "auth_ok":
            self.settings["cgw_key"] = ev[1]
            save_settings(self.settings)
            self.auth_thread = None
            self._auth_msg("授权成功，Key 已保存在本机。")
        elif kind == "auth_fail":
            self.auth_thread = None
            self._auth_msg("授权未完成：" + ev[1])
        elif kind == "finished":
            self.worker = None
            self.btn_cancel.state(["disabled"])
            self.refresh_shots()
            self.refresh_results()
            failed = sum(1 for s in self.shots if s["state"] == "失败")
            self.say(f"识别结束。" + (f"有 {failed} 张失败，可点「识别待处理」重试。" if failed else ""))

    def find_shots(self, ids):
        return [s for s in self.shots if s["id"] in ids]

    def apply_batch(self, ids, cols, rows, secs):
        """按模型标注的图片序号（_img，从 1 开始，即该批内第几张）把行分配到对应截图。"""
        shots = [s for s in (self.shot_by_id(i) for i in ids) if s]
        for s in shots:
            s.update(state="已识别", cols=cols, rows=[])
        for img, data in rows:
            idx = img - 1 if 1 <= img <= len(shots) else 0
            shots[idx]["rows"].append(data)
        for s in shots:
            s["msg"] = f"用时 {secs:.1f} 秒，识别出 {len(s['rows'])} 行"

    # ---------- 截图
    def capture(self):
        target = self.target
        try:
            if target is None:
                path = capture_screen()
            elif not user32.IsWindow(target.hwnd):
                self.clear_target()
                return self.say("指定的窗口已关闭，已恢复为全屏。请重新截图。")
            else:
                path = capture_window(target)
        except Exception as e:
            log("capture failed:\n" + traceback.format_exc())
            return self.say("截图失败：" + str(e))
        self.add_shot(path)
        self.nb.select(self.tab_shots)
        what = f"窗口「{target.title[:30]}」" if target else "全屏"
        self.say(f"已截取{what}，共 {len(self.shots)} 张。")

    def on_ctrl_v(self, event):
        """只在「截图」页、焦点不在文本输入控件时，且剪贴板确实有图片时才接管 Ctrl+V。"""
        if self.nb.select() != str(self.tab_shots):
            return None
        if event.widget.winfo_class() in ("Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"):
            return None
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            return None
        if data is None:
            return None               # 剪贴板不是图片，交给系统默认粘贴
        self.paste_images()
        return "break"

    def import_files(self):
        paths = filedialog.askopenfilenames(
            parent=self.root, title="选择要导入的图片（可多选）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp *.webp *.gif"), ("所有文件", "*.*")])
        if paths:
            self._import_paths(list(paths))

    def paste_images(self):
        """粘贴：剪贴板里是图片，或是资源管理器里复制的图片文件。没有图片时不做任何事。"""
        try:
            data = ImageGrab.grabclipboard()
        except Exception as e:
            log("grabclipboard failed:\n" + traceback.format_exc())
            return self.say("读取剪贴板失败：" + str(e))
        if data is None:
            return self.say("剪贴板里没有图片。")
        if isinstance(data, list):
            paths = [p for p in data if isinstance(p, str)]
            return self._import_paths(paths) if paths else self.say("剪贴板里没有图片。")
        path = save_shot(data.convert("RGB"), "paste")
        self.add_shot(path, refresh=False)
        self._after_import(added=1, failed=[])

    def _import_paths(self, paths):
        added, failed = 0, []
        for src in paths:
            if os.path.splitext(src)[1].lower() not in IMAGE_EXTS:
                failed.append(os.path.basename(src) + "（不是图片）")
                continue
            try:
                with Image.open(src) as im:
                    im.load()
                    img = im.convert("RGB")
                path = save_shot(img, "import")
                self.add_shot(path, refresh=False)
                added += 1
            except Exception as e:
                log("import failed: " + src + "\n" + traceback.format_exc())
                failed.append(f"{os.path.basename(src)}（{e}）")
        self._after_import(added, failed)

    def _after_import(self, added, failed):
        self.refresh_shots()
        if self.shots:
            self.shot_tree.selection_set(str(self.shots[-1]["id"]))
            self.shot_tree.see(str(self.shots[-1]["id"]))
        self.nb.select(self.tab_shots)
        msg = f"已导入 {added} 张，共 {len(self.shots)} 张。" if added else "没有导入任何图片。"
        if failed:
            msg += " 失败 " + str(len(failed)) + " 个：" + "；".join(failed[:3])
        self.say(msg)

    def add_shot(self, path, refresh=True):
        with Image.open(path) as im:
            size = f"{im.width} × {im.height}"
        self.shots.append({"id": self._next_id, "path": path, "time": time.strftime("%H:%M:%S"),
                           "size": size, "state": "待识别", "rows": [], "cols": [], "msg": ""})
        self._next_id += 1
        if not refresh:
            return
        self.refresh_shots()
        self.shot_tree.selection_set(str(self.shots[-1]["id"]))
        self.shot_tree.see(str(self.shots[-1]["id"]))

    def refresh_shots(self):
        sel = set(self.shot_tree.selection())
        self.shot_tree.delete(*self.shot_tree.get_children())
        for i, s in enumerate(self.shots, start=1):
            self.shot_tree.insert("", "end", iid=str(s["id"]),
                                  values=(i, s["time"], s["size"], s["state"]))
        for iid in sel:
            if self.shot_tree.exists(iid):
                self.shot_tree.selection_add(iid)
        self.update_mode()

    def pick_window(self):
        wins = list_windows(own_hwnd=self.root.winfo_id())
        if not wins:
            return self.say("没有找到可截取的窗口")
        dlg = tk.Toplevel(self.root)
        dlg.title("选择要截取的窗口")
        dlg.transient(self.root)
        dlg.geometry("640x420")
        ttk.Label(dlg, text="选中一个窗口后点「选择」。之后截图都只截这个窗口，窗口会被置于最前。",
                  padding=(10, 8)).pack(anchor="w")
        frm = ttk.Frame(dlg, padding=(10, 0))
        frm.pack(fill="both", expand=True)
        tree = ttk.Treeview(frm, columns=("title", "size"), show="headings", selectmode="browse")
        tree.heading("title", text="窗口标题")
        tree.heading("size", text="尺寸")
        tree.column("title", width=460)
        tree.column("size", width=120, stretch=False)
        vsb = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        by_id = {}
        for w in wins:
            iid = str(w.hwnd)
            by_id[iid] = w
            tree.insert("", "end", iid=iid, values=(w.title, f"{w.rect[2]-w.rect[0]} × {w.rect[3]-w.rect[1]}"))
        tree.selection_set(next(iter(by_id)))

        def take(_=None):
            sel = tree.selection()
            if not sel:
                return
            info = by_id[sel[0]]
            dlg.destroy()
            self.set_target(info)

        btns = ttk.Frame(dlg, padding=10)
        btns.pack(fill="x")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="选择", command=take).pack(side="right", padx=6)
        tree.bind("<Double-1>", take)
        dlg.grab_set()

    def set_target(self, info):
        self.target = info
        self.update_mode()
        self.say(f"截图范围已设为窗口「{info.title[:40]}」。之后的截图都只截这个窗口。")

    def clear_target(self):
        self.target = None
        self.update_mode()
        self.say("截图范围已恢复为全屏。")

    def update_mode(self):
        if self.target:
            self.lbl_mode.config(text=f"窗口：{self.target.title[:30]}", foreground="#0a6")
            self.btn_clear_target.pack(side="left", padx=(4, 0))
        else:
            self.lbl_mode.config(text="全屏", foreground="#666")
            self.btn_clear_target.pack_forget()

    def selected_shot_ids(self):
        return [int(i) for i in self.shot_tree.selection()]

    def delete_selected(self):
        ids = set(self.selected_shot_ids())
        if not ids:
            return self.say("请先在列表中选中要删除的截图")
        if self.worker is not None and any(s["state"] == "识别中" for s in self.shots if s["id"] in ids):
            return self.say("选中的截图正在识别中，请等识别结束后再删除")
        keep = []
        for s in self.shots:
            if s["id"] in ids:
                self.remove_file(s["path"])
            else:
                keep.append(s)
        self.shots = keep
        self.refresh_shots()
        self.refresh_results()
        self.show_preview(None)
        self.say(f"已删除 {len(ids)} 张，剩余 {len(self.shots)} 张")

    def clear_shots(self):
        if not self.shots:
            return self.say("截图列表已经是空的")
        if self.worker is not None:
            return self.say("正在识别中，请先取消或等待结束")
        if not messagebox.askyesno("确认", f"清空全部 {len(self.shots)} 张截图（同时清空识别结果）？",
                                   parent=self.root):
            return
        for s in self.shots:
            self.remove_file(s["path"])
        self.shots = []
        self.done.clear()
        self.refresh_shots()
        self.refresh_results()
        self.show_preview(None)
        self.say("截图和识别结果已清空")

    def clear_results(self):
        """只清空识别结果，保留截图；截图状态回到「待识别」。"""
        if self.worker is not None:
            return self.say("正在识别中，请先取消或等待结束")
        for s in self.shots:
            s.update(state="待识别", rows=[], cols=[], msg="")
        self.done.clear()
        self.refresh_shots()
        self.refresh_results()
        self.say("识别结果已清空，截图已保留")

    @staticmethod
    def remove_file(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def shot_by_id(self, sid):
        return next((s for s in self.shots if s["id"] == sid), None)

    def on_shot_select(self, _=None):
        sel = self.selected_shot_ids()
        if sel:
            s = self.shot_by_id(sel[-1])
            if s:
                self.show_preview(s)

    def show_preview(self, s):
        if not s:
            self.preview.config(image="", text="选中截图预览；双击放大查看")
            self._photo = None
            return
        try:
            im = Image.open(s["path"])
            im.thumbnail((620, 460))
            self._photo = ImageTk.PhotoImage(im.convert("RGB"))
            self.preview.config(image=self._photo, text="")
        except OSError as e:
            self.preview.config(image="", text=f"无法预览：{e}")

    def on_shot_double(self, _=None):
        sel = self.selected_shot_ids()
        if sel:
            s = self.shot_by_id(sel[-1])
            if s:
                self.open_zoom(s["path"], s["time"])

    def on_source_double(self, _=None):
        if self.src_shot_id is not None:
            s = self.shot_by_id(self.src_shot_id)
            if s:
                self.open_zoom(s["path"], s["time"])

    def open_zoom(self, path, title):
        """放大查看：全尺寸，超出窗口时可滚动拖动；Ctrl+滚轮缩放。"""
        win = tk.Toplevel(self.root)
        win.title(f"查看截图 - {title}")
        win.geometry("1000x700")
        frm = ttk.Frame(win)
        frm.pack(fill="both", expand=True)
        cv = tk.Canvas(frm, background="#222", highlightthickness=0)
        vsb = ttk.Scrollbar(frm, orient="vertical", command=cv.yview)
        hsb = ttk.Scrollbar(frm, orient="horizontal", command=cv.xview)
        cv.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        cv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frm.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)

        base = Image.open(path).convert("RGB")
        state = {"scale": 1.0, "photo": None, "item": None}

        def draw():
            w = max(1, int(base.width * state["scale"]))
            h = max(1, int(base.height * state["scale"]))
            img = base.resize((w, h), Image.Resampling.LANCZOS) if state["scale"] != 1.0 else base
            state["photo"] = ImageTk.PhotoImage(img)
            if state["item"] is None:
                state["item"] = cv.create_image(0, 0, anchor="nw", image=state["photo"])
            else:
                cv.itemconfigure(state["item"], image=state["photo"])
            cv.configure(scrollregion=(0, 0, w, h))
            win.title(f"查看截图 - {title}  （{int(state['scale'] * 100)}%）")

        def zoom(factor):
            state["scale"] = min(max(state["scale"] * factor, 0.1), 5.0)
            draw()

        def on_wheel(e):
            if e.state & 0x0004:            # Ctrl + 滚轮：缩放
                zoom(1.15 if e.delta > 0 else 1 / 1.15)
            else:
                cv.yview_scroll(-1 if e.delta > 0 else 1, "units")

        cv.bind("<MouseWheel>", on_wheel)
        cv.bind("<ButtonPress-1>", lambda e: cv.scan_mark(e.x, e.y))
        cv.bind("<B1-Motion>", lambda e: cv.scan_dragto(e.x, e.y, gain=1))
        win.bind("<plus>", lambda e: zoom(1.25))
        win.bind("<equal>", lambda e: zoom(1.25))
        win.bind("<minus>", lambda e: zoom(1 / 1.25))
        win.bind("<Key-0>", lambda e: (state.update(scale=1.0), draw()))
        bar = ttk.Frame(win)
        bar.place(relx=1.0, rely=0.0, anchor="ne")
        ttk.Button(bar, text="放大", command=lambda: zoom(1.25)).pack(side="left")
        ttk.Button(bar, text="缩小", command=lambda: zoom(1 / 1.25)).pack(side="left", padx=4)
        ttk.Button(bar, text="原始大小", command=lambda: (state.update(scale=1.0), draw())).pack(side="left")
        draw()
        win.focus_set()

    # ---------- 识别调度
    def _start_batches(self, shots, label):
        """shots: 要识别的截图记录列表。按「每次请求张数」切分后启动后台线程。"""
        if self.worker is not None:
            return self.say("正在识别中，请等待或先取消")
        if not is_configured(self.settings):
            return self.open_settings()
        if not shots:
            return self.say("没有需要识别的截图")
        n = clamp_int(self.var_batch.get(), 1, 20, 3)
        if n != self.settings["batch_size"]:
            self.settings["batch_size"] = n
            save_settings(self.settings)
        batches = chunk([{"id": s["id"], "path": s["path"]} for s in shots], n)
        for s in shots:
            s["state"] = "等待"
        self.cancel.clear()
        self.worker = BatchWorker(dict(self.settings), batches, self.q, self.cancel)
        self.worker.start()
        self.btn_cancel.state(["!disabled"])
        self.auto_last_check = time.time()
        self.refresh_shots()
        self.say(f"{label}：{len(shots)} 张，分 {len(batches)} 次请求，每次最多 {n} 张")

    def run_pending(self):
        """识别所有未识别（待识别/失败）的截图。"""
        shots = [s for s in self.shots if s["state"] in ("待识别", "失败")]
        if not shots:
            return self.say("没有待处理的截图")
        self._start_batches(shots, "识别待处理")

    def rerun_all(self):
        """重新识别全部截图（覆盖已有结果）。"""
        if not self.shots:
            return self.say("还没有截图")
        if self.worker is not None:
            return self.say("正在识别中，请等待或先取消")
        if any(s["state"] == "已识别" for s in self.shots) and not messagebox.askyesno(
                "确认", "重新识别会覆盖已有结果，继续吗？", parent=self.root):
            return
        self.done.clear()
        for s in self.shots:
            s.update(rows=[], cols=[], msg="")
        self._start_batches(list(self.shots), "重新识别全部")

    def cancel_run(self):
        if self.worker is not None:
            self.cancel.set()
            self.say("已请求取消：当前请求返回后停止，之后的不再发送")

    # ---------- 自动识别
    def toggle_auto(self):
        self.auto_on = self.var_auto.get()
        if self.auto_on:
            self.auto_last_check = time.time()
            n = clamp_int(self.var_batch.get(), 1, 20, 3)
            self.say(f"自动识别已开启：待识别截图攒够 {n} 张（每次请求张数）立即识别；"
                     f"不足 {n} 张时每 {AUTO_INTERVAL_SEC} 秒识别一次。")
        else:
            self.say("自动识别已关闭")

    def auto_tick(self):
        if not self.auto_on or self.worker is not None:
            return
        if not is_configured(self.settings):       # 未配置接口时关闭自动识别，避免反复弹设置窗口
            self.auto_on = False
            self.var_auto.set(False)
            return self.say("自动识别已关闭：请先在「设置」里填写 Base URL、API Key 和模型名称。")
        # 只自动处理「待识别」的截图。失败的不自动重试，避免每分钟反复消耗额度，由用户手动重试。
        pending = [s for s in self.shots if s["state"] == "待识别"]
        if not pending:
            self.auto_last_check = time.time()
            return
        # 触发条件：待识别截图攒够「每次请求张数」就识别一次（只取最早的 n 张）；
        # 不足 n 张时，每 AUTO_INTERVAL_SEC 秒把剩下的全部识别一次。
        n = clamp_int(self.var_batch.get(), 1, 20, 3)
        if len(pending) >= n:
            self._start_batches(pending[:n], f"自动识别（已攒够 {n} 张）")
        elif time.time() - self.auto_last_check >= AUTO_INTERVAL_SEC:
            self._start_batches(pending, f"自动识别（每 {AUTO_INTERVAL_SEC} 秒检查）")

    # ---------- 结果表
    def refresh_results(self):
        """由截图记录生成结果表：每张截图的行依次展开，记录来源截图。"""
        cols = list(parse_columns(self.settings["columns"]))
        for s in self.shots:
            if s["state"] != "已识别":
                continue
            for c in s["cols"]:
                if c not in cols:
                    cols.append(c)
        self.columns = cols
        self.result_rows = []
        for s in self.shots:
            if s["state"] != "已识别":
                continue
            for i, data in enumerate(s["rows"]):
                self.result_rows.append({"shot_id": s["id"], "idx": i, "data": data})

        ids = [f"c{i}" for i in range(len(cols))]
        self.res_tree.delete(*self.res_tree.get_children())
        self.res_tree["columns"] = ["seq"] + ids
        self.res_tree.heading("seq", text="#")
        self.res_tree.column("seq", width=70, stretch=False)
        for i, name in enumerate(cols):
            on = self.col_enabled.get(name, True)
            self.res_tree.heading(ids[i], text=("☑ " if on else "☐ ") + name)
            self.res_tree.column(ids[i], width=max(90, min(260, 14 * len(name) + 60)), stretch=True)

        seen = {}
        for n, r in enumerate(self.result_rows):
            key = self.row_key(r["data"], cols)
            seen[key] = seen.get(key, 0) + 1
        for n, r in enumerate(self.result_rows):
            key = self.row_key(r["data"], cols)
            dup = seen[key] > 1
            tag = f"shot{r['shot_id']}"
            vals = [f"{n + 1}{' ⚠重复' if dup else ''}"] + [
                self.disp(n, c, r["data"].get(c, "")) for c in cols]
            self.res_tree.insert("", "end", iid=str(n), values=vals, tags=(tag,))
        for i, sid in enumerate(sorted({r["shot_id"] for r in self.result_rows})):
            self.res_tree.tag_configure(f"shot{sid}", background=GROUP_COLORS[i % len(GROUP_COLORS)])
        self.update_progress()

    @staticmethod
    def row_key(data, cols):
        return tuple(one_line(data.get(c, "")).strip() for c in cols)

    def dkey(self, n, col):
        """已处理标记的稳定键：用截图 id + 行在该截图内的序号 + 列名，不随行号变化。"""
        r = self.result_rows[n]
        return (r["shot_id"], r["idx"], col)

    def disp(self, n, col, value):
        shown = one_line(value)
        return ("✓ " + shown) if value and self.dkey(n, col) in self.done else shown

    def update_progress(self):
        total = done = 0
        for n, r in enumerate(self.result_rows):
            for c in self.columns:
                if self.col_enabled.get(c, True) and r["data"].get(c, ""):
                    total += 1
                    if self.dkey(n, c) in self.done:
                        done += 1
        self.lbl_progress.config(text=f"进度 {done}/{total}" if total else "")

    def next_pos(self):
        """下一个可粘贴的单元格：所在列开关必须开启，且有内容、未处理。"""
        for n, r in enumerate(self.result_rows):
            for c in self.columns:
                if not self.col_enabled.get(c, True):
                    continue
                if r["data"].get(c, "") and self.dkey(n, c) not in self.done:
                    return n, c
        return None

    def on_result_select(self, _=None):
        sel = self.res_tree.selection()
        if not sel:
            return
        n = int(sel[0])
        sid = self.result_rows[n]["shot_id"]
        self.show_source(sid)
        self.show_detail(n)

    def show_detail(self, n):
        """下方详细面板：列出选中行的每个字段完整内容（多行原样显示），以及来源信息。"""
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        if n is None or n >= len(self.result_rows):
            self.detail.insert("end", "选中左侧一行，这里显示该行的完整内容。", "meta")
            self.detail.config(state="disabled")
            return
        r = self.result_rows[n]
        s = self.shot_by_id(r["shot_id"])
        src = (f"来源：截图 #{self.shot_index(r['shot_id'])}（{s['time']}，{s['size']}）"
               if s else "来源：截图已删除")
        self.detail.insert("end", f"第 {n + 1} 行    {src}\n", "meta")
        for c in self.columns:
            self.detail.insert("end", f"{c}\n", "key")
            value = r["data"].get(c, "")
            self.detail.insert("end", (value if value else "（空）") + "\n\n", "val")
        if self.row_key(r["data"], self.columns) in self.duplicate_keys():
            self.detail.insert("end", "⚠ 该行内容与其他行重复", "meta")
        self.detail.config(state="disabled")

    def shot_index(self, sid):
        return next((i for i, s in enumerate(self.shots, start=1) if s["id"] == sid), "?")

    def duplicate_keys(self):
        counts = {}
        for r in self.result_rows:
            k = self.row_key(r["data"], self.columns)
            counts[k] = counts.get(k, 0) + 1
        return {k for k, v in counts.items() if v > 1}

    def show_source(self, sid):
        s = self.shot_by_id(sid)
        self.src_shot_id = sid
        if not s:
            self.src_label.config(image="", text="来源截图已删除")
            return
        try:
            im = Image.open(s["path"])
            im.thumbnail((420, 520))
            self._photo_src = ImageTk.PhotoImage(im.convert("RGB"))
            self.src_label.config(image=self._photo_src, text="")
        except OSError as e:
            self.src_label.config(image="", text=f"无法显示：{e}")

    def on_result_click(self, event):
        """点击表头的列名切换该列开关（☑ 启用 / ☐ 停用）。"""
        if self.res_tree.identify_region(event.x, event.y) != "heading":
            return
        col_id = self.res_tree.identify_column(event.x)     # "#1" 是序号列，数据列从 "#2" 开始
        if col_id in ("#0", "#1"):
            return
        name = self.columns[int(col_id[1:]) - 2]
        self.toggle_column(name)
        return "break"

    def toggle_column(self, name):
        self.col_enabled[name] = not self.col_enabled.get(name, True)
        self.refresh_results()
        self.say(f"列「{name}」已{'启用，粘贴时会包含它' if self.col_enabled[name] else '停用，粘贴时整列跳过'}。")

    def on_result_right_click(self, event):
        iid = self.res_tree.identify_row(event.y)
        if not iid:
            return
        self.res_tree.selection_set(iid)
        n = int(iid)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="删除此行", command=lambda: self.delete_rows([n]))
        sid = self.result_rows[n]["shot_id"]
        menu.add_command(label="删除此截图的全部结果行", command=lambda: self.delete_shot_rows(sid))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def delete_rows(self, ns):
        """从识别数据里删除指定的结果行（按 result_rows 下标）。"""
        if self.worker is not None:
            return self.say("正在识别中，请等待结束后再删除")
        targets = {(self.result_rows[n]["shot_id"], self.result_rows[n]["idx"]) for n in ns}
        self._remove_from_shots(targets)
        self.say(f"已删除 {len(targets)} 行")

    def delete_shot_rows(self, sid):
        if self.worker is not None:
            return self.say("正在识别中，请等待结束后再删除")
        targets = {(r["shot_id"], r["idx"]) for r in self.result_rows if r["shot_id"] == sid}
        if not targets:
            return self.say("该截图没有结果行")
        if not messagebox.askyesno("确认", f"删除该截图的全部 {len(targets)} 行结果？（截图本身保留）",
                                   parent=self.root):
            return
        self._remove_from_shots(targets)
        self.say(f"已删除该截图的 {len(targets)} 行结果，截图已保留")

    def _remove_from_shots(self, targets):
        """从截图记录里删除行，并把同一截图后面行的序号、开关、已处理标记整体前移，保持一致。"""
        for s in self.shots:
            if s["state"] != "已识别":
                continue
            removed = sorted((i for (sid, i) in targets if sid == s["id"]), reverse=True)
            if not removed:
                continue
            for i in removed:
                del s["rows"][i]
            self._reindex_shot(s["id"], removed)
        self.refresh_shots()
        self.refresh_results()

    def _reindex_shot(self, sid, removed_idx):
        """removed_idx 为已删除的行号（删除前的原序号）。把同一截图中之后的行号前移，保持已粘贴标记对齐。"""
        def new_index(i):
            return i - sum(1 for d in removed_idx if d < i)

        removed_set = set(removed_idx)
        self.done = {
            (s, new_index(i), c) if s == sid else (s, i, c)
            for (s, i, c) in self.done
            if not (s == sid and i in removed_set)
        }

    def on_double_click(self, event):
        iid = self.res_tree.identify_row(event.y)
        col = self.res_tree.identify_column(event.x)
        if not iid or col in ("#0", "#1"):
            return
        box = self.res_tree.bbox(iid, col)
        if not box:
            return
        n = int(iid)
        cname = self.columns[int(col[1:]) - 2]
        row = self.result_rows[n]["data"]
        x, y, w, h = box
        entry = ttk.Entry(self.res_tree)
        entry.place(x=x, y=y, width=w, height=h)
        entry.insert(0, row.get(cname, ""))
        entry.select_range(0, "end")
        entry.focus_set()

        def commit(_=None):
            if entry.winfo_exists():
                row[cname] = entry.get()
                entry.destroy()
                self.refresh_results()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", lambda e: entry.destroy())

    def reset_progress(self):
        self.done.clear()
        self.refresh_results()
        self.say("进度已重置")

    # ---------- 复制模式
    def toggle_copy(self):
        self.copy_on = not self.copy_on
        self.btn_copy.config(text="复制模式：开" if self.copy_on else "复制模式：关")
        self.say("复制模式已开启：按粘贴快捷键，把下一个未填单元格粘贴到当前窗口。" if self.copy_on
                 else "复制模式已关闭，粘贴快捷键不再生效。")

    def on_paste_hotkey(self):
        if not self.copy_on:
            return self.say("复制模式未开启：请在「识别结果」页点击「复制模式」后再使用粘贴快捷键")
        pos = self.next_pos()
        if pos is None:
            return self.say("没有可粘贴的单元格了（已全部完成或还没有结果）")
        n, col = pos
        value = self.result_rows[n]["data"].get(col, "")
        self.done.add(self.dkey(n, col))
        self.refresh_results()
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.root.update()
        wait_modifiers_released()
        send_ctrl_v()
        self.say(f"已粘贴 第{n + 1}行 [{col}]：{one_line(value)}")

    def skip_next(self):
        pos = self.next_pos()
        if pos is None:
            return self.say("没有剩余单元格")
        n, col = pos
        self.done.add(self.dkey(n, col))
        self.refresh_results()
        self.say(f"已跳过 第{n + 1}行 [{col}]")

    # ---------- 设置窗口
    def open_settings(self):
        s = self.settings
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        vars_ = {}

        def field(row, label, key, hint, show=""):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=(6, 0))
            var = tk.StringVar(value=str(s[key]))
            ttk.Entry(frm, textvariable=var, width=56, show=show).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))
            ttk.Label(frm, text=hint, foreground="#888").grid(row=row + 1, column=1, sticky="w", padx=(10, 0))
            vars_[key] = var

        def hotkey_field(row, label, key, hint):
            """按键录制框：点一下后直接按组合键即可，Esc 取消。文本不可手输。"""
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=(6, 0))
            var = tk.StringVar(value=str(s[key]))
            box = tk.Entry(frm, textvariable=var, width=56, state="readonly", cursor="hand2",
                           readonlybackground="#fff")
            box.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))
            ttk.Label(frm, text=hint, foreground="#888").grid(row=row + 1, column=1, sticky="w", padx=(10, 0))
            # held：当前按住的修饰键，由 KeyPress/KeyRelease 维护，不依赖 event.state 的位
            state = {"prev": str(s[key]), "recording": False, "held": set()}

            def start(_=None):
                if not state["recording"]:          # 连续点击时不能把提示文字当成原值
                    state["prev"] = var.get()
                state["recording"] = True
                state["held"] = set()
                box.config(state="normal")
                var.set("请按下组合键…（Esc 取消）")
                box.config(state="readonly")
                box.focus_set()
                return "break"

            def on_press(event):
                if not state["recording"]:
                    return "break"
                if event.keysym in _MODIFIER_KEYSYMS:
                    state["held"].add(_MODIFIER_KEYSYMS[event.keysym])
                    return "break"
                if event.keysym == "Escape":
                    var.set(state["prev"])
                    state["recording"] = False
                    return "break"
                text = hotkey_from_event(event, state["held"])
                if text is None:
                    return "break"              # 没有修饰键或无法识别的键，继续等
                try:
                    parse_hotkey(text)          # 与保存时的校验一致
                except ValueError:
                    var.set("该按键组合不可用，请换一个")
                    state["recording"] = False
                    return "break"
                var.set(text)
                state["recording"] = False
                return "break"

            def on_release(event):
                if event.keysym in _MODIFIER_KEYSYMS:
                    state["held"].discard(_MODIFIER_KEYSYMS[event.keysym])
                return "break"

            box.bind("<Button-1>", start)
            box.bind("<KeyPress>", on_press)
            box.bind("<KeyRelease>", on_release)
            box.bind("<FocusOut>", lambda e: (state.update(recording=False),
                                               var.set(state["prev"]) if var.get().startswith("请按下") else None))
            vars_[key] = var
            return var

        hotkey_field(0, "截图快捷键", "recognize_hotkey", "点击输入框后直接按组合键，例如 Ctrl+Shift+R")
        hotkey_field(2, "粘贴快捷键", "paste_hotkey", "点击输入框后直接按组合键，例如 Ctrl+Shift+V")

        # ---- 接口来源
        ttk.Label(frm, text="接口来源").grid(row=4, column=0, sticky="w", pady=(10, 0))
        prov_var = tk.StringVar(value=PROVIDER_LABELS.get(s.get("provider"), PROVIDER_LABELS[PROVIDER_CUSTOM]))
        combo = ttk.Combobox(frm, textvariable=prov_var, state="readonly", width=54,
                             values=list(PROVIDER_LABELS.values()))
        combo.grid(row=4, column=1, sticky="ew", padx=(10, 0), pady=(10, 0))

        # 自定义接口：Base URL、API Key
        custom = ttk.Frame(frm)
        custom.grid(row=5, column=0, columnspan=2, sticky="ew")
        custom.columnconfigure(1, weight=1)
        for r, (label, key, hint, show) in enumerate((
                ("Base URL", "base_url", "OpenAI 协议地址，例如 https://api.openai.com/v1", ""),
                ("API Key", "api_key", "接口密钥（保存在本机 %APPDATA%\ScreenshotExtract 下）", "*"))):
            ttk.Label(custom, text=label).grid(row=r * 2, column=0, sticky="w", pady=(6, 0))
            var = tk.StringVar(value=str(s[key]))
            ttk.Entry(custom, textvariable=var, width=56, show=show).grid(
                row=r * 2, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))
            ttk.Label(custom, text=hint, foreground="#888").grid(row=r * 2 + 1, column=1, sticky="w", padx=(10, 0))
            vars_[key] = var
        # 模型名称只属于自定义接口；cgwimg 方式固定使用 CGW_MODEL，界面不显示
        ttk.Label(custom, text="模型名称").grid(row=4, column=0, sticky="w", pady=(6, 0))
        model_var = tk.StringVar(value=str(s["model"]))
        ttk.Entry(custom, textvariable=model_var, width=56).grid(
            row=4, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))
        ttk.Label(custom, text="需支持图片输入的模型，例如 gpt-4o", foreground="#888").grid(
            row=5, column=1, sticky="w", padx=(10, 0))
        vars_["model"] = model_var

        # cgwimg：授权登录
        cgw = ttk.Frame(frm)
        cgw.grid(row=5, column=0, columnspan=2, sticky="ew")
        cgw.columnconfigure(1, weight=1)
        ttk.Label(cgw, text="cgwimg 授权").grid(row=0, column=0, sticky="nw", pady=(6, 0))
        status_var = tk.StringVar()
        self.auth_status = status_var

        def refresh_status():
            if self.auth_thread is not None:
                return
            status_var.set("已授权，Key 已保存在本机。" if s.get("cgw_key") else "尚未授权。")

        def start_auth():
            if self.auth_thread is not None:
                return
            self.auth_cancel.clear()
            self.auth_thread = AuthThread(self.q, self.auth_cancel)
            self.auth_thread.start()
            status_var.set("正在申请授权…")

        def clear_auth():
            if messagebox.askyesno("确认", "清除本机保存的 cgwimg 授权？之后需要重新授权。", parent=win):
                self.settings["cgw_key"] = ""
                save_settings(self.settings)
                refresh_status()

        btn_row = ttk.Frame(cgw)
        btn_row.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=(6, 0))
        ttk.Button(btn_row, text="授权登录", command=start_auth).pack(side="left")
        ttk.Button(btn_row, text="清除授权", command=clear_auth).pack(side="left", padx=6)
        ttk.Label(cgw, textvariable=status_var, foreground="#888").grid(
            row=1, column=1, sticky="w", padx=(10, 0))
        ttk.Label(cgw, text="点「授权登录」会打开浏览器，登录 cgwimg 并同意后自动获取 Key，无需手填 Base URL 和 API Key。",
                  foreground="#888", wraplength=440, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=(0, 0), pady=(4, 0))

        def on_provider(_=None):
            is_cgw = prov_var.get() == PROVIDER_LABELS[PROVIDER_CGW]
            if is_cgw:
                custom.grid_remove()
                cgw.grid()
                refresh_status()
            else:
                cgw.grid_remove()
                custom.grid()

        combo.bind("<<ComboboxSelected>>", on_provider)
        on_provider()

        field(8, "提取格式（列名）", "columns", "用逗号分隔，例如：项目,数量,单价。留空则按截图表头自动识别")
        field(10, "图片 WebP 质量", "webp_quality", "1-100，默认 85")

        ttk.Label(frm, text="额外提取要求").grid(row=12, column=0, sticky="nw", pady=(6, 0))
        extra = tk.Text(frm, width=56, height=4, wrap="word")
        extra.grid(row=12, column=1, sticky="ew", padx=(10, 0), pady=(6, 0))
        extra.insert("1.0", s["extra_prompt"])
        ttk.Label(frm, text="可选，例如：金额保留两位小数；忽略合计行", foreground="#888").grid(
            row=13, column=1, sticky="w", padx=(10, 0))

        def save():
            new = dict(s)
            for k, var in vars_.items():
                new[k] = var.get().strip()
            new["provider"] = (PROVIDER_CGW if prov_var.get() == PROVIDER_LABELS[PROVIDER_CGW]
                               else PROVIDER_CUSTOM)
            if new["provider"] == PROVIDER_CGW:
                new["model"] = CGW_MODEL          # cgwimg 固定模型，不接受界面输入
            new["extra_prompt"] = extra.get("1.0", "end").strip()
            try:
                rk = parse_hotkey(new["recognize_hotkey"])
                pk = parse_hotkey(new["paste_hotkey"])
            except ValueError as e:
                return messagebox.showerror("设置有误", str(e), parent=win)
            if rk == pk:
                return messagebox.showerror("设置有误", "截图快捷键和粘贴快捷键不能相同", parent=win)
            if new["provider"] == PROVIDER_CGW and not str(new.get("cgw_key", "")).strip():
                return messagebox.showerror("设置有误", "请先点「授权登录」完成 cgwimg 授权", parent=win)
            if new["provider"] == PROVIDER_CUSTOM and not str(new.get("model", "")).strip():
                return messagebox.showerror("设置有误", "请填写模型名称", parent=win)
            new["recognize_hotkey"] = new["recognize_hotkey"].lower()
            new["paste_hotkey"] = new["paste_hotkey"].lower()
            new["webp_quality"] = clamp_int(new["webp_quality"], 1, 100, 85)
            self.settings = new
            save_settings(new)
            self.update_hotkey_label()
            self.start_hotkeys()
            self.refresh_results()
            if self.hk and not self.hk.error:
                self.say("设置已保存，快捷键已生效")
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=14, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="保存", command=save).pack(side="right", padx=6)

    def on_close(self):
        self.cancel.set()
        self.auth_cancel.set()
        self.stop_hotkeys()
        self.root.destroy()


# ---------------------------------------------------------------- 入口

def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
