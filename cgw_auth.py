# -*- coding: utf-8 -*-
"""cgwimg 授权登录：申请授权、轮询直到拿到 Key。不依赖界面。"""
import json
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from app_config import BROWSER_UA, CGW_APP_ID, CGW_SITE, log

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
