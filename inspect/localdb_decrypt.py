"""本地库解密探针二：用 salt 后 32 字节当 key，试解密 page1。

只读拷贝文件，不碰原库。SQLCipher 4 版式：页 4096，保留区 80，
IV 藏在保留区里 —— 位置不确定就把几种组合都试一遍，
page1 明文以 b'SQLite format 3\\x00' 开头即中。
"""
from __future__ import annotations

from Crypto.Cipher import AES

DB_COPY = r"C:\Users\almz7\AppData\Local\Temp\opencode\msg0.db"
SALT_HEX = "bf62ab35ee461032ff44ce47736f5643"
PAGE_SIZE = 4096
RESERVE = 80
USABLE = PAGE_SIZE - RESERVE

# 上一步内存扫描里，salt 命中 #3 后面紧跟的 32 字节（64 hex）
MEMORY_KEYS = [
    bytes.fromhex("aed420b5b0f4b79a572bdc085afd228d3b2c63d070ef196c2bed82af9d677be09"),
]


def _decrypt_page1_candidates(data: bytes, keys: list[bytes]) -> None:
    page1 = data[:PAGE_SIZE]
    if len(page1) < PAGE_SIZE:
        print("文件不足一页")
        return
    ciphertext = page1[:USABLE]
    reserve = page1[USABLE:]
    iv_candidates = {
        "reserve[:16]": reserve[:16],
        "reserve[16:32]": reserve[16:32],
        "reserve[-16:]": reserve[-16:],
        "zeros": bytes(16),
    }
    for ki, key in enumerate(keys):
        if len(key) != 32:
            print(f"key{ki} 长度 {len(key)} 不是 32，跳过")
            continue
        for name, iv in iv_candidates.items():
            try:
                plain = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
            except Exception as exc:
                print(f"key{ki}/{name} 解密出错：{exc}")
                continue
            if plain.startswith(b"SQLite format 3\x00"):
                print(f"*** 命中 key{ki} + {name}，page1 魔数正确 ***")
                print(f"    前 100 字节：{plain[:100]!r}")
                return
    print("都没中：key 或版式不对，换组合再试")


def main() -> int:
    with open(DB_COPY, "rb") as handle:
        data = handle.read()
    print(f"拷贝 {len(data)} 字节，头 salt={data[:16].hex()}")
    assert data[:16].hex() == SALT_HEX
    _decrypt_page1_candidates(data, MEMORY_KEYS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
