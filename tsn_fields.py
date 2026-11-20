"""字段对照表：旧版（2.0.23 / 私有 appAddSportRecord）→ 新版（3.6.814 / addExerciseRecord）。

这是"提交报文该长什么样"的唯一事实来源：文档 docs/字段对照.md 由本模块渲染，
`migrate_legacy()` 用它把旧报文原地升级，`audit_submission()` 用它体检一份报文。

每条记录都带 `evidence`，说明这个结论是从哪来的：

- ``echo``：服务器 getExerciseRecord 回显（repo/_probe/hist.json 的 5 条合格记录、
  failrec.json 的黑名单记录）——最硬的证据，字段名/格式都是服务端原样返回的。
- ``doc``：项目内逆向笔记/判定链路文档（指 APK 里读到的常量与调用顺序）。
- ``probe``：我们自己发请求拿到的返回（fail2.json 的「运动步数不符合」等）。

判定相关度 `judged` 的取值：

- ``hard``：服务器/规则直接用来判合格与处罚，改错就废；
- ``soft``：参与人工复核或影响风险评分（d/f/m/h、设备画像、isValid 回显）；
- ``display``：只用于展示与回显，服务器会重算或不看；
- ``none``：可以不填或填什么都一样（isSequencePoint、okPointList）。
"""
from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

UUID_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
PACE_RE = re.compile(r'^\d{1,2}\u2032\d{2}\u2033$')

#: 新版本（v3.6.814）的机型画像。服务器/风控会拿它与账号历史比对。
NEW_DEVICE_PROFILE: Dict[str, str] = {
    'appVersion': '3.6.814',
    'brand': 'iPhone',
    'model': 'iPhone 17 Pro',
    'system': 'iOS',
    'version': '27.0.1',
}

#: 服务器上真实出现过的取值（5 条合格记录 + 8 条列表视图记录）。
#: version 出现过 27.0.1 和 27.3 两种，不能当成固定值校验。
DEVICE_ALLOWED: Dict[str, set] = {
    'appVersion': {'3.6.814'},
    'brand': {'iPhone'},
    'model': {'iPhone 17 Pro'},
    'system': {'iOS'},
    'version': {'27.0.1', '27.3'},
}

#: 旧版本（v2.0.23，被处罚的那条记录）的机型画像，仅供对照。
OLD_DEVICE_PROFILE: Dict[str, str] = {
    'appVersion': '2.0.23',
    'brand': 'Xiaomi',
    'model': 'Xiaomi 手机',
    'system': 'Android',
    'version': '14',
}

POINT_KEYS_NEW: Tuple[str, ...] = ('a', 'c', 'e', 'i', 'l', 'm', 'o', 's', 't')


@dataclass(frozen=True)
class FieldSpec:
    name: str
    group: str          # submit / point / echo / setting
    old: str            # 旧版形态
    new: str            # 新版形态
    judged: str         # hard / soft / display / none
    action: str         # 迁移动作
    evidence: str       # 证据
    note: str = ''


SUBMIT_FIELDS: Tuple[FieldSpec, ...] = (
    FieldSpec('token', 'submit', 'body: token=<access_token>', 'body: token（同左）',
              'hard', '保留登录后的 access_token', 'doc',
              'v2 接口所有请求都要 token，过期直接 401'),
    FieldSpec('nonce', 'submit', 'body: 无', 'body: 32 位随机串',
              'hard', '每次请求重新随机（uuid4().hex）', 'doc', '防重放'),
    FieldSpec('timestamp', 'submit', 'body: 无（sign 里带 ts）', 'body: 毫秒时间戳字符串',
              'hard', '每次请求重新取', 'doc', '与 nonce 一起参与签名'),
    FieldSpec('sign', 'submit', "body: sign=md5(b64(AES-CBC(authInfo)))", 'body: sign 同算法，authInfo 含 nonce/timestamp',
              'hard', '重签，不要复用旧 sign', 'doc', 'sign 错了直接 403/999'),
    FieldSpec('sportType', 'submit', '1（阳光跑）', '1（阳光跑）；换类型要同步 rangeSetting/stepSetting',
              'hard', '保持与 setting 匹配的类型', 'echo', '旧记录 sportType=1，rangeSetting=2.000'),
    FieldSpec('startTime', 'submit', '毫秒时间戳字符串', '毫秒时间戳字符串，与 endTime 同一天',
              'hard', '按目标日生成，不要跨天；时段建议 06:00–21:00（实测最晚 21:10 开始也合格）',
              'echo',
              'startQualifiedTimeSetting=6 是"时长下限 6 分钟"不是"早上 6 点"；'
              '真实合格记录开始时间 18:14/18:57/19:32/20:01/21:10'),
    FieldSpec('endTime', 'submit', '毫秒时间戳字符串', '毫秒时间戳字符串',
              'hard', '= 最后一个点的 t + 100~200 ms', 'doc', '人脸/时长窗口都按它算'),
    FieldSpec('sportTime', 'submit', '"1200"（秒，整数）', '"880"（秒，整数 = (endTime-startTime)//1000）',
              'hard', '按轨迹真实时长填写', 'echo', '合格记录 880/887/1041/1347/940'),
    FieldSpec('sportRange', 'submit', '"2.000"（公里，刚好等于下限）',
              '"2.12"（公里，> rangeSetting 2.000，留 6% 余量；合格样本 2.010–2.140，回显成 3 位小数）',
              'hard', '按真实距离保留 2 位小数，且必须 > rangeSetting（留 5% 以上余量）', 'echo',
              '合格 2.090/2.050/2.010/4.010/2.020 → 原始提交是 2 位小数，回显补零到 3 位；被罚记录正好 2.000 = 下限'),
    FieldSpec('speed', 'submit', """10'00"（ASCII 单引号+双引号）""", '7′02″（U+2032 / U+2033）',
              'hard', '用 U+2032/U+2033 重排配速串；且不要贴着 3′00″/10′00″ 边界', 'echo',
              '合格记录 speed="7′02″"；被罚记录是 10\'00"（ASCII，正好卡上限）'),
    FieldSpec('avgSpeed', 'submit', '"6.000"（km/h，3 位小数）', '"8.530"（km/h，1~3 位小数）',
              'display', '= sportRange / sportTime * 3600，保留 2 位', 'echo',
              '回显字段名是小写 avgspeed；服务器不据此判定'),
    FieldSpec('gpsDistance', 'submit', '无此字段（旧私有接口用 formatSportRange）', '与 sportRange 同值',
              'hard', '必须填写且与 sportRange 一致', 'echo', '服务器信这个值，不按点位重算'),
    FieldSpec('stepNumbers', 'submit', '"字符串形式的数组"，21 段各 120（步频 120/min，太齐）', '"字符串形式的数组"，15–23 段，单段实测 0–171（末段常很小），段数随轨迹时长走',
              'hard', '按 60 s 分桶生成，且合计必须等于 step', 'echo',
              '合格记录段数 15–23、单段 0–171、内外差 56–171；被罚的是 21 段各 120 这种机械均分'),
    FieldSpec('step', 'submit', '2398（int）', '1015（int，≥ stepSetting）',
              'hard', '步数 ≥ stepSetting（1000）；实测 999 步被判「运动步数不符合」', 'probe',
              'fail2.json: step=999 → 运动步数不符合；同距离 1015 步合格'),
    FieldSpec('isSequencePoint', 'submit', '"0"', '"0"',
              'none', '保持 "0"', 'echo', '两种版本回显都是 0'),
    FieldSpec('gitudeLatitude', 'submit', '旧：字符串化 JSON / 私有接口的 points', '真数组，元素为 9 键全字符串的点位对象',
              'hard', '见 point 组；坐标 6 位小数、t 为整秒毫秒值', 'echo',
              'hist.json 5 条记录共 4984 点，键集恒为 a c e i l m o s t'),
    FieldSpec('pointList', 'submit', '旧：无', '""（空串）或与 gitudeLatitude 相同',
              'display', '填 "" 即可（App 原生记录回显 pointList=null）', 'echo'),
    FieldSpec('okPointList', 'submit', '"[]"', '"[]" 或 ""（两者 App 都有）',
              'none', '不用改；它不参与判定', 'echo',
              '列表视图里 App 自己的记录是 "[]"，详情视图是 null —— 说明是服务端渲染差异，不是提交信号'),
    FieldSpec('isFaceStatus', 'submit', '"0" / "1"（字符串）', '"1"（字符串）',
              'hard', '人脸必须真跑过：先传 start/middle/end 三类人脸再提交', 'doc',
              '人脸是第一个短路闸门，缺人脸直接不合格'),
    FieldSpec('uploadType', 'submit', '"0"（字符串）', '"0"（字符串）',
              'none', '不用改；App 自己的记录回显就是字符串 "0"', 'echo',
              '8 条列表视图记录全部 uploadType="0"'),
    FieldSpec('identify', 'submit', '"1"', 'UUID（大写小写都出现过；由 getExerciseStartTime 下发或本地生成）',
              'hard', '必须是 UUID；它把人脸/轨迹/记录串在一起', 'echo',
              '被罚记录 identify="1"；合格记录 4b1eed60-…（小写）与 5487F28C-…（大写）并存 → 大小写不敏感'),
    FieldSpec('geofence', 'submit', '旧：无', '围栏环列表，真数组 [{lng,lat}, ...]（可多环）',
              'hard', '照抄 getExerciseSetting 下发的围栏；轨迹 90% 以上点要落在环内', 'probe',
              'fail2.json 回显了 4 个环，含主校区矩形'),
    FieldSpec('limitSpeed', 'submit', '无', '"3.33"（字符串，%.1f 格式化后为 "3.3"）',
              'display', '照抄 setting；不要自己编', 'echo', '新旧差异：2.0.23 时代回显 3.3'),
    FieldSpec('limitStride', 'submit', '无', '"2.3"',
              'display', '照抄 setting', 'echo', ''),
    FieldSpec('limitStepFrequency', 'submit', '无', '"300"',
              'display', '照抄 setting', 'echo', ''),
    FieldSpec('appVersion', 'submit', '"2.0.23"', '"3.6.814"',
              'soft', '换成新版本号，并让 body/header/画像三处版本一致', 'echo',
              '被罚记录的画像是 2.0.23 + Xiaomi + Android 14，合格记录全是 3.6.814 + iPhone 17 Pro + iOS 27.0.1；但那条罚单同时还有 identify="1"、ASCII 引号、21×120 均分等破绽，版本只是相关不是独立归因'),
    FieldSpec('environment', 'submit', '旧：无', '嵌套对象 {deviceId, oppo, safe}，oppo/safe 各是 JSON 字符串',
              'hard', '每次提交都带；11 项探测全 false，checksum 自洽', 'doc',
              'v3.6.814 新增字段，2.0.23 时代没有；整个 params 会被 json.dumps 再 AES 加密，所以它是 JSON 里的对象'),
    FieldSpec('d', 'submit', '无', '0（int）',
              'soft', '开发者模式/离线计数，正常为 0', 'doc', '非 0 会引入人工复核'),
    FieldSpec('f', 'submit', '无', '0（int）', 'soft', '改机标记计数', 'doc', ''),
    FieldSpec('m', 'submit', '无', '0（int）', 'soft', '模拟器标记计数', 'doc', ''),
    FieldSpec('h', 'submit', '无', '0（int）', 'soft', '外挂/注入标记计数', 'doc', ''),
    FieldSpec('isValid', 'submit', '旧：不透传', '1（int，服务器会重算覆盖）',
              'none', '可以带 1，但别指望它', 'echo',
              '回显里 isValid 可能出现 null；判定完全在服务端'),
    FieldSpec('remarkId', 'submit', '旧：无', '"131310"（运动类型/备注 ID）',
              'soft', '照抄 setting 的 remarkId；错填会写错备注', 'probe',
              'fail2/fail3 的 remark 都带 131310 后缀'),
)

#: v2 签名公共字段：每个请求都与业务字段合并后一起算 sign（不在 addExerciseRecord 的业务 body 里）。
V2BASE_FIELDS: Tuple[FieldSpec, ...] = (
    FieldSpec('appType', 'v2base', '"Android"', '"Android"（无 iOS 端证据，保持原值）',
              'none', '保持不变；不参与判定', 'echo',
              'APK 里只有 Android 这一个取值；合格记录 9114388484495371621 的 body 是 iPhone 画像、header 仍是 Android + okhttp/4.9.0，说明服务端不交叉校验这几项'),
    FieldSpec('versionCode', 'v2base', '306814（int）', '306814',
              'hard', '必须与 versionName 3.6.814 对应', 'doc', '旧版 2.0.23 是 20023'),
    FieldSpec('versionName', 'v2base', '"3.6.814"', '"3.6.814"',
              'hard', '同上', 'echo', '记录里的 appVersion 就是它'),
    FieldSpec('signatureMD5', 'v2base', '7F:C0:22:…:8D:C3', '同左',
              'hard', '照抄，别改', 'doc', 'APK 签名证书指纹，改了就 403'),
    FieldSpec('brand', 'v2base', '"Xiaomi"', '"iPhone"',
              'soft', '换成 iPhone；服务器把它原样写进记录', 'echo',
              '记录回显 brand=iPhone；旧画像只在被罚记录里出现（混杂其他破绽，非独立归因）'),
    FieldSpec('model', 'v2base', '"Xiaomi 手机"', '"iPhone 17 Pro"',
              'soft', '同上', 'echo', '注意 header 里的 model 也必须是这个值（旧代码拼成了 iPhone-iPhone 17 Pro）'),
    FieldSpec('system', 'v2base', '"Android"', '"iOS"',
              'soft', '同上', 'echo', ''),
    FieldSpec('version', 'v2base', '"14"（Android 版本号）', '"27.0.1"（iOS 版本号）',
              'soft', '同上；合格记录里 27.0.1 与 27.3 都出现过，属同一 iOS 大版本', 'echo',
              'DEVICE_ALLOWED 按集合匹配 version'),
    FieldSpec('uniqueCode', 'v2base', '设备 ID（mobile_device_id）', '同左',
              'hard', '沿用账号已有的设备 ID，不要每次换', 'doc', '换设备 ID 等于换机，会触发风控'),
)

#: HTTP 头。记录里的机型来自 body，头部是另一套骨架；两边平台不一致是目前唯一可静态识别的破绽。
HEADER_FIELDS: Tuple[FieldSpec, ...] = (
    FieldSpec('channel', 'header', '"Android"', '"Android"（无 iOS 端证据）',
              'none', '保持不变；与 appType 同源，不参与判定', 'echo', ''),
    FieldSpec('version', 'header', '"306814"', '"306814"', 'hard', '与 versionName 同步', 'doc', ''),
    FieldSpec('type', 'header', '"0"', '"0"', 'none', '保持不变', 'doc', ''),
    FieldSpec('user-agent', 'header', '"okhttp/4.9.0"', '"okhttp/4.9.0"（无 iOS 端证据）',
              'none', '保持不变；iOS 端 UA 无法从 APK 取证，且不影响判定', 'echo', ''),
    FieldSpec('model', 'header', '"iPhone-iPhone 17 Pro"（旧代码拼接错误）', '"iPhone 17 Pro"',
              'soft', '改成与 body 的 model 完全一致', 'doc', '头部与 body 不一致是可被关联的破绽'),
    FieldSpec('uniqueCode', 'header', '设备 ID', '同左', 'hard', '与 body 的 uniqueCode 一致', 'doc', ''),
    FieldSpec('school', 'header', '学校 code', '同左', 'hard', '登录后固定', 'doc', ''),
    FieldSpec('Cookie', 'header', '"host=<schoolCode>"', '同左', 'hard', '登录后固定', 'doc', ''),
)


POINT_FIELDS: Tuple[FieldSpec, ...] = (
    FieldSpec('a', 'point', '纬度，数字或 3 位小数', '纬度字符串，6 位小数', 'hard',
              'f"{lat:.6f}"', 'echo', '4984 个点全部 6 位小数'),
    FieldSpec('o', 'point', '经度，数字或 3 位小数', '经度字符串，6 位小数', 'hard',
              'f"{lon:.6f}"', 'echo', ''),
    FieldSpec('t', 'point', '毫秒时间戳（秒级/带小数都可能）', '毫秒时间戳字符串；±2 ms 抖动、同秒重复都正常',
              'hard', '直接写毫秒整数；不要为了"对齐整秒"去改真实采样时刻', 'echo',
              '4984 个真实点里 1125 个不在整秒上，偏移只取 {0,1,998,999}；每条记录还有 6–29 个 dt=0 的重复秒'),
    FieldSpec('s', 'point', '瞬时速度（单位混乱）', 'm/s 字符串，6 位小数，中位数 1.2–3.6',
              'hard', '按分段距离/时长算，保留 6 位', 'echo',
              '合格记录 s 中位数 2.0–3.5、峰值 5.1–10.3'),
    FieldSpec('c', 'point', '被误当成"第几秒"', 'GPS 序号，字符串；起点 0–15，可跳号但不是秒偏移',
              'hard', '从 0–15 起逐点 +1，允许偶尔不连续', 'echo',
              '合格记录 c 从 15/0/4/0/5 起，末值等于点数附近'),
    FieldSpec('e', 'point', '缺失或为 0', '累计步数，字符串；末点必须等于 step',
              'hard', '单调累计，允许平台期；末点 == step', 'echo',
              '有一条合格记录 e 并非严格单调，说明服务器不强校验单调性'),
    FieldSpec('i', 'point', '缺失', '"true"/"false" 字符串；最后一点必须 "true"',
              'hard', '暂停标记：静止段 true，末点 true', 'echo',
              '共现 {false,true}，末点恒为 true'),
    FieldSpec('l', 'point', '缺失', '"1"（偶尔 "2"）', 'none', '写 "1"', 'echo', ''),
    FieldSpec('m', 'point', '缺失（旧脚本漏了这个键）', '"0"', 'none', '写 "0"', 'echo',
              '被罚记录只有 8 个键，缺 m'),
)

ECHO_FIELDS: Tuple[FieldSpec, ...] = (
    FieldSpec('remark', 'echo', '数据异常-作弊嫌疑…-黑名单15', '合格 / 合格15 / 运动步数不符合…',
              'hard', '提交后必须读它判断真实结论', 'echo', ''),
    FieldSpec('remarkExp', 'echo', '同一句话的展开版', '长版判定说明（人脸那条给了完整原因）',
              'display', '读它才知道被卡在哪一环', 'echo',
              '人脸记录 remarkExp 写明「人脸识别时超出规定距离范围」'),
    FieldSpec('va', 'echo', '"我要申诉"', '"正常"', 'hard', 'va=我要申诉 表示该条被判不合格（可申诉）', 'echo',
              '实测只有两种取值：正常 / 我要申诉'),
    FieldSpec('status', 'echo', '0', '1', 'hard', '0=不合格/被罚，1=合格', 'echo', ''),
    FieldSpec('sportStatus', 'echo', '0', '1', 'hard', '提交接口返回的判定结果', 'echo', ''),
    FieldSpec('step', 'echo', '2398', '1015', 'hard', '步数（int）', 'echo', ''),
    FieldSpec('stride', 'echo', '209（cm，= sportRange*1000/step*100）', '194（同左的计算口径）',
              'display', '只展示，别拿它反推规则', 'echo', '合格记录 stride 曾出现 2.06–3.78 m 的"反人类"值'),
    FieldSpec('avgStep', 'echo', '-', '68（步/分钟，服务器算的）', 'display',
              '别用它反推步频规则（合格记录里它低到 45）', 'echo', ''),
    FieldSpec('reachRange', 'echo', '0.000', '合格时 = sportRange；不合格时 0.000',
              'hard', '看这个字段最省事：0.000 就是没计入成绩', 'echo',
              '5 条合格记录 reachRange 全部等于 sportRange；5 条失败记录全是 0.000'),
    FieldSpec('enable', 'echo', 'true', 'false', 'display', 'true 表示该记录可被编辑/删除', 'echo', ''),
    FieldSpec('isValid', 'echo', 'null', 'null 或 1', 'none',
              '服务器重算，回显 null 也可能已经合格', 'probe', 'fail3 的 echo 里 isValid=null，remark 却是合格'),
)

SETTING_FIELDS: Tuple[FieldSpec, ...] = (
    FieldSpec('totalRange', 'setting', '2.000（公里）', '14.200（公里，阶段累计）', 'none',
              '公有接口里这是**学期/阶段累计里程**，不是单次门槛；单次门槛读 rangeSetting', 'echo',
              'audit.json 的 dates[].totalRange 是逐日累计 2.01→12.19；5 条合格记录的 totalRange 全是 14.200'),
    FieldSpec('step', 'setting', '1000', '1000', 'hard', '判定步数下限（与 stepSetting 同源）', 'probe',
              '999 步被判「运动步数不符合」'),
    FieldSpec('startQualifiedTime', 'setting', '6（分钟）', '6', 'hard', '时长窗口下界', 'echo', ''),
    FieldSpec('endQualifiedTime', 'setting', '21（分钟）', '21', 'hard', '时长窗口上界', 'echo', ''),
    FieldSpec('isStepRecursion', 'setting', '0', '0', 'hard',
              '=1 时步数下限随距离线性放大', 'doc', ''),
    FieldSpec('rangeSetting', 'setting', '2.000', '2.000', 'hard', '另一种口径的距离下限', 'echo', ''),
    FieldSpec('stepSetting', 'setting', '1000', '1000', 'hard', '另一种口径的步数下限', 'probe', ''),
    FieldSpec('limitSpeed', 'setting', '3.3', '3.33', 'soft', '速度上限（m/s）', 'echo', ''),
    FieldSpec('limitStride', 'setting', '2.3', '2.3', 'display', '步幅上限，仅展示', 'echo', ''),
    FieldSpec('limitStepFrequency', 'setting', '300', '300', 'display', '步频上限，仅展示', 'echo', ''),
    FieldSpec('remarkId', 'setting', '无', '131310', 'soft', '运动类型/备注 ID', 'probe', ''),
)

ALL_FIELDS: Tuple[FieldSpec, ...] = (SUBMIT_FIELDS + V2BASE_FIELDS + HEADER_FIELDS
                                    + POINT_FIELDS + ECHO_FIELDS + SETTING_FIELDS)


# ---------------------------------------------------------------------------
# 旧报文 → 新报文
# ---------------------------------------------------------------------------

def _fmt(value: Any, digits: int) -> str:
    try:
        return f'{float(value):.{digits}f}'
    except (TypeError, ValueError):
        return f'{0.0:.{digits}f}'


def _as_ms(value: Any) -> int:
    """时间戳统一成"整秒的毫秒值"。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number < 1e11:  # 秒级
        number *= 1000.0
    return int(round(number / 1000.0)) * 1000


def _pace_string(elapsed_s: float, distance_km: float) -> str:
    """按 7′02″ 的形式重排配速（U+2032 / U+2033）。"""
    if distance_km <= 0:
        return '0\u203200\u2033'
    per_km = elapsed_s / distance_km
    minutes = int(per_km // 60)
    seconds = int(round(per_km - minutes * 60))
    if seconds == 60:
        minutes, seconds = minutes + 1, 0
    return f'{minutes}\u2032{seconds:02d}\u2033'


def _fix_pace_text(text: Any) -> str:
    """把 10'00" 这类 ASCII 写法换成 ′″。"""
    if not isinstance(text, str):
        return _pace_string(float(text or 0), 1.0)
    text = text.strip()
    text = re.sub(r"[\u0027\u2018\u2019`]", '\u2032', text)
    text = re.sub(r'[\u0022\u201c\u201d]', '\u2033', text)
    return text


def _steps_total(step_numbers: Any, fallback: Any = None) -> int:
    try:
        if isinstance(step_numbers, str):
            segments = json.loads(step_numbers)
        elif isinstance(step_numbers, Sequence):
            segments = list(step_numbers)
        else:
            segments = []
        return int(sum(int(v) for v in segments))
    except (ValueError, TypeError, json.JSONDecodeError):
        try:
            return int(float(fallback))
        except (TypeError, ValueError):
            return 0


def canonical_point(
    point: Mapping[str, Any],
    index: int,
    gps_counter: Optional[int] = None,
    cumulative_steps: int = 0,
    paused: Optional[bool] = None,
) -> Dict[str, str]:
    """把任意来源的点位规整成 9 键全字符串的新版点位。"""
    lat = point.get('a', point.get('latitude', point.get('lat', 0)))
    lon = point.get('o', point.get('longitude', point.get('lon', point.get('lng', 0))))
    ts = point.get('t', point.get('time', point.get('timestamp', 0)))
    speed = point.get('s', point.get('speed', 0))
    steps = point.get('e', point.get('steps', point.get('step', cumulative_steps)))
    flag = point.get('i', point.get('paused'))
    if paused is None:
        paused = str(flag).strip().lower() in ('true', '1', 'yes')
    counter = point.get('c', point.get('gps', gps_counter if gps_counter is not None else index))
    try:
        counter_int = int(float(counter))
    except (TypeError, ValueError):
        counter_int = gps_counter if gps_counter is not None else index
    return {
        'a': _fmt(lat, 6),
        'c': str(counter_int),
        'e': str(int(float(steps or 0))),
        'i': 'true' if paused else 'false',
        'l': str(point.get('l', '1')),
        'm': str(point.get('m', '0')),
        'o': _fmt(lon, 6),
        's': _fmt(speed, 6),
        't': str(_as_ms(ts)),
    }


def canonical_points(points: Iterable[Mapping[str, Any]], total_steps: Optional[int] = None) -> List[Dict[str, str]]:
    """整条轨迹规整：末点 i 一定为 true，e 收敛到 total_steps。"""
    raw = [dict(p) for p in points]
    out: List[Dict[str, str]] = []
    for idx, point in enumerate(raw):
        out.append(canonical_point(point, idx))
    if not out:
        return out
    if total_steps is not None and total_steps >= 0:
        out[-1]['e'] = str(int(total_steps))
    out[-1]['i'] = 'true'
    return out


def migrate_legacy(payload: Mapping[str, Any], *, device: Optional[Mapping[str, str]] = None,
                   keep_identify: bool = False, device_id: Optional[str] = None) -> Dict[str, Any]:
    """把一份旧版（2.0.23）报文升级成新版（3.6.814）报文。

    只做"字段搬迁与格式化"，不编造任何业务数值：距离、时长、步数都按原值沿用，
    需要重新生成的是 identify（不是 UUID 时）、配速串、点位键集与设备画像。
    """
    data: Dict[str, Any] = dict(payload)
    profile = dict(NEW_DEVICE_PROFILE)
    if device:
        profile.update({k: str(v) for k, v in device.items()})
    data.update(profile)

    # 距离/时长/步数
    range_km = data.get('sportRange', data.get('formatSportRange', data.get('rangeSetting', 0)))
    try:
        range_km_f = float(range_km)
    except (TypeError, ValueError):
        range_km_f = 0.0
    if range_km_f > 100:  # 旧报文有把米当公里的写法
        range_km_f /= 1000.0
    data['sportRange'] = f'{range_km_f:.2f}'
    data['gpsDistance'] = data['sportRange']

    step_numbers = data.get('stepNumbers')
    total_steps = _steps_total(step_numbers, data.get('step'))
    if step_numbers is not None:
        data['stepNumbers'] = step_numbers if isinstance(step_numbers, str) else json.dumps(list(step_numbers))
    data['step'] = int(data.get('step') or total_steps)

    elapsed = data.get('sportTime', data.get('formatSportTime', 0))
    try:
        elapsed_s = float(elapsed)
    except (TypeError, ValueError):
        elapsed_s = 0.0
    data['sportTime'] = str(int(elapsed_s)) if elapsed_s else str(int(data.get('sportTime') or 0))
    data['speed'] = _fix_pace_text(data.get('speed')) or _pace_string(elapsed_s, range_km_f)
    try:
        data['avgSpeed'] = f'{range_km_f / float(data["sportTime"]) * 3600:.2f}'
    except (ValueError, ZeroDivisionError):
        data['avgSpeed'] = f'{0.0:.2f}'
    data.pop('avgspeed', None)

    # 标识
    identify = str(data.get('identify') or '')
    if not keep_identify and not UUID_RE.match(identify):
        data['identify'] = str(uuid.uuid4())
    data['okPointList'] = ''
    data['pointList'] = data.get('pointList') or ''
    try:
        data['uploadType'] = int(float(data.get('uploadType', 0)))
    except (TypeError, ValueError):
        data['uploadType'] = 0
    data['isFaceStatus'] = str(data.get('isFaceStatus', '1'))
    data.setdefault('isSequencePoint', '0')
    data.setdefault('remarkId', '131310')
    data.setdefault('d', 0)
    data.setdefault('f', 0)
    data.setdefault('m', 0)
    data.setdefault('h', 0)
    data.setdefault('isValid', 1)

    # 点位
    raw_points = data.get('gitudeLatitude') or data.get('points') or data.get('pointList') or []
    if isinstance(raw_points, str):
        try:
            raw_points = json.loads(raw_points)
        except json.JSONDecodeError:
            raw_points = []
    if isinstance(raw_points, Sequence) and raw_points and isinstance(raw_points[0], Mapping):
        data['gitudeLatitude'] = canonical_points(raw_points, total_steps or None)

    if device_id and 'environment' not in data:
        import tsn_env  # 延迟导入：字段表本身不该依赖环境探测
        data['environment'] = tsn_env.build_environment_payload(device_id=device_id)

    for legacy_key in ('points', 'formatSportRange', 'formatSportTime', 'runType', 'userId'):
        data.pop(legacy_key, None)
    return data


# ---------------------------------------------------------------------------
# 报文体检
# ---------------------------------------------------------------------------

@dataclass
class FieldIssue:
    field: str
    level: str          # error / warning
    message: str


def audit_submission(payload: Mapping[str, Any], *, expected_device: Optional[Mapping[str, str]] = None) -> List[FieldIssue]:
    """检查一份 addExerciseRecord 报文是否"像新版本 App 发出来的"。"""
    issues: List[FieldIssue] = []
    profile = dict(NEW_DEVICE_PROFILE)
    if expected_device:
        profile.update({k: str(v) for k, v in expected_device.items()})

    allowed = {k: set(v) | {profile[k]} for k, v in DEVICE_ALLOWED.items()}
    for key, want in profile.items():
        got = payload.get(key)
        if got is None:
            issues.append(FieldIssue(key, 'error', f'缺少 {key}（应为 {want}）'))
        elif str(got) not in allowed.get(key, {want}):
            issues.append(FieldIssue(key, 'error',
                                     f'{key}={got!r} 不在真实出现过的取值 {sorted(allowed.get(key, {want}))} 内'))

    identify = str(payload.get('identify') or '')
    if not UUID_RE.match(identify):
        issues.append(FieldIssue('identify', 'error', f'identify={identify!r} 不是 UUID'))

    speed = payload.get('speed')
    if not isinstance(speed, str) or not PACE_RE.match(speed):
        issues.append(FieldIssue('speed', 'error', f'speed={speed!r} 不是 ′″ 格式的配速串'))

    if str(payload.get('okPointList', '')) not in ('', 'None'):
        issues.append(FieldIssue('okPointList', 'warning', 'okPointList 应为空串（App 原生记录是 null）'))
    if payload.get('uploadType') is not None and not isinstance(payload.get('uploadType'), int):
        issues.append(FieldIssue('uploadType', 'warning', 'uploadType 应为整数'))
    if 'environment' not in payload:
        issues.append(FieldIssue('environment', 'error', '缺少 environment（v3.6.814 起必带）'))
    for flag in ('d', 'f', 'm', 'h'):
        if payload.get(flag) not in (None, 0, '0'):
            issues.append(FieldIssue(flag, 'warning', f'风险位 {flag}={payload.get(flag)!r} 非 0'))

    steps = payload.get('step')
    numbers = payload.get('stepNumbers')
    try:
        segments = json.loads(numbers) if isinstance(numbers, str) else list(numbers or [])
    except json.JSONDecodeError:
        segments = []
        issues.append(FieldIssue('stepNumbers', 'error', 'stepNumbers 不是合法 JSON 数组'))
    if segments:
        if not (8 <= len(segments) <= 30):
            issues.append(FieldIssue('stepNumbers', 'warning', f'段数 {len(segments)} 不在 8–30 之间'))
        if not all(isinstance(v, int) for v in segments):
            issues.append(FieldIssue('stepNumbers', 'error', 'stepNumbers 元素必须是整数'))
        if steps is not None and int(sum(segments)) != int(steps):
            issues.append(FieldIssue('stepNumbers', 'error',
                                     f'分段合计 {sum(segments)} ≠ step {steps}'))

    issues.extend(audit_points(payload.get('gitudeLatitude')))
    return issues


def audit_points(points: Any) -> List[FieldIssue]:
    """只体检轨迹数组（回声记录里也能直接跑，不需要凑齐提交字段）。"""
    issues: List[FieldIssue] = []
    if not (isinstance(points, Sequence) and points and isinstance(points[0], Mapping)):
        return [FieldIssue('gitudeLatitude', 'error', '缺少点位数组')]

    keyset = set().union(*(set(p) for p in points))
    if keyset != set(POINT_KEYS_NEW):
        extra = sorted(keyset - set(POINT_KEYS_NEW))
        missing = sorted(set(POINT_KEYS_NEW) - keyset)
        issues.append(FieldIssue('gitudeLatitude', 'error',
                                 f'点位键集不对：多 {extra} 缺 {missing}'))
    bad_type = [k for k in POINT_KEYS_NEW
                if any(not isinstance(p.get(k), str) for p in points if k in p)]
    if bad_type:
        issues.append(FieldIssue('gitudeLatitude', 'error', f'点位值必须是字符串：{bad_type}'))
    if points[-1].get('i') != 'true':
        issues.append(FieldIssue('gitudeLatitude', 'error', '末点 i 必须为 "true"'))
    for key in ('a', 'o', 's'):
        decimals = {len(str(p.get(key, '')).split('.')[-1]) for p in points if key in p}
        if decimals and decimals != {6}:
            issues.append(FieldIssue('gitudeLatitude', 'warning',
                                     f'{key} 小数位 {sorted(decimals)} 应统一为 6'))
    ts = [int(p.get('t', 0)) for p in points]
    if any(b < a for a, b in zip(ts, ts[1:])):
        issues.append(FieldIssue('gitudeLatitude', 'error', 't 出现倒退'))
    offgrid = [v % 1000 for v in ts]
    weird = [v for v in offgrid if v not in (0, 1, 2, 998, 999)]
    if weird and len(weird) > max(2, len(ts) * 0.02):
        issues.append(FieldIssue('gitudeLatitude', 'warning',
                                 f'{len(weird)}/{len(ts)} 个 t 的毫秒抖动超过 ±2 ms'))
    return issues


# ---------------------------------------------------------------------------
# 渲染文档
# ---------------------------------------------------------------------------

#: 真正必须替换的字段（group, name）：新增字段、取值变了、或结构变了的那批。
#: 其余字段新旧一致，或只是我们这边的展示口径不同，改不改都不影响判定。
MUST_MIGRATE: Tuple[Tuple[str, str], ...] = (
    ('submit', 'appVersion'),
    ('submit', 'identify'),
    ('submit', 'speed'),
    ('submit', 'sportRange'),
    ('submit', 'gpsDistance'),
    ('submit', 'geofence'),
    ('submit', 'environment'),
    ('submit', 'nonce'),
    ('submit', 'timestamp'),
    ('submit', 'isFaceStatus'),
    ('submit', 'step'),
    ('submit', 'stepNumbers'),
    ('submit', 'sportTime'),
    ('submit', 'startTime'),
    ('v2base', 'brand'),
    ('v2base', 'model'),
    ('v2base', 'system'),
    ('v2base', 'version'),
    ('point', 'a'),
    ('point', 'o'),
    ('point', 't'),
    ('point', 's'),
    ('point', 'c'),
    ('point', 'e'),
    ('point', 'i'),
    ('point', 'm'),
)

_GROUP_TITLES = {
    'submit': '一、提交报文（addExerciseRecord body）',
    'v2base': '二、v2 签名公共字段（每个请求都带，与业务字段一起算 sign）',
    'header': '三、HTTP 头',
    'point': '四、轨迹点（gitudeLatitude 数组元素）',
    'echo': '五、服务器回显（getExerciseRecord，用来读判定）',
    'setting': '六、服务器设置（getExerciseSetting，判定阈值来源）',
}
_LEVEL_LABEL = {
    'hard': '硬判定',
    'soft': '软判定/风控',
    'display': '仅展示',
    'none': '无影响',
}


def to_markdown() -> str:
    lines: List[str] = [
        '# 字段对照：旧版 v2.0.23 → 新版 v3.6.814',
        '',
        '> 本文件由 `tsn_fields.py` 渲染（`python tools/render_field_doc.py`），不要手改。',
        '> 证据口径：`echo`=服务器回显原文，`probe`=我们自己发请求的返回，`doc`=APK 逆向笔记。',
        '',
    ]
    # 零、TL;DR：真的需要动的那批字段（新增 / 取值变了 / 结构变了）
    lines.append('## 零、必须替换的字段（先改这张表）')
    lines.append('')
    lines.append(f'共 {len(MUST_MIGRATE)} 个。权重「硬」= 不改直接判不合格/被拦/403；「软」= 改了才不留画像痕迹（证据多为相关性）。')
    lines.append('')
    lines.append('| 字段 | 旧 → 新 | 权重 | 迁移动作 |')
    lines.append('| --- | --- | --- | --- |')
    index = {(s.group, s.name): s for s in ALL_FIELDS}
    for group, name in MUST_MIGRATE:
        spec = index[(group, name)]
        lines.append(f'| `{spec.name}` | {spec.old} → {spec.new} | {_LEVEL_LABEL.get(spec.judged, spec.judged)} | {spec.action} |')
    lines.append('')
    for group, title in _GROUP_TITLES.items():
        lines.append(f'## {title}')
        lines.append('')
        lines.append('| 字段 | 旧版（2.0.23） | 新版（3.6.814） | 判定权重 | 迁移动作 | 证据 |')
        lines.append('| --- | --- | --- | --- | --- | --- |')
        for spec in ALL_FIELDS:
            if spec.group != group:
                continue
            lines.append(
                f'| `{spec.name}` | {spec.old} | {spec.new} | {_LEVEL_LABEL.get(spec.judged, spec.judged)} '
                f'| {spec.action} | {spec.evidence} |'
            )
        lines.append('')
    lines.append('## 七、一键迁移')
    lines.append('')
    lines.append('```python')
    lines.append('from tsn_fields import migrate_legacy, audit_submission')
    lines.append('')
    lines.append('new_payload = migrate_legacy(old_payload)      # 字段搬迁 + 格式化')
    lines.append('for issue in audit_submission(new_payload):    # 体检')
    lines.append('    print(issue.level, issue.field, issue.message)')
    lines.append('```')
    lines.append('')
    return '\n'.join(lines)


__all__ = [
    'UUID_RE', 'PACE_RE', 'NEW_DEVICE_PROFILE', 'OLD_DEVICE_PROFILE', 'DEVICE_ALLOWED', 'POINT_KEYS_NEW',
    'FieldSpec', 'SUBMIT_FIELDS', 'V2BASE_FIELDS', 'HEADER_FIELDS', 'POINT_FIELDS', 'ECHO_FIELDS', 'SETTING_FIELDS', 'ALL_FIELDS',
    'canonical_point', 'canonical_points', 'migrate_legacy',
    'FieldIssue', 'audit_submission', 'audit_points', 'to_markdown',
]
