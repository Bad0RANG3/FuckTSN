"""加解密：请求体 AES 对称加密 + 密码 RSA 非对称加密。

逆向结论（决定了这里的每一个参数）：
- 请求体：客户端每次调用生成一个 16 字节随机 AES key，用 AES/ECB/PKCS7 加密
  真实参数 JSON，再用服务器 RSA 公钥（PKCS#1 v1.5）加密这个 key；两者一起
  放在 `key` / `param` 字段里。ECB 模式没有 IV。
- 登录密码：固定 key/iv（明文口令）走 AES 加密 + **零填充**（不是 PKCS7），key/iv 都是固定串
  `thanks,pig4cloud`；零填充意味着明文长度必须刚好对齐 16 字节，否则解出来会
  带尾随 0x00。
- 旧版私有接口的 sign：AES/CBC/PKCS7，key/iv 来自 md5(token) 的前后 16 位。

注意 `key` 长度：AES 只接受 16/24/32 字节，这里固定 16 字节（md5 十六进制串
取前 16 个字符，正好是 16 字节 ASCII）。
"""
from __future__ import annotations

import base64
from typing import Union

from Crypto.Cipher import AES

BLOCK_SIZE = 16
RSA_MAX_CHUNK = 117  # 1024-bit RSA + PKCS#1 v1.5 padding 单块最多 117 字节


class CryptoError(RuntimeError):
    """加解密失败。"""


def pkcs7_pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    pad_length = block_size - len(data) % block_size
    return data + bytes([pad_length] * pad_length)


def pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_length = data[-1]
    if pad_length < 1 or pad_length > BLOCK_SIZE:
        raise CryptoError(f'PKCS7 填充非法: 末字节 {pad_length}')
    return data[:-pad_length]


def zero_pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    """零填充：补 (块长 - 长度%块长) 个 0x00。

    注意与 PKCS7 的差别：长度已经对齐时仍会补满一整块 16 个零 —— 真机就是这么做的
    （16 位密码加密后密文是 48 字节 base64），服务端按尾部剥零还原。
    """
    return data + b'\x00' * (block_size - len(data) % block_size)


def zero_unpad(data: bytes) -> bytes:
    return data.rstrip(b'\x00')


def bytes_pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    """历史别名：对 bytes 做 PKCS7 填充。"""
    return pkcs7_pad(data, block_size)


class AESCrypto:
    """AES 加解密：默认 CBC，`ecb_mode=True` 时 ECB（此时 iv 可为 None）。"""

    def __init__(self, key: Union[str, bytes], iv: Union[str, bytes, None] = None, ecb_mode: bool = False):
        if isinstance(key, str):
            key = key.encode()
        if isinstance(iv, str):
            iv = iv.encode()
        if len(key) not in (16, 24, 32):
            raise CryptoError(f'AES key 长度必须是 16/24/32 字节，当前 {len(key)}')
        if not ecb_mode and (iv is None or len(iv) != BLOCK_SIZE):
            raise CryptoError('CBC 模式需要 16 字节 IV')
        self.key = key
        self.iv = iv
        self.ecb_mode = ecb_mode

    @property
    def _mode(self):
        return AES.MODE_ECB if self.ecb_mode else AES.MODE_CBC

    def _cipher(self):
        if self.ecb_mode:
            return AES.new(key=self.key, mode=AES.MODE_ECB)
        return AES.new(key=self.key, mode=AES.MODE_CBC, IV=self.iv)

    def encrypt(self, text: Union[str, bytes], zero_padding: bool = False, is_bytes: bool = False) -> str:
        """返回 base64 后的密文。三种填充互斥：bytes→PKCS7(bytes)、零填充、PKCS7。"""
        if is_bytes:
            padded = pkcs7_pad(bytes(text))
        elif zero_padding:
            padded = zero_pad(text.encode() if isinstance(text, str) else bytes(text))
        else:
            padded = pkcs7_pad(text.encode() if isinstance(text, str) else bytes(text))
        return base64.b64encode(self._cipher().encrypt(padded)).decode()

    def decrypt(self, encrypted_text: Union[str, bytes], return_bytes: bool = False) -> Union[str, bytes]:
        """PKCS7 去填充；`return_bytes=True` 时不尝试 utf-8 解码。"""
        raw = base64.b64decode(encrypted_text)
        plain = pkcs7_unpad(self._cipher().decrypt(raw))
        return plain if return_bytes else plain.decode('utf-8')

    def decrypt_zero_padded(self, encrypted_text: Union[str, bytes], return_bytes: bool = False) -> Union[str, bytes]:
        """零填充密文的解密（登录密码那条链路用）。"""
        raw = base64.b64decode(encrypted_text)
        plain = zero_unpad(self._cipher().decrypt(raw))
        return plain if return_bytes else plain.decode('utf-8')


class RSACrypto:
    """RSA 公钥加密：输入裸公钥字符串（无 PEM 头尾），输出 base64 密文。"""

    def __init__(self, public_key: Union[str, bytes]):
        self.public_key: bytes = self._format_public_key(public_key)

    @staticmethod
    def _format_public_key(key: Union[str, bytes]) -> bytes:
        if isinstance(key, bytes):
            key = key.decode()
        key = key.replace('\n', '').replace('\r', '').strip()
        if key.startswith('-----'):
            return key.encode()
        body = '\n'.join(key[i:i + 64] for i in range(0, len(key), 64))
        return f'-----BEGIN PUBLIC KEY-----\n{body}\n-----END PUBLIC KEY-----'.encode()

    def _load(self):
        import rsa

        return rsa.PublicKey.load_pkcs1_openssl_pem(self.public_key)

    def encrypt_bytes(self, data: bytes) -> str:
        import rsa

        return base64.b64encode(rsa.encrypt(data, self._load())).decode()

    def encrypt(self, message: str) -> str:
        """长文本分块加密（1024-bit 公钥单块 117 字节），密文直接拼接。"""
        import rsa

        public_key = self._load()
        plain = message.encode()
        chunks = b''.join(
            rsa.encrypt(plain[i:i + RSA_MAX_CHUNK], public_key)
            for i in range(0, len(plain), RSA_MAX_CHUNK)
        )
        return base64.b64encode(chunks).decode()


__all__ = [
    'BLOCK_SIZE', 'RSA_MAX_CHUNK', 'CryptoError',
    'pkcs7_pad', 'pkcs7_unpad', 'zero_pad', 'zero_unpad', 'bytes_pad',
    'AESCrypto', 'RSACrypto',
]
