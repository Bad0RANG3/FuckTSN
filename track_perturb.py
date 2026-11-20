"""轨迹偏移与噪声模型。

用途：把库里那条"标准路线"变成每次都不完全一样的真实 GPS 采集轨迹。

分层建模（数值取自真实手机跑步 GPS 的典型误差）：
  1. 整体平移      ：每次站在跑道上的位置不同，±2.5 m
  2. 低频横向漂移  ：卫星几何变化，σ 0.5~1.8 m，周期 60~250 m
  3. 中频横向漂移  ：σ 0.2~0.6 m，周期 15~40 m
  4. 高频逐点抖动  ：σ 0.30~0.45 m（1 Hz 采样）
  5. 多径跳点      ：约 1% 概率，偏移 3~12 m，持续 1~3 点后回归
  6. 起跑点随机    ：闭合跑道随机起点 + 50% 反向跑
"""
from __future__ import annotations

import math
import random
from typing import List, Sequence, Tuple

M_PER_DEG_LAT = 111320.0


def _lon_scale(lat_deg: float) -> float:
    return M_PER_DEG_LAT * math.cos(math.radians(lat_deg))


def _to_xy(points: Sequence[Sequence[float]]) -> Tuple[List[float], List[float], float]:
    lat0 = sum(p[0] for p in points) / len(points)
    lon0 = sum(p[1] for p in points) / len(points)
    sx = _lon_scale(lat0)
    xs = [(p[1] - lon0) * sx for p in points]
    ys = [(p[0] - lat0) * M_PER_DEG_LAT for p in points]
    return xs, ys, lat0


def _from_xy(xs: Sequence[float], ys: Sequence[float], lat0: float, lon0: float) -> List[Tuple[float, float]]:
    sx = _lon_scale(lat0)
    return [(lat0 + y / M_PER_DEG_LAT, lon0 + x / sx) for x, y in zip(xs, ys)]


def _arc_lengths(xs: Sequence[float], ys: Sequence[float]) -> List[float]:
    acc = [0.0]
    for i in range(1, len(xs)):
        acc.append(acc[-1] + math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1]))
    return acc


def perturb_route(points: Sequence[Sequence[float]], rng: random.Random | None = None,
                  translate_m: float = 4.0, low_amp: float = 3.0, mid_amp: float = 1.0) -> List[Tuple[float, float]]:
    """对整条参考路线做低频形变（不改变长度量级），返回新的 (lat, lon) 序列。"""
    rng = rng or random.Random()
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 4:
        return pts

    xs, ys, lat0 = _to_xy(pts)
    lon0 = sum(p[1] for p in pts) / len(pts)

    # 1) 整体平移：模拟每次站位不同
    dx = rng.uniform(-translate_m, translate_m)
    dy = rng.uniform(-translate_m, translate_m)
    xs = [x + dx for x in xs]
    ys = [y + dy for y in ys]

    # 2) 沿弧长的低频/中频横向漂移
    s = _arc_lengths(xs, ys)
    total = s[-1] or 1.0
    waves = []
    for amp_range, period_range in ((low_amp, (60.0, 250.0)), (mid_amp, (15.0, 40.0))):
        amp = rng.uniform(amp_range * 0.3, amp_range)
        period = rng.uniform(*period_range)
        phase = rng.uniform(0, 2 * math.pi)
        waves.append((amp, 2 * math.pi / period, phase))

    out_x, out_y = [], []
    for i in range(len(xs)):
        # 法向量：垂直于局部行进方向
        j0, j1 = max(0, i - 1), min(len(xs) - 1, i + 1)
        tx, ty = xs[j1] - xs[j0], ys[j1] - ys[j0]
        norm = math.hypot(tx, ty) or 1.0
        nx, ny = -ty / norm, tx / norm
        offset = sum(amp * math.sin(k * s[i] + phase) for amp, k, phase in waves)
        out_x.append(xs[i] + nx * offset)
        out_y.append(ys[i] + ny * offset)

    return _from_xy(out_x, out_y, lat0, lon0)


def rotate_route(points: Sequence[Sequence[float]], rng: random.Random, max_deg: float = 1.2) -> List[Tuple[float, float]]:
    """绕质心做极小角度旋转，模拟跑道朝向判定误差。"""
    pts = [(float(p[0]), float(p[1])) for p in points]
    ang = math.radians(rng.uniform(-max_deg, max_deg))
    xs, ys, lat0 = _to_xy(pts)
    lon0 = sum(p[1] for p in pts) / len(pts)
    ca, sa = math.cos(ang), math.sin(ang)
    rx = [x * ca - y * sa for x, y in zip(xs, ys)]
    ry = [x * sa + y * ca for x, y in zip(xs, ys)]
    return _from_xy(rx, ry, lat0, lon0)


def reverse_route(points: Sequence[Sequence[float]], rng: random.Random, prob: float = 0.5) -> List[Tuple[float, float]]:
    pts = [(float(p[0]), float(p[1])) for p in points]
    return list(reversed(pts)) if rng.random() < prob else pts


def start_offset_route(points: Sequence[Sequence[float]], rng: random.Random) -> List[Tuple[float, float]]:
    """闭合路线随机起点（模拟从跑道不同位置起跑），非闭合路线原样返回。"""
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 10:
        return pts
    d = math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1])
    if d > 0.0004:  # 约 45 m，视为不闭合
        return pts
    cut = rng.randrange(len(pts) // 10, len(pts) - len(pts) // 10)
    return pts[cut:] + pts[:cut] + [pts[cut]]


def point_noise(lat: float, lon: float, rng: random.Random, sigma_m: float = 0.38) -> Tuple[float, float]:
    """单点高频抖动。"""
    dlat = rng.gauss(0, sigma_m) / M_PER_DEG_LAT
    dlon = rng.gauss(0, sigma_m) / _lon_scale(lat)
    return lat + dlat, lon + dlon


def build_variant(reference: Sequence[Sequence[float]], rng: random.Random | None = None) -> List[Tuple[float, float]]:
    """一步到位生成一条"同场地不同人不同次"的参考路线。"""
    rng = rng or random.Random()
    pts = reverse_route(reference, rng)
    pts = start_offset_route(pts, rng)
    pts = perturb_route(pts, rng)
    pts = rotate_route(pts, rng)
    return pts
