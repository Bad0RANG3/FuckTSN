# -*- coding: utf-8 -*-
"""用真实抓取的记录回放门禁，检验"会不会误杀 / 能不能识别"。

用法::

    python tools/audit_gates.py            # 全部回放
    python tools/audit_gates.py -v         # 连警告一起打印

断言两件事：

1. **不误杀**：5 条服务器判定为合格的记录（含 2 条带账号级黑名单后缀但 verdict=合格），
   在 ``balanced`` 模式下必须 ``ok=True``（硬闸门全过）。
2. **能识别**：5 条被拒记录必须判不通过，且命中与服务器 remark 对应的那条闸门::

       运动步数不符合  → step_setting_low
       人脸对比超出…   → face_status
       数据异常-作弊嫌疑 → identify / pace_format
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tsn import compliance  # noqa: E402

FIXTURES = ROOT / 'tests' / 'fixtures'
PLACEHOLDER_IDENTIFY = '93e09535-aea8-5005-8cdd-2533746aa284'

EXPECT = {
    '9420518228053629781': set(),                       # 自由跑，不计成绩，不是拒绝
    '9114388484495371621': set(),                       # 合格
    '9139855955804789768': set(),                       # 合格10-黑名单15（verdict 仍是合格）
    '9135904292753296251': set(),                       # 合格131310-黑名单15
    '9582671916210712292': {'identify', 'pace_format'},  # 数据异常-作弊嫌疑
    '9956093493943514196': {'step_setting_low'},        # 运动步数不符合
    '9389972479532754524': {'step_setting_low'},        # 运动步数不符合
    '9473049021110561641': {'face_status'},             # 人脸对比超出规定范围
}

STEP_SETTING = 1000
RANGE_SETTING = 2.000


def build_meta(record: dict, *, face_ok: bool | None = None) -> dict:
    steps = int(record.get('step') or 0)
    raw_range = record.get('sportRange') or '0'
    distance = float(raw_range) * 1000.0
    elapsed = float(record.get('sportTime') or 0)
    identify = record.get('identify') or ''
    if not compliance.UUID_RE.match(identify):
        identify = identify if identify == '1' else PLACEHOLDER_IDENTIFY
    meta = {
        'distance_m': distance,
        'sport_time_s': elapsed,
        'total_steps': steps,
        'identify': identify,
        'pace': record.get('speed') or '',
        'sport_type': record.get('sportType'),
        'required_range_m': RANGE_SETTING * 1000.0,
        'range_setting': RANGE_SETTING,
        'step_setting': STEP_SETTING,
        'start_qualified_min': int(record.get('startQualifiedTimeSetting') or 6),
        'end_qualified_min': int(record.get('endQualifiedTimeSetting') or 21),
        'face_ok': (record.get('isFaceStatus') == '1') if face_ok is None else face_ok,
        'ok_point_count': 3 if record.get('reachRange') == record.get('sportRange') else 0,
        'start_ts_ms': (int(record['startTime']) if record.get('startTime') else 0)
        * (1000 if len(str(record.get('startTime', ''))) == 10 else 1),
        'end_ts_ms': (int(record['endTime']) if record.get('endTime') else 0)
        * (1000 if len(str(record.get('endTime', ''))) == 10 else 1),
        'app_version': record.get('appVersion'),
        'brand': record.get('brand'),
        'model': record.get('model'),
        'system': record.get('system'),
        'osver': record.get('version'),
    }
    return meta


def points_for(record: dict) -> list:
    return record.get('gitudeLatitude') or []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    listview = json.loads((FIXTURES / 'records_listview.json').read_text(encoding='utf-8'))
    detail = json.loads((FIXTURES / 'records_qualified.json').read_text(encoding='utf-8'))

    failures = 0
    print(f'{"id":>20} {"remark":<34} {"ok":>5}  致命闸门')
    print('-' * 96)
    for rid, record in listview.items():
        merged = dict(record)
        merged.update({k: v for k, v in detail.get(rid, {}).items() if k == 'gitudeLatitude'})
        points = points_for(merged)
        meta = build_meta(merged)
        report = compliance.check_submission(points, record.get('stepNumbers'), meta, history=[])
        # 列表视图没有点位，只能用标量闸门判定
        point_codes = {'empty_points', 'chain_short', 'chain_over_hint', 'point_count',
                       'fence_outside', 'pause_ratio_hint', 'still_ratio_hint', 't_jitter'}
        codes = {i.code for i in report.errors}
        if not points:
            codes -= point_codes
        expected = EXPECT.get(rid, set())
        if str(record.get('sportType')) == '0':
            good = True                     # 自由跑：不计成绩，不参与判定
            shown = '<自由跑>'
        elif expected:
            good = bool(codes & expected)
            shown = None
        else:
            good = not codes
            shown = None
        shown = shown or (','.join(sorted(codes)) or '—')
        if not good:
            failures += 1
        remark = (record.get('remark') or '')[:32]
        print(f'{rid:>20} {remark:<34} {str(good):>5}  {shown}')
        if args.verbose or not good:
            for line in report.detail_lines():
                print(line)

    # 5 条详情记录（4984 点）单独跑一遍：它们没有设备画像，必须靠点位与标量过关
    print()
    print('详情记录（4984 点）逐条：')
    for rid, record in detail.items():
        record = dict(record)
        record.setdefault('identify', PLACEHOLDER_IDENTIFY)
        record.setdefault('isFaceStatus', '1')
        record.setdefault('appVersion', '3.6.814')
        record.setdefault('brand', 'iPhone')
        record.setdefault('model', 'iPhone 17 Pro')
        record.setdefault('system', 'iOS')
        record.setdefault('version', '27.0.1')
        record.setdefault('sportType', '1')
        record.setdefault('reachRange', record.get('sportRange'))
        report = compliance.check_submission(record['gitudeLatitude'], record.get('stepNumbers'),
                                             build_meta(record), history=[])
        flag = 'OK ' if report.ok else 'FAIL'
        if not report.ok:
            failures += 1
        print(f'  {flag} {rid} {record.get("sportRange")}km {record.get("step")}步 '
              f'{record.get("sportTime")}s {sorted({i.code for i in report.errors}) or "—"}')
        if args.verbose:
            for line in report.detail_lines():
                print('     ' + line)

    print()
    print('结论：' + ('全部符合预期' if failures == 0 else f'{failures} 条不符合预期'))
    return 0 if failures == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
