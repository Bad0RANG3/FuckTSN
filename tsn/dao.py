"""数据访问层：账号表与学校表（SQLAlchemy 异步 Session）。

原来这两个 DAO 分在两个包目录里（services/tsnAccount、services/tsnSchool），
每个包还带一个空的 __init__.py —— 只是上游脚手架的痕迹。合并成一个模块后，
调用方只需要 `from tsn_dao import ...`，少一层包结构，也便于把"账号 → 学校"
这条唯一的外键关系看清楚（TsnAccount.school_id → TsnSchool.school_id）。

注意 TsnAccount_Model.school 是 relationship，查询账号时必须 selectinload，
否则异步 session 在序列化阶段会因为懒加载抛 MissingGreenlet。
"""
from __future__ import annotations

from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import func

from app.models import TsnAccount_Model, TsnSchool_Model


# ---------------------------------------------------------------------------
# 学校
# ---------------------------------------------------------------------------

async def addOrUpdateSchool(
    schoolId,
    schoolName,
    schoolUrl,
    lanUrl,
    openId,
    isOpenKeep,
    isOpenLive,
    isOpenEncry,
    sys_type,
    school_code,
    session,
    province_name=None,
):
    """按 school_id upsert 一条学校配置，返回主键 id。"""
    existing = await getSchoolBySchoolId(schoolId, session)
    if existing:
        stmt = (
            update(TsnSchool_Model)
            .where(TsnSchool_Model.school_id == schoolId)
            .values(
                school_name=schoolName,
                school_url=schoolUrl,
                lan_url=lanUrl,
                open_id=openId,
                is_open_keep=isOpenKeep,
                is_open_live=isOpenLive,
                is_open_encry=isOpenEncry,
                sys_type=sys_type,
                province_name=province_name,
            )
        )
        await session.execute(stmt)
        await session.flush()
        return existing.id

    school = TsnSchool_Model(
        school_id=schoolId,
        school_name=schoolName,
        school_url=schoolUrl,
        lan_url=lanUrl,
        open_id=openId,
        is_open_keep=isOpenKeep,
        is_open_live=isOpenLive,
        is_open_encry=isOpenEncry,
        sys_type=sys_type,
        school_code=school_code,
        province_name=province_name,
    )
    session.add(school)
    await session.flush()
    return school.id


async def getSchoolListDao(session, filterPublic: bool = False) -> List[TsnSchool_Model]:
    """学校列表；filterPublic=True 时排除 sys_type==2 的"公共"条目。"""
    stmt = select(TsnSchool_Model)
    if filterPublic:
        stmt = stmt.where(TsnSchool_Model.sys_type != 2)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def getSchoolBySchoolId(schoolId, session) -> Optional[TsnSchool_Model]:
    stmt = select(TsnSchool_Model).where(TsnSchool_Model.school_id == schoolId)
    result = await session.execute(stmt)
    return result.scalars().first()


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------

async def getTsnAccountByid(accountId, session) -> Optional[TsnAccount_Model]:
    stmt = (
        select(TsnAccount_Model)
        .options(selectinload(TsnAccount_Model.school))
        .where(TsnAccount_Model.id == accountId)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def getTsnAccountByUid(uid, session, schoolId=None) -> Optional[TsnAccount_Model]:
    """按 user_id 查账号；uid 允许写成 'schoolId:userId' 的复合形式。"""
    uid = str(uid)
    if ':' in uid:
        head, tail = uid.split(':', 1)
        uid, schoolId = tail, head
    stmt = (
        select(TsnAccount_Model)
        .options(selectinload(TsnAccount_Model.school))
        .where(TsnAccount_Model.user_id == uid)
    )
    if schoolId:
        stmt = stmt.where(TsnAccount_Model.school_id == schoolId)
    result = await session.execute(stmt)
    return result.scalars().first()


async def updateAccessToken(accountId, session, accessToken, refreshToken, expiresIn):
    """写入新 token，并把 token_updated_at 打到当前时间（用于判断过期）。"""
    stmt = (
        update(TsnAccount_Model)
        .where(TsnAccount_Model.id == accountId)
        .values(
            access_token=accessToken,
            refresh_token=refreshToken,
            expires_in=expiresIn,
            token_updated_at=func.now(),
        )
    )
    await session.execute(stmt)
    await session.flush()
    return True


async def addTsnAccount(tsnAccountModel: TsnAccount_Model, session):
    session.add(tsnAccountModel)
    await session.flush()
    return tsnAccountModel.id


__all__ = [
    'addOrUpdateSchool', 'getSchoolListDao', 'getSchoolBySchoolId',
    'getTsnAccountByid', 'getTsnAccountByUid', 'updateAccessToken', 'addTsnAccount',
]
