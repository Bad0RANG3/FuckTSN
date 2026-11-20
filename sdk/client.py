"""客户端工厂：把数据库里的账号记录变成可用的 SDK 实例。

这一层原本混了三件事：取账号、刷新 token、登录失败时写库。重构后：
- 凭据读写集中在这里（password 出库即解密，入库即加密）；
- 「token 还能用吗」由 `token_is_fresh` 一处判断；
- 失败原因来自 `tsn_protocol` 的分类，不再用中文字符串猜（例如把
  服务端降级误报成"账号密码错误"）。
"""
from __future__ import annotations

import datetime as _dt
import uuid

from loguru import logger

from tsn import protocol
from tsn import secrets
from sdk.errors import TiShiNengError
from app.database import get_db
from app.device import deviceModel
from app.models import TsnAccount_Model
from tsn.dao import addTsnAccount, getTsnAccountByUid, getTsnAccountByid, updateAccessToken
from tsn.dao import getSchoolBySchoolId
from sdk.private import TiShiNengPrivate
from sdk.public import TiShiNengSdkPublic

TOKEN_SKEW_SECONDS = 120

_LOGIN_MSG_MAP = (
    ('bad credentials', '用户名错误'),
    ('wrong password', '密码错误'),
    ('用户不存在', '用户名错误'),
    ('密码错误', '密码错误'),
)


def _account_password(account: TsnAccount_Model) -> str:
    """出库解密；历史明文原样返回（下次登录成功后会回写密文）。"""
    try:
        return secrets.decrypt_secret(account.password)
    except Exception as exc:
        raise TiShiNengError(f'本地凭据无法解密（密钥文件丢失？）：{exc}', 10001)


def _store_password(password: str) -> str:
    return secrets.migrate_legacy(password)


def token_is_fresh(account: TsnAccount_Model) -> bool:
    """按 expires_in + 写入时间判断 token 是否还值得用。

    老库没有 token_updated_at 字段时返回 False，走一次刷新（代价很小）。
    """
    issued = getattr(account, 'token_updated_at', None)
    if not issued or not account.access_token:
        return False
    lifetime = int(account.expires_in or 0)
    if lifetime <= 0:
        return False
    deadline = issued + _dt.timedelta(seconds=max(0, lifetime - TOKEN_SKEW_SECONDS))
    return _dt.datetime.now(deadline.tzinfo) < deadline


def _map_login_error(result: protocol.TsnResult) -> str:
    text = (result.message or '').lower()
    for needle, message in _LOGIN_MSG_MAP:
        if needle in text:
            return message
    return result.message or '登录失败'


async def _save_token(account_id: int, token_resp: dict, *, refresh: bool = True) -> None:
    async for session in get_db():
        await updateAccessToken(
            account_id,
            session,
            token_resp['access_token'],
            token_resp.get('refresh_token', '') if refresh else '',
            int(token_resp.get('expires_in') or 86399),
        )


async def _rotate_token(tsn, account: TsnAccount_Model, *, allow_refresh: bool) -> None:
    """优先 refresh_token，失败再密码登录。抛出的异常保留分类信息。"""
    password = _account_password(account)
    if allow_refresh:
        try:
            resp = await tsn.freshToken(account.refresh_token)
            if resp.get('access_token'):
                await _save_token(account.id, resp)
                tsn.setToken(resp['access_token'])
                account.access_token = resp['access_token']
                account.refresh_token = resp.get('refresh_token', account.refresh_token)
                logger.info('token 已通过 refresh_token 轮换')
                return
        except protocol.TsnProtocolError as exc:
            if exc.kind is protocol.ResultKind.SERVER_DEGRADED:
                raise TiShiNengError(f'认证服务暂时不可用：{exc.message}', 503) from exc
            logger.info('refresh_token 不可用（{}），改用密码登录', exc.kind.value)
        except TiShiNengError as exc:
            if getattr(exc, 'code', None) not in (401, 10001):
                raise

    try:
        resp = await tsn.getAccessToken(account.username, password)
    except protocol.TsnProtocolError as exc:
        raise TiShiNengError(_map_login_error(exc), exc.http_status or 10001) from exc
    if not resp.get('access_token'):
        raise TiShiNengError('重新登录失败：响应缺少 access_token', 10001)
    await _save_token(account.id, resp)
    tsn.setToken(resp['access_token'])
    account.access_token = resp['access_token']
    account.refresh_token = resp.get('refresh_token', '')
    logger.info('token 已通过密码重新登录获得')


async def getPublicVersionClient(accountModel: TsnAccount_Model):
    school = accountModel.school
    tsn = TiShiNengSdkPublic(
        accountModel.user_id, accountModel.school_id, school.school_code, school.open_id,
        accountModel.mobile_device_id, deviceModel.brand, deviceModel.model,
        deviceModel.osver, accountModel.access_token, deviceModel.a_list,
    )
    if school.lan_url:
        tsn.setCloudUrl(school.lan_url)
    if not token_is_fresh(accountModel):
        await _rotate_token(tsn, accountModel, allow_refresh=bool(accountModel.refresh_token))
    return tsn


async def getPrivateVersionClient(accountModel: TsnAccount_Model):
    school = accountModel.school
    tsn = TiShiNengPrivate(
        accountModel.user_id, accountModel.school_id, school.school_code, school.is_open_encry,
        accountModel.mobile_device_id, deviceModel.brand, deviceModel.model, accountModel.access_token,
    )
    tsn.setSchoolUrl(school.school_url)
    tsn.setAppId(school.open_id)

    try:
        await tsn.getStudentInfo()
        return tsn
    except protocol.TsnProtocolError as exc:
        if not exc.is_auth_expired:
            raise
    except TiShiNengError as exc:
        if not protocol.is_auth_expired(exc):
            raise

    logger.info('私有版 token 失效，重新登录')
    password = _account_password(accountModel)
    login_resp = await tsn.appLogin(accountModel.username, password)
    token = (login_resp or {}).get('token')
    if not token:
        raise TiShiNengError('私有版登录失败：响应缺少 token', 10001)
    async for session in get_db():
        await updateAccessToken(accountModel.id, session, token, '', 86399)
    tsn.setAccessToken(token)
    accountModel.access_token = token
    return tsn


async def getTsnClientById(accountId, session):
    account = await getTsnAccountByid(accountId, session)
    if account is None:
        raise TiShiNengError('账号不存在', 10001)
    return await _client_for_account(account)


async def getTsnClientByUid(uid, session):
    account = await getTsnAccountByUid(uid, session)
    if account is None:
        raise TiShiNengError('账号不存在', 10001)
    return await _client_for_account(account)


async def _client_for_account(account: TsnAccount_Model):
    sys_type = account.school.sys_type
    if sys_type == 2:
        return await getPublicVersionClient(account)
    if sys_type == 1:
        return await getPrivateVersionClient(account)
    raise TiShiNengError(f'未知的系统类型: {sys_type}', 10001)


def _new_public_client(school, schoolId: int, deviceId: str, token: str = ''):
    tsn = TiShiNengSdkPublic(
        0, schoolId, school.school_code, school.open_id, deviceId,
        deviceModel.brand, deviceModel.model, deviceModel.osver, token, deviceModel.a_list,
    )
    if school.lan_url:
        tsn.setCloudUrl(school.lan_url)
    return tsn


async def tsnPasswordAuthServer(schoolId, userName, password, session):
    """账号密码授权；返回 SDK 侧 user_id。"""
    school = await getSchoolBySchoolId(schoolId, session)
    if not school:
        raise TiShiNengError('学校不存在')
    deviceId = str(uuid.uuid4())

    if school.isPublicVersion():
        tsn = _new_public_client(school, schoolId, deviceId)
        try:
            login_resp = await tsn.getAccessToken(userName, password)
        except protocol.TsnProtocolError as exc:
            if exc.kind is protocol.ResultKind.BIZ_ERROR:
                raise TiShiNengError(_map_login_error(exc)) from exc
            raise
        if 'access_token' not in login_resp:
            raise TiShiNengError(login_resp.get('msg') or '登录失败：响应缺少 access_token')

        access_token = login_resp['access_token']
        refresh_token = login_resp.get('refresh_token', '')
        expires_in = int(login_resp.get('expires_in') or 86399)
        uid = login_resp.get('user_id')
        await tsn.aclose()

        tsn = _new_public_client(school, schoolId, deviceId, access_token)
        user_info = await tsn.getLoginUserInfo()

        account = await getTsnAccountByUid(uid, session)
        is_new = account is None
        if is_new:
            account = TsnAccount_Model()
        account.student_id = user_info.get('studentId')
        account.user_id = uid
        account.school_id = schoolId
        account.username = userName
        account.password = _store_password(password)
        account.mobile_device_id = deviceId
        account.access_token = access_token
        account.refresh_token = refresh_token
        account.expires_in = expires_in
        await _touch_token_time(account)
        if is_new:
            await addTsnAccount(account, session)
        else:
            await session.flush()
        return uid

    tsn = TiShiNengPrivate(0, schoolId, school.school_code, school.is_open_encry, deviceId, deviceModel.brand, deviceModel.model, '')
    tsn.setAppId(school.open_id)
    tsn.setSchoolUrl(school.school_url)
    login_resp = await tsn.appLogin(userName, password)
    for key, label in (('userNum', '学号'), ('id', '用户ID'), ('token', 'token')):
        if not (login_resp or {}).get(key):
            raise TiShiNengError(f'私有版登录失败：响应缺少{label}')
    uid = login_resp['id']
    await tsn.aclose()

    account = await getTsnAccountByUid(uid, session, schoolId)
    is_new = account is None
    if is_new:
        account = TsnAccount_Model()
    account.student_id = login_resp['userNum']
    account.user_id = uid
    account.school_id = schoolId
    account.username = userName
    account.password = _store_password(password)
    account.mobile_device_id = deviceId
    account.access_token = login_resp['token']
    account.refresh_token = ''
    account.expires_in = 86399
    await _touch_token_time(account)
    if is_new:
        await addTsnAccount(account, session)
    else:
        await session.flush()
    return f'{schoolId}:{uid}'


async def _touch_token_time(account: TsnAccount_Model) -> None:
    if hasattr(account, 'token_updated_at'):
        account.token_updated_at = _dt.datetime.now()
