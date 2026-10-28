"""QQ 经典农场挂机 —— 图形界面。双击「启动农场助手.bat」打开。

开始 / 停止 / 立即跑一轮，F10 全局停止，挂机期间阻止电脑休眠和自动锁屏。
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
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import farm_bot
from farm_bot import FarmBot, Stopped, load_config
from farmlib import BASE_DIR, GameWindow
import pyautogui

CONFIG_PATH = BASE_DIR / "config.ini"
LOG_DIR = BASE_DIR / "logs"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
VK_F10 = 0x79
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312


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


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.worker = None
        self.bot = None
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.started_at = None
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

    # ---------- 界面 ----------
    def build_ui(self):
        r = self.root
        r.title("农场挂机助手")   # 标题别含游戏窗口名，免得被当成游戏窗口
        r.minsize(420, 520)
        pad = {"padx": 10, "pady": 4}

        top = ttk.Frame(r)
        top.pack(fill="x", **pad)
        self.lbl_state = tk.Label(top, text="● 已停止", fg="#888", font=("Microsoft YaHei UI", 14, "bold"))
        self.lbl_state.pack(side="left")
        self.lbl_next = ttk.Label(top, text="")
        self.lbl_next.pack(side="right")

        self.lbl_stats = ttk.Label(r, text="", foreground="#555", wraplength=560)
        self.lbl_stats.pack(fill="x", **pad)

        box = ttk.LabelFrame(r, text="设置（开始时自动保存）")
        box.pack(fill="x", **pad)
        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=3)
        ttk.Checkbutton(row, text="收自己的菜", variable=self.v_harvest).pack(side="left")
        ttk.Checkbutton(row, text="自动种菜（有种子才种）", variable=self.v_plant).pack(side="left", padx=16)
        ttk.Checkbutton(row, text="自家务农（除草/除虫/浇水）", variable=self.v_farm).pack(side="left")
        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=3)
        ttk.Checkbutton(row, text="去好友家偷菜", variable=self.v_steal).pack(side="left")
        ttk.Checkbutton(row, text="帮好友务农（较花时间）", variable=self.v_help).pack(side="left", padx=16)
        ttk.Checkbutton(row, text="自动领取每日任务奖励", variable=self.v_tasks).pack(side="left")
        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=3)
        ttk.Checkbutton(row, text="去「奶茶热销中」领取收益，每", variable=self.v_milktea).pack(side="left")
        ttk.Spinbox(row, from_=10, to=1440, width=5, textvariable=self.v_milktea_iv).pack(side="left", padx=4)
        ttk.Label(row, text="分钟一次（最多攒 48 小时）", foreground="#888").pack(side="left")
        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=3)
        ttk.Checkbutton(row, text="被挤下线自动重新登录，等", variable=self.v_relogin).pack(side="left")
        ttk.Spinbox(row, from_=0, to=240, width=4, textvariable=self.v_relogin_wait).pack(side="left", padx=4)
        ttk.Label(row, text="分钟再登（自己在手机上玩时可设 30）", foreground="#888").pack(side="left")
        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=3)
        ttk.Label(row, text="每轮间隔").pack(side="left")
        ttk.Spinbox(row, from_=1, to=240, width=5, textvariable=self.v_interval).pack(side="left", padx=4)
        ttk.Label(row, text="分钟，随机浮动 ±").pack(side="left")
        ttk.Spinbox(row, from_=0, to=60, width=4, textvariable=self.v_jitter).pack(side="left", padx=4)
        ttk.Label(row, text="分钟").pack(side="left")
        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=3)
        ttk.Label(row, text="休息时段").pack(side="left")
        ttk.Entry(row, width=8, textvariable=self.v_quiet).pack(side="left", padx=4)
        ttk.Label(row, text="如 1-7 表示凌晨 1 点到 7 点不操作，留空不休息", foreground="#888").pack(side="left")
        row = ttk.Frame(box); row.pack(fill="x", padx=8, pady=3)
        ttk.Checkbutton(row, text="挂机期间阻止电脑休眠和自动锁屏（锁屏后脚本无法工作）",
                        variable=self.v_awake).pack(side="left")

        btns = ttk.Frame(r)
        btns.pack(fill="x", **pad)
        self.btn_start = ttk.Button(btns, text="▶ 开始挂机", command=self.start)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=2)
        self.btn_stop = ttk.Button(btns, text="■ 停止 (F10)", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=2)
        self.btn_now = ttk.Button(btns, text="⟳ 立即跑一轮", command=self.run_now)
        self.btn_now.pack(side="left", expand=True, fill="x", padx=2)
        ttk.Button(btns, text="打开文件夹", command=lambda: os.startfile(BASE_DIR)).pack(side="left", padx=2)

        self.txt = ScrolledText(r, height=16, font=("Consolas", 9), state="disabled", wrap="word")
        self.txt.pack(fill="both", expand=True, padx=10, pady=(4, 10))

    def place_window(self):
        """放在农场窗口右侧，避免挡住游戏（被挡住会识别失败）。"""
        self.root.update_idletasks()
        try:
            title = load_config(CONFIG_PATH)["window"].get("title", "QQ经典农场")
            x, y, w, h = GameWindow(title, activate=False).client_rect()
            self.root.geometry(f"+{x + w + 30}+{max(0, y)}")
        except Exception:
            pass

    # ---------- 日志 ----------
    def log(self, line):
        self.q.put(("log", line))
        try:
            with open(LOG_DIR / f"farm_{dt.date.today():%Y%m%d}.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def append_log(self, line):
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n")
        if int(self.txt.index("end-1c").split(".")[0]) > 2000:
            self.txt.delete("1.0", "500.0")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    # ---------- 运行控制 ----------
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
            return None
        quiet = self.v_quiet.get().strip()
        if quiet and not re.fullmatch(r"\d{1,2}-\d{1,2}", quiet):
            messagebox.showerror("设置有误", "休息时段格式：起-止 小时，如 1-7")
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

    def set_running(self, on, once=False):
        if on:
            self.lbl_state.config(text="● 单轮运行中" if once else "● 挂机中", fg="#1a7f37")
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
            if once:
                self.btn_now.config(state="disabled")
            if self.v_awake.get():
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
        else:
            self.lbl_state.config(text="● 已停止", fg="#888")
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            self.btn_now.config(state="normal")
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

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
            self.lbl_stats.config(text="")
            self.lbl_next.config(text="")
            return
        up = ""
        if self.started_at and self.running():
            mins = int((dt.datetime.now() - self.started_at).total_seconds() // 60)
            up = f"已运行 {mins // 60} 小时 {mins % 60} 分　"
        lost = f"　⚠ 连续 {b.lost_rounds} 轮找不到农场画面" if b.lost_rounds else ""
        self.lbl_stats.config(text=f"{up}完成 {b.rounds} 轮　收菜 {b.harvests}　播种 {b.planted}　"
                                   f"务农 {b.farmed}　偷菜 {b.friends_stolen} 家　帮忙 {b.friends_helped} 家　"
                                   f"领任务 {b.tasks_claimed}　奶茶收益 {b.milktea_claims}　重连 {b.reconnects}　重新登录 {b.relogins}　"
                                   f"出错 {b.errors}{lost}")
        if self.running() and b.next_round_at:
            left = int((b.next_round_at - dt.datetime.now()).total_seconds())
            self.lbl_next.config(text=f"下一轮 {b.next_round_at:%H:%M:%S}（{max(0, left) // 60}:{max(0, left) % 60:02d}）")
        elif self.running():
            self.lbl_next.config(text="正在操作游戏，请勿动鼠标")
        else:
            self.lbl_next.config(text="")

    def on_close(self):
        if self.running():
            if not messagebox.askyesno("退出", "脚本还在运行，确定停止并退出吗？"):
                return
            self.stop_event.set()
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
