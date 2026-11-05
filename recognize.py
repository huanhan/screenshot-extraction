# -*- coding: utf-8 -*-
"""识别：图片转 WebP、调用 OpenAI 兼容接口、解析表格、后台批量识别线程。不依赖界面。"""
import base64
import io
import json
import re
import threading
import time
import traceback
import urllib.error
import urllib.request

from PIL import Image

from app_config import BROWSER_UA, MAX_IMAGE_WIDTH, REQUEST_TIMEOUT, endpoint, log, parse_columns

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
         "每一行还要包含字段 \"_box\"，值为该行在其所在图片中的位置 [x1, y1, x2, y2]，四个整数取值 0 到 1000"
         "（左上角为 0,0，右下角为 1000,1000）。"
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


def call_chat(settings, content):
    """向 OpenAI 兼容接口发一次请求。content 为消息内容列表（文本或图片），返回模型输出的文字。"""
    base, api_key = endpoint(settings)
    url = base.rstrip("/") + "/chat/completions"
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


def call_vision_model(settings, images_b64):
    content = [{"type": "text", "text": build_prompt(parse_columns(settings["columns"]),
                                                    settings["extra_prompt"], len(images_b64))}]
    for b in images_b64:
        content.append({"type": "image_url", "image_url": {"url": "data:image/webp;base64," + b}})
    return call_chat(settings, content)


def build_text_prompt(columns, extra):
    if columns:
        fmt = f"每一行输出一个对象，键必须严格为这些列名（按此顺序）：{json.dumps(columns, ensure_ascii=False)}。"
    else:
        fmt = "每一行输出一个对象，键由你根据聊天内容确定（如 发送人、时间、内容），同一批数据的键保持一致。"
    p = ("你是聊天记录提取助手。下面是从微信聊天窗口复制出来的聊天记录文本，请提取其中的全部消息。"
         "按原文顺序输出，不要去重，不要遗漏。"
         + fmt +
         "每一行还要包含字段 \"_lines\"，值为该行内容在聊天记录中的起止行号 [起始行, 结束行]（整数）。"
         "行号即聊天记录每行开头的数字。数据值中不要包含行号。"
         "单元格没有内容时填空字符串。保持原文，不要编造。"
         "只输出 JSON，格式为 {\"rows\": [ {...}, {...} ]}，不要输出其他文字或代码块标记。")
    if extra.strip():
        p += "\n额外要求：" + extra.strip()
    return p


def recognize_text(settings, text):
    """文本记录：纯文本请求，返回 (列名, 行)，与图片识别的结果格式相同。每行加行号，便于模型给出位置。"""
    prompt = build_text_prompt(parse_columns(settings["columns"]), settings["extra_prompt"])
    numbered = "\n".join(f"{i}| {line}" for i, line in enumerate(text.split("\n"), 1))
    content = [{"type": "text", "text": prompt + "\n\n【聊天记录（每行开头的数字为行号）】\n" + numbered}]
    return parse_table(call_chat(settings, content), parse_columns(settings["columns"]))


def parse_loc(r):
    """行的位置：图片为 ["box", x1, y1, x2, y2]（0-1000），文本为 ["lines", 起始行, 结束行]。格式不对就返回 None。"""
    box = r.get("_box")
    if isinstance(box, list) and len(box) == 4:
        try:
            x1, y1, x2, y2 = [min(max(int(float(v)), 0), 1000) for v in box]
            return ["box", min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
        except (TypeError, ValueError):
            pass
    lines = r.get("_lines")
    if isinstance(lines, list) and len(lines) == 2:
        try:
            a, b = sorted(int(float(v)) for v in lines)
            if a >= 1:
                return ["lines", a, b]
        except (TypeError, ValueError):
            pass
    return None


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
                if not k.startswith("_") and k not in cols:
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
        data["_loc"] = parse_loc(r)       # 位置信息，不是列；界面用它高亮原图/原文
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
                    if shots[0].get("text") is not None:          # 文本记录：每条单独一次请求
                        cols, rows = recognize_text(self.settings, shots[0]["text"])
                        text = ""
                    else:
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
