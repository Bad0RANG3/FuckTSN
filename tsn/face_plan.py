"""中途人脸点的排期：把"随机抽几个比例"翻译成带时间戳的坐标点。

逆向结论（v3.6.814）：
- 一次跑步要上传多张人脸：起点附近 1 张（type=1）、若干"中途"抽检（type=2）、
  终点附近 1 张（type=3）。中途抽检的坐标必须落在轨迹上、时间戳必须在
  起止时间之内，否则服务器会当"人脸缺失"处理。
- 抽检比例由服务端下发的 `middleFace` 配置决定（常见取值形如 0.2/0.35/0.7），
  比例会被夹到 [0.05, 1.2]；相邻两张之间至少间隔 5 秒，太密会被平台判成
  重复上传同一张。
- 人脸接口本身是 multipart：identify/type/runType/coordinates/timeStamp/
  exception/environment + 一个 file 分段（见 public.exerciseRunningFace）。

本模块只负责"该在哪一秒、哪个坐标拍这张脸"，不碰网络。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

MIN_RATIO = 0.05
MAX_RATIO = 1.2
MIN_GAP_S = 5.0


@dataclass
class FacePoint:
    timestamp_ms: int
    latitude: float
    longitude: float
    face_type: int = 2

    def as_dict(self) -> Dict[str, Any]:
        """上传坐标用的结构（注意：不含 face_type，类型走 multipart 的 type 字段）。"""
        return {
            'timestamp': int(self.timestamp_ms),
            'latitude': float(self.latitude),
            'longitude': float(self.longitude),
        }


def normalize_middle_faces(raw: Optional[Iterable[Union[float, Dict[str, Any]]]]) -> List[Union[float, Dict[str, Any]]]:
    """把服务端下发的比例（可能是 dict）夹到合法区间。"""
    if not raw:
        return []
    result: List[Union[float, Dict[str, Any]]] = []
    for item in raw:
        if isinstance(item, dict):
            result.append(dict(item))
            continue
        try:
            value = float(item)
        except (TypeError, ValueError):
            continue
        result.append(min(MAX_RATIO, max(MIN_RATIO, value)))
    return result


def _point_time(point: Dict[str, Any]) -> Optional[int]:
    for key in ('t', 'time', 'timestamp'):
        if key in point and point[key] is not None:
            try:
                return int(float(point[key]))
            except (TypeError, ValueError):
                return None
    return None


def _point_lon_lat(point: Dict[str, Any]):
    if 'o' in point and 'a' in point:
        try:
            return float(point['o']), float(point['a'])
        except (TypeError, ValueError):
            return None
    if 'longitude' in point and 'latitude' in point:
        try:
            return float(point['longitude']), float(point['latitude'])
        except (TypeError, ValueError):
            return None
    return None


def build_middle_face_points(
    path: Sequence[Dict[str, Any]],
    start_timestamp_ms: int,
    end_timestamp_ms: int,
    middle_faces: Optional[Iterable[Union[float, Dict[str, Any]]]] = None,
    is_public: bool = True,
    face_type: int = 2,
    min_gap_s: float = MIN_GAP_S,
) -> List[FacePoint]:
    """按比例在轨迹上取点，生成待上传的人脸点（已按时间排序并去重）。"""
    ratios = normalize_middle_faces(middle_faces)
    if not ratios or not path:
        return []

    timeline = []
    for point in path:
        ts = _point_time(point)
        coords = _point_lon_lat(point)
        if ts is None or coords is None:
            continue
        timeline.append((ts, coords[1], coords[0]))
    if not timeline:
        return []
    timeline.sort(key=lambda row: row[0])

    total_ms = max(0, int(end_timestamp_ms) - int(start_timestamp_ms))
    picked: List[FacePoint] = []
    for item in ratios:
        if isinstance(item, dict):
            ts = _point_time(item)
            coords = _point_lon_lat(item)
            if ts is None or coords is None:
                continue
            picked.append(FacePoint(ts, coords[1], coords[0], face_type))
            continue
        target_ms = int(start_timestamp_ms) + int(total_ms * float(item))
        chosen = timeline[-1]
        for row in timeline:
            if row[0] >= target_ms:
                chosen = row
                break
        picked.append(FacePoint(chosen[0], chosen[1], chosen[2], face_type))

    picked.sort(key=lambda fp: fp.timestamp_ms)
    deduped: List[FacePoint] = []
    gap_ms = float(min_gap_s) * 1000.0
    for fp in picked:
        if deduped and fp.timestamp_ms - deduped[-1].timestamp_ms < gap_ms:
            continue
        deduped.append(fp)
    return deduped


__all__ = ['MIN_RATIO', 'MAX_RATIO', 'MIN_GAP_S', 'FacePoint', 'normalize_middle_faces', 'build_middle_face_points']
