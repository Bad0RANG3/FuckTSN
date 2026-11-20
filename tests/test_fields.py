"""字段对照 + 迁移 + 体检的回归测试。

数据全部来自真实抓取（repo/_probe），因此这些断言同时也是"判定规则"的取证：
- records_qualified.json：5 条服务器的合格记录（详情视图，含点位）
- records_listview.json：8 条列表视图记录（含设备画像、remark、status、reachRange）
- record_banned.json：被"数据异常-作弊嫌疑"处罚的那条
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tsn import fields

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding='utf-8'))


def _qualified():
    return list(_load('records_qualified.json').values())


def _listview():
    return list(_load('records_listview.json').values())


def _legacy_submit():
    return _load('submit_legacy_2023.json')


# ---------------------------------------------------------------------------
# 一、合格记录的点位形态（4984 个真实点）
# ---------------------------------------------------------------------------

def test_qualified_records_pass_point_audit():
    for record in _qualified():
        issues = fields.audit_points(record['gitudeLatitude'])
        errors = [i for i in issues if i.level == 'error']
        assert not errors, f"{record['id']}: {[e.message for e in errors]}"


def test_qualified_records_point_shape():
    total = 0
    for record in _qualified():
        points = record['gitudeLatitude']
        total += len(points)
        assert all(set(p) == set(fields.POINT_KEYS_NEW) for p in points)
        assert all(isinstance(v, str) for p in points for v in p.values())
        assert points[-1]['i'] == 'true'
        assert all(p['m'] == '0' for p in points)
        assert all(isinstance(p['t'], str) and p['t'].isdigit() for p in points)
    assert total == 4984


def test_qualified_records_step_numbers_sum_to_step():
    for record in _qualified():
        assert sum(json.loads(record['stepNumbers'])) == int(record['step'])


# ---------------------------------------------------------------------------
# 二、列表视图：判定结论长什么样
# ---------------------------------------------------------------------------

def test_listview_device_profile_is_iphone():
    for record in _listview():
        if record['appVersion'] == '2.0.23':      # 唯一一条旧版记录
            assert record['brand'] == 'Xiaomi'
            continue
        assert record['brand'] == 'iPhone'
        assert record['model'] == 'iPhone 17 Pro'
        assert record['system'] == 'iOS'
        assert record['version'] in ('27.0.1', '27.3')
        assert record['appVersion'] == '3.6.814'


def test_listview_identify_is_uuid_except_banned():
    for record in _listview():
        if record['id'] == '9582671916210712292':
            assert record['identify'] == '1'
        else:
            assert fields.UUID_RE.match(record['identify']), record['identify']


def test_reach_range_equals_sport_range_only_when_counted():
    for record in _listview():
        if record['status'] == '1' or record['remark'].startswith('合格'):
            assert record['reachRange'] == record['sportRange'], record['id']
        else:
            assert record['reachRange'] == '0.000', record['id']


def test_step_gate_is_step_setting_not_stride_window():
    """999 步被判「运动步数不符合」；1075/1099/2109 步同样距离却合格。"""
    for record in _listview():
        steps = int(record['step'])
        if record['remark'].startswith('运动步数不符合'):
            assert steps < 1000
        elif record['remark'].startswith('合格'):
            assert steps >= 1000


def test_face_gate_message():
    face = [r for r in _listview() if r['remark'].startswith('人脸对比超出规定范围')]
    assert len(face) == 1
    assert face[0]['isFaceStatus'] == '0'
    assert '人脸识别' in face[0]['remarkExp']


def test_cheat_suspect_signature():
    banned = _load('record_banned.json')
    assert banned['remark'].startswith('数据异常-作弊嫌疑')
    assert banned['appVersion'] == '2.0.23'
    assert banned['brand'] == 'Xiaomi'
    assert banned['identify'] == '1'
    assert banned['speed'] == "10'00\""          # ASCII 引号，不是 ′″
    assert banned.get('gitudeLatitude') is None   # 服务端把点位整段剔除了


def test_penalty_suffix_is_account_level():
    penalized = [r for r in _listview() if r['remark'].endswith('-黑名单15')]
    assert len(penalized) == 6
    # 其中有 remark 已经是「合格」的，说明后缀是账号级处罚，不是该条的判定
    assert any(r['remark'].startswith('合格') for r in penalized)


# ---------------------------------------------------------------------------
# 三、旧报文 → 新报文
# ---------------------------------------------------------------------------

def test_migrate_legacy_replaces_device_profile():
    new = fields.migrate_legacy(_legacy_submit())
    for key, want in fields.NEW_DEVICE_PROFILE.items():
        assert new[key] == want


def test_migrate_legacy_fixes_identify_and_pace():
    new = fields.migrate_legacy(_legacy_submit())
    assert fields.UUID_RE.match(new['identify'])
    assert fields.PACE_RE.match(new['speed'])
    assert '\u2032' in new['speed'] and '\u2033' in new['speed']


def test_migrate_legacy_keeps_identify_when_uuid():
    payload = dict(_legacy_submit(), identify='7dacd1d0-09fd-5306-82d5-9145dbd1d461')
    assert fields.migrate_legacy(payload)['identify'] == '7dacd1d0-09fd-5306-82d5-9145dbd1d461'


def test_migrate_legacy_relabels_types():
    new = fields.migrate_legacy(_legacy_submit())
    assert new['gpsDistance'] == new['sportRange']
    assert new['sportRange'] == '2.00'
    assert 'points' not in new and 'formatSportRange' not in new
    assert new['okPointList'] == ''            # 旧脚本的 '[]' 归一成空串（两者服务器都收）


def test_migrate_legacy_points_become_nine_key_strings():
    new = fields.migrate_legacy(_legacy_submit())
    points = new['gitudeLatitude']
    assert all(set(p) == set(fields.POINT_KEYS_NEW) for p in points)
    assert all(isinstance(v, str) for p in points for v in p.values())
    assert points[0]['a'] == '39.994742'
    assert points[0]['s'] == '2.500000'
    assert int(points[0]['t']) % 1000 == 0
    assert points[-1]['i'] == 'true'
    assert points[-1]['e'] == str(new['step'])


def test_migrated_payload_has_no_error_except_environment():
    new = fields.migrate_legacy(_legacy_submit())
    errors = [i for i in fields.audit_submission(new) if i.level == 'error']
    assert [i.field for i in errors] == ['environment']


def test_migrate_legacy_can_synthesize_environment():
    new = fields.migrate_legacy(_legacy_submit(), device_id='DEVICE_FIXTURE')
    env = new['environment']
    assert env['deviceId'] == 'DEVICE_FIXTURE'
    json.loads(env['oppo'])
    json.loads(env['safe'])
    assert fields.audit_submission(new) == []


def test_migrate_legacy_steps_consistent():
    new = fields.migrate_legacy(_legacy_submit())
    assert sum(json.loads(new['stepNumbers'])) == int(new['step'])


def test_banned_scalars_are_flagged():
    banned = {k: v for k, v in _load('record_banned.json').items() if k != 'gitudeLatitude'}
    payload = dict(banned, environment={}, gitudeLatitude=[])
    errs = {i.field for i in fields.audit_submission(payload) if i.level == 'error'}
    assert {'appVersion', 'brand', 'model', 'system', 'version', 'identify'} <= errs
    assert 'speed' in errs


# ---------------------------------------------------------------------------
# 四、字段表自身
# ---------------------------------------------------------------------------

def test_field_table_covers_core_groups():
    groups = {spec.group for spec in fields.ALL_FIELDS}
    assert groups == {'submit', 'v2base', 'header', 'point', 'echo', 'setting'}
    names = {spec.name for spec in fields.ALL_FIELDS}
    for required in ('sportRange', 'gpsDistance', 'identify', 'environment', 'stepNumbers',
                     'a', 'o', 't', 's', 'c', 'e', 'i', 'l', 'm',
                     'remark', 'sportStatus', 'reachRange', 'totalRange', 'stepSetting'):
        assert required in names


def test_every_field_has_action_and_evidence():
    for spec in fields.ALL_FIELDS:
        assert spec.evidence in ('echo', 'probe', 'doc'), spec
        assert spec.action.strip(), spec


def test_markdown_doc_is_generated_from_table():
    text = fields.to_markdown()
    assert text.startswith('# 字段对照')
    assert text.count('| `') >= len(fields.ALL_FIELDS)
    for title in ('提交报文', 'v2 签名公共字段', '轨迹点', '服务器回显', '服务器设置'):
        assert title in text


# ---------------------------------------------------------------------------
# 五、「必须替换的字段」清单
# ---------------------------------------------------------------------------

def test_must_migrate_resolves_to_real_specs():
    index = {(spec.group, spec.name): spec for spec in fields.ALL_FIELDS}
    assert len(fields.MUST_MIGRATE) == len(set(fields.MUST_MIGRATE))
    for key in fields.MUST_MIGRATE:
        assert key in index, f'MUST_MIGRATE 指向不存在的字段：{key}'
        spec = index[key]
        assert spec.old != spec.new, f'{key} 新旧写法一样，不该留在必改清单'
        assert spec.judged != 'display', f'{key} 只是展示字段，不该留在必改清单'


def test_must_migrate_covers_the_known_breaking_changes():
    names = {name for _, name in fields.MUST_MIGRATE}
    for required in ('appVersion', 'identify', 'speed', 'sportRange', 'gpsDistance',
                     'geofence', 'environment', 'isFaceStatus', 'step', 'stepNumbers',
                     'brand', 'model', 'system', 'version',
                     'a', 'o', 't', 's', 'c', 'e', 'i', 'm'):
        assert required in names, required


def test_must_migrate_excludes_harmless_fields():
    names = {name for _, name in fields.MUST_MIGRATE}
    for harmless in ('token', 'signatureMD5', 'versionCode', 'uniqueCode', 'Cookie', 'school'):
        assert harmless not in names, harmless


def test_markdown_starts_with_migration_checklist():
    text = fields.to_markdown()
    head = text[:text.index('## 一')]
    assert '必须替换的字段' in head
    assert f'共 {len(fields.MUST_MIGRATE)} 个' in head
    rows = [line for line in head.splitlines() if line.startswith('| `')]
    assert len(rows) == len(fields.MUST_MIGRATE)
    index = {(spec.group, spec.name): spec for spec in fields.ALL_FIELDS}
    for (group, name), line in zip(fields.MUST_MIGRATE, rows):
        spec = index[(group, name)]
        assert line.startswith(f'| `{spec.name}` |')
        assert spec.action in line
