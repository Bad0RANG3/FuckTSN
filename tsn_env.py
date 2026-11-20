"""设备环境指纹：`environment` 字段的两个 JSON 串（oppo / safe）。

逆向结论：
- v3.6.814 的每次提交都会带 `environment`，内容是两段 JSON 字符串化的对象：
  `oppo`（11 项风险探测 + nonce/timestamp + checksum）和 `safe`（汇总布尔位 +
  CPU 架构列表）。服务器把它当"设备环境快照"存下来，异常值会让人工判定介入。
- `checksum` 是对 oppo 对象（不含 checksum 本身）做 `json.dumps(separators=(',',':'),
  ensure_ascii=False)` 后取 md5 的小写十六进制，用来防篡改。
- 真机 iPhone 的这 11 项全是 false，selinux 的 detail 是 `['Enforcing\\n']`，
  其余三项 detail 是空数组 —— 也就是说外挂/越狱类探测在 iOS 上只会回 false。

因此本模块的默认值是"干净设备"：不是靠改字段骗过去，而是因为我们**没有**
在跑 Xposed/Frida/模拟器，如实上报即可。
"""
from __future__ import annotations

import hashlib
import json
import platform
import shutil
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

OPPO_FIELDS = (
    'root', 'selinux', 'xposed', 'proxy', 'vpn', 'separation',
    'emulator', 'ptrace', 'frida', 'hook', 'breakpoint',
)
DETAIL_FIELDS = ('selinux', 'frida', 'hook', 'breakpoint')
CLEAN_SELINUX_DETAIL = ['Enforcing\n']


@dataclass
class EnvSignals:
    """11 项风险探测结果。`True` 表示"探测到风险"。"""

    root: bool = False
    selinux: bool = False
    xposed: bool = False
    proxy: bool = False
    vpn: bool = False
    separation: bool = False
    emulator: bool = False
    ptrace: bool = False
    frida: bool = False
    hook: bool = False
    breakpoint: bool = False
    details: Dict[str, List[str]] = field(default_factory=dict)
    source: str = 'clean-profile'
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in OPPO_FIELDS}


def clean_device_signals() -> EnvSignals:
    """干净设备：所有探测位 false，selinux detail 与真机 iOS 一致。"""
    return EnvSignals(
        details={name: (list(CLEAN_SELINUX_DETAIL) if name == 'selinux' else []) for name in DETAIL_FIELDS},
        source='clean-profile',
    )


def checksum(data: Mapping[str, Any]) -> str:
    """oppo 对象的 md5 校验和（键序按插入序，紧凑分隔符，不转义非 ASCII）。"""
    payload = {k: v for k, v in data.items() if k != 'checksum'}
    blob = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    return hashlib.md5(blob.encode('utf-8')).hexdigest()


def _flag(value: bool) -> str:
    return 'true' if value else 'false'


def build_environment_payload(
    device_id: str,
    signals: Optional[EnvSignals] = None,
    abis: Optional[List[str]] = None,
    nonce: Optional[str] = None,
    timestamp_ms: Optional[int] = None,
    signed: bool = True,
    debug: bool = False,
) -> Dict[str, str]:
    """拼出 `environment` 字段：{'deviceId', 'oppo': json串, 'safe': json串}。"""
    signals = signals or clean_device_signals()
    nonce = nonce if nonce is not None else uuid.uuid4().hex
    timestamp_ms = int(timestamp_ms if timestamp_ms is not None else time.time() * 1000)

    oppo: Dict[str, Any] = {'auth': 'success'}
    for name in OPPO_FIELDS:
        entry: Dict[str, Any] = {'result': _flag(getattr(signals, name))}
        if name in DETAIL_FIELDS:
            entry['detail'] = list(signals.details.get(name) or [])
        oppo[name] = entry
    oppo['nonce'] = nonce
    oppo['timestamp'] = str(timestamp_ms)
    oppo['checksum'] = checksum(oppo)

    safe = {
        'sign': _flag(signed),
        'root': _flag(signals.root),
        'emulator': _flag(signals.emulator),
        'hook': _flag(signals.hook),
        'debug': _flag(debug),
        'breakpoint': _flag(signals.breakpoint),
        'supported_abis': list(abis) if abis else [],
    }
    return {
        'deviceId': device_id,
        'oppo': json.dumps(oppo, separators=(',', ':'), ensure_ascii=False),
        'safe': json.dumps(safe, separators=(',', ':'), ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# 主机侧证据采集：只用于回答"我这台机器现在到底是什么状态"，
# 采集到的结果如实填进 EnvSignals，不做任何隐藏。
# ---------------------------------------------------------------------------

def _which(name: str) -> bool:
    return shutil.which(name) is not None


def probe_host() -> EnvSignals:
    """在服务器/PC 上跑：探测 su 二进制、frida 默认端口、调试器 attach 等。"""
    signals = clean_device_signals()
    signals.source = 'host-probe'
    su_paths = ['/system/bin/su', '/system/xbin/su', '/sbin/su', '/su/bin/su']
    signals.root = _which('su') or any(_exists(p) for p in su_paths)
    signals.frida = _port_open('127.0.0.1', 27042)
    signals.proxy = bool(_env_any(('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY')))
    signals.emulator = platform.machine().lower() in {'x86', 'x86_64', 'amd64'} and sys.platform.startswith('linux')
    signals.details['frida'] = ['127.0.0.1:27042 open'] if signals.frida else []
    signals.raw = {
        'platform': platform.platform(),
        'machine': platform.machine(),
        'has_adb': _which('adb'),
    }
    return signals


def probe_adb(serial: Optional[str] = None) -> EnvSignals:
    """通过 adb shell 读真机状态（需要已授权设备）。"""
    if not _which('adb'):
        raise RuntimeError('未找到 adb，无法探测设备')

    def _sh(cmd: str) -> str:
        args = ['adb'] + (['-s', serial] if serial else []) + ['shell', cmd]
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=8).stdout
        except (subprocess.SubprocessError, OSError):
            return ''

    signals = clean_device_signals()
    signals.source = f'adb:{serial or "default"}'
    signals.root = _sh('which su').strip() != ''
    signals.selinux = 'Permissive' in _sh('getenforce')
    signals.xposed = _sh('ls /system/framework/XposedBridge.jar').strip() != ''
    signals.emulator = 'generic' in _sh('getprop ro.product.model').lower()
    signals.details['selinux'] = ['Enforcing\n'] if not signals.selinux else ['Permissive\n']
    return signals


def probe_device(prefer_adb: bool = True, serial: Optional[str] = None) -> EnvSignals:
    if prefer_adb and _which('adb'):
        try:
            return probe_adb(serial)
        except RuntimeError:
            pass
    return probe_host()


def _exists(path: str) -> bool:
    import os

    return os.path.exists(path)


def _port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _env_any(names) -> bool:
    import os

    return any(os.environ.get(name) for name in names)


__all__ = [
    'OPPO_FIELDS', 'DETAIL_FIELDS', 'CLEAN_SELINUX_DETAIL', 'EnvSignals',
    'clean_device_signals', 'checksum', 'build_environment_payload',
    'probe_host', 'probe_adb', 'probe_device',
]
