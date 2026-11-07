# -*- coding: utf-8 -*-
"""应用配置、路径、设置读写、日志。纯逻辑，不依赖界面。"""
import json
import os
import re
import time

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
CGW_SITE = "https://cgwimg.com"              # cgwimg 站点（授权页与接口都在这里）
CGW_BASE_URL = CGW_SITE + "/api/v1"          # OpenAI 兼容接口地址
CGW_APP_ID = "screenshot-extraction"         # 在 cgwimg 后台登记的应用 ID
CGW_MODEL = "gpt-6.1-sol"                    # cgwimg 方式固定使用的模型
PROVIDER_CUSTOM, PROVIDER_CGW = "custom", "cgwimg"
PROVIDER_LABELS = {PROVIDER_CUSTOM: "自定义（OpenAI 兼容接口）",
                   PROVIDER_CGW: "cgwimg（授权登录，无需 Key）"}

DEFAULT_SETTINGS = {
    "recognize_hotkey": "ctrl+shift+r",   # 截图快捷键
    "paste_hotkey": "ctrl+shift+v",       # 粘贴模式下粘贴下一个单元格
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
