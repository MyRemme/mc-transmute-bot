#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gui.py — 嬗变台自动刷取 图形界面

双击 gui.bat 或 python gui.py 打开。所有操作都在窗口里点点点，不用命令行。
"""
from __future__ import annotations

import os
import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

import mcb
from hotkeys import InputListener, format_tokens, key_identity, key_token, normalize

NL = chr(10)
FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_T = ("Microsoft YaHei UI", 11, "bold")
LOG_FONT = ("Consolas", 9)


class Overlay:
    """全屏半透明遮罩，用 grab_set 抢输入，保证拖框一定生效。"""

    @staticmethod
    def pick_rect(master, hint="拖一个矩形框住目标区域"):
        top = tk.Toplevel(master)
        top.attributes("-topmost", True)
        try:
            top.attributes("-fullscreen", True)
        except Exception:
            top.geometry(f"{master.winfo_screenwidth()}x{master.winfo_screenheight()}+0+0")
        try:
            top.attributes("-alpha", 0.30)
        except Exception:
            pass
        top.configure(bg="#101010", cursor="crosshair")
        cv = tk.Canvas(top, bg="#101010", highlightthickness=0)
        cv.pack(fill="both", expand=True)
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        cv.create_text(sw // 2, 40, anchor="n", fill="#00ff88", font=("Microsoft YaHei UI", 20, "bold"), text=hint)
        cv.create_text(sw // 2, 80, anchor="n", fill="#dddddd", font=FONT,
                       text="按住左键拖拽 · 松开确认 · Esc 取消")
        top.deiconify()
        top.lift()
        top.focus_force()
        try:
            top.grab_set()          # 关键：独占输入，不再被游戏抢走
        except Exception:
            pass

        res = {"box": None}

        def down(e):
            res["x0"], res["y0"] = e.x, e.y
            res["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#00ff88", width=2)

        def move(e):
            if res.get("rect"):
                cv.coords(res["rect"], res["x0"], res["y0"], e.x, e.y)

        def up(e):
            x0, y0 = res.get("x0", e.x), res.get("y0", e.y)
            x1, y1 = e.x, e.y
            if x1 < x0:
                x0, x1 = x1, x0
            if y1 < y0:
                y0, y1 = y1, y0
            if x1 - x0 >= 4 and y1 - y0 >= 4:
                res["box"] = [int(x0), int(y0), int(x1), int(y1)]
            top.grab_release()
            top.destroy()

        cv.bind("<Button-1>", down)
        cv.bind("<B1-Motion>", move)
        cv.bind("<ButtonRelease-1>", up)
        top.bind("<Escape>", lambda e: (top.grab_release(), top.destroy()))
        master.wait_window(top)
        return res["box"]

    @staticmethod
    def pick_point(master, img: Image.Image, hint="在物品上点一下"):
        W, H = img.size
        s = min(1.0, 1400 / W, 800 / H)
        disp = img.resize((int(W * s), int(H * s)), Image.LANCZOS) if s < 1 else img
        top = tk.Toplevel(master)
        top.title("点选物品")
        top.attributes("-topmost", True)
        tkimg = ImageTk.PhotoImage(disp)
        cv = tk.Canvas(top, width=disp.size[0], height=disp.size[1],
                       highlightthickness=0, cursor="crosshair")
        cv.pack()
        cv.create_image(0, 0, anchor="nw", image=tkimg)
        cv.create_text(8, 8, anchor="nw", fill="#00ff88", font=FONT_B, text=hint)
        top.lift()
        top.focus_force()
        try:
            top.grab_set()
        except Exception:
            pass
        res = {"pt": None}

        def click(e):
            res["pt"] = (int(e.x / s), int(e.y / s))
            try:
                top.grab_release()
            except Exception:
                pass
            top.destroy()

        cv.bind("<Button-1>", click)
        top.bind("<Escape>", lambda e: (top.grab_release(), top.destroy()))
        master.wait_window(top)
        return res["pt"]

    @staticmethod
    def pick_on_image(master, img: Image.Image, hint="拖一个矩形框住单个物品图标"):
        W, H = img.size
        s = min(1.0, 1400 / W, 800 / H)
        disp = img.resize((int(W * s), int(H * s)), Image.LANCZOS) if s < 1 else img
        top = tk.Toplevel(master)
        top.title("框选目标物品")
        top.attributes("-topmost", True)
        tkimg = ImageTk.PhotoImage(disp)
        cv = tk.Canvas(top, width=disp.size[0], height=disp.size[1],
                       highlightthickness=0, cursor="crosshair")
        cv.pack()
        cv.create_image(0, 0, anchor="nw", image=tkimg)
        cv.create_text(8, 8, anchor="nw", fill="#00ff88", font=FONT_B, text=hint)
        top.lift()
        top.focus_force()
        try:
            top.grab_set()
        except Exception:
            pass
        res = {"box": None}

        def down(e):
            res["x0"], res["y0"] = e.x, e.y
            res["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3366", width=2)

        def move(e):
            if res.get("rect"):
                cv.coords(res["rect"], res["x0"], res["y0"], e.x, e.y)

        def up(e):
            x0, y0 = res.get("x0", e.x), res.get("y0", e.y)
            x1, y1 = e.x, e.y
            if x1 < x0:
                x0, x1 = x1, x0
            if y1 < y0:
                y0, y1 = y1, y0
            res["box"] = [int(x0 / s), int(y0 / s), int(x1 / s), int(y1 / s)]

        def ok(_=None):
            top.grab_release()
            top.destroy()

        cv.bind("<Button-1>", down)
        cv.bind("<B1-Motion>", move)
        cv.bind("<ButtonRelease-1>", up)
        top.bind("<Return>", ok)
        top.bind("<Escape>", lambda e: (res.update(box=None), ok()))
        master.wait_window(top)
        return res["box"]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(mcb.APP_NAME)
        self.app_icon = tk.PhotoImage(file=os.path.join(mcb.RESOURCE_DIR, "assets", "app.png"))
        self.iconphoto(True, self.app_icon)
        self.iconbitmap(os.path.join(mcb.RESOURCE_DIR, "assets", "app.ico"))
        self._native_icons = []
        self.after_idle(self._apply_window_icon)
        self.geometry("900x760+80+60")
        self.minsize(760, 700)
        self.configure(bg="#f4f6f7")

        self.cfg = mcb.load_cfg()
        self.q: queue.Queue = queue.Queue()
        self.state = {"stop": False, "paused": False}
        self.worker: threading.Thread | None = None
        self.thumbs: list = []
        self.recording = False
        self.settings_open = False
        self.calibrating = False
        self._window_check_at = 0.0
        self.protocol("WM_DELETE_WINDOW", self.close)

        self._build()
        self.after(80, self._drain)
        self.refresh()
        self._start_hotkeys()
        self.log("接手前请先投料并手动刷新一次。自动模式从当前左侧三个候选开始检查。")

    def _apply_window_icon(self):
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [wt.HWND, wt.UINT]
        user.GetAncestor.restype = wt.HWND
        user.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                    ctypes.c_int, ctypes.c_int, wt.UINT]
        user.LoadImageW.restype = wt.HANDLE
        user.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        user.SendMessageW.restype = wt.LPARAM
        hwnd = user.GetAncestor(self.winfo_id(), 2)
        path = os.path.join(mcb.RESOURCE_DIR, "assets", "app.ico")
        for kind, metric in ((0, 49), (1, 11)):
            size = user.GetSystemMetrics(metric)
            icon = user.LoadImageW(None, path, 1, size, size, 0x10)
            if icon:
                self._native_icons.append(icon)
                user.SendMessageW(hwnd, 0x0080, kind, icon)

    # ------------------------------------------------------------------ UI
    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=FONT, background="#f4f6f7", foreground="#20292c")
        style.configure("TButton", padding=(12, 7), background="#ffffff")
        style.configure("Go.TButton", font=FONT_B, background="#157d70", foreground="white")
        style.map("Go.TButton", background=[("active", "#116658")])
        style.configure("Treeview", rowheight=48, background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=FONT_B, padding=6)
        style.configure("TNotebook.Tab", padding=(18, 8))
        style.configure("TLabelframe", padding=10)
        style.configure("TLabelframe.Label", font=FONT_B)

        head = tk.Frame(self, bg="#20292c")
        head.pack(fill="x")
        self.header_hint = tk.Label(head,
            text="还在为嬗变台刷取发愁？担心刷过头、手动操作累？嬗变台自动刷取，一键搞定！",
            font=FONT_T, fg="white", bg="#20292c", justify="left", anchor="w", wraplength=850)
        self.header_hint.pack(fill="x", padx=18, pady=(12, 4))
        head.bind("<Configure>", lambda event: self.header_hint.configure(wraplength=max(100, event.width - 36)))
        self.lbl_win = tk.Label(head, text="", font=FONT, fg="#b7d9cf", bg="#20292c")
        self.lbl_win.pack(anchor="e", padx=18, pady=(0, 8))

        footer = ttk.Frame(self, padding=(16, 0, 16, 8))
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, text=f"MyRemme · v{mcb.APP_VERSION}", foreground="#59696d").pack(side="left")
        for label, url in (("☆ GitHub 仓库", mcb.REPO_URL), ("B站主页", mcb.BILIBILI_URL)):
            link = ttk.Label(footer, text=label, foreground="#157d70", cursor="hand2", takefocus=True)
            link.pack(side="right", padx=(16, 0))
            link.bind("<Button-1>", lambda event, address=url: webbrowser.open(address))
            link.bind("<Return>", lambda event, address=url: webbrowser.open(address))
        ttk.Label(footer, text="觉得好用可以给我的仓库点Star", foreground="#59696d").pack(side="right", padx=(12, 0))

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        runbar = ttk.Frame(body)
        runbar.pack(fill="x", pady=(0, 12))
        self.btn_start = ttk.Button(runbar, text="▶ 开始刷", style="Go.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_pause = ttk.Button(runbar, text="Ⅱ 暂停", command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(runbar, text="■ 停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")
        ttk.Button(runbar, text="测试识别", command=self.test_vision).pack(side="left", padx=12)
        self.lbl_run = ttk.Label(runbar, text="空闲", font=FONT_B)
        self.lbl_run.pack(side="right")
        self.lbl_metrics = ttk.Label(body, text="", foreground="#59696d")
        self.lbl_metrics.pack(anchor="w", pady=(0, 12))

        tabs = ttk.Notebook(body)
        tabs.pack(fill="both", expand=True)
        targets_tab = ttk.Frame(tabs, padding=12)
        settings_tab = ttk.Frame(tabs, padding=12)
        advanced_tab = ttk.Frame(tabs, padding=12)
        tabs.add(targets_tab, text="目标物品")
        tabs.add(settings_tab, text="标定与设置")
        tabs.add(advanced_tab, text="录制模式")

        row = ttk.Frame(targets_tab)
        row.pack(fill="x", pady=(0, 10))
        ttk.Button(row, text="＋ 添加图片", command=self.add_image).pack(side="left")
        ttk.Button(row, text="截取目标", command=self.grab_target).pack(side="left", padx=6)
        ttk.Button(row, text="点选目标", command=self.pick_item_click).pack(side="left")
        ttk.Button(row, text="移除选中", command=self.remove_targets).pack(side="right")
        self.target_tree = ttk.Treeview(targets_tab, columns=("size",), show="tree headings", selectmode="extended", height=4)
        self.target_tree.heading("#0", text="目标图片")
        self.target_tree.heading("size", text="尺寸")
        self.target_tree.column("#0", width=400, minwidth=180)
        self.target_tree.column("size", width=100, stretch=False, anchor="center")
        scroll = ttk.Scrollbar(targets_tab, orient="vertical", command=self.target_tree.yview)
        self.target_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.target_tree.pack(fill="both", expand=True)
        self.lbl_targets = ttk.Label(targets_tab, text="")
        self.lbl_targets.pack(anchor="w", pady=(8, 0))

        f1 = ttk.Frame(settings_tab)
        f1.pack(fill="x", pady=(0, 14))
        self.lbl_region = ttk.Label(f1, text="未标定", font=FONT_B)
        self.lbl_region.pack(side="left")
        ttk.Button(f1, text="标定三个候选", command=self.mark_slots).pack(side="right")
        ttk.Button(f1, text="标定刷新点", command=self.mark_refill).pack(side="right", padx=6)
        r1 = ttk.Frame(settings_tab)
        r1.pack(fill="x", pady=6)
        ttk.Label(r1, text="匹配阈值").pack(side="left")
        self.var_thr = tk.DoubleVar(value=float(self.cfg.get("threshold", 0.90)))
        ttk.Scale(r1, from_=0.60, to=0.99, variable=self.var_thr,
                  command=lambda v: self._on_thr(), length=180).pack(side="left", padx=8)
        self.lbl_thr = ttk.Label(r1, text=f"{self.var_thr.get():.2f}", width=5)
        self.lbl_thr.pack(side="left")
        ttk.Label(r1, text="刷新等待（毫秒）").pack(side="left", padx=(20, 4))
        self.var_settle = tk.IntVar(value=round(self.cfg.get("settle", .15) * 1000))
        ttk.Spinbox(r1, from_=50, to=3000, increment=10, width=6, textvariable=self.var_settle).pack(side="left")

        options = ttk.Frame(settings_tab)
        options.pack(fill="x", pady=8)
        self.var_top = tk.BooleanVar(value=False)
        self.var_min = tk.BooleanVar(value=True)
        self.var_focus = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="窗口置顶", variable=self.var_top, command=self._apply_top).pack(side="left")
        ttk.Checkbutton(options, text="开刷时最小化", variable=self.var_min).pack(side="left", padx=12)
        ttk.Checkbutton(options, text="自动切到游戏", variable=self.var_focus).pack(side="left")
        row = ttk.Frame(settings_tab)
        row.pack(fill="x", pady=10)
        ttk.Button(row, text="快捷键设置", command=self.open_settings).pack(side="left")
        ttk.Button(row, text="试听成功提示音", command=lambda: mcb.success_sound(self.cfg)).pack(side="left", padx=8)
        self.lbl_hotkeys = ttk.Label(settings_tab, text="", foreground="#59696d", wraplength=650)
        self.lbl_hotkeys.pack(anchor="w", pady=4)

        windowbar = ttk.Frame(settings_tab)
        windowbar.pack(fill="x", pady=(12, 6))
        ttk.Label(windowbar, text="游戏窗口", font=FONT_B).pack(side="left")
        ttk.Button(windowbar, text="手动选择", command=self.choose_window).pack(side="right")
        ttk.Button(windowbar, text="自动识别", command=self.auto_window).pack(side="right", padx=6)
        self.lbl_window_detail = ttk.Label(settings_tab, text="", wraplength=650,
                                           foreground="#59696d")
        self.lbl_window_detail.pack(anchor="w")

        self.var_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(advanced_tab, text="自动模式", variable=self.var_auto).pack(anchor="w", pady=(0, 10))
        self.lbl_macro = ttk.Label(advanced_tab, text="")
        self.lbl_macro.pack(anchor="w", pady=6)
        row = ttk.Frame(advanced_tab)
        row.pack(fill="x")
        self.btn_rec = ttk.Button(row, text="录制循环", command=self.record_loop)
        self.btn_rec.pack(side="left")
        self.btn_feed = ttk.Button(row, text="录制投料", command=lambda: self.record_loop("feed"))
        self.btn_feed.pack(side="left", padx=6)
        ttk.Button(row, text="截图框选", command=lambda: self.pick_region(True)).pack(side="left")
        r1 = ttk.Frame(advanced_tab)
        r1.pack(fill="x", pady=14)
        ttk.Label(r1, text="回放倍速").pack(side="left")
        self.var_speed = tk.DoubleVar(value=float(self.cfg.get("speed", 1.3)))
        ttk.Spinbox(r1, from_=0.5, to=3.0, increment=0.1, width=5,
                    textvariable=self.var_speed).pack(side="left", padx=4)
        ttk.Label(r1, text="收取数量").pack(side="left", padx=(20, 4))
        self.var_want = tk.IntVar(value=0)
        ttk.Spinbox(r1, from_=0, to=999, width=5, textvariable=self.var_want).pack(side="left", padx=4)
        f5 = ttk.Labelframe(body, text="运行日志", padding=6)
        f5.pack(fill="x", pady=(12, 0))
        self.txt = tk.Text(f5, height=7, font=LOG_FONT, bg="#ffffff", fg="#34454b",
                           relief="flat", wrap="word")
        sb = ttk.Scrollbar(f5, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_config("ok", foreground="#147468")
        self.txt.tag_config("warn", foreground="#aa5b16")
        self.txt.tag_config("hit", foreground="#a82c60")

    # ------------------------------------------------------------- helpers
    def log(self, msg: str, tag: str | None = None):
        ts = time.strftime("%H:%M:%S")
        self.txt.insert("end", f"[{ts}] {msg}\n", tag or ())
        self.txt.see("end")
        if int(self.txt.index("end-1c").split(".")[0]) > 1000:
            self.txt.delete("1.0", "200.0")

    def log_async(self, msg: str, tag: str | None = None):
        self.q.put(("log", msg, tag))

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.log(item[1], item[2])
                elif kind == "progress":
                    _, i, hits, rate, score, name = item
                    self.lbl_run.config(text=f"第 {i} 轮 · {rate:.2f} 轮/秒")
                    self.lbl_metrics.config(text=f"命中 {hits} 次    匹配分数 {score:.3f}    {name}")
                elif kind == "done":
                    self._on_done(item[1])
                elif kind == "refresh":
                    self.refresh()
                elif kind == "fatal":
                    messagebox.showerror("游戏窗口看不到", item[1] + NL + NL +
                                         "把游戏窗口点到最前面（不要最小化），再点开始。")
                elif kind == "toggle_pause":
                    self.toggle_pause()
                elif kind == "pause_display":
                    self.pause_display()
                elif kind == "start":
                    if not self.settings_open and not self.recording:
                        self.start()
                elif kind == "stop":
                    self.stop()
                elif kind == "record_done":
                    self.recording = False
                    self.btn_rec.config(state="normal")
                    self.btn_feed.config(state="normal")
                elif kind == "iconify":
                    self.iconify()
                elif kind == "slot_progress":
                    _, i, n, x, y = item
                    hud = getattr(self, "_slot_hud", None)
                    if hud:
                        self._slot_count.config(text=f"已标记 {i}/3 个  ({x}, {y})")
                elif kind == "slot_error":
                    messagebox.showerror("槽位没标对", NL.join(item[1]) + NL + NL + "重新点「标定槽位」，这次鼠标要真的移到每个槽位上。")
                elif kind == "slots_done":
                    self.var_auto.set(True)
                    self.refresh()
                    if item[1]:
                        self.after(800, self.start)
                elif kind == "slots_closed":
                    self.calibrating = False
                    hud = getattr(self, "_slot_hud", None)
                    if hud:
                        hud.destroy()
                        self._slot_hud = None
        except queue.Empty:
            pass
        if time.monotonic() >= self._window_check_at:
            self.refresh_window_status()
            self._window_check_at = time.monotonic() + 2.0
        self.after(80, self._drain)

    def refresh(self):
        cfg = mcb.load_cfg()
        self.cfg = cfg
        n = len(mcb.get_slots(cfg))
        text = f"已标定 {n}/3 格"
        if cfg.get("refill"):
            text += " · 自定刷新点"
        self.lbl_region.config(text=text, foreground="#15803d" if n == 3 else "#b91c1c")

        tg = mcb.load_targets()
        self.lbl_targets.config(text=f"{len(tg)} 张目标图片" if tg else "尚未添加目标图片")
        children = self.target_tree.get_children()
        if children:
            self.target_tree.delete(*children)
        self.thumbs = []
        for name, native, _ in tg:
            h, wd, _g, _m, rgb = native
            im = Image.fromarray(rgb.astype(np.uint8))
            im.thumbnail((36, 36), Image.NEAREST)
            ph = ImageTk.PhotoImage(im)
            self.thumbs.append(ph)
            self.target_tree.insert("", "end", iid=name, text="  " + name,
                                    image=ph, values=(f"{wd} × {h}",))
        self.lbl_hotkeys.config(text="    ".join(f"{label} {mcb.hk(cfg, action).upper()}"
            for action, label in [("start", "开始"), ("pause", "暂停"), ("stop", "停止"), ("mark", "标定")]))

        m = mcb.load_macro("loop")
        if m and m.get("spin"):
            self.lbl_macro.config(
                text=f"✓ 已录制  spin {len(m['spin'])} 事件 / clear {len(m.get('clear', []))} 事件",
                foreground="#15803d")
        else:
            self.lbl_macro.config(text="未录制", foreground="#6b7280")

        self.refresh_window_status()

    # -------------------------------------------------------------- actions
    def refresh_window_status(self):
        hwnd = mcb.find_game_window(self.cfg.get("window_title", ""))
        info = mcb.window_details(hwnd) if hwnd else {}
        selection = self.cfg.get("selected_window")
        if info:
            mode = "已选定" if selection else "自动识别"
            title = info["title"]
            short_title = title if len(title) <= 30 else title[:29] + "…"
            self.lbl_win.config(text=f"● {mode} · {short_title}", fg="#4ade80")
            self.lbl_window_detail.config(text=f"{mode}  {title}\n{info['exe']} · PID {info['pid']} · HWND {hwnd}")
        else:
            self.lbl_win.config(text="○ 请重新选择游戏窗口" if selection else "○ 未找到游戏窗口", fg="#f87171")
            self.lbl_window_detail.config(text="选定的窗口已关闭，请重新选择。" if selection else "尚未检测到 Java / LWJGL 游戏窗口。")

    def _can_select_window(self):
        if self.settings_open or self.recording or self.calibrating or self.worker and self.worker.is_alive():
            self.log("请先结束当前操作，再选择游戏窗口", "warn")
            return False
        return True

    def _bind_window(self, info):
        current = mcb.window_details(info["hwnd"])
        identity = ("pid", "started", "exe", "class")
        if not current or any(current[key] != info[key] for key in identity):
            messagebox.showwarning("窗口已关闭", "该窗口已不可用，请刷新列表后重新选择")
            return False
        previous = mcb.find_game_window(self.cfg.get("window_title", ""))
        cfg = mcb.load_cfg()
        cfg["selected_window"] = {key: current[key] for key in ("hwnd", "title") + identity}
        if previous != current["hwnd"]:
            for key in ("slots", "slots_norm", "watch_norm", "refill", "refill_norm", "client_at_mark"):
                cfg.pop(key, None)
            cfg["watch"] = [0, 0, 0, 0]
        mcb.save_cfg(cfg)
        mcb.set_window_selection(cfg["selected_window"])
        self.refresh()
        self.log(f"已选定 {current['title']}（{current['exe']}，PID {current['pid']}）", "ok")
        if previous != current["hwnd"]:
            self.log("已切换窗口，请重新标定左侧三个候选", "warn")
        return True

    def auto_window(self):
        if not self._can_select_window():
            return
        candidates = mcb.available_game_windows(self.cfg.get("window_title", ""))
        if not candidates:
            self.log("没有找到 Java / LWJGL 游戏窗口，请先打开 Minecraft", "warn")
        elif len(candidates) == 1:
            self._bind_window(candidates[0])
        else:
            self.choose_window()

    def choose_window(self):
        if not self._can_select_window():
            return
        self.settings_open = True
        top = tk.Toplevel(self)
        top.title("选择游戏窗口")
        top.geometry("760x330")
        top.minsize(600, 280)
        top.transient(self)
        body = ttk.Frame(top, padding=12)
        body.pack(fill="both", expand=True)
        controls = ttk.Frame(body)
        controls.pack(side="bottom", fill="x")
        status = ttk.Label(body, text="", foreground="#59696d")
        status.pack(side="bottom", anchor="w", pady=8)
        listing = ttk.Frame(body)
        listing.pack(fill="both", expand=True)
        tree = ttk.Treeview(listing, columns=("exe", "pid"), show="tree headings", selectmode="browse", height=4)
        tree.heading("#0", text="窗口标题")
        tree.heading("exe", text="进程")
        tree.heading("pid", text="PID")
        tree.column("#0", width=420, minwidth=200)
        tree.column("exe", width=100, stretch=False)
        tree.column("pid", width=80, stretch=False)
        scroll = ttk.Scrollbar(listing, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        candidates = {}

        def reload():
            children = tree.get_children()
            if children:
                tree.delete(*children)
            candidates.clear()
            for info in mcb.available_game_windows(self.cfg.get("window_title", "")):
                key = str(info["hwnd"])
                candidates[key] = info
                tree.insert("", "end", iid=key, text=info["title"], values=(info["exe"], info["pid"]))
            children = tree.get_children()
            if children:
                tree.selection_set(children[0])
            status.config(text=f"{len(candidates)} 个 Java / LWJGL 候选窗口")

        def close():
            self.settings_open = False
            top.destroy()

        def select(event=None):
            selected = tree.selection()
            if selected and self._bind_window(candidates[selected[0]]):
                close()

        ttk.Button(controls, text="刷新列表", command=reload).pack(side="left")
        ttk.Button(controls, text="选定窗口", command=select).pack(side="right")
        ttk.Button(controls, text="取消", command=close).pack(side="right", padx=6)
        tree.bind("<Double-1>", select)
        top.bind("<Return>", select)
        top.bind("<Escape>", lambda event: close())
        top.protocol("WM_DELETE_WINDOW", close)
        reload()
        top.grab_set()

    def open_settings(self):
        if self.settings_open:
            return
        if self.recording or self.calibrating or self.worker and self.worker.is_alive():
            self.log("请先停止当前操作，再修改快捷键", "warn")
            return
        self.settings_open = True
        cfg = mcb.load_cfg()
        cur = dict(cfg.get("hotkeys") or {})
        top = tk.Toplevel(self)
        top.title("快捷键设置")
        top.attributes("-topmost", True)
        top.resizable(False, False)
        def close():
            self.settings_open = False
            top.destroy()
        top.protocol("WM_DELETE_WINDOW", close)
        top.configure(bg="#f3f4f6")
        frm = ttk.Frame(top, padding=14)
        frm.pack(fill="both", expand=True)

        items = [("start", "开始刷取"), ("pause", "暂停 / 继续"), ("stop", "停止"),
                 ("record_start", "开始录制"), ("record_mark", "录制打点"),
                 ("record_stop", "结束录制"), ("mark", "标定打点")]
        vs = {}
        for r, (k, label) in enumerate(items):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=5)
            v = tk.StringVar(value=cur.get(k) or mcb.DEFAULT_CFG["hotkeys"][k])
            ttk.Entry(frm, textvariable=v, width=27).grid(row=r, column=1, padx=12)
            ttk.Button(frm, text="录入", command=lambda value=v: self.capture_hotkey(top, value)).grid(row=r, column=2)
            vs[k] = v

        def save():
            try:
                bindings = {key: normalize(value.get()) for key, value in vs.items()}
                if len(set(bindings.values())) != len(bindings):
                    raise ValueError("不同动作需要使用不同快捷键")
            except ValueError as error:
                messagebox.showerror("快捷键无效", str(error), parent=top)
                return
            c = mcb.load_cfg()
            c["hotkeys"] = bindings
            mcb.save_cfg(c)
            self.log("✓ 快捷键已保存：" + "  ".join(f"{lab}={v.get().upper()}"
                     for (k, lab), v in zip(items, vs.values())), "ok")
            self.refresh()
            self._start_hotkeys()
            close()

        ttk.Button(frm, text="保存", command=save).grid(row=len(items), column=1, sticky="e", pady=(12, 0))

    def capture_hotkey(self, master, variable):
        from pynput import keyboard, mouse
        top = tk.Toplevel(master)
        top.title("录入快捷键")
        top.geometry("380x130")
        top.transient(master)
        top.attributes("-topmost", True)
        label = ttk.Label(top, text="按下按键或组合键", anchor="center", font=FONT_B)
        label.pack(fill="both", expand=True, padx=12, pady=12)
        held = {}
        candidate = {"value": None}
        result = queue.Queue()
        capture_lock = threading.Lock()
        def update(identity, token, pressed):
            with capture_lock:
                if not pressed:
                    if identity in held and candidate["value"]:
                        result.put(candidate["value"])
                    held.pop(identity, None)
                    return
                if not token:
                    return
                held[identity] = token
                candidate["value"] = format_tokens(held.values())
        kl = keyboard.Listener(on_press=lambda key: update(key_identity(key), key_token(key), True),
                               on_release=lambda key: update(key_identity(key), key_token(key), False))
        ml = mouse.Listener(on_click=lambda x, y, button, pressed: update(
            ("mouse", button), f"mouse_{button.name}", pressed))
        def close():
            kl.stop()
            ml.stop()
            top.destroy()
        top.protocol("WM_DELETE_WINDOW", close)
        def poll():
            if not top.winfo_exists():
                return
            try:
                value = result.get_nowait()
            except queue.Empty:
                top.after(40, poll)
                return
            if value:
                variable.set(value)
            close()
        kl.start()
        ml.start()
        top.after(40, poll)

    def remove_targets(self):
        selected = self.target_tree.selection()
        if not selected:
            return
        for name in selected:
            os.remove(os.path.join(mcb.TARGET_DIR, name))
        self.refresh()

    def _apply_top(self):
        try:
            self.attributes("-topmost", bool(self.var_top.get()))
        except Exception:
            pass

    def _on_thr(self):
        v = round(self.var_thr.get(), 2)
        self.lbl_thr.config(text=f"{v:.2f}")
        self.cfg["threshold"] = v
        mcb.save_cfg(self.cfg)

    def pick_region(self, by_shot=False):
        self.withdraw()
        self.update()
        try:
            time.sleep(0.25)
            if by_shot:
                mcb.focus_window(self.cfg.get("window_title", ""))
                img, (ox, oy) = mcb.capture_game()
                box = Overlay.pick_on_image(self, img, "拖框框住掉落槽区域")
                if box:
                    box = [box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy]
            else:
                box = Overlay.pick_rect(self, "框住掉落槽区域（1~3 格）")
        except Exception as e:
            self.log(f"截图失败：{e}", "warn")
            return
        finally:
            self.deiconify()
            self.lift()

        if not box:
            self.log("已取消", "warn")
            return
        mcb.save_watch(self.cfg, box)
        self.log(f"槽位区域 = {box}  ({box[2]-box[0]}x{box[3]-box[1]})", "ok")
        self.refresh()

    def test_vision(self):
        """逐个槽位测分数（自动缩下面板、切前台、截图画分）。"""
        cfg = mcb.load_cfg()
        slots = mcb.get_slots(cfg)
        if len(slots) != 3:
            messagebox.showwarning("还差一步", "请先标定左侧三个候选物品")
            return
        targets = mcb.load_targets()
        if not targets:
            messagebox.showwarning("还差一步", "先加一个目标物品图片")
            return
        debug = os.path.join(mcb.HERE, "debug")
        os.makedirs(debug, exist_ok=True)
        thr = float(cfg.get("threshold", 0.9))

        try:
            # 先尝试直接把游戏切到前台；不行再把面板缩下去重试
            title = cfg.get("window_title", "")
            mcb.focus_window(title)
            ok, why = mcb.game_ready()
            if not ok:
                self.withdraw()
                self.update()
                time.sleep(0.35)
                mcb.focus_window(title)
                time.sleep(0.3)
                ok, why = mcb.game_ready()
            if not ok:
                self.log(f"✗ {why}", "warn")
                return

            self.log(f"逐槽位测试（阈值 {thr:.2f}）：", None)
            top = (-1.0, "", 0)
            for idx, (cx, cy) in enumerate(slots, 1):
                img, _off = mcb.grab_patch(cx, cy, 48)
                img.save(os.path.join(debug, f"slot_{idx}.png"))
                s, name, _pos, _sz = mcb.scan(
                    mcb._to_gray(img), targets, thr,
                    rgb=np.asarray(img.convert("RGB"), dtype=np.float32))
                if s > top[0]:
                    top = (s, name, idx)
                tag = "ok" if s >= thr else "warn"
                self.log(f"  槽位{idx} ({cx},{cy})：{s:.3f}  {name}", tag)

            tag = "ok" if top[0] >= thr else "warn"
            self.log(f"最高 {top[0]:.3f} 在槽位{top[2]}  阈值 {thr:.2f} → "
                     + ("检测到目标 ✓" if top[0] >= thr else "当前槽位未检测到目标"), tag)
            self.log(f"{len(slots)} 个槽位的彩色截图已存 debug/slot_1~{len(slots)}.png")
        except Exception as e:
            self.log(f"测试出错：{type(e).__name__}: {e}", "warn")
            messagebox.showerror("测试出错", f"{type(e).__name__}: {e}")
        finally:
            # 无论如何都要把面板收回来
            try:
                self.deiconify()
                self.lift()
            except Exception:
                pass

    def mark_refill(self):
        """允许记录整合包自定义的刷新位置，默认选择第一行候选刷新。"""
        if self.calibrating or self.worker and self.worker.is_alive():
            return
        self.calibrating = True
        self.log(f"标定刷新点：悬停刷新位置，按 {mcb.hk(self.cfg, 'mark').upper()} 保存", "warn")

        def run():
            from pynput import mouse
            got: list = []
            done = threading.Event()
            mark_key = mcb.hk(mcb.load_cfg(), "mark")

            def action(name):
                if name == "cancel":
                    done.set()
                elif name == "mark":
                    p = mouse.Controller().position
                    got.append([int(p[0]), int(p[1])])
                    done.set()
            bindings = {"mark": mark_key}
            if normalize(mark_key) != "esc":
                bindings["cancel"] = "esc"
            with InputListener(bindings, action):
                done.wait()
            if not got:
                self.q.put(("log", "已取消刷新点标定", "warn"))
                self.q.put(("slots_closed",))
                return
            cfg = mcb.load_cfg()
            mcb.save_refill(cfg, got[0])
            self.q.put(("log", f"✓ 刷新按钮 = {got[0]}（三格没目标时点它重抽）", "ok"))
            self.q.put(("refresh",))
            self.q.put(("slots_closed",))

        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def _validate_slots(pos):
        errs = []
        if len(pos) != 3:
            errs.append("需要标定左侧三个候选物品中心")
        for i in range(len(pos)):
            for j in range(i + 1, len(pos)):
                if abs(pos[i][0] - pos[j][0]) < 10 and abs(pos[i][1] - pos[j][1]) < 10:
                    errs.append(f"第 {i+1} 和第 {j+1} 个槽位几乎重合 —— 鼠标没移动？")
        return errs

    def mark_slots(self, then_start=False):
        """悬停 + 空格，依次记录结果槽坐标；右上角有悬浮窗显示进度。"""
        if self.calibrating or self.worker and self.worker.is_alive():
            return
        self.calibrating = True
        n = 3
        done = {"flag": False, "cancelled": False}

        hud = tk.Toplevel(self)
        self._slot_hud = hud
        hud.overrideredirect(True)
        hud.attributes("-topmost", True)
        hud.configure(bg="#111827")
        sw = self.winfo_screenwidth()
        hud.geometry(f"330x118+{sw - 350}+40")
        mark_key = mcb.hk(mcb.load_cfg(), "mark")
        hint = tk.Label(hud, text=f"从上到下标记三个候选物品，按 {mark_key.upper()}，满三格保存",
                        fg="white", bg="#111827", justify="left", wraplength=310,
                        font=("Microsoft YaHei UI", 11, "bold"))
        hint.pack(anchor="w", padx=12, pady=(12, 4))
        cnt = tk.Label(hud, text="已标记 0/3 个  Esc 取消", fg="#4ade80", bg="#111827",
                       font=("Consolas", 10))
        cnt.pack(anchor="w", padx=12)
        self._slot_count = cnt

        def close():
            done["flag"] = True
            done["cancelled"] = True
            self.calibrating = False
            try:
                hud.destroy()
            except Exception:
                pass
            self._slot_hud = None

        ttk.Button(hud, text="取消", command=close).pack(anchor="e", padx=10, pady=6)
        self.log(f"标定槽位：从上到下悬停三个候选，按 {mark_key.upper()}，满三格保存", "warn")

        def run():
            from pynput import mouse
            pos: list = []

            MK = mcb.hk(mcb.load_cfg(), "mark")

            def action(name):
                if name == "cancel":
                    done["flag"] = True
                    done["cancelled"] = True
                    pos.clear()
                    return
                if done["flag"]:
                    return
                p = mouse.Controller().position
                pos.append([int(p[0]), int(p[1])])
                i = len(pos)
                self.q.put(("slot_progress", i, n or 0, int(p[0]), int(p[1])))
                if n and i >= n:
                    done["flag"] = True

            bindings = {"mark": MK}
            if normalize(MK) != "esc":
                bindings["cancel"] = "esc"
            with InputListener(bindings, action):
                while (not n or len(pos) < n) and not done["flag"]:
                    time.sleep(0.05)

            if pos and not done["cancelled"]:
                cfg = mcb.load_cfg()
                errs = self._validate_slots(pos)
                if errs:
                    for e in errs:
                        self.q.put(("log", "✗ " + e, "warn"))
                    self.q.put(("slot_error", errs))
                    self.q.put(("slots_closed",))
                    return
                mcb.save_slots(cfg, pos)
                self.q.put(("log", f"✓ 已保存 {len(pos)} 个槽位：{pos}", "ok"))
                self.q.put(("slots_done", then_start))
            self.q.put(("slots_closed",))

        threading.Thread(target=run, daemon=True).start()

    def add_image(self):
        paths = filedialog.askopenfilenames(
            title="选择目标物品图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.bmp"), ("所有文件", "*.*")])
        if not paths:
            return
        os.makedirs(mcb.TARGET_DIR, exist_ok=True)
        for path in paths:
            name = os.path.splitext(os.path.basename(path))[0] + ".png"
            with Image.open(path) as source:
                source.convert("RGBA").save(os.path.join(mcb.TARGET_DIR, name))
        self.log(f"已加入 {len(paths)} 张目标图片", "ok")
        self.refresh()

    def grab_target(self):
        """截取游戏窗口，然后在图上框出物品图标。"""
        w = mcb.get_watch(self.cfg)
        self.withdraw()
        self.update()
        try:
            time.sleep(0.25)
            mcb.focus_window(self.cfg.get("window_title", ""))
            img, _origin = mcb.capture_game(tuple(w) if w[2] > w[0] else None)
            box = Overlay.pick_on_image(
                self, img, "框住要刷的那个物品图标（框小了更准）")
        except Exception as e:
            self.log(f"截图失败：{e}", "warn")
            return
        finally:
            self.deiconify()
            self.lift()
        if not box:
            self.log("已取消", "warn")
            return
        crop = mcb.autotrim(img.crop(tuple(box))).convert("RGBA")
        os.makedirs(mcb.TARGET_DIR, exist_ok=True)
        name = time.strftime("target_%H%M%S.png")
        crop.save(os.path.join(mcb.TARGET_DIR, name))
        self.log(f"已截取目标 {name}  {crop.size[0]}x{crop.size[1]}", "ok")
        self.refresh()

    def pick_item_click(self):
        """从当前槽位画面里点一下物品，自动裁 34x34 做模板。"""
        cfg = mcb.load_cfg()
        w = mcb.get_watch(cfg)
        self.withdraw()
        self.update()
        try:
            time.sleep(0.25)
            mcb.focus_window(cfg.get("window_title", ""))
            img, _origin = mcb.capture_game(tuple(w) if w[2] > w[0] else None)
            pt = Overlay.pick_point(self, img, "在目标物品图标的中心点一下（底部物品也可以选择）")
        except Exception as e:
            self.log(f"截图失败：{e}", "warn")
            return
        finally:
            self.deiconify()
            self.lift()
        if not pt:
            self.log("已取消", "warn")
            return
        s = 34
        l = max(0, min(img.size[0] - s, pt[0] - s // 2))
        t = max(0, min(img.size[1] - s, pt[1] - s // 2))
        crop = img.crop((l, t, l + s, t + s)).convert("RGBA")
        os.makedirs(mcb.TARGET_DIR, exist_ok=True)
        name = time.strftime("item_%H%M%S.png")
        crop.save(os.path.join(mcb.TARGET_DIR, name))
        self.log(f"✓ 已截取物品模板 {name} ({s}x{s})", "ok")
        self.refresh()
        self.after(300, self.test_vision)

    def clear_targets(self):
        if not messagebox.askyesno("确认", "清空所有目标图片？"):
            return
        for f in os.listdir(mcb.TARGET_DIR):
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                os.remove(os.path.join(mcb.TARGET_DIR, f))
        self.log("已清空目标", "warn")
        self.refresh()

    # ------------------------------------------------------------ recording
    def record_loop(self, name="loop"):
        if self.recording or self.worker and self.worker.is_alive():
            return
        self.recording = True
        self.record_cancel = threading.Event()
        self.btn_rec.config(state="disabled")
        self.btn_feed.config(state="disabled")
        cfg = mcb.load_cfg()
        def run():
            try:
                events, mark = mcb.record_events(cfg, self.log_async, self.record_cancel)
                if events and not self.record_cancel.is_set():
                    a = [e for e in events if mark is None or e[1] <= mark]
                    b = [e for e in events if mark is not None and e[1] > mark]
                    if b:
                        off = b[0][1]
                        for e in b:
                            e[1] = round(e[1] - off, 4)
                    os.makedirs(mcb.MACRO_DIR, exist_ok=True)
                    import json
                    with open(os.path.join(mcb.MACRO_DIR, f"{name}.json"), "w", encoding="utf-8") as f:
                        json.dump({"spin": a, "clear": b, "has_marker": mark is not None}, f)
                    self.q.put(("log", f"录制完成，共 {len(events)} 个事件", "ok"))
            except Exception as error:
                self.log_async(f"录制失败：{error}", "warn")
            finally:
                self.q.put(("record_done",))
                self.q.put(("refresh",))
        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------------- 运行
    def start(self):
        if self.recording or self.settings_open or self.calibrating or self.worker and self.worker.is_alive():
            return
        self.cfg = mcb.load_cfg()
        auto = bool(self.var_auto.get())
        if auto and len(mcb.get_slots(self.cfg)) != 3:
            self.mark_slots(then_start=True)
            return
        if not auto and self.cfg["watch"][2] <= self.cfg["watch"][0]:
            messagebox.showwarning("还差一步", "先点「框选槽位」")
            return
        targets = mcb.load_targets()
        if not targets:
            messagebox.showwarning("还差一步", "先加一个目标物品图片")
            return
        macro = mcb.load_macro("loop")
        if not auto and (not macro or not macro.get("spin")):
            msg = ("还没有可用的循环。" + NL + NL +
                   "要现在标定左侧三个候选物品吗？" + NL + NL +
                   "点「是」→ 我带你标定，标完自动开刷" + NL +
                   "点「否」→ 你自己去点「录制循环」")
            if messagebox.askyesno("还差一步", msg):
                self.mark_slots(then_start=True)
            return

        try:
            ui = {
                "thr": round(self.var_thr.get(), 2),
                "speed": round(self.var_speed.get(), 1),
                "want": int(self.var_want.get()),
                "minimize": bool(self.var_min.get()),
                "focus": bool(self.var_focus.get()),
                "settle": max(50, min(3000, int(self.var_settle.get()))) / 1000,
            }
        except (ValueError, tk.TclError):
            messagebox.showerror("参数无效", "请输入有效的阈值、等待时间、倍速和数量")
            return
        self.cfg["threshold"] = ui["thr"]
        self.cfg["speed"] = ui["speed"]
        self.cfg["settle"] = ui["settle"]
        mcb.save_cfg(self.cfg)

        self.state.clear()
        self.state.update({"stop": False, "paused": False})
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal", text="⏸ 暂停")
        self.btn_stop.config(state="normal")
        self.log("3 秒后开始，请把鼠标交给游戏…（窗口会先缩下去，再切游戏到前台）", "warn")

        def run():
            time.sleep(2.4)
            if self.state["stop"]:
                self.q.put(("done", {"rounds": 0, "hits": 0, "elapsed": 0}))
                return
            if ui["minimize"]:
                self.q.put(("iconify",))
                time.sleep(0.4)
            if ui["focus"]:
                mcb.focus_window(self.cfg.get("window_title", ""))
                time.sleep(0.25)

            ok, why = mcb.game_ready()
            if not ok:
                self.q.put(("log", f"！{why}", "warn"))
                self.q.put(("fatal", why))
                self.q.put(("done", {"rounds": 0, "hits": 0, "elapsed": 0}))
                return
            opts = {
                "threshold": self.cfg["threshold"],
                "speed": self.cfg["speed"],
                "passes": 0,
                "want": ui["want"],
                "keep_going": ui["want"] > 1,
                "dry": False,
            }
            try:
                cb_log = lambda m, t=None: self.q.put(("log", m, t))
                cb_prog = lambda i, h, r, s, n: self.q.put(("progress", i, h, r, s, n))
                if auto:
                    res = mcb.run_auto(self.cfg, targets, self.state, opts,
                                       on_log=cb_log, on_progress=cb_prog)
                else:
                    res = mcb.run_loop(self.cfg, targets, macro, self.state, opts,
                                       on_log=cb_log, on_progress=cb_prog)
            except Exception as e:
                self.q.put(("log", f"出错：{e}", "warn"))
                res = {"rounds": 0, "hits": 0, "elapsed": 0}
            self.q.put(("done", res))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _start_hotkeys(self):
        if getattr(self, "hk", None):
            self.hk.stop()
        cfg = mcb.load_cfg()
        def action(name):
            if self.settings_open or self.recording or self.calibrating:
                return
            if name in ("pause", "stop") and not (self.worker and self.worker.is_alive()):
                return
            if name == "stop":
                self.state["stop"] = True
            if name == "pause":
                self.state["paused"] = not self.state.get("paused", False)
                self.q.put(("pause_display",))
                return
            self.q.put((name,))

        try:
            self.hk = InputListener({name: mcb.hk(cfg, name) for name in ("start", "pause", "stop")}, action).start()
        except Exception as error:
            self.log(f"快捷键加载失败：{error}", "warn")
            self.hk = None

    def _on_done(self, res: dict):
        try:
            self.deiconify()
            self.lift()
        except Exception:
            pass
        self.btn_start.config(state="normal")
        self.btn_pause.config(state="disabled", text="⏸ 暂停")
        self.btn_stop.config(state="disabled")
        hits = res.get("hits", 0)
        msg = f"结束：{res.get('rounds',0)} 轮 / 命中 {hits} 次 / 用时 {res.get('elapsed',0):.1f}s"
        self.log(msg, "hit" if hits else None)
        self.lbl_run.config(text="刷到了！" if hits else "空闲")

    def toggle_pause(self):
        if not (self.worker and self.worker.is_alive()):
            return
        self.state["paused"] = not self.state.get("paused", False)
        self.pause_display()

    def pause_display(self):
        if self.state["paused"]:
            self.btn_pause.config(text="▶ 继续")
            self.lbl_run.config(text="已暂停")
        else:
            self.btn_pause.config(text="⏸ 暂停")

    def stop(self):
        self.state["stop"] = True
        self.log("已请求停止", "warn")

    def close(self):
        self.state["stop"] = True
        if getattr(self, "record_cancel", None):
            self.record_cancel.set()
        if getattr(self, "hk", None):
            self.hk.stop()
        if self.worker and self.worker.is_alive():
            self.after(50, self.close)
            return
        self.destroy()


def main():
    try:
        app = App()
        app.mainloop()
    except Exception as error:
        detail = traceback.format_exc()
        log_path = os.path.join(mcb.HERE, "startup-error.log")
        try:
            with open(log_path, "w", encoding="utf-8") as stream:
                stream.write(detail)
        except OSError:
            log_path = "无法写入日志，请检查目录写入权限"
        messagebox.showerror("启动失败", f"{type(error).__name__}: {error}\n\n{log_path}")
        raise


if __name__ == "__main__":
    main()
