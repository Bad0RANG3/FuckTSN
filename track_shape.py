# -*- coding: utf-8 -*-
"""在围栏内合成形状各异的跑步路径（整条 2.1–2.4 km，不是单圈）。

三种原型，每次随机选一种，并随机化参数：

1. ``ellipse``   椭圆/跑道形套圈：中心、长短轴比例、旋转角、每圈车道抖动都随机。
2. ``outback``   折返：从围栏一侧跑到另一侧再折回，路线是平滑随机折线。
3. ``eight``     8 字形套圈：两个相交的圆，比例与夹角随机。

三者都是「像人跑出来的」：曲率连续、圈间有 1–4 m 的车道漂移、不会出现自交尖角。
"""
from __future__ import annotations

import math
import random
from typing import List, Sequence, Tuple

M_PER_DEG_LAT = 111320.0


def _to_xy(lat: float, lon: float, lat0: float) -> Tuple[float, float]:
    return (lon * M_PER_DEG_LAT * math.cos(math.radians(lat0)), lat * M_PER_DEG_LAT)


def _ring_xy(ring: Sequence[Sequence[float]]) -> Tuple[List[Tuple[float, float]], float]:
    lat0 = sum(v[1] for v in ring) / len(ring)
    out = [((v[0]) * M_PER_DEG_LAT * math.cos(math.radians(lat0)), v[1] * M_PER_DEG_LAT) for v in ring]
    return out, lat0


def _to_ll(x: float, y: float, lat0: float) -> Tuple[float, float]:
    return (y / M_PER_DEG_LAT, x / (M_PER_DEG_LAT * math.cos(math.radians(lat0))))


def _bbox(pts: Sequence[Tuple[float, float]]):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


def _length(pts: Sequence[Tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def _ellipse_lap(cx, cy, rx, ry, rot, n=180, jitter=0.0, rng=None) -> List[Tuple[float, float]]:
    ca, sa = math.cos(rot), math.sin(rot)
    out = []
    for i in range(n):
        t = 2 * math.pi * i / n
        k = 1.0 + (rng.uniform(-jitter, jitter) if (jitter and rng) else 0.0)
        x = rx * math.cos(t) * k
        y = ry * math.sin(t) * k
        out.append((cx + x * ca - y * sa, cy + x * sa + y * ca))
    out.append(out[0])
    return out


def _smooth(points: Sequence[Tuple[float, float]], rounds: int = 3) -> List[Tuple[float, float]]:
    pts = list(points)
    for _ in range(rounds):
        nxt = [pts[0]]
        for a, b in zip(pts, pts[1:]):
            nxt.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        pts = nxt
    return pts


def _resample(pts: Sequence[Tuple[float, float]], step: float) -> List[Tuple[float, float]]:
    total = _length(pts)
    if total <= 0:
        return list(pts)
    n = max(8, int(total / step))
    want = total / n
    out, acc = [pts[0]], 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.dist(a, b)
        if seg <= 0:
            continue
        pos = 0.0
        while acc + (seg - pos) >= want:
            pos += want - acc
            acc = 0.0
            t = pos / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        acc += seg - pos
    return out


def _archetype_ellipse(ring_xy, rng, target_m):
    x0, x1, y0, y1 = _bbox(ring_xy)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = (x1 - x0) / 2 - 6.0, (y1 - y0) / 2 - 6.0
    aspect = rng.uniform(0.55, 0.98)
    rot = rng.uniform(-0.35, 0.35)
    rx = hw * rng.uniform(0.72, 0.97)
    ry = hh * rng.uniform(0.72, 0.97) * aspect
    lap = _ellipse_lap(cx, cy, rx, ry, rot, n=200)
    lap_len = _length(lap)
    if lap_len < 120:
        return None
    laps = max(2, int(round(target_m / lap_len)))
    pts = []
    for i in range(laps):
        k = 1.0 + rng.uniform(-0.012, 0.012)          # 车道漂移
        dx, dy = rng.uniform(-2.5, 2.5), rng.uniform(-2.5, 2.5)
        seg = _ellipse_lap(cx + dx, cy + dy, rx * k, ry * k, rot, n=90)
        pts.extend(seg if i % 2 == 0 else seg[::-1])
    return pts


def _archetype_outback(ring_xy, rng, target_m):
    x0, x1, y0, y1 = _bbox(ring_xy)
    a = (rng.uniform(x0 + 12, x1 - 12), rng.uniform(y0 + 12, y1 - 12))
    b = (rng.uniform(x0 + 12, x1 - 12), rng.uniform(y0 + 12, y1 - 12))
    if math.dist(a, b) < (target_m / 2) * 0.45:
        b = (x1 - a[0] if x1 - a[0] > x0 else x1 - 5, a[1] + rng.uniform(-20, 20))
    ctrl = [a]
    for k in range(1, 4):
        t = k / 4
        jx = rng.uniform(-12, 12)
        jy = rng.uniform(-12, 12)
        ctrl.append((a[0] + (b[0] - a[0]) * t + jx, a[1] + (b[1] - a[1]) * t + jy))
    ctrl.append(b)
    line = _resample(_smooth(ctrl, 4), 4.0)
    reps = max(1, int(round(target_m / (2 * _length(line)))))
    pts = []
    for i in range(reps):
        off = (rng.uniform(-3, 3), rng.uniform(-3, 3))
        fwd = [(x + off[0], y + off[1]) for x, y in line]
        pts.extend(fwd if i % 2 == 0 else fwd[::-1])
        pts.extend((fwd[::-1] if i % 2 == 0 else fwd))
    return pts


def _archetype_eight(ring_xy, rng, target_m):
    x0, x1, y0, y1 = _bbox(ring_xy)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = min((x1 - x0), (y1 - y0)) * rng.uniform(0.18, 0.26)
    sep = r * rng.uniform(1.0, 1.6)
    ang = rng.uniform(-0.6, 0.6)
    ca, sa = math.cos(ang), math.sin(ang)
    pts = []
    for i in range(160):
        t = 2 * math.pi * i / 160
        x = r * math.cos(t) + sep * math.sin(t if False else 0)  # 占位，下面覆盖
        x = r * math.cos(t)
        y = r * math.sin(2 * t) / 1.0
        px = x + (sep if t < math.pi else -sep)
        pts.append((cx + px * ca - y * sa, cy + px * sa + y * ca))
    lap_len = _length(pts)
    if lap_len < 100:
        return None
    reps = max(2, int(round(target_m / lap_len)))
    out = []
    for i in range(reps):
        out.extend(pts if i % 2 == 0 else pts[::-1])
    return out


ARCHETYPES = ('ellipse', 'outback', 'eight')


def _clamp_into_ring(points: List[List[float]], ring: Sequence[Sequence[float]],
                     inset_m: float = 3.5) -> List[List[float]]:
    """把落到围栏外的点吸附到内缩边界上（在「米」坐标系里做，不能拿度数当米用）。"""
    try:
        from shapely.geometry import Point, Polygon
        from shapely.ops import nearest_points
    except Exception:
        return points
    ring_xy, lat0 = _ring_xy(ring)
    poly = Polygon(ring_xy)
    if not poly.is_valid:
        poly = poly.buffer(0)
    inner = poly.buffer(-inset_m)
    if inner.is_empty or inner.geom_type != 'Polygon':
        inner = poly
    cx, cy = inner.centroid.x, inner.centroid.y
    out = []
    for lon, lat in points:
        px, py = _to_xy(lat, lon, lat0)
        pt = Point(px, py)
        if inner.contains(pt):
            out.append([lon, lat])
            continue
        nx, ny = nearest_points(inner, pt)[0].x, nearest_points(inner, pt)[0].y
        dx, dy = cx - nx, cy - ny
        d = math.hypot(dx, dy) or 1.0
        ll = _to_ll(nx + dx / d * 0.8, ny + dy / d * 0.8, lat0)
        out.append([round(ll[1], 7), round(ll[0], 7)])
    return out


def _extent(points: Sequence[Sequence[float]]) -> float:
    lats = [p[1] for p in points]
    lons = [p[0] for p in points]
    return math.dist((min(lats) * M_PER_DEG_LAT, min(lons) * M_PER_DEG_LAT * math.cos(math.radians(lats[0]))),
                     (max(lats) * M_PER_DEG_LAT, max(lons) * M_PER_DEG_LAT * math.cos(math.radians(lats[0]))))


def synthesize_path(ring: Sequence[Sequence[float]], rng: random.Random, target_m: float = 2200.0,
                    kind: str | None = None) -> List[List[float]]:
    """返回 [[lon, lat], ...] 的整条跑步路径（已保证落在围栏内、跨度像真跑）。"""
    ring_xy, lat0 = _ring_xy(ring)
    first = kind or rng.choice(ARCHETYPES)
    order = [first] + [k for k in ARCHETYPES if k != first]
    for kind_try in order:
        for _ in range(18):
            if kind_try == 'ellipse':
                pts = _archetype_ellipse(ring_xy, rng, target_m)
            elif kind_try == 'outback':
                pts = _archetype_outback(ring_xy, rng, target_m)
            else:
                pts = _archetype_eight(ring_xy, rng, target_m)
            if not pts:
                continue
            pts = _resample(pts, 4.0)
            if len(pts) < 40:
                continue
            acc, cut = 0.0, len(pts) - 1
            for i, (a, b) in enumerate(zip(pts, pts[1:])):
                acc += math.dist(a, b)
                if acc >= target_m:
                    cut = i + 1
                    break
            pts = pts[:cut + 1]
            if _length(pts) < target_m * 0.94:
                continue
            ll = [[round(_to_ll(x, y, lat0)[1], 7), round(_to_ll(x, y, lat0)[0], 7)] for x, y in pts]
            out = _clamp_into_ring(ll, ring, inset_m=3.5)
            if len(out) < 40 or _extent(out) < 140.0:
                continue
            if out[0] == out[-1]:
                out = out[:-1] + [[round(out[-1][0] + 1e-6, 7), out[-1][1]]]
            return out
    # 兜底：内缩椭圆套圈（一定在内缩区内）
    x0, x1, y0, y1 = _bbox(ring_xy)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = min(x1 - x0, y1 - y0) / 2 - 10.0
    lap = _ellipse_lap(cx, cy, r, r * 0.92, 0.0, n=200)
    reps = max(2, int(round(target_m / _length(lap))))
    pts = lap * reps
    ll = [[round(_to_ll(x, y, lat0)[1], 7), round(_to_ll(x, y, lat0)[0], 7)] for x, y in pts]
    return _clamp_into_ring(ll, ring, inset_m=3.5)
