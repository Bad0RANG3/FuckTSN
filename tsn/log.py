"""统一日志出口。

优先使用 loguru（项目 requirements 里已锁定），缺少依赖时退回标准库 logging，
保证任何环境下 `from tsn_log import logger` 都能拿到可用的 logger。
"""
from __future__ import annotations

try:  # pragma: no cover - 取决于运行环境是否装了 loguru
    from loguru import logger as _logger
except ImportError:  # pragma: no cover
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(name)s:%(funcName)s:%(lineno)d - %(message)s',
    )
    _logger = logging.getLogger('tsn')

logger = _logger


def get_logger(name: str = 'tsn'):
    """按名字取 logger；loguru 只有一个全局 sink，直接返回同一个对象。"""
    try:
        return _logger.bind(module=name)
    except AttributeError:  # 标准库 logging
        return _logger.getChild(name)


__all__ = ['logger', 'get_logger']
