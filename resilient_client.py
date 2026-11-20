"""传输层重试客户端。

只处理「连接级」故障：连接被重置、握手失败、连接/读超时。
响应体代表什么（业务错误 / 网关拦截 / 服务端降级）由 `tsn_protocol` 判断，
协议级重试由 `tsn_http` 决定 —— 这里不解析 body，避免两套重试策略互相打架。

重试次数可在环境变量里调整：
    TSN_MAX_RETRIES    默认 6
    TSN_RETRY_BACKOFF  默认 0.5 秒（指数退避，上限 8 秒）
    TSN_NO_RESILIENT=1 关闭本客户端（退回原生 httpx.AsyncClient）
"""
from __future__ import annotations

import asyncio
import os
import random

import httpx
from loguru import logger

TRANSIENT_NET_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    httpx.WriteTimeout,
)

MAX_BACKOFF = 8.0


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


class ResilientAsyncClient(httpx.AsyncClient):
    def __init__(self, *args, max_retries: int | None = None, backoff: float | None = None, **kwargs):
        kwargs.setdefault('timeout', httpx.Timeout(60.0, connect=20.0))
        super().__init__(*args, **kwargs)
        self.max_retries = _env_int('TSN_MAX_RETRIES', 6) if max_retries is None else max_retries
        self.backoff = _env_float('TSN_RETRY_BACKOFF', 0.5) if backoff is None else backoff

    def _delay(self, attempt: int) -> float:
        return min(self.backoff * (2 ** attempt), MAX_BACKOFF) + random.uniform(0, 0.2)

    async def request(self, method, url, **kwargs) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return await super().request(method, url, **kwargs)
            except TRANSIENT_NET_ERRORS as exc:
                last_exc = exc
                if attempt + 1 >= self.max_retries:
                    break
                delay = self._delay(attempt)
                logger.warning(
                    '传输层重试 {}/{} {} {}：{}，{:.1f}s 后重试',
                    attempt + 1, self.max_retries, method.upper(),
                    _safe_url(url), type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc


def _safe_url(url) -> str:
    """日志里只保留 host + path，避免把加密密码/ token 写进日志。"""
    try:
        parsed = httpx.URL(str(url))
        return f'{parsed.scheme}://{parsed.host}{parsed.path}'
    except Exception:
        return '<url>'
