"""生成程序图标 assets/farm.ico（多尺寸）和 assets/farm.png。改了样式重新运行一次即可：
    python tools/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets"
S = 1024   # 先画大图再缩小，边缘更平滑


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def leaf_points(base, angle, length, width, n=40):
    """两头尖的叶子轮廓：从 base 出发，沿 angle 方向长 length，最宽 width。"""
    import math
    a = math.radians(angle)
    ux, uy = math.cos(a), math.sin(a)          # 沿叶子方向
    vx, vy = -uy, ux                            # 垂直方向
    top, bot = [], []
    for i in range(n + 1):
        t = i / n
        half = width / 2 * math.sin(math.pi * t) ** 0.8
        cx, cy = base[0] + ux * length * t, base[1] + uy * length * t
        top.append((cx + vx * half, cy + vy * half))
        bot.append((cx - vx * half, cy - vy * half))
    return top + bot[::-1]


def leaf(d, base, angle, length, width, fill, outline, vein):
    import math
    d.polygon(leaf_points(base, angle, length + 40, width + 50), fill=outline)
    d.polygon(leaf_points(base, angle, length, width), fill=fill)
    a = math.radians(angle)
    d.line([base, (base[0] + math.cos(a) * length * 0.82, base[1] + math.sin(a) * length * 0.82)],
           fill=vein, width=18)


def draw_icon():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 1. 圆角方块底，竖向渐变绿
    grad = Image.new("RGB", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bottom = (126, 206, 96), (52, 140, 70)
    for y in range(S):
        gd.line([(0, y), (S, y)], fill=lerp(top, bottom, y / S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=230, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # 2. 右上角小太阳
    d.ellipse((680, 150, 840, 310), fill=(255, 214, 92))

    # 3. 底部土堆（棕色），只保留圆角方块里面的部分
    soil = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(soil)
    sd.ellipse((60, 690, 964, 1180), fill=(139, 90, 50))
    sd.ellipse((180, 720, 844, 900), fill=(163, 110, 62))
    soil_mask = Image.new("L", (S, S), 0)
    soil_mask.paste(mask, (0, 0), soil.split()[3])
    img.paste(soil, (0, 0), soil_mask)

    # 4. 嫩芽：奶白色弯茎 + 两片尖叶（带深绿描边，小尺寸也看得清）
    leaf_fill, outline, vein = (255, 248, 222), (36, 96, 48), (126, 206, 96)
    stem = [(512, 780), (506, 690), (500, 600), (504, 520)]
    d.line(stem, fill=outline, width=80, joint="curve")
    d.line(stem, fill=leaf_fill, width=46, joint="curve")
    base = (504, 530)
    leaf(d, base, angle=-148, length=340, width=150, fill=leaf_fill, outline=outline, vein=vein)  # 左叶
    leaf(d, base, angle=-38, length=400, width=170, fill=leaf_fill, outline=outline, vein=vein)   # 右叶

    # 5. 轻微阴影让图标在浅色任务栏上更立体
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((50, 64, S - 30, S - 16), radius=230, fill=(0, 0, 0, 70))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    return Image.alpha_composite(shadow, img)


def main():
    OUT.mkdir(exist_ok=True)
    icon = draw_icon()
    icon.resize((256, 256), Image.LANCZOS).save(OUT / "farm.png")
    icon.save(OUT / "farm.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
                                     (48, 48), (64, 64), (128, 128), (256, 256)])
    print("已生成", OUT / "farm.ico", OUT / "farm.png")


if __name__ == "__main__":
    main()
