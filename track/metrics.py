from __future__ import annotations
import math
import random
from dataclasses import dataclass
from typing import List, Optional, Sequence
from tsn.geo import haversine_distance
# 真实人类慢跑生理参数（2.1 km / 6'30"-7'30" / km）
# 步幅在 2.40 m/s（6'56"/km）附近约 0.86 m，随速度线性增长；步频由 60*v/stride 推出并夹在 150-190 spm
STRIDE_BASE_M = 0.86
STRIDE_SLOPE_S = 0.22
STRIDE_REF_SPEED_MPS = 2.40
STRIDE_MIN_M = 0.62
STRIDE_MAX_M = 1.30
CADENCE_MIN_SPM = 150.0
CADENCE_MAX_SPM = 190.0

@dataclass
class TrackMetrics:
    distance_m: float
    elapsed_s: float
    point_count: int
    avg_speed_mps: float
    pace_s_per_km: float
    pace_str: str

    def as_dict(self) -> dict:
        return {'distance_m': self.distance_m, 'elapsed_s': self.elapsed_s, 'point_count': self.point_count, 'avg_speed_mps': self.avg_speed_mps, 'pace_s_per_km': self.pace_s_per_km, 'pace': self.pace_str}

def cumulative_distances(points: Sequence[Sequence[float]]) -> List[float]:
    cum = [0.0]
    for i in range(1, len(points)):
        prev = points[i - 1]
        cur = points[i]
        cum.append(cum[-1] + haversine_distance(prev[1], prev[0], cur[1], cur[0]))
    return cum

def distance_between(a: Sequence[float], b: Sequence[float]) -> float:
    return haversine_distance(a[1], a[0], b[1], b[0])

def segment_speeds(points: Sequence[Sequence[float]], timestamps_ms: Sequence[float]) -> List[float]:
    speeds: List[float] = []
    for i in range(1, len(points)):
        dt = (timestamps_ms[i] - timestamps_ms[i - 1]) / 1000.0
        if dt <= 0:
            speeds.append(0.0)
            continue
        speeds.append(distance_between(points[i - 1], points[i]) / dt)
    return speeds

def pace_seconds_per_km(distance_m: float, elapsed_s: float) -> float:
    if distance_m <= 0 or elapsed_s <= 0:
        return 0.0
    return elapsed_s / (distance_m / 1000.0)

def format_pace(seconds_per_km: float) -> str:
    if seconds_per_km <= 0 or math.isnan(seconds_per_km) or math.isinf(seconds_per_km):
        return '0\'0"'
    total = int(round(seconds_per_km))
    minutes = total // 60
    seconds = total % 60
    return f'''{minutes}'{seconds}"'''

def compute_metrics(points: Sequence[Sequence[float]], timestamps_ms: Sequence[float]) -> TrackMetrics:
    distance = cumulative_distances(points)[-1] if len(points) > 1 else 0.0
    elapsed = (timestamps_ms[-1] - timestamps_ms[0]) / 1000.0 if len(timestamps_ms) > 1 else 0.0
    avg_speed = distance / elapsed if elapsed > 0 else 0.0
    pace = pace_seconds_per_km(distance, elapsed)
    return TrackMetrics(distance_m=distance, elapsed_s=elapsed, point_count=len(points), avg_speed_mps=avg_speed, pace_s_per_km=pace, pace_str=format_pace(pace))

def stride_for_speed(speed_mps: float, rng: Optional[random.Random]=None, jitter: float=0.0) -> float:
    speed = max(0.0, float(speed_mps))
    stride = STRIDE_BASE_M + STRIDE_SLOPE_S * (speed - STRIDE_REF_SPEED_MPS)
    if jitter > 0 and rng is not None:
        stride *= 1.0 + rng.uniform(-jitter, jitter)
    return min(STRIDE_MAX_M, max(STRIDE_MIN_M, stride))

def steps_for_interval(distance_m: float, speed_mps: float, rng: Optional[random.Random]=None, jitter: float=0.0) -> float:
    stride = stride_for_cadence(speed_mps) or stride_for_speed(speed_mps, rng=rng, jitter=jitter)
    if jitter > 0 and rng is not None:
        stride *= 1.0 + rng.uniform(-jitter, jitter)
    if stride <= 0:
        return 0.0
    return max(0.0, distance_m) / stride

def cadence_spm(speed_mps: float) -> float:
    speed = max(0.0, float(speed_mps))
    stride = stride_for_speed(speed)
    if stride <= 0 or speed <= 0:
        return 0.0
    raw = speed / stride * 60.0
    return min(CADENCE_MAX_SPM, max(CADENCE_MIN_SPM, raw))


def stride_for_cadence(speed_mps: float) -> float:
    """夹住步频后再回算步幅，保证 (速度, 步幅, 步频) 三者自洽。"""
    cad = cadence_spm(speed_mps)
    if cad <= 0:
        return 0.0
    return speed_mps / cad * 60.0
