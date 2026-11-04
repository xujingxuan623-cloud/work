"""QQ 经典农场挂机 —— 图形界面。双击「启动农场助手.bat」打开。

开始 / 停止 / 立即跑一轮，F10 全局停止，挂机期间阻止电脑休眠和自动锁屏。
界面用 customtkinter，仿苹果风格（留白、少颜色、一个主按钮、分组列表），跟随 Windows 浅色 / 深色模式。
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
import win32gui
from PIL import Image

import farm_bot
from farm_bot import FarmBot, Stopped, load_config
from farmlib import BASE_DIR, GameWindow
import pyautogui

CONFIG_PATH = BASE_DIR / "config.ini"
LOG_DIR = BASE_DIR / "logs"
ICON_ICO = BASE_DIR / "assets" / "sprout.ico"   # 换过图标要换文件名，否则 Windows 图标缓存一直显示旧的
ICON_PNG = BASE_DIR / "assets" / "farm.png"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
VK_F10 = 0x79
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312

# ---------- 配色：苹果系统色（浅色, 深色），跟随 Windows 的浅色 / 深色模式 ----------
C = {
    "bg": ("#F2F2F7", "#000000"),            # 分组列表底色
    "card": ("#FFFFFF", "#1C1C1E"),          # 白色圆角卡片
    "sep": ("#E5E5EA", "#38383A"),           # 行与行之间的细分隔线
    "fill": ("#E9E9EE", "#2C2C2E"),          # 灰色按钮 / 分段控件底槽 / 输入框
    "fill_hover": ("#DCDCE1", "#3A3A3C"),
    "seg_sel": ("#FFFFFF", "#636366"),       # 分段控件选中的滑块
    "text": ("#1C1C1E", "#FFFFFF"),
    "muted": ("#8E8E93", "#98989D"),
    "blue": ("#007AFF", "#0A84FF"),
    "blue_hover": ("#0066D6", "#0071E3"),
    "blue_dis": ("#A6CDFF", "#16365C"),
    "green": ("#34C759", "#30D158"),
    "green_tint": ("#E3F7E8", "#0E2A16"),
    "green_text": ("#248A3D", "#30D158"),
    "red": ("#FF3B30", "#FF453A"),
    "red_tint": ("#FFE5E3", "#3A1715"),
    "red_tint_hover": ("#FFD6D2", "#4A1D1A"),
    "orange_tint": ("#FFF1DC", "#33240B"),
    "orange_text": ("#C25E00", "#FF9F0A"),
    "switch_off": ("#E3E3E8", "#39393D"),
}
FONT = "Microsoft YaHei UI"
NUM_FONT = "Segoe UI Variable Display"   # 数字字形接近苹果的 SF 字体
FONT_MEDIUM = "Noto Sans SC Medium"     # 标题用中等粗细，比粗体更精致
THEMES = {"跟随系统": "system", "浅色": "light", "深色": "dark"}   # 设置里的主题选项 -> customtkinter 的模式
TAB_RUN, TAB_SET = "            概览            ", "            设置            "

# 统计：(属性名, 标题, 单位)
STATS = [
    ("harvests", "收菜", "次"),
    ("planted", "播种", "次"),
    ("farmed", "务农", "次"),
    ("friends_stolen", "偷菜", "家"),
    ("friends_helped", "帮好友", "家"),
    ("tasks_claimed", "任务奖励", "个"),
    ("milktea_claims", "奶茶收益", "次"),
    ("reconnects_total", "掉线恢复", "次"),
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


def parent_chain():
    """从父进程往上的进程名列表，用来看是谁启动了我们。"""
    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(2, 0)   # TH32CS_SNAPPROCESS
    procs = {}
    try:
        e = PE(); e.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(wintypes.HANDLE(snap), ctypes.byref(e))
        while ok:
            procs[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile)
            ok = k32.Process32NextW(wintypes.HANDLE(snap), ctypes.byref(e))
    finally:
        k32.CloseHandle(wintypes.HANDLE(snap))
    names, pid = [], procs.get(os.getpid(), (0, ""))[0]
    while pid in procs and len(names) < 8:
        ppid, name = procs[pid]
        names.append(name)
        pid = ppid
    return names


def install_crash_logging():
    """pythonw 没有控制台，崩溃信息会直接丢掉；把它们写进 logs/crash.log 方便事后查。"""
    import faulthandler
    import sys
    LOG_DIR.mkdir(exist_ok=True)
    f = open(LOG_DIR / "crash.log", "a", encoding="utf-8", buffering=1)
    f.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} 启动 PID {os.getpid()} =====\n")
    faulthandler.enable(f, all_threads=True)   # 底层崩溃（访问越界等）也能留下各线程的调用栈
    sys.excepthook = lambda *a: f.write("未捕获异常：\n" + "".join(traceback.format_exception(*a)))
    threading.excepthook = lambda a: f.write(
        f"线程 {a.thread.name if a.thread else '?'} 未捕获异常：\n"
        + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    if sys.stderr is None:
        sys.stderr = f
    return f


def load_fonts():
    """加载 assets/fonts 里的字体（只给本程序用，不安装到系统），再挑本机有的字体。"""
    global FONT, FONT_MEDIUM, NUM_FONT
    for p in (BASE_DIR / "assets" / "fonts").glob("*.[ot]tf"):
        ctypes.windll.gdi32.AddFontResourceExW(str(p), 0x10, 0)   # FR_PRIVATE
    import tkinter.font as tkfont
    have = set(tkfont.families())
    FONT = next((f for f in ("Noto Sans SC", "Microsoft YaHei UI") if f in have), FONT)
    FONT_MEDIUM = FONT_MEDIUM if FONT_MEDIUM in have else FONT
    NUM_FONT = next((f for f in ("Segoe UI Variable Display Semib", "Segoe UI Variable Display", "Segoe UI")
                     if f in have), FONT)


def font(size=13, weight="normal"):
    """weight 可以是 normal / medium / bold；medium 用单独的中等粗细字体。"""
    if weight == "medium":
        return ctk.CTkFont(family=FONT_MEDIUM, size=size, weight="bold" if FONT_MEDIUM == FONT else "normal")
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


def pick(color):
    """(浅色, 深色) 里按当前模式取一个，给不支持两套颜色的 tk 原生控件用。"""
    return color[1] if ctk.get_appearance_mode() == "Dark" else color[0]


class IOSSwitch(ctk.CTkLabel):
    """仿 iPhone 的开关。customtkinter 自带的开关圆钮会盖住轨道、和白卡片混在一起，
    所以用 PIL 画好「开 / 关 × 浅色 / 深色」四张抗锯齿图片（圆钮带淡阴影），点一下切换。"""
    W, H = 46, 28        # 显示大小
    _images = {}

    @classmethod
    def _render(cls, on, dark, k=4):
        from PIL import ImageDraw, ImageFilter
        w, h = cls.W * k, cls.H * k
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        track = C["green"][dark] if on else C["switch_off"][dark]
        ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=track)
        pad = 2 * k
        d = h - 2 * pad
        x0 = w - pad - d if on else pad
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).ellipse((x0, pad + k, x0 + d, pad + k + d), fill=(0, 0, 0, 70))
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(1.5 * k)))
        ImageDraw.Draw(img).ellipse((x0, pad, x0 + d, pad + d), fill="#FFFFFF")
        return img

    @classmethod
    def image(cls, on):
        if on not in cls._images:
            cls._images[on] = ctk.CTkImage(light_image=cls._render(on, 0), dark_image=cls._render(on, 1),
                                           size=(cls.W, cls.H))
        return cls._images[on]

    def __init__(self, master, variable):
        super().__init__(master, text="", image=self.image(bool(variable.get())), width=self.W, height=self.H,
                         fg_color="transparent", cursor="hand2")
        self.var = variable
        self.bind("<Button-1>", lambda e: self.var.set(not self.var.get()))
        variable.trace_add("write", lambda *_: self.configure(image=self.image(bool(self.var.get()))))


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
        self.v_patrol = tk.BooleanVar(value=run.getboolean("friend_patrol", True))
        self.v_patrol_min = tk.StringVar(value=run.get("friend_patrol_min", "60"))
        self.v_interval = tk.StringVar(value=run.get("round_interval_min", "10"))
        self.v_jitter = tk.StringVar(value=run.get("interval_jitter_min", "3"))
        self.v_quiet = tk.StringVar(value=run.get("quiet_hours", ""))
        self.v_awake = tk.BooleanVar(value=True)
        theme = cfg["ui"].get("theme", "system") if cfg.has_section("ui") else "system"
        self.v_theme = tk.StringVar(value=next((k for k, v in THEMES.items() if v == theme), "跟随系统"))

        self.build_ui()
        self.place_window()
        threading.Thread(target=hotkey_listener, args=(self.on_hotkey,), daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(200, self.poll)
        try:
            chain = parent_chain()
        except Exception:
            chain = []
        self.log(f"===== 程序启动 {dt.datetime.now():%Y-%m-%d %H:%M:%S}（PID {os.getpid()}，"
                 f"启动来源：{' ← '.join(chain[:3]) or '未知'}）=====")
        if any("claude" in n.lower() for n in chain):
            self.log("提示：这次是从 Claude 里启动的，Claude 更新或重启时会把它一起关掉。"
                     "长期挂机请关掉这个窗口，改用桌面上的「农场挂机助手」打开")
        self.log("就绪。先在 QQ 里打开「QQ经典农场」，再点「开始挂机」。挂机时按 F10 可随时停止。")

    # ================= 界面（苹果风格：留白、少颜色、一个主按钮、分组列表） =================
    def build_ui(self):
        r = self.root
        r.title("农场挂机助手")   # 标题别含游戏窗口名，免得被当成游戏窗口
        r.configure(fg_color=C["bg"])
        r.geometry("500x800")
        r.minsize(460, 660)
        self.set_icon()
        self.hairlines = []   # tk 原生细线（customtkinter 画不出 1 像素的线），换浅色 / 深色时重新上色

        wrap = ctk.CTkFrame(r, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=20, pady=(18, 14))

        self.build_header(wrap)
        self.build_buttons(wrap)

        self.tabs = ctk.CTkTabview(
            wrap, fg_color="transparent", corner_radius=9, border_width=0, anchor="center",
            segmented_button_fg_color=C["fill"],
            segmented_button_selected_color=C["seg_sel"],
            segmented_button_selected_hover_color=C["seg_sel"],
            segmented_button_unselected_color=C["fill"],
            segmented_button_unselected_hover_color=C["fill"],
            text_color=C["text"])
        self.tabs.pack(fill="both", expand=True, pady=(14, 0))
        self.tabs._segmented_button.configure(font=font(13, "medium"), height=30, border_width=2)
        tab_run = self.tabs.add(TAB_RUN)
        tab_set = self.tabs.add(TAB_SET)
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

    @staticmethod
    def card(master, **kw):
        return ctk.CTkFrame(master, fg_color=C["card"], corner_radius=12, border_width=0, **kw)

    def hairline(self, master, padx=(16, 0)):
        line = tk.Frame(master, height=1, bd=0, highlightthickness=0, bg=pick(C["sep"]))
        line.pack(fill="x", padx=padx)
        self.hairlines.append(line)

    def build_header(self, master):
        head = ctk.CTkFrame(master, fg_color="transparent")
        head.pack(fill="x")
        if ICON_PNG.exists():
            img = ctk.CTkImage(Image.open(ICON_PNG), size=(50, 50))
            ctk.CTkLabel(head, image=img, text="").pack(side="left")
        titles = ctk.CTkFrame(head, fg_color="transparent")
        titles.pack(side="left", padx=(12, 0))
        ctk.CTkLabel(titles, text="农场挂机助手", font=font(21, "medium"), text_color=C["text"],
                     height=30).pack(anchor="w")
        status = ctk.CTkFrame(titles, fg_color="transparent")
        status.pack(anchor="w")
        self.dot = ctk.CTkLabel(status, text="●", font=font(10), text_color=C["muted"], height=20)
        self.dot.pack(side="left", padx=(1, 5))
        self.lbl_status = ctk.CTkLabel(status, text="已停止", font=font(13), text_color=C["muted"], height=20)
        self.lbl_status.pack(side="left")

        # 异常提示（平时隐藏）
        self.warn = ctk.CTkLabel(master, text="", font=font(12), corner_radius=10,
                                 fg_color=C["orange_tint"], text_color=C["orange_text"], height=34)

    def build_buttons(self, master):
        box = ctk.CTkFrame(master, fg_color="transparent")
        box.pack(fill="x", pady=(18, 0))
        self.btn_row = box
        # 只有一个主按钮：没挂机时「开始挂机」（蓝），挂机中变成「停止挂机」（浅红底红字）
        self.btn_main = ctk.CTkButton(box, text="开始挂机", command=self.toggle, height=46, corner_radius=12,
                                      font=font(15, "medium"), border_width=0,
                                      fg_color=C["blue"], hover_color=C["blue_hover"], text_color="white")
        self.btn_main.pack(fill="x")
        sub = ctk.CTkFrame(box, fg_color="transparent")
        sub.pack(pady=(6, 0))
        self.btn_now = ctk.CTkButton(sub, text="立即跑一轮", command=self.run_now, width=80, height=26,
                                     corner_radius=6, font=font(13), fg_color="transparent",
                                     hover_color=C["fill"], text_color=C["blue"], text_color_disabled=C["muted"])
        self.btn_now.pack(side="left")
        ctk.CTkLabel(sub, text="·   按 F10 随时停止", font=font(12), text_color=C["muted"]).pack(
            side="left", padx=(4, 0))
        self.style_buttons()

    def build_run_tab(self, tab):
        tab.configure(fg_color="transparent")
        # 统计：一张白卡，黑色数字 + 灰色小字，不用花哨的颜色
        stats = self.card(tab)
        stats.pack(fill="x", pady=(4, 0))
        self.stat_labels = {}
        for r0 in (0, 4):
            if r0:
                self.hairline(stats, padx=16)
            grid = ctk.CTkFrame(stats, fg_color="transparent")
            grid.pack(fill="x", padx=8, pady=10)
            grid.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="s")
            for j, (key, title, unit) in enumerate(STATS[r0:r0 + 4]):
                cell = ctk.CTkFrame(grid, fg_color="transparent")
                cell.grid(row=0, column=j, sticky="w", padx=8)
                line = ctk.CTkFrame(cell, fg_color="transparent")
                line.pack(anchor="w")
                num = ctk.CTkLabel(line, text="0", font=ctk.CTkFont(family=NUM_FONT, size=24),
                                   text_color=C["text"], height=32)
                num.pack(side="left")
                ctk.CTkLabel(line, text=unit, font=font(11), text_color=C["muted"], height=32).pack(
                    side="left", padx=(3, 0), pady=(6, 0))
                ctk.CTkLabel(cell, text=title, font=font(12), text_color=C["muted"], height=18).pack(anchor="w")
                self.stat_labels[key] = num
        self.lbl_uptime = ctk.CTkLabel(tab, text="", font=font(12), text_color=C["muted"], height=20)
        self.lbl_uptime.pack(anchor="w", padx=16, pady=(6, 0))

        # 日志
        bar = ctk.CTkFrame(tab, fg_color="transparent")
        bar.pack(fill="x", pady=(12, 4))
        ctk.CTkLabel(bar, text="日志", font=font(12), text_color=C["muted"]).pack(side="left", padx=(16, 0))
        link = dict(height=24, corner_radius=6, font=font(13), fg_color="transparent",
                    hover_color=C["fill"], text_color=C["blue"])
        ctk.CTkButton(bar, text="打开文件夹", width=78, command=lambda: os.startfile(BASE_DIR),
                      **link).pack(side="right")
        ctk.CTkButton(bar, text="清空", width=44, command=self.clear_log, **link).pack(side="right", padx=2)

        self.txt = ctk.CTkTextbox(tab, font=font(12), wrap="word", fg_color=C["card"], text_color=C["text"],
                                  corner_radius=12, border_width=0, border_spacing=12,
                                  scrollbar_button_color=C["fill"], scrollbar_button_hover_color=C["muted"])
        self.txt.pack(fill="both", expand=True, pady=(0, 2))
        self.apply_mode_colors()
        self.txt.configure(state="disabled")

    def apply_mode_colors(self):
        """tk 原生控件（日志文字颜色、细线）不支持浅色 / 深色两套，按当前模式上色，模式变了再来一次。"""
        self.color_mode = ctk.get_appearance_mode()
        self.txt.tag_config("time", foreground=pick(C["muted"]))
        self.txt.tag_config("good", foreground=pick(C["text"]))
        self.txt.tag_config("warn", foreground=pick(C["orange_text"]))
        self.txt.tag_config("bad", foreground=pick(C["red"]))
        for line in self.hairlines:
            line.configure(bg=pick(C["sep"]))

    # ---------- 设置页：iOS 分组列表，每行只有名称和开关，说明放在组下方 ----------
    def build_settings_tab(self, tab):
        tab.configure(fg_color="transparent")
        sc = ctk.CTkScrollableFrame(tab, fg_color="transparent", scrollbar_button_color=C["fill"],
                                    scrollbar_button_hover_color=C["muted"])
        sc.pack(fill="both", expand=True)
        self.settings_scroll = sc

        g = self.section(sc, "外观")
        row = self.row(g, "主题")
        ctk.CTkSegmentedButton(row, values=list(THEMES), variable=self.v_theme, command=self.set_theme,
                               font=font(12, "medium"), height=28, corner_radius=8, border_width=2,
                               fg_color=C["fill"], selected_color=C["seg_sel"], selected_hover_color=C["seg_sel"],
                               unselected_color=C["fill"], unselected_hover_color=C["fill"],
                               text_color=C["text"]).pack(side="right")

        g = self.section(sc, "自家农场")
        self.switch_row(g, "收菜", self.v_harvest)
        self.switch_row(g, "种菜", self.v_plant)
        self.switch_row(g, "务农", self.v_farm)
        self.footer(sc, "有空地且有种子才种，优先种数量最多的种子；务农是除草、除虫、浇水。")

        g = self.section(sc, "好友")
        self.switch_row(g, "偷菜", self.v_steal)
        self.switch_row(g, "帮好友务农", self.v_help)
        self.switch_row(g, "定时巡逻所有好友", self.v_patrol)
        self.number_row(g, "巡逻间隔", self.v_patrol_min, "分钟")
        self.footer(sc, "平时只在自家右下角出现可偷或求助提示时才去；定时巡逻会挨个看一遍所有好友。")

        g = self.section(sc, "奖励")
        self.switch_row(g, "每日任务奖励", self.v_tasks)
        self.switch_row(g, "奶茶店收益", self.v_milktea)
        self.number_row(g, "奶茶收益领取间隔", self.v_milktea_iv, "分钟")
        self.footer(sc, "任务奖励每天领到一次就不再点；奶茶店收益最多攒 48 小时。")

        g = self.section(sc, "节奏")
        self.number_row(g, "每轮间隔", self.v_interval, "分钟")
        self.number_row(g, "随机浮动", self.v_jitter, "分钟", prefix="±")
        row = self.row(g, "休息时段")
        self.entry(row, self.v_quiet, 64)
        self.switch_row(g, "阻止休眠和锁屏", self.v_awake)
        self.footer(sc, "休息时段如1-7表示凌晨1点到7点不操作，留空不休息。锁屏后截图是黑的，脚本无法工作。")

        g = self.section(sc, "掉线")
        self.switch_row(g, "被挤下线后重新登录", self.v_relogin)
        self.number_row(g, "等待", self.v_relogin_wait, "分钟")
        self.footer(sc, "自己在手机上玩时，可以设等30分钟再登录，免得把手机挤掉。")

        ctk.CTkLabel(sc, text="设置会在开始挂机时自动保存", font=font(11), text_color=C["muted"]).pack(pady=(16, 14))

    def set_theme(self, label):
        """切换浅色 / 深色，马上生效，并记到 config.ini 里下次打开还是这个。"""
        mode = THEMES.get(label, "system")
        ctk.set_appearance_mode(mode)
        self.root.after(50, self.apply_mode_colors)
        try:
            save_config_values("ui", {"theme": mode})
        except Exception:
            pass

    def section(self, master, title):
        ctk.CTkLabel(master, text=title, font=font(12), text_color=C["muted"], height=18).pack(
            anchor="w", padx=16, pady=(16, 6))
        box = self.card(master)
        box.pack(fill="x", padx=(0, 8))
        box.rows = 0
        return box

    @staticmethod
    def footer(master, text):
        ctk.CTkLabel(master, text=text, font=font(11), text_color=C["muted"], justify="left",
                     wraplength=380, anchor="w").pack(fill="x", padx=16, pady=(6, 0))

    def row(self, card, title):
        if card.rows:
            self.hairline(card)
        card.rows += 1
        row = ctk.CTkFrame(card, fg_color="transparent", height=46)
        row.pack(fill="x", padx=16)
        row.pack_propagate(False)
        ctk.CTkLabel(row, text=title, font=font(14), text_color=C["text"]).pack(side="left")
        return row

    def switch_row(self, card, title, var):
        row = self.row(card, title)
        IOSSwitch(row, var).pack(side="right")
        return row

    def entry(self, row, var, width=52):
        ctk.CTkEntry(row, textvariable=var, width=width, height=28, corner_radius=8, font=font(13),
                     justify="center", border_width=0, fg_color=C["fill"],
                     text_color=C["text"]).pack(side="right")

    def number_row(self, card, title, var, unit, prefix=""):
        """一行：名称 …… [前缀] [数字] 单位（pack 从右往左，所以倒着放）"""
        row = self.row(card, title)
        ctk.CTkLabel(row, text=unit, font=font(13), text_color=C["muted"]).pack(side="right", padx=(6, 0))
        self.entry(row, var)
        if prefix:
            ctk.CTkLabel(row, text=prefix, font=font(13), text_color=C["muted"]).pack(side="right", padx=(0, 6))
        return row

    def place_window(self):
        """放在农场窗口右侧，避免挡住游戏（被挡住会识别失败）。"""
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        ww, wh = self.root.winfo_width(), self.root.winfo_height()
        # 默认放屏幕中间；农场窗口最小化时坐标是 -32000，不能跟着它放，否则界面跑到屏幕外
        px, py = (sw - ww) // 2, max(0, (sh - wh) // 2 - 30)
        try:
            title = load_config(CONFIG_PATH)["window"].get("title", "QQ经典农场")
            gw = GameWindow(title, activate=False)
            if gw.find() and not win32gui.IsIconic(gw.hwnd):
                x, y, w, h = gw.client_rect()
                if 0 <= x + w + 24 <= sw - ww and 0 <= y < sh:
                    px, py = x + w + 24, max(0, y - 30)
        except Exception:
            pass
        self.root.geometry(f"+{px}+{py}")

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
            patrol_min = max(10.0, float(self.v_patrol_min.get()))
        except ValueError:
            messagebox.showerror("设置有误", "间隔 / 等待分钟数请填数字")
            self.tabs.set(TAB_SET)
            return None
        quiet = self.v_quiet.get().strip()
        if quiet and not re.fullmatch(r"\d{1,2}-\d{1,2}", quiet):
            messagebox.showerror("设置有误", "休息时段格式：起-止 小时，如 1-7")
            self.tabs.set(TAB_SET)
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
            "friend_patrol": str(self.v_patrol.get()).lower(),
            "friend_patrol_min": f"{patrol_min:g}",
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
        self.tabs.set(TAB_RUN)
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

    def toggle(self):
        self.stop() if self.running() else self.start()

    def style_buttons(self):
        """主按钮：没挂机时蓝色「开始挂机」，挂机中浅红底红字「停止挂机」。"""
        if self.mode == "stopped":
            self.btn_main.configure(text="开始挂机", fg_color=C["blue"], hover_color=C["blue_hover"],
                                    text_color="white")
        else:
            self.btn_main.configure(text="停止挂机" if self.mode == "running" else "停止",
                                    fg_color=C["red_tint"], hover_color=C["red_tint_hover"], text_color=C["red"])
        self.btn_now.configure(state="disabled" if self.mode == "once" else "normal")

    def set_running(self, on, once=False):
        if on:
            self.mode = "once" if once else "running"
            if self.v_awake.get():
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        else:
            self.mode = "stopped"
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self.style_buttons()
        self.show_status()

    def show_status(self, text=None):
        """标题下面那行小字：● 挂机中 · 下一轮 …"""
        if self.mode == "stopped":
            self.dot.configure(text_color=C["muted"])
            self.lbl_status.configure(text="已停止")
        else:
            self.dot.configure(text_color=C["green"])
            head = "单轮运行中" if self.mode == "once" else "挂机中"
            self.lbl_status.configure(text=f"{head} · {text}" if text else head)

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
        if ctk.get_appearance_mode() != self.color_mode:   # 系统切换了浅色 / 深色
            self.apply_mode_colors()
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
            self.lbl_uptime.configure(text=f"已运行 {mins // 60} 小时 {mins % 60} 分，完成 {b.rounds} 轮")
        elif not running:
            self.lbl_uptime.configure(text=f"上次完成 {b.rounds} 轮" if b.rounds else "")

        if running and b.next_round_at:
            left = max(0, int((b.next_round_at - dt.datetime.now()).total_seconds()))
            self.show_status(f"下一轮 {b.next_round_at:%H:%M}，还有 {left // 60}:{left % 60:02d}")
        elif running:
            self.show_status("正在操作游戏，请勿动鼠标")

        # 异常提示条
        msg = ""
        if b.lost_rounds:
            msg = f"连续 {b.lost_rounds} 轮找不到农场画面，请看一下 QQ 农场窗口"
        elif b.errors:
            msg = f"有 {b.errors} 轮出错，详情见日志"
        if msg:
            self.warn.configure(text=msg)
            if not self.warn.winfo_ismapped():
                self.warn.pack(fill="x", pady=(14, 0), before=self.btn_row)
        elif self.warn.winfo_ismapped():
            self.warn.pack_forget()

    def on_close(self):
        if self.running():
            if not messagebox.askyesno("退出", "脚本还在运行，确定停止并退出吗？"):
                return
            self.stop_event.set()
        self.log(f"===== 窗口被关闭，程序退出 {dt.datetime.now():%Y-%m-%d %H:%M:%S} =====")
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self.root.destroy()


def main():
    crash_log = install_crash_logging()
    # 让任务栏显示我们自己的图标，而不是 Python 的
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("qqfarm.autohelper")
    except Exception:
        pass
    cfg = load_config(CONFIG_PATH)
    theme = cfg["ui"].get("theme", "system") if cfg.has_section("ui") else "system"
    ctk.set_appearance_mode(theme)   # 设置里选的主题；默认跟随 Windows 的浅色 / 深色模式
    root = ctk.CTk()
    load_fonts()
    # 界面回调里的异常默认打印到 stderr（pythonw 下看不到），改成写进 crash.log
    root.report_callback_exception = lambda *a: crash_log.write(
        "界面回调异常：\n" + "".join(traceback.format_exception(*a)))
    App(root)
    root.mainloop()
    crash_log.write(f"===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} 正常退出 =====\n")


if __name__ == "__main__":
    main()
