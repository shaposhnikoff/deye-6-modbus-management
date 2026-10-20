"""Render `dmon top` frames to PNG for the documentation.

Uses the same row builders, thresholds and colours as the curses UI, but paints them directly so
the result is deterministic (no terminal emulation artifacts).

    uv run --with pillow python tools/render_tui_png.py docs/assets/dmon-top.png \
        docs/assets/dmon-top-tou.png
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw, ImageFont

from deye_monitoring.cache import CacheSnapshot, read_snapshot
from deye_monitoring.tui import OK, Row, build_overview, build_tou, format_age

BG = (13, 17, 23)
FG = (201, 209, 217)
MUTED = (110, 118, 129)
CYAN = (57, 197, 207)
GREEN = (63, 185, 80)
YELLOW = (210, 153, 34)
RED = (248, 81, 73)
STATUS_COLOR = {OK: GREEN, "warn": YELLOW, "crit": RED}
FONT_PATHS = (
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)
FONT_SIZE = 17
PAD_X = 18
PAD_Y = 14
LINE = 26


def _font() -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, FONT_SIZE)
        except OSError:
            continue
    return ImageFont.load_default()


def _rows(snapshot: CacheSnapshot, page: str) -> list[Row]:
    return build_overview(snapshot.values) if page == "1" else build_tou(snapshot)


def render(snapshot: CacheSnapshot, page: str, columns: int = 96) -> Image.Image:
    """Paint one frame."""
    font = _font()
    char_width = int(font.getlength("M")) or FONT_SIZE // 2
    rows = _rows(snapshot, page)
    height_lines = len(rows) + 6
    width = char_width * columns + PAD_X * 2
    height = LINE * height_lines + PAD_Y * 2
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    header = (
        f"Deye SUN-6K  serial {snapshot.serial or '?'}  "
        f"{'online' if snapshot.online else 'OFFLINE'}  age {format_age(snapshot.age())}  "
        f"interval {snapshot.interval:g}s  page {page} (1/2)"
    )
    draw.text((PAD_X, PAD_Y), header, font=font, fill=CYAN)

    y = PAD_Y + LINE * 2
    for row in rows:
        if y > height - LINE * 3:
            break
        if row.section:
            if row.section == "SLOTS":
                y += LINE
                continue
            draw.text((PAD_X, y), f"[{row.section}]", font=font, fill=MUTED)
        else:
            draw.text((PAD_X, y), row.label, font=font, fill=STATUS_COLOR.get(row.status, FG))
            draw.text(
                (PAD_X + char_width * 20, y),
                row.text,
                font=font,
                fill=STATUS_COLOR.get(row.status, FG),
            )
        y += LINE

    footer = " q quit   p pause   + / - interval   1 / 2 page   r redraw "
    footer_y = height - PAD_Y - LINE
    draw.rectangle([0, footer_y, width, footer_y + LINE], fill=FG)
    draw.text((PAD_X, footer_y + 3), footer, font=font, fill=BG)
    return image


def main(argv: list[str]) -> int:
    """CLI entry point: writes the overview and TOU frames."""
    if len(argv) != 3:
        print(__doc__)
        return 2
    snapshot = read_snapshot()
    if snapshot is None:
        print("error: cache is missing; start deye-monitoring first")
        return 1
    render(snapshot, "1").save(argv[1])
    render(snapshot, "2").save(argv[2])
    print(f"wrote {argv[1]} and {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
