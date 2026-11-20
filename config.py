# -*- coding: utf-8 -*-
"""本地配置层（纯标准库，无第三方依赖）。

原先散落在 backfill.py / progress.py / verify_plan.py 里的
「学校 ID / 学校代码 / 服务地址 / 账号文件 / 计划文件 / 台账文件」全部收敛到这里：

    config.json                     # 真实值，已列入 .gitignore
    config.example.json             # 占位值，可以入库
    TSN_CONFIG=/path/to/x.json      # 指定另一份配置（CI / 多账号）

用法::

    import config
    cfg = config.load()             # dict，缺失键自动补 DEFAULTS
    config.get('school_id')         # 便捷访问
    config.config_path()            # 当前生效的配置文件路径

本模块只负责「读」，不打印任何字段值：配置里可能出现账号文件路径、学校代码等
本地信息，需要打进日志的字段请调用方自己用 `mask()` 脱敏。
"""
from __future__ import annotations

import json
import os

DEFAULTS = {
    'school_id': 0, 'school_code': '', 'base_url': 'https://h.tsnkj.com',
    'account_file': 'account.txt', 'plan_file': 'plan.json',
    'history_file': 'run_history.json', 'ledger_file': 'logs/backfill_done.json',
}

ENV_VAR = 'TSN_CONFIG'
DEFAULT_PATH = 'config.json'

# path(abspath) -> (mtime_ns, cfg)；只为省掉循环里反复读盘，文件一改就失效
_CACHE: dict = {}


def config_path() -> str:
    """当前生效的配置文件路径（TSN_CONFIG 优先，否则 ./config.json）。"""
    return os.environ.get(ENV_VAR) or DEFAULT_PATH


def _coerce(key: str, value):
    """按默认值的类型收敛，避免 "42" 这种字符串进到需要 int 的地方。"""
    default = DEFAULTS.get(key)
    if key == 'school_id':
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str) and value is None:
        return ''
    return value


def load(path: str | None = None) -> dict:
    """读配置：文件不存在 / 解析失败 / 不是对象时，静默回落到 DEFAULTS。"""
    target = path or config_path()
    cfg = dict(DEFAULTS)
    if not target:
        return cfg

    real = os.path.abspath(target)
    try:
        stamp = os.stat(real).st_mtime_ns
    except OSError:
        return cfg

    cached = _CACHE.get(real)
    if cached and cached[0] == stamp:
        return dict(cached[1])

    try:
        with open(real, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg

    for key, value in raw.items():
        cfg[key] = _coerce(key, value) if key in DEFAULTS else value
    _CACHE[real] = (stamp, dict(cfg))
    return cfg


def get(key: str, default=None):
    """便捷访问：等价于 load().get(key, default)。"""
    return load().get(key, default)


def mask(value, keep: int = 3) -> str:
    """日志用的脱敏：只留首尾各 keep 个字符。"""
    text = '' if value is None else str(value)
    if len(text) <= keep * 2:
        return '*' * len(text)
    return f'{text[:keep]}{"*" * (len(text) - keep * 2)}{text[-keep:]}'


def path_of(key: str) -> str:
    """取路径类配置并展开 ~。"""
    return os.path.expanduser(str(DEFAULTS.get(key) if get(key) is None else get(key)))
