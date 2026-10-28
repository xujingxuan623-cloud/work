"""QQ 经典农场挂机 —— 图形界面。双击「启动农场助手.bat」打开。

开始 / 停止 / 立即跑一轮，F10 全局停止，挂机期间阻止电脑休眠和自动锁屏。
界面用 customtkinter（圆角、开关、卡片），配色跟游戏的田园风格一致。
"""
import ctypes
import datetime as dt
import os
import queue
import re
import threading
import traceback
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox

import customtkinter as ctk
from PIL import Image

import farm_bot
from farm_bot import FarmBot, Stopped, load_config
from farmlib import BASE_DIR, GameWindow
import pyautogui

CONFIG_PATH = BASE_DIR / "config.ini"
LOG_DIR = BASE_DIR / "logs"
ICON_ICO = BASE_DIR / "assets" / "farm.ico"
ICON_PNG = BASE_DIR / "assets" / "farm.png"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
VK_F10 = 0x79
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

# ---------- 配色（田园风：奶油底、草绿主色、橙色点缀） ----------
C = {
    "bg": "#F5EFE3",          # 窗口底色
    "card": "#FFFDF8",        # 卡片
    "border": "#E8DCC6",
    "text": "#4A3B2A",        # 深棕文字
    "muted": "#9A8A76",
    "green": "#4CAF50",
    "green_hover": "#3E9442",
    "green_soft": "#E5F4E1",
    "red": "#E5604D",
    "red_hover": "#C94D3B",
    "orange": "#F2A33A",
    "orange_hover": "#DB8D25",
    "orange_soft": "#FDF0DC",
    "gray_soft": "#EEE8DD",
    "log_bg": "#FBF8F2",
}
FONT = "Microsoft YaHei UI"

# 统计卡片：(属性名, 标题, 单位, 颜色)
STATS = [
    ("harvests", "收菜", "次", "#4CAF50"),
    ("planted", "播种", "次", "#7CB342"),
    ("farmed", "自家务农", "次", "#26A69A"),
    ("friends_stolen", "偷菜", "家", "#F2A33A"),
    ("friends_helped", "帮好友务农", "家", "#8D6E63"),
    ("tasks_claimed", "领任务奖励", "个", "#5C9BD6"),
    ("milktea_claims", "奶茶收益", "次", "#D9822B"),
    ("reconnects_total", "掉线恢复", "次", "#9575CD"),
]


def save_config_values(section, values):
    """只改 config.ini 里对应的 key = value 行，保留注释。"""
    text = CONFIG_PATH.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    cur, done = None, set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[(.+?)\]", line)
        if m:
            cur = m.group(1)
            continue
        if cur != section:
            continue
        m = re.match(r"\s*([A-Za-z_]+)\s*=", line)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)} = {values[m.group(1)]}"
            done.add(m.group(1))
    missing = [k for k in values if k not in done]
    if missing:  # 节里没有这一项就追加到节末尾
        idx = next(i for i, l in enumerate(lines) if l.strip() == f"[{section}]") + 1
        while idx < len(lines) and not lines[idx].lstrip().startswith("["):
            idx += 1
        for k in missing:
            lines.insert(idx, f"{k} = {values[k]}")
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def hotkey_listener(on_hotkey):
    """在独立线程里注册全局热键 F10。"""
    user32 = ctypes.windll.user32
    if not user32.RegisterHotKey(None, 1, MOD_NOREPEAT, VK_F10):
        on_hotkey(failed=True)
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            on_hotkey()


def font(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.worker = None
        self.bot = None
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.started_at = None
        self.mode = "stopped"   # stopped / running / once
        LOG_DIR.mkdir(exist_ok=True)
        farm_bot.set_log_sink(self.log)

        cfg = load_config(CONFIG_PATH)
        run = cfg["run"]
        self.v_harvest = tk.BooleanVar(value=run.getboolean("do_harvest", True))
        self.v_steal = tk.BooleanVar(value=run.getboolean("do_steal", True))
        self.v_plant = tk.BooleanVar(value=run.getboolean("do_plant", True))
        self.v_farm = tk.BooleanVar(value=run.getboolean("do_farm", True))
        self.v_help = tk.BooleanVar(value=run.getboolean("do_help", False))
        self.v_tasks = tk.BooleanVar(value=run.getboolean("do_tasks", True))
        self.v_milktea = tk.BooleanVar(value=run.getboolean("do_milktea", True))
        self.v_milktea_iv = tk.StringVar(value=run.get("milktea_interval_min", "60"))
        self.v_relogin = tk.BooleanVar(value=run.getboolean("relogin", True))
        self.v_relogin_wait = tk.StringVar(value=run.get("relogin_wait_min", "0"))
        self.v_interval = tk.StringVar(value=run.get("round_interval_min", "10"))
        self.v_jitter = tk.StringVar(value=run.get("interval_jitter_min", "3"))
        self.v_quiet = tk.StringVar(value=run.get("quiet_hours", ""))
        self.v_awake = tk.BooleanVar(value=True)

        self.build_ui()
        self.place_window()
        threading.Thread(target=hotkey_listener, args=(self.on_hotkey,), daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self.poll)
        self.log("就绪。先在 QQ 里打开「QQ经典农场」，再点「开始挂机」。挂机时按 F10 可随时停止。")

    # ================= 界面 =================
    def build_ui(self):
        r = self.root
        r.title("农场挂机助手")   # 标题别含游戏窗口名，免得被当成游戏窗口
        r.configure(fg_color=C["bg"])
        r.geometry("560x800")
        r.minsize(500, 640)
        self.set_icon()

        wrap = ctk.CTkFrame(r, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        self.build_header(wrap)
        self.build_buttons(wrap)

        self.tabs = ctk.CTkTabview(
            wrap, fg_color=C["card"], border_width=1, border_color=C["border"], corner_radius=14,
            segmented_button_fg_color=C["gray_soft"], segmented_button_selected_color=C["green"],
            segmented_button_selected_hover_color=C["green_hover"],
            segmented_button_unselected_color=C["gray_soft"],
            segmented_button_unselected_hover_color=C["border"],
            text_color=C["text"], anchor="w")
        self.tabs.pack(fill="both", expand=True, pady=(12, 0))
        self.tabs._segmented_button.configure(font=font(13, "bold"))
        tab_run = self.tabs.add("  运行情况  ")
        tab_set = self.tabs.add("  设置  ")
        self.build_run_tab(tab_run)
        self.build_settings_tab(tab_set)

    def set_icon(self):
        """窗口和任务栏图标。customtkinter 启动后会把图标换成自己的，所以稍后再设一次。"""
        if not ICON_ICO.exists():
            return
        try:
            self.root.iconbitmap(str(ICON_ICO))
            self.root.after(300, lambda: self.root.iconbitmap(str(ICON_ICO)))
        except tk.TclError:
            pass

    def card(self, master, **kw):
        return ctk.CTkFrame(master, fg_color=C["card"], corner_radius=14,
                            border_width=1, border_color=C["border"], **kw)

    def build_header(self, master):
        head = self.card(master)
        head.pack(fill="x")
        inner = ctk.CTkFrame(head, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=12)

        if ICON_PNG.exists():
            img = ctk.CTkImage(Image.open(ICON_PNG), size=(48, 48))
            ctk.CTkLabel(inner, image=img, text="").pack(side="left")
        titles = ctk.CTkFrame(inner, fg_color="transparent")
        titles.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(titles, text="农场挂机助手", font=font(20, "bold"),
                     text_color=C["text"]).pack(anchor="w")
        self.lbl_sub = ctk.CTkLabel(titles, text="QQ 经典农场 · 收菜 · 种菜 · 务农 · 领奖励",
                                    font=font(12), text_color=C["muted"])
        self.lbl_sub.pack(anchor="w")

        self.pill = ctk.CTkLabel(inner, text="● 已停止", font=font(13, "bold"), corner_radius=12,
                                 fg_color=C["gray_soft"], text_color=C["muted"], width=96, height=30)
        self.pill.pack(side="right")

        # 第二行：下一轮倒计时 / 已运行时长
        info = ctk.CTkFrame(head, fg_color="transparent")
        info.pack(fill="x", padx=14, pady=(0, 12))
        self.lbl_next = ctk.CTkLabel(info, text="还没开始", font=font(13), text_color=C["text"])
        self.lbl_next.pack(side="left")
        self.lbl_uptime = ctk.CTkLabel(info, text="", font=font(12), text_color=C["muted"])
        self.lbl_uptime.pack(side="right")

        # 异常提示条（平时隐藏）
        self.warn = ctk.CTkLabel(master, text="", font=font(12, "bold"), corner_radius=10,
                                 fg_color=C["orange_soft"], text_color="#B36B12", height=32)

    def build_buttons(self, master):
        row = ctk.CTkFrame(master, fg_color="transparent")
        row.pack(fill="x", pady=(12, 0))
        self.btn_row = row
        row.grid_columnconfigure((0, 1, 2), weight=1, uniform="b")
        common = dict(height=44, corner_radius=12, font=font(14, "bold"))
        self.btn_start = ctk.CTkButton(row, text="▶  开始挂机", command=self.start,
                                       fg_color=C["green"], hover_color=C["green_hover"],
                                       text_color="white", text_color_disabled="#E3F1E2", **common)
        self.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.btn_stop = ctk.CTkButton(row, text="■  停止  F10", command=self.stop,
                                      fg_color=C["red"], hover_color=C["red_hover"],
                                      text_color="white", text_color_disabled="#F7DCD7", **common)
        self.btn_stop.grid(row=0, column=1, sticky="ew", padx=6)
        self.btn_now = ctk.CTkButton(row, text="⟳  立即跑一轮", command=self.run_now,
                                     fg_color=C["card"], hover_color=C["orange_soft"],
                                     border_width=2, border_color=C["orange"],
                                     text_color="#B36B12", text_color_disabled=C["muted"], **common)
        self.btn_now.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        self.style_buttons()

    def build_run_tab(self, tab):
        tab.configure(fg_color=C["card"])
        # 统计卡片 4 x 2
        grid = ctk.CTkFrame(tab, fg_color="transparent")
        grid.pack(fill="x", padx=4, pady=(4, 8))
        grid.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="s")
        self.stat_labels = {}
        for i, (key, title, unit, color) in enumerate(STATS):
            tile = ctk.CTkFrame(grid, fg_color=C["bg"], corner_radius=12)
            tile.grid(row=i // 4, column=i % 4, sticky="nsew", padx=4, pady=4)
            ctk.CTkFrame(tile, fg_color=color, width=28, height=4, corner_radius=2).pack(
                anchor="w", padx=12, pady=(10, 0))
            num = ctk.CTkLabel(tile, text="0", font=font(22, "bold"), text_color=C["text"])
            num.pack(anchor="w", padx=12, pady=(2, 0))
            ctk.CTkLabel(tile, text=f"{title}（{unit}）", font=font(11), text_color=C["muted"]).pack(
                anchor="w", padx=12, pady=(0, 10))
            self.stat_labels[key] = num

        # 日志
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", padx=8, pady=(4, 4))
        ctk.CTkLabel(bar, text="运行日志", font=font(13, "bold"), text_color=C["text"]).pack(side="left")
        ctk.CTkButton(bar, text="打开文件夹", width=86, height=26, corner_radius=8, font=font(12),
                      fg_color=C["gray_soft"], hover_color=C["border"], text_color=C["text"],
                      command=lambda: os.startfile(BASE_DIR)).pack(side="right")
        ctk.CTkButton(bar, text="清空", width=56, height=26, corner_radius=8, font=font(12),
                      fg_color=C["gray_soft"], hover_color=C["border"], text_color=C["text"],
                      command=self.clear_log).pack(side="right", padx=6)

        self.txt = ctk.CTkTextbox(tab, font=ctk.CTkFont(family="Consolas", size=12), wrap="word",
                                  fg_color=C["log_bg"], text_color=C["text"], corner_radius=10,
                                  border_width=1, border_color=C["border"])
        self.txt.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.txt.tag_config("time", foreground="#B0A08A")
        self.txt.tag_config("good", foreground="#2E8B3E")
        self.txt.tag_config("warn", foreground="#C8691B")
        self.txt.tag_config("bad", foreground="#C93B2B")
        self.txt.configure(state="disabled")

    # ---------- 设置页 ----------
    def build_settings_tab(self, tab):
        tab.configure(fg_color=C["card"])
        sc = ctk.CTkScrollableFrame(tab, fg_color="transparent", scrollbar_button_color=C["border"],
                                    scrollbar_button_hover_color=C["muted"])
        sc.pack(fill="both", expand=True)
        self.settings_scroll = sc
        ctk.CTkLabel(sc, text="改完点「开始挂机」或「立即跑一轮」时自动保存", font=font(12),
                     text_color=C["muted"]).pack(anchor="w", padx=6, pady=(0, 6))

        sec = self.section(sc, "自家农场")
        self.switch(sec, "收自己的菜", self.v_harvest, "成熟了点「一键收获」")
        self.switch(sec, "自动种菜", self.v_plant, "有空地且有种子才种，优先种数量最多的种子")
        self.switch(sec, "自家务农", self.v_farm, "除草 / 除虫 / 浇水")

        sec = self.section(sc, "好友")
        self.switch(sec, "去好友家偷菜", self.v_steal, "只去头像上有「戴墨镜小手」的好友家")
        self.switch(sec, "帮好友务农", self.v_help, "挨个去有「小农夫」的好友家，比较花时间")

        sec = self.section(sc, "领取奖励")
        self.switch(sec, "自动领取每日任务奖励", self.v_tasks, "书本图标出现红色角标时去领")
        row = self.switch(sec, "领取「奶茶热销中」收益", self.v_milktea, "收益最多攒 48 小时")
        self.number(row, self.v_milktea_iv, "分钟一次", prefix="每")

        sec = self.section(sc, "挂机节奏")
        row = self.line(sec, "每轮间隔", "两轮之间等待的时间，加一点随机更像真人")
        self.number(row, self.v_jitter, "分钟", prefix="±")
        self.number(row, self.v_interval, "分钟")
        row = self.line(sec, "休息时段", "如 1-7 表示凌晨 1 点到 7 点不操作；留空不休息")
        ctk.CTkEntry(row, textvariable=self.v_quiet, width=70, height=30, corner_radius=8,
                     font=font(13), justify="center", border_color=C["border"],
                     fg_color="white", text_color=C["text"]).pack(side="right")
        self.switch(sec, "阻止电脑休眠和自动锁屏", self.v_awake, "锁屏后截图是黑的，脚本无法工作")

        sec = self.section(sc, "掉线处理")
        row = self.switch(sec, "被挤下线后自动重新登录", self.v_relogin, "自己在手机上玩时可设等 30 分钟")
        self.number(row, self.v_relogin_wait, "分钟后", prefix="等")

    def section(self, master, title):
        box = ctk.CTkFrame(master, fg_color=C["bg"], corner_radius=12)
        box.pack(fill="x", padx=4, pady=6)
        ctk.CTkLabel(box, text=title, font=font(14, "bold"), text_color=C["text"]).pack(
            anchor="w", padx=14, pady=(10, 2))
        return box

    def line(self, master, title, desc):
        row = ctk.CTkFrame(master, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(4, 8))
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(left, text=title, font=font(13), text_color=C["text"]).pack(anchor="w")
        ctk.CTkLabel(left, text=desc, font=font(11), text_color=C["muted"], justify="left",
                     wraplength=300).pack(anchor="w")
        return row

    def switch(self, master, title, var, desc):
        row = self.line(master, title, desc)
        ctk.CTkSwitch(row, text="", variable=var, onvalue=True, offvalue=False, width=46,
                      progress_color=C["green"], button_color="white", button_hover_color="#F4F4F4",
                      fg_color=C["border"]).pack(side="right")
        return row

    def number(self, row, var, unit, prefix=""):
        """在一行右侧放一个小数字输入框：[前缀] [数字] [单位]（pack 从右往左，所以倒着放）"""
        ctk.CTkLabel(row, text=unit, font=font(12), text_color=C["muted"]).pack(side="right", padx=(4, 8))
        ctk.CTkEntry(row, textvariable=var, width=52, height=30, corner_radius=8, font=font(13),
                     justify="center", border_color=C["border"], fg_color="white",
                     text_color=C["text"]).pack(side="right")
        if prefix:
            ctk.CTkLabel(row, text=prefix, font=font(12), text_color=C["muted"]).pack(side="right", padx=(8, 4))

    def place_window(self):
        """放在农场窗口右侧，避免挡住游戏（被挡住会识别失败）。"""
        self.root.update_idletasks()
        try:
            title = load_config(CONFIG_PATH)["window"].get("title", "QQ经典农场")
            x, y, w, h = GameWindow(title, activate=False).client_rect()
            self.root.geometry(f"+{x + w + 24}+{max(0, y - 30)}")
        except Exception:
            pass

    # ================= 日志 =================
    def log(self, line):
        self.q.put(("log", line))
        try:
            with open(LOG_DIR / f"farm_{dt.date.today():%Y%m%d}.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    @staticmethod
    def log_tag(line):
        if any(k in line for k in ("异常", "出错", "失败", "无法", "紧急停止")):
            return "bad"
        if any(k in line for k in ("掉线", "挤下线", "网络连接超时", "没种上", "种不上", "认不出",
                                   "变暗", "没打开", "跳过", "已停止", "正在停止")):
            return "warn"
        if any(k in line for k in ("一键收获", "逐个收获", "种上", "种数量", "领取了", "领取任务", "共领取",
                                   "一键务农", "帮忙务农", "已恢复", "偷取点击 1", "重新登录")):
            return "good"
        return None

    def append_log(self, line):
        self.txt.configure(state="normal")
        m = re.match(r"(\[\d\d:\d\d:\d\d\])(.*)", line, re.S)
        if m:
            self.txt.insert("end", m.group(1), "time")
            tag = self.log_tag(m.group(2))
            self.txt.insert("end", m.group(2) + "\n", tag) if tag else self.txt.insert("end", m.group(2) + "\n")
        else:
            tag = self.log_tag(line)
            self.txt.insert("end", line + "\n", tag) if tag else self.txt.insert("end", line + "\n")
        if int(self.txt.index("end-1c").split(".")[0]) > 2000:
            self.txt.delete("1.0", "500.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    # ================= 运行控制 =================
    def running(self):
        return self.worker is not None and self.worker.is_alive()

    def collect_settings(self):
        try:
            interval = max(1.0, float(self.v_interval.get()))
            jitter = max(0.0, float(self.v_jitter.get()))
            relogin_wait = max(0.0, float(self.v_relogin_wait.get()))
            milktea_iv = max(10.0, float(self.v_milktea_iv.get()))
        except ValueError:
            messagebox.showerror("设置有误", "间隔 / 等待分钟数请填数字")
            self.tabs.set("  设置  ")
            return None
        quiet = self.v_quiet.get().strip()
        if quiet and not re.fullmatch(r"\d{1,2}-\d{1,2}", quiet):
            messagebox.showerror("设置有误", "休息时段格式：起-止 小时，如 1-7")
            self.tabs.set("  设置  ")
            return None
        values = {
            "do_harvest": str(self.v_harvest.get()).lower(),
            "do_steal": str(self.v_steal.get()).lower(),
            "do_plant": str(self.v_plant.get()).lower(),
            "do_farm": str(self.v_farm.get()).lower(),
            "do_help": str(self.v_help.get()).lower(),
            "do_tasks": str(self.v_tasks.get()).lower(),
            "do_milktea": str(self.v_milktea.get()).lower(),
            "milktea_interval_min": f"{milktea_iv:g}",
            "relogin": str(self.v_relogin.get()).lower(),
            "relogin_wait_min": f"{relogin_wait:g}",
            "round_interval_min": f"{interval:g}",
            "interval_jitter_min": f"{jitter:g}",
            "quiet_hours": quiet,
        }
        save_config_values("run", values)
        return values

    def launch(self, once):
        if self.running():
            return
        if self.collect_settings() is None:
            return
        self.stop_event.clear()
        self.wake_event.clear()
        cfg = load_config(CONFIG_PATH)
        self.bot = FarmBot(cfg, self.stop_event)
        self.started_at = dt.datetime.now()
        self.worker = threading.Thread(target=self.work, args=(once,), daemon=True)
        self.worker.start()
        self.tabs.set("  运行情况  ")
        self.set_running(True, once)

    def work(self, once):
        try:
            self.log("开始挂机" if not once else "开始单轮运行")
            self.bot.loop(once=once, wake_event=self.wake_event)
            self.log("单轮运行完成")
        except Stopped:
            self.log("已停止")
        except pyautogui.FailSafeException:
            self.log("鼠标移到了屏幕左上角，紧急停止")
        except Exception:
            self.log("异常退出：\n" + traceback.format_exc())
        finally:
            self.q.put(("stopped", None))

    def start(self):
        self.launch(once=False)

    def run_now(self):
        if self.running():
            self.wake_event.set()   # 挂机中：跳过等待，马上开始下一轮
            self.log("已请求立即开始下一轮")
        else:
            self.launch(once=True)

    def stop(self):
        if self.running():
            self.stop_event.set()
            self.log("正在停止…（当前动作结束后停下）")

    def on_hotkey(self, failed=False):
        if failed:
            self.log("提示：F10 热键注册失败（可能被其他程序占用），请用界面上的停止按钮")
        else:
            self.root.after(0, self.stop)

    def style_buttons(self):
        """按状态切换按钮可用 / 灰掉的样子。"""
        running = self.mode != "stopped"
        self.btn_start.configure(state="disabled" if running else "normal",
                                 fg_color="#A5D6A7" if running else C["green"])
        self.btn_stop.configure(state="normal" if running else "disabled",
                                fg_color=C["red"] if running else "#F0B8AF")
        once = self.mode == "once"
        self.btn_now.configure(state="disabled" if once else "normal",
                               border_color=C["border"] if once else C["orange"])

    def set_running(self, on, once=False):
        if on:
            self.mode = "once" if once else "running"
            self.pill.configure(text="● 单轮运行" if once else "● 挂机中",
                                fg_color=C["green_soft"], text_color="#2E7D32")
            if self.v_awake.get():
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        else:
            self.mode = "stopped"
            self.pill.configure(text="● 已停止", fg_color=C["gray_soft"], text_color=C["muted"])
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self.style_buttons()

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.append_log(data)
                elif kind == "stopped":
                    self.worker = None
                    self.set_running(False)
        except queue.Empty:
            pass
        self.refresh_status()
        self.root.after(300, self.poll)

    def refresh_status(self):
        b = self.bot
        if b is None:
            return
        for key, *_ in STATS:
            val = b.reconnects + b.relogins if key == "reconnects_total" else getattr(b, key, 0)
            lbl = self.stat_labels[key]
            if lbl.cget("text") != str(val):
                lbl.configure(text=str(val))

        running = self.running()
        if self.started_at and running:
            mins = int((dt.datetime.now() - self.started_at).total_seconds() // 60)
            self.lbl_uptime.configure(text=f"已运行 {mins // 60} 小时 {mins % 60} 分 · 完成 {b.rounds} 轮")
        elif not running:
            self.lbl_uptime.configure(text=f"上次共完成 {b.rounds} 轮" if b.rounds else "")

        if running and b.next_round_at:
            left = max(0, int((b.next_round_at - dt.datetime.now()).total_seconds()))
            self.lbl_next.configure(text=f"下一轮 {b.next_round_at:%H:%M:%S}　还有 {left // 60}:{left % 60:02d}",
                                    text_color=C["text"])
        elif running:
            self.lbl_next.configure(text="正在操作游戏，请勿动鼠标…", text_color="#2E7D32")
        else:
            self.lbl_next.configure(text="已停止", text_color=C["muted"])

        # 异常提示条
        msg = ""
        if b.lost_rounds:
            msg = f"⚠  连续 {b.lost_rounds} 轮找不到农场画面，请看一下 QQ 农场窗口"
        elif b.errors:
            msg = f"⚠  有 {b.errors} 轮出错，详情见日志"
        if msg:
            self.warn.configure(text=msg)
            if not self.warn.winfo_ismapped():
                self.warn.pack(fill="x", pady=(10, 0), before=self.btn_row)
        elif self.warn.winfo_ismapped():
            self.warn.pack_forget()

    def on_close(self):
        if self.running():
            if not messagebox.askyesno("退出", "脚本还在运行，确定停止并退出吗？"):
                return
            self.stop_event.set()
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self.root.destroy()


def main():
    # 让任务栏显示我们自己的图标，而不是 Python 的
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("qqfarm.autohelper")
    except Exception:
        pass
    ctk.set_appearance_mode("light")
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
