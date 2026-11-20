"""轨迹自检：在提交之前用纯数学方式检查一遍"这条轨迹自己是否自洽"。

定位（很重要）：这是**客户端自查**，不是服务器判定。服务器只看
`sportRange`/`gpsDistance` 和 `stepNumbers`，并不会按 pointList 重算距离；
这里的检查只是为了在本地提前发现"明显不像人"的轨迹（时间倒流、速度与位移
对不上、累计步数断裂），避免把它们送到服务器上去踩规则。

因此严重程度分两档：
- error：内部不一致，说明生成逻辑有 bug，直接拦下；
- warning：形态可疑但真机样本里也出现过（真机 chord_deviation 就经常 >15%），
  只提示不拦。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from tsn.buckets import BucketAllocation
from tsn.geo import haversine_distance


@dataclass
class Issue:
    code: str
    message: str
    severity: str = 'error'
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    ok: bool
    issues: List[Issue] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == 'error']

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == 'warning']

    def summary(self) -> str:
        metrics = ', '.join(f'{k}={v}' for k, v in self.metrics.items())
        verdict = '通过' if self.ok else '未通过'
        return f'轨迹校验{verdict}：error={len(self.errors)} warning={len(self.warnings)} 指标={metrics}'

    def raise_if_failed(self) -> None:
        if not self.ok:
            detail = '; '.join(f'{i.code}: {i.message}' for i in self.errors)
            raise ValueError(f'轨迹校验未通过: {detail}')


class _Collector:
    def __init__(self) -> None:
        self.issues: List[Issue] = []

    def error(self, code: str, message: str, **context) -> None:
        self.issues.append(Issue(code, message, 'error', dict(context)))

    def warn(self, code: str, message: str, **context) -> None:
        self.issues.append(Issue(code, message, 'warning', dict(context)))


def validate_series(
    points: Sequence[Sequence[float]],
    timestamps_ms: Sequence[float],
    distances_m: Optional[Sequence[float]] = None,
    speeds_mps: Optional[Sequence[float]] = None,
    cumulative_m: Optional[Sequence[float]] = None,
    target_distance_m: Optional[float] = None,
    bucket_allocation: Optional[BucketAllocation] = None,
    total_steps: Optional[int] = None,
    summary_coords: Optional[Sequence[Sequence[float]]] = None,
    distance_eps_m: float = 0.5,
    speed_tolerance_ratio: float = 0.001,
    chord_warning_ratio: float = 0.15,
    initial_interval_s: float = 0.0,
) -> ValidationReport:
    """对一条"等间隔采样"的轨迹做一致性检查。"""
    out = _Collector()
    n = len(points)
    lengths = {
        'timestamps_ms': len(timestamps_ms),
        'distances_m': len(distances_m) if distances_m is not None else n,
        'speeds_mps': len(speeds_mps) if speeds_mps is not None else n,
        'cumulative_m': len(cumulative_m) if cumulative_m is not None else n,
    }
    if any(length != n for length in lengths.values()):
        out.error('length_mismatch', '各序列长度不一致', points=n, **lengths)
        return ValidationReport(ok=False, issues=out.issues, metrics={'points': n})

    if n == 0:
        out.error('empty_track', '轨迹为空')
        return ValidationReport(ok=False, issues=out.issues, metrics={'points': 0})

    for idx, point in enumerate(points):
        lon, lat = float(point[0]), float(point[1])
        if not all(math.isfinite(v) for v in (lon, lat)):
            out.error('non_finite_coord', '坐标包含 NaN/Inf', index=idx)

    for idx in range(1, n):
        if float(timestamps_ms[idx]) <= float(timestamps_ms[idx - 1]):
            out.error('timestamp_not_increasing', '时间戳未严格递增', index=idx)
            break

    # 预算距离含首段的位移（首段由 initial_interval_s 覆盖），几何弦长则是相邻点连线的合计
    budget_distance_m = sum(float(v) for v in distances_m) if distances_m is not None else 0.0
    chord_total = 0.0
    max_chord_ratio = 0.0
    max_chord_index = -1
    for idx in range(1, n):
        prev, cur = points[idx - 1], points[idx]
        chord = haversine_distance(prev[1], prev[0], cur[1], cur[0])
        chord_total += chord
        segment = float(distances_m[idx]) if distances_m is not None else chord
        if distances_m is not None and speeds_mps is not None:
            dt_s = (float(timestamps_ms[idx]) - float(timestamps_ms[idx - 1])) / 1000.0
            expected = float(speeds_mps[idx]) * dt_s
            if abs(segment - expected) > max(distance_eps_m, abs(expected) * speed_tolerance_ratio):
                out.error(
                    'speed_distance_mismatch', '分段位移与瞬时速度不自洽',
                    index=idx, segment_m=round(segment, 3), expected_m=round(expected, 3),
                )
        if segment < 0:
            out.error('negative_distance', '出现负位移', index=idx)
        if chord > 1e-06:
            ratio = abs(segment - chord) / chord
            if ratio > max_chord_ratio:
                max_chord_ratio, max_chord_index = ratio, idx

    if max_chord_index >= 0 and max_chord_ratio > chord_warning_ratio:
        out.warn(
            'chord_deviation', '分段位移与直线弦长偏差偏大（真机也会如此，仅提示）',
            index=max_chord_index, ratio=round(max_chord_ratio, 4),
        )

    if cumulative_m is not None:
        for idx in range(1, n):
            delta = float(cumulative_m[idx]) - float(cumulative_m[idx - 1])
            if delta < 0:
                out.error('cumulative_not_monotonic', '累计距离出现回退', index=idx)
                break
            expected = float(distances_m[idx]) if distances_m is not None else delta
            if abs(delta - expected) > max(distance_eps_m, abs(expected) * 0.01):
                out.error(
                    'cumulative_mismatch', '累计距离与分段位移不一致',
                    index=idx, delta_m=round(delta, 3), segment_m=round(expected, 3),
                )
                break

    first_ts = float(timestamps_ms[0])
    last_ts = float(timestamps_ms[-1])
    elapsed_s = (last_ts - first_ts) / 1000.0 + initial_interval_s
    if distances_m is None:
        budget_distance_m = chord_total

    if target_distance_m is not None and budget_distance_m > float(target_distance_m) + distance_eps_m:
        out.error(
            'target_distance_exceeded', '分段位移合计超过目标距离',
            budget_m=round(budget_distance_m, 3), target_m=round(float(target_distance_m), 3),
        )

    if summary_coords is not None and len(summary_coords) == 2:
        start_pt, end_pt = summary_coords[0], summary_coords[1]
        start_gap = haversine_distance(start_pt[1], start_pt[0], points[0][1], points[0][0])
        end_gap = haversine_distance(end_pt[1], end_pt[0], points[-1][1], points[-1][0])
        if start_gap > 1.0:
            out.error('summary_start_mismatch', '首点与上报起点不一致', gap_m=round(start_gap, 3))
        if end_gap > 1.0:
            out.error('summary_end_mismatch', '末点与上报终点不一致', gap_m=round(end_gap, 3))

    if bucket_allocation is not None and total_steps is not None:
        bucket_total = int(bucket_allocation.total_steps)
        if bucket_total != int(total_steps):
            out.error(
                'step_bucket_sum_mismatch', '分桶步数合计与总步数不一致',
                bucket_total=bucket_total, total_steps=int(total_steps),
            )

    metrics = {
        'points': n,
        'budget_distance_m': round(budget_distance_m, 9),
        'chord_distance_m': round(chord_total, 9),
        'elapsed_s': round(elapsed_s, 9),
        'avg_speed_mps': round(budget_distance_m / elapsed_s, 9) if elapsed_s > 0 else 0.0,
    }
    return ValidationReport(ok=not any(i.severity == 'error' for i in out.issues), issues=out.issues, metrics=metrics)


def validate_track(
    track,
    bucket_allocation: Optional[BucketAllocation] = None,
    total_steps: Optional[int] = None,
    summary_coords: Optional[Sequence[Sequence[float]]] = None,
    distance_eps_m: float = 0.5,
) -> ValidationReport:
    """校验一个 `tsn_track_gen.ResampledTrack`（鸭子类型，避免循环 import）。"""
    samples = list(track.samples)
    points = [sample.geo() for sample in samples]
    timestamps = [float(sample.t_ms) for sample in samples]
    distances = [float(sample.distance_m) for sample in samples]
    speeds = [float(sample.speed_mps) for sample in samples]
    cumulative = [float(sample.cumulative_m) for sample in samples]
    initial_interval_s = float(samples[0].dt_ms) / 1000.0 if samples else 0.0
    return validate_series(
        points=points,
        timestamps_ms=timestamps,
        distances_m=distances,
        speeds_mps=speeds,
        cumulative_m=cumulative,
        target_distance_m=getattr(track, 'target_distance_m', None),
        bucket_allocation=bucket_allocation,
        total_steps=total_steps,
        summary_coords=summary_coords,
        distance_eps_m=distance_eps_m,
        initial_interval_s=initial_interval_s,
    )


__all__ = ['Issue', 'ValidationReport', 'validate_series', 'validate_track']
