"""行为等价性测试：新模块 vs 旧实现录制的基线（tests/fixtures/legacy_golden.json）。

这组用例的作用不是"测试新代码写没写对"，而是证明**重写没有改变任何数值行为**。
基线由 tools/snapshot_legacy.py 在删除旧模块之前生成；只要这里的断言全绿，
就说明几何、分桶、清洗、校验、提交字段、环境指纹、人脸排期、加解密的输出
与旧实现逐位一致（唯一的例外见 KNOWN_DIVERGENCES）。
"""
from __future__ import annotations

import json
import os
import random
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tsn_buckets  # noqa: E402
import tsn_crypto  # noqa: E402
import tsn_env  # noqa: E402
import tsn_face_plan  # noqa: E402
import tsn_geo  # noqa: E402
import tsn_submit  # noqa: E402
import tsn_track_audit  # noqa: E402
import tsn_track_filter  # noqa: E402
import track_resample  # noqa: E402

GOLDEN_PATH = os.path.join(ROOT, 'tests', 'fixtures', 'legacy_golden.json')

# 已知的、有意修正的行为差异：
# 旧实现的 unpad 用 `s[:-ord(s[-1:])]`，末字节为 0x00 时切片成 s[:-0] → 空串，
# 零填充密文解出来永远是空。新实现提供 decrypt_zero_padded，正确剥掉尾随 0x00。
KNOWN_DIVERGENCES = {'aes.roundtrip_zero'}


@pytest.fixture(scope='module')
def golden():
    with open(GOLDEN_PATH, encoding='utf-8') as fh:
        return json.load(fh)


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


def test_geo_matches_golden(golden):
    expected = golden['geo']
    assert tsn_geo.EARTH_RADIUS_M == expected['radius_m']
    assert tsn_geo.METERS_PER_KM == expected['meters_per_km']
    got = [round(tsn_geo.haversine_distance(*case), 9) for case in HAVERSINE_CASES]
    assert got == expected['haversine']
    assert [[round(float(v), 9) for v in p] for p in tsn_geo.normalize_points(SQUARE)] == expected['normalize']
    assert round(tsn_geo.point_list_distance(SQUARE), 9) == expected['chain_m']

    proj = tsn_geo.LocalProjection(BASE_LON, BASE_LAT)
    assert [round(v, 9) for v in proj.to_xy(BASE_LON + 0.0009, BASE_LAT + 0.0008)] == expected['projection_xy']
    xy = proj.to_xy(BASE_LON + 0.0004, BASE_LAT + 0.0002)
    assert [round(v, 12) for v in proj.to_lon_lat(*xy)] == expected['projection_roundtrip']
    assert round(proj.distance(BASE_LON, BASE_LAT, BASE_LON + 0.0009, BASE_LAT + 0.0008), 9) == expected['projection_distance']
    assert [round(v, 12) for v in proj.interpolate(BASE_LON, BASE_LAT, BASE_LON + 0.0009, BASE_LAT + 0.0008, 0.25)] == expected['projection_interpolate']


def test_buckets_match_golden(golden):
    expected = golden['buckets']
    intervals = tsn_buckets.intervals_from_samples(INTERVALS)
    assert tsn_buckets.allocate_steps(intervals).buckets == expected['fixed']
    assert tsn_buckets.allocate_steps(intervals, total_steps=353).buckets == expected['with_total']
    empty = tsn_buckets.allocate_steps([])
    assert empty.buckets == expected['empty']['buckets']
    assert empty.total_steps == expected['empty']['total']
    assert tsn_buckets.DEFAULT_BUCKET_SECONDS == expected['bucket_seconds']
    # 分桶不变量：合计恒等于总步数
    assert sum(tsn_buckets.allocate_steps(intervals).buckets) == 353


def test_clean_matches_golden(golden):
    dirty = [list(p) for p in SQUARE]
    dirty.insert(2, list(SQUARE[1]))
    dirty.insert(3, [BASE_LON + 0.0030, BASE_LAT + 0.0030])
    result = tsn_track_filter.clean_track(dirty)
    expected = golden['clean']
    assert [[round(v, 9) for v in p] for p in result.points] == expected['points']
    assert [[r.index, r.reason] for r in result.removed] == expected['removed']
    assert result.removed_reason_counts() == expected['removed_counts']


def test_resample_matches_golden(golden):
    track = _track()
    expected = golden['resample']
    assert len(track.samples) == expected['count']
    assert round(track.total_distance_m, 9) == expected['total_distance_m']
    assert round(track.elapsed_s, 9) == expected['elapsed_s']
    assert track.total_steps == expected['total_steps']
    assert [[round(a, 9), round(b, 9), round(c, 9)] for a, b, c in track.step_samples()] == expected['step_samples']
    head = [
        [round(s.lon, 9), round(s.lat, 9), round(s.t_ms, 3), round(s.dt_ms, 3),
         round(s.distance_m, 9), round(s.cumulative_m, 9), round(s.speed_mps, 9), s.paused]
        for s in track.samples[:5]
    ]
    tail = [
        [round(s.lon, 9), round(s.lat, 9), round(s.t_ms, 3), round(s.dt_ms, 3),
         round(s.distance_m, 9), round(s.cumulative_m, 9), round(s.speed_mps, 9), s.paused]
        for s in track.samples[-3:]
    ]
    assert head == expected['head']
    assert tail == expected['tail']
    assert round(sum(s.speed_mps for s in track.samples), 9) == expected['speed_sum']


def test_validate_matches_golden(golden):
    track = _track()
    allocation = tsn_buckets.allocate_steps(
        tsn_buckets.intervals_from_samples(track.step_samples()), total_steps=track.total_steps
    )
    report = tsn_track_audit.validate_track(track, bucket_allocation=allocation, total_steps=track.total_steps)
    expected = golden['validate']
    assert report.ok is expected['ok']
    assert [[i.code, i.severity] for i in report.issues] == expected['issues']
    got_metrics = {k: (round(v, 9) if isinstance(v, float) else v) for k, v in report.metrics.items()}
    assert got_metrics == expected['metrics']
    assert 'warning=1' in report.summary()


def test_submit_fields_match_golden(golden):
    expected = golden['payload']
    points = [
        {'a': f'{p[1]:.6f}', 'o': f'{p[0]:.6f}', 's': '2.500000', 't': '1793020234000',
         'e': str(i * 3), 'i': 'false', 'l': '1', 'm': '0', 'c': str(i)}
        for i, p in enumerate([[round(v, 9) for v in q] for q in SQUARE] * 3)
    ]
    assert tsn_submit.build_submit_extras(SETTING, points=points, elapsed_s=880.0, distance_m=2090.0, steps=1015) == expected['extras']
    detail = tsn_submit.qualification_detail(SETTING, 880.0, 2090.0, 1015)
    # 新增的单次要求里程（rangeSetting）在黑盒基线里等于 totalRange
    assert detail.pop('per_run_range_m') == tsn_submit.total_range_meters(SETTING)
    assert detail == expected['qualification']
    assert tsn_submit.required_steps(SETTING, 2090.0) == expected['required_steps']
    assert tsn_submit.required_steps(dict(SETTING, isStepRecursion=1), 3000.0) == expected['required_steps_recursion']
    assert tsn_submit.compute_is_valid(SETTING, 880.0, 2090.0, 1015) == expected['is_valid']
    assert tsn_submit.compute_is_valid(SETTING, 600.0, 1500.0, 700) == expected['is_valid_short']
    assert tsn_submit.remark_id(SETTING) == expected['remark_id']
    assert tsn_submit.select_remark(SETTING, '阳光跑') == expected['select_remark']
    assert tsn_submit.build_auth_info({'b': 2, 'a': None, 'c': True, 'd': 'x y'}) == expected['auth_info']
    assert tsn_submit.session_key('token-abc', '1793020234000') == expected['session_key']


def test_environment_matches_golden(golden):
    got = tsn_env.build_environment_payload(
        device_id='fixture-device',
        signals=tsn_env.clean_device_signals(),
        abis=['arm64-v8a', 'armeabi-v7a'],
        nonce='fixture-nonce',
        timestamp_ms=1793020234000,
        signed=True,
        debug=False,
    )
    assert got == golden['environment']
    oppo = json.loads(got['oppo'])
    assert oppo['checksum'] == tsn_env.checksum(oppo)


def test_face_plan_matches_golden(golden):
    got = [
        p.as_dict()
        for p in tsn_face_plan.build_middle_face_points(
            path=FACE_PATH,
            start_timestamp_ms=1793020234000,
            end_timestamp_ms=1793020264000,
            middle_faces=[0.2, 0.35, 0.7, 1.5, -1.0],
        )
    ]
    assert got == golden['face']


def test_crypto_matches_golden(golden):
    expected = golden['aes']
    cbc = tsn_crypto.AESCrypto('0123456789abcdef', 'fedcba9876543210')
    ecb = tsn_crypto.AESCrypto('0123456789abcdef', None, ecb_mode=True)
    zero = tsn_crypto.AESCrypto('0123456789abcdef', 'fedcba9876543210')

    assert cbc.encrypt('{"a":1,"b":"测试"}') == expected['cbc_pkcs7']
    assert ecb.encrypt('thanks,pig4cloud') == expected['ecb_pkcs7']
    assert zero.encrypt('1357924680135792', zero_padding=True) == expected['cbc_zero_pad']
    assert cbc.decrypt(expected['cbc_pkcs7']) == expected['roundtrip_cbc']
    assert ecb.decrypt(expected['ecb_pkcs7']) == expected['roundtrip_ecb']
    # 差异项：旧实现零填充解密返回空串（切片 bug），新实现还原明文
    assert expected['roundtrip_zero'] == ''
    assert zero.decrypt_zero_padded(expected['cbc_zero_pad']) == '1357924680135792'


def test_known_divergences_are_documented():
    assert KNOWN_DIVERGENCES == {'aes.roundtrip_zero'}
