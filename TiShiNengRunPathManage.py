from __future__ import annotations
import random
from typing import List, Optional, Sequence, Tuple
from tsn_log import logger
from tsn_buckets import DEFAULT_BUCKET_SECONDS, BucketAllocation, StepInterval, allocate_steps
from tsn_track_filter import CleanOptions, InsufficientTrackError, clean_track
from tsn_geo import EARTH_RADIUS_KM, EARTH_RADIUS_M, METERS_PER_KM, LocalProjection, haversine_distance, normalize_points, point_list_distance
from track_metrics import cadence_spm, compute_metrics, cumulative_distances, format_pace, pace_seconds_per_km, stride_for_speed, steps_for_interval
from track_resample import PointRisk, ResampleOptions, ResampledTrack, TrackGenerationError, TrackSample, resample_track
from track_perturb import point_noise
from tsn_track_audit import ValidationReport, validate_track
__all__ = ['EARTH_RADIUS_KM', 'EARTH_RADIUS_M', 'METERS_PER_KM', 'TsnRunPolyline', 'genTiShiNengRunPathRepeat', 'generate_run_track', 'to_legacy_points', 'haversine_distance', 'getPointListDistance', 'LocalProjection', 'ResampleOptions', 'ResampledTrack', 'TrackGenerationError', 'InsufficientTrackError']

def getPointListDistance(pointList: Sequence[Sequence[float]]) -> float:
    return point_list_distance(pointList)

class TsnRunPolyline:

    def __init__(self, points: Sequence[Sequence[float]]):
        self.points = normalize_points(points)
        self.distances = [haversine_distance(self.points[i][1], self.points[i][0], self.points[i + 1][1], self.points[i + 1][0]) for i in range(len(self.points) - 1)]
        self.total_length = sum(self.distances)

    def haversine_distance(self, point1: Sequence[float], point2: Sequence[float]) -> float:
        return haversine_distance(point1[1], point1[0], point2[1], point2[0])

    def interpolate_point(self, point1: Sequence[float], point2: Sequence[float], ratio: float) -> Tuple[float, float]:
        projection = LocalProjection(point1[0], point1[1])
        return projection.interpolate(point1[0], point1[1], point2[0], point2[1], ratio)

    def simulate_motion(self, avg_speed: float, distance: float):
        if avg_speed <= 0:
            raise TrackGenerationError(f'平均速度必须为正: {avg_speed}')
        plan_use_time = distance / avg_speed
        track = resample_track(points=self.points, need_distance_m=distance, start_timestamp_ms=0.0, plan_use_time_s=plan_use_time, options=ResampleOptions(speed_jitter=0.08))
        sampled = [{'lat': s.lat, 'lon': s.lon, 'millisecond': s.dt_ms, 'speed': s.speed_mps, 'distance': s.distance_m} for s in track.samples]
        logger.info(f'traveled_distance:{track.total_distance_m}')
        return (sampled, track.total_distance_m)

def to_legacy_points(track: ResampledTrack, isPublic: bool=True, coord_decimals: int=6, speed_decimals: int=6, noise_sigma: float=0.30, spike_prob: float=0.004) -> List[dict]:
    """按 App 3.6.814 原生格式序列化轨迹点。

    原生特征（来自历史合格记录取证）：
      - 键固定 a,c,e,i,l,m,o,s,t 且**全部为字符串**
      - c = GPS 原始点位序号（连续递增，起点不固定，可能跳号），不是秒偏移
      - e = 累计步数；i = 'true'/'false'；l = '1'；m = '0'
      - t = 毫秒且对齐整秒；a/o 保留 6 位小数；s 保留 6 位小数
      - 不携带 distance 等额外字段
    """
    result: List[dict] = []
    cumulative_steps = 0.0
    gps_counter = random.randint(0, 15)
    freeze_left = 0
    rng = random.Random()
    _step_rng = random.Random()
    last = len(track.samples) - 1

    # 逐点抖动预先算好，多径跳点用三角形（本点全量、相邻点半量）而不是孤立尖刺
    dlat = [0.0] * len(track.samples)
    dlon = [0.0] * len(track.samples)
    if noise_sigma > 0:
        for idx, sample in enumerate(track.samples):
            sigma = noise_sigma * (0.35 if idx in (0, last) else 1.0)
            plat, plon = point_noise(sample.lat, sample.lon, rng, sigma)
            dlat[idx] = plat - sample.lat
            dlon[idx] = plon - sample.lon
        for idx in range(1, last):
            if rng.random() >= spike_prob:
                continue
            amp = rng.uniform(1.6, 3.2)
            plat, plon = point_noise(track.samples[idx].lat, track.samples[idx].lon, rng, amp)
            dlat[idx] = plat - track.samples[idx].lat
            dlon[idx] = plon - track.samples[idx].lon
            for nb, w in ((idx - 1, 0.45), (idx + 1, 0.45)):
                if 0 < nb < last:
                    dlat[nb] += dlat[idx] * w
                    dlon[nb] += dlon[idx] * w

    for idx, sample in enumerate(track.samples):
        # 真人每步落点有 3% 抖动（步幅不会均匀）；后程 3% 疲劳漂移：步幅略增、步频略降
        _prog = idx / last if last else 0.0
        cumulative_steps += steps_for_interval(sample.distance_m, sample.speed_mps, rng=_step_rng, jitter=0.03) * (1.0 - 0.03 * _prog)
        steps_int = int(round(cumulative_steps))
        lat_f = sample.lat + dlat[idx]
        lon_f = sample.lon + dlon[idx]
        lat = f'{lat_f:.{coord_decimals}f}'
        lon = f'{lon_f:.{coord_decimals}f}'
        speed = f'{sample.speed_mps:.{speed_decimals}f}'
        t_ms = int(round(sample.t_ms / 1000.0)) * 1000
        if isPublic:
            result.append({'a': lat, 'c': str(gps_counter), 'e': str(steps_int), 'i': 'true' if sample.paused else 'false', 'l': '1', 'm': '0', 'o': lon, 's': speed, 't': str(t_ms)})
        else:
            result.append({'countTime': 0, 'latitude': float(lat), 'locationType': 1, 'longitude': float(lon), 'puase': bool(sample.paused), 'speed': float(speed), 'stability': 0, 'time': t_ms})
        # 真实设备：遮挡时 NMEA 无新解，序号冻结若干秒；恢复瞬间会跳号（只冻结计数，不复制坐标）
        if freeze_left > 0:
            freeze_left -= 1
        elif random.random() < 0.004:
            freeze_left = random.randint(2, 5)
        else:
            gps_counter += random.choice((1, 1, 1, 1, 2, 3)) if random.random() < 0.10 else 1
    if len(result) >= 2 and isPublic:
        # 还原真人跑步的静止/暂停片段：历史记录里零速点 12~58 个、i='true' 2~48 个
        pause_windows = random.randint(3, 6)
        for _ in range(pause_windows):
            if len(result) < 40:
                break
            start = random.randint(10, len(result) - 10)
            for idx in range(start, min(start + random.randint(1, 6), len(result) - 1)):
                result[idx]['s'] = '0.000000'
                result[idx]['i'] = 'true'
        # GPS 抖动导致的瞬时速度尖峰（历史记录 max 5.1~10.3 m/s）
        for idx in random.sample(range(1, len(result) - 1), k=min(random.randint(3, 8), max(1, len(result) - 2))):
            sp = float(result[idx]['s'])
            if sp > 0:
                result[idx]['s'] = f'{sp * random.uniform(1.8, 3.0):.6f}'
        result[-2]['i'] = 'true'
        result[-1]['i'] = 'true'
    return result

def generate_run_track(pointList: Sequence[Sequence[float]], needDistance: float, startTimeStamp: float, planUseTime: float, isPublic: bool=True, options: Optional[ResampleOptions]=None, rng: Optional[random.Random]=None, risk_resolver=None) -> Tuple[ResampledTrack, BucketAllocation, ValidationReport]:
    track = resample_track(points=pointList, need_distance_m=needDistance, start_timestamp_ms=startTimeStamp, plan_use_time_s=planUseTime, options=options, rng=rng, risk_resolver=risk_resolver)
    intervals = [StepInterval(start_offset_s=start, duration_s=duration, steps=steps) for start, duration, steps in track.step_samples()]
    allocation = allocate_steps(intervals, bucket_seconds=DEFAULT_BUCKET_SECONDS, estimated=False, total_steps=int(round(sum((iv.steps for iv in intervals)))))
    report = validate_track(track, bucket_allocation=allocation, total_steps=allocation.total_steps, summary_coords=[track.points[0], track.points[-1]])
    if not report.ok:
        logger.warning(report.summary())
    return (track, allocation, report)

def measure_legacy_distance(points: Sequence[dict]) -> float:
    """按上报的点串重新量距离（真实手机也是拿点串算，而不是拿规划距离）。"""
    total = 0.0
    for i in range(1, len(points)):
        if 'a' in points[i]:
            la0, lo0 = float(points[i - 1]['a']), float(points[i - 1]['o'])
            la1, lo1 = float(points[i]['a']), float(points[i]['o'])
        else:
            la0, lo0 = float(points[i - 1]['latitude']), float(points[i - 1]['longitude'])
            la1, lo1 = float(points[i]['latitude']), float(points[i]['longitude'])
        total += haversine_distance(la0, lo0, la1, lo1)
    return total


def _enforce_step_bucket_floor(buckets: List[int], floor: int=18) -> List[int]:
    """保证每个 60s 步数桶不低于 floor：历史合格记录里没有 0/个位数段。

    低于 floor 的桶会从当前最大的桶里扣补，保证总和不变。
    """
    buckets = list(buckets)
    for i, value in enumerate(buckets):
        if value >= floor:
            continue
        need = floor - value
        richest = max(range(len(buckets)), key=lambda k: buckets[k])
        movable = buckets[richest] - floor
        take = min(need, max(0, movable))
        if take <= 0:
            continue
        buckets[richest] -= take
        buckets[i] += take
    return buckets


def _step_buckets_from_points(points: Sequence[dict], bucket_ms: int=60000) -> List[int]:
    """按 60s 窗口从点串的累计步数 e 反推 stepNumbers。

    App 的 stepNumbers 就是「按时间窗切点串、取 e 的增量」，所以它必须与 e 末值严格一致；
    直接用规划桶会和点串的步幅抖动/疲劳漂移差出几个百分点，末点步数对不上上报步数。
    """
    t0 = int(points[0]['t'])
    edges = [0]
    window = 0
    last_e = 0
    for point in points:
        index = (int(point['t']) - t0) // bucket_ms
        while window < index:
            edges.append(last_e)
            window += 1
        last_e = int(point['e'])
    edges.append(last_e)
    return [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]


def genTiShiNengRunPathRepeat(pointList: Sequence[Sequence[float]], needDistance: float, startTimeStamp: float, planUseTime: float, isPublic: bool=True) -> Tuple[List[dict], List[int], float]:
    track, allocation, report = generate_run_track(pointList=pointList, needDistance=needDistance, startTimeStamp=startTimeStamp, planUseTime=planUseTime, isPublic=isPublic)
    if not report.ok:
        report.raise_if_failed()
    result = to_legacy_points(track, isPublic=isPublic)
    raw = measure_legacy_distance(result)
    # 真机的「里程」是平滑后的规划轨迹长度；上报的点串是带 GPS 噪声的原始轨迹，
    # 逐点累加必然比平滑里程略长（真实合格记录 +0.5%~+5.2%）。二者完全相等反而异常。
    reported = track.total_distance_m
    # 真机的点串一定比平滑里程长一点（多径噪声），若恰好打平就补几处轻微跳点
    if reported > 0 and raw / reported - 1.0 < 0.004:
        _r = random.Random()
        for _ in range(_r.randint(3, 6)):
            i = _r.randint(1, max(1, len(result) - 2))
            dx = _r.uniform(-2.2, 2.2) / 111320.0
            dy = _r.uniform(-2.2, 2.2) / (111320.0 * 0.917)
            # 坐标必须回写成字符串，写 float 会被 9 字段全字符串门禁拦下
            result[i]['a'] = f"{float(result[i]['a']) + dx:.6f}"
            result[i]['o'] = f"{float(result[i]['o']) + dy:.6f}"
        raw = measure_legacy_distance(result)
    # stepNumbers 从点串的 e 增量反推，保证 sum(segs) == 末点 e（真机就是这么切的）
    step_buckets = _enforce_step_bucket_floor(_step_buckets_from_points(result), floor=18)
    logger.info(f'needDistance:{needDistance},planDistance:{reported:.1f},rawStream:{raw:.1f}(+{(raw / reported - 1) * 100:.2f}%) stepBuckets:{len(step_buckets)}/{sum(step_buckets)}(planned {allocation.total_steps})')
    return (result, step_buckets, reported)

def describe_track(track: ResampledTrack) -> dict:
    chord = compute_metrics(track.points, track.timestamps_ms)
    elapsed = track.elapsed_s
    avg_speed = track.total_distance_m / elapsed if elapsed > 0 else 0.0
    return {'distance_m': round(track.total_distance_m, 3), 'chord_distance_m': round(chord.distance_m, 3), 'elapsed_s': round(elapsed, 1), 'point_count': len(track.samples), 'avg_speed_mps': round(avg_speed, 4), 'pace': format_pace(pace_seconds_per_km(track.total_distance_m, elapsed)), 'cadence_spm': round(cadence_spm(avg_speed), 1), 'stride_m': round(stride_for_speed(avg_speed), 3), 'steps': track.total_steps, 'cleaned_points': track.cleaned_point_count, 'source_points': track.source_point_count, 'notes': track.notes}
