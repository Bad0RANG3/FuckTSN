import hashlib
import io
import time
import urllib.parse

from loguru import logger

from tsn import protocol
from tsn import routes
from tsn.crypto import AESCrypto
from tsn.crypto import RSACrypto
from sdk.errors import TiShiNengError
from sdk.base import TiShiNengSdkBase

# 私有化部署版：报文是 {returnCode, returnMsg, data} 三元组，
# 且部分接口用「token 派生密钥」再签一次（getSign2）。
# 报文分类统一走 tsn_protocol，不再各写一套 if/else。


class TiShiNengPrivate:

    appId = '791f3afb1da44db3bd0c67ffdfb2394b'
    appSecret = 'e8167ef026cbc5e456ab837d9d6d9254'
    appSign = '7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3'

    def __init__(self, uid, schoolId, schoolCode, isOpenEncry, deviceId, brandName, deviceNum, token):
        self.uid = uid
        self.schoolCode = schoolCode
        self.isOpenEncry = isOpenEncry
        self.schoolId = schoolId
        self.deviceId = deviceId
        self.brandName = brandName
        self.deviceNum = deviceNum
        self.schoolUrl = ''
        self.platform = '1'
        self.tiShiNengBaseClient = TiShiNengSdkBase(uid, schoolId, deviceId, brandName, deviceNum, token)
        self.versionName = self.tiShiNengBaseClient.versionName
        self.version = self.tiShiNengBaseClient.version
        self.core = self.tiShiNengBaseClient.core
        self.token = token
        self.rsaUtils = RSACrypto('MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAq4laolA7zAk7jzsqDb3Oa5pS/uCPlZfASK8Soh/NzEmry77QDZ2koyr96M5Wx+A9cxwewQMHzi8RoOfb3UcQO4UDQlMUImLuzUnfbk3TTppijSLH+PU88XQxcgYm2JTa546c7JdZSI6dBeXOJH20quuxWyzgLk9jAlt3ytYygPQ7C6o6ZSmjcMgE3xgLaHGvixEVpOjL/pdVLzXhrMqWVAnB/snMjpCqesDVTDe5c6OOmj2q5J8n+tzIXtnvrkxQSDaUp8DWF8meMwyTErmYklMXzKic2rjdYZpHh4x98Fg0Q28sp6i2ZoWiGrJDKW29mntVQQiDNhKDawb4B45zUwIDAQAB')
        self.headers = self._build_headers()

    # ---- 兼容层 -----------------------------------------------------
    @property
    def httpClient(self):
        return self.core.client

    @httpClient.setter
    def httpClient(self, value):
        self.core.client = value

    def isPublic(self):
        return False

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

    def _build_headers(self) -> dict:
        return {
            'token': self.token,
            'channel': 'Android',
            'version': self.version,
            'type': '0',
            'Content-Type': 'application/x-www-form-urlencoded',
            'accept-encoding': 'gzip',
            'user-agent': 'okhttp/4.9.0',
        }

    # ---- 状态 -------------------------------------------------------
    def setAccessToken(self, token):
        self.token = token
        self.headers = self._build_headers()
        self.core.set_token(token)

    def setSchoolUrl(self, schoolUrl):
        if not schoolUrl:
            return
        normalized = routes.normalize_base_url(schoolUrl)
        self.schoolUrl = normalized.rstrip('/')
        self.core.set_base_url(self.schoolUrl)

    def setAppId(self, appId):
        self.appId = appId

    # ---- 签名 -------------------------------------------------------
    def getTokenAesEncrypt(self, text):
        md5Token = self.getMd5(self.token)
        return AESCrypto(md5Token[0:16], md5Token[16:32]).encrypt(text)

    def getEncryptAppId(self, timeStamp):
        return self.getMd5(self.getTokenAesEncrypt(self.appId + self.token + timeStamp))

    def getEncryptAppSecret(self, timeStamp):
        return self.getMd5(self.getTokenAesEncrypt(self.token + timeStamp))

    def _concat(self, params: dict) -> str:
        return '&'.join(self.kVtoStr(key, params[key], False) for key in sorted(params.keys()))

    def getSign2(self, params: dict, timestamp: str):
        params.pop('sign', None)
        params.update({'appId': self.getEncryptAppId(timestamp), 'appSecret': self.getEncryptAppSecret(timestamp)})
        return self.getTokenAesEncrypt(self._concat(params))

    def getSign(self, params: dict):
        params.pop('sign', None)
        params.update({'appId': self.appId, 'appSecret': self.appSecret})
        return self.getMd5(self._concat(params))

    # ---- 请求 -------------------------------------------------------
    def _needs_sign2(self, url: str) -> bool:
        return self.isOpenEncry and ('app/sportRecordSetting/getSetting' in url or 'app/appSportRecord/appAddSportRecord' in url)

    async def httpPost(self, url, data):
        """返回 data 字段；失败抛 TiShiNengError（含报文分类说明）。"""
        payload = dict(data or {})
        timestamp = str(int(time.time() * 1000))
        payload['timestamp'] = timestamp
        payload['sign'] = self.getSign2(payload.copy(), timestamp) if self._needs_sign2(url) else self.getSign(payload.copy())
        result = await self.core.send('POST', url, data=payload, headers=self.headers, context=url)
        if not result.ok:
            raise result.to_error()
        return result.data

    # ---- 登录前置接口（复用 base） ----------------------------------
    async def findAllProvince(self):
        return await self.tiShiNengBaseClient.findAllProvince()

    async def listSchoolByProvinceId(self, provinceId):
        return await self.tiShiNengBaseClient.listSchoolByProvinceId(provinceId)

    async def getSchoolById(self, schoolId):
        return await self.tiShiNengBaseClient.getSchoolById(schoolId)

    async def addUploadRecord(self):
        return await self.tiShiNengBaseClient.addUploadRecord()

    async def stuAppVersion2(self):
        return await self.tiShiNengBaseClient.stuAppVersion2()

    async def getReplyFeedBackCountUser(self):
        return await self.tiShiNengBaseClient.getReplyFeedBackCountUser()

    # ---- 业务接口 ---------------------------------------------------
    async def appAnnouncementContentList(self):
        return await self.httpPost('/app/announcementContent/appAnnouncementContentList', {})

    async def appLogin(self, username, password):
        return await self.httpPost('/app/appstu/login', {'uname': username, 'pwd': self.rsaUtils.encrypt(password)})

    async def noReadNotice(self):
        return await self.httpPost('/app/appnotice/noReadNotice', {'userId': self.uid, 'identity': '0'})

    async def ispwdmod(self, username):
        return await self.httpPost('/app/ispwdmod', {'userNum': username})

    async def appListNoticeData(self):
        return await self.httpPost('/app/appnotice/appListNoticeData', {'userId': self.uid, 'identity': '0', 'pageIndex': '1', 'pageSize': '10'})

    async def getStudentInfo(self):
        return await self.httpPost('/app/getStudentInfo', {})

    async def getCampusList(self):
        return await self.httpPost('/app/syscampus/getCampusList', {})

    async def getFieldByCampus(self, campusId):
        return await self.httpPost('/app/field/getFieldByCampus', {'campusId': campusId})

    async def sumSportRecord(self):
        return await self.httpPost('/app/appSportRecord/sumSportRecord', {'userId': self.uid})

    async def sportRecordSetting(self):
        return await self.httpPost('/app/sportRecordSetting/setting', {'uid': self.uid})

    async def getSportSetting(self, runType=2):
        return await self.httpPost('/app/sportRecordSetting/getSetting', {'uid': self.uid, 'runType': runType})

    async def getSportSpecification(self, runType=2):
        return await self.httpPost('/app/appSportRecord/getSportSpecification', {'runType': runType})

    async def getRunningStartTime(self, identify):
        return await self.httpPost('/app/sportRecordSetting/getRunningStartTime', {'identify': identify})

    async def appSportRecordList(self, sportType=2, pageIndex=1, pageSize=10):
        return await self.httpPost('/app/appSportRecord/appSportRecordList', {'userId': self.uid, 'sportType': str(sportType), 'pageIndex': str(pageIndex), 'pageSize': str(pageSize)})

    async def getSportRecordId(self, sportRecordId):
        return await self.httpPost('/app/appSportRecord/getSportRecordId', {'sportRecordId': sportRecordId})

    async def appAddSportRecord(self, runType, startTime, endTime, gitudeLatitude, identify, formatSportTime, formatSportRange, avgspeed, speed, okPointList, stepNumbers, isFaceStatus, points, uploadType):
        params = {
            'userId': self.uid, 'runType': runType, 'startTime': startTime, 'endTime': endTime,
            'gitudeLatitude': gitudeLatitude, 'identify': identify,
            'formatSportTime': formatSportTime, 'formatSportRange': formatSportRange,
            'avgspeed': avgspeed, 'speed': speed, 'okPointList': okPointList,
            'brand': self.brandName, 'model': self.deviceNum, 'system': 'Android',
            'version': '13', 'appVersion': self.versionName, 'stepNumbers': stepNumbers,
            'isFaceStatus': isFaceStatus, 'points': points, 'uploadType': uploadType,
        }
        return await self.httpPost('/app/appSportRecord/appAddSportRecord', params)

    async def appRuningFace(self, faceBytes: bytes, identify):
        timestamp = str(int(time.time() * 1000))
        data = {'userId': self.uid, 'identify': identify, 'timestamp': timestamp}
        sign = self.getSign(dict(data))
        headers = self.headers.copy()
        headers.pop('Content-Type', None)
        fields = {
            'userId': (None, str(self.uid).encode(), 'multipart/form-data; charset=utf-8'),
            'identify': (None, identify.encode(), 'multipart/form-data; charset=utf-8'),
            'file': (f'avatar{timestamp}.png', io.BytesIO(faceBytes), 'image/png'),
            'timestamp': (None, timestamp.encode(), 'multipart/form-data; charset=utf-8'),
            'sign': (None, sign.encode(), 'multipart/form-data; charset=utf-8'),
        }
        result = await self.core.send(
            'POST', '/app/runingFace/appRuningFace', files=fields,
            headers=headers, timeout=30, context='appRuningFace',
        )
        result.raise_for_error()
        return result.data

    async def aclose(self):
        await self.tiShiNengBaseClient.aclose()
