"""统一异常与错误码。

所有对外抛出的异常都是 `TiShiNengError`（或其子类 `TsnProtocolError`），
业务层只依赖 `.code` / `.message` 两个字段，方便 CLI / Web 层统一处理。

错误码分区：

- 1xxxx：客户端侧问题（网络、重登录、网关拦截等）
- 4xx / 9xx：服务端 HTTP 语义（401 失效、428 限流/验证码、999 反作弊）
- 2xxxx：业务子流程（人脸、路线）
- 服务端业务码：原样透传（`server_code`）
"""
from __future__ import annotations

import enum
from typing import Any, Optional


class ErrorCode(enum.IntEnum):
    UNKNOWN = 10000
    RELOGIN_FAILED = 10001
    GATEWAY_BLOCKED = 10002
    RUN_TIME_CONFLICT = 10003
    TRANSPORT = 10004
    AUTH_EXPIRED = 401
    RATE_LIMITED = 428
    ANTI_CHEAT = 999
    SERVER_DEGRADED = 503
    FACE_LIST_FAILED = 20001
    FACE_NONE = 20002
    FACE_DOWNLOAD_FAILED = 20003
    RUN_UNAVAILABLE = 200000


class TiShiNengError(Exception):
    """项目统一异常。`code` 是客户端错误码，不一定是服务端业务码。"""

    def __init__(self, message: str, code: int | str | None = ErrorCode.UNKNOWN):
        try:
            self.code = int(code)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            self.code = int(ErrorCode.UNKNOWN)
        self.message = message
        super().__init__(self.message)

    def __str__(self) -> str:
        return f'[{self.code}] {self.message}'


class TsnProtocolError(TiShiNengError):
    """由报文分类层抛出的异常，额外携带分类与原始诊断信息。"""

    def __init__(
        self,
        message: str,
        code: int = ErrorCode.UNKNOWN,
        *,
        kind: Optional[str] = None,
        http_status: Optional[int] = None,
        server_code: Any = None,
        snippet: str = '',
        context: str = '',
    ):
        super().__init__(message, code)
        self.kind = kind
        self.http_status = http_status
        self.server_code = server_code
        self.snippet = snippet
        self.context = context

    def to_dict(self) -> dict:
        return {
            'code': self.code,
            'message': self.message,
            'kind': self.kind,
            'http_status': self.http_status,
            'server_code': self.server_code,
            'context': self.context,
        }
