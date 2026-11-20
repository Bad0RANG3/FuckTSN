"""轨迹清洗：剔除重复点与几何尖刺。

为什么需要（逆向依据）：真机 GPS 采样在静止时会出现"原地抖动"，直接喂给
重采样会把 0 长度的段算进链长；而"尖刺"（一个点突然跳到几百米外再跳回来）
会让逐秒速度出现 20 m/s+ 的毛刺，落进服务器的速度上限判断里。
真机样本里 s 的峰值在 5~10 m/s，所以我们只要能去掉离群点即可。

清洗顺序是固定的：先合并重复（距离 < min_segment_m），再迭代去尖刺，
因为重复点会让尖刺判定里的 d_prev 变成 0，把真正的尖刺漏掉。

注意：服务器不校验点位链长与上报距离的等价性（合格样本里链长比上报值高
37% 也照样合格），所以这里的清洗只为"让轨迹看起来像人"服务，不会影响判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from tsn_geo import CoordinateSystem, WGS84, haversine_distance, normalize_points
from tsn_log import logger


class InsufficientTrackError(ValueError):
    """清洗后点数不足。"""


@dataclass
class CleanOptions:
    min_segment_m: float = 0.5          # 小于该间距视为原地抖动
    spike_ratio: float = 2.5            # 往返距离 > 直达距离 * ratio 视为尖刺
    spike_min_segment_m: float = 3.0    # 尖刺两侧至少要这么长，避免误杀拐点
    max_spike_passes: int = 8
    min_points: int = 2


@dataclass
class RemovedPoint:
    index: int
    lon: float
    lat: float
    reason: str


@dataclass
class CleanResult:
    points: List[List[float]]
    removed: List[RemovedPoint] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    crs: CoordinateSystem = WGS84

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    def removed_reason_counts(self) -> dict:
        counts: dict = {}
        for item in self.removed:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts


def _merge_duplicates(points: Sequence[Sequence[float]], min_segment_m: float) -> Tuple[List[List[float]], List[RemovedPoint]]:
    kept: List[List[float]] = []
    dropped: List[RemovedPoint] = []
    for point in points:
        lon, lat = float(point[0]), float(point[1])
        if kept and haversine_distance(kept[-1][1], kept[-1][0], lat, lon) < min_segment_m:
            dropped.append(RemovedPoint(len(kept), lon, lat, 'duplicate_or_too_close'))
            continue
        kept.append([lon, lat])
    return kept, dropped


def _remove_spikes(
    points: Sequence[Sequence[float]],
    options: CleanOptions,
) -> Tuple[List[List[float]], List[RemovedPoint]]:
    current = [[float(p[0]), float(p[1])] for p in points]
    dropped: List[RemovedPoint] = []
    for _ in range(max(1, options.max_spike_passes)):
        if len(current) < 3:
            break
        removed_this_pass = False
        i = 1
        while i < len(current) - 1:
            prev, mid, nxt = current[i - 1], current[i], current[i + 1]
            d_prev = haversine_distance(prev[1], prev[0], mid[1], mid[0])
            d_next = haversine_distance(mid[1], mid[0], nxt[1], nxt[0])
            d_direct = haversine_distance(prev[1], prev[0], nxt[1], nxt[0])
            if (
                d_prev >= options.spike_min_segment_m
                and d_next >= options.spike_min_segment_m
                and d_prev + d_next > options.spike_ratio * max(d_direct, 1e-06)
            ):
                dropped.append(RemovedPoint(i, mid[0], mid[1], 'geometric_spike'))
                current.pop(i)
                removed_this_pass = True
                continue
            i += 1
        if not removed_this_pass:
            break
    return current, dropped


def clean_track(
    points: Sequence[Sequence[float]],
    options: Optional[CleanOptions] = None,
    crs: CoordinateSystem = WGS84,
    strict: bool = True,
) -> CleanResult:
    """清洗轨迹点（入参 [[lon, lat], ...]）。"""
    options = options or CleanOptions()
    normalized = normalize_points(points)
    merged, dropped_dup = _merge_duplicates(normalized, options.min_segment_m)
    cleaned, dropped_spike = _remove_spikes(merged, options)

    if len(cleaned) < options.min_points:
        message = f'清洗后仅剩 {len(cleaned)} 点，少于下限 {options.min_points}'
        if strict:
            raise InsufficientTrackError(message)
        logger.warning(message)
        return CleanResult(points=cleaned, removed=dropped_dup + dropped_spike, notes=[message], crs=crs)

    logger.info(f'轨迹清洗：原始 {len(normalized)} 点 -> {len(cleaned)} 点，剔除 {len(dropped_dup) + len(dropped_spike)} 点')
    return CleanResult(points=cleaned, removed=dropped_dup + dropped_spike, crs=crs)


__all__ = [
    'InsufficientTrackError', 'CleanOptions', 'RemovedPoint', 'CleanResult', 'clean_track',
]
