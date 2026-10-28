"""截取模板图片。

用法：
    python capture_template.py <角色名>     截取农场窗口，用鼠标框选图标，保存为 templates/<角色名>.png
    python capture_template.py --shot       只保存一张整窗口截图到 debug/

框选操作：按住左键拖出方框 → 按 回车/空格 确认；按 c 取消。
同一个角色再次截取会自动存为 <角色名>_2.png、_3.png ...，多张模板会一起参与识别。
"""
import argparse
import time

import cv2

from farm_bot import ROLES, load_config
from farmlib import DEBUG_DIR, TEMPLATE_DIR, GameWindow, imwrite


def next_path(role):
    p = TEMPLATE_DIR / f"{role}.png"
    i = 2
    while p.exists():
        p = TEMPLATE_DIR / f"{role}_{i}.png"
        i += 1
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("role", nargs="?", help="角色名：" + " / ".join(ROLES))
    ap.add_argument("--shot", action="store_true", help="只保存整窗口截图")
    ap.add_argument("--delay", type=float, default=0, help="延迟几秒再截图（方便先切到某个画面）")
    args = ap.parse_args()

    if not args.shot and args.role not in ROLES:
        print("请指定角色名，可选：")
        for k, v in ROLES.items():
            print(f"  {k:<14}{v}")
        return

    cfg = load_config()
    win = GameWindow(cfg["window"].get("title"), activate=True)
    if args.delay:
        print(f"{args.delay} 秒后截图...")
        time.sleep(args.delay)
    screen = win.grab()

    if args.shot:
        DEBUG_DIR.mkdir(exist_ok=True)
        path = DEBUG_DIR / f"shot_{time.strftime('%H%M%S')}.png"
        imwrite(path, screen)
        print(f"已保存 {path}")
        return

    print(f"框选「{args.role}」：{ROLES[args.role]}")
    print("拖动鼠标框选 → 回车确认，按 c 取消")
    # 窗口标题用英文，OpenCV 窗口不支持中文标题
    title = "Select template - Enter to save, c to cancel"
    cv2.namedWindow(title, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO)
    cv2.resizeWindow(title, screen.shape[1], screen.shape[0])
    x, y, w, h = cv2.selectROI(title, screen, showCrosshair=True, fromCenter=False)
    cv2.destroyAllWindows()
    if w == 0 or h == 0:
        print("已取消")
        return
    TEMPLATE_DIR.mkdir(exist_ok=True)
    path = next_path(args.role)
    imwrite(path, screen[y:y + h, x:x + w])
    print(f"已保存 {path}（{w}x{h}）")


if __name__ == "__main__":
    main()
