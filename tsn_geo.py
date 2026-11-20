"""地理计算：球面距离、局部平面投影、坐标校验。

为什么需要这三样（逆向依据）：提交给体测服务器的 pointList 里，坐标是
6 位小数的字符串（a=纬度、o=经度），服务器只看它自己的 `sportRange` 字段，
不会按点位重算距离；但客户端要自己造一条"链长 ≥ 上报距离"的轨迹，并且要能
在两点之间插值补点，所以本地必须有一套可控的米制度量。

实现取舍：
- 距离用 haversine（球面），半径取 IUGG 平均半径 6371008.8 m，与 App 内
  `Location.distanceBetween` 的量级一致，避免整条链与上报值出现系统性偏差。
- 局部投影用等距圆柱（equirectangular）近似：研究范围是单个校园（<5 km），
  该近似在 2 km 尺度上的相对误差 < 0.02%，足够支撑插值与闭环。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

EARTH_RADIUS_M = 6371008.8
EARTH_RADIUS_KM = EARTH_RADIUS_M / 1000.0
METERS_PER_KM = 1000.0
MAX_LAT = 90.0
MAX_LON = 180.0


class GeoError(ValueError):
    """地理计算的基类异常。"""


class CrsMismatchError(GeoError):
    """坐标系混用。"""


class InvalidCoordinateError(GeoError):
    """坐标非法（非数字 / NaN / Inf / 越界 / 缺分量）。"""


@dataclass(frozen=True)
class CoordinateSystem:
    name: str

    def __str__(self) -> str:
        return self.name


WGS84 = CoordinateSystem('WGS84')
GCJ02 = CoordinateSystem('GCJ02')
BD09 = CoordinateSystem('BD09')


def validate_lon_lat(lon, lat) -> Tuple[float, float]:
    """校验并规范化一个经纬度对，返回 (lon, lat) 浮点元组。"""
    try:
        lon_f = float(lon)
        lat_f = float(lat)
    except (TypeError, ValueError) as exc:
        raise InvalidCoordinateError(f'坐标不是数字: lon={lon!r} lat={lat!r}') from exc
    if math.isnan(lon_f) or math.isnan(lat_f) or math.isinf(lon_f) or math.isinf(lat_f):
        raise InvalidCoordinateError(f'坐标包含 NaN/Inf: lon={lon!r} lat={lat!r}')
    if not -MAX_LON <= lon_f <= MAX_LON:
        raise InvalidCoordinateError(f'经度越界: {lon_f}')
    if not -MAX_LAT <= lat_f <= MAX_LAT:
        raise InvalidCoordinateError(f'纬度越界: {lat_f}')
    return lon_f, lat_f


def assert_same_crs(*crs: CoordinateSystem) -> CoordinateSystem:
    given = [c for c in crs if c is not None]
    if not given:
        return WGS84
    first = given[0]
    for other in given[1:]:
        if other.name != first.name:
            raise CrsMismatchError(f'坐标系混用: {first.name} vs {other.name}')
    return first


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点球面距离（米）。参数顺序是 (lat, lon)，与 App 的 API 习惯一致。"""
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    d_lat = lat2_rad - lat1_rad
    d_lon = lon2_rad - lon1_rad
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(d_lon / 2) ** 2
    a = min(1.0, max(0.0, a))
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class LocalProjection:
    """以某点为原点的局部平面投影（米），用于插值、闭环与曲率控制。"""

    def __init__(self, origin_lon: float, origin_lat: float):
        self.origin_lon, self.origin_lat = validate_lon_lat(origin_lon, origin_lat)
        self._cos_lat = math.cos(math.radians(self.origin_lat))
        self._m_per_deg_lon = math.radians(1.0) * EARTH_RADIUS_M * self._cos_lat
        self._m_per_deg_lat = math.radians(1.0) * EARTH_RADIUS_M

    def to_xy(self, lon: float, lat: float) -> Tuple[float, float]:
        lon_f, lat_f = validate_lon_lat(lon, lat)
        return (
            (lon_f - self.origin_lon) * self._m_per_deg_lon,
            (lat_f - self.origin_lat) * self._m_per_deg_lat,
        )

    def to_lon_lat(self, x: float, y: float) -> Tuple[float, float]:
        return (
            self.origin_lon + x / self._m_per_deg_lon,
            self.origin_lat + y / self._m_per_deg_lat,
        )

    def distance(self, lon1: float, lat1: float, lon2: float, lat2: float) -> float:
        x1, y1 = self.to_xy(lon1, lat1)
        x2, y2 = self.to_xy(lon2, lat2)
        return math.hypot(x2 - x1, y2 - y1)

    def interpolate(self, lon1: float, lat1: float, lon2: float, lat2: float, ratio: float) -> Tuple[float, float]:
        x1, y1 = self.to_xy(lon1, lat1)
        x2, y2 = self.to_xy(lon2, lat2)
        return self.to_lon_lat(x1 + (x2 - x1) * ratio, y1 + (y2 - y1) * ratio)


def normalize_points(points: Iterable[Sequence[float]]) -> List[List[float]]:
    """统一成 [[lon, lat], ...]（注意入参就是这个顺序）。"""
    result: List[List[float]] = []
    for idx, point in enumerate(points):
        if point is None or len(point) < 2:
            raise InvalidCoordinateError(f'第 {idx} 个点缺少经纬度: {point!r}')
        lon, lat = validate_lon_lat(point[0], point[1])
        result.append([lon, lat])
    return result


def point_list_distance(point_list: Sequence[Sequence[float]]) -> float:
    """[[lon, lat], ...] 的逐段链长合计（米）。"""
    total = 0.0
    for i in range(1, len(point_list)):
        prev = point_list[i - 1]
        cur = point_list[i]
        total += haversine_distance(prev[1], prev[0], cur[1], cur[0])
    return total


__all__ = [
    'EARTH_RADIUS_M', 'EARTH_RADIUS_KM', 'METERS_PER_KM', 'MAX_LAT', 'MAX_LON',
    'GeoError', 'CrsMismatchError', 'InvalidCoordinateError',
    'CoordinateSystem', 'WGS84', 'GCJ02', 'BD09',
    'validate_lon_lat', 'assert_same_crs', 'haversine_distance', 'LocalProjection',
    'normalize_points', 'point_list_distance',
]
