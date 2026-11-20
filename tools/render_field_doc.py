"""把 tsn_fields 的字段表渲染成 docs/字段对照.md。

用法：``python tools/render_field_doc.py``（在 D:\\TSN\\pub 下执行）
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tsn import fields  # noqa: E402


def main() -> int:
    target = ROOT / 'docs' / '字段对照.md'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(fields.to_markdown(), encoding='utf-8', newline='\n')
    print(f'wrote {target}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
