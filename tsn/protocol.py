"""统一的报文分类层：把每一次 HTTP 交换归类成确定的 `ResultKind`。

这个模块是「报文 → 判断」的唯一真源：

    SDK 层  ── classify_response() ──> TsnResult ── raise_for_error() ──> 异常
                                       │
                                       ├─ should_retry()        传输层是否重试
                                       ├─ is_auth_expired()     是否需要重新登录
                                       └─ KIND_INFO            生成文档 / 日志文案

这样做的原因：此前同一个响应在 5 个地方各判断一次
（`code == 0` / `returnCode == '200'` / `resp.json()` / `status_code == 200`），
导致同一份报文在不同接口上给出不同结论，例如：

* 网关返回 HTML 时，`getAccessToken` 抛 JSONDecodeError，`freshToken` 静默失败；
* `code=1, msg=server_error` 的降级报文被当成普通业务错误，不重试；
* HTTP 999（反作弊）被当成网络错误，重试 60 次；
* `resp['msg']` 在缺少 msg 字段时直接 KeyError。

只依赖 httpx 与 TiShiNengError，不反向依赖任何 SDK，便于离线单测。
"""
from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from sdk.errors import ErrorCode, TiShiNengError, TsnProtocolError


class ResultKind(enum.Enum):
    OK = 'ok'
    BIZ_ERROR = 'biz_error'
    AUTH_EXPIRED = 'auth_expired'
    ANTI_CHEAT = 'anti_cheat'
    RATE_LIMITED = 'rate_limited'
    GATEWAY_BLOCKED = 'gateway_blocked'
    SERVER_DEGRADED = 'server_degraded'
    TRANSPORT = 'transport'
    MALFORMED = 'malformed'


#: 供文档/日志使用的分类元数据（kind -> 展示信息）
KIND_INFO: dict[ResultKind, dict[str, Any]] = {
    ResultKind.OK: {
        'label': '成功',
        'action': '正常返回 data',
        'retry': False,
        'error_code': None,
    },
    ResultKind.BIZ_ERROR: {
        'label': '业务失败',
        'action': '把服务端 msg 展示给用户，不重试',
        'retry': False,
        'error_code': None,  # 透传服务端业务码
    },
    ResultKind.AUTH_EXPIRED: {
        'label': '登录态失效',
        'action': '用 refresh_token 续期，失败则重新登录后再重放一次',
        'retry': False,
        'error_code': ErrorCode.AUTH_EXPIRED,
    },
    ResultKind.ANTI_CHEAT: {
        'label': '反作弊 / 禁跑',
        'action': '立即终止流程并展示服务端原文，不要重试（重试会加重风控）',
        'retry': False,
        'error_code': ErrorCode.ANTI_CHEAT,
    },
    ResultKind.RATE_LIMITED: {
        'label': '限流 / 需要验证码',
        'action': '提示稍后再试；不要无脑重试',
        'retry': False,
        'error_code': ErrorCode.RATE_LIMITED,
    },
    ResultKind.GATEWAY_BLOCKED: {
        'label': '网关 / WAF 拦截',
        'action': '换传输方式（表单改 query、缩短参数、加 UA/Cookie）或稍后重试',
        'retry': False,
        'error_code': ErrorCode.GATEWAY_BLOCKED,
    },
    ResultKind.SERVER_DEGRADED: {
        'label': '服务端降级',
        'action': '可重试：服务端明确报 server_error / 5xx，通常是抖动',
        'retry': True,
        'error_code': ErrorCode.SERVER_DEGRADED,
    },
    ResultKind.TRANSPORT: {
        'label': '传输层失败',
        'action': '可重试：连接被重置 / 超时 / DNS 失败',
        'retry': True,
        'error_code': ErrorCode.TRANSPORT,
    },
    ResultKind.MALFORMED: {
        'label': '报文结构不符',
        'action': '记录原始报文；属于契约变更或服务端 bug，不要重试',
        'retry': False,
        'error_code': ErrorCode.GATEWAY_BLOCKED,
    },
}

_ANTI_CHEAT_HINTS = ('禁跑黑名单', '作弊', '反作弊')
_DEGRADED_HINTS = ('server_error', 'internal server error', '服务开小差', '系统繁忙')
_TRANSIENT_NET_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.PoolTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
)


@dataclass
class TsnResult:
    """一次接口调用的归一化结果。"""

    kind: ResultKind
    http_status: int = 0
    message: str = ''
    data: Any = None
    server_code: Any = None
    payload: Any = None
    snippet: str = ''
    context: str = ''
    response: Optional[httpx.Response] = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.kind is ResultKind.OK

    @property
    def retryable(self) -> bool:
        return KIND_INFO[self.kind]['retry']

    @property
    def is_auth_expired(self) -> bool:
        return self.kind is ResultKind.AUTH_EXPIRED

    def describe(self) -> str:
        label = KIND_INFO[self.kind]['label']
        return f'{label}(HTTP {self.http_status}, code={self.server_code}, msg={self.message[:80]})'

    def to_error(self) -> TsnProtocolError:
        info = KIND_INFO[self.kind]
        code = info['error_code']
        if code is None:
            code = self.server_code if isinstance(self.server_code, int) else ErrorCode.UNKNOWN
        message = self.message or info['label']
        if self.kind is ResultKind.GATEWAY_BLOCKED:
            message = f'接口返回非 JSON（HTTP {self.http_status}，可能被网关/WAF 拦截）：{self.snippet or "空响应"}'
        elif self.kind is ResultKind.SERVER_DEGRADED:
            message = f'服务端暂时不可用（HTTP {self.http_status}，code={self.server_code}）：{self.message or "server_error"}'
        elif self.kind is ResultKind.MALFORMED:
            message = f'接口返回结构不符合预期（HTTP {self.http_status}）：{self.snippet or self.message}'
        elif self.kind is ResultKind.AUTH_EXPIRED and not self.message:
            message = '登录状态已失效，需要重新授权'
        if self.context:
            message = f'{self.context}: {message}'
        return TsnProtocolError(
            message,
            code,
            kind=self.kind.value,
            http_status=self.http_status,
            server_code=self.server_code,
            snippet=self.snippet,
            context=self.context,
        )

    def raise_for_error(self) -> Any:
        if self.ok:
            return self.data
        raise self.to_error()


def _text_of(resp: httpx.Response) -> str:
    try:
        return resp.text or ''
    except Exception:  # 响应体读取失败（编码/连接问题）时不要让诊断逻辑再崩一次
        return ''


def _parse_payload(resp: httpx.Response) -> tuple[Any, bool]:
    """返回 (payload, 是否成功解析为 JSON)。"""
    text = _text_of(resp).strip()
    if not text:
        return None, False
    try:
        return json.loads(text), True
    except (ValueError, TypeError):
        return None, False


def _extract_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ('msg', 'message', 'returnMsg', 'error_description', 'error', 'statusMsg'):
            value = payload.get(key)
            if value:
                return str(value)
    if isinstance(payload, str):
        return payload
    return ''


def _extract_data(payload: Any) -> Any:
    if isinstance(payload, dict) and 'data' in payload:
        return payload['data']
    return payload


def _snippet(text: str, limit: int = 200) -> str:
    return re.sub(r'\s+', ' ', text).strip()[:limit]


def classify_response(
    resp: Optional[httpx.Response],
    *,
    context: str = '',
    expect_token: bool = False,
) -> TsnResult:
    """把 httpx 响应归类。不抛异常，由调用方决定 `raise_for_error()` 还是重试。"""
    if resp is None:
        return TsnResult(ResultKind.TRANSPORT, message='没有收到响应', context=context)

    status = resp.status_code
    payload, parsed = _parse_payload(resp)
    text = _text_of(resp).strip()
    snippet = _snippet(text)
    message = _extract_message(payload)
    lowered = (message or text or '').lower()

    def build(kind: ResultKind, *, data: Any = None, code: Any = None, msg: str = '') -> TsnResult:
        return TsnResult(
            kind,
            http_status=status,
            message=msg or message,
            data=data,
            server_code=code,
            payload=payload,
            snippet=snippet,
            context=context,
            response=resp,
        )

    # 1. HTTP 语义优先：999 / 401 / 428 / 429 / 5xx
    if status == 999:
        return build(ResultKind.ANTI_CHEAT)
    if status == 401:
        return build(ResultKind.AUTH_EXPIRED, msg=message or '登录状态已失效')
    if status in (428, 429):
        return build(ResultKind.RATE_LIMITED)
    if status >= 500:
        return build(ResultKind.SERVER_DEGRADED)

    if not parsed:
        # 非 JSON：网关/WAF 拦截页最常见；空响应也归到这里便于排查
        return build(ResultKind.GATEWAY_BLOCKED)

    # 2. 业务报文：code / returnCode / access_token
    if isinstance(payload, dict):
        code = payload.get('code', payload.get('returnCode'))
        if code is None and payload.get('access_token'):
            return build(ResultKind.OK, data=payload)
        if code is not None:
            ok = str(code) in ('0', '200')
            if ok:
                return build(ResultKind.OK, data=_extract_data(payload), code=code)
            if any(h in lowered for h in _ANTI_CHEAT_HINTS):
                return build(ResultKind.ANTI_CHEAT, code=code)
            if any(h in lowered for h in _DEGRADED_HINTS):
                return build(ResultKind.SERVER_DEGRADED, code=code)
            return build(ResultKind.BIZ_ERROR, code=code)
        if payload.get('error'):
            return build(ResultKind.BIZ_ERROR)
        return build(ResultKind.MALFORMED)
    if isinstance(payload, list):
        return build(ResultKind.OK, data=payload)
    return build(ResultKind.MALFORMED)


def classify_exception(exc: BaseException, *, context: str = '') -> TsnResult:
    if isinstance(exc, _TRANSIENT_NET_ERRORS):
        return TsnResult(ResultKind.TRANSPORT, message=f'{type(exc).__name__}: {exc}', context=context)
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return TsnResult(ResultKind.GATEWAY_BLOCKED, message=f'响应无法解析: {exc}', context=context)
    return TsnResult(ResultKind.TRANSPORT, message=str(exc), context=context)


def should_retry(result: TsnResult) -> bool:
    return KIND_INFO[result.kind]['retry']


def is_auth_expired(exc: BaseException) -> bool:
    return isinstance(exc, TiShiNengError) and exc.code == ErrorCode.AUTH_EXPIRED


_TOKEN_RE = re.compile(r'("(?:access|refresh)_token"\s*:\s*")[^"]+(")')
_PWD_RE = re.compile(r'(password=)[^&\s]+')


def redact(text: str) -> str:
    """日志脱敏：token / 密码不落盘。"""
    if not text:
        return text
    text = _TOKEN_RE.sub(r'\1***\2', text)
    return _PWD_RE.sub(r'\1***', text)
