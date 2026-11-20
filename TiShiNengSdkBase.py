"""m.boxkj.com 第三方接口的公共基类（登录前置接口：省份/学校/版本/反馈）。

这里原本是「自己拼 URL + 自己解报文 + 出错返回 None」的三合一实现，
现在统一交给 `TsnHttpCore`（URL/头部/协议重试）与 `tsn_protocol`（报文分类）：

* `httpPost` 保留历史语义（成功返回完整响应体 dict，失败返回 None），
  因为存量调用方（main.py 的学校搜索）依赖 `if not resp`；
* 需要区分失败原因时用 `request()`，它返回 `TsnResult` 并可按需抛异常。
"""
from __future__ import annotations

import hashlib
import os
import time
import urllib.parse

import httpx
from loguru import logger

import tsn_protocol
from TiShiNengError import TiShiNengError
from resilient_client import ResilientAsyncClient
from tsn_http import DEFAULT_TIMEOUT, TsnHttpCore


class TiShiNengSdkBase:

    apiUrl = 'https://m.boxkj.com'
    appId = 'move'
    appSecret = 'e8167ef026cbc5e456ab837d9d6d9254'
    appSign = '7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3'

    def __init__(self, uid, schoolId, deviceId, brandName, deviceNum, token):
        self.uid = uid
        self.schoolId = schoolId
        self.deviceId = deviceId
        self.brandName = brandName
        self.deviceNum = deviceNum
        self.platform = '1'
        self.versionName = '3.6.814'
        self.version = '306814'
        self.token = token
        self.headers = {
            'Host': 'm.boxkj.com',
            'token': self.token,
            'channel': 'Android',
            'version': self.version,
            'type': '0',
            'Content-Type': 'application/x-www-form-urlencoded',
            'accept-encoding': 'gzip',
            'user-agent': 'okhttp/4.9.0',
        }
        client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        if os.environ.get('TSN_NO_RESILIENT') != '1':
            client = ResilientAsyncClient(timeout=DEFAULT_TIMEOUT)
        self._core = TsnHttpCore(base_url=self.apiUrl, headers=self.headers, client=client, token=token)

    # ---- HTTP 基础设施 ---------------------------------------------
    @property
    def httpClient(self) -> httpx.AsyncClient:
        return self._core.client

    @httpClient.setter
    def httpClient(self, value: httpx.AsyncClient) -> None:
        self._core.client = value

    @property
    def core(self) -> TsnHttpCore:
        """供子类复用（签名不同，但 URL/重试/分类一致）。"""
        return self._core

    def setToken(self, token: str) -> None:
        self.token = token
        self.headers['token'] = token
        self._core.set_token(token)

    @staticmethod
    def kVtoStr(key, value, is_encoded):
        if is_encoded:
            try:
                return f'{key}={urllib.parse.quote(value)}'
            except Exception:
                return f'{key}={value}'
        return f'{key}={value}'

    @staticmethod
    def getMd5(text):
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    def getSign(self, params: dict):
        params.update({'appId': self.appId, 'appSecret': self.appSecret})
        keys = sorted(params.keys())
        concatenated_string = '&'.join(self.kVtoStr(key, params[key], False) for key in keys)
        logger.debug('sign source: {}', concatenated_string)
        return hashlib.md5(concatenated_string.encode()).hexdigest()

    async def request(self, url: str, data: dict | None = None, *, method: str = 'POST', context: str = '') -> tsn_protocol.TsnResult:
        """发送带签名的请求并返回分类结果（不抛异常）。"""
        payload = dict(data or {})
        payload['timestamp'] = str(int(time.time() * 1000))
        payload['sign'] = self.getSign(payload.copy())
        result = await self._core.send(method, url, data=payload, context=context or url)
        logger.debug('{} -> {}', context or url, result.describe())
        return result

    async def httpPost(self, url, data):
        """历史签名：成功返回响应体 dict，失败返回 None（错误已分类记录）。"""
        result = await self.request(url, data)
        if result.ok:
            return result.payload
        if result.kind is tsn_protocol.ResultKind.TRANSPORT:
            return None
        logger.warning('{} 失败：{}', url, result.describe())
        return None

    async def httpPostStrict(self, url, data):
        """严格版：失败抛 TiShiNengError，供新代码使用。"""
        result = await self.request(url, data)
        result.raise_for_error()
        return result.payload

    # ---- 登录前置接口 ----------------------------------------------
    async def findAllProvince(self):
        return await self.httpPost('/app/province/findAllProvince', {})

    async def listSchoolByProvinceId(self, provinceId):
        return await self.httpPost('/app/sch/listSchoolByProvinceId', {'provinceId': provinceId})

    async def getSchoolById(self, schoolId):
        return await self.httpPost('/app/sch/getSchoolById', {'schoolId': schoolId})

    async def addUploadRecord(self):
        params = {
            'schoolId': self.schoolId,
            'platform': self.platform,
            'deviceId': self.deviceId,
            'brandName': self.brandName,
            'deviceNum': self.deviceNum,
            'sysUserId': '',
            'versionName': self.versionName,
        }
        return await self.httpPost('/admin/analysis/addUploadRecord', params)

    async def stuAppVersion2(self):
        return await self.httpPost('/admin/studentAppUpdate/stuAppVersion2', {'schoolId': self.schoolId, 'appSign': self.appSign})

    async def getReplyFeedBackCountUser(self):
        return await self.httpPost('/app/replyFeedBack/getReplyFeedBackCountUser', {'schoolId': self.schoolId, 'userId': self.uid})

    def getHttpClient(self):
        return self.httpClient

    async def aclose(self):
        await self._core.aclose()


__all__ = ['TiShiNengSdkBase', 'TiShiNengError']
