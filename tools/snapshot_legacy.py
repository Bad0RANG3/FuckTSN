# -*- coding: utf-8 -*-
"""为「旧实现清除 + 自研重写」录制行为基线。

在删掉旧同名模块**之前**跑过一次，把纯函数的输出冻结到
tests/fixtures/legacy_golden.json；重写完成后由
tests/test_legacy_equivalence.py 逐项比对，证明重写没有改动任何数值行为。

旧模块现已从本仓库移除，脚本改为从归档目录加载（`TSN_LEGACY_DIR`，默认 `../archive`）。
只有本仓库仍保留的模块（如 `track_resample`）才从本仓库导入 —— `sys.path` 里 `ROOT`
排在归档目录之前，优先命中本仓库版本。

用法：
    python tools/snapshot_legacy.py                       # 默认读 ../archive
    TSN_LEGACY_DIR=/path/to/old python tools/snapshot_legacy.py
"""
from __future__ import annotations

import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LEGACY_DIR = os.environ.get('TSN_LEGACY_DIR') or os.path.join(os.path.dirname(ROOT), 'archive')
if not os.path.isdir(LEGACY_DIR):
    sys.exit(f'找不到旧实现归档目录：{LEGACY_DIR}（用 TSN_LEGACY_DIR 指定）')
sys.path.insert(0, LEGACY_DIR)
sys.path.insert(0, ROOT)

from AesUtils import AESCrypto  # noqa: E402
import step_buckets  # noqa: E402
import track_clean  # noqa: E402
import track_geo  # noqa: E402
import track_resample  # noqa: E402
import track_validate  # noqa: E402
import tsn_environment  # noqa: E402
import tsn_face_schedule  # noqa: E402
import tsn_payload  # noqa: E402

GOLDEN = os.path.join(ROOT, 'tests', 'fixtures', 'legacy_golden.json')

# 固定输入：一条 200 m 的方形闭合路线（经纬度按 6 位小数给出）
BASE_LON, BASE_LAT = 116.3055, 39.9928
SQUARE = [
    [BASE_LON, BASE_LAT],
    [BASE_LON + 0.0009, BASE_LAT],
    [BASE_LON + 0.0009, BASE_LAT + 0.0008],
    [BASE_LON, BASE_LAT + 0.0008],
    [BASE_LON, BASE_LAT],
]
HAVERSINE_CASES = [
    (39.994742, 116.306054, 39.991179, 116.304797),
    (39.994742, 116.306054, 39.993218, 116.306581),
    (0.0, 0.0, 0.0, 1.0),
    (89.0, 179.0, -89.0, -179.0),
    (39.994742, 116.306054, 39.994742, 116.306054),
]
INTERVALS = [(0.0, 60.0, 118.0), (60.0, 60.0, 121.0), (120.0, 45.0, 90.0), (165.0, 12.0, 24.0)]
FACE_PATH = [
    {'t': '1793020234000', 'a': '39.994742', 'o': '116.306054'},
    {'t': '1793020235000', 'a': '39.994750', 'o': '116.306068'},
    {'t': '1793020244000', 'a': '39.994785', 'o': '116.306061'},
    {'t': '1793020254000', 'a': '39.994836', 'o': '116.306059'},
    {'t': '1793020264000', 'a': '39.994882', 'o': '116.306062'},
]
SETTING = {
    'totalRange': '2.000',
    'rangeSetting': '2.000',
    'step': '1000',
    'stepSetting': 1000,
    'isStepRecursion': 0,
    'startQualifiedTime': 6,
    'endQualifiedTime': 21,
    'limitSpeed': '3.33',
    'limitStepFrequency': '300',
    'remarkId': '131310',
    'remarks': [{'id': '131310', 'name': '阳光跑'}],
    'userOpenDevelop': False,
}


def _track():
    rng = random.Random(20261120)
    return track_resample.resample_track(
        points=SQUARE,
        need_distance_m=2090.0,
        start_timestamp_ms=1793020234000.0,
        plan_use_time_s=880.0,
        options=track_resample.ResampleOptions(),
        rng=rng,
        clean=True,
    )


def _dump_points(points):
    return [[round(float(p[0]), 9), round(float(p[1]), 9)] for p in points]


def main() -> None:
    geo = track_geo
    proj = geo.LocalProjection(BASE_LON, BASE_LAT)
    golden = {
        'meta': {'generated_by': 'tools/snapshot_legacy.py', 'module_version': 1},
        'geo': {
            'radius_m': geo.EARTH_RADIUS_M,
            'haversine': [round(geo.haversine_distance(*c), 9) for c in HAVERSINE_CASES],
            'normalize': _dump_points(geo.normalize_points(SQUARE)),
            'chain_m': round(geo.point_list_distance(SQUARE), 9),
            'projection_xy': [round(v, 9) for v in proj.to_xy(BASE_LON + 0.0009, BASE_LAT + 0.0008)],
            'projection_roundtrip': [round(v, 12) for v in proj.to_lon_lat(*proj.to_xy(BASE_LON + 0.0004, BASE_LAT + 0.0002))],
            'projection_distance': round(proj.distance(BASE_LON, BASE_LAT, BASE_LON + 0.0009, BASE_LAT + 0.0008), 9),
            'projection_interpolate': [round(v, 12) for v in proj.interpolate(BASE_LON, BASE_LAT, BASE_LON + 0.0009, BASE_LAT + 0.0008, 0.25)],
            'meters_per_km': geo.METERS_PER_KM,
        },
        'buckets': {
            'fixed': step_buckets.allocate_steps(
                step_buckets.intervals_from_samples(INTERVALS)
            ).buckets,
            'with_total': step_buckets.allocate_steps(
                step_buckets.intervals_from_samples(INTERVALS), total_steps=353
            ).buckets,
            'empty': {
                'buckets': step_buckets.allocate_steps([]).buckets,
                'total': step_buckets.allocate_steps([]).total_steps,
            },
            'bucket_seconds': step_buckets.DEFAULT_BUCKET_SECONDS,
        },
        'clean': {},
        'resample': {},
        'validate': {},
        'payload': {},
        'environment': {},
        'face': [],
        'aes': {},
    }

    # —— 清洗：在方形路线里塞重复点与一个几何尖刺 ——
    dirty = [list(p) for p in SQUARE]
    dirty.insert(2, list(SQUARE[1]))                     # 与上一点重复
    dirty.insert(3, [BASE_LON + 0.0030, BASE_LAT + 0.0030])  # 尖刺
    cleaned = track_clean.clean_track(dirty)
    golden['clean'] = {
        'points': _dump_points(cleaned.points),
        'removed': [(r.index, r.reason) for r in cleaned.removed],
        'removed_counts': cleaned.removed_reason_counts(),
    }

    # —— 重采样（rng 固定种子，逐点比对）——
    track = _track()
    samples = track.samples
    golden['resample'] = {
        'count': len(samples),
        'total_distance_m': round(track.total_distance_m, 9),
        'elapsed_s': round(track.elapsed_s, 9),
        'total_steps': track.total_steps,
        'step_samples': [[round(a, 9), round(b, 9), round(c, 9)] for a, b, c in track.step_samples()],
        'head': [
            [round(s.lon, 9), round(s.lat, 9), round(s.t_ms, 3), round(s.dt_ms, 3),
             round(s.distance_m, 9), round(s.cumulative_m, 9), round(s.speed_mps, 9), s.paused]
            for s in samples[:5]
        ],
        'tail': [
            [round(s.lon, 9), round(s.lat, 9), round(s.t_ms, 3), round(s.dt_ms, 3),
             round(s.distance_m, 9), round(s.cumulative_m, 9), round(s.speed_mps, 9), s.paused]
            for s in samples[-3:]
        ],
        'speed_sum': round(sum(s.speed_mps for s in samples), 9),
    }

    # —— 校验报告 ——
    allocation = step_buckets.allocate_steps(
        step_buckets.intervals_from_samples(track.step_samples()), total_steps=track.total_steps
    )
    report = track_validate.validate_track(track, bucket_allocation=allocation, total_steps=track.total_steps)
    golden['validate'] = {
        'ok': report.ok,
        'issues': [[i.code, i.severity] for i in report.issues],
        'metrics': {k: (round(v, 9) if isinstance(v, float) else v) for k, v in report.metrics.items()},
    }

    # —— 提交字段 ——
    points = [
        {'a': f'{p[1]:.6f}', 'o': f'{p[0]:.6f}', 's': '2.500000', 't': '1793020234000',
         'e': str(i * 3), 'i': 'false', 'l': '1', 'm': '0', 'c': str(i)}
        for i, p in enumerate(_dump_points(SQUARE) * 3)
    ]
    golden['payload'] = {
        'extras': tsn_payload.build_submit_extras(
            SETTING, points=points, elapsed_s=880.0, distance_m=2090.0, steps=1015
        ),
        'qualification': tsn_payload.qualification_detail(SETTING, 880.0, 2090.0, 1015),
        'required_steps': tsn_payload.required_steps(SETTING, 2090.0),
        'required_steps_recursion': tsn_payload.required_steps(
            dict(SETTING, isStepRecursion=1), 3000.0
        ),
        'is_valid': tsn_payload.compute_is_valid(SETTING, 880.0, 2090.0, 1015),
        'is_valid_short': tsn_payload.compute_is_valid(SETTING, 600.0, 1500.0, 700),
        'remark_id': tsn_payload.remark_id(SETTING),
        'select_remark': tsn_payload.select_remark(SETTING, '阳光跑'),
        'auth_info': tsn_payload.build_auth_info({'b': 2, 'a': None, 'c': True, 'd': 'x y'}),
        'session_key': tsn_payload.session_key('token-abc', '1793020234000'),
    }

    # —— 环境指纹 ——
    golden['environment'] = tsn_environment.build_environment_payload(
        device_id='fixture-device',
        signals=tsn_environment.clean_device_signals(),
        abis=['arm64-v8a', 'armeabi-v7a'],
        nonce='fixture-nonce',
        timestamp_ms=1793020234000,
        signed=True,
        debug=False,
    )

    # —— 中途人脸 ——
    golden['face'] = [
        p.as_dict()
        for p in tsn_face_schedule.build_middle_face_points(
            path=FACE_PATH,
            start_timestamp_ms=1793020234000,
            end_timestamp_ms=1793020264000,
            middle_faces=[0.2, 0.35, 0.7, 1.5, -1.0],
        )
    ]

    # —— 加解密（标准向量，可跨实现比对）——
    aes_cbc = AESCrypto('0123456789abcdef', 'fedcba9876543210')
    aes_ecb = AESCrypto('0123456789abcdef', b'', ecb_mode=True)
    aes_zero = AESCrypto('0123456789abcdef', 'fedcba9876543210')
    golden['aes'] = {
        'cbc_pkcs7': aes_cbc.encrypt('{"a":1,"b":"测试"}'),
        'ecb_pkcs7': aes_ecb.encrypt('thanks,pig4cloud'),
        'cbc_zero_pad': aes_zero.encrypt('1357924680135792', zero_padding=True),
        'roundtrip_cbc': aes_cbc.decrypt(aes_cbc.encrypt('{"a":1,"b":"测试"}')),
        'roundtrip_ecb': aes_ecb.decrypt(aes_ecb.encrypt('thanks,pig4cloud')),
        'roundtrip_zero': aes_zero.decrypt(aes_zero.encrypt('1357924680135792', zero_padding=True)),
    }

    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, 'w', encoding='utf-8') as fh:
        json.dump(golden, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f'wrote {GOLDEN}')
    print(f"  resample samples={len(samples)} validate_ok={report.ok} issues={len(report.issues)}")


if __name__ == '__main__':
    main()
