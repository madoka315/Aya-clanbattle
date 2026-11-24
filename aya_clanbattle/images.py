"""图像渲染层：状态大图 / 业绩表文字转图 / 成员头像缓存。

移植自 yobot_remix image_engine.py + realize.challenger_info，视觉效果对齐：
- BackGroundGenerator 延迟画布（免手算宽高）；
- chips 二分装箱排版；
- boss 卡片（头像圆角/周目徽章/双色血条自适应文字）；
- 组合拼图（投影+圆角）。
字体解析顺序：config font_path → 包内 fonts/msyh.ttf → Windows 系统字体 → PIL 默认（降级）。
"""
import asyncio
import base64
import os
import re
import sys
from io import BytesIO
from typing import Callable, Dict, List, Optional, Tuple, Union

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .gameconfig import GameConfig
from .state import BOSS_NUMS, GuildState

PKG_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_CANDIDATES = [
    os.path.join(PKG_DIR, "fonts", "msyh.ttf"),
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
]
_avatar_dir: str = os.path.join(os.path.dirname(PKG_DIR), "data", "user_profile")
_font_path: Optional[str] = None
_missing_avatars: set = set()


def configure(font_path: Optional[str] = None, avatar_dir: Optional[str] = None) -> None:
    global _font_path, _avatar_dir
    if font_path:
        _font_path = font_path
    if avatar_dir:
        _avatar_dir = avatar_dir
    os.makedirs(_avatar_dir, exist_ok=True)


def _resolve_font() -> Optional[str]:
    global _font_path
    if _font_path:
        return _font_path
    for cand in FONT_CANDIDATES:
        if os.path.exists(cand):
            _font_path = cand
            return cand
    return None


def load_font(size: int) -> ImageFont.FreeTypeFont:
    path = _resolve_font()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


# ================================================================= 基础绘制
class BackGroundGenerator:
    """延迟画布：只记录操作与占用尺寸，generate() 时统一生成（生成后缓存图销毁）。"""

    def __init__(self, color=(255, 255, 255), padding=(0, 0, 0, 0), override_size=None):
        self._composite_ops: List[tuple] = []
        self._paste_ops: List[tuple] = []
        self._used_w = override_size[0] if override_size and override_size[0] else 0
        self._used_h = override_size[1] if override_size and override_size[1] else 0
        self.color = color
        self.padding = padding
        self.override_size = override_size

    def alpha_composite(self, im, dest, *args, **kw) -> None:
        self._composite_ops.append((im, dest, args, kw))
        self._used_w = max(dest[0] + im.width, self._used_w)
        self._used_h = max(dest[1] + im.height, self._used_h)

    def paste(self, im, box, mask=None, *args, **kw) -> None:
        self._paste_ops.append((im, box, mask, args, kw))
        self._used_w = max(box[0] + im.width, self._used_w)
        self._used_h = max(box[1] + im.height, self._used_h)

    def center(self, im) -> Tuple[int, int]:
        return round((self.use_width - im.width) / 2), round((self._used_h - im.height) / 2)

    def generate(self) -> Image.Image:
        result = Image.new("RGBA", self.size, self.color)
        for im, dest, args, kw in self._composite_ops:
            result.alpha_composite(im, (dest[0] + self.padding[0], dest[1] + self.padding[1]),
                                   *args, **kw)
            im.close()
        for im, box, mask, args, kw in self._paste_ops:
            result.paste(im, (box[0] + self.padding[0], box[1] + self.padding[1]), mask, *args, **kw)
            im.close()
        return result

    @property
    def size(self) -> Tuple[int, int]:
        w = self._used_w + self.padding[0] + self.padding[2]
        h = self._used_h + self.padding[1] + self.padding[3]
        if self.override_size:
            if self.override_size[0] is not None:
                w = self.override_size[0] + self.padding[0] + self.padding[2]
            if self.override_size[1] is not None:
                h = self.override_size[1] + self.padding[1] + self.padding[3]
        return w, h

    @property
    def use_width(self) -> int:
        return self._used_w

    @property
    def use_height(self) -> int:
        return self._used_h

    @property
    def width(self) -> int:
        return self.size[0]

    @property
    def height(self) -> int:
        return self.size[1]


def get_font_image(text: str, size: int, color=(0, 0, 0)) -> Image.Image:
    if "\n" in text:
        return get_font_image_vertical(text, size, color)
    font = load_font(size)
    box = font.getbbox(text=text)
    bg = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (255, 255, 255, 0))
    ImageDraw.Draw(bg).text((-box[0], -box[1]), text=text, font=font, fill=color)
    return bg


def get_font_image_vertical(text: str, size: int, color=(0, 0, 0)) -> Image.Image:
    gap = round(size / 3)
    bg = BackGroundGenerator(color=(255, 255, 255, 0))
    y = 0
    for line in text.split("\n"):
        bg.alpha_composite(get_font_image(line, size, color), (0, y))
        y = bg.use_height + gap
    return bg.generate()


def round_corner(image: Image.Image, radius: Optional[int] = None) -> Image.Image:
    size = (radius if radius is not None else image.height // 2) * 2 or 2
    circle = Image.new("L", (size * 5, size * 5), 0)
    ImageDraw.Draw(circle).ellipse((0, 0, size * 5, size * 5), 255)
    circle = circle.resize((size, size))
    r = size // 2
    mask = Image.new("L", image.size, 255)
    if radius is None:
        half = circle.size[0] // 2
        mask.paste(circle.crop((0, 0, half, circle.size[1])), (0, 0))
        mask.paste(circle.crop((half, 0, circle.size[0], circle.size[1])),
                   (image.width - (circle.size[0] - half), 0))
    else:
        mask.paste(circle.crop((0, 0, r, r)), (0, 0))
        mask.paste(circle.crop((r, 0, 2 * r, r)), (image.width - r, 0))
        mask.paste(circle.crop((0, r, r, 2 * r)), (0, image.height - r))
        mask.paste(circle.crop((r, r, 2 * r, 2 * r)), (image.width - r, image.height - r))
    bg = Image.new("RGBA", image.size, (255, 255, 255, 0))
    result = Image.composite(image, bg, mask)
    circle.close(); mask.close(); bg.close(); image.close()
    return result


def _center_in(source: Image.Image, target: Image.Image) -> Tuple[int, int]:
    box = target.getbbox()
    if box is None:
        return (0, 0)
    return (round((source.width - (box[2] - box[0])) / 2),
            round((source.height - (box[3] - box[1])) / 2))


# ================================================================= chips 排版
def _smaller_search(array: List[int], key, seek_map=None) -> Optional[int]:
    """降序数组中找小于 key 的最大值下标（二分）。"""
    if not array or array[-1] > key:
        return None
    n = len(array)
    seek_map = seek_map or list(range(n))
    if n == 1:
        return seek_map[0]
    if n == 2:
        return seek_map[0] if array[0] < key else seek_map[1]
    half = n // 2
    if array[half] < key:
        return _smaller_search(array[:half + 1], key, seek_map[:half + 1])
    return _smaller_search(array[half + 1:], key, seek_map[half + 1:])


def chips_list_sort(widths: List[int], target_num: int, interval: int) -> List[List[int]]:
    """长短板匹配贪心换行，返回 [行][chip 下标]。"""
    result: List[List[int]] = []
    addr = list(range(len(widths)))
    src = list(widths)
    while src:
        result.append([addr[0]])
        cur = src[0] + interval
        src.pop(0); addr.pop(0)
        if not src:
            break
        if cur + src[-1] > target_num:
            continue
        while cur < target_num and src:
            seek = _smaller_search(src, target_num - cur)
            if seek is None:
                break
            result[-1].append(addr[seek])
            cur += src[seek] + interval
            src.pop(seek); addr.pop(seek)
            if not src or cur + src[-1] > target_num:
                break
    return result


CHIP_LIST_WIDTH = 400 - 10 - 29
CHIP_NAME_MAX = 6  # chips 内名字最大长度（yobot 原为 4）


def _text_color_on(bg_rgb) -> Tuple[int, int, int]:
    r, g, b = bg_rgb[:3]
    return (255, 255, 255) if (r * 0.299 + g * 0.587 + b * 0.114) / 255 < 0.5 else (0, 0, 0)


def user_chips(head_icon: Optional[Image.Image], user_name: str,
               bg_color=(189, 189, 189)) -> Image.Image:
    size = 20
    if head_icon is not None:
        icon = head_icon.resize((size, size))
        icon = round_corner(icon)
    else:
        icon = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    color = _text_color_on(bg_color)
    name_img = get_font_image(user_name, 20, color)
    if name_img.width > CHIP_LIST_WIDTH - 35:
        name_img = name_img.crop((0, 0, CHIP_LIST_WIDTH - 35, 20))
    bg = BackGroundGenerator(color=bg_color, padding=(5, 5, 5, 5), override_size=(None, 20))
    bg.alpha_composite(icon, (0, 0))
    if name_img.getbbox() is not None:
        bg.alpha_composite(name_img, (25, bg.center(name_img)[1]))
    return round_corner(bg.generate(), 5)


def chips_list(chips: Dict[str, object], text: str = "内容",
               bg_color=(255, 255, 255)) -> Image.Image:
    bg_color = chips.pop("style-background-color", bg_color)
    chips_color = chips.pop("style-chips-color", (189, 189, 189))
    color = _text_color_on(bg_color)
    text_img = get_font_image("\n".join(list(text)), 24, color)
    if not chips:
        bg = BackGroundGenerator(color=bg_color, override_size=(390, 65), padding=(5, 5, 5, 5))
        bg.alpha_composite(text_img, (0, bg.center(text_img)[1]))
        empty = get_font_image(f"暂无{text}", 24, color)
        bg.alpha_composite(empty, bg.center(empty))
        return round_corner(bg.generate(), 5)
    chip_imgs = []
    for uid, nick in chips.items():
        if not isinstance(uid, str) or not uid.isdigit() or not isinstance(nick, str):
            continue
        path = os.path.join(_avatar_dir, f"{uid}.jpg")
        icon = Image.open(path, "r") if os.path.isfile(path) else None
        if icon is None:
            _missing_avatars.add(int(uid))
        chip_imgs.append(user_chips(icon, nick, chips_color))
    chip_imgs.sort(key=lambda im: im.width, reverse=True)
    layout = chips_list_sort([im.width for im in chip_imgs], CHIP_LIST_WIDTH, 5)
    inner = BackGroundGenerator(color=bg_color)
    y = x = 0
    for line in layout:
        for seek in line:
            inner.alpha_composite(chip_imgs[seek], (x, y))
            x += chip_imgs[seek].width + 5
        y += 30 + 5
        x = 0
    bg = BackGroundGenerator(color=bg_color, padding=(5, 5, 5, 5), override_size=(390, None))
    bg.alpha_composite(Image.new("RGBA", (1, 65), (255, 255, 255, 0)), (0, 0))
    bg.alpha_composite(inner.generate(), (29, 0))
    bg.alpha_composite(text_img, (0, bg.center(text_img)[1]))
    return round_corner(bg.generate(), 5)


# ================================================================= 『状态』大图（web 面板 boss 卡同款渲染）
def _fmt_hp(n: float) -> str:
    if n >= 1e8:
        v = n / 1e8
        return f"{v:.2f}亿" if v % 1 else f"{int(v)}亿"
    return f"{n:,.0f}"


def _tape_rgba(w, h, angle=2.0):
    """双色斜纹和纸胶带（web --tape：樱粉×蜜桃 45° 条纹、radius 2px、opacity .72、
    rotate(-2deg)=Pillow 逆时针 +2°）。w/h 为超采样像素。"""
    w, h = int(w), int(h)
    size = (w + 8, h + 8)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((4, 4, w + 4, h + 4),
                                           radius=max(2, round(h / 7)), fill=255)
    layer = Image.new("RGBA", size, (246, 198, 212, 255))
    dl = ImageDraw.Draw(layer)
    st = round(h * 0.71)  # 45° 斜纹横向半周期 ≈ 7px×√2（web 条纹垂直宽 7px）
    for sx in range(0, w + h + 8, st * 2):
        dl.polygon([(sx, h + 8), (sx + st, h + 8), (sx + st + h + 8, 0), (sx + h + 8, 0)],
                   fill=(255, 224, 207, 255))
    tape = Image.new("RGBA", size, (0, 0, 0, 0))
    tape.paste(layer, (0, 0), mask)
    tape.putalpha(tape.getchannel("A").point(lambda v: v * 184 // 255))  # opacity .72
    return tape.rotate(angle, expand=True, resample=Image.BICUBIC)


def _rr_pts(box, radii):
    """圆角矩形路径点列（四角半径 tl/tr/br/bl 可不同），相邻重复点已去除。"""
    import math
    x0, y0, x1, y1 = box
    ws, hs = x1 - x0, y1 - y0
    rtl, rtr, rbr, rbl = (max(0, min(r, ws / 2, hs / 2)) for r in radii)
    pts = []

    def arc(cx, cy, r, a0, a1):
        n = max(2, int(abs(a1 - a0) / (math.pi / 10)))
        for k in range(n + 1):
            a = a0 + (a1 - a0) * k / n
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))

    pts.append((x0 + rtl, y0))
    pts.append((x1 - rtr, y0))
    arc(x1 - rtr, y0 + rtr, rtr, -math.pi / 2, 0)
    pts.append((x1, y1 - rbr))
    arc(x1 - rbr, y1 - rbr, rbr, 0, math.pi / 2)
    pts.append((x0 + rbl, y1))
    arc(x0 + rbl, y1 - rbl, rbl, math.pi / 2, math.pi)
    pts.append((x0, y0 + rtl))
    arc(x0 + rtl, y0 + rtl, rtl, math.pi, 3 * math.pi / 2)
    out = [pts[0]]
    for p in pts[1:]:
        if p != out[-1]:
            out.append(p)
    return out


def _sticker_rect(draw, box, radii, fill=None, outline=None, width=1):
    """贴纸圆角矩形（web 手帖签名形：左上小圆角 3/4px、其余 10~12px）。"""
    pts = _rr_pts(box, radii)
    if fill is not None:
        draw.polygon(pts, fill=fill)
    if outline is not None:
        draw.line(pts + [pts[0]], fill=outline, width=width, joint="curve")


def _dashed_rounded_rect(draw, box, radius, color, dash=5, gap=4, width=1):
    """沿圆角矩形路径画虚线（血条轮廓/虚线描边）；radius 可为 int 或 (tl,tr,br,bl)。"""
    import math
    pts = _rr_pts(box, (radius,) * 4 if isinstance(radius, int) else tuple(radius))
    total = 0
    segs = []
    for p0, p1 in zip(pts, pts[1:]):
        d = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
        segs.append((p0, p1, d))
        total += d
    period = dash + gap
    for p0, p1, d in segs:
        if d <= 0:
            continue  # 弧线接点的零长段（pos/d 会产生 NaN 令 Pillow 死循环）
        acc, drawing, pos = 0, True, 0  # 每段独立起 pattern（相位断差肉眼不可见）
        while pos < d:
            if drawing:
                e = min(pos + (period - acc if period - acc else dash), d)
                draw.line((p0[0] + (p1[0] - p0[0]) * pos / d, p0[1] + (p1[1] - p0[1]) * pos / d,
                           p0[0] + (p1[0] - p0[0]) * e / d, p0[1] + (p1[1] - p0[1]) * e / d),
                          fill=color, width=width)
                acc += e - pos
                pos = e
                if acc >= period:
                    acc, drawing = 0, False
            else:
                skip = period - acc
                if pos + skip >= d:
                    break
                pos += skip
                acc, drawing = 0, True


def _card_ctx(u):
    """boss 卡渲染上下文：尺寸函数+字体+测量 scratch（状态图/单卡图共用）。
    字号整体大一档+卡片收窄（280）：QQ 缩略图下文字/整图占比更高、更可读。"""
    return {"u": u, "scratch": ImageDraw.Draw(Image.new("RGB", (8, 8))),
            "card_w": u(280), "pad": u(12), "inner": u(280) - u(24),
            "f_name": load_font_bold(u(16)), "f_num_sb": load_font_num_bold(u(16)),
            "f_num_b": load_font_num_bold(u(22)), "f_num": load_font_num(u(12)),
            "f_cjk": load_font(u(11.5)), "f_small": load_font(u(12.5)),
            "f_chip": load_font(u(12.5)), "f_chip_bold": load_font_bold(u(12.5)),
            "f_tiny": load_font(u(10.5)), "f_num_b_cjk": load_font_bold(u(22))}


def _boss_card_measure(b: dict, ctx: dict) -> dict:
    """boss 卡测量：pct/chips 分段与装箱/高度（状态图与单卡图共用）。"""
    u, scratch = ctx["u"], ctx["scratch"]
    inner, pad = ctx["inner"], ctx["pad"]
    f_chip, f_chip_bold = ctx["f_chip"], ctx["f_chip_bold"]
    killed = b["health"] == 0 and not b["is_next"]
    pct = 0 if killed or not b["full"] else max(0, min(100, round(b["health"] / b["full"] * 100)))
    chips = []
    for s in b["subs"]:
        # web subChip：<b>预约</b>名字:note，teal 底（QQ 侧加头像，名字前补空格分隔）
        segs = [("预约", _IMG_INK, f_chip_bold), (f" {s['name']}", _IMG_INK, f_chip)]
        if s["note"]:
            segs.append((f":{s['note']}", _IMG_INK, f_chip))
        chips.append((segs, _IMG_TEAL_TINT, _IMG_TEAL, s.get("qqid"), s["name"]))
    for c in b["challengers"]:
        # web chipOf：名字+青磁「补」+灰「xx代」+@伤害+留言；挂树=整 chip 换 warn 黄底
        # （去括号后名字与状态段之间补空格分隔）
        segs = [(_clip_rich(c["name"], CHIP_NAME_MAX), _IMG_INK, f_chip)]
        if c["comp"]:
            segs.append((" 补", _IMG_TEAL, f_chip))
        if c["behalf"]:
            segs.append((f" {_clip_rich(c['behalf'], CHIP_NAME_MAX)}代", _IMG_INK2, f_chip))
        if 0 < c["damage"] <= 11000:
            segs.append((f"@{c['seconds']}s,{c['damage']}w", _IMG_INK, f_chip))
        elif c["damage"] > 11000:
            segs.append((f"@{c['seconds']}s,{c['damage']:,}", _IMG_INK, f_chip))
        if c["comment"]:
            segs.append((f" ({c['comment']})", _IMG_INK, f_chip))
        if c["tree"]:
            segs.append(("(挂树)", _IMG_INK, f_chip))
        bg, bd = (_IMG_WARN_TINT, _IMG_WARN_C) if c["tree"] else (_IMG_ACCENT_TINT, _IMG_ACCENT)
        chips.append((segs, bg, bd, c["qqid"], _clip_rich(c["name"], CHIP_NAME_MAX)))
    rows_of_chips = []
    if chips:
        cur_x, cur_row = 0, []
        for segs, bg, bd, qqid, fb_name in chips:
            segs = _chip_fit(scratch, segs, inner - u(24) - u(25))
            tw = sum(_rich_textlength(scratch, t, f) for t, _c, f in segs)
            cw = int(u(12) * 2 + (u(18) + u(5) if qqid is not None else 0) + tw)
            if cur_x + cw > inner and cur_row:
                rows_of_chips.append(cur_row)
                cur_x, cur_row = 0, []
            cur_row.append((segs, bg, bd, qqid, cw, fb_name))
            cur_x += cw + u(6)
        if cur_row:
            rows_of_chips.append(cur_row)
    chips_h = (u(25) + u(5)) * len(rows_of_chips) - u(5) if rows_of_chips else u(19)
    # 纵向节奏（同 web：头 46 → hp 行 → 血条 → chips）
    h = pad + u(46) + u(22) + u(7) + u(14) + u(6) + chips_h + pad
    return {"num": b["num"], "name": b["name"], "icon": b.get("icon", "0"),
            "cycle": b["cycle"], "is_next": b["is_next"], "health": b["health"],
            "full": b["full"], "pct": pct, "killed": killed,
            "rows_of_chips": rows_of_chips, "chips_empty": not chips, "h": h}


def render_panel_status(title: str, date_str: str, summary: dict, bosses: List[dict]) -> bytes:
    """『状态』大图——web 面板同款：topbar+双色胶带条+竖向双列 boss 卡+补偿卡。"""
    S = 3
    K = 1.3

    def u(v):
        return int(v * S * K)

    margin, gap = u(12), u(11)
    card_w = u(280)
    pad = u(12)
    inner = card_w - pad * 2
    W = margin * 2 + card_w * 2 + gap
    top_h = u(52)
    ctx = _card_ctx(u)
    f_title = load_font_bold(u(17))
    scratch = ctx["scratch"]
    f_name, f_small, f_chip = ctx["f_name"], ctx["f_small"], ctx["f_chip"]

    # —— boss 卡内容布局（先测量后绘制）
    cards = [_boss_card_measure(b, ctx) for b in bosses]

    # —— 补偿卡（旧 remix 语义：独立卡片，列出持有补偿刀的成员与刀数）
    comps = summary.get("comps") or []
    comp_rows, cur_x, cur_row = [], 0, []
    for m in comps:
        text = _truncate(scratch, f"{m['name']} ×{m['count']}", f_chip, inner - u(24) - u(25))
        cw = int(u(12) * 2 + u(18) + u(5) + _rich_textlength(scratch, text, f_chip))
        if cur_x + cw > inner and cur_row:
            comp_rows.append(cur_row)
            cur_x, cur_row = 0, []
        cur_row.append((text, m["qqid"], cw))
        cur_x += cw + u(6)
    if cur_row:
        comp_rows.append(cur_row)
    comp_chips_h = (u(25) + u(5)) * len(comp_rows) - u(5) if comp_rows else u(19)
    comp_card = {"h": pad + u(22) + u(7) + comp_chips_h + pad,
                 "total": sum(m["count"] for m in comps), "rows": comp_rows}

    # —— 竖向双列（旧 remix 布局语义）：左列=补偿卡+boss1/2，右列=boss3/4/5，左列下沉错开
    grid_y = top_h + u(20)  # 彩条下缘(top_h+u(8)) + u(12)
    stagger = u(14)
    col_x = (margin, margin + card_w + gap)
    col_y = (grid_y + stagger, grid_y)
    bottom = grid_y
    for ci, items in enumerate(([comp_card] + cards[:2], cards[2:])):
        yy = col_y[ci]
        for c in items:
            c["_x"], c["_y"] = col_x[ci], yy
            yy += c["h"] + gap
        bottom = max(bottom, yy - gap)
    H = bottom + margin

    im = Image.new("RGB", (W, H), _IMG_PAPER)
    draw = ImageDraw.Draw(im)

    # —— topbar：群名 → 今日出刀 chip → 阶段 → 阶段提示（全部左对齐一行）
    draw.rectangle((0, 0, W, top_h), fill=_IMG_PANEL)
    f_badge = load_font_bold(u(14))
    f_chip_num = load_font_num_bold(u(12))
    f_chip_cjk = load_font_bold(u(12))
    cy = top_h // 2
    g = u(12)
    done = summary["blades"] >= summary["quota"]
    c_fg = _IMG_OK if done else _IMG_ACCENT_S
    c_bd = _IMG_OK if done else _IMG_ACCENT
    c_bg = _IMG_OK_TINT if done else _IMG_ACCENT_TINT
    parts = [("今日 ", f_chip_cjk), (f"{summary['blades']:g} / {summary['quota']:g} ", f_chip_num),
             ("刀", f_chip_cjk)]
    chip_w = _mixed_w(scratch, parts) + u(22)
    lv_txt = f"阶段 {summary['level']}"
    lv_w = u(20) + scratch.textlength(lv_txt, font=f_badge) + u(10)
    hint = summary["next_hint"]
    usable = W - margin * 2
    avail = usable - (chip_w + lv_w + g * 3)
    tw = scratch.textlength(title, font=f_title)
    if tw + scratch.textlength(hint, font=f_small) > avail:  # 溢出：先截提示，仍不够再截群名
        tmax = max(avail - scratch.textlength(hint, font=f_small), int(usable * 0.45))
        if tw > tmax:
            title = _truncate(scratch, title, f_title, tmax)
            tw = scratch.textlength(title, font=f_title)
        hmax = max(0, avail - tw)
        if scratch.textlength(hint, font=f_small) > hmax:
            hint = _truncate(scratch, hint, f_small, hmax) if hmax >= u(16) else ""
    x = margin
    _draw_rich(draw, (x, cy), title, f_title, _IMG_INK, "lm",
               stroke_width=u(1), stroke_fill=_IMG_PANEL)
    x += tw + g
    box = (x, cy - u(15), x + chip_w, cy + u(15))
    _sticker_rect(draw, box, (u(3), u(10), u(10), u(10)), fill=c_bg)
    _dashed_rounded_rect(draw, box, (u(3), u(10), u(10), u(10)), c_bd, dash=u(5), gap=u(4), width=u(1))
    _draw_mixed(draw, x + u(10), cy, parts, fill=c_fg)
    x += chip_w + g
    _sticker_rect(draw, (x, cy - u(15), x + lv_w, cy + u(15)), (u(3), u(10), u(10), u(10)),
                  fill=_IMG_ACCENT)
    draw.text((x + u(10), cy), lv_txt, font=f_badge, fill="#ffffff", anchor="lm")
    x += lv_w + g
    draw.text((x, cy), hint, font=f_small, fill=_IMG_INK2, anchor="lm")
    for dx in range(0, int(W), u(10)):
        draw.line((dx, top_h, min(dx + u(6), W), top_h), fill=_IMG_RULE, width=u(2))
    # 三色和纸胶带条：半透明，盖在顶栏虚线上（web #topbar::after：height 13px、
    # bottom -8px ⇒ 条带自顶栏底边上方 5px 盖到下方 8px，虚线从条下透出）
    tape_colors = [(246, 198, 212), (255, 224, 207), (207, 232, 224)]
    seg_w, sx, i = u(26), 0, 0
    strip = Image.new("RGBA", (W, u(13)), (0, 0, 0, 0))
    sd = ImageDraw.Draw(strip)
    while sx < W:
        sd.rectangle((sx, 0, min(sx + seg_w, W), u(13)), fill=tape_colors[i % 3] + (128,))
        sx += seg_w
        i += 1
    im.paste(strip, (0, top_h - u(5)), strip)

    # —— 补偿卡（左列首）：标题 + 共 N 刀印章徽章 + 成员 chips（头像+名字 ×刀数）
    x0, y = comp_card["_x"], comp_card["_y"]
    _sticker_rect(draw, (x0, y, x0 + card_w, y + comp_card["h"]), (u(4), u(12), u(12), u(12)),
                  fill=_IMG_PANEL, outline=_IMG_BORDER, width=u(1))
    tp = _tape_rgba(u(64), u(14))  # web .boss::before：64×14 @ left18 top-6
    im.paste(tp, (x0 + u(18), y - u(6)), tp)
    ix, iy = x0 + pad, y + pad
    draw.text((ix, iy + u(12)), "待出补偿", font=f_name, fill=_IMG_INK, anchor="lm",
              stroke_width=u(1), stroke_fill=_IMG_PANEL)
    bparts = [("共 ", f_chip_cjk), (f"{comp_card['total']}", f_chip_num), (" 刀", f_chip_cjk)]
    bw = _mixed_w(scratch, bparts) + u(16)
    bx = x0 + card_w - pad - bw
    _sticker_rect(draw, (bx, iy + u(1), bx + bw, iy + u(21)), (u(3), u(8), u(8), u(8)),
                  fill=_IMG_SEAL_TINT)
    _draw_mixed(draw, bx + u(8), iy + u(11), bparts, fill=_IMG_SHU)
    cy2 = iy + u(22) + u(7)
    if not comp_card["rows"]:
        draw.text((ix, cy2 + u(12)), "暂无补偿", font=f_small, fill=_IMG_INK3)
    for chips_row in comp_card["rows"]:
        cx2 = ix
        for text, qqid, cw in chips_row:
            _sticker_rect(draw, (cx2, cy2, cx2 + cw, cy2 + u(27)), (u(3), u(13), u(13), u(13)),
                          fill=_IMG_PANEL, outline=_IMG_BORDER, width=u(1))
            cx3 = cx2 + u(10)
            av = _round_avatar(qqid, text, u(18))
            im.paste(av, (cx3, cy2 + u(3)), av)
            _draw_rich(draw, (cx3 + u(23), cy2 + u(12)), text, f_chip, _IMG_INK, "lm")
            cx2 += cw + u(6)
        cy2 += u(25) + u(5)

    # —— boss 卡（竖向双列定位：左列 boss1/2、右列 boss3/4/5）
    for c in cards:
        _boss_card_draw(im, draw, c["_x"], c["_y"], c, ctx)

    im = im.resize((W // S, H // S), Image.LANCZOS)
    bio = BytesIO()
    im.save(bio, format="PNG")
    return bio.getvalue()


def _boss_card_draw(im, draw, x0, y, c, ctx):
    """boss 卡绘制（状态图与单卡图共用）：胶带+blob 头像+周目+血量+血条+chips。"""
    u, scratch = ctx["u"], ctx["scratch"]
    card_w, pad, inner = ctx["card_w"], ctx["pad"], ctx["inner"]
    f_name, f_num_sb = ctx["f_name"], ctx["f_num_sb"]
    f_num_b, f_num, f_cjk = ctx["f_num_b"], ctx["f_num"], ctx["f_cjk"]
    f_b_cjk = ctx["f_num_b_cjk"]  # 血量「亿」字（web hp-now 700 重的 CJK 回退）
    f_small, f_chip, f_tiny = ctx["f_small"], ctx["f_chip"], ctx["f_tiny"]
    _sticker_rect(draw, (x0, y, x0 + card_w, y + c["h"]), (u(4), u(12), u(12), u(12)),
                  fill=_IMG_PANEL, outline=_IMG_BORDER, width=u(1))
    tp = _tape_rgba(u(64), u(14))  # web .boss::before：64×14 @ left18 top-6
    im.paste(tp, (x0 + u(18), y - u(6)), tp)
    ix, iy = x0 + pad, y + pad
    # 头行：blob 头像（描边+左倾 2°）+ N-(SemiBold)+名字(粗体)
    av = _boss_icon(c["icon"], u(46)).rotate(2, expand=True, resample=Image.BICUBIC)
    im.paste(av, (ix, iy), av)
    tx = ix + u(46) + u(10)
    num_part = f"{c['num']}-"
    asc = max(f_num_sb.getmetrics()[0], f_name.getmetrics()[0])
    desc = max(f_num_sb.getmetrics()[1], f_name.getmetrics()[1])
    name_base = iy + u(13) + (asc - desc) / 2  # 编号与名字跨字体基线对齐
    draw.text((tx, name_base), num_part, font=f_num_sb, fill=_IMG_INK, anchor="ls")
    tx2 = tx + scratch.textlength(num_part, font=f_num_sb)
    _draw_rich(draw, (tx2, name_base), c["name"], f_name, _IMG_INK, "ls",
               stroke_width=u(1), stroke_fill=_IMG_PANEL)
    # 周目 + 下周目（同行，同 web）
    cy_row = iy + u(36)
    cyc_parts = [(f"{c['cycle']} ", f_num), ("周目", f_cjk)]
    _draw_mixed(draw, tx, cy_row, cyc_parts, fill=_IMG_INK3)  # web .cycle=ink-3
    cyc_w = _mixed_w(scratch, cyc_parts)
    if c["is_next"]:
        tgx = tx + cyc_w + u(8)
        _sticker_rect(draw, (tgx, cy_row - u(8), tgx + u(48), cy_row + u(8)),
                      (u(8), u(8), u(8), u(2)),  # web .tag：999 999 999 2px（左下方角）
                      fill=_IMG_TEAL_TINT, outline=_IMG_TEAL, width=u(1))
        draw.text((tgx + u(24), cy_row), "下周目", font=f_tiny, fill=_IMG_TEAL, anchor="mm")
    # 血量行（基线对齐，同 web）
    hb = iy + u(68)
    hp_s, hp_unit = _fmt_hp(c["health"]), ""
    if hp_s.endswith("亿"):
        hp_s, hp_unit = hp_s[:-1], "亿"
    draw.text((ix, hb), hp_s, font=f_num_b, fill=_IMG_INK, anchor="ls")
    hx = ix + scratch.textlength(hp_s, font=f_num_b)
    if hp_unit:  # web fmtHp：当前血量同样带「亿」（此前算了没画）
        draw.text((hx, hb), hp_unit, font=f_b_cjk, fill=_IMG_INK, anchor="ls")
        hx += scratch.textlength(hp_unit, font=f_b_cjk)
    full_s = _fmt_hp(c["full"])
    full_unit = ""
    if full_s.endswith("亿"):
        full_s, full_unit = full_s[:-1], "亿"
    draw.text((hx + u(6), hb), f"/ {full_s}", font=f_num, fill=_IMG_INK3, anchor="ls")
    hx2 = hx + u(6) + scratch.textlength(f"/ {full_s}", font=f_num)
    if full_unit:
        draw.text((hx2 + u(2), hb), full_unit, font=f_cjk, fill=_IMG_INK3, anchor="ls")
    if c["killed"]:
        bw = u(46)
        draw.rounded_rectangle((x0 + card_w - pad - bw, hb - u(16), x0 + card_w - pad, hb + u(2)),
                               radius=u(3), fill=_IMG_INK3)
        draw.text((x0 + card_w - pad - bw / 2, hb - u(7)), "击破", font=f_tiny,
                  fill="#ffffff", anchor="mm")
    # 血条（圆角虚线轮廓 + 填充 + 白刻度）
    by = hb + u(7)
    draw.rounded_rectangle((ix, by, ix + inner, by + u(14)), radius=u(7), fill=(243, 230, 226))
    _dashed_rounded_rect(draw, (ix, by, ix + inner, by + u(14)), u(7), _IMG_RULE,
                         dash=u(5), gap=u(4), width=u(1))
    if c["pct"] > 0:
        fw = max(u(5), int((inner - u(4)) * c["pct"] / 100))
        fill_c = _IMG_OK if c["pct"] >= 62 else (_IMG_WARN_C if c["pct"] >= 25 else _IMG_SHU)
        draw.rounded_rectangle((ix + u(2), by + u(2), ix + u(2) + fw, by + u(12)), radius=u(5), fill=fill_c)
    for tick in (25, 50, 75):
        txp = ix + int(inner * tick / 100)
        draw.line((txp, by + u(2), txp, by + u(12)), fill=(255, 255, 255), width=u(1))
    # chips 流式
    cy2 = by + u(14) + u(6)
    if c["chips_empty"]:
        draw.text((ix, cy2 + u(12)), "暂无挑战 · 暂无预约", font=f_small, fill=_IMG_INK3)
    for chips_row in c["rows_of_chips"]:
        cx2 = ix
        for segs, bg, bd, qqid, cw, fb_name in chips_row:
            _sticker_rect(draw, (cx2, cy2, cx2 + cw, cy2 + u(25)),
                          (u(3), u(12), u(12), u(12)), fill=bg, outline=bd, width=u(1))
            cx3 = cx2 + u(10)
            if qqid is not None:
                av = _round_avatar(qqid, fb_name or "?", u(18))
                im.paste(av, (cx3, cy2 + u(3)), av)
                cx3 += u(23)
            for t, col, f in segs:
                _draw_rich(draw, (cx3, cy2 + u(12)), t, f, col, "lm")
                cx3 += _rich_textlength(draw, t, f)
            cx2 += cw + u(6)
        cy2 += u(25) + u(5)


def render_boss_card(boss: dict) -> bytes:
    """单个 boss 卡图——群聊申请出刀/报刀/尾刀/挂树等交互指令回图（=状态图卡片同款）。"""
    S = 3
    K = 1.3

    def u(v):
        return int(v * S * K)

    ctx = _card_ctx(u)
    c = _boss_card_measure(boss, ctx)
    margin, top_pad = u(12), u(8)  # 顶部留胶带出挑
    W = margin * 2 + ctx["card_w"]
    H = margin + top_pad + c["h"] + margin
    im = Image.new("RGB", (W, H), _IMG_PAPER)
    _boss_card_draw(im, ImageDraw.Draw(im), margin, margin + top_pad, c, ctx)
    im = im.resize((W // S, H // S), Image.LANCZOS)
    bio = BytesIO()
    im.save(bio, format="PNG")
    return bio.getvalue()


def text_2_pic(text: str, weight: int, height: int, bg_color=(255, 255, 255),
               text_color="#000000", font_size: int = 15, offset=(10, 5)) -> bytes:
    """业绩表等文本转图（移植 realize.text_2_pic）。"""
    im = Image.new("RGB", (weight, height), bg_color)
    font = load_font(font_size)
    ImageDraw.Draw(im).text(offset, text, font=font, fill=text_color)
    bio = BytesIO()
    im.save(bio, format="PNG")
    return bio.getvalue()


# ================================================================= 『查刀』表（web 记录页同款渲染）
_IMG_INK = (70, 58, 60)        # 樱花手帖 ink #463a3c
_IMG_INK2 = (138, 115, 119)    # ink-2
_IMG_PAPER = (250, 243, 238)   # paper
_IMG_PANEL = (255, 253, 251)   # panel
_IMG_BORDER = (237, 220, 218)  # rule
_IMG_RULE = (216, 188, 185)    # rule-strong
_IMG_HEAD_BG = (246, 235, 232)  # th-bg
_IMG_ACCENT = (226, 132, 159)   # accent
_IMG_ACCENT_S = (207, 106, 136)  # accent-strong
_IMG_ACCENT_TINT = (251, 240, 243)
_IMG_ACCENT_HI = (247, 223, 231)  # accent-tint-hi（头像描边环）
_IMG_OK = (85, 148, 110)
_IMG_OK_TINT = (234, 243, 236)
_IMG_SHU = (188, 74, 60)
_IMG_SEAL_TINT = (249, 232, 228)
_IMG_TEAL = (77, 158, 148)
_IMG_TEAL_TINT = (232, 243, 241)
_IMG_GOLD = (168, 128, 42)
_IMG_INK3 = (185, 159, 164)    # ink-3
_IMG_WARN_C = (199, 142, 58)   # warn 蜂蜜橙
_IMG_WARN_TINT = (247, 239, 221)  # warn-tint #f7efdd（挂树 chip 黄底）


def _blob_mask(size: int) -> Image.Image:
    """web .boss-ava 遮罩：border-radius 40% 60% 55% 45% / 50% 45% 55% 50%
    = 四角椭圆弧 + 短直边的平滑鹅卵石形（非波浪花边）。
    CSS 溢出压缩 f=100/115（右缘横半径和 60%+55% 超限，全体半径统一缩放）。"""
    import math
    f = 100 / 115
    hr = (40 * f, 60 * f, 55 * f, 45 * f)  # tl/tr/br/bl 横向半径 %
    vr = (50 * f, 45 * f, 55 * f, 50 * f)  # 纵向半径 %
    rx = [p / 100 * size for p in hr]
    ry = [p / 100 * size for p in vr]
    pts = []

    def arc(cx, cy, arx, ary, a0, a1):
        n = max(6, int(abs(a1 - a0) / (math.pi / 12)))
        for k in range(n + 1):
            a = a0 + (a1 - a0) * k / n
            pts.append((cx + arx * math.cos(a), cy + ary * math.sin(a)))

    pts.append((rx[0], 0))
    arc(size - rx[1], ry[1], rx[1], ry[1], -math.pi / 2, 0)      # 右上（与右下弧相接，直边为零）
    arc(size - rx[2], size - ry[2], rx[2], ry[2], 0, math.pi / 2)  # 右下
    arc(rx[3], size - ry[3], rx[3], ry[3], math.pi / 2, math.pi)   # 左下
    arc(rx[0], ry[0], rx[0], ry[0], math.pi, 3 * math.pi / 2)      # 左上
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    return m


def _boss_icon(icon_id: str, size: int) -> Image.Image:
    """boss 头像（web .boss-ava 同款：blob 异形 + #f7dfe7 描边环，缺文件=粉底占位）。"""
    ring = max(2, size // 24)  # web border 2px @ 48px
    art = size - ring * 2
    fill = Image.new("RGBA", (art, art), _IMG_ACCENT_TINT + (255,))
    path = os.path.join(PKG_DIR, "static", "boss_icon", f"{icon_id}.webp")
    if os.path.isfile(path):
        try:
            with Image.open(path) as src:
                fill.paste(src.convert("RGBA").resize((art, art), Image.LANCZOS), (0, 0))
        except Exception:  # noqa: BLE001
            pass
    im = Image.new("RGBA", (art, art), (0, 0, 0, 0))
    im.paste(fill, (0, 0), _blob_mask(art))
    back = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    back.paste(Image.new("RGBA", (size, size), _IMG_ACCENT_HI + (255,)), (0, 0), _blob_mask(size))
    back.alpha_composite(im, (ring, ring))
    return back


def _dashed_rect(draw, box, color, dash=4, gap=3, width=1):
    """虚线矩形（web 刀轨空位格）。"""
    x0, y0, x1, y1 = box

    def seg(p0, p1):
        xa, ya = p0
        xb, yb = p1
        if xa == xb and ya == yb:
            return
        step = 1 if (xb > xa or yb > ya) else -1
        total = abs(xb - xa) + abs(yb - ya)
        d = 0
        while d < total:
            e = min(d + dash, total)
            if xa == xb:
                draw.line((xa, ya + step * d, xa, ya + step * e), fill=color, width=width)
            else:
                draw.line((xa + step * d, ya, xa + step * e, ya), fill=color, width=width)
            d = e + gap

    seg((x0, y0), (x1, y0))
    seg((x0, y1), (x1, y1))
    seg((x0, y0), (x0, y1))
    seg((x1, y0), (x1, y1))


def _round_avatar(qqid: int, name: str, size: int) -> Image.Image:
    """圆形头像；缺文件=粉底+名字首字。"""
    fill = Image.new("RGBA", (size, size), _IMG_ACCENT_TINT + (255,))
    path = os.path.join(_avatar_dir, f"{qqid}.jpg")
    if os.path.isfile(path):
        try:
            with Image.open(path) as src:
                fill.paste(src.convert("RGB").resize((size, size), Image.LANCZOS), (0, 0))
        except Exception:  # noqa: BLE001
            pass
    else:
        d = ImageDraw.Draw(fill)
        segs = _rich_segments(name or "?")
        ch = segs[0][0] if segs[0][1] else segs[0][0][:1] or "?"
        _draw_rich(d, (size / 2, size / 2), ch, load_font(int(size * 0.52)),
                   _IMG_ACCENT_S, "mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    im.paste(fill, (0, 0), mask)
    return im


_WIN_FONTS = "C:\\Windows\\Fonts"
_font_bold_cache: dict = {}
_font_num_cache: dict = {}
_BOLD_CANDIDATES = [os.path.join(_WIN_FONTS, "msyhbd.ttc"),
                    "/System/Library/Fonts/PingFang.ttc"]
_NUM_CANDIDATES = [os.path.join(_WIN_FONTS, "bahnschrift.ttf"),
                   "/System/Library/Fonts/Supplemental/DIN Alternate.ttf"]


def _load_first(candidates, size, cache):
    if size in cache:
        return cache[size]
    f = None
    for p in candidates:
        if os.path.isfile(p):
            try:
                f = ImageFont.truetype(p, size)
                break
            except OSError:
                continue
    if f is None:
        f = load_font(size)
    cache[size] = f
    return f


def load_font_bold(size):
    return _load_first(_BOLD_CANDIDATES, size, _font_bold_cache)


def load_font_num_bold(size):
    """Bahnschrift SemiBold（web hp-now 的 font-weight:700）。"""
    key = ("nb", size)
    if key in _font_num_cache:
        return _font_num_cache[key]
    f = None
    p = os.path.join(_WIN_FONTS, "bahnschrift.ttf")
    if os.path.isfile(p):
        try:
            f = ImageFont.truetype(p, size)
            try:
                f.set_variation_by_name("SemiBold")
            except Exception:  # noqa: BLE001
                try:
                    f.set_variation_by_axes([600])
                except Exception:  # noqa: BLE001
                    pass
        except OSError:
            f = None
    if f is None:
        f = load_font_bold(size)
    _font_num_cache[key] = f
    return f


def load_font_num(size):
    return _load_first(_NUM_CANDIDATES, size, _font_num_cache)


# ================================================================= emoji 混排
# msyh 没有彩色 emoji 字形：按 emoji 段切给 Segoe UI Emoji（COLR 彩色）绘制。
_EMOJI_CANDIDATES = [
    os.path.join(_WIN_FONTS, "seguiemj.ttf"),
    "/System/Library/Fonts/Apple Color Emoji.ttc",
]
_emoji_font_cache: dict = {}
_emoji_font_path = False  # False=未探测；None=不可用


def _emoji_path():
    global _emoji_font_path
    if _emoji_font_path is not False:
        return _emoji_font_path
    _emoji_font_path = None
    for p in _EMOJI_CANDIDATES:
        if not os.path.isfile(p):
            continue
        try:
            ImageFont.truetype(p, 20)
        except Exception:  # noqa: BLE001
            continue
        _emoji_font_path = p
        break
    return _emoji_font_path


def _emoji_font(size: int):
    path = _emoji_path()
    if not path:
        return None
    key = (path, size)
    if key not in _emoji_font_cache:
        try:
            _emoji_font_cache[key] = ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            _emoji_font_cache[key] = None
    return _emoji_font_cache[key]


# 非 BMP 表情、旗帜、keycap、以及带 U+FE0F 的 BMP 符号；普通符号（♡☆）留给主字体
_EMOJI_RE = re.compile(
    "("
    r"[\U0001F1E6-\U0001F1FF]{2}"
    r"|[0-9#*]\ufe0f?\u20e3"
    r"|[\u2600-\u27bf\u2b00-\u2bff\u00a9\u00ae\u2122]\ufe0f"
    r"|[\U0001F000-\U0001FAFF](?:[\ufe0f\U0001F3FB-\U0001F3FF])?"
    r"(?:\u200d[\U0001F000-\U0001FAFF](?:[\ufe0f\U0001F3FB-\U0001F3FF])?)*"
    ")"
)


def _rich_segments(text):
    """把字符串切成 [(片段, 是否emoji), ...]。"""
    out, pos = [], 0
    for m in _EMOJI_RE.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], False))
        out.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], False))
    return out or [(str(text), False)]


def _seg_font(seg, emoji, font):
    if emoji:
        ef = _emoji_font(getattr(font, "size", 16) or 16)
        if ef is not None:
            return ef
    return font


def _rich_textlength(draw, text, font):
    return sum(draw.textlength(seg, font=_seg_font(seg, e, font))
               for seg, e in _rich_segments(str(text)))


def _clip_rich(text, n):
    """按字符数截断，避免切断 ZWJ/变体选择符/肤色修饰。"""
    s = str(text)[:n]
    while s and (s[-1] in "\u200d\ufe0f"
                 or "\U0001F3FB" <= s[-1] <= "\U0001F3FF"):
        s = s[:-1]
    return s


def _draw_rich(draw, xy, text, font, fill, anchor="lm",
               stroke_width=0, stroke_fill=None):
    """emoji 混排绘制：单位换算与 anchor 语义与 draw.text 一致（l/m/r + t/m/s/b）。"""
    text = str(text)
    segs = _rich_segments(text)
    width = sum(draw.textlength(seg, font=_seg_font(seg, e, font)) for seg, e in segs)
    x, y = xy
    h = anchor[0] if len(anchor) > 1 else "l"
    v = anchor[1] if len(anchor) > 1 else "a"
    cx = x - width if h == "r" else (x - width / 2 if h == "m" else x)
    asc, desc = font.getmetrics()
    if v == "t" or v == "a":
        base = y + asc
    elif v == "b":
        base = y - desc
    elif v == "s":
        base = y
    else:  # m
        base = y + (asc - desc) / 2
    for seg, emoji in segs:
        f = _seg_font(seg, emoji, font)
        color_emoji = emoji and f is not font
        draw.text((cx, base), seg, font=f, fill=fill, anchor="ls",
                  stroke_width=0 if color_emoji else stroke_width,
                  stroke_fill=stroke_fill, embedded_color=color_emoji)
        cx += draw.textlength(seg, font=f)
    return width



def _truncate(draw, s: str, font, max_w: int):
    if not s or _rich_textlength(draw, s, font) <= max_w:
        return s
    out = ""
    for seg, emoji in _rich_segments(str(s)):
        for part in ([seg] if emoji else list(seg)):
            if _rich_textlength(draw, out + part + "…", font) <= max_w:
                out += part
            else:
                return out + "…"
    return out + "…"


def _chip_fit(scratch, segs, max_w):
    """chip 多段文案（web chipOf 着色语义）总宽超限时从尾部逐字截断并补 …。"""
    def tw():
        return sum(_rich_textlength(scratch, t, f) for t, _c, f in segs)

    if tw() <= max_w:
        return segs
    while segs and tw() > max_w - _rich_textlength(scratch, "…", segs[-1][2]):
        t, c, f = segs[-1]
        if len(t) <= 1:
            segs.pop()
        else:
            segs[-1] = (_clip_rich(t, len(t) - 1), c, f)
    if segs:
        t, c, f = segs[-1]
        segs[-1] = (t + "…", c, f)
    return segs


def _mixed_w(scratch, parts):
    return sum(scratch.textlength(t, font=f) for t, f in parts)


def _draw_mixed(draw, x, y, parts, fill=None):
    """多段混排（数字字体/中文字体分段），按基线对齐（anchor=ls）：数字字体与 CJK
    行盒高度不同，anchor=lm 会高低错位（用户实测第x刀/33-x王 y 轴偏移）。y=整行视觉中心。"""
    amax = max(f.getmetrics()[0] for _t, f in parts)
    dmax = max(f.getmetrics()[1] for _t, f in parts)
    baseline = y + (amax - dmax) / 2
    cx = x
    for t, f in parts:
        draw.text((cx, baseline), t, font=f, fill=fill, anchor="ls")
        cx += draw.textlength(t, font=f)
    return cx


def render_day_table(title: str, date_str: str, rows: List[dict],
                     total: float, quota: int, comp_left: float) -> bytes:
    """『查刀』当天出刀表——web「记录」页同款渲染：头像+今日已SL+刀数徽章+6 格刀轨。
    rows: [{qqid, name, fin, sl, recs}]，已按展示顺序排好。"""
    S = 3          # 3x 超采样
    K = 1.3        # 逻辑尺寸放大：QQ 端会把小图放大显示，源图小=糊

    def u(v):
        return int(v * S * K)

    margin, pad = u(12), u(14)
    row_h, ava = u(52), u(36)
    name_w, pill_w, gap = u(116), u(48), u(10)
    cell_w, cell_h, cell_gap = u(52), u(26), u(3)
    track_w = cell_w * 6 + cell_gap * 5
    W = margin * 2 + pad * 2 + ava + gap + name_w + gap + pill_w + gap + track_w

    head_font, name_font = load_font(u(12)), load_font(u(13))
    small_font, seg_font = load_font(u(10)), load_font(u(10.5))
    pill_font, title_font = load_font(u(13)), load_font(u(16))

    # —— 卡片尺寸与底
    head_h = u(24)
    head_row, dbl = u(22), u(2)
    card_h = pad + head_h + u(6) + head_row + u(5) + row_h * max(len(rows), 1) + pad
    W_card = W - margin * 2
    im = Image.new("RGB", (W, card_h + margin * 2), _IMG_PAPER)
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle((margin, margin, margin + W_card, margin + card_h),
                           radius=u(12), fill=_IMG_PANEL, outline=_IMG_BORDER, width=u(1))
    ox, oy = margin + pad, margin + pad  # 卡片内容原点

    # —— 头部：群名 + 日期/进度
    title = _truncate(draw, title, title_font, W_card - pad * 2 - u(230))
    _draw_rich(draw, (ox, oy), title, title_font, _IMG_INK, "lt",
               stroke_width=u(1), stroke_fill=_IMG_PANEL)
    sub = f"{date_str}   出刀 {total:g}/{quota}   待出补偿 {comp_left:g}"
    draw.text((ox + W_card - pad * 2 - draw.textlength(sub, font=head_font), oy + u(5)),
              sub, font=head_font, fill=_IMG_INK2)
    y = oy + head_h + u(6)

    # —— 表头（成员/刀数/出刀详情）+ 双线
    draw.rectangle((ox, y, ox + W_card - pad * 2, y + head_row), fill=_IMG_HEAD_BG)
    cx = ox
    for label, w in (("成员", ava + gap + name_w), ("刀数", pill_w), ("出刀详情", track_w + gap)):
        draw.text((cx + u(4), y + u(5)), label, font=small_font, fill=_IMG_ACCENT_S)
        cx += w
    draw.line((ox, y + head_row + dbl, ox + W_card - pad * 2, y + head_row + dbl), fill=_IMG_RULE, width=u(1))
    draw.line((ox, y + head_row + dbl * 2, ox + W_card - pad * 2, y + head_row + dbl * 2), fill=_IMG_RULE, width=u(1))
    y += head_row + u(5)

    # —— 成员行
    for ri, r in enumerate(rows):
        cy = y + row_h // 2
        # 头像
        im.paste(_round_avatar(r["qqid"], r["name"], ava), (ox, y + (row_h - ava) // 2),
                 _round_avatar(r["qqid"], r["name"], ava))
        # 名字 + 今日已SL
        nm = _truncate(draw, r["name"], name_font, name_w)
        _draw_rich(draw, (ox + ava + gap, cy - u(12)), nm, name_font, _IMG_INK, "lm",
                   stroke_width=u(1), stroke_fill=_IMG_PANEL)
        if r["sl"]:
            draw.text((ox + ava + gap, cy + u(6)), "今日已SL", font=small_font, fill=_IMG_GOLD)
        # 刀数徽章
        ok = r["fin"] >= 3
        px = ox + ava + gap + name_w + gap
        draw.rounded_rectangle((px, cy - u(11), px + pill_w, cy + u(11)), radius=u(11),
                               fill=_IMG_OK_TINT if ok else _IMG_SEAL_TINT)
        draw.text((px + pill_w / 2, cy), f"{r['fin']:g}", font=pill_font,
                  fill=_IMG_OK if ok else _IMG_SHU, anchor="mm")
        # 6 格刀轨（完整刀占 2 格；尾=绿 补=青 空=虚线）
        tx = px + pill_w + gap
        ty = cy - cell_h // 2
        segs, rem = [], 6
        for c in r["recs"]:
            comp = bool(c["is_continue"])
            tail = c["boss_health_remain"] == 0 and not comp
            span = 1 if (comp or tail) else 2
            dmg = (f"{round(c['challenge_damage'] / 10000)}w"
                   if c["challenge_damage"] >= 10000 else f"{c['challenge_damage']}")
            kind = "补" if comp else ("尾" if tail else "")
            segs.append({"span": span, "cls": "comp" if comp else ("tail" if tail else "full"),
                         "text": f"{c['boss_cycle']}-{c['boss_num']} {dmg}", "kind": kind,
                         "behalf": bool(c["behalf"])})
            rem -= span
        while rem >= 2:
            segs.append({"span": 2, "cls": "empty", "text": "", "kind": "", "behalf": False})
            rem -= 2
        if rem == 1:
            segs.append({"span": 1, "cls": "empty", "text": "", "kind": "", "behalf": False})
        sx = tx
        for sg in segs:
            w = cell_w * sg["span"] + cell_gap * (sg["span"] - 1)
            box = (sx, ty, sx + w, ty + cell_h)
            if sg["cls"] == "empty":
                _dashed_rect(draw, box, _IMG_BORDER, dash=u(4), gap=u(3), width=u(1))
            else:
                bg = {"full": _IMG_ACCENT_TINT, "tail": _IMG_OK_TINT, "comp": _IMG_TEAL_TINT}[sg["cls"]]
                bd = {"full": _IMG_ACCENT, "tail": _IMG_OK, "comp": _IMG_TEAL}[sg["cls"]]
                draw.rounded_rectangle(box, radius=u(6), fill=bg, outline=bd, width=u(1))
            if sg["text"]:
                # 主文本+kind 同字号自适应缩小（保数据完整），预留 kind 宽度，整体居中（kind 不压边框）
                size, min_size = u(10.5), u(7)
                f = load_font(size)
                kw = draw.textlength(sg["kind"], font=f) if sg["kind"] else 0
                avail = w - u(8) - (kw + u(2) if kw else 0)
                while draw.textlength(sg["text"], font=f) > avail and size > min_size:
                    size = max(min_size, int(size * avail / max(1, draw.textlength(sg["text"], font=f))))
                    f = load_font(size)
                    kw = draw.textlength(sg["kind"], font=f) if sg["kind"] else 0
                    avail = w - u(8) - (kw + u(2) if kw else 0)
                main = _truncate(draw, sg["text"], f, avail)
                mw = draw.textlength(main, font=f)
                sx0 = sx + (w - mw - kw) / 2
                draw.text((sx0, ty + cell_h / 2), main, font=f, fill=_IMG_INK, anchor="lm")
                if kw:
                    draw.text((sx0 + mw, ty + cell_h / 2), sg["kind"], font=f,
                              fill={"tail": _IMG_OK, "comp": _IMG_TEAL}.get(sg["cls"], _IMG_INK2),
                              anchor="lm")
            sx += w + cell_gap
        # 行间虚线分隔
        if ri < len(rows) - 1:
            _dashed_rect(draw, (ox, y + row_h, ox + W_card - pad * 2, y + row_h),
                         _IMG_BORDER, dash=u(4), gap=u(3), width=u(1))
        y += row_h

    im = im.resize((W // S, im.height // S), Image.LANCZOS)
    bio = BytesIO()
    im.save(bio, format="PNG")
    return bio.getvalue()


def render_member_table(title: str, date_str: str, member: str, qqid: int,
                        rows: List[dict], total: float) -> bytes:
    """『刀伤』个人出刀表——web 记录页按人视图同款风格（一行=一刀，当日记录）。
    rows: [{idx, cycle, boss, damage, kind(full/tail/comp), behalf}]，已按出刀顺序排好。"""
    S = 3
    K = 1.3

    def u(v):
        return int(v * S * K)

    margin, pad = u(12), u(14)
    row_h = u(38)
    W_card = u(470)
    inner = W_card - pad * 2
    W = margin * 2 + W_card
    head_h = u(52)
    head_row = u(26)
    body_h = head_h + u(8) + head_row + u(5) + row_h * max(len(rows), 1)
    card_h = body_h + u(40) + pad
    H = card_h + margin * 2
    im = Image.new("RGB", (W, H), _IMG_PAPER)
    draw = ImageDraw.Draw(im)
    draw.rounded_rectangle((margin, margin, margin + W_card, margin + card_h),
                           radius=u(12), fill=_IMG_PANEL, outline=_IMG_BORDER, width=u(1))
    tp = _tape_rgba(u(64), u(14))  # web .boss::before：64×14 @ left18 top-6
    im.paste(tp, (margin + u(18), margin - u(6)), tp)
    ox, oy = margin + pad, margin + pad
    f_title = load_font_bold(u(16))
    f_head = load_font(u(12))
    f_kind = load_font(u(11))
    f_num = load_font_num(u(11.5))
    f_boss_cjk = load_font(u(11.5))
    f_dmg = load_font_num_bold(u(13))
    f_foot = load_font(u(12.5))
    scratch = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    # —— 头部：头像 + 成员名 + 日期·群名 + 今日 x/3 chip（虚线贴纸）
    av = _round_avatar(qqid, member, u(40))
    im.paste(av, (ox, oy + u(2)), av)
    nm_x = ox + u(48)
    _draw_rich(draw, (nm_x, oy + u(11)), _truncate(draw, member, f_title, inner - u(200)),
               f_title, _IMG_INK, "lm", stroke_width=u(1), stroke_fill=_IMG_PANEL)
    _draw_rich(draw, (nm_x, oy + u(33)), f"{date_str} · {title}", f_head, _IMG_INK2, "lm")
    parts = [("今日 ", load_font_bold(u(12))),
             (f"{total:g} / 3 ", load_font_num_bold(u(12))),
             ("刀", load_font_bold(u(12)))]
    chip_w = _mixed_w(scratch, parts) + u(20)
    done = total >= 3
    cx0 = margin + W_card - pad - chip_w
    chip_box = (cx0, oy + u(5), cx0 + chip_w, oy + u(31))
    _sticker_rect(draw, chip_box, (u(3), u(10), u(10), u(10)),
                  fill=_IMG_OK_TINT if done else _IMG_ACCENT_TINT)
    _dashed_rounded_rect(draw, chip_box, (u(3), u(10), u(10), u(10)),
                         _IMG_OK if done else _IMG_ACCENT, dash=u(5), gap=u(4), width=u(1))
    _draw_mixed(draw, cx0 + u(10), oy + u(18), parts,
                fill=_IMG_OK if done else _IMG_ACCENT_S)

    y = oy + head_h + u(6)
    # —— 表头
    draw.rectangle((ox, y, ox + inner, y + head_row), fill=_IMG_HEAD_BG)
    cx = ox
    for label, w in (("刀次", u(66)), ("目标", u(92)), ("伤害", inner - u(66) - u(92) - u(118)), ("类型", u(118))):
        draw.text((cx + u(4), y + u(6)), label, font=f_kind, fill=_IMG_ACCENT_S)
        cx += w
    y += head_row + u(5)

    # —— 行（一行=一刀）
    kind_style = {"full": ("完整刀", _IMG_ACCENT_TINT, _IMG_ACCENT),
                  "tail": ("收尾刀", _IMG_OK_TINT, _IMG_OK),
                  "comp": ("补偿刀", _IMG_TEAL_TINT, _IMG_TEAL)}
    for i, r in enumerate(rows):
        cy = y + row_h // 2
        _draw_mixed(draw, ox + u(2), cy, [("第", f_kind), (f"{r['idx']}", f_num), ("刀", f_kind)],
                    fill=_IMG_INK2)
        _draw_mixed(draw, ox + u(70), cy,
                    [(f"{r['cycle']}-", f_num), (f"{r['boss']}王", f_boss_cjk)], fill=_IMG_INK)
        dmg = f"{r['damage']:,}"
        draw.text((ox + u(70) + u(92) + u(8), cy), dmg, font=f_dmg, fill=_IMG_INK, anchor="lm")
        ktext, kbg, kbd = kind_style.get(r["kind"], ("完整刀", _IMG_ACCENT_TINT, _IMG_ACCENT))
        kx = ox + inner - u(118)
        draw.rounded_rectangle((kx, cy - u(10), kx + u(72), cy + u(10)), radius=u(6),
                               fill=kbg, outline=kbd, width=u(1))
        draw.text((kx + u(36), cy), ktext, font=f_kind, fill=kbd, anchor="mm")
        if r.get("behalf"):
            bh = _truncate(draw, r["behalf"], f_kind, u(40))
            draw.text((kx + u(76), cy), f"{bh}代", font=f_kind, fill=_IMG_INK2, anchor="lm")
        if i < len(rows) - 1:
            _dashed_rect(draw, (ox, y + row_h, ox + inner, y + row_h),
                         _IMG_BORDER, dash=u(4), gap=u(3), width=u(1))
        y += row_h
    if not rows:
        draw.text((ox + inner / 2, y + row_h / 2), "尚未出刀", font=f_foot,
                  fill=_IMG_INK3, anchor="mm")

    # —— 底部小结
    fy = margin + card_h - pad - u(14)
    if not rows:
        foot, color = "尚未出刀，今天记得出刀哦~", _IMG_INK3
    elif total < 3:
        foot, color = f"还差 {3 - total:g} 刀下班，加油！", _IMG_INK2
    else:
        foot, color = "今日出刀已完成！", _IMG_OK
    draw.text((ox + u(2), fy), foot, font=f_foot, fill=color, anchor="lm")

    im = im.resize((W // S, H // S), Image.LANCZOS)
    bio = BytesIO()
    im.save(bio, format="PNG")
    return bio.getvalue()


def _stat_num(n) -> str:
    """统计大数：亿保留一位小数（≥100 亿取整），万取整，其余千分位。"""
    n = float(n or 0)
    if n >= 1e8:
        y = n / 1e8
        return (f"{round(y):d}" if y >= 100 else f"{y:.1f}") + "亿"
    if n >= 1e4:
        return f"{round(n / 1e4):d}万"
    return f"{int(n):,}"


def _stat_clock(sec: float) -> str:
    """PCR 时间秒 → 本地时钟 HH:MM（5 点换日）。"""
    local = (5 * 3600 + round(sec or 0)) % 86400
    return f"{local // 3600:02d}:{local % 3600 // 60:02d}"


def _num_unit(s) -> tuple:
    """把「1323亿」拆成 ("1323", "亿")，供数字字体与中文字体分别绘制。"""
    s = str(s)
    for i, ch in enumerate(s):
        if not (ch.isdigit() or ch in ",.-"):
            return s[:i], s[i:]
    return s, ""


# 前三名奖牌色（金/银/铜），图内自绘不用 emoji（字体缺字形）
_MEDAL_COLORS = [_IMG_GOLD, (150, 152, 158), (176, 122, 82)]


def render_stats_report(title: str, range_text: str, data: dict,
                        group_name: str = "") -> bytes:
    """『战报』长图：网页统计页同源数据的纸质手帖版。
    data = stats.build_statistics(...) 的返回值。"""
    S, K = 3, 1.3

    def u(v):
        return int(v * S * K)

    margin, gap, pad = u(12), u(10), u(12)
    W = u(580)
    CW = W - margin * 2
    inner = CW - pad * 2
    x0, x1 = margin + pad, margin + CW - pad

    days = data.get("days") or []
    members = data.get("members") or []
    bosses = data.get("bosses") or []
    hours = data.get("hours") or [0] * 24
    fastest = data.get("fastest") or []
    slowest = data.get("slowest") or []

    f_title = load_font_bold(u(20))
    f_range = load_font(u(10.5))
    f_block = load_font_bold(u(14))
    f_note = load_font(u(10))
    f_name = load_font_bold(u(12.5))
    f_small = load_font(u(11))
    f_tiny = load_font(u(9.5))
    f_num = load_font_num(u(12))
    f_num_s = load_font_num(u(9.5))
    f_tot = load_font_num_bold(u(23))
    f_tot_cjk = load_font_bold(u(13))
    f_tot_label = load_font_bold(u(10.5))
    f_unit = load_font(u(9.5))
    f_unit_s = load_font(u(8.5))

    scratch = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def stat_w(text, f_digit, f_cjk=f_unit):
        num, unit = _num_unit(text)
        return (scratch.textlength(num, font=f_digit)
                + (scratch.textlength(unit, font=f_cjk) if unit else 0))

    def stat_text(text, x, y, f_digit, f_cjk=f_unit, anchor="lm", fill=_IMG_INK):
        """数字用数字字体、亿/万等单位用中文字体（banschrift 无 CJK 字形）。"""
        num, unit = _num_unit(text)
        parts = [(num, f_digit)] + ([(unit, f_cjk)] if unit else [])
        w = _mixed_w(scratch, parts)
        xx = x - w if anchor == "rm" else (x - w / 2 if anchor == "mm" else x)
        _draw_mixed(draw, xx, y, parts, fill=fill)
        return w

    # ---------------- 高度预排
    head_h = u(94)
    chart_h = u(150)
    hours_h = u(126)
    row_h = u(29)
    rank_h = u(24) + row_h * max(len(members), 1)
    boss_card_h = u(170)
    boss_rows = (len(bosses) + 1) // 2 if bosses else 0
    boss_h = (u(34) + boss_rows * boss_card_h + max(0, boss_rows - 1) * u(8)) if bosses else 0
    end_rows = min(max(len(fastest), len(slowest), 1), 10)
    end_h = u(30) + u(21) + u(23) * end_rows + u(22)

    blocks = [chart_h, chart_h, u(30) + rank_h] + ([boss_h] if bosses else []) \
        + [hours_h, end_h]
    H = margin * 2 + head_h + len(blocks) * gap + sum(blocks)

    im = Image.new("RGB", (W, H), _IMG_PAPER)
    draw = ImageDraw.Draw(im)

    def card(y, h):
        _sticker_rect(draw, (margin, y, margin + CW, y + h), (u(4), u(12), u(12), u(12)),
                      fill=_IMG_PANEL, outline=_IMG_BORDER, width=u(1))

    def section(y, text, note=""):
        draw.text((x0, y + u(15)), text, font=f_block, fill=_IMG_ACCENT_S, anchor="lm")
        if note:
            avail = inner - scratch.textlength(text, font=f_block) - u(16)
            if avail > u(24):
                note = _truncate(scratch, note, f_note, avail)
                draw.text((x1, y + u(15)), note, font=f_note, fill=_IMG_INK3, anchor="rm")
        _dashed_rect(draw, (x0, y + u(29), x1, y + u(29)), _IMG_BORDER,
                     dash=u(4), gap=u(3), width=u(1))

    def grid(y0, y1, vmax, fmt):
        for i in range(4):
            gy = y1 - (y1 - y0) * i / 3
            draw.line((x0 + u(30), gy, x1, gy), fill=_IMG_BORDER, width=u(1))
            draw.text((x0 + u(26), gy), fmt(vmax * i / 3), font=f_tiny,
                      fill=_IMG_INK3, anchor="rm")

    # ---------------- 报头
    y = margin
    card(y, head_h)
    tp = _tape_rgba(u(72), u(14))
    im.paste(tp, (margin + CW - u(96), y - u(6)), tp)
    if group_name:
        gname = _truncate(scratch, group_name, f_range, inner * 0.46)
        _draw_rich(draw, (x0, y + u(5)), gname, f_range, _IMG_INK3, "lt")
    title = _truncate(scratch, title, f_title, inner * 0.52)
    draw.text((x0, y + u(20)), title, font=f_title, fill=_IMG_INK, anchor="lt")
    rtext = _truncate(scratch, range_text, f_range, inner * 0.46)
    draw.text((x1, y + u(21)), rtext, font=f_range, fill=_IMG_INK2, anchor="rt")
    total_blades = sum(d["blades"] for d in days)
    total_damage = sum(d["damage"] for d in days)
    total_tails = sum(m["tails"] for m in members)
    totals = [(f"{total_blades:,}", "总刀数"),
              (_stat_num(total_damage), "总伤害"),
              (f"{total_tails:,}", "尾刀"),
              (f"{len(members)}", "出刀成员")]
    cell_w = inner / 4
    ty = y + u(50)
    for i, (num, label) in enumerate(totals):
        cx = x0 + cell_w * (i + 0.5)
        stat_text(num, cx, ty + u(12), f_tot, f_tot_cjk, "mm", _IMG_ACCENT_S)
        draw.text((cx, ty + u(34)), label, font=f_tot_label, fill=_IMG_INK3, anchor="mm")

    # ---------------- 每日刀数
    y += head_h + gap
    card(y, chart_h)
    section(y, "每日刀数")
    py0, py1 = y + u(44), y + u(124)
    vals = [d["blades"] for d in days]
    vmax = max(vals) or 1
    grid(py0, py1, vmax, lambda v: f"{round(v):d}")
    n = len(vals)
    slot = (x1 - (x0 + u(30))) / max(n, 1)
    bw = max(u(4), slot * 0.62)
    for i, v in enumerate(vals):
        if v <= 0:
            continue
        bh = (py1 - py0) * v / vmax
        cx = x0 + u(30) + slot * (i + 0.5)
        draw.rounded_rectangle((cx - bw / 2, py1 - bh, cx + bw / 2, py1),
                               radius=u(2), fill=_IMG_ACCENT)
    every = max(1, -(-n // 10))
    if n <= 31:  # 与 web 同理：点太多时标值会糊成一团
        for i, v in enumerate(vals):
            cx = x0 + u(30) + slot * (i + 0.5)
            bh = (py1 - py0) * v / vmax
            draw.text((cx, py1 - bh - u(4)), f"{v}", font=f_num_s,
                      fill=_IMG_INK2, anchor="mm")
    for i in range(0, n, every):
        cx = x0 + u(30) + slot * (i + 0.5)
        draw.text((cx, py1 + u(5)), days[i]["date"][5:], font=f_tiny,
                  fill=_IMG_INK3, anchor="mt")

    # ---------------- 每日伤害
    y += chart_h + gap
    card(y, chart_h)
    section(y, "每日伤害")
    py0, py1 = y + u(44), y + u(124)
    vals = [d["damage"] for d in days]
    vmax = max(vals) or 1
    grid(py0, py1, vmax, _stat_num)
    pts = [(x0 + u(30) + slot * (i + 0.5), py1 - (py1 - py0) * v / vmax)
           for i, v in enumerate(vals)]
    if len(pts) > 1:
        draw.line(pts, fill=_IMG_GOLD, width=u(2), joint="curve")
    for px, py in pts:
        draw.ellipse((px - u(2.4), py - u(2.4), px + u(2.4), py + u(2.4)), fill=_IMG_GOLD)
    if n <= 15:  # 伤害标签更宽，点少时才标
        for (px, py), v in zip(pts, vals):
            stat_text(_stat_num(v), px, py - u(10), f_num_s, f_unit_s,
                      "mm", _IMG_INK2)
    for i in range(0, n, every):
        cx = x0 + u(30) + slot * (i + 0.5)
        draw.text((cx, py1 + u(5)), days[i]["date"][5:], font=f_tiny,
                  fill=_IMG_INK3, anchor="mt")

    # ---------------- 成员贡献榜（按总伤害降序，与网页默认一致）
    y += chart_h + gap
    card(y, u(30) + rank_h)
    section(y, "成员贡献榜", "按总伤害降序")
    ranked = sorted(members, key=lambda m: m["damage"], reverse=True)
    max_dmg = max((m["damage"] for m in ranked), default=1) or 1
    ry = y + u(30)
    draw.rectangle((x0, ry, x1, ry + u(22)), fill=_IMG_HEAD_BG)
    c_rank = x0 + u(4)
    c_name = x0 + u(32)
    c_blade = x0 + u(206)
    c_tail = x0 + u(252)
    c_bar = x0 + u(298)
    bar_w = x1 - c_bar - u(80)
    for text, cx, anchor in (("成员", c_name, "lm"), ("刀数", c_blade, "rm"),
                             ("尾刀", c_tail, "rm"), ("总伤害", c_bar, "lm")):
        draw.text((cx, ry + u(11)), text, font=f_tiny, fill=_IMG_ACCENT_S, anchor=anchor)
    yrow = ry + u(22)
    for i, m in enumerate(ranked):
        cy = yrow + i * row_h + row_h / 2
        if i < 3:
            draw.ellipse((c_rank, cy - u(7), c_rank + u(14), cy + u(7)),
                         fill=_MEDAL_COLORS[i])
            draw.text((c_rank + u(7), cy), f"{i + 1}", font=f_tiny, fill="#ffffff", anchor="mm")
        else:
            draw.text((c_rank + u(7), cy), f"{i + 1}", font=f_tiny, fill=_IMG_INK3, anchor="mm")
        av = _round_avatar(m["qqid"], m["name"], u(20))
        im.paste(av, (int(c_name), int(cy - u(10))), av)
        nm = _truncate(scratch, m["name"], f_name, u(132))
        _draw_rich(draw, (c_name + u(26), cy), nm, f_name, _IMG_INK, "lm")
        draw.text((c_blade, cy), f"{m['blades']}", font=f_num, fill=_IMG_INK, anchor="rm")
        draw.text((c_tail, cy), f"{m['tails'] or '—'}", font=f_num, fill=_IMG_INK2, anchor="rm")
        bx1 = c_bar + bar_w
        draw.rounded_rectangle((c_bar, cy - u(4.5), bx1, cy + u(4.5)), radius=u(4.5),
                               fill=_IMG_ACCENT_TINT)
        fw = max(u(3), bar_w * m["damage"] / max_dmg)
        draw.rounded_rectangle((c_bar, cy - u(4.5), c_bar + fw, cy + u(4.5)), radius=u(4.5),
                               fill=_IMG_ACCENT)
        stat_text(_stat_num(m["damage"]), bx1 + u(8), cy, f_num, f_unit, "lm", _IMG_INK)
        if i < len(ranked) - 1:
            _dashed_rect(draw, (x0, yrow + (i + 1) * row_h, x1, yrow + (i + 1) * row_h),
                         _IMG_BORDER, dash=u(4), gap=u(3), width=u(1))

    # ---------------- 各王主力（3 列网格）
    y += u(30) + rank_h + gap
    if bosses:
        card(y, boss_h)
        section(y, "各王主力", "上：刀数 · 下：完整刀均伤（不含尾/补）")
        col_w = (inner - gap) / 2
        for idx, b in enumerate(bosses):
            col, row = idx % 2, idx // 2
            bx = x0 + col * (col_w + gap)
            by = y + u(32) + row * (boss_card_h + u(8))
            draw.rounded_rectangle((bx, by, bx + col_w, by + boss_card_h), radius=u(8),
                                   fill=_IMG_PANEL, outline=_IMG_BORDER, width=u(1))
            ic = _boss_icon(b["icon_id"], u(20))
            im.paste(ic, (int(bx + u(8)), int(by + u(7))), ic)
            bname = _truncate(scratch, b["name"] or f"{b['boss']}王", f_name, col_w - u(36))
            draw.text((bx + u(32), by + u(17)), bname, font=f_name, fill=_IMG_INK, anchor="lm")
            yy = by + u(30)
            for label, key in (("刀数", "top"), ("刀均", "top_avg")):
                draw.text((bx + u(8), yy + u(7)), label, font=f_tiny, fill=_IMG_INK3, anchor="lm")
                yy += u(14)
                rows_ = b.get(key) or []
                if not rows_:
                    draw.text((bx + u(8), yy + u(8)), "—", font=f_tiny, fill=_IMG_INK3, anchor="lm")
                    yy += u(16)
                for j, t in enumerate(rows_):
                    my = yy + j * u(18)
                    if j < 3:
                        draw.ellipse((bx + u(8), my + u(3), bx + u(18), my + u(13)),
                                     fill=_MEDAL_COLORS[j])
                        draw.text((bx + u(13), my + u(8)), f"{j + 1}", font=f_tiny,
                                  fill="#ffffff", anchor="mm")
                    val = (f"{t['count']}刀" if key == "top"
                           else f"{_stat_num(t['avg'])}·{t['count']}刀")
                    vw = stat_w(val, f_num)
                    mn = _truncate(scratch, t["name"], f_small, col_w - vw - u(36))
                    _draw_rich(draw, (bx + u(22), my + u(8)), mn, f_small, _IMG_INK, "lm")
                    stat_text(val, bx + col_w - u(6), my + u(8), f_num, f_unit,
                              "rm", _IMG_INK2)
                yy += u(18) * max(len(rows_), 1) if rows_ else 0

    # ---------------- 出刀时刻分布
    if bosses:
        y += boss_h + gap
    card(y, hours_h)
    section(y, "出刀时刻分布", "按本档案全部出刀，5 点换日对齐")
    py0, py1 = y + u(44), y + u(104)
    vmax = max(hours) or 1
    grid(py0, py1, vmax, lambda v: f"{round(v):d}")
    hx0 = x0 + u(30)
    hslot = (x1 - hx0) / 24
    hbw = max(u(3), hslot * 0.6)
    for i, v in enumerate(hours):
        if v <= 0:
            continue
        bh = (py1 - py0) * v / vmax
        cx = hx0 + hslot * (i + 0.5)
        draw.rounded_rectangle((cx - hbw / 2, py1 - bh, cx + hbw / 2, py1),
                               radius=u(2), fill=_IMG_TEAL)
    for i in range(0, 24, 3):
        cx = hx0 + hslot * (i + 0.5)
        draw.text((cx, py1 + u(5)), f"{i}时", font=f_tiny, fill=_IMG_INK3, anchor="mt")

    # ---------------- 下班榜（最速 / 最慢 并排）
    y += hours_h + gap
    card(y, end_h)
    section(y, "下班榜", "每天出满 3 刀的完成时刻")
    tw = (inner - gap) / 2
    for ti, (rows_, key, label, head) in enumerate((
            (fastest, "best", "最早", "最速下班"),
            (slowest, "worst", "最晚", "最慢下班"))):
        tx0 = x0 + ti * (tw + gap)
        tx1 = tx0 + tw
        draw.text((tx0, y + u(43)), head, font=f_name, fill=_IMG_INK, anchor="lm")
        hy = y + u(55)
        draw.rectangle((tx0, hy, tx1, hy + u(19)), fill=_IMG_HEAD_BG)
        draw.text((tx0 + u(4), hy + u(9.5)), "成员", font=f_tiny, fill=_IMG_ACCENT_S, anchor="lm")
        draw.text((tx1 - u(72), hy + u(9.5)), "平均", font=f_tiny, fill=_IMG_ACCENT_S, anchor="rm")
        draw.text((tx1 - u(6), hy + u(9.5)), label, font=f_tiny, fill=_IMG_ACCENT_S, anchor="rm")
        if not rows_:
            draw.text((tx0 + u(4), hy + u(33)), "暂无数据（还没人出满 3 刀）",
                      font=f_tiny, fill=_IMG_INK3, anchor="lm")
            continue
        for i, m in enumerate(rows_[:10]):
            ry2 = hy + u(19) + i * u(23) + u(11.5)
            draw.text((tx0 + u(4), ry2), f"{i + 1}", font=f_tiny,
                      fill=_MEDAL_COLORS[i] if i < 3 else _IMG_INK3, anchor="lm")
            nm = _truncate(scratch, m["name"], f_small, tw - u(96))
            _draw_rich(draw, (tx0 + u(20), ry2), nm, f_small, _IMG_INK, "lm")
            draw.text((tx1 - u(72), ry2), _stat_clock(m["avg"]), font=f_num,
                      fill=_IMG_INK2, anchor="rm")
            draw.text((tx1 - u(6), ry2), _stat_clock(m[key]), font=f_num,
                      fill=_IMG_INK, anchor="rm")

    im = im.resize((W // S, H // S), Image.LANCZOS)
    bio = BytesIO()
    im.save(bio, format="PNG")
    return bio.getvalue()


def to_cq_image(data: bytes) -> str:
    return f"[CQ:image,file=base64://{base64.b64encode(data).decode()}]"


# ================================================================= 头像缓存
import time as _time
AVATAR_MAX_AGE = 7 * 86400  # 头像 7 天自动更新


def _avatar_stale(qqid: int) -> bool:
    path = os.path.join(_avatar_dir, f"{qqid}.jpg")
    if not os.path.isfile(path):
        return True
    return _time.time() - os.path.getmtime(path) > AVATAR_MAX_AGE


async def download_avatars(qqids: List[int]) -> None:
    """拉取缺失/过期头像（q1.qlogo.cn），失败静默。"""
    import aiohttp
    os.makedirs(_avatar_dir, exist_ok=True)
    todo = [q for q in set(qqids) if _avatar_stale(q)]
    if not todo:
        return

    async def one(session, qqid):
        path = os.path.join(_avatar_dir, f"{qqid}.jpg")
        try:
            async with session.get(f"http://q1.qlogo.cn/g?b=qq&nk={qqid}&s=1",
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    with open(path, "wb") as f:
                        f.write(await resp.read())
        except Exception:  # noqa: BLE001
            pass

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(one(session, q) for q in todo))


async def ensure_avatars(qqids: List[int], timeout: float = 5.0) -> None:
    """渲染前调用：尽量在超时内把缺失/过期头像补齐（失败不阻塞出图）。"""
    todo = [q for q in set(qqids) if _avatar_stale(q)]
    if not todo:
        return
    try:
        await asyncio.wait_for(download_avatars(todo), timeout)
    except asyncio.TimeoutError:
        pass


def missing_avatars_from(chips_used: set) -> List[int]:
    global _missing_avatars
    pending = list(_missing_avatars)
    _missing_avatars = set()
    return pending
