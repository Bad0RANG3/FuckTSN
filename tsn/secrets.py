"""本地凭据加密存储。

SQLite 里的 `password` 列保存的不再是明文，而是 `enc:v1:<base64>` 形式的
AES-GCM 密文。密钥文件位于：

    Windows: %LOCALAPPDATA%/tsn/secret.key
    其它:    ~/.tsn/secret.key
    覆盖:    环境变量 TSN_KEY_FILE

读取时兼容历史明文（无前缀），便于从旧库平滑迁移；`migrate_legacy`
可在登录成功后把明文回写为密文。

设计约束：
- 只做「本地静态存储」保护，不改变线上协议（服务端仍按原方式接收密码）；
- 密钥文件丢失 = 所有已存密码不可解，需要重新授权（这是有意的取舍：
  密钥与库放在不同目录，单偷一个 db 文件不足以还原密码）。
"""
from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from Crypto.Cipher import AES

_PREFIX = 'enc:v1:'
_NONCE_SIZE = 12


def key_path() -> Path:
    override = os.getenv('TSN_KEY_FILE')
    if override:
        return Path(override)
    base = os.getenv('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return Path(base) / 'tsn' / 'secret.key'


def _load_key(create: bool = True) -> bytes:
    path = key_path()
    if path.exists():
        raw = path.read_bytes().strip()
        if len(raw) >= 32:
            return raw[:32]
    if not create:
        raise FileNotFoundError(f'找不到凭据密钥文件: {path}')
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 上 chmod 语义有限，密钥目录本身尽量别共享
    return key


def is_encrypted(value: str | None) -> bool:
    return bool(value) and str(value).startswith(_PREFIX)


def encrypt_secret(plain: str | None) -> str:
    """加密；空值原样返回，已是密文则不变（幂等）。"""
    if not plain:
        return plain or ''
    if is_encrypted(plain):
        return plain
    key = _load_key()
    nonce = secrets.token_bytes(_NONCE_SIZE)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(str(plain).encode('utf-8'))
    blob = base64.b64encode(nonce + tag + ct).decode('ascii')
    return _PREFIX + blob


def decrypt_secret(value: str | None) -> str:
    """解密；历史明文原样返回，保证旧库可用。"""
    if not value:
        return ''
    if not is_encrypted(value):
        return value
    blob = base64.b64decode(value[len(_PREFIX):])
    nonce, tag, ct = blob[:_NONCE_SIZE], blob[_NONCE_SIZE:_NONCE_SIZE + 16], blob[_NONCE_SIZE + 16:]
    key = _load_key(create=False)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ct, tag).decode('utf-8')


def migrate_legacy(value: str | None) -> str:
    """明文 -> 密文；已加密或空值原样返回。"""
    if not value or is_encrypted(value):
        return value or ''
    return encrypt_secret(value)
