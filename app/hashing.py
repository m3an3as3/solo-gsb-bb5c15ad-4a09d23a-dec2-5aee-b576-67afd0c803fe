"""承诺哈希方案。

承诺值 = SHA-256( 原文字节长度的 8 字节大端整数 || 原文 UTF-8 字节 || 盐 UTF-8 字节 )

长度前缀用于消除「原文 + 盐」拼接的歧义，使不同分段方式得到不同摘要。
"""

import hashlib

HASH_ALGORITHM = "sha256"
HASH_SCHEME = "sha256(u64be(len(utf8(text))) || utf8(text) || utf8(salt))"


def compute_commitment(text: str, salt: str) -> str:
    """按约定方案计算承诺哈希，返回 64 位小写十六进制字符串。"""
    text_bytes = text.encode("utf-8")
    salt_bytes = salt.encode("utf-8")
    digest = hashlib.sha256()
    digest.update(len(text_bytes).to_bytes(8, "big"))
    digest.update(text_bytes)
    digest.update(salt_bytes)
    return digest.hexdigest()
