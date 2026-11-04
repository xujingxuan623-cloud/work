"""生成程序图标 assets/sprout.ico（多尺寸）和 assets/farm.png。改了样式重新运行一次即可：
    python tools/make_icon.py

风格仿苹果 App 图标：超椭圆（圆润的方形）外形 + 柔和的绿色渐变 + 一株白色嫩芽，不加多余装饰。
"""
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets"
S = 1024   # 先画大图再缩小，边缘更平滑


def squircle_mask(size, n=5.0, margin=0.0):
    """苹果图标那种「连续曲率」圆角方形：|x|^n + |y|^n <= 1。"""
    ss = size * 4
    c = np.linspace(-1 - margin, 1 + margin, ss)
    x, y = np.meshgrid(c, c)
    m = (np.abs(x) ** n + np.abs(y) ** n <= 1).astype(np.uint8) * 255
    return Image.fromarray(m, "L").resize((size, size), Image.LANCZOS)


def gradient(size, top, bottom):
    """竖向渐变，左上角再加一点柔光，让颜色不死板。"""
    t = np.linspace(0, 1, size)[:, None, None]
    img = np.array(top)[None, None] * (1 - t) + np.array(bottom)[None, None] * t
    img = np.repeat(img, size, axis=1)
    yy, xx = np.mgrid[0:size, 0:size] / size
    glow = np.exp(-(((xx - 0.3) ** 2) + ((yy - 0.15) ** 2)) / 0.12)[..., None]
    img = img + glow * 22
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


def leaf_points(base, angle, length, width, n=80):
    """两头尖、中间饱满的叶子轮廓：从 base 出发沿 angle（度，0 = 向右，逆时针）长 length。"""
    a = math.radians(angle)
    ux, uy = math.cos(a), -math.sin(a)
    vx, vy = -uy, ux
    side1, side2 = [], []
    for i in range(n + 1):
        t = i / n
        half = width / 2 * math.sin(math.pi * t) ** 0.75 * (1 - 0.25 * t)
        cx, cy = base[0] + ux * length * t, base[1] + uy * length * t
        side1.append((cx + vx * half, cy + vy * half))
        side2.append((cx - vx * half, cy - vy * half))
    return side1 + side2[::-1]


def stem_points(p0, p1, p2, n=60):
    """二次贝塞尔曲线上的点（嫩芽的茎）。"""
    pts = []
    for i in range(n + 1):
        t = i / n
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        pts.append((x, y))
    return pts


def sprout_layer(size):
    """白色嫩芽（单独一层，方便加阴影）。"""
    layer = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(layer)
    k, g = size / 1024, 1.18   # g：整株放大倍数；下面的坐标按 1024 画布写，整体往上挪到视觉居中

    def P(x, y):
        return ((x - 512) * g + 512) * k, ((y - 610) * g + 525) * k

    stem = stem_points(P(512, 790), P(500, 640), P(520, 500))
    r = 32 * g * k
    for x, y in stem:
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    d.polygon(leaf_points(P(528, 520), 38, 330 * g * k, 175 * g * k), fill=255)    # 右边大叶
    d.polygon(leaf_points(P(506, 585), 150, 250 * g * k, 135 * g * k), fill=255)   # 左边小叶
    return layer


def make(size=S):
    mask = squircle_mask(size)
    bg = gradient(size, (112, 222, 140), (22, 160, 84))
    sprout = sprout_layer(size)
    # 嫩芽下方一层很淡的投影，显得有立体感
    shadow = Image.new("L", (size, size), 0)
    sh = sprout.filter(ImageFilter.GaussianBlur(size * 0.02)).point(lambda v: int(v * 0.22))
    shadow.paste(sh, (0, int(size * 0.012)))
    img = bg.copy()
    img.paste((10, 90, 45), mask=shadow)
    img.paste((255, 255, 255), mask=sprout)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, mask=mask)
    return out


def main():
    OUT.mkdir(exist_ok=True)
    big = make(S)
    # 外面留一点透明边，任务栏 / 桌面上和系统图标大小更协调
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    inner = big.resize((int(S * 0.9), int(S * 0.9)), Image.LANCZOS)
    canvas.paste(inner, ((S - inner.width) // 2, (S - inner.height) // 2), inner)
    canvas.resize((256, 256), Image.LANCZOS).save(OUT / "farm.png")
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    canvas.resize((256, 256), Image.LANCZOS).save(OUT / "sprout.ico", sizes=[(s, s) for s in sizes])
    print("已生成", OUT / "farm.png", OUT / "sprout.ico")


if __name__ == "__main__":
    main()
