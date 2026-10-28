"""窗口查找、截图、模板匹配、点击等底层工具。"""
import ctypes
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path

# 必须在 pyautogui 之前设置，否则高分屏缩放下截图坐标和点击坐标对不上
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import cv2
import mss
import numpy as np
import pyautogui
import win32api
import win32con
import win32gui
import win32process

pyautogui.FAILSAFE = True   # 鼠标甩到屏幕左上角 = 紧急停止
pyautogui.PAUSE = 0.05

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
DEBUG_DIR = BASE_DIR / "debug"


def imread(path):
    """支持中文路径的读图。"""
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)


def imwrite(path, img):
    ok, buf = cv2.imencode(Path(path).suffix or ".png", img)
    if ok:
        buf.tofile(str(path))
    return ok


def list_windows():
    titles = []

    def cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if t.strip():
                titles.append((hwnd, t))
    win32gui.EnumWindows(cb, None)
    return titles


class GameWindow:
    def __init__(self, title_keyword, activate=True):
        self.keyword = title_keyword
        self.activate = activate
        self.hwnd = None

    def find(self):
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            return self.hwnd
        for hwnd, title in list_windows():
            # 跳过本程序自己的窗口（比如界面标题里也带“农场”）
            if win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid():
                continue
            if self.keyword in title:
                self.hwnd = hwnd
                return hwnd
        raise RuntimeError(f"找不到标题包含「{self.keyword}」的窗口，请先在 QQ 里打开农场小程序")

    def bring_to_front(self):
        hwnd = self.find()
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.5)
        if win32gui.GetForegroundWindow() != hwnd:
            # Windows 限制后台程序抢焦点，先模拟按一下 Alt 绕过
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            time.sleep(0.3)

    def client_rect(self):
        """返回窗口客户区在屏幕上的 (left, top, width, height)。"""
        hwnd = self.find()
        l, t, r, b = win32gui.GetClientRect(hwnd)
        sx, sy = win32gui.ClientToScreen(hwnd, (l, t))
        return sx, sy, r - l, b - t

    def grab(self):
        if self.activate:
            self.bring_to_front()
        x, y, w, h = self.client_rect()
        if w <= 0 or h <= 0:
            raise RuntimeError("窗口大小为 0，可能被最小化了")
        with getattr(mss, "MSS", mss.mss)() as sct:
            shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
        return cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)

    def to_screen(self, cx, cy):
        x, y, _, _ = self.client_rect()
        return x + cx, y + cy


@dataclass
class Hit:
    role: str
    score: float
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self):
        return self.x + self.w // 2, self.y + self.h // 2


def _nms(cands, iou_thresh=0.3):
    cands.sort(key=lambda c: c[0], reverse=True)
    kept = []
    for s, x, y, w, h in cands:
        ok = True
        for _, kx, ky, kw, kh in kept:
            ix = max(0, min(x + w, kx + kw) - max(x, kx))
            iy = max(0, min(y + h, ky + kh) - max(y, ky))
            inter = ix * iy
            if inter / float(w * h + kw * kh - inter) > iou_thresh:
                ok = False
                break
        if ok:
            kept.append((s, x, y, w, h))
    return kept


class Matcher:
    """一个“角色”(role) 可以有多张模板：templates/<role>.png、<role>_2.png ..."""

    def __init__(self, default_threshold=0.8, scales=(1.0,), thresholds=None, regions=None):
        self.default_threshold = default_threshold
        self.scales = scales
        self.thresholds = thresholds or {}
        # role -> (x0, y0, x1, y1)，窗口宽高比例，只在这个范围内找
        self.regions = regions or {}
        self._cache = {}
        self._warned = set()

    def templates(self, role):
        if role not in self._cache:
            files = sorted(TEMPLATE_DIR.glob(f"{role}.png")) + sorted(TEMPLATE_DIR.glob(f"{role}_*.png"))
            self._cache[role] = [imread(f) for f in files]
        return self._cache[role]

    def has(self, role):
        return bool(self.templates(role))

    def _scaled(self, tpl):
        for s in self.scales:
            if s == 1.0:
                yield tpl
            else:
                interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
                yield cv2.resize(tpl, None, fx=s, fy=s, interpolation=interp)

    def find_all(self, screen, role, threshold=None):
        tpls = self.templates(role)
        if not tpls:
            if role not in self._warned:
                print(f"  [提示] 没有「{role}」的模板，跳过相关步骤")
                self._warned.add(role)
            return []
        thr = threshold or self.thresholds.get(role, self.default_threshold)
        screen, ox, oy = self._crop_region(screen, role)
        H, W = screen.shape[:2]
        cands = []
        for tpl in tpls:
            for t in self._scaled(tpl):
                th, tw = t.shape[:2]
                if th > H or tw > W:
                    continue
                res = cv2.matchTemplate(screen, t, cv2.TM_CCOEFF_NORMED)
                ys, xs = np.where(res >= thr)
                if len(xs) > 3000:   # 阈值过低时点太多，只保留最高分的一批
                    idx = np.argsort(res[ys, xs])[-3000:]
                    ys, xs = ys[idx], xs[idx]
                cands += [(float(res[y, x]), int(x) + ox, int(y) + oy, tw, th) for x, y in zip(xs, ys)]
        return [Hit(role, *c) for c in _nms(cands)]

    def _crop_region(self, screen, role):
        if role not in self.regions:
            return screen, 0, 0
        H, W = screen.shape[:2]
        x0, y0, x1, y1 = self.regions[role]
        ox, oy = int(x0 * W), int(y0 * H)
        return screen[oy:int(y1 * H), ox:int(x1 * W)], ox, oy

    def find(self, screen, role, threshold=None):
        hits = self.find_all(screen, role, threshold)
        return hits[0] if hits else None

    def best_score(self, screen, role):
        """不管阈值，返回最高相似度及位置（用于 --test 调阈值）。"""
        best = None
        screen, ox, oy = self._crop_region(screen, role)
        for tpl in self.templates(role):
            for t in self._scaled(tpl):
                th, tw = t.shape[:2]
                if th > screen.shape[0] or tw > screen.shape[1]:
                    continue
                _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(screen, t, cv2.TM_CCOEFF_NORMED))
                if best is None or mx > best.score:
                    best = Hit(role, float(mx), loc[0] + ox, loc[1] + oy, tw, th)
        return best


def human_click(win, hit, jitter=0.25):
    """在命中区域中心附近随机偏移后点击。"""
    cx, cy = hit.center
    cx += int(random.uniform(-jitter, jitter) * hit.w)
    cy += int(random.uniform(-jitter, jitter) * hit.h)
    sx, sy = win.to_screen(cx, cy)
    pyautogui.moveTo(sx, sy, duration=random.uniform(0.04, 0.12))
    pyautogui.click()


def drag(win, start, end):
    """start/end 为窗口宽高比例 (fx, fy)。"""
    x, y, w, h = win.client_rect()
    sx, sy = x + int(start[0] * w), y + int(start[1] * h)
    ex, ey = x + int(end[0] * w), y + int(end[1] * h)
    pyautogui.moveTo(sx, sy, duration=0.08)
    pyautogui.mouseDown()
    pyautogui.moveTo(ex, ey, duration=random.uniform(0.3, 0.4))
    time.sleep(0.1)   # 停一下再松手，避免惯性甩太远
    pyautogui.mouseUp()


def annotate(screen, hits):
    img = screen.copy()
    for h in hits:
        cv2.rectangle(img, (h.x, h.y), (h.x + h.w, h.y + h.h), (0, 0, 255), 2)
        cv2.putText(img, f"{h.role} {h.score:.2f}", (h.x, max(12, h.y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 1, cv2.LINE_AA)
    return img
