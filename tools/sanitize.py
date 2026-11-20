# -*- coding: utf-8 -*-
"""把仓库里的真实抓取数据脱敏成可公开的等价样本。

做三件事，全部是**可复现的确定性变换**，跑多少次结果都一样：

1. 坐标：`lat -> -lat`、`lon -> lon - 100`。
   纬度取负号后 `cos(lat)` 不变，因此任意两点间的 haversine 距离、局部投影距离、
   里程/配速/速度区间**一个数都不变**——测试里那些"精确到小数点后 9 位"的基线
   仍然成立，但地点已经不在原地（落在南大西洋）。
2. 标识：19 位记录 ID、`basUserId`、`identify` UUID、昵称、`userName` 学号，
   全部换成由 sha256/uuid5 推出的假值，且**同一原值在所有文件里映射到同一个假值**。
3. 只改 `FIXTURES` / `TEXT_TARGETS` 里列出的文件，不碰协议常量与真实凭据。

用法::

    python tools/sanitize.py            # 就地脱敏
    python tools/sanitize.py --check    # 只报告还残留多少处，不写盘
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import uuid
from typing import Any, Dict, Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 真实抓取数据所在的经纬度窗口：只有落在这个窗口里的数字才当作坐标处理
LAT_WINDOW = (23.0, 24.0)
LON_WINDOW = (113.0, 114.0)

LAT_FLIP = True
LON_SHIFT = -100.0

# 记录 ID / 账号 ID 形如 13xxxxxxxxxxxxxxxxx（19 位），时间戳是 13 位，不要碰
ID_RE = re.compile(r'(?<!\d)(1[3-9]\d{17})(?!\d)')
UUID_RE = re.compile(r'(?<![0-9a-fA-F])([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
                     r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?![0-9a-fA-F])')
NUM_RE = re.compile(r'(?<![\d.\-])(\d{2,3}\.\d{4,15})(?![\d])')
NEG_RUN_RE = re.compile(r'-{2,}(?=\d)')

# 明文 PII 字段：JSON 里按键替换，文本里按字面替换
PII_KEYS = ('nickName', 'userName')
PII_LITERALS = {
    '2651021900129': '1000000000001',      # userName（学号形状）
}

FIXTURES = [
    'tests/fixtures/records_qualified.json',
    'tests/fixtures/records_listview.json',
    'tests/fixtures/record_banned.json',
    'tests/fixtures/submit_legacy_2023.json',
]
TEXT_TARGETS = [
    'docs/逆向笔记.md',
    'docs/判定链路.md',
    'docs/接口报文与判定.md',
    'docs/合格规格.md',
    'docs/字段对照.md',
    'docs/重构说明.md',
    'README.md',
    'tests/test_fields.py',
    'tests/test_legacy_equivalence.py',
    'tools/snapshot_legacy.py',
    'tools/audit_gates.py',
    'fields.py',
]


# ---------------------------------------------------------------------------
# 确定性映射
# ---------------------------------------------------------------------------

def fake_id(value: str) -> str:
    """19 位假 ID：以 9 开头，后 18 位来自 sha256，保证稳定且不撞号。"""
    digest = hashlib.sha256(('tsn-id:' + value).encode()).hexdigest()
    return '9' + str(int(digest[:16], 16)).zfill(18)[:18]


def fake_uuid(value: str) -> str:
    """UUIDv5 假值；保留原大小写风格，方便验证"大小写不敏感"的结论。

    幂等：服务端下发的都是 v4 UUID，本函数产出 v5，因此再跑一次会原样跳过。
    """
    out = str(uuid.uuid5(uuid.NAMESPACE_URL, 'tsn-uuid:' + value.lower()))
    return out.upper() if value.isupper() else out


def _is_sanitized_uuid(value: str) -> bool:
    return value[14].lower() == '5'


def _decimals(text: str) -> int:
    return len(text.split('.')[1])


def fake_coord(number_text: str) -> str:
    """坐标变换：纬度取负、经度平移，保留原始小数位数。"""
    value = float(number_text)
    digits = _decimals(number_text)
    if LAT_WINDOW[0] <= value <= LAT_WINDOW[1]:
        value = -value if LAT_FLIP else value
    elif LON_WINDOW[0] <= value <= LON_WINDOW[1]:
        value = value + LON_SHIFT
    else:
        return number_text
    return f'{value:.{digits}f}'


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------

def sanitize_text(text: str) -> str:
    # 修掉历史上重复取负留下的双负号（例如 --39.99）：只保留一个负号
    text = NEG_RUN_RE.sub('-', text)
    text = ID_RE.sub(lambda m: fake_id(m.group(1)), text)
    text = UUID_RE.sub(lambda m: m.group(1) if _is_sanitized_uuid(m.group(1)) else fake_uuid(m.group(1)), text)
    text = NUM_RE.sub(lambda m: fake_coord(m.group(1)), text)
    for real, fake in PII_LITERALS.items():
        text = text.replace(real, fake)
    return text


def sanitize_obj(node: Any) -> Any:
    """递归处理 JSON：PII 键整体替换，其余字符串过一遍文本规则。"""
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for key, value in node.items():
            # 顶层键就是记录 ID，要一起脱敏
            key = sanitize_text(key) if isinstance(key, str) else key
            if key in PII_KEYS and isinstance(value, str) and value:
                out[key] = '测试用户' if key == 'nickName' else '1000000000001'
            else:
                out[key] = sanitize_obj(value)
        return out
    if isinstance(node, list):
        return [sanitize_obj(item) for item in node]
    if isinstance(node, str):
        return sanitize_text(node)
    if isinstance(node, float) and (LAT_WINDOW[0] <= node <= LAT_WINDOW[1]
                                    or LON_WINDOW[0] <= node <= LON_WINDOW[1]):
        return round(fake_coord(f'{node:.6f}') and float(fake_coord(f'{node:.6f}')), 6)
    return node


def _read(path: str) -> str:
    with io.open(path, encoding='utf-8') as handle:
        return handle.read()


def _write(path: str, text: str) -> None:
    with io.open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)


def _count_raw(text: str) -> int:
    hits = len(ID_RE.findall(text))
    hits += sum(1 for m in NUM_RE.findall(text)
                if LAT_WINDOW[0] <= float(m) <= LAT_WINDOW[1]
                or LON_WINDOW[0] <= float(m) <= LON_WINDOW[1])
    return hits


def main(argv: Iterable[str]) -> int:
    check_only = '--check' in list(argv)
    total = 0
    for rel in FIXTURES:
        path = os.path.join(ROOT, rel)
        data = json.load(io.open(path, encoding='utf-8'))
        cleaned = sanitize_obj(data)
        before = _count_raw(_read(path))
        if check_only:
            print(f'{rel}: 残留 {before} 处')
            total += before
            continue
        _write(path, json.dumps(cleaned, ensure_ascii=False, separators=(',', ':')))
        print(f'{rel}: 处理 {before} 处')
        total += before
    for rel in TEXT_TARGETS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        text = _read(path)
        before = _count_raw(text)
        if check_only:
            print(f'{rel}: 残留 {before} 处')
            total += before
            continue
        cleaned = sanitize_text(text)
        if cleaned != text:
            _write(path, cleaned)
        print(f'{rel}: 处理 {before} 处')
        total += before
    print(('残留合计 ' if check_only else '处理合计 ') + str(total))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
