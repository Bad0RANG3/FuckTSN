#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把样本坐标整体搬到一个与任何真实校区无关的演示位置（北京大学一带）。

做法是球面刚体旋转：把"当前样本的第一点"旋到固定锚点 ``ANCHOR``。
旋转是等距变换（isometry），任意两点的 haversine 距离保持不变，
所以样本的链长、步幅、配速等指标全部不变，只是地图上的位置换了。

幂等：第二次运行时第一点已经等于锚点，算出来的旋转是单位矩阵，文件不变。

    python tools/reanchor.py --check   # 只报还需要搬多少点
    python tools/reanchor.py           # 执行
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = [
    'tests/fixtures/records_qualified.json',
    'tests/fixtures/record_banned.json',
    'tests/fixtures/records_listview.json',
    'tests/fixtures/submit_legacy_2023.json',
]
TEXT_TARGETS = [
    'tools/snapshot_legacy.py',
    'tests/test_legacy_equivalence.py',
    'tests/test_fields.py',
    'tests/test_compliance.py',
    'docs/合格规格.md',
    'docs/字段说明.md',
    'README.md',
]
ANCHOR = (39.9928, 116.3055)          # 北京大学一带，纯演示用

NUM_BODY = r'-?\d{1,3}\.\d{3,}'


def _unit(lat: float, lon: float):
    la, lo = math.radians(lat), math.radians(lon)
    return (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))


def _latlon(v):
    x, y, z = v
    return (math.degrees(math.asin(max(-1.0, min(1.0, z)))),
            math.degrees(math.atan2(y, x)))


def _rotation(src, dst):
    a, b = _unit(*src), _unit(*dst)
    v = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])
    s = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    c = a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    if s < 1e-12:
        if c > 0:                      # 已经是锚点
            return None
        raise SystemExit('锚点与样本点正好对跖，无法用一次旋转对齐')
    k = ((1.0 - c) / (s * s))
    vx, vy, vz = v

    def apply(p):
        # Rodrigues: R p = c*p + (v × p) + k*(v·p)*v，k = (1-c)/|v|²
        cross = (vy * p[2] - vz * p[1], vz * p[0] - vx * p[2], vx * p[1] - vy * p[0])
        vdot = vx * p[0] + vy * p[1] + vz * p[2]
        return tuple(c * p[i] + cross[i] + k * vdot * v[i] for i in range(3))

    return apply


def _walk(node, rot, stats):
    """递归旋转 dict 里的点（a/o、lat/lng、latitude/longitude）与坐标数组。"""
    if isinstance(node, dict):
        if 'a' in node and 'o' in node:
            lat, lon = float(node['a']), float(node['o'])
            new_lat, new_lon = _latlon(rot(_unit(lat, lon)))
            node['a'] = '%.6f' % new_lat
            node['o'] = '%.6f' % new_lon
            stats['point'] += 1
        for lat_key, lon_key, digits in (('lat', 'lng', 9), ('latitude', 'longitude', 9)):
            if lat_key in node and lon_key in node:
                lat, lon = float(node[lat_key]), float(node[lon_key])
                new_lat, new_lon = _latlon(rot(_unit(lat, lon)))
                node[lat_key] = round(new_lat, digits)
                node[lon_key] = round(new_lon, digits)
                stats['ring'] += 1
        for key, val in list(node.items()):
            _walk(val, rot, stats)
    elif isinstance(node, list):
        if len(node) == 2 and all(isinstance(v, (int, float)) for v in node) \
                and abs(node[0]) <= 90 and abs(node[1]) <= 180 and (node[0] or node[1]):
            new_lat, new_lon = _latlon(rot(_unit(float(node[0]), float(node[1]))))
            node[0], node[1] = round(new_lat, 6), round(new_lon, 6)
            stats['pair'] += 1
            return
        for item in node:
            _walk(item, rot, stats)


def _centroid(obj):
    """把所有点的三维单位向量取平均，得到样本簇的"重心"，用于推算旋转。"""
    acc = [0.0, 0.0, 0.0]
    count = 0

    def scan(node):
        nonlocal count
        if isinstance(node, dict):
            if 'a' in node and 'o' in node:
                lat, lon = float(node['a']), float(node['o'])
                if abs(lat) <= 90 and abs(lon) <= 180 and (lat or lon):
                    v = _unit(lat, lon)
                    acc[0] += v[0]; acc[1] += v[1]; acc[2] += v[2]
                    count += 1
                return
            for val in node.values():
                scan(val)
        elif isinstance(node, list):
            for item in node:
                scan(item)

    scan(obj)
    if not count:
        return None
    norm = math.sqrt(acc[0] ** 2 + acc[1] ** 2 + acc[2] ** 2) or 1.0
    return _latlon((acc[0] / norm, acc[1] / norm, acc[2] / norm))


LINE_PAIR_RE = re.compile(r'(?<![\d.])(' + NUM_BODY + r'), (' + NUM_BODY + r')(?![\d.])')
DICT_POINT_RE = re.compile(r"'a':\s*'(" + NUM_BODY + r")'(.*?)'o':\s*'(" + NUM_BODY + r")'")


def _rot_point(rot, lat, lon):
    new_lat, new_lon = _latlon(rot(_unit(lat, lon)))
    return new_lat, new_lon


def rewrite_text(path, rot, stats):
    if not os.path.exists(path):
        return
    src = io.open(path, encoding='utf-8').read()
    out_lines = []
    for line in src.split('\n'):
        def fix_dict(m):
            lat, mid, lon = float(m.group(1)), m.group(2), float(m.group(3))
            if abs(lat) > 90 or abs(lon) > 180:
                return m.group(0)
            nl, no = _rot_point(rot, lat, lon)
            stats['point'] += 1
            return "'a': '%0.6f'%s'o': '%0.6f'" % (nl, mid, no)

        line = DICT_POINT_RE.sub(fix_dict, line)

        def fix_pair(m):
            lat, lon = float(m.group(1)), float(m.group(2))
            if abs(lat) > 90 or abs(lon) > 180:
                return m.group(0)
            nl, no = _rot_point(rot, lat, lon)
            stats['pair'] += 1
            return '%0.6f, %0.6f' % (nl, no)

        line = LINE_PAIR_RE.sub(fix_pair, line)
        out_lines.append(line)
    new = '\n'.join(out_lines)
    if new != src:
        if not stats.get('check'):
            io.open(path, 'w', encoding='utf-8', newline='\n').write(new)
        stats['files'].append(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description='把样本坐标搬到演示锚点（等距旋转，不影响距离指标）')
    ap.add_argument('--check', action='store_true', help='只统计，不写文件')
    args = ap.parse_args(argv)

    stats = {'point': 0, 'ring': 0, 'pair': 0, 'files': [], 'check': args.check}

    # 用第一个 fixture 的首点定旋转
    anchor_src = None
    for rel in FIXTURES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        obj = json.load(io.open(path, encoding='utf-8'))
        anchor_src = _centroid(obj)
        if anchor_src:
            break
    if anchor_src is None:
        raise SystemExit('没找到任何样本点')

    if abs(anchor_src[0] - ANCHOR[0]) < 0.02 and abs(anchor_src[1] - ANCHOR[1]) < 0.02:
        print('样本重心已在演示锚点附近（%.4f,%.4f），无需处理' % anchor_src)
        return 0

    rot = _rotation(anchor_src, ANCHOR)
    if rot is None:
        print('已位于演示锚点 %.4f,%.4f，无需处理' % ANCHOR)
        return 0

    for rel in FIXTURES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        obj = json.load(io.open(path, encoding='utf-8'))
        before = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        _walk(obj, rot, stats)
        after = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        if before != after:
            if not args.check:
                io.open(path, 'w', encoding='utf-8', newline='\n').write(
                    json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True))
            stats['files'].append(path)

    for rel in TEXT_TARGETS:
        rewrite_text(os.path.join(ROOT, rel), rot, stats)

    mode = '需要搬' if args.check else '已搬'
    print('%s：点位 %d、围栏角 %d、代码里的坐标对 %d，文件 %d 个' %
          (mode, stats['point'], stats['ring'], stats['pair'], len(stats['files'])))
    for f in stats['files']:
        print('  -', os.path.relpath(f, ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
