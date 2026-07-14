"""Auto-generate a 1280x720 YouTube thumbnail from a video title.

Dark premium style: black background, an accent glow, bold white title with
accent-colored highlights on numbers, an optional channel logo, no faces/people.
The accent color is read from THUMBNAIL_ACCENT_COLOR so it matches your brand.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("faceless_kit.thumbnail")

_FONTS = [
    os.path.join(os.path.dirname(__file__), "assets", "title-bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
WHITE = (245, 248, 255)
# Default accent (electric blue). Overridden per-render by THUMBNAIL_ACCENT_COLOR.
DEFAULT_ACCENT = (45, 180, 255)

_HL = re.compile(r"[0-9$%#]")  # words containing these get the accent color


def _accent_rgb() -> tuple[int, int, int]:
    """Parse THUMBNAIL_ACCENT_COLOR (#RRGGBB) into an RGB tuple; fall back to the
    default electric blue on anything unparseable."""
    raw = (os.getenv("THUMBNAIL_ACCENT_COLOR") or "").strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    if len(raw) == 6:
        try:
            return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            pass
    return DEFAULT_ACCENT


def _font_path() -> str:
    env = os.getenv("THUMBNAIL_FONT")
    if env and os.path.exists(env):
        return env
    for p in _FONTS:
        if os.path.exists(p):
            return p
    return _FONTS[-1]


def _wrap(draw, words, font, max_w):
    lines, cur = [], []
    for w in words:
        trial = " ".join(cur + [w])
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def generate(title: str, out_path: str, logo_path: str | None = None) -> str | None:
    try:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
    except Exception as exc:  # noqa: BLE001
        log.warning("Pillow not available, skipping thumbnail: %s", exc)
        return None

    W, H = 1280, 720
    fp = _font_path()
    BLUE = _accent_rgb()  # channel accent (THUMBNAIL_ACCENT_COLOR), default electric blue
    img = Image.new("RGB", (W, H), (5, 6, 10))

    # background glow + vignette
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-200, 120, 760, 840], fill=(12, 40, 78))
    gd.ellipse([820, -120, 1500, 560], fill=(8, 26, 55))
    img = ImageChops.add(img, glow.filter(ImageFilter.GaussianBlur(170)))
    vig = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vig).ellipse([-240, -240, W + 240, H + 240], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(190))
    img = Image.composite(img, Image.new("RGB", (W, H), (0, 0, 0)), vig)

    draw = ImageDraw.Draw(img)
    text = title.upper().strip()
    words = text.split()
    margin_x, max_w, max_h = 70, 1010, 470

    # fit: shrink until the wrapped title fits the box
    size = 150
    while size > 48:
        font = ImageFont.truetype(fp, size)
        lines = _wrap(draw, words, font, max_w)
        lh = (font.getbbox("Ag")[3] - font.getbbox("Ag")[1]) + size * 0.28
        if len(lines) * lh <= max_h and len(lines) <= 4:
            break
        size -= 4
    font = ImageFont.truetype(fp, size)
    lines = _wrap(draw, words, font, max_w)
    lh = (font.getbbox("Ag")[3] - font.getbbox("Ag")[1]) + size * 0.28

    # glow layer for the title
    glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gl = ImageDraw.Draw(glow_layer)
    y = 130
    positions = []
    for line in lines:
        x = margin_x
        for w in line:
            col = BLUE if _HL.search(w) else WHITE
            positions.append((x, y, w + " ", col))
            gl.text((x, y), w + " ", font=font, fill=(BLUE if col == BLUE else (20, 60, 110)) + (255,))
            x += draw.textlength(w + " ", font=font)
        y += lh
    glow_rgb = glow_layer.filter(ImageFilter.GaussianBlur(14)).convert("RGB")
    img = ImageChops.add(img, glow_rgb)
    img = ImageChops.add(img, glow_rgb)

    # sharp title (per-word color) + dark stroke for legibility
    draw = ImageDraw.Draw(img)
    for x, yy, w, col in positions:
        draw.text((x, yy), w, font=font, fill=col, stroke_width=2, stroke_fill=(0, 0, 0))

    # accent bar under the title
    bar_y = 130 + len(lines) * lh + 14
    draw.rounded_rectangle([margin_x, bar_y, margin_x + 360, bar_y + 12], radius=6, fill=BLUE)

    # logo bottom-right (black background keyed out via screen blend)
    if logo_path and os.path.exists(logo_path):
        try:
            lg = Image.open(logo_path).convert("RGB")
            lh2 = 165
            lw2 = int(lg.width * lh2 / lg.height)
            lg = lg.resize((lw2, lh2))
            lx, ly = W - lw2 - 50, H - lh2 - 36
            region = img.crop((lx, ly, lx + lw2, ly + lh2))
            img.paste(ImageChops.lighter(region, lg), (lx, ly))
        except Exception as exc:  # noqa: BLE001
            log.warning("Logo overlay on thumbnail failed: %s", exc)

    img.save(out_path, "JPEG", quality=90)
    log.info("Thumbnail saved: %s", out_path)
    return out_path
