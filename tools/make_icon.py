# -*- coding: utf-8 -*-
"""生成 DiskGuard 应用图标, 输出 assets/DiskGuard.ico 与尺寸预览图。

设计(全几何绘制, 不外借素材):
  深石墨色圆角方块底  ->  机加工齿轮环(机械感, 作为背景层次)
  ->  金属渐变字母 "D"  ->  主色蓝刻度弧(呼应程序品牌色, 兼作"计量"语义)
所有元素在 4 倍超采样画布上绘制, 最后按目标尺寸 LANCZOS 降采样,
保证 16x16 下仍能看清 "D" 的轮廓(小尺寸只保留底/字两块高对比信息)。

用法: python make_icon.py
产物: assets/DiskGuard.ico, assets/icon_256.png, assets/icon_preview.png
"""
import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "assets")

SS = 4                      # 超采样倍数
BASE = 512                  # 逻辑画布边长(几何尺寸都按这个坐标系写)
W = BASE * SS               # 实际像素画布边长

# ---- 配色 ----
TILE_TOP = (48, 57, 73)
TILE_MID = (32, 39, 51)
TILE_BOT = (18, 22, 30)
TILE_RIM = (128, 142, 165)
GEAR_HI = (86, 98, 118)
GEAR_LO = (34, 40, 52)
METAL_HI = (255, 255, 255)
METAL_MID = (203, 214, 230)
METAL_LO = (137, 150, 171)
D_EDGE = (39, 46, 58)
SHADOW = (6, 8, 12)
ACCENT_HI = (96, 165, 250)
ACCENT_LO = (37, 99, 235)

# ---- 几何(逻辑坐标, 单位 = BASE 坐标系的像素) ----
TILE_INSET = 7              # 方块距画布边缘
TILE_RADIUS = 114           # 圆角半径(约 22%, Windows 大图标观感)
GEAR_C = BASE / 2.0
GEAR_R_OUT = 198            # 齿顶圆
GEAR_R_ROOT = 176           # 齿根圆
GEAR_R_IN = 156             # 内孔(环的内径)
GEAR_TEETH = 12
GEAR_TOOTH_SPAN = 0.5       # 单齿占整个齿距的比例
ARC_R = 166                 # 刻度弧所在半径(落在环体上)
ARC_W = 13
ARC_FROM, ARC_TO = -52, 30  # 刻度弧角度(0=3点钟方向, 顺时针为正)
D_CAP = 292                 # 字母 D 的字面高度
D_CAP_SIMPLE = 344          # 小尺寸版: 字放大, 抵掉降采样导致的笔画变细
D_EDGE_W = 7                # 字母深色描边宽度
SHADOW_DY = 7               # 字母投影偏移
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
SIMPLE_BELOW = 48           # 小于该尺寸改用简化版底图


def s(v):
    """逻辑坐标 -> 像素。"""
    return v * SS


def _ramp(stops, t):
    """按 [(位置, (r,g,b)), ...] 在 t∈[0,1] 上做分段线性插值。"""
    out = np.zeros(t.shape + (3,), np.float32)
    stops = sorted(stops)
    for i in range(len(stops) - 1):
        t0, c0 = stops[i]
        t1, c1 = stops[i + 1]
        if t1 <= t0:
            continue
        seg = (t >= t0) & (t <= t1)
        k = np.clip((t - t0) / (t1 - t0), 0.0, 1.0)
        for ch in range(3):
            out[:, :, ch] = np.where(seg, c0[ch] * (1.0 - k) + c1[ch] * k,
                                     out[:, :, ch])
    out[t < stops[0][0]] = stops[0][1]
    out[t > stops[-1][0]] = stops[-1][1]
    return out


def _grid():
    yy, xx = np.mgrid[0:W, 0:W].astype(np.float32)
    return xx / (W - 1), yy / (W - 1)


def vgrad(stops):
    """纵向渐变(覆盖整块画布)。"""
    _, ty = _grid()
    return Image.fromarray(_ramp(stops, ty).astype(np.uint8), "RGB")


def dgrad(stops, xw=0.42):
    """斜向渐变: 让金属面有受光方向感。"""
    tx, ty = _grid()
    return Image.fromarray(
        _ramp(stops, np.clip(tx * xw + ty * (1.0 - xw), 0, 1)).astype(np.uint8), "RGB")


def blank():
    return Image.new("L", (W, W), 0)


def as_layer(mask, rgb, alpha=255):
    """纯色层: 用 L 掩膜当透明度。"""
    if alpha != 255:
        mask = mask.point(lambda v: int(v * alpha / 255.0))
    lay = Image.new("RGBA", (W, W), tuple(rgb) + (0,))
    lay.putalpha(mask)
    return lay


def grad_layer(mask, rgb_img, alpha=255):
    """渐变层: 用 L 掩膜当透明度。"""
    if alpha != 255:
        mask = mask.point(lambda v: int(v * alpha / 255.0))
    lay = rgb_img.convert("RGBA")
    lay.putalpha(mask)
    return lay


def gear_mask():
    """齿轮环掩膜(多边形齿 + 挖内孔)。"""
    m = blank()
    d = ImageDraw.Draw(m)
    step = 2.0 * math.pi / GEAR_TEETH
    half = step * GEAR_TOOTH_SPAN / 2.0
    pts = []
    for i in range(GEAR_TEETH):
        a0 = i * step - math.pi / 2.0
        for r, a in ((GEAR_R_ROOT, a0 - half), (GEAR_R_OUT, a0 - half),
                     (GEAR_R_OUT, a0 + half), (GEAR_R_ROOT, a0 + half)):
            pts.append((s(GEAR_C + r * math.cos(a)), s(GEAR_C + r * math.sin(a))))
    d.polygon(pts, fill=255)
    d.ellipse([s(GEAR_C - GEAR_R_IN), s(GEAR_C - GEAR_R_IN),
               s(GEAR_C + GEAR_R_IN), s(GEAR_C + GEAR_R_IN)], fill=0)
    return m


def hex_bolt_mask(cx, cy, r, rot=0.0):
    """内六角螺栓头(角落机械细节)。"""
    m = blank()
    d = ImageDraw.Draw(m)
    pts = [(s(cx + r * math.cos(rot + i * math.pi / 3.0)),
            s(cy + r * math.sin(rot + i * math.pi / 3.0))) for i in range(6)]
    d.polygon(pts, fill=255)
    d.ellipse([s(cx - r * 0.42), s(cy - r * 0.42),
               s(cx + r * 0.42), s(cy + r * 0.42)], fill=0)
    return m


def load_font(size_px):
    for name in ("segoeuib.ttf", "arialbd.ttf", "seguisb.ttf"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size_px)
    raise SystemExit("找不到可用的粗体字库(segoeuib/arialbd)")


def letter_mask(stroke_w=0, cap=None):
    """字母 D 的掩膜, 按字面高度 cap 精确缩放并居中。"""
    cap = cap or D_CAP
    probe = load_font(200)
    b = probe.getbbox("D")
    ch = b[3] - b[1]
    size = int(round(200 * s(cap) / ch))
    font = load_font(size)
    b = font.getbbox("D")
    gw, gh = b[2] - b[0], b[3] - b[1]
    m = blank()
    d = ImageDraw.Draw(m)
    # 用 bbox 反推落笔点, 让字形视觉中心落在齿轮圆心
    x = s(GEAR_C) - gw / 2.0 - b[0]
    y = s(GEAR_C) - gh / 2.0 - b[1]
    d.text((x, y), "D", font=font, fill=255,
           stroke_width=stroke_w, stroke_fill=255)
    return m


def build_master(simple=False):
    """绘制 4 倍超采样主图(RGBA)。

    simple=True 用在小尺寸(<=32px): 齿轮与螺栓降采样后会糊成噪点, 反而干扰识别,
    此时只保留"底 + 更大的 D + 蓝色刻度弧"三块高对比信息, 保证 16px 仍清晰。
    """
    cap = D_CAP_SIMPLE if simple else D_CAP
    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))

    # 1) 圆角方块底: 斜向石墨渐变 + 顶部受光
    tile = blank()
    ImageDraw.Draw(tile).rounded_rectangle(
        [s(TILE_INSET), s(TILE_INSET), s(BASE - TILE_INSET), s(BASE - TILE_INSET)],
        radius=s(TILE_RADIUS), fill=255)
    bg = dgrad([(0.0, TILE_TOP), (0.42, TILE_MID), (1.0, TILE_BOT)])
    arr = np.asarray(bg, np.float32)
    _, ty = _grid()
    arr += (np.clip(1.0 - ty / 0.42, 0.0, 1.0) ** 2)[:, :, None] * 20.0   # 顶部微高光
    bg = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    canvas = Image.alpha_composite(canvas, grad_layer(tile, bg))

    # 2) 方块内沿描边: 让图标在浅色与深色背景上都能与背景分离
    rim_w = 2.6 if simple else 2.2
    rim = blank()
    ImageDraw.Draw(rim).rounded_rectangle(
        [s(TILE_INSET + 1.5), s(TILE_INSET + 1.5),
         s(BASE - TILE_INSET - 1.5), s(BASE - TILE_INSET - 1.5)],
        radius=s(TILE_RADIUS - 1.5), outline=255, width=int(s(rim_w)))
    canvas = Image.alpha_composite(canvas, as_layer(rim, TILE_RIM, 150 if simple else 132))

    if not simple:
        # 3) 角落内六角螺栓(机械风格细节; 小尺寸下会变成噪点, 故 simple 版不画)
        for bx, by, rot in ((72, 72, 0.0), (440, 72, 0.5),
                            (72, 440, 0.2), (440, 440, 0.7)):
            bm = hex_bolt_mask(bx, by, 17, rot)
            canvas = Image.alpha_composite(canvas, as_layer(bm, (16, 20, 27), 150))
            bm2 = hex_bolt_mask(bx - 1.2, by - 1.2, 17, rot)
            canvas = Image.alpha_composite(canvas, as_layer(bm2, (118, 132, 154), 70))

        # 4) 齿轮环: 金属感纵向渐变
        canvas = Image.alpha_composite(
            canvas, grad_layer(gear_mask(), vgrad([(0.16, GEAR_HI), (0.9, GEAR_LO)])))

    # 5) 主色刻度弧: 压在环体上, 兼作"计量/监控"语义
    arc_r = ARC_R + (26 if simple else 0)
    arc_w = ARC_W + (16 if simple else 0)
    arc = blank()
    ImageDraw.Draw(arc).arc(
        [s(GEAR_C - arc_r), s(GEAR_C - arc_r),
         s(GEAR_C + arc_r), s(GEAR_C + arc_r)],
        start=ARC_FROM, end=ARC_TO, fill=255, width=int(s(arc_w)))
    canvas = Image.alpha_composite(
        canvas, grad_layer(arc, vgrad([(0.14, ACCENT_HI), (0.86, ACCENT_LO)])))

    # 6) 字母 D: 投影 -> 深色描边 -> 金属渐变面
    glyph = letter_mask(cap=cap)
    glyph_edge = letter_mask(stroke_w=int(s(D_EDGE_W)), cap=cap)
    blur = 4 if simple else 5
    shadow = glyph.filter(ImageFilter.GaussianBlur(s(blur))).point(
        lambda v: int(v * (0.42 if simple else 0.55)))
    shadow = shadow.transform((W, W), Image.AFFINE, (1, 0, 0, 0, 1, -s(SHADOW_DY)),
                              resample=Image.BILINEAR)
    canvas = Image.alpha_composite(canvas, as_layer(shadow, SHADOW, 160))
    canvas = Image.alpha_composite(canvas, as_layer(glyph_edge, D_EDGE, 235))
    top = (GEAR_C - cap / 2.0) / BASE
    canvas = Image.alpha_composite(
        canvas, grad_layer(glyph, vgrad([(top * 0.72, METAL_HI),
                                         (top + 0.30, METAL_MID),
                                         (top + 0.62, METAL_LO)])))
    return canvas


def down(size, master):
    return master.resize((size, size), Image.Resampling.LANCZOS)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    master = build_master()
    master_simple = build_master(simple=True)
    master.save(os.path.join(OUT_DIR, "icon_master.png"))

    frames = {n: down(n, master_simple if n < SIMPLE_BELOW else master)
              for n in ICON_SIZES}
    # ICO 用 BMP/DIB 帧(而非 PNG 压缩帧): PyInstaller 内嵌进 exe 资源时最稳
    ico = os.path.join(OUT_DIR, "DiskGuard.ico")
    frames[256].save(ico, format="ICO", bitmap_format="bmp",
                     sizes=[(n, n) for n in ICON_SIZES],
                     append_images=[frames[n] for n in ICON_SIZES if n != 256])
    frames[256].save(os.path.join(OUT_DIR, "icon_256.png"))

    make_preview(frames)
    verify_ico(ico, ICON_SIZES)
    print("OK ->", ico, os.path.getsize(ico), "bytes;", len(ICON_SIZES), "frames")


def make_preview(frames, extra=None):
    """尺寸/明暗对照预览图: 浅底 + 深底 + 模拟任务栏。"""
    slot = 256
    pw, band, tb = 1200, 400, 92
    ph = band * 2 + tb
    img = Image.new("RGB", (pw, ph), (243, 245, 249))
    d = ImageDraw.Draw(img)
    d.rectangle([0, band, pw, band * 2], fill=(16, 19, 26))
    font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeui.ttf")
    font = ImageFont.truetype(font_path, 16)
    small = ImageFont.truetype(font_path, 12)

    for band_i, fg in ((0, (74, 84, 100)), (1, (152, 164, 182))):
        y0 = band_i * band
        d.text((48, y0 + 24), "light background" if band_i == 0
               else "dark background", font=font, fill=fg)
        x = 48
        for n in (256, 128, 64, 48, 32, 16):
            im = frames[n]
            top = y0 + 66 + (slot - n)          # 底对齐, 便于横向比较轮廓
            img.paste(im, (x, top), im)
            d.text((x, y0 + 66 + slot + 14), f"{n}px", font=small, fill=fg)
            x += n + 46

    # 模拟任务栏(深色): 看真实使用环境下的观感
    tb_y = band * 2
    d.rectangle([0, tb_y, pw, ph], fill=(31, 36, 48))
    img.paste(frames[32], (52, tb_y + 20), frames[32])
    d.text((98, tb_y + 30), "DiskGuard", font=font, fill=(232, 236, 244))
    img.paste(frames[16], (232, tb_y + 28), frames[16])
    d.text((258, tb_y + 30), "16px (通知区/标题栏观感)",
           font=small, fill=(150, 162, 180))
    img.save(os.path.join(OUT_DIR, "icon_preview.png"))
    if extra:
        extra.show(frames)


def verify_ico(path, sizes):
    """回读 .ico 验证帧数/尺寸, 并导出 16-64px 供肉眼检查。"""
    with Image.open(path) as ico:
        got = sorted(ico.ico.sizes())
        assert len(got) == len(sizes), (got, sizes)
        for n in sizes:
            assert (n, n) in got, (n, got)
            ico.size = (n, n)
            ico.load()
    print("ICO frames OK:", got)


if __name__ == "__main__":
    main()
