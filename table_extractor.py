# -*- coding: utf-8 -*-
"""
截图表格提取工具（Windows）—— 界面层。

业务逻辑分在同目录的模块里：
- app_config.py：路径、设置读写、日志
- hotkeys.py：全局快捷键、粘贴按键
- capture.py：全屏/窗口截图
- recognize.py：WebP 转换、接口调用、表格解析、后台识别线程
- cgw_auth.py：cgwimg 授权登录
- model.py：结果表的纯数据逻辑

界面分两个标签页：
- 「截图」：顶部工具栏（选择窗口 / 截图 / 导入图片 / 清空截图），左侧截图列表（右键可删除），右侧原图（双击放大）。
- 「识别结果」：识别操作、粘贴模式，左侧结果表，右侧详细。

依赖：pip install pillow（tkinter 随 Python 自带）。
"""

import ctypes
import os
import queue
import threading
import time
import traceback
import tkinter as tk
import webbrowser
from tkinter import ttk, messagebox, filedialog

from PIL import Image, ImageDraw, ImageGrab, ImageTk

from app_config import (APP_NAME, AUTO_INTERVAL_SEC, CGW_MODEL, IMAGE_EXTS, LOG_PATH, PROVIDER_CGW,
                        PROVIDER_CUSTOM, PROVIDER_LABELS, clamp_int, is_configured, load_settings, log,
                        one_line, parse_columns, save_settings)
from capture import capture_screen, capture_window, list_windows, save_shot, window_alive
from cgw_auth import AuthThread
from hotkeys import (_MODIFIER_KEYSYMS, HotkeyThread, hotkey_from_event, parse_hotkey, send_ctrl_v,
                     wait_modifiers_released)
from model import export_csv, reindex_done, row_key
from recognize import BatchWorker, chunk

# 同一张图里的行用这些颜色成组（浅色底，便于看清）
GROUP_COLORS = ["#e8f1ff", "#fff4e0", "#e9f8ee", "#f6e9ff", "#ffe9ea", "#e6f7f9"]


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
        self.paste_on = False
        self.paste_start = None     # 右键设置的粘贴起点，存 (截图id, 行号, 列名)，None 表示从头开始
        self.done = set()           # {(截图id, 行号, 列名)} 已粘贴的单元格
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
        self.settings_win = None    # 设置窗口打开期间为 True，全局热键暂停，避免录制时误触发截图/粘贴

        root.title(APP_NAME)
        root.geometry("1240x740")
        root.minsize(900, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        top = ttk.Frame(root, padding=(8, 6))
        top.pack(fill="x")
        ttk.Button(top, text="设置", command=self.open_settings).pack(side="right")
        self.lbl_hotkeys = ttk.Label(top, foreground="#666")
        self.lbl_hotkeys.pack(side="left")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=4)
        self.tab_shots = ttk.Frame(self.notebook, padding=(8, 6, 8, 6))
        self.tab_run = ttk.Frame(self.notebook, padding=(8, 6, 8, 6))
        self.notebook.add(self.tab_shots, text="截图")
        self.notebook.add(self.tab_run, text="识别结果")
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

    # ---------- 布局：两个标签页
    #   「截图」：顶部工具栏（选择窗口 / 截图 / 导入 / 清空截图），左侧截图列表，右侧原图
    #   「识别结果」：识别操作和粘贴模式，左侧结果表，右侧详细
    def build_shots_tab(self):
        page = self.tab_shots
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 6))
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
        ttk.Button(bar, text="文本识别…", command=self.text_input).pack(side="left", padx=6)
        ttk.Button(bar, text="清空截图", command=self.clear_shots).pack(side="left", padx=6)
        # Ctrl+V 剪贴板有图片时导入；没有图片就不拦截，文本粘贴照常工作
        self.root.bind_all("<Control-v>", self.on_ctrl_v, add="+")

        pane = ttk.PanedWindow(page, orient="horizontal")
        pane.pack(fill="both", expand=True)

        # 左：截图列表
        lf = ttk.Frame(pane)
        pane.add(lf, weight=1)
        ttk.Label(lf, text="截图列表（可多选，右键删除，双击放大）").pack(anchor="w")
        lbox = ttk.Frame(lf)
        lbox.pack(fill="both", expand=True)
        lbox.rowconfigure(0, weight=1)
        lbox.columnconfigure(0, weight=1)
        self.shot_tree = ttk.Treeview(lbox, columns=("no", "time", "size", "state"), show="headings",
                                      selectmode="extended")
        for cid, text, w in (("no", "序号", 46), ("time", "时间", 66), ("size", "尺寸", 96),
                             ("state", "状态", 66)):
            self.shot_tree.heading(cid, text=text)
            self.shot_tree.column(cid, width=w, stretch=False)
        shot_vsb = ttk.Scrollbar(lbox, orient="vertical", command=self.shot_tree.yview)
        self.shot_tree.configure(yscrollcommand=shot_vsb.set)
        self.shot_tree.grid(row=0, column=0, sticky="nsew")
        shot_vsb.grid(row=0, column=1, sticky="ns")
        self.shot_tree.bind("<<TreeviewSelect>>", self.on_shot_select)
        self.shot_tree.bind("<Double-1>", self.on_shot_double)
        self.shot_tree.bind("<Button-3>", self.on_shot_right_click)

        # 右：原图
        imgf = ttk.Frame(pane, padding=(8, 0, 0, 0))
        pane.add(imgf, weight=2)
        ttk.Label(imgf, text="原图（双击放大）").pack(anchor="w")
        self.src_label = ttk.Label(imgf, anchor="center", text="选中截图或结果行查看原图",
                                   foreground="#888", cursor="hand2")
        self.src_label.pack(fill="both", expand=True)
        self.src_label.bind("<Double-1>", self.on_source_double)
        self.src_text = self._make_src_text(imgf)     # 文本记录：显示聊天原文，与图片互换

        self.src_shot_id = None

    @staticmethod
    def _make_src_text(parent):
        """右侧预览的文本版本（只读）：文本记录选中时显示原文，平时不显示。"""
        return tk.Text(parent, wrap="word", state="disabled", background="#fafafa",
                       relief="solid", borderwidth=1, padx=6, pady=4)

    def build_run_tab(self):
        page = self.tab_run

        # 提取设置（原在「设置」窗口）：放在操作栏上方，修改后失焦或回车即保存
        fmt = ttk.Frame(page)
        fmt.pack(fill="x", pady=(0, 4))
        ttk.Label(fmt, text="提取格式（列名，逗号分隔，留空按截图表头）").pack(side="left")
        self.var_columns = tk.StringVar(value=self.settings["columns"])
        ent_cols = ttk.Entry(fmt, textvariable=self.var_columns, width=28)
        ent_cols.pack(side="left", padx=(4, 16))
        ttk.Label(fmt, text="额外提取要求").pack(side="left")
        self.var_extra = tk.StringVar(value=self.settings["extra_prompt"])
        ent_extra = ttk.Entry(fmt, textvariable=self.var_extra)
        ent_extra.pack(side="left", fill="x", expand=True, padx=(4, 0))
        for ent in (ent_cols, ent_extra):
            ent.bind("<FocusOut>", self.on_extract_changed)
            ent.bind("<Return>", self.on_extract_changed)

        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 6))
        self.btn_run_pending = ttk.Button(bar, text="识别", command=self.run_pending)
        self.btn_run_pending.pack(side="left")
        self.btn_rerun = ttk.Button(bar, text="重新识别全部", command=self.rerun_all)
        self.btn_rerun.pack(side="left", padx=6)
        ttk.Label(bar, text="每次请求张数").pack(side="left", padx=(16, 4))
        self.var_batch = tk.StringVar(value=str(self.settings["batch_size"]))
        ttk.Spinbox(bar, from_=1, to=20, width=4, textvariable=self.var_batch).pack(side="left")
        self.var_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="自动识别", variable=self.var_auto,
                        command=self.toggle_auto).pack(side="left", padx=(16, 0))
        self.lbl_progress = ttk.Label(bar, text="")
        self.lbl_progress.pack(side="right")

        ops = ttk.Frame(page)
        ops.pack(fill="x", pady=(0, 6))
        self.btn_paste = ttk.Button(ops, text="粘贴模式：关", command=self.toggle_paste)
        self.btn_paste.pack(side="left")
        ttk.Button(ops, text="清空识别结果", command=self.clear_results).pack(side="left", padx=6)
        ttk.Button(ops, text="导出表格…", command=self.export_table).pack(side="left", padx=6)
        ttk.Label(ops, text="点击行查看来源截图、原图和详细", foreground="#888").pack(side="right")

        self.lbl_paste_hint = ttk.Label(page, foreground="#555", justify="left")
        self.lbl_paste_hint.pack(fill="x", pady=(0, 6))

        pane = ttk.PanedWindow(page, orient="horizontal")
        pane.pack(fill="both", expand=True)

        # 左：结果表
        rf = ttk.Frame(pane)
        pane.add(rf, weight=3)
        rbox = ttk.Frame(rf)
        rbox.pack(fill="both", expand=True)
        rbox.rowconfigure(0, weight=1)
        rbox.columnconfigure(0, weight=1)
        self.res_tree = ttk.Treeview(rbox, show="headings", selectmode="browse")
        vsb = ttk.Scrollbar(rbox, orient="vertical", command=self.res_tree.yview)
        hsb = ttk.Scrollbar(rbox, orient="horizontal", command=self.res_tree.xview)
        self.res_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.res_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.res_tree.bind("<<TreeviewSelect>>", self.on_result_select)
        self.res_tree.bind("<Double-1>", self.on_double_click)
        self.res_tree.bind("<Button-1>", self.on_result_click)
        self.res_tree.bind("<Button-3>", self.on_result_right_click)

        # 右：原图（上）+ 详细（下）
        rt = ttk.PanedWindow(pane, orient="vertical")
        pane.add(rt, weight=2)
        imgf = ttk.Frame(rt, padding=(8, 0, 0, 4))
        ttk.Label(imgf, text="原图（双击放大）").pack(anchor="w")
        self.src_label2 = ttk.Label(imgf, anchor="center", text="选中结果行查看原图",
                                    foreground="#888", cursor="hand2")
        self.src_label2.pack(fill="both", expand=True)
        self.src_label2.bind("<Double-1>", self.on_source_double)
        self.src_text2 = self._make_src_text(imgf)
        rt.add(imgf, weight=3)

        detf = ttk.Frame(rt, padding=(8, 0, 0, 0))
        rt.add(detf, weight=2)
        ttk.Label(detf, text="详细").pack(anchor="w")
        df = ttk.Frame(detf)
        df.pack(fill="both", expand=True)
        self.detail = tk.Text(df, height=8, wrap="word", state="disabled",
                              background="#fafafa", relief="solid", borderwidth=1, padx=6, pady=4)
        dsb = ttk.Scrollbar(df, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dsb.set)
        self.detail.pack(side="left", fill="both", expand=True)
        dsb.pack(side="left", fill="y")
        self.detail.tag_configure("key", foreground="#555", font=("Microsoft YaHei UI", 9, "bold"))
        self.detail.tag_configure("val", font=("Microsoft YaHei UI", 10))
        self.detail.tag_configure("meta", foreground="#888")
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
        self.lbl_paste_hint.config(text=(
            f"粘贴模式开启后，按粘贴快捷键（{self.settings['paste_hotkey'].upper()}）即可把下一格内容粘贴到当前窗口，"
            "粘贴完自动跳到下一格。\n"
            "右键单元格可设为粘贴起点（标 ▶）；双击单元格可手动修改识别结果。"))

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
            self.hk.stop()
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
        if kind.startswith("hotkey_") and self.settings_win is not None:
            return                      # 设置窗口打开时忽略热键（防止关闭前已排队的事件生效）
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
                s["failed_at"] = time.time()
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
            self.refresh_shots()
            self.refresh_results()
            failed = sum(1 for s in self.shots if s["state"] == "失败")
            self.say("识别结束。" + (f"有 {failed} 张失败，可点「识别」重试。" if failed else ""))

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
            elif not window_alive(target):
                self.clear_target()
                return self.say("指定的窗口已关闭，已恢复为全屏。请重新截图。")
            else:
                path = capture_window(target)
        except Exception as e:
            log("capture failed:\n" + traceback.format_exc())
            return self.say("截图失败：" + str(e))
        self.add_shot(path)
        what = f"窗口「{target.title[:30]}」" if target else "全屏"
        self.say(f"已截取{what}，共 {len(self.shots)} 张。")

    def on_ctrl_v(self, event):
        """焦点不在文本输入控件时，且剪贴板确实有图片时才接管 Ctrl+V。"""
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

    # ---------- 文本识别
    def text_input(self):
        """弹出输入框，粘贴微信聊天记录；点「提取」后加入截图列表并立即识别。"""
        win = tk.Toplevel(self.root)
        win.title("文本识别")
        win.geometry("560x420")
        win.transient(self.root)
        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="可以复制微信聊天记录，粘贴到下面的输入框，点「提取」自动识别。",
                  foreground="#555").pack(anchor="w")
        box = tk.Text(frm, wrap="word", relief="solid", borderwidth=1, padx=6, pady=4)
        box.pack(fill="both", expand=True, pady=(8, 8))
        box.focus_set()

        def submit():
            text = box.get("1.0", "end").strip()
            if not text:
                return messagebox.showwarning("提示", "请先粘贴聊天记录", parent=win)
            win.destroy()
            self.add_text_shot(text)

        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="提取", command=submit).pack(side="right", padx=6)

    def add_text_shot(self, text):
        """文本记录与截图同列表：没有图片文件，path 为 None，text 为聊天原文。"""
        self.shots.append({"id": self._next_id, "path": None, "text": text,
                           "time": time.strftime("%H:%M:%S"), "size": f"文本 {len(text)} 字",
                           "state": "待识别", "rows": [], "cols": [], "msg": ""})
        self._next_id += 1
        self.refresh_shots()
        self.shot_tree.selection_set(str(self.shots[-1]["id"]))
        self.shot_tree.see(str(self.shots[-1]["id"]))
        if self.worker is None:
            self._start_batches([self.shots[-1]], "文本识别")
        else:
            self.say("已加入文本记录，等当前识别结束后处理。")

    def shot_color(self, sid):
        """截图的底色：按截图在列表中的位置取色。截图列表和结果行用同一个函数，颜色始终一致。"""
        idx = next((i for i, s in enumerate(self.shots) if s["id"] == sid), 0)
        return GROUP_COLORS[idx % len(GROUP_COLORS)]

    def apply_shot_colors(self):
        for s in self.shots:
            tag = f"shot{s['id']}"
            self.shot_tree.tag_configure(tag, background=self.shot_color(s["id"]))
            self.res_tree.tag_configure(tag, background=self.shot_color(s["id"]))

    def refresh_shots(self):
        sel = set(self.shot_tree.selection())
        self.shot_tree.delete(*self.shot_tree.get_children())
        for i, s in enumerate(self.shots, start=1):
            self.shot_tree.insert("", "end", iid=str(s["id"]), tags=(f"shot{s['id']}",),
                                  values=(i, s["time"], s["size"], s["state"]))
        for iid in sel:
            if self.shot_tree.exists(iid):
                self.shot_tree.selection_add(iid)
        self.apply_shot_colors()
        self.update_mode()

    def on_shot_select(self, _=None):
        sel = self.selected_shot_ids()
        if sel and sel[-1] != self.src_shot_id and self.shot_by_id(sel[-1]):
            self.show_source(sel[-1])

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

    def on_shot_right_click(self, event):
        iid = self.shot_tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.shot_tree.selection():
            self.shot_tree.selection_set(iid)
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="删除选中截图", command=self.delete_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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
        self.show_source(None)
        self.say(f"已删除 {len(ids)} 张，剩余 {len(self.shots)} 张")

    def clear_shots(self):
        if not self.shots:
            return self.say("截图列表已经是空的")
        if self.worker is not None:
            return self.say("正在识别中，请等待识别结束")
        if not messagebox.askyesno("确认", f"清空全部 {len(self.shots)} 张截图（同时清空识别结果）？",
                                   parent=self.root):
            return
        for s in self.shots:
            self.remove_file(s["path"])
        self.shots = []
        self.done.clear()
        self.paste_start = None
        self.refresh_shots()
        self.refresh_results()
        self.show_source(None)
        self.say("截图和识别结果已清空")

    def clear_results(self):
        """只清空识别结果，保留截图；截图状态回到「待识别」。"""
        if self.worker is not None:
            return self.say("正在识别中，请等待识别结束")
        for s in self.shots:
            s.update(state="待识别", rows=[], cols=[], msg="")
        self.done.clear()
        self.paste_start = None
        self.refresh_shots()
        self.refresh_results()
        self.say("识别结果已清空，截图已保留")

    def export_table(self):
        """把当前结果表（所有列、所有行，按界面顺序）导出为 CSV。"""
        if not self.result_rows:
            return self.say("没有可导出的结果")
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出表格", defaultextension=".csv",
            filetypes=[("CSV 表格（可用 Excel 打开）", "*.csv")],
            initialfile=f"识别结果_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        if not path:
            return
        try:
            export_csv(path, self.columns, [r["data"] for r in self.result_rows])
        except OSError as e:
            log("export failed:\n" + traceback.format_exc())
            return self.say("导出失败：" + str(e))
        self.say(f"已导出 {len(self.result_rows)} 行到 {path}")

    @staticmethod
    def remove_file(path):
        if not path:                    # 文本记录没有文件
            return
        try:
            os.remove(path)
        except OSError:
            pass

    def shot_by_id(self, sid):
        return next((s for s in self.shots if s["id"] == sid), None)

    def on_extract_changed(self, _=None):
        cols = self.var_columns.get().strip()
        extra = self.var_extra.get().strip()
        if cols == self.settings["columns"] and extra == self.settings["extra_prompt"]:
            return
        self.settings["columns"] = cols
        self.settings["extra_prompt"] = extra
        save_settings(self.settings)
        self.refresh_results()
        self.say("提取格式已保存，下次识别生效。" if cols or extra else "提取设置已清空。")

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
        """放大查看：全尺寸，超出窗口时可滚动拖动；Ctrl+滚轮缩放。文本记录没有图片，不放大。"""
        if not path:
            return
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
            return self.say("正在识别中，请等待识别结束")
        if not is_configured(self.settings):
            return self.open_settings()
        if not shots:
            return self.say("没有需要识别的截图")
        n = clamp_int(self.var_batch.get(), 1, 20, 3)
        if n != self.settings["batch_size"]:
            self.settings["batch_size"] = n
            save_settings(self.settings)
        # 图片按「每次请求张数」合并；文本记录各自一次请求，不和图片混在一起
        imgs = [s for s in shots if s.get("text") is None]
        texts = [s for s in shots if s.get("text") is not None]
        batches = chunk([{"id": s["id"], "path": s["path"], "text": None} for s in imgs], n)
        batches += [[{"id": s["id"], "path": None, "text": s["text"]}] for s in texts]
        for s in shots:
            s["state"] = "等待"
        self.cancel.clear()
        self.worker = BatchWorker(dict(self.settings), batches, self.q, self.cancel)
        self.worker.start()
        self.auto_last_check = time.time()
        self.refresh_shots()
        self.say(f"{label}：{len(shots)} 张，分 {len(batches)} 次请求，每次最多 {n} 张")

    def run_pending(self):
        """识别所有未识别（待识别/失败）的截图。"""
        shots = [s for s in self.shots if s["state"] in ("待识别", "失败")]
        if not shots:
            return self.say("没有待处理的截图")
        self._start_batches(shots, "识别")

    def rerun_all(self):
        """重新识别全部截图（覆盖已有结果）。"""
        if not self.shots:
            return self.say("还没有截图")
        if self.worker is not None:
            return self.say("正在识别中，请等待识别结束")
        if any(s["state"] == "已识别" for s in self.shots) and not messagebox.askyesno(
                "确认", "重新识别会覆盖已有结果，继续吗？", parent=self.root):
            return
        self.done.clear()
        for s in self.shots:
            s.update(rows=[], cols=[], msg="")
        self._start_batches(list(self.shots), "重新识别全部")

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
        # 待识别的截图，以及失败超过 AUTO_INTERVAL_SEC 秒的截图，都会被自动识别。
        # 失败的截图要冷却一段时间再重试：接口持续出错时不会每次检查都立刻重发。
        now = time.time()
        pending = [s for s in self.shots if s["state"] == "待识别"
                   or (s["state"] == "失败" and now - s.get("failed_at", 0) >= AUTO_INTERVAL_SEC)]
        if not pending:
            self.auto_last_check = now
            return
        # 触发条件：待处理截图攒够「每次请求张数」就识别一次（只取最早的 n 张）；
        # 不足 n 张时，每 AUTO_INTERVAL_SEC 秒把剩下的全部识别一次。
        n = clamp_int(self.var_batch.get(), 1, 20, 3)
        if len(pending) >= n:
            self._start_batches(pending[:n], f"自动识别（已攒够 {n} 张）")
        elif now - self.auto_last_check >= AUTO_INTERVAL_SEC:
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
            key = row_key(r["data"], cols)
            seen[key] = seen.get(key, 0) + 1
        for n, r in enumerate(self.result_rows):
            key = row_key(r["data"], cols)
            dup = seen[key] > 1
            tag = f"shot{r['shot_id']}"
            vals = [f"{n + 1}{' ⚠重复' if dup else ''}"] + [
                self.disp(n, c, r["data"].get(c, "")) for c in cols]
            self.res_tree.insert("", "end", iid=str(n), values=vals, tags=(tag,))
        self.apply_shot_colors()
        self.update_progress()

    def dkey(self, n, col):
        """已处理标记的稳定键：用截图 id + 行在该截图内的序号 + 列名，不随行号变化。"""
        r = self.result_rows[n]
        return (r["shot_id"], r["idx"], col)

    def disp(self, n, col, value):
        shown = one_line(value)
        key = self.dkey(n, col)
        if value and key in self.done:
            shown = "✓ " + shown
        if key == self.paste_start:
            shown = "▶ " + shown
        return shown

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
        """下一个可粘贴的单元格：从粘贴起点开始向后找，到末尾后回到开头。
        所在列开关必须开启，且有内容、未粘贴。"""
        cells = [(n, c) for n in range(len(self.result_rows)) for c in self.columns]
        start = 0
        if self.paste_start is not None:
            start = next((i for i, (n, c) in enumerate(cells) if self.dkey(n, c) == self.paste_start), 0)
        for n, c in cells[start:] + cells[:start]:
            if not self.col_enabled.get(c, True):
                continue
            if self.result_rows[n]["data"].get(c, "") and self.dkey(n, c) not in self.done:
                return n, c
        return None

    def on_result_select(self, _=None):
        sel = self.res_tree.selection()
        if not sel:
            return
        n = int(sel[0])
        sid = self.result_rows[n]["shot_id"]
        self.show_source(sid, self.result_rows[n]["data"].get("_loc"))
        if self.shot_tree.exists(str(sid)):
            self.shot_tree.selection_set(str(sid))
            self.shot_tree.see(str(sid))
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
        if row_key(r["data"], self.columns) in self.duplicate_keys():
            self.detail.insert("end", "⚠ 该行内容与其他行重复", "meta")
        self.detail.config(state="disabled")

    def shot_index(self, sid):
        return next((i for i, s in enumerate(self.shots, start=1) if s["id"] == sid), "?")

    def duplicate_keys(self):
        counts = {}
        for r in self.result_rows:
            k = row_key(r["data"], self.columns)
            counts[k] = counts.get(k, 0) + 1
        return {k for k, v in counts.items() if v > 1}

    def show_source(self, sid, loc=None):
        """在「截图」页和「识别结果」页同时显示来源截图（或文本原文）。
        loc 为结果行的位置：图片框或文本行号范围，用来高亮；为 None 时不高亮。"""
        s = self.shot_by_id(sid) if sid is not None else None
        self.src_shot_id = sid
        self._show_thumb(self.src_label, self.src_text, s, sid, (360, 420), "_photo_src", loc)
        self._show_thumb(self.src_label2, self.src_text2, s, sid, (360, 260), "_photo_src2", loc)

    def _show_thumb(self, label, text_w, s, sid, box, attr, loc=None):
        """右侧预览：图片记录显示缩略图（按 loc 画框）；文本记录显示原文（按 loc 高亮行）。"""
        if s and s.get("text") is not None:
            label.pack_forget()
            text_w.pack(fill="both", expand=True)
            text_w.config(state="normal")
            text_w.delete("1.0", "end")
            text_w.insert("1.0", s["text"])
            text_w.tag_remove("hl", "1.0", "end")
            if loc and loc[0] == "lines":
                nlines = s["text"].count("\n") + 1
                a, b = min(loc[1], nlines), min(loc[2], nlines)
                text_w.tag_configure("hl", background="#ffe066")
                text_w.tag_add("hl", f"{a}.0", f"{b}.end")
                text_w.see(f"{a}.0")
            text_w.config(state="disabled")
            return
        text_w.pack_forget()
        label.pack(fill="both", expand=True)
        if not s:
            label.config(image="", text="选中截图或结果行查看原图" if sid is None else "来源截图已删除")
            return
        try:
            im = Image.open(s["path"])
            im.thumbnail(box)
            if loc and loc[0] == "box":
                im = im.convert("RGB")
                w, h = im.size
                x1, y1, x2, y2 = loc[1:]
                ImageDraw.Draw(im).rectangle(
                    [x1 * w / 1000, y1 * h / 1000, x2 * w / 1000, y2 * h / 1000],
                    outline=(255, 40, 40), width=max(2, w // 120))
            # 必须保留 PhotoImage 的引用，否则会被垃圾回收而不显示
            setattr(self, attr, ImageTk.PhotoImage(im.convert("RGB")))
            label.config(image=getattr(self, attr), text="")
        except OSError as e:
            label.config(image="", text=f"无法显示：{e}")

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
        col_id = self.res_tree.identify_column(event.x)
        if self.columns:
            # 序号列按第一列计算；数据列从 "#2" 开始
            idx = 0 if col_id in ("#0", "#1") else int(col_id[1:]) - 2
            if 0 <= idx < len(self.columns):
                cname = self.columns[idx]
                menu.add_command(label=f"从这里开始粘贴（{cname}）",
                                 command=lambda: self.set_paste_start(n, cname))
                menu.add_separator()
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
        self.paste_start = None
        self.refresh_shots()
        self.refresh_results()

    def _reindex_shot(self, sid, removed_idx):
        """removed_idx 为已删除的行号（删除前的原序号）。"""
        self.done = reindex_done(self.done, sid, removed_idx)

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

    def set_paste_start(self, n, col):
        self.paste_start = self.dkey(n, col)
        self.refresh_results()
        self.say(f"粘贴起点已设为第{n + 1}行 [{col}]（标 ▶）。按粘贴快捷键从这里开始粘贴。")

    # ---------- 粘贴模式
    def toggle_paste(self):
        self.paste_on = not self.paste_on
        self.btn_paste.config(text="粘贴模式：开" if self.paste_on else "粘贴模式：关")
        self.say("粘贴模式已开启：按粘贴快捷键，把下一个未填单元格粘贴到当前窗口。" if self.paste_on
                 else "粘贴模式已关闭，粘贴快捷键不再生效。")

    def on_paste_hotkey(self):
        if not self.paste_on:
            return self.say("粘贴模式未开启：请在「识别结果」页点击「粘贴模式」后再使用粘贴快捷键")
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

        # 打开设置时先注销全局热键：否则按旧快捷键会直接截图，而且录制新快捷键时会被系统吞掉。
        # 关闭（保存、取消或点 ×）时按已保存的设置重新注册；取消时就恢复到原来的快捷键。
        self.settings_win = win
        self.stop_hotkeys()

        def close_settings():
            self.settings_win = None
            self.start_hotkeys()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close_settings)

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

        # 提取格式、额外提取要求已移到「识别结果」页，识别按钮上方
        field(8, "图片 WebP 质量", "webp_quality", "1-100，默认 85")

        def save():
            new = dict(s)
            for k, var in vars_.items():
                new[k] = var.get().strip()
            new["provider"] = (PROVIDER_CGW if prov_var.get() == PROVIDER_LABELS[PROVIDER_CGW]
                               else PROVIDER_CUSTOM)
            if new["provider"] == PROVIDER_CGW:
                new["model"] = CGW_MODEL          # cgwimg 固定模型，不接受界面输入
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
            self.settings_win = None
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=10, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="取消", command=close_settings).pack(side="right")
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
