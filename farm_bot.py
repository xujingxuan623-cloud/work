"""QQ 经典农场（电脑版 QQ 小程序）自动收菜 / 偷菜。

用法：
    python farm_bot.py                 正常循环运行
    python farm_bot.py --once          只跑一轮
    python farm_bot.py --test          截一张图，测试每个模板的识别分数
    python farm_bot.py --list-windows  列出所有窗口标题

紧急停止：把鼠标甩到屏幕左上角，或在命令行按 Ctrl+C。
"""
import argparse
import configparser
import datetime as dt
import random
import sys
import threading
import time

import cv2
import numpy as np

from farmlib import (BASE_DIR, DEBUG_DIR, GameWindow, Hit, Matcher, annotate, drag,
                     human_click, imwrite, list_windows)
import pyautogui  # 放在 farmlib 之后，farmlib 里先设置了 DPI 感知

# 模板角色说明（文件放在 templates/ 下，同一角色可放多张：role.png、role_2.png ...）
ROLES = {
    "harvest_all":  "自家农场的「一键收获」按钮",
    "ripe":         "成熟作物上的「可摘」气泡（自家和好友家通用）",
    "friends":      "自家右下角「好友」按钮",
    "visit":        "好友列表里的「拜访」按钮",
    "panel_close":  "好友列表右上角的关闭 ✕",
    "friend_steal": "好友家底部好友栏里，头像右上角「戴墨镜的小手」= 可以偷",
    "steal_all":    "好友家的「一键偷取」按钮",
    "steal_ripe":   "好友家可偷作物上的图标（不截则用 ripe）",
    "go_home":      "好友家右下角「回家」按钮",
    "empty_tile":   "空地块的中间部分（没有种子堆、没有苗）；紫色地、黄色地各一张",
    "empty_plot":   "空地上偶尔出现的白色小手提示（补充用）",
    "reconnect":    "「网络连接超时」弹窗里的绿色「重新连接」按钮",
    "relogin":      "「账号已在其他地方登录」弹窗里的绿色「重新登录」按钮",
    "task_icon":    "自家左下角「等级提升至…」条上的书本图标（任务入口）",
    "milktea_icon": "自家右上角「奶茶热销中」图标",
    "milktea_claim": "奶茶店左边「领取收益」的奶茶券图标",
    "milktea_take": "「累计收益」窗口里的「领取」按钮（黄色可领，灰色不可领）",
    "milktea_close": "「累计收益」窗口右上角的 ✕",
    "milktea_back": "奶茶店左上角的返回箭头",
    "farm_all":     "「一键务农」按钮（自家、好友家通用）",
    "friend_help":  "好友家底部好友栏里，头像右上角「小农夫」= 需要帮忙",
    "close":        "各种弹窗的关闭 / 确定按钮（可截多张：close.png、close_2.png ...）",
}

# 底部好友栏里，头像中心相对「可偷」小图标中心的偏移（像素，按 452x843 窗口量的）
AVATAR_OFFSET = (-42, 22)
# 空地中心相对白色小手中心的偏移
PLOT_OFFSET = (-27, -4)
# 种子栏：种子图标中心相对数量气泡中心的偏移
SEED_OFFSET = (0, 24)
# 好友家左上角显示好友名字的区域（窗口宽高比例），切换好友时这里会变
NAME_REGION = (0.04, 0.12, 0.35, 0.18)
# 任务书本图标右上角红色数字角标的位置（相对书本模板左上角：x0, y0, x1, y1）
TASK_BADGE_REL = (32, -10, 48, 6)


_log_sink = print


def set_log_sink(fn):
    """界面用：把日志输出重定向到别处。"""
    global _log_sink
    _log_sink = fn


def log(msg):
    _log_sink(f"[{dt.datetime.now():%H:%M:%S}] {msg}")


class Stopped(Exception):
    """收到停止信号。"""


def load_config(path=BASE_DIR / "config.ini"):
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";",))
    cfg.read(path, encoding="utf-8-sig")
    return cfg


def parse_pair(s):
    a, b = (float(v) for v in s.split(","))
    return a, b


def in_quiet_hours(spec):
    if not spec.strip():
        return False
    start, end = (int(v) for v in spec.split("-"))
    h = dt.datetime.now().hour
    return start <= h < end if start <= end else (h >= start or h < end)


class FarmBot:
    def __init__(self, cfg, stop_event=None):
        self.cfg = cfg
        self.stop_event = stop_event or threading.Event()
        self.next_round_at = None
        self.rounds = 0          # 统计：完成轮数
        self.friends_stolen = 0  # 统计：偷过的好友家数
        self.planted = 0         # 统计：播种次数
        self.errors = 0
        self.harvests = 0        # 统计：收菜次数
        self.farmed = 0          # 统计：自家一键务农次数
        self.friends_helped = 0  # 统计：帮好友务农家数
        self.reconnects = 0      # 统计：掉线重连次数
        self.lost_rounds = 0     # 连续几轮回不到农场画面
        self.relogins = 0        # 统计：被挤下线后重新登录次数
        self.tasks_claimed = 0   # 统计：领取任务奖励次数
        self.kicked_at = None    # 发现被挤下线的时间
        self.last_task_check = None
        self.milktea_claims = 0  # 统计：领取奶茶店收益次数
        self.last_milktea = None
        run = cfg["run"]
        self.milktea_interval = run.getfloat("milktea_interval_min", 60)
        self.relogin = run.getboolean("relogin", True)
        self.relogin_wait = run.getfloat("relogin_wait_min", 0)
        self.plant_pause_until = None
        run = cfg["run"]
        self.action_delay = run.getfloat("action_delay", 0.8)
        self.load_delay = run.getfloat("page_load_delay", 2.5)
        self.max_friends = run.getint("max_friends_per_round", 30)
        self.max_pages = run.getint("max_scroll_pages", 8)
        self.debug = run.getboolean("debug", False)

        m = cfg["match"]
        scales = tuple(float(s) for s in m.get("scales", "1.0").split(","))
        thresholds = {k: float(v) for k, v in m.items()
                      if k not in ("default_threshold", "scales")}
        regions = {}
        if cfg.has_section("regions"):
            regions = {k: tuple(float(x) for x in v.split(",")) for k, v in cfg["regions"].items()}
        self.matcher = Matcher(m.getfloat("default_threshold", 0.8), scales, thresholds, regions)

        self.win = GameWindow(cfg["window"].get("title", "QQ经典农场"),
                              cfg["window"].getboolean("activate", True))
        sc = cfg["friend_bar"]
        self.bar_from = parse_pair(sc.get("scroll_from", "0.85, 0.88"))
        self.bar_to = parse_pair(sc.get("scroll_to", "0.15, 0.88"))
        self.bar_region = tuple(float(x) for x in sc.get("region", "0, 0.83, 1, 0.93").split(","))
        pl = cfg["plant"] if cfg.has_section("plant") else {}
        self.seed_choice = pl.get("seed_choice", "most").strip()
        self.blank_spot = parse_pair(pl.get("blank_spot", "0.13, 0.71"))

    # ---------- 基础动作 ----------
    def check_stop(self):
        if self.stop_event.is_set():
            raise Stopped()

    def sleep(self, base):
        if self.stop_event.wait(base * random.uniform(0.9, 1.15)):
            raise Stopped()

    def grab(self):
        self.check_stop()
        return self.win.grab()

    def find_all(self, screen, role):
        hits = self.matcher.find_all(screen, role)
        if self.debug and hits:
            DEBUG_DIR.mkdir(exist_ok=True)
            imwrite(DEBUG_DIR / f"{dt.datetime.now():%H%M%S_%f}_{role}.png", annotate(screen, hits))
        return hits

    def find(self, screen, role):
        hits = self.find_all(screen, role)
        return hits[0] if hits else None

    def click(self, hit, wait=None):
        self.check_stop()
        human_click(self.win, hit)
        self.sleep(self.action_delay if wait is None else wait)

    def click_role(self, role, wait=None):
        hit = self.find(self.grab(), role)
        if hit:
            self.click(hit, wait)
        return hit is not None

    def wait_for(self, role, timeout=4.0):
        """等某个模板出现在画面上，返回命中或 None。"""
        end = time.time() + timeout
        while True:
            hit = self.find(self.grab(), role)
            if hit or time.time() > end:
                return hit
            self.sleep(0.15)

    def wait_gone(self, role, timeout=3.0):
        """等某个模板从画面上消失（比如点了「一键收获」后按钮消失 = 点成功了）。"""
        end = time.time() + timeout
        while time.time() < end:
            self.sleep(0.15)
            if not self.find(self.grab(), role):
                return True
        return False

    @staticmethod
    def crop_frac(screen, region):
        H, W = screen.shape[:2]
        x0, y0, x1, y1 = region
        return screen[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)]

    def wait_change(self, region, before, timeout=4.0, settle=0.4):
        """等画面某块区域发生变化（比如切换好友后左上角名字变了），变了再稍等 settle 秒让画面稳定。"""
        end = time.time() + timeout
        while time.time() < end:
            self.sleep(0.15)
            cur = self.crop_frac(self.grab(), region)
            if cur.shape == before.shape and np.abs(cur.astype(int) - before).mean() > 6:
                self.sleep(settle)
                return True
        return False

    def click_and_gone(self, role, timeout=3.0):
        """找到就点，并等它消失；返回是否点了。"""
        hit = self.find(self.grab(), role)
        if not hit:
            return False
        self.click(hit, wait=0.1)
        self.wait_gone(role, timeout)
        return True

    def click_until(self, role, expect, tries=3, timeout=3.0):
        """点击 role，直到 expect 出现（点击可能因为画面还在加载被吞掉，所以重试）。"""
        for _ in range(tries):
            hit = self.find(self.grab(), role)
            if hit:
                self.click(hit, wait=0.1)
            if self.wait_for(expect, timeout):
                return True
        return False

    def close_popups(self, rounds=3):
        if not self.matcher.has("close"):
            return
        for _ in range(rounds):
            if not self.click_role("close"):
                return

    def go_home(self):
        if self.click_role("go_home", wait=0.1):
            self.wait_for("friends", timeout=self.load_delay + 3)   # 等自家画面出来就继续
            self.sleep(0.3)
            self.close_popups()

    def click_each(self, role, limit=30):
        """连续点击某类图标直到画面上没有（每次点完重新截图，防止位置变化）。
        同一位置点过一次还在（比如偷不了），就不再点它。"""
        n, clicked = 0, []
        for _ in range(limit):
            hits = [h for h in self.find_all(self.grab(), role)
                    if all(abs(h.x - x) > 8 or abs(h.y - y) > 8 for x, y in clicked)]
            if not hits:
                break
            hit = hits[0]
            clicked.append((hit.x, hit.y))
            self.click(hit, wait=0.25)
            n += 1
        return n

    # ---------- 收自己的菜 ----------
    def harvest_home(self):
        log("检查成熟作物")
        harvested = False
        if self.click_and_gone("harvest_all"):
            log("  点击了一键收获")
            harvested = True
            self.close_popups()
        n = self.click_each("ripe")
        if n:
            log(f"  逐个收获 {n} 次")
            harvested = True
            self.close_popups()
        if harvested:
            self.harvests += 1
            self.sleep(1.0)        # 等收获动画 / 奖励弹窗
            self.ensure_home()     # 有弹窗挡着就处理掉

    # ---------- 种菜 ----------
    def read_seed_bar(self, screen, plot):
        """点空地后弹出的种子栏：返回 [(数量, x, y)]；没弹出种子栏返回 None。
        用 OCR 找「一键种植」当锚点，它上方一排纯数字就是各种子的数量。"""
        import ocr
        H = screen.shape[0]
        y0, y1 = max(0, plot.y - 60), min(H, plot.y + 220)
        words = ocr.read_words(screen[y0:y1])
        anchor = next((w for w in words if "种植" in w[0]), None)
        if anchor is None:
            return None
        ay = int(anchor[2] + y0)
        # 第二遍：只截锚点上方那一条（数量气泡所在行），放大后再识别，单个数字也能认出
        sy0, sy1 = max(0, ay - 105), max(0, ay - 40)
        strip = cv2.resize(screen[sy0:sy1], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        seeds = []
        for text, cx, cy, _ in ocr.read_words(strip):
            n = ocr.to_int(text)
            if n:   # 数量 0 的种子不会显示，读出 0 多半是把图标误认成数字
                seeds.append((n, int(cx / 2), int(cy / 2) + sy0))
        if seeds:   # 只保留和多数气泡在同一行的
            row_y = sorted(s[2] for s in seeds)[len(seeds) // 2]
            seeds = [s for s in seeds if abs(s[2] - row_y) <= 6]
        return sorted(seeds, key=lambda s: s[1])

    def dismiss(self):
        """点一下空白草地，关掉种子栏 / 施肥栏之类的浮层。"""
        _, _, w, h = self.win.client_rect()
        fx, fy = self.blank_spot
        self.click(Hit("blank", 1, int(fx * w) - 4, int(fy * h) - 4, 8, 8), wait=0.8)

    def find_empty_plot(self):
        """返回一块空地的点击位置（Hit），没有空地返回 None。
        主要靠 empty_tile（空地块中间没有种子堆/幼苗）；白色小手 empty_plot 只是偶尔出现的提示，作为补充。"""
        for _ in range(5):   # 小手是动画，多看几帧
            screen = self.grab()
            tiles = self.find_all(screen, "empty_tile")
            if tiles:
                t = max(tiles, key=lambda h: h.score)
                cx, cy = t.center
                return Hit("plot", t.score, cx - 4, cy - 4, 8, 8)
            hand = self.find(screen, "empty_plot")
            if hand:
                cx, cy = hand.center
                return Hit("plot", hand.score, cx + PLOT_OFFSET[0] - 4, cy + PLOT_OFFSET[1] - 4, 8, 8)
            self.sleep(0.2)
        return None

    def save_shot(self, tag, screen=None, keep=40):
        """存一张排查用截图到 debug/<tag>_时间.png，只保留最近 keep 张。"""
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            imwrite(DEBUG_DIR / f"{tag}_{dt.datetime.now():%m%d_%H%M%S}.png",
                    self.grab() if screen is None else screen)
            old = sorted(DEBUG_DIR.glob(f"{tag}_*.png"))[:-keep]
            for f in old:
                f.unlink()
        except Exception:
            pass

    def plant_home(self):
        """有空地就种：点空地 → 种子栏选数量最多（或第一个）的种子。
        「一键种植」勾选时一次种满所有空地；没勾选或种子不够就再来一次。
        点完种子后核对：种子栏还开着 / 种子数量没变 = 这种种子种不上，换下一种。"""
        if not (self.matcher.has("empty_tile") or self.matcher.has("empty_plot")):
            return
        if self.plant_pause_until and dt.datetime.now() < self.plant_pause_until:
            return
        bad = []        # 本轮种不上的种子（按在种子栏里的 x 位置记）
        last = None     # 上一次点的种子 (数量, x, 当时所有数量)
        for _ in range(12):
            plot = self.find_empty_plot()
            if not plot:
                if last:
                    log("  空地都种上了")
                return
            self.click(plot, wait=0.7)
            screen = self.grab()
            seeds = self.read_seed_bar(screen, plot)
            self.save_shot("plant", screen)
            if seeds is None:
                log("  点了空地但没出现种子栏，跳过种菜（截图在 debug/plant_*.png）")
                self.dismiss()
                return
            counts = sorted(s[0] for s in seeds)
            if last is not None:
                if counts == last[2]:
                    bad.append(last[1])
                    log(f"  数量 {last[0]} 的种子没种上（可能这块地种不了），换一种")
                else:
                    log("  种上了一部分，还有空地，继续")
            if not seeds:
                log("  有空地，但没有种子了，不种")
                self.dismiss()
                return
            cands = [s for s in seeds if all(abs(s[1] - bx) > 12 for bx in bad)]
            if not cands:
                log("  现有种子都种不上，1 小时后再试（截图在 debug/plant_*.png）")
                self.plant_pause_until = dt.datetime.now() + dt.timedelta(hours=1)
                self.dismiss()
                return
            if self.seed_choice == "first":
                n, sx, sy = min(cands, key=lambda s: s[1])
            else:
                n, sx, sy = max(cands, key=lambda s: s[0])
            log(f"  种子数量：{'、'.join(str(s[0]) for s in seeds)}，种数量 {n} 的那种")
            self.click(Hit("seed", 1, sx + SEED_OFFSET[0] - 6, sy + SEED_OFFSET[1] - 6, 12, 12),
                       wait=1.2)
            self.planted += 1
            last = (n, sx, counts)
            # 种子栏如果还开着，说明没种上：直接按“数量没变”处理
            after = self.grab()
            if self.read_seed_bar(after, plot) is not None:
                self.save_shot("plant_fail", after)
                self.planted -= 1
                bad.append(sx)
                log(f"  点了数量 {n} 的种子但种子栏没关，没种上，换一种")
                cands = [s for s in seeds if all(abs(s[1] - bx) > 12 for bx in bad)]
                if not cands:
                    log("  现有种子都种不上，1 小时后再试（截图在 debug/plant_fail_*.png）")
                    self.plant_pause_until = dt.datetime.now() + dt.timedelta(hours=1)
                    self.dismiss()
                    return
                self.dismiss()
                last = None
        log("  种菜重复次数过多，停止本轮种菜")

    # ---------- 保证在自家农场（掉线 / 弹窗恢复） ----------
    # 画面认不出时，用 OCR 找这些按钮点（越靠前越优先）
    RECOVER_WORDS = ["重新连接", "重连", "重新进入", "重新加载", "重新登录", "重试", "重启",
                     "进入游戏", "开始游戏", "返回游戏", "继续游戏", "回到农场", "返回农场",
                     "我知道了", "知道了", "收下", "领取", "确定", "确认", "关闭"]
    # 画面上有这些字时，不点「确定/确认」（避免误买东西）
    DANGER_WORDS = ["支付", "购买", "充值", "付款", "花费", "消耗", "钻石", "点券"]

    def click_text_button(self, screen):
        import ocr
        words = ocr.read_words(screen)
        texts = " ".join(w[0] for w in words)
        danger = any(d in texts for d in self.DANGER_WORDS)
        for key in self.RECOVER_WORDS:
            if danger and key in ("确定", "确认", "领取"):
                continue
            for text, cx, cy, score in words:
                if key in text and len(text) <= len(key) + 4 and score > 0.6:
                    log(f"  点击画面上的「{text}」")
                    self.click(Hit("text", score, int(cx) - 5, int(cy) - 5, 10, 10), wait=3.0)
                    return True
        return False

    @staticmethod
    def is_dimmed(screen):
        """弹窗出现时整个画面会盖一层暗色遮罩：天空和左下草地都变暗。
        （正常画面天空亮度约 235、开着面板约 200，弹窗遮罩下只有约 70）"""
        v = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)[:, :, 2]
        H, W = v.shape
        sky = v[int(.18 * H):int(.24 * H), int(.33 * W):int(.66 * W)].mean()
        grass = v[int(.735 * H):int(.78 * H), int(.04 * W):int(.27 * W)].mean()
        return sky < 120 and grass < 110

    def ensure_home(self, tries=10):
        """确认现在在自家农场；不在就想办法回来（重新连接、回家按钮、关面板、点确定之类的按钮）。
        注意：弹窗下面的「好友」按钮仍然能认出来，所以先处理掉线 / 弹窗，再判断是否在家。"""
        saved = False
        for _ in range(tries):
            screen = self.grab()
            hit = self.find(screen, "relogin")
            if hit:
                if self.kicked_at is None:
                    self.kicked_at = dt.datetime.now()
                    self.save_shot("unknown", screen, keep=20)
                    log("  账号被挤下线了（在其他地方登录）")
                if not self.relogin:
                    log("  自动重新登录已关闭，本轮跳过")
                    self.lost_rounds += 1
                    return False
                due = self.kicked_at + dt.timedelta(minutes=self.relogin_wait)
                if dt.datetime.now() < due:
                    log(f"  按设置等到 {due:%H:%M} 再重新登录，本轮跳过")
                    self.lost_rounds += 1
                    return False
                log("  点击重新登录")
                self.relogins += 1
                self.kicked_at = None
                saved = True
                self.click(hit, wait=0.1)
                self.wait_gone("relogin", timeout=self.load_delay * 3)
                continue
            hit = self.find(screen, "reconnect")
            if hit:
                log("  检测到「网络连接超时」，点击重新连接")
                self.save_shot("unknown", screen, keep=20)
                self.reconnects += 1
                saved = True
                self.click(hit, wait=self.load_delay * 3)
                continue
            # 画面变暗 = 多半有弹窗；能找到按钮就点掉（找不到再按正常画面处理，防止游戏夜景之类误判）
            if self.is_dimmed(screen):
                if not saved:
                    log("  画面变暗，可能有弹窗，截图存到 debug/unknown_*.png")
                    self.save_shot("unknown", screen, keep=20)
                    saved = True
                if self.click_text_button(screen):
                    continue
            if self.find(screen, "friends"):
                if saved:
                    log("  已恢复到自家农场")
                self.lost_rounds = 0
                return True
            if self.find(screen, "go_home"):
                self.go_home()
                continue
            hit = (self.find(screen, "panel_close") or self.find(screen, "close")
                   or self.find(screen, "milktea_close") or self.find(screen, "milktea_back"))
            if hit:
                self.click(hit, wait=1.0)
                continue
            if not saved:
                log("  画面认不出（可能掉线 / 更新 / 弹窗），截图存到 debug/unknown_*.png，尝试恢复")
                self.save_shot("unknown", screen, keep=20)
                saved = True
            if not self.click_text_button(screen):
                self.sleep(4)   # 可能在加载，等一会儿
        self.lost_rounds += 1
        log(f"  无法回到农场画面（已连续 {self.lost_rounds} 轮），本轮跳过。"
            "如果一直这样，请看一下 QQ 农场窗口，截图在 debug/unknown_*.png")
        return False

    # ---------- 偷菜 ----------
    def steal_in_farm(self):
        got = 0
        if self.click_and_gone("steal_all", timeout=2.5):
            got += 1
            self.close_popups()
        role = "steal_ripe" if self.matcher.has("steal_ripe") else "ripe"
        got += self.click_each(role)
        self.close_popups()
        return got

    def in_friend_farm(self, screen=None):
        return self.find(self.grab() if screen is None else screen, "go_home") is not None

    def enter_any_friend(self):
        """从自家 → 好友列表 → 拜访第一个好友，进入好友家（之后用底部好友栏切换）。"""
        if self.in_friend_farm():
            return True
        if not self.click_until("friends", "visit"):
            log("  好友列表没有打开")
            return False
        if not self.click_until("visit", "go_home", timeout=self.load_delay + 2):
            log("  拜访好友失败")
            self.click_role("panel_close")
            return False
        self.sleep(0.6)   # 「回家」按钮先出来，地里的作物 / 按钮稍晚一点
        self.close_popups()
        return True

    def avatar_hit(self, screen, mark):
        """由「可偷」小图标推算出好友头像的位置（点头像切换好友）。"""
        cx, cy = mark.center
        ax, ay = cx + AVATAR_OFFSET[0], cy + AVATAR_OFFSET[1]
        H, W = screen.shape[:2]
        x0, y0 = max(0, ax - 15), max(0, ay - 15)
        x1, y1 = min(W, ax + 15), min(H, ay + 15)
        return Hit("friend", mark.score, x0, y0, x1 - x0, y1 - y0)

    @staticmethod
    def same_face(a, b):
        if a.shape != b.shape:
            return False
        return cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)[0, 0] > 0.9

    def bar_crop(self, screen):
        H, W = screen.shape[:2]
        x0, y0, x1, y1 = self.bar_region
        return screen[int(y0 * H):int(y1 * H), int(x0 * W):int(x1 * W)]

    def work_in_friend_farm(self, steal, help_):
        """在当前好友家偷菜 / 帮忙务农，返回描述文字。"""
        parts = []
        if steal:
            got = self.steal_in_farm()
            if got:
                self.friends_stolen += 1
            parts.append(f"偷取点击 {got} 次")
        if help_:
            if self.click_and_gone("farm_all", timeout=2.5):
                self.close_popups()
                self.friends_helped += 1
                parts.append("帮忙务农")
            else:
                parts.append("不需要帮忙")
        return "，".join(parts)

    def visit_friends(self, steal=True, help_=False):
        """进好友家，用底部好友栏挨个找「可偷」（墨镜小手）/「需要帮忙」（小农夫）的好友。"""
        roles = [r for r, on in (("friend_steal", steal), ("friend_help", help_))
                 if on and self.matcher.has(r)]
        if not roles:
            log("没有好友标记模板，跳过好友")
            return
        what = "、".join(n for n, on in (("偷菜", steal), ("帮好友务农", help_)) if on)
        log(f"开始去好友家（{what}）")
        if not self.enter_any_friend():
            log("  没能进入好友家，跳过")
            return
        # 第一个拜访的好友也顺手处理一下
        log(f"  列表第一位好友：{self.work_in_friend_farm(steal, help_)}")
        visited, scrolls, tried = 0, 0, []
        while visited < self.max_friends:
            screen = self.grab()
            if not self.in_friend_farm(screen):
                log("  已不在好友家，结束")
                break
            cands = []
            marks = [m for r in roles for m in self.find_all(screen, r)]
            for mark in marks:
                hit = self.avatar_hit(screen, mark)
                face = cv2.cvtColor(screen[hit.y:hit.y + hit.h, hit.x:hit.x + hit.w], cv2.COLOR_BGR2GRAY)
                if (not any(self.same_face(face, f) for f in tried)
                        and all(abs(hit.x - c[0].x) > 20 for c in cands)):   # 一个人同时有两种标记只算一次
                    cands.append((hit, face))
            if not cands:
                # 当前可见的好友栏里没有可偷的了，往左拖一段；拖不动说明到头了
                if scrolls >= self.max_pages:
                    break
                before = self.bar_crop(screen)
                self.check_stop()
                drag(self.win, self.bar_from, self.bar_to)
                self.sleep(0.5)
                after = self.bar_crop(self.grab())
                if before.shape == after.shape and np.abs(after.astype(int) - before).mean() < 3:
                    break
                scrolls += 1
                continue
            hit, face = min(cands, key=lambda c: c[0].x)   # 从左到右依次处理
            tried.append(face)   # 偷不了 / 帮不了时标记还在，不会反复点同一个人
            name_before = self.crop_frac(screen, NAME_REGION)
            self.click(hit, wait=0.1)
            # 左上角好友名字变了就说明切换过去了，不用固定等几秒
            self.wait_change(NAME_REGION, name_before, timeout=self.load_delay + 1.5, settle=0.5)
            self.close_popups()
            visited += 1
            log(f"  第 {visited} 位好友：{self.work_in_friend_farm(steal, help_)}")
        log(f"好友家处理完，共去了 {visited} 位好友家")
        self.go_home()

    # ---------- 自家务农 ----------
    def farm_home(self):
        """自家地里有草 / 虫 / 缺水时，底部按钮会变成「一键务农」，点它。"""
        n = 0
        for _ in range(3):
            if not self.click_and_gone("farm_all", timeout=2.5):
                break
            n += 1
            self.close_popups()
        if n:
            log("  点击了一键务农")
            self.farmed += 1
            self.sleep(0.5)
            self.ensure_home()

    # ---------- 每日任务 ----------
    def has_task_badge(self, screen, icon):
        """书本右上角有红色数字角标 = 有任务奖励可以领。"""
        x0, y0, x1, y1 = TASK_BADGE_REL
        crop = screen[max(0, icon.y + y0):icon.y + y1, max(0, icon.x + x0):icon.x + x1]
        if crop.size == 0:
            return False
        h = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        red = ((h[:, :, 0] < 10) | (h[:, :, 0] > 170)) & (h[:, :, 1] > 120) & (h[:, :, 2] > 60)
        return red.mean() > 0.25

    def claim_tasks(self, force=False):
        """打开任务面板，OCR 找「领取」按钮挨个点（不点「前往」「已完成」），领完关面板。
        平时只在书本上有红色角标时才打开；另外每 2 小时兜底检查一次。"""
        screen = self.grab()
        icon = self.find(screen, "task_icon")
        if not icon:
            return
        now = dt.datetime.now()
        overdue = self.last_task_check is None or now - self.last_task_check > dt.timedelta(hours=2)
        if not (force or overdue or self.has_task_badge(screen, icon)):
            return
        self.last_task_check = now
        import ocr
        self.click(icon, wait=0.1)
        if not self.wait_for("panel_close", timeout=3):
            log("  任务面板没打开")
            self.ensure_home()
            return
        self.sleep(0.5)
        got, clicked = 0, []
        for _ in range(10):
            words = ocr.read_words(self.grab())
            btns = [(t, cx, cy) for t, cx, cy, sc in words
                    if "领取" in t and "已" not in t and len(t) <= 6 and sc > 0.6
                    and all(abs(cx - x) > 10 or abs(cy - y) > 10 for x, y in clicked)]
            if not btns:
                break
            t, cx, cy = min(btns, key=lambda b: b[2])   # 从上往下领
            clicked.append((cx, cy))
            log(f"  领取任务奖励：点击「{t}」")
            self.click(Hit("task", 1, int(cx) - 5, int(cy) - 5, 10, 10), wait=1.0)
            got += 1
            s = self.grab()
            if self.is_dimmed(s):        # 领奖励后可能弹出奖励展示，点掉
                self.click_text_button(s)
        if got:
            self.tasks_claimed += got
            log(f"  共领取 {got} 个任务奖励")
        self.click_role("panel_close", wait=0.5)
        self.ensure_home()

    # ---------- 奶茶店收益 ----------
    @staticmethod
    def is_colorful(screen, hit, min_sat=60):
        """按钮是彩色（可点）还是灰色（不可点）：灰按钮饱和度只有个位数，黄按钮 150 左右。"""
        crop = screen[hit.y:hit.y + hit.h, hit.x:hit.x + hit.w]
        return cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)[:, :, 1].mean() > min_sat

    def leave_milktea(self):
        """从奶茶店回到自家农场：先关「累计收益」窗口，再点左上角返回箭头。"""
        for _ in range(4):
            screen = self.grab()
            if self.find(screen, "friends") and not self.find(screen, "milktea_back"):
                break
            hit = self.find(screen, "milktea_close") or self.find(screen, "milktea_back")
            if not hit:
                break
            self.click(hit, wait=0.6)
        self.wait_for("friends", timeout=self.load_delay + 3)
        self.ensure_home()

    def claim_milktea(self, force=False):
        """打开「奶茶热销中」→ 领取收益 → 领取 → 关掉「恭喜获得」→ 返回农场。
        收益最多攒 48 小时，所以默认每 60 分钟去领一次就够了。不会点「一键爆单」。"""
        now = dt.datetime.now()
        if not force and self.last_milktea and now - self.last_milktea < dt.timedelta(minutes=self.milktea_interval):
            return
        hit = self.find(self.grab(), "milktea_icon")
        if not hit:
            return
        self.last_milktea = now
        self.click(hit, wait=0.1)
        claim = self.wait_for("milktea_claim", timeout=self.load_delay + 6)   # 奶茶店加载要 2~3 秒
        if not claim:
            log("  奶茶店没打开")
            self.leave_milktea()
            return
        self.sleep(0.3)
        self.click(claim, wait=0.1)
        take = self.wait_for("milktea_take", timeout=3)
        screen = self.grab()
        if take and self.is_colorful(screen, take):
            amount = self.read_milktea_amount(screen)
            self.click(take, wait=1.2)
            self.milktea_claims += 1
            log(f"  领取了奶茶店收益{amount}")
            # 「恭喜获得」：点空白处（顶部标题栏位置，在遮罩上）关闭
            _, _, w, h = self.win.client_rect()
            self.click(Hit("blank", 1, w // 2 - 4, int(h * 0.09) - 4, 8, 8), wait=0.8)
        else:
            log("  奶茶店暂时没有收益可领")
        self.leave_milktea()

    def read_milktea_amount(self, screen):
        """OCR「合计收益」那一行的奶茶券 / 金币数量，只用来写日志，读不出也无所谓。"""
        try:
            import ocr
            H = screen.shape[0]
            nums = [ocr.to_int(t) for t, cx, cy, sc in ocr.read_words(screen[int(H * .71):int(H * .83)])]
            nums = [n for n in nums if n]
            if len(nums) >= 2:
                return f"（奶茶券 {nums[0]}，金币 {nums[1]}）"
        except Exception:
            pass
        return ""

    # ---------- 一轮 ----------
    def run_round(self):
        run = self.cfg["run"]
        if not self.ensure_home():
            return
        if run.getboolean("do_harvest", True):
            self.harvest_home()
        if run.getboolean("do_farm", True):
            self.farm_home()
        if run.getboolean("do_plant", True):
            self.plant_home()
        steal, help_ = run.getboolean("do_steal", True), run.getboolean("do_help", False)
        if steal or help_:
            self.visit_friends(steal, help_)
            self.ensure_home()
        if run.getboolean("do_milktea", True):
            self.claim_milktea()
        if run.getboolean("do_tasks", True):   # 放最后：采摘、去好友家互动这些任务这时已经做完了
            self.claim_tasks()

    def loop(self, once=False, wake_event=None):
        """循环运行，直到 stop_event 被设置（Stopped 异常会抛给调用者）。
        长期挂机：单轮出错只记日志，下一轮继续；wake_event 被设置时跳过等待立即开始下一轮。"""
        run = self.cfg["run"]
        interval = run.getfloat("round_interval_min", 10)
        jitter = run.getfloat("interval_jitter_min", 3)
        while True:
            self.next_round_at = None
            if in_quiet_hours(run.get("quiet_hours", "")):
                log("休息时段，暂不操作")
            else:
                try:
                    self.run_round()
                    self.rounds += 1
                except (Stopped, pyautogui.FailSafeException):
                    raise
                except Exception as e:
                    self.errors += 1
                    log(f"本轮出错：{e}（下一轮再试）")
            if once:
                return
            wait = max(1.0, interval + random.uniform(-jitter, jitter))
            if self.lost_rounds:   # 回不到农场画面时，2 分钟后就再试
                wait = 2.0
            self.next_round_at = dt.datetime.now() + dt.timedelta(minutes=wait)
            log(f"本轮结束，下一轮在 {self.next_round_at:%H:%M:%S}（约 {wait:.1f} 分钟后）")
            end = time.time() + wait * 60
            while time.time() < end:
                if self.stop_event.wait(1):
                    raise Stopped()
                if wake_event is not None and wake_event.is_set():
                    wake_event.clear()
                    log("手动触发，立即开始下一轮")
                    break

    # ---------- 测试识别 ----------
    def test(self):
        screen = self.grab()
        DEBUG_DIR.mkdir(exist_ok=True)
        imwrite(DEBUG_DIR / "test_screen.png", screen)
        hits = []
        print(f"窗口截图 {screen.shape[1]}x{screen.shape[0]}，已存 debug/test_screen.png\n")
        print(f"{'模板':<14}{'最高分':>8}{'阈值':>8}  {'命中数':>6}  说明")
        for role, desc in ROLES.items():
            if not self.matcher.has(role):
                print(f"{role:<14}{'--':>8}{'':>8}  {'':>6}  （未截模板）{desc}")
                continue
            best = self.matcher.best_score(screen, role)
            thr = self.matcher.thresholds.get(role, self.matcher.default_threshold)
            found = self.matcher.find_all(screen, role)
            hits += found
            print(f"{role:<14}{best.score:>8.3f}{thr:>8.2f}  {len(found):>6}  {desc}")
        imwrite(DEBUG_DIR / "test_result.png", annotate(screen, hits))
        print("\n标注结果已存 debug/test_result.png（红框 = 达到阈值的命中）")


def main():
    ap = argparse.ArgumentParser(description="QQ 经典农场自动收菜 / 偷菜")
    ap.add_argument("--once", action="store_true", help="只运行一轮")
    ap.add_argument("--test", action="store_true", help="测试模板识别")
    ap.add_argument("--list-windows", action="store_true", help="列出窗口标题")
    ap.add_argument("--config", default=str(BASE_DIR / "config.ini"))
    args = ap.parse_args()

    if args.list_windows:
        for hwnd, title in list_windows():
            print(f"{hwnd:>10}  {title}")
        return

    bot = FarmBot(load_config(args.config))
    if args.test:
        bot.test()
        return
    log("启动。紧急停止：鼠标甩到屏幕左上角 或 Ctrl+C")
    try:
        bot.loop(once=args.once)
    except (KeyboardInterrupt, Stopped):
        log("已手动停止")
    except Exception as e:   # pyautogui.FailSafeException 等
        log(f"停止：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
