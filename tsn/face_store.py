"""人脸图片本地缓存与下载安全。

原来的实现直接 `open(face_dir / f'{image_id}{ext}')`，而 `image_id` / `ext`
都来自服务端 JSON —— 一个被篡改（或本身有漏洞）的学校网关就能让客户端
写到 `../../` 之外，或者把一个任意 URL 变成 SSRF 探针。

本模块把三件事收口：
1. 目录分量只允许 `[A-Za-z0-9_-]`，杜绝路径穿越；
2. 扩展名只允许 jpg/jpeg/png，且以真实内容嗅探为准（不信 URL）；
3. 下载前校验 scheme/host —— 拒绝环回、内网、链路本地地址与非 http(s)。
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import urllib.parse
from pathlib import Path

from loguru import logger

from sdk.errors import ErrorCode, TiShiNengError

SAFE_COMPONENT = re.compile(r'[^A-Za-z0-9_.-]')
ALLOWED_EXT = {'.jpg', '.jpeg', '.png'}
MAX_IMAGE_BYTES = int(os.getenv('TSN_MAX_FACE_BYTES', 10 * 1024 * 1024))
DEFAULT_EXT = '.jpg'

_MAGIC = {
    b'\xff\xd8\xff': '.jpg',
    b'\x89PNG\r\n\x1a\n': '.png',
}


def safe_component(value, default: str = '0') -> str:
    text = SAFE_COMPONENT.sub('_', str(value or '').strip())
    text = text.strip('._')[:64]
    return text or default


def build_face_dir(root, school_id, user_id) -> Path:
    return Path(root) / safe_component(school_id) / safe_component(user_id)


def extension_for(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    ext = Path(path).suffix.lower()
    return ext if ext in ALLOWED_EXT else DEFAULT_EXT


def sniff_image(data: bytes) -> str | None:
    for magic, ext in _MAGIC.items():
        if data.startswith(magic):
            return ext
    return None


def _is_private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # 解析不了就别下
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def validate_image_url(url: str) -> str:
    """返回可安全下载的 URL，否则抛 TiShiNengError。"""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise TiShiNengError(f'人脸图片地址协议不受支持: {parsed.scheme or "(空)"}', ErrorCode.FACE_DOWNLOAD_FAILED)
    host = parsed.hostname or ''
    if not host:
        raise TiShiNengError('人脸图片地址缺少主机名', ErrorCode.FACE_DOWNLOAD_FAILED)
    if os.getenv('TSN_FACE_TRUST_HOSTS'):
        trusted = {h.strip().lower() for h in os.environ['TSN_FACE_TRUST_HOSTS'].split(',') if h.strip()}
        if host.lower() in trusted:
            return url
    if _is_private_host(host):
        logger.warning('拒绝下载疑似内网地址: {}', host)
        raise TiShiNengError(f'人脸图片地址指向内网/环回地址，已拒绝: {host}', ErrorCode.FACE_DOWNLOAD_FAILED)
    return url


def store_image(directory: Path, image_id, data: bytes) -> Path:
    if len(data) > MAX_IMAGE_BYTES:
        raise TiShiNengError(f'人脸图片过大({len(data)} bytes)，已拒绝保存', ErrorCode.FACE_DOWNLOAD_FAILED)
    ext = sniff_image(data)
    if ext is None:
        raise TiShiNengError('下载内容不是 JPEG/PNG 图片，已拒绝保存', ErrorCode.FACE_DOWNLOAD_FAILED)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{safe_component(image_id, "face")}{ext}'
    path.write_bytes(data)
    return path


def list_images(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT)
