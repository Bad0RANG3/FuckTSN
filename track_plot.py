"""把参考路线与生成的变体画成一张俯视图（PNG），用于目视检查偏差是否自然。"""
from __future__ import annotations

import math
from typing import Sequence, Tuple

from PIL import Image, ImageDraw

M_PER_DEG_LAT = 111320.0

COLORS = [(255, 92, 92), (255, 190, 60), (80, 180, 255), (200, 120, 255), (110, 220, 170)]


def draw_map(ref: Sequence[Tuple[float, float]], runs: Sequence[Sequence[Tuple[float, float]]],
             fence: Sequence[Sequence[float]] | None = None, path: str = 'map.png',
             width: int = 1000, grid_m: float = 50.0) -> str:
    all_pts = list(ref) + [p for r in runs for p in r]
    lats = [p[0] for p in all_pts]
    lons = [p[1] for p in all_pts]
    pad = 0.0001
    minlat, maxlat = min(lats) - pad, max(lats) + pad
    minlon, maxlon = min(lons) - pad, max(lons) + pad
    scale = width / (maxlon - minlon)
    W = int((maxlon - minlon) * scale)
    H = int((maxlat - minlat) * scale)

    def to_px(p):
        return ((p[1] - minlon) * scale, H - (p[0] - minlat) * scale)

    img = Image.new('RGB', (W, H), (18, 22, 28))
    d = ImageDraw.Draw(img)
    step_lat = grid_m / M_PER_DEG_LAT
    step_lon = grid_m / (M_PER_DEG_LAT * math.cos(math.radians(lats[0])))
    y = minlat
    while y < maxlat:
        d.line([to_px((y, minlon)), to_px((y, maxlon))], fill=(30, 36, 45))
        y += step_lat
    x = minlon
    while x < maxlon:
        d.line([to_px((minlat, x)), to_px((maxlat, x))], fill=(30, 36, 45))
        x += step_lon
    if fence:
        d.line([to_px(p) for p in fence], fill=(80, 200, 120), width=2)
    d.line([to_px(p) for p in ref], fill=(140, 140, 150), width=4)
    for i, r in enumerate(runs):
        d.line([to_px(p) for p in r], fill=COLORS[i % len(COLORS)], width=2)
        d.ellipse([to_px(r[0])[0] - 4, to_px(r[0])[1] - 4, to_px(r[0])[0] + 4, to_px(r[0])[1] + 4],
                  outline=COLORS[i % len(COLORS)], width=2)
    if fence:
        img.save(path)
    else:
        img.save(path)
    return path
