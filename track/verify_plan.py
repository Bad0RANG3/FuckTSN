#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""plan.json 独立复核（不复用 compliance.check_submission 的任何判定代码）。

只向 ``backfill.py`` 借「生成」这一段（``load_reference`` / ``build_setting`` /
``build_run``）；所有指标都在本文件里重新实现——haversine、里程、配速、步幅、
步频、stepNumbers 分桶、点位结构、时间窗，一个都不调用被检查模块的现成函数。

两种输入都认：

* ``plan.json`` 里带 ``points`` / ``stepNumbers`` / ``meta``（``backfill.py
  --dry-run`` 写出来的就是这种）→ 直接复核落盘的那份点串，不重新生成；
* 只有日期（``["2026-11-01", ...]``）→ 用 ``--seed`` 现场重生成再复核。

用法::

    python verify_plan.py                       # 复核 config.json 的 plan_file
    python verify_plan.py --plan plan.json --seed 7
    python verify_plan.py --no-fix-noise        # 保留生成器原始噪声（不可复现）
"""
from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import math
import os
import random
import statistics
import sys
import unicodedata

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app import backfill        # noqa: E402  （只借生成函数；判定代码全部在下面重写）
from app import config          # noqa: E402

try:                   # 只为在报告里记录步幅基准漂移，不参与判定
    from track import metrics  # noqa: E402
    STRIDE_BASE_M = float(getattr(metrics, 'STRIDE_BASE_M', 0.0))
except Exception:      # noqa: BLE001
    STRIDE_BASE_M = 0.0

# ---------------------------------------------------------------- 硬判线（服务端实测）
EARTH_R = 6371000.0          # 自己的 haversine 半径，与 tsn_geo 的 6371008.8 无关
PACE_MIN_S = 180.0           # 3'00"/km
PACE_MAX_S = 600.0           # 10'00"/km
LIMIT_SPEED = 3.33           # limitSpeed，m/s
RANGE_SETTING_M = 2000.0     # rangeSetting='2.000'
RANGE_BUFFER = 1.02          # 服务端按 2.000km 判线，留 2% 余量才算稳
STEP_FLOOR = 1000            # 999 步被判「运动步数不符合」
STRIDE_MIN, STRIDE_MAX = 0.62, 1.20
CADENCE_MIN, CADENCE_MAX = 145.0, 195.0
POINT_MIN, POINT_MAX = 300, 1500
BUCKET_MIN, BUCKET_MAX = 110, 210
BUCKET_CNT_MIN, BUCKET_CNT_MAX = 8, 30
NONINT_MAX = 0.02            # 非整秒点占比阈值
DELTA_MIN, DELTA_MAX = 0.002, 0.06   # 点串累加 vs 上报里程：+0.2%~+6%
WIN_START_S, WIN_END_S = 6 * 3600, 21 * 3600
POINT_KEYS = ('a', 'c', 'e', 'i', 'l', 'm', 'o', 's', 't')


# ---------------------------------------------------------------- 自实现几何
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """标准球面 haversine，单位米（不复用 tsn_geo / compliance）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_R * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def polyline_length(points) -> float:
    total = 0.0
    prev = None
    for point in points:
        cur = (float(point['a']), float(point['o']))
        if prev is not None:
            total += haversine(prev[0], prev[1], cur[0], cur[1])
        prev = cur
    return total


def decimals(text: str) -> int:
    return 0 if '.' not in text else len(text.split('.', 1)[1])


def install_deterministic_random(seed: int) -> None:
    """把 ``random.Random()`` 的默认种子接到确定性计数器上。

    runpath.to_legacy_points 内部用的是未播种的 random.Random()，
    不接管的话同一份 plan 每次重跑坐标噪声都不同（里程抖动几米），--seed 就名不副实。
    """
    real = random.Random
    if getattr(real, '_plan_verify_patched', False):
        return
    counter = itertools.count(seed * 7919 + 13)

    class _DeterministicRandom(real):
        _plan_verify_patched = True

        def __init__(self, x=None):
            super().__init__(next(counter) if x is None else x)

    random.Random = _DeterministicRandom


def nearest_margin(value: float, lo: float, hi: float) -> float:
    """值落在 [lo,hi] 内时返回相对余量（0=贴线，1=一倍余量）；越界返回负数。"""
    return min((value - lo) / lo, (hi - value) / hi)


def _hms_to_s(hhmm: str) -> float:
    hour, minute = hhmm.split(':')[:2]
    return int(hour) * 3600 + int(minute) * 60


# ---------------------------------------------------------------- 输入归一
def load_plan(path: str):
    """返回 (records, 是否有落盘点串)。records 原样交给 normalize。"""
    with open(path, encoding='utf-8') as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get('records') or data.get('days') or []
    records = [r for r in (data or []) if isinstance(r, (dict, str))]
    has_points = any(isinstance(r, dict) and r.get('points') for r in records)
    return records, has_points


def normalize(rec, gen) -> dict:
    """把一条记录整理成 measure() 需要的形状。

    gen(day) 只在记录里没有点串时才调用（现场重生成）。
    """
    if isinstance(rec, dict) and rec.get('points'):
        meta = rec.get('meta') or {}
        points = rec['points']
        segs = [int(float(x)) for x in (rec.get('stepNumbers') or rec.get('segs') or [])]
        if not segs:
            segs = backfill.steps_from_points(points)
        start_ts = int(float(meta.get('start_ts_ms') or points[0]['t']))
        end_ts = int(float(meta.get('end_ts_ms') or points[-1]['t']))
        used = int(float(meta.get('sport_time_s') or max(0, (end_ts - start_ts) // 1000)))
        dist = float(meta.get('distance_m') or 0.0)
        steps = int(float(meta.get('total_steps') or sum(segs)))
        raw_day = rec.get('day')
        try:
            day = dt.date.fromisoformat(str(raw_day)[:10])
        except ValueError:
            day = dt.date.today()
        return {'day': day, 'points': points, 'segs': segs, 'dist': dist,
                'start_ts': start_ts, 'end_ts': end_ts, 'used': used,
                'steps': steps, 'pace': meta.get('pace'), 'meta': meta}

    raw_day = rec.get('day') if isinstance(rec, dict) else rec
    day = dt.date.fromisoformat(str(raw_day)[:10])
    return gen(day)


# ---------------------------------------------------------------- 单条重算
def measure(run: dict) -> dict:
    pts = run['points']
    n = len(pts)
    ts = [int(p['t']) for p in pts]
    span_s = (ts[-1] - ts[0]) / 1000.0          # 首末点 t 差
    used_s = float(run['used'])                 # 上报时长 sportTime
    rep_m = float(run['dist'])                  # 上报里程 meta.distance_m
    rec_m = polyline_length(pts)                # 点串重算里程

    stride = rep_m / run['steps'] if run['steps'] else 0.0
    step_required = max(STEP_FLOOR, int(round(rep_m / 1.20)))   # 人类步幅上限 1.20m
    cadence = run['steps'] / (used_s / 60.0) if used_s else 0.0
    pace = used_s / (rec_m / 1000.0) if rec_m else 0.0          # 用点串里程算的配速
    pace_rep = used_s / (rep_m / 1000.0) if rep_m else 0.0      # 用上报里程算的配速

    start_local = dt.datetime.fromtimestamp(run['start_ts'] / 1000.0)
    end_local = start_local + dt.timedelta(seconds=used_s)
    start_sec = start_local.hour * 3600 + start_local.minute * 60 + start_local.second
    end_sec = end_local.hour * 3600 + end_local.minute * 60 + end_local.second

    segs = [int(x) for x in run['segs']]
    keys_bad = sum(1 for p in pts if set(p.keys()) != set(POINT_KEYS))
    type_bad = sum(1 for p in pts if any(not isinstance(p[k], str) for k in POINT_KEYS))
    prec_bad = sum(1 for p in pts if decimals(p['a']) != 6 or decimals(p['o']) != 6)
    dt_list = [b - a for a, b in zip(ts, ts[1:])]
    dt_med = float(statistics.median(dt_list)) if dt_list else 0.0
    nonint_ratio = sum(1 for t in ts if t % 1000 != 0) / n if n else 0.0
    c_seq = [int(p['c']) for p in pts]
    e_seq = [int(p['e']) for p in pts]

    return {
        'day': run['day'].isoformat(),
        'segments': segs,
        'first_point_offset_ms': ts[0] - int(run['start_ts']),
        'end_ts_minus_start_ms': int(run['end_ts']) - int(run['start_ts']),
        'reported_pace_str': (run.get('meta') or {}).get('pace'),
        'start_local': start_local.strftime('%Y-%m-%d %H:%M:%S'),
        'end_local': end_local.strftime('%Y-%m-%d %H:%M:%S'),
        'start_ts_ms': int(run['start_ts']),
        'end_ts_ms': int(run['end_ts']),
        'point_count': n,
        'duration_s': round(used_s, 3),
        'point_span_s': round(span_s, 3),
        'span_vs_used_ms': round((used_s - span_s) * 1000.0, 1),
        'distance_recomputed_m': round(rec_m, 2),
        'distance_reported_m': round(rep_m, 2),
        'distance_delta_ratio': round((rec_m - rep_m) / rep_m, 6) if rep_m else 1.0,
        'avg_speed_mps': round(rec_m / used_s, 4) if used_s else 0.0,
        'pace_s_per_km': round(pace, 1),
        'pace_s_per_km_reported': round(pace_rep, 1),
        'pace_in_rule': PACE_MIN_S <= pace <= PACE_MAX_S,
        'steps': int(run['steps']),
        'step_required': step_required,
        'step_number_sum': sum(segs),
        'step_floor': STEP_FLOOR,
        'step_margin_pct': round((run['steps'] - STEP_FLOOR) / STEP_FLOOR * 100.0, 2),
        'stride_m': round(stride, 3),
        'cadence_spm': round(cadence, 1),
        'start_hhmm': start_local.strftime('%H:%M'),
        'end_hhmm': end_local.strftime('%H:%M'),
        'start_in_window': WIN_START_S <= start_sec < WIN_END_S,
        'end_same_day': end_local.date() == start_local.date(),
        'end_before_2100': end_sec <= WIN_END_S,
        'buckets': {
            'count': len(segs),
            'min': min(segs[:-1]) if len(segs) > 1 else (segs[0] if segs else 0),
            'max': max(segs[:-1]) if len(segs) > 1 else (segs[0] if segs else 0),
            'sum': sum(segs),
            'all_in_range': (all(BUCKET_MIN <= b <= BUCKET_MAX for b in segs[:-1])
                             and bool(segs) and 1 <= segs[-1] <= BUCKET_MAX),
            'count_in_range': BUCKET_CNT_MIN <= len(segs) <= BUCKET_CNT_MAX,
            'sum_matches_steps': sum(segs) == int(run['steps']),
        },
        'schema': {
            'keys_exact': keys_bad == 0,
            'all_strings': type_bad == 0,
            'coord_6dp': prec_bad == 0,
            'keys_bad': keys_bad,
            'type_bad': type_bad,
            'precision_bad': prec_bad,
            'dt_median_ms': dt_med,
            'non_integer_second_ratio': round(nonint_ratio, 4),
        },
        'extras': {
            'c_monotonic': all(b >= a for a, b in zip(c_seq, c_seq[1:])),
            'e_monotonic': all(b >= a for a, b in zip(e_seq, e_seq[1:])),
            'e_last': e_seq[-1] if e_seq else 0,
            'e_last_matches_steps': (e_seq[-1] if e_seq else -1) == int(run['steps']),
            'pause_tail': pts[-1]['i'] == 'true' if pts else False,
            'stride_base_m_in_effect': round(STRIDE_BASE_M, 3),
        },
    }


# ---------------------------------------------------------------- 判定
def judge(m: dict) -> list:
    b = m['buckets']
    s = m['schema']
    checks = []

    def add(code, label, ok, value, limit, margin=None, level='hard', unit=''):
        checks.append({'code': code, 'label': label, 'ok': bool(ok), 'value': value,
                       'limit': limit, 'margin': margin, 'level': level, 'unit': unit})

    add('point_count', '点数下限', m['point_count'] >= POINT_MIN, m['point_count'],
        f'>={POINT_MIN}', (m['point_count'] - POINT_MIN) / POINT_MIN, unit='pt')
    add('point_count_high', '点数上限', m['point_count'] <= POINT_MAX, m['point_count'],
        f'<={POINT_MAX}', (POINT_MAX - m['point_count']) / POINT_MAX, 'advisory', 'pt')

    add('duration', '时长>0', m['duration_s'] > 0, m['duration_s'], '>0', None)
    _dd = m['distance_delta_ratio']
    add('distance_delta', f'点串累加略长于上报(+{DELTA_MIN:.1%}~+{DELTA_MAX:.0%})',
        DELTA_MIN <= _dd <= DELTA_MAX, round(_dd * 100, 4), f'{DELTA_MIN * 100:.1f}%~{DELTA_MAX * 100:.0f}%',
        min(max(0.0, _dd - DELTA_MIN), max(0.0, DELTA_MAX - _dd)) / (DELTA_MAX - DELTA_MIN), unit='%')

    add('range_low', '里程≥2.000km', m['distance_reported_m'] >= RANGE_SETTING_M,
        m['distance_reported_m'], f'>={RANGE_SETTING_M:.0f}',
        (m['distance_reported_m'] - RANGE_SETTING_M) / RANGE_SETTING_M, unit='m')
    add('range_buffer', '里程2%余量(2040m)', m['distance_reported_m'] >= RANGE_SETTING_M * RANGE_BUFFER,
        m['distance_reported_m'], f'>={RANGE_SETTING_M * RANGE_BUFFER:.0f}',
        (m['distance_reported_m'] - RANGE_SETTING_M * RANGE_BUFFER) / (RANGE_SETTING_M * RANGE_BUFFER),
        'advisory', 'm')
    add('range_high', '里程≤3km', m['distance_reported_m'] <= 3000, m['distance_reported_m'],
        '<=3000', (3000 - m['distance_reported_m']) / 3000, 'advisory', 'm')

    add('pace_rule', '配速180-600s/km', m['pace_in_rule'], m['pace_s_per_km'], '180-600',
        nearest_margin(m['pace_s_per_km'], PACE_MIN_S, PACE_MAX_S) if m['pace_in_rule'] else -1.0,
        unit='s/km')
    add('speed_limit', '平均速度<3.33m/s', m['avg_speed_mps'] < LIMIT_SPEED, m['avg_speed_mps'],
        f'<{LIMIT_SPEED}', (LIMIT_SPEED - m['avg_speed_mps']) / LIMIT_SPEED, unit='m/s')

    add('steps_low', '步数≥max(1000, 里程/1.20)', m['steps'] >= m['step_required'], m['steps'],
        f">={m['step_required']}", (m['steps'] - m['step_required']) / m['step_required'], unit='步')
    add('stride', f'步幅{STRIDE_MIN}-{STRIDE_MAX}m', STRIDE_MIN <= m['stride_m'] <= STRIDE_MAX,
        m['stride_m'], f'{STRIDE_MIN}-{STRIDE_MAX}',
        nearest_margin(m['stride_m'], STRIDE_MIN, STRIDE_MAX), unit='m')
    add('cadence', f'步频{CADENCE_MIN:.0f}-{CADENCE_MAX:.0f}spm',
        CADENCE_MIN <= m['cadence_spm'] <= CADENCE_MAX, m['cadence_spm'],
        f'{CADENCE_MIN:.0f}-{CADENCE_MAX:.0f}',
        nearest_margin(m['cadence_spm'], CADENCE_MIN, CADENCE_MAX), unit='spm')

    add('start_window', '起跑06:00-21:00', m['start_in_window'], m['start_hhmm'], '06:00-21:00',
        min(_hms_to_s(m['start_hhmm']) - WIN_START_S, WIN_END_S - _hms_to_s(m['start_hhmm'])) / 3600.0,
        unit='h')
    end_ok = m['end_same_day'] and m['end_before_2100']
    add('end_window', '结束≤21:00同日', end_ok,
        m['end_hhmm'] + ('' if m['end_same_day'] else '+跨日'), '<=21:00',
        (WIN_END_S - _hms_to_s(m['end_hhmm'])) / 3600.0 if m['end_same_day'] else -1.0, unit='h')

    worst_bucket = min((nearest_margin(x, BUCKET_MIN, BUCKET_MAX) for x in
                        ([b['min']] + [b['max']])), default=0.0)
    add('bucket_range', f'桶{BUCKET_MIN}-{BUCKET_MAX}(末段≤{BUCKET_MAX})', b['all_in_range'],
        f"{b['min']}-{b['max']}", f'{BUCKET_MIN}-{BUCKET_MAX}', worst_bucket, unit='步')
    add('bucket_count', f'桶数{BUCKET_CNT_MIN}-{BUCKET_CNT_MAX}', b['count_in_range'], b['count'],
        f'{BUCKET_CNT_MIN}-{BUCKET_CNT_MAX}',
        nearest_margin(b['count'], BUCKET_CNT_MIN, BUCKET_CNT_MAX) if b['count_in_range'] else -1.0,
        unit='段')
    add('bucket_sum', '桶和=总步数', b['sum_matches_steps'], f"{b['sum']}/{m['steps']}", '相等', None)

    add('schema_keys', '点9字段', s['keys_exact'], f"{len(POINT_KEYS)}键", 'a c e i l m o s t', None)
    add('schema_types', '全字符串', s['all_strings'], '是' if s['all_strings'] else '否', 'str', None)
    add('schema_precision', '坐标6位小数', s['coord_6dp'], '是' if s['coord_6dp'] else '否', '6dp', None)
    add('dt_median', 'dt中位1000ms', s['dt_median_ms'] == 1000, s['dt_median_ms'], '=1000', None, unit='ms')
    add('dt_nonint', f'非整秒≤{NONINT_MAX:.0%}', s['non_integer_second_ratio'] <= NONINT_MAX,
        round(s['non_integer_second_ratio'] * 100, 3), f'<={NONINT_MAX:.0%}',
        (NONINT_MAX - s['non_integer_second_ratio']) / NONINT_MAX, unit='%')
    add('steps_sum_e', '末点e=总步数', m['extras']['e_last_matches_steps'],
        f"{m['extras']['e_last']}/{m['steps']}", '相等', None)
    return checks


def tightest(checks: list):
    hard = [c for c in checks if c['level'] == 'hard']
    fails = [c for c in hard if not c['ok']]
    if fails:
        pool = [c for c in fails if c['margin'] is not None]
        return min(pool, key=lambda c: c['margin']) if pool else fails[0]
    pool = [c for c in hard if c['margin'] is not None]
    return min(pool, key=lambda c: c['margin']) if pool else None


# ---------------------------------------------------------------- 表格
def _w(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1 for ch in text)


def _pad(text: str, width: int, align: str = '<') -> str:
    text = str(text)
    gap = max(0, width - _w(text))
    if align == '>':
        return ' ' * gap + text
    if align == '^':
        left = gap // 2
        return ' ' * left + text + ' ' * (gap - left)
    return text + ' ' * gap


def render_table(headers, rows, aligns):
    widths = [max([_w(str(h))] + [_w(str(r[i])) for r in rows]) for i, h in enumerate(headers)]
    out = ['  '.join(_pad(h, widths[i], '^') for i, h in enumerate(headers))]
    out.append('  '.join('-' * widths[i] for i in range(len(headers))))
    for row in rows:
        out.append('  '.join(_pad(str(row[i]), widths[i], aligns[i]) for i in range(len(headers))))
    return '\n'.join(out)


# ---------------------------------------------------------------- 主流程
def main() -> int:
    cfg = config.load()
    ap = argparse.ArgumentParser(prog='verify_plan.py', description='独立复核 plan.json 的补跑记录')
    ap.add_argument('--plan', default='', help='计划文件（默认取 config.json 的 plan_file）')
    ap.add_argument('--seed', type=int, default=20261120, help='现场重生成时的随机种子')
    ap.add_argument('--out', default=os.path.join('logs', 'plan_verify.json'), help='复核报告落盘路径')
    ap.add_argument('--no-fix-noise', action='store_true',
                    help='不接管 random.Random()，保留生成器原始随机噪声（结果不可复现）')
    args = ap.parse_args()
    args.plan = args.plan or (cfg.get('plan_file') or 'plan.json')

    try:
        records_in, has_points = load_plan(args.plan)
    except (OSError, ValueError) as exc:
        print(f'⚠️ 读不到计划 {args.plan}：{type(exc).__name__}: {exc}')
        print('   先生成一份：python backfill.py --count 14 --dry-run')
        return 2
    if not records_in:
        print(f'⚠️ 计划 {args.plan} 里没有记录。先跑 python backfill.py --count 14 --dry-run')
        return 2

    mode = '落盘点串（离线复核）' if has_points else '仅日期→现场重生成'
    ref, refrec = ([], None)
    setting = backfill.build_setting(None)
    rng = random.Random(args.seed)
    random.seed(args.seed)
    if has_points:
        setting = backfill.build_setting(None)
    else:
        if not args.no_fix_noise:
            install_deterministic_random(args.seed)
        ref, refrec = backfill.load_reference()
        setting = backfill.build_setting(refrec)
        fence = backfill._norm_fence(setting.get('geofence'))
        if not fence:
            lat0, lon0 = backfill._cfg_center()
            fence = backfill._circle_fence(lat0, lon0, backfill.DEFAULT_FENCE_RADIUS_M)
            print(f'⚠️ 没有配置围栏，重生成用 ({lat0:.4f}, {lon0:.4f}) 半径 '
                  f'{backfill.DEFAULT_FENCE_RADIUS_M:.0f}m 的圆（与 backfill.py 一致）')
        setting['geofence'] = fence

    def gen(day):
        return backfill.build_run(day, ref, setting, rng)

    print('== plan_verify：独立复核（不调用 compliance.check_submission）==')
    print(f'plan={args.plan}  条数={len(records_in)}  输入={mode}  seed={args.seed}  '
          f'rangeSetting={setting["totalRange"]:.3f}km  limitSpeed={setting["limitSpeed"]}  '
          f'STRIDE_BASE_M={STRIDE_BASE_M}')
    print()

    records = []
    for rec in records_in:
        run = normalize(rec, gen)
        m = measure(run)
        checks = judge(m)
        hard = [c for c in checks if c['level'] == 'hard']
        failed = [c for c in hard if not c['ok']]
        t = tightest(checks)
        m['checks'] = checks
        m['failed_checks'] = [c['code'] for c in failed]
        m['verdict'] = 'PASS' if not failed else 'FAIL'
        m['tightest'] = ({'code': t['code'], 'label': t['label'], 'value': t['value'],
                          'limit': t['limit'],
                          'margin_pct': None if t['margin'] is None else round(t['margin'] * 100, 3)}
                         if t else None)
        m['min_hard_margin_pct'] = min(
            (c['margin'] * 100 for c in hard if c['margin'] is not None), default=None)
        records.append(m)

    passed = sum(1 for r in records if r['verdict'] == 'PASS')

    # ---------------- 表 1：运动量与配速
    rows1 = [[
        r['day'], r['point_count'], f"{r['duration_s']:.0f}",
        f"{r['distance_recomputed_m']:.1f}", f"{r['distance_reported_m']:.1f}",
        f"{r['distance_delta_ratio'] * 100:.3f}", f"{r['avg_speed_mps']:.3f}",
        f"{r['pace_s_per_km']:.0f}", 'Y' if r['pace_in_rule'] else 'N',
        r['steps'], f"{r['step_margin_pct']:.1f}", f"{r['stride_m']:.3f}",
        f"{r['cadence_spm']:.1f}", r['start_hhmm'],
        r['end_hhmm'] + ('' if r['end_same_day'] else '!'),
        r['verdict'],
    ] for r in records]
    print('表1  运动量 / 配速 / 步数（Δ% = 点串重算 vs 上报里程）')
    print(render_table(
        ['日期', '点数', '时长s', '重算m', '上报m', 'Δ%', 'm/s', '配速', '180-600',
         '步数', '余量%', '步幅m', '步频', '起跑', '结束', '判定'],
        rows1,
        ['<', '>', '>', '>', '>', '>', '>', '>', '^', '>', '>', '>', '>', '^', '^', '^']))
    print()

    # ---------------- 表 2：结构合规
    rows2 = []
    for r in records:
        b, s = r['buckets'], r['schema']
        rows2.append([
            r['day'], b['count'], f"{b['min']}-{b['max']}",
            ('=' if b['sum_matches_steps'] else '≠') + str(b['sum']),
            'Y' if s['keys_exact'] else f"N({s['keys_bad']})",
            'Y' if s['all_strings'] else f"N({s['type_bad']})",
            'Y' if s['coord_6dp'] else f"N({s['precision_bad']})",
            f"{s['dt_median_ms']:.0f}", f"{s['non_integer_second_ratio'] * 100:.2f}",
            'Y' if (r['start_in_window'] and r['end_same_day'] and r['end_before_2100']) else 'N',
            r['verdict'],
            (f"{r['tightest']['label']} {r['tightest']['margin_pct']:+.1f}%"
             if r['tightest'] and r['tightest']['margin_pct'] is not None
             else (r['tightest']['label'] if r['tightest'] else '-')),
        ])
    print('表2  步数分桶 / 点位结构 / 时间窗 / 最紧余量')
    print(render_table(
        ['日期', '桶段', '桶min-max', '桶Σ', '9键', '全str', '6dp', 'dt中位', '非整秒%',
         '时间窗', '判定', '最紧项'],
        rows2,
        ['<', '>', '^', '>', '^', '^', '^', '>', '>', '^', '^', '<']))
    print()

    # ---------------- 汇总
    flat = []
    for r in records:
        for c in r['checks']:
            if c['level'] == 'hard' and c['margin'] is not None:
                flat.append({'day': r['day'], 'code': c['code'], 'label': c['label'],
                             'value': c['value'], 'limit': c['limit'],
                             'margin_pct': round(c['margin'] * 100, 3)})
    flat.sort(key=lambda x: x['margin_pct'])
    top3 = flat[:3]
    borderline = [x for x in flat if x['margin_pct'] < 5.0]
    borderline_counts: dict = {}
    for x in borderline:
        borderline_counts[x['code']] = borderline_counts.get(x['code'], 0) + 1
    failed_days = [r['day'] for r in records if r['verdict'] == 'FAIL']

    ranges = {}
    for key in ('steps', 'pace_s_per_km', 'stride_m', 'cadence_spm', 'avg_speed_mps',
                'distance_reported_m', 'distance_delta_ratio', 'step_margin_pct'):
        vals = [r[key] for r in records]
        ranges[key] = {'min': min(vals), 'max': max(vals)}

    summary = {
        'plan_count': len(records),
        'pass': passed,
        'fail': len(records) - passed,
        'all_pass': passed == len(records),
        'failed_days': failed_days,
        'tightest_three': top3,
        'borderline_under_5pct': borderline,
        'borderline_counts': borderline_counts,
        'has_borderline': bool(borderline),
        'extremes': ranges,
        'checks_per_record': len(records[0]['checks']) if records else 0,
        'stride_base_m': STRIDE_BASE_M,
        'input_mode': mode,
        'thresholds': {
            'step_floor': STEP_FLOOR,
            'pace_s_per_km': [PACE_MIN_S, PACE_MAX_S],
            'limit_speed_mps': LIMIT_SPEED, 'range_setting_m': RANGE_SETTING_M,
            'range_buffer_m': RANGE_SETTING_M * RANGE_BUFFER,
            'stride_m': [STRIDE_MIN, STRIDE_MAX], 'cadence_spm': [CADENCE_MIN, CADENCE_MAX],
            'bucket_steps': [BUCKET_MIN, BUCKET_MAX],
            'bucket_count': [BUCKET_CNT_MIN, BUCKET_CNT_MAX],
            'distance_delta': [DELTA_MIN, DELTA_MAX],
            'nonint_ratio': NONINT_MAX, 'window': '06:00-21:00',
        },
    }

    payload = {
        'meta': {
            'generated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'plan_file': args.plan,
            'input_mode': mode,
            'seed': args.seed,
            'reference_points': len(ref),
            'reference_record_id': (refrec or {}).get('id'),
            'setting': {k: setting[k] for k in
                        ('totalRange', 'startQualifiedTime', 'endQualifiedTime', 'step',
                         'limitSpeed', 'limitStride', 'limitStepFrequency')},
            'note': '指标全部由 verify_plan.py 自行重算，未调用 compliance.check_submission',
        },
        'records': records,
        'summary': summary,
    }
    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)

    print(f'判定汇总: PASS {passed}/{len(records)}'
          + (f'，FAIL: {", ".join(failed_days)}' if failed_days else '，全部通过'))
    print('全批最紧三项:')
    for i, x in enumerate(top3, 1):
        print(f'  {i}. {x["day"]}  {x["label"]}={x["value"]} (判线 {x["limit"]})  '
              f'余量 {x["margin_pct"]:+.2f}%')
    if borderline:
        grouped = '，'.join(f'{k}×{v}' for k, v in
                            sorted(borderline_counts.items(), key=lambda kv: -kv[1]))
        print(f'贴线(<5%)条目 {len(borderline)} 个（按判项: {grouped}）:')
        for x in borderline[:10]:
            print(f'  - {x["day"]}  {x["label"]}={x["value"]}  余量 {x["margin_pct"]:+.2f}%')
        if len(borderline) > 10:
            print(f'  … 其余 {len(borderline) - 10} 条见 {args.out} summary.borderline_under_5pct')
    else:
        print('贴线(<5%)条目: 无')
    for r in records:
        if r['verdict'] == 'FAIL':
            print(f'  !! {r["day"]} FAIL: ' + '; '.join(
                f'{c["code"]}({c["value"]})' for c in r['checks']
                if c['level'] == 'hard' and not c['ok']))
    print(f'写出 {args.out}（{os.path.getsize(args.out)} bytes）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
