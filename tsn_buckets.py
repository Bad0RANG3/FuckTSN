"""步数分桶：把"某段时间走了多少步"折算成按 60 秒对齐的整数步数数组。

为什么需要它（逆向依据）：提交记录里的 `stepNumbers` 是服务器直接展示、并且
按"每分钟步频"做合理性判断的字段。真机（v3.6.814）抓到的样本里，各段步数
通常落在 100~200 之间，最后一段允许很小（收尾不足一分钟）。所以我们把整条
轨迹的步数按时间切进 60 秒桶，再做"最大余数法"取整，保证：
  1. 各桶之和恒等于总步数（不会凭空多/少一步）；
  2. 每个桶的步数是整数，可直接 JSON 序列化；
  3. 桶的数量由最后一条区间的结束时间决定，与 App 的分钟对齐一致。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

DEFAULT_BUCKET_SECONDS = 60.0


@dataclass
class StepInterval:
    """一段等速区间：从 start_offset_s 起持续 duration_s 秒，走了 steps 步。"""

    start_offset_s: float
    duration_s: float
    steps: float

    def __post_init__(self) -> None:
        for name, value in (
            ('start_offset_s', self.start_offset_s),
            ('duration_s', self.duration_s),
            ('steps', self.steps),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{name} 必须是有限数值')
        if self.start_offset_s < 0:
            raise ValueError('start_offset_s 不能为负')
        if self.duration_s < 0:
            raise ValueError('duration_s 不能为负')
        if self.steps < 0:
            raise ValueError('steps 不能为负')


@dataclass
class BucketAllocation:
    buckets: List[int] = field(default_factory=list)
    total_steps: int = 0
    estimated: bool = False
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS

    @property
    def bucket_count(self) -> int:
        return len(self.buckets)

    def as_step_numbers(self) -> List[int]:
        """提交时直接放进 stepNumbers 的数组。"""
        return list(self.buckets)


def bucket_index(offset_s: float, bucket_seconds: float = DEFAULT_BUCKET_SECONDS) -> int:
    return int(offset_s // bucket_seconds)


def largest_remainder(values: Sequence[float], target_total: int) -> List[int]:
    """最大余数法取整：先取下界，再把差额按小数部分从大到小逐一分给各桶。"""
    floors = [int(v) for v in values]
    remainder = target_total - sum(floors)
    if remainder == 0:
        return floors
    if remainder > 0:
        order = sorted(range(len(values)), key=lambda i: values[i] - floors[i], reverse=True)
        for i in order[:remainder]:
            floors[i] += 1
        return floors
    order = sorted(range(len(values)), key=lambda i: values[i] - floors[i])
    need = -remainder
    for i in order:
        if need == 0:
            break
        if floors[i] > 0:
            floors[i] -= 1
            need -= 1
    return floors


def allocate_steps(
    intervals: Sequence[StepInterval],
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
    estimated: bool = False,
    total_steps: Optional[int] = None,
) -> BucketAllocation:
    """把区间步数按时间重叠比例摊到各桶，再取整成整数数组。"""
    if bucket_seconds <= 0:
        raise ValueError('bucket_seconds 必须为正')
    if not intervals:
        return BucketAllocation(buckets=[], total_steps=0, estimated=estimated, bucket_seconds=bucket_seconds)

    bucket_count = 1
    for interval in intervals:
        end_offset = interval.start_offset_s + interval.duration_s
        if interval.duration_s > 0:
            touched = max(1, int(math.ceil(end_offset / bucket_seconds)))
        else:
            touched = bucket_index(interval.start_offset_s, bucket_seconds) + 1
        bucket_count = max(bucket_count, touched)

    acc = [0.0] * bucket_count
    for interval in intervals:
        if interval.duration_s <= 0 or interval.steps <= 0:
            # 零长度区间：整段计入其起点所在的桶
            if interval.steps > 0:
                acc[bucket_index(interval.start_offset_s, bucket_seconds)] += interval.steps
            continue
        cursor = max(0.0, interval.start_offset_s)
        left = interval.duration_s
        while left > 1e-09:
            idx = bucket_index(cursor, bucket_seconds)
            if idx >= bucket_count:
                idx = bucket_count - 1
            boundary = (idx + 1) * bucket_seconds
            segment = min(left, boundary - cursor)
            if segment <= 0:
                segment = left
            acc[idx] += interval.steps * (segment / interval.duration_s)
            cursor += segment
            left -= segment

    expected_total = int(round(sum(interval.steps for interval in intervals)))
    if total_steps is not None:
        if abs(int(total_steps) - expected_total) > 1:
            raise ValueError(
                f'total_steps({total_steps}) 与区间步数合计({expected_total}) 不一致；分桶不会凭空产生或丢弃步数'
            )
        expected_total = int(total_steps)

    buckets = largest_remainder(acc, expected_total)
    return BucketAllocation(
        buckets=buckets,
        total_steps=sum(buckets),
        estimated=estimated,
        bucket_seconds=bucket_seconds,
    )


def intervals_from_samples(samples: Sequence[Sequence[float]]) -> List[StepInterval]:
    """[(start_offset_s, duration_s, steps), ...] → StepInterval 列表。"""
    return [StepInterval(start_offset_s=s, duration_s=d, steps=n) for s, d, n in samples]


__all__ = [
    'DEFAULT_BUCKET_SECONDS', 'StepInterval', 'BucketAllocation',
    'bucket_index', 'largest_remainder', 'allocate_steps', 'intervals_from_samples',
]
