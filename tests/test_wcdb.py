"""解密库会话来源的回归测试：合成向量，不碰真实微信和真实密钥。

覆盖：
* verify_enc_key：对的钥匙过，错的钥匙/坏页不过；
* decrypt_database 往返：用测试钥匙按同版式加密一页，解出来是原文；
* display_name：备注优先，其次昵称；
* read_sessions/read_all_groups/read_all_friends：映射、人数、排序；
* WcdbReader：缺钥匙/缺文件/验签失败都报不可用，不抛别的错；
* RealConversationSource：解密路优先，失败回落内存路。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import sqlite3
import struct
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from pyweixin import wcdb  # noqa: E402
from pyweixin.conversations import (  # noqa: E402
    ConversationKind,
    RealConversationSource,
)
from pyweixin.wcdb import (  # noqa: E402
    PAGE_SIZE,
    RESERVE_SIZE,
    SQLITE_MAGIC,
    WcdbReader,
    WcdbUnavailable,
    decrypt_database,
    display_name,
    read_all_friends,
    read_all_groups,
    read_sessions,
    verify_enc_key,
)

TEST_KEY = bytes(range(32))


def _encrypt_page1(body: bytes) -> bytes:
    """按 SQLCipher4 版式造一页：salt + AES(4000B体) + IV + HMAC。

    HMAC 拼接与线上一致（真库验证过）：HMAC(ct + iv) + LE32(1)。
    解密端会在前面补 magic（见 decrypt_database）。
    """
    from Crypto.Cipher import AES

    assert len(body) == PAGE_SIZE - RESERVE_SIZE - wcdb.SALT_SIZE
    salt = b"SALT" + b"1" * 12
    iv = b"IV" + b"2" * 14
    assert len(salt) == wcdb.SALT_SIZE and len(iv) == 16
    ct = AES.new(TEST_KEY, AES.MODE_CBC, iv).encrypt(body)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", TEST_KEY, mac_salt, 2, dklen=32)
    hm = hmac_mod.new(mac_key, ct + iv, hashlib.sha512)
    hm.update(struct.pack("<i", 1))
    return salt + ct + iv + hm.digest()


class VerifyTests(unittest.TestCase):
    def test_good_key_passes_bad_key_fails(self):
        body = b"A" * (PAGE_SIZE - RESERVE_SIZE - wcdb.SALT_SIZE)
        page1 = _encrypt_page1(body)
        self.assertTrue(verify_enc_key(TEST_KEY, page1))
        self.assertFalse(verify_enc_key(b"\x01" * 32, page1))
        self.assertFalse(verify_enc_key(TEST_KEY, b"\x00" * PAGE_SIZE))
        self.assertFalse(verify_enc_key(b"short", page1))


class DecryptRoundTripTests(unittest.TestCase):
    def test_page1_round_trip(self):
        body = b"B" * (PAGE_SIZE - RESERVE_SIZE - wcdb.SALT_SIZE)
        with tempfile.TemporaryDirectory(prefix="pyw-wcdb-") as tmp:
            src = Path(tmp) / "x.db"
            dst = Path(tmp) / "x.plain.db"
            src.write_bytes(_encrypt_page1(body))
            decrypt_database(src, dst, TEST_KEY.hex())
            out = dst.read_bytes()
            self.assertEqual(len(out), PAGE_SIZE)
            self.assertEqual(out[:PAGE_SIZE - RESERVE_SIZE], SQLITE_MAGIC + body)
            self.assertEqual(out[PAGE_SIZE - RESERVE_SIZE:], b"\x00" * RESERVE_SIZE)

    def test_wrong_key_raises_unavailable(self):
        body = b"C" * (PAGE_SIZE - RESERVE_SIZE - wcdb.SALT_SIZE)
        with tempfile.TemporaryDirectory(prefix="pyw-wcdb-") as tmp:
            src = Path(tmp) / "x.db"
            dst = Path(tmp) / "x.plain.db"
            src.write_bytes(_encrypt_page1(body))
            with self.assertRaises(WcdbUnavailable):
                decrypt_database(src, dst, "01" * 32)


def _contact_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, "
                    "remark TEXT, nick_name TEXT, local_type INTEGER)")
        con.execute("CREATE TABLE chatroom_member (room_id INTEGER, member_id INTEGER)")
        con.executemany(
            "INSERT INTO contact VALUES (?,?,?,?,?)",
            [(10, "g1@chatroom", "鲜果备注", "鲜果群", 1),
             (11, "wxid_a", "张姐", "xiaozhang", 1),
             (12, "g2@chatroom", "", "社区团购", 1),
             (13, "wxid_b", "", "李女士", 3)],
        )
        con.executemany("INSERT INTO chatroom_member VALUES (?,?)",
                        [(10, 11), (10, 13), (12, 11)])
        con.commit()
    finally:
        con.close()


def _session_db(path: Path) -> None:
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE SessionTable (username TEXT, type INTEGER, "
                    "last_timestamp TEXT, summary TEXT, sort_timestamp INTEGER)")
        con.execute("INSERT INTO SessionTable VALUES (?,?,?,?,?)",
                    ("g1@chatroom", 0, "1791900000", "苹果两箱", 99))
        con.execute("INSERT INTO SessionTable VALUES (?,?,?,?,?)",
                    ("wxid_a", 0, "1791800000", "好的", 98))
        con.commit()
    finally:
        con.close()


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pyw-wcdb-")
        self.contact = Path(self.tmp.name) / "contact.db"
        self.session = Path(self.tmp.name) / "session.db"
        _contact_db(self.contact)
        _session_db(self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def test_display_name_remark_first(self):
        row = {"remark": "张姐", "nick_name": "xiaozhang"}
        self.assertEqual(display_name(row), "张姐")
        self.assertEqual(display_name({"remark": "", "nick_name": "李女士"}), "李女士")
        self.assertEqual(display_name({"remark": "", "nick_name": ""}), "")

    def test_sessions_have_names_counts_and_order(self):
        got = read_sessions(self.contact, self.session)
        self.assertEqual([g.username for g in got], ["g1@chatroom", "wxid_a"])
        self.assertEqual(got[0].display, "鲜果备注")
        self.assertEqual(got[0].member_count, 2)
        self.assertEqual(got[0].last_timestamp, 1791900000)
        self.assertEqual(got[0].summary, "苹果两箱")
        self.assertEqual(got[1].kind, "friend")

    def test_all_groups(self):
        got = read_all_groups(self.contact)
        by_name = {g.username: g for g in got}
        self.assertEqual(set(by_name), {"g1@chatroom", "g2@chatroom"})
        self.assertEqual(by_name["g1@chatroom"].display, "鲜果备注")
        self.assertEqual(by_name["g1@chatroom"].member_count, 2)
        self.assertEqual(by_name["g2@chatroom"].member_count, 1)

    def test_all_friends_only_local_type_1(self):
        got = read_all_friends(self.contact)
        self.assertEqual([f.username for f in got], ["wxid_a"])


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pyw-wcdb-")
        self.root = Path(self.tmp.name)
        (self.root / "contact").mkdir()
        (self.root / "session").mkdir()
        _contact_db(self.root / "contact" / "contact.db")
        _session_db(self.root / "session" / "session.db")
        # 明文库冒充"活库"：prepare 会拷贝它们，但解密需要密文——
        # 这里只测缺钥匙/缺文件路径；解密成功路径由 WcdbUnavailable 语义覆盖。
        self.keys = self.root / "wcdb_keys.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_keys_file_is_unavailable(self):
        reader = WcdbReader(self.root, self.root / "nope.json")
        self.assertFalse(reader.available)
        self.assertIn("密钥", reader.error)

    def test_missing_key_entry_lists_nothing(self):
        self.keys.write_text(json.dumps({"other/other.db": {"enc_key": "00" * 32}}),
                             encoding="utf-8")
        reader = WcdbReader(self.root, self.keys)
        self.assertFalse(reader.available)

    def test_bad_key_is_unavailable_not_crash(self):
        self.keys.write_text(json.dumps({
            "contact/contact.db": {"enc_key": "01" * 32},
            "session/session.db": {"enc_key": "01" * 32}}), encoding="utf-8")
        reader = WcdbReader(self.root, self.keys)
        with self.assertRaises(WcdbUnavailable):
            reader.list_conversations("group")

    def test_cleanup_removes_decrypted(self):
        reader = WcdbReader(self.root, self.keys)
        junk = reader.workdir / "x.db"
        junk.parent.mkdir(parents=True, exist_ok=True)
        junk.write_bytes(b"secret")
        reader.cleanup()
        self.assertFalse(junk.exists())

    def test_unkeyed_shard_lists_for_reextract(self):
        """磁盘上有、钥匙里没有的分片：unkeyed 报出来，prepare 报重提。

        回归真机 case：message_1.db 建出来了但钥匙是旧的，
        之前静默跳过导致导入群 0 条。
        """
        (self.root / "message").mkdir(exist_ok=True)
        (self.root / "message" / "message_0.db").write_bytes(b"0" * 100)
        (self.root / "message" / "message_1.db").write_bytes(b"1" * 100)
        self.keys.write_text(json.dumps({
            "contact/contact.db": {"enc_key": "00" * 32},
            "session/session.db": {"enc_key": "00" * 32},
            "message/message_0.db": {"enc_key": "00" * 32}}), encoding="utf-8")
        reader = WcdbReader(self.root, self.keys)
        self.assertEqual(reader.unkeyed_databases(), ["message/message_1.db"])
        # 全部分片都列出来（含没钥匙的），prepare 到缺钥匙处明说重提。
        self.assertEqual(reader.message_shards(),
                         ["message/message_0.db", "message/message_1.db"])
        import shutil as _shutil

        with unittest.mock.patch("pyweixin.wcdb.decrypt_database",
                                 side_effect=lambda s, d, k: _shutil.copyfile(s, d)):
            with self.assertRaises(WcdbUnavailable) as ctx:
                reader.prepare(["message/message_1.db"])
        self.assertIn("重新提取", str(ctx.exception))


class SourcePriorityTests(unittest.TestCase):
    def test_wcdb_first(self):
        from pyweixin.wcdb import WcdbConversation

        rows = [WcdbConversation(username="g@chatroom", display="真群",
                                 kind="group", member_count=128)]
        source = RealConversationSource(
            ui_lock=None, wcdb_fn=lambda kind: rows)
        got = source.list_conversations(ConversationKind.GROUP)
        self.assertEqual([(c.name, c.member_count, c.wxid) for c in got],
                         [("真群", 128, "g@chatroom")])

    def test_wcdb_failure_falls_back(self):
        def boom(kind):
            raise WcdbUnavailable("没钥匙")

        source = RealConversationSource(ui_lock=None, wcdb_fn=boom)
        with unittest.mock.patch("pyweixin.localdb.list_group_conversations",
                                 return_value=[("内存群", "m@chatroom")]):
            got = source.list_conversations(ConversationKind.GROUP)
        self.assertEqual([(c.name, c.wxid) for c in got], [("内存群", "m@chatroom")])


def _encrypt_page(body: bytes, pgno: int, salt: bytes, iv: bytes) -> bytes:
    """任意页加密（WAL 帧页用）：AES(体) + IV + HMAC(体+iv+页号)。"""
    from Crypto.Cipher import AES

    assert len(body) == PAGE_SIZE - RESERVE_SIZE
    ct = AES.new(TEST_KEY, AES.MODE_CBC, iv).encrypt(body)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", TEST_KEY, mac_salt, 2, dklen=32)
    hm = hmac_mod.new(mac_key, ct + iv, hashlib.sha512)
    hm.update(struct.pack("<i", pgno))
    return ct + iv + hm.digest()


def _wal_bytes(frames: list[tuple[int, bytes]]) -> bytes:
    """合成 WAL：32B 头 + 每帧 24B 头 + 4096B 页。commit 全置 1（已提交）。"""
    out = struct.pack(">IIIIIIII", 0x377F0682, 3007000, PAGE_SIZE, 0, 1, 2, 0, 0)
    for pgno, page in frames:
        out += struct.pack(">IIIIII", pgno, 1, 1, 2, 0, 0) + page
    return out


class WalTests(unittest.TestCase):
    def test_uncommitted_tail_is_ignored(self):
        """没提交的尾巴不要：最后一帧 commit=0 时只合之前已提交的。"""
        salt = b"S" * 16
        iv = b"I" * 16
        committed = _encrypt_page(b"C" * (PAGE_SIZE - RESERVE_SIZE), 2, salt, iv)
        dangling = _encrypt_page(b"D" * (PAGE_SIZE - RESERVE_SIZE), 3, salt, iv)
        raw = struct.pack(">IIIIIIII", 0x377F0682, 3007000, PAGE_SIZE, 0, 1, 2, 0, 0)
        raw += struct.pack(">IIIIII", 2, 1, 1, 2, 0, 0) + committed
        raw += struct.pack(">IIIIII", 3, 0, 1, 2, 0, 0) + dangling
        with tempfile.TemporaryDirectory(prefix="pyw-wal-") as tmp:
            wal = Path(tmp) / "x.db-wal"
            wal.write_bytes(raw)
            size, frames = wcdb._read_wal_frames(wal)
            self.assertEqual(size, PAGE_SIZE)
            self.assertEqual([pgno for pgno, _ in frames], [2])

    def test_apply_merges_verified_frames(self):
        """WAL 合入：解密后的库文件里对应页被替换，坏帧跳过。"""
        salt = b"S" * 16
        iv = b"I" * 16
        body2 = b"P" * (PAGE_SIZE - RESERVE_SIZE)
        frame2 = _encrypt_page(body2, 2, salt, iv)
        bad = bytearray(_encrypt_page(b"Q" * (PAGE_SIZE - RESERVE_SIZE), 5, salt, iv))
        bad[100] ^= 0xFF  # 弄坏一字节，验签必须拦住
        with tempfile.TemporaryDirectory(prefix="pyw-wal-") as tmp:
            db = Path(tmp) / "x.db"
            db.write_bytes(b"\x00" * PAGE_SIZE * 6)
            wal = Path(tmp) / "x.db-wal"
            wal.write_bytes(_wal_bytes([(2, bytes(frame2)), (5, bytes(bad))]))
            merged = wcdb.apply_wal(db, wal, TEST_KEY, salt)
            self.assertEqual(merged, 1)
            self.assertEqual(db.read_bytes()[PAGE_SIZE:2 * PAGE_SIZE], body2 + b"\x00" * 80)
            self.assertEqual(db.read_bytes()[4 * PAGE_SIZE:5 * PAGE_SIZE], b"\x00" * PAGE_SIZE)


def _message_db(path: Path, username: str) -> None:
    import hashlib as _hashlib

    table = "Msg_" + _hashlib.md5(username.encode()).hexdigest()
    con = sqlite3.connect(str(path))
    try:
        con.execute("CREATE TABLE %s (local_id INTEGER, server_id INTEGER, "
                    "local_type INTEGER, sort_seq INTEGER, create_time INTEGER, "
                    "message_content TEXT)" % table)
        con.execute("INSERT INTO %s VALUES (?,?,?,?,?,?)" % table,
                    (1, 777, 1, 1000, 1791900000, "wxid_a:\n苹果两箱"))
        con.execute("INSERT INTO %s VALUES (?,?,?,?,?,?)" % table,
                    (2, 0, 10000, 1001, 1791900060,
                     "<sysmsg>wxid_a 加入了群聊</sysmsg>"))
        con.execute("INSERT INTO %s VALUES (?,?,?,?,?,?)" % table,
                    (3, 778, 3, 1002, 1791900120, "聊天图片"))
        con.commit()
    finally:
        con.close()


class ReadMessagesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pyw-wcdb-msg-")
        self.contact = Path(self.tmp.name) / "contact.db"
        self.message = Path(self.tmp.name) / "message.db"
        con = sqlite3.connect(str(self.contact))
        try:
            con.execute("CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, "
                        "remark TEXT, nick_name TEXT, local_type INTEGER)")
            con.execute("INSERT INTO contact VALUES (11,'wxid_a','张姐','xiaozhang',1)")
            con.commit()
        finally:
            con.close()
        _message_db(self.message, "g@chatroom")

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_sender_content_time(self):
        from pyweixin.wcdb import read_messages

        got = read_messages(self.message, self.contact, "g@chatroom", 10)
        self.assertEqual(len(got), 3)
        text, system, image = got
        self.assertEqual((text.sender_username, text.sender_display,
                          text.content, text.kind, text.timestamp),
                         ("wxid_a", "张姐", "苹果两箱", "文本", 1791900000))
        self.assertTrue(text.source_id.startswith("wcdb-777"))
        self.assertEqual(system.kind, "系统通知")
        self.assertIn("加入了群聊", system.content)
        self.assertEqual((image.kind, image.content), ("图片", ""))

    def test_missing_table_is_empty_not_error(self):
        from pyweixin.wcdb import message_table_exists, read_messages

        self.assertEqual(read_messages(self.message, self.contact, "nobody", 10), [])
        self.assertFalse(message_table_exists(self.message, "nobody"))
        self.assertTrue(message_table_exists(self.message, "g@chatroom"))

    def test_split_sender(self):
        from pyweixin.wcdb import _split_sender

        self.assertEqual(_split_sender("wxid_a:\n你好"), ("wxid_a", "你好"))
        self.assertEqual(_split_sender("没前缀"), ("", "没前缀"))
        # 正文里的冒号换行不是发送人（发送人不能有空格）。
        self.assertEqual(_split_sender("他说 :\n明天见"), ("", "他说 :\n明天见"))


class ZstdMessagesTests(unittest.TestCase):
    """长文本存成 ZSTD 整帧：不解连发送人前缀都看不到。

    回归真机 case：群里读出 2 条，第 5 步内容全空 —— local_type=1 的
    正文是 28 b5 2f fd 开头的二进制，旧代码 decode 失败直接丢了整条。
    """

    def test_frame_is_detected_and_decoded(self):
        import zstandard

        from pyweixin.wcdb import _maybe_decompress

        frame = zstandard.ZstdCompressor().compress("wxid_a:\n压缩长文本".encode())
        self.assertTrue(frame.startswith(b"\x28\xb5\x2f\xfd"))
        self.assertEqual(_maybe_decompress(frame), "wxid_a:\n压缩长文本".encode())
        # 明文和短串原样过。
        self.assertEqual(_maybe_decompress("wxid_a:\n明文"), "wxid_a:\n明文")
        self.assertEqual(_maybe_decompress(b"ab"), b"ab")
        self.assertEqual(_maybe_decompress(""), "")

    def test_compressed_text_maps_sender_and_content(self):
        import hashlib as _hashlib

        import zstandard

        from pyweixin.wcdb import read_messages

        tmp = tempfile.TemporaryDirectory(prefix="pyw-zstd-")
        try:
            contact = Path(tmp.name) / "contact.db"
            message = Path(tmp.name) / "message.db"
            con = sqlite3.connect(str(contact))
            try:
                con.execute("CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, "
                            "remark TEXT, nick_name TEXT, local_type INTEGER)")
                con.execute("INSERT INTO contact VALUES (11,'wxid_a','张姐','xiaozhang',1)")
                con.commit()
            finally:
                con.close()
            table = "Msg_" + _hashlib.md5("g@chatroom".encode()).hexdigest()
            frame = zstandard.ZstdCompressor().compress(
                "wxid_a:\n压缩长文本".encode())
            mcon = sqlite3.connect(str(message))
            try:
                mcon.execute("CREATE TABLE %s (local_id INTEGER, server_id INTEGER, "
                             "local_type INTEGER, sort_seq INTEGER, create_time INTEGER, "
                             "message_content BLOB)" % table)
                mcon.execute("INSERT INTO %s VALUES (?,?,?,?,?,?)" % table,
                             (1, 999, 1, 1000, 1791900000, frame))
                mcon.commit()
            finally:
                mcon.close()
            got = read_messages([message], contact, "g@chatroom", 10)
            self.assertEqual(len(got), 1)
            self.assertEqual((got[0].sender_username, got[0].sender_display,
                              got[0].content, got[0].kind),
                             ("wxid_a", "张姐", "压缩长文本", "文本"))
        finally:
            tmp.cleanup()


class HistoryWcdbRoutingTests(unittest.TestCase):
    """HistoryReader 走解密库：自动升级、失败回落、读完清场。"""

    class _FakeReader:
        available = True

        def __init__(self, contact, message):
            self._contact = contact
            self._message = message
            self.cleaned = 0

        def message_shards(self):
            return ["message/message_0.db"]

        def prepare(self, extra=()):
            return {"contact/contact.db": self._contact,
                    "message/message_0.db": self._message}

        def cleanup(self, ready=None):
            self.cleaned += 1

    def _history(self, fake):
        from pyweixin.history_read import HistoryReader

        return HistoryReader(lambda **kw: [], sender_source="members",
                             wcdb_reader_fn=lambda: fake)

    def _dbs(self):
        tmp = tempfile.TemporaryDirectory(prefix="pyw-hrw-")
        contact = Path(tmp.name) / "contact.db"
        message = Path(tmp.name) / "message.db"
        con = sqlite3.connect(str(contact))
        try:
            con.execute("CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, "
                        "remark TEXT, nick_name TEXT, local_type INTEGER)")
            con.execute("INSERT INTO contact VALUES (11,'wxid_a','张姐','xiaozhang',1)")
            con.commit()
        finally:
            con.close()
        _message_db(message, "g@chatroom")
        return tmp, contact, message

    def test_members_auto_upgrades_to_wcdb(self):
        from pyweixin.conversations import Conversation, ConversationKind
        from pyweixin.history_read import HistoryRange, ReadMode

        tmp, contact, message = self._dbs()
        try:
            fake = HistoryWcdbRoutingTests._FakeReader(contact, message)
            history = self._history(fake)
            conv = Conversation(name="群", kind=ConversationKind.GROUP, wxid="g@chatroom")
            result = history.read([conv], HistoryRange.window("last_day"),
                                  merchant_id="m", max_messages=10,
                                  mode=ReadMode.RECENT, sender_source="members")
            self.assertEqual(result.total_messages, 3)  # 文本 + 系统通知 + 图片占位
            self.assertEqual(fake.cleaned, 1)  # 读完清场
        finally:
            tmp.cleanup()

    def test_no_reader_stays_on_members(self):
        from pyweixin.conversations import Conversation, ConversationKind
        from pyweixin.history_read import HistoryReader, HistoryRange, ReadMode

        seen = {}

        def dump(**kwargs):
            seen["called"] = True
            return [{"sender": "张姐", "content": "苹果", "kind": "text",
                     "sent_at": "2026-10-13 10:00", "source_id": "s1"}]

        history = HistoryReader(dump, sender_source="members", wcdb_reader_fn=lambda: None)
        conv = Conversation(name="群", kind=ConversationKind.GROUP, wxid="g@chatroom")
        history.read([conv], HistoryRange.window("last_day"), merchant_id="m",
                     max_messages=10, mode=ReadMode.RECENT, sender_source="members")
        self.assertTrue(seen.get("called"))

class EnsureKeysEncodingTests(unittest.TestCase):
    """提取密钥的子进程必须强制 UTF-8：这台机器默认编码是 cp1252，
    子进程一句中文 print 就崩（之前"提取没成功"报的就是 charmap 错）。

    锁两点：env 里有 PYTHONUTF8=1；输出按字节拿、按 UTF-8 解，
    不经过 text=True 的 locale 解码。
    """

    def _app(self, tmp: str):
        from pyweixin.application import MerchantApplication
        from pyweixin.conversations import SimulatedConversations
        from pyweixin.history_read import SimulatedHistory
        from pyweixin.orders import OrderStore
        from pyweixin.wechat_state import SimulatedWeChat, WeChatState

        store = OrderStore(str(Path(tmp) / "orders.db"))
        merchant = store.create_merchant("t")
        app = MerchantApplication(
            data_dir=Path(tmp), orders=store, tenant=merchant["merchant_id"],
            probe=SimulatedWeChat([WeChatState.LOGGED_IN]),
            conversation_source=SimulatedConversations([]),
            history_reader_fn=SimulatedHistory(default_count=0),
            sender=lambda t, x: None)
        return app, store

    def test_child_forced_utf8_and_bytes_decoded(self):
        import subprocess

        captured: dict = {}
        tmp = tempfile.TemporaryDirectory(prefix="pyw-ensure-t-")
        try:
            app, store = self._app(tmp.name)
            try:
                out = "结果: 1/1 salts 找到密钥\n".encode("utf-8")

                def fake_run(*args, **kwargs):
                    captured.update(kwargs)
                    return subprocess.CompletedProcess(
                        args=args[0] if args else (), returncode=0,
                        stdout=out, stderr=b"")

                with unittest.mock.patch.object(
                        app, "_wcdb_db_storage",
                        return_value=Path(tmp.name)), \
                    unittest.mock.patch("subprocess.run", side_effect=fake_run), \
                    unittest.mock.patch.object(
                        app, "wcdb_status", return_value=(True, "ok")):
                    ok, _message = app.ensure_wcdb_keys()
                self.assertTrue(ok)
                env = captured.get("env") or {}
                self.assertEqual(env.get("PYTHONUTF8"), "1")
                self.assertNotIn("text", captured)  # 不走 locale 解码
            finally:
                app.close()
                store.close()
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass  # Windows 文件锁（后台线程刚放手），下次系统清


class RefreshAutoEnsureTests(unittest.TestCase):
    """点"读取"没钥匙就自动提一次，提完直接读；每个进程只试一次。"""

    def _app(self, tmp: str):
        from pyweixin.application import MerchantApplication
        from pyweixin.conversations import RealConversationSource
        from pyweixin.history_read import SimulatedHistory
        from pyweixin.orders import OrderStore
        from pyweixin.wechat_state import SimulatedWeChat, WeChatState

        store = OrderStore(str(Path(tmp) / "orders.db"))
        merchant = store.create_merchant("t")
        # 真实来源但不碰真微信：列表调用被 mock 掉，只测自动提钥匙的门禁。
        source = RealConversationSource(ui_lock=None)
        source.list_conversations = lambda kind="": ()
        app = MerchantApplication(
            data_dir=Path(tmp), orders=store, tenant=merchant["merchant_id"],
            probe=SimulatedWeChat([WeChatState.LOGGED_IN]),
            conversation_source=source,
            history_reader_fn=SimulatedHistory(default_count=0),
            sender=lambda t, x: None)
        return app, store

    def _run(self, app, status, ensure):
        with unittest.mock.patch.object(app, "wcdb_status", return_value=status), \
            unittest.mock.patch.object(app, "ensure_wcdb_keys",
                                       side_effect=ensure) as mocked, \
            unittest.mock.patch.object(app.wizard, "refresh_conversations",
                                       return_value=[]), \
            unittest.mock.patch.object(app, "_wcdb_db_storage",
                                       return_value=None):
            app.refresh_conversations()
            return mocked

    def test_missing_keys_auto_ensures_once(self):
        tmp = tempfile.TemporaryDirectory(prefix="pyw-auto-")
        try:
            app, store = self._app(tmp.name)
            try:
                calls: list = []
                mocked = self._run(app, (False, "没钥匙"),
                                   lambda: calls.append(1) or (True, "ok"))
                self.assertEqual(mocked.call_count, 1)
                # 第二次点只读列表，不再扫内存。
                mocked2 = self._run(app, (True, "ok"), lambda: (True, "ok"))
                self.assertEqual(mocked2.call_count, 0)
            finally:
                app.close()
                store.close()
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass

    def test_ready_keys_never_ensures(self):
        tmp = tempfile.TemporaryDirectory(prefix="pyw-auto-")
        try:
            app, store = self._app(tmp.name)
            try:
                mocked = self._run(app, (True, "ok"),
                                   lambda: (_ for _ in ()).throw(
                                       AssertionError("有钥匙不该提")))
                self.assertEqual(mocked.call_count, 0)
            finally:
                app.close()
                store.close()
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass

    def test_simulated_source_never_touches_real_wechat(self):
        """模拟来源不碰真微信：自动提钥匙只对真实来源生效。

        回归：之前没加这个门禁时，模拟测试在本机（微信登录中）会被
        真实提钥匙劫持，读出 0 条。
        """
        from pyweixin.application import MerchantApplication
        from pyweixin.conversations import SimulatedConversations
        from pyweixin.history_read import SimulatedHistory
        from pyweixin.orders import OrderStore
        from pyweixin.wechat_state import SimulatedWeChat, WeChatState

        tmp = tempfile.TemporaryDirectory(prefix="pyw-auto-")
        try:
            store = OrderStore(str(Path(tmp.name) / "orders.db"))
            merchant = store.create_merchant("t")
            app = MerchantApplication(
                data_dir=Path(tmp.name), orders=store,
                tenant=merchant["merchant_id"],
                probe=SimulatedWeChat([WeChatState.LOGGED_IN]),
                conversation_source=SimulatedConversations([]),
                history_reader_fn=SimulatedHistory(default_count=0),
                sender=lambda t, x: None)
            try:
                with unittest.mock.patch.object(
                        app, "ensure_wcdb_keys",
                        side_effect=AssertionError("模拟来源不该提钥匙")), \
                    unittest.mock.patch.object(
                        app.wizard, "refresh_conversations", return_value=[]):
                    app.refresh_conversations()
                self.assertIsNone(app.history._wcdb_reader_fn)
            finally:
                app.close()
                store.close()
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass

    def test_unkeyed_shard_triggers_reensure(self):
        """微信新建了没钥匙的库：超过节流时间后点读取会自动补提。

        回归真机 case：手机导入建了 message_1.db，钥匙还是旧的，
        导入群一直 0 条。
        """
        import time as _time

        tmp = tempfile.TemporaryDirectory(prefix="pyw-auto-")
        try:
            app, store = self._app(tmp.name)
            try:
                app._wcdb_ensure_attempted = True
                app._wcdb_ensure_at = _time.time() - 400
                calls: list = []
                with unittest.mock.patch.object(
                        app, "wcdb_status", return_value=(True, "ok")), \
                    unittest.mock.patch.object(
                        app, "_wcdb_db_storage",
                        return_value=Path(tmp.name)), \
                    unittest.mock.patch.object(
                        app, "ensure_wcdb_keys",
                        side_effect=lambda: calls.append(1) or (True, "ok")), \
                    unittest.mock.patch.object(
                        app.wizard, "refresh_conversations", return_value=[]):
                    # 磁盘上有 message_1.db 但钥匙里没有 → 触发补提。
                    (Path(tmp.name) / "message").mkdir(exist_ok=True)
                    (Path(tmp.name) / "message" / "message_1.db").write_bytes(b"x")
                    app.refresh_conversations()
                self.assertEqual(len(calls), 1)
            finally:
                app.close()
                store.close()
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass

    def test_read_history_auto_ensures_in_background(self):
        """点读取也在后台线程里补钥匙：用户不用回第 2 步。"""
        import time as _time

        tmp = tempfile.TemporaryDirectory(prefix="pyw-auto-")
        try:
            app, store = self._app(tmp.name)
            try:
                calls: list = []
                done = _time.time() + 10
                with unittest.mock.patch.object(
                        app, "_auto_ensure_wcdb_keys",
                        side_effect=lambda: calls.append(1) or True), \
                    unittest.mock.patch.object(
                        app.wizard, "read_history") as read_mock, \
                    unittest.mock.patch.object(app, "refresh_home"):
                    app.read_history(mode="update", sender_source="wcdb")
                    while not calls and _time.time() < done:
                        _time.sleep(0.05)
                self.assertEqual(len(calls), 1)
                self.assertTrue(read_mock.called)
            finally:
                app.close()
                store.close()
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass


class MultiShardTests(unittest.TestCase):
    """手机导入建新分片：全分片合并、去重、按时间取最新。"""

    def _shard(self, path, username, rows):
        import hashlib as _hashlib

        table = "Msg_" + _hashlib.md5(username.encode()).hexdigest()
        con = sqlite3.connect(str(path))
        try:
            con.execute("CREATE TABLE %s (local_id INTEGER, server_id INTEGER, "
                        "local_type INTEGER, sort_seq INTEGER, create_time INTEGER, "
                        "message_content TEXT)" % table)
            for row in rows:
                con.execute("INSERT INTO %s VALUES (?,?,?,?,?,?)" % table, row)
            con.commit()
        finally:
            con.close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pyw-shard-")
        self.contact = Path(self.tmp.name) / "contact.db"
        con = sqlite3.connect(str(self.contact))
        try:
            con.execute("CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, "
                        "remark TEXT, nick_name TEXT, local_type INTEGER)")
            con.execute("INSERT INTO contact VALUES (11,'wxid_a','张姐','xiaozhang',1)")
            con.commit()
        finally:
            con.close()
        self.m0 = Path(self.tmp.name) / "m0.db"
        self.m1 = Path(self.tmp.name) / "m1.db"
        # 旧分片 2 条，新分片 1 条新的 + 1 条重复（同 server_id）。
        self._shard(self.m0, "g@chatroom", [
            (1, 101, 1, 1000, 1791900000, "wxid_a:\n第一条"),
            (2, 102, 1, 1001, 1791900060, "wxid_a:\n第二条"),
        ])
        self._shard(self.m1, "g@chatroom", [
            (1, 102, 1, 1001, 1791900060, "wxid_a:\n第二条"),
            (2, 103, 1, 2000, 1791999999, "wxid_a:\n第三条"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_merge_dedupes_and_orders(self):
        from pyweixin.wcdb import read_messages

        got = read_messages([self.m0, self.m1], self.contact, "g@chatroom", 10)
        self.assertEqual([m.content for m in got], ["第一条", "第二条", "第三条"])
        self.assertEqual([m.sender_display for m in got], ["张姐"] * 3)

    def test_limit_takes_newest(self):
        from pyweixin.wcdb import read_messages

        got = read_messages([self.m0, self.m1], self.contact, "g@chatroom", 2)
        self.assertEqual([m.content for m in got], ["第二条", "第三条"])

    def test_message_shards_lists_keyed_shards(self):
        from pyweixin.wcdb import WcdbReader

        root = Path(self.tmp.name)
        (root / "message").mkdir(exist_ok=True)
        for name in ("message_0.db", "message_1.db", "message_9.db", "notes.txt",
                     "message_x.db"):
            (root / "message" / name).write_bytes(b"x")
        keys = root / "keys.json"
        keys.write_text(json.dumps({
            "message/message_0.db": {"enc_key": "00" * 32},
            "message/message_1.db": {"enc_key": "00" * 32}}), encoding="utf-8")
        reader = WcdbReader(root, keys)
        # 没钥匙的 message_9 也列出来：prepare 到它会明说重提，
        # 不在这里静默跳过（跳过就是导入群 0 条）。
        self.assertEqual(reader.message_shards(),
                         ["message/message_0.db", "message/message_1.db",
                          "message/message_9.db"])


class WcdbReimportUpgradeTests(unittest.TestCase):
    """wcdb 重读回填空壳行：旧代码解不出压缩正文入库的空 content，
    新代码读出来后按稳定 ID 合到同一条上，只填空不覆盖。

    回归真机 case：群读出 2 条但第 5 步全空 —— 库里是旧空行，
    重读因去重直接跳过。
    """

    def _store(self):
        from pyweixin.orders import OrderStore

        store = OrderStore(":memory:")
        tenant = store.create_merchant("测试店")["merchant_id"]
        return store, tenant

    def _import(self, store, tenant, sender, content):
        return store.add_import(
            tenant, "水果群",
            [{"sender": sender, "content": content, "kind": "text",
              "sent_at": "2026-10-13 10:00", "source_id": "wcdb-999",
              "stable_source_id": True}],
            "test-src")

    def test_empty_shell_is_filled_on_reread(self):
        store, tenant = self._store()
        try:
            # 第一遍：旧代码解不出，空正文 + 群名发送人入库。
            self._import(store, tenant, "水果群", "")
            # 第二遍：新代码解出来了，同 ID 合并，只填空不另起行。
            result = self._import(store, tenant, "张姐", "苹果两箱")
            self.assertEqual(result["inserted"], 0)
            rows = store.db.execute(
                "SELECT COUNT(*) n FROM messages WHERE merchant_id=?",
                (tenant,)).fetchone()
            self.assertEqual(rows["n"], 1)
            row = store.db.execute(
                "SELECT sender, content FROM messages WHERE merchant_id=?",
                (tenant,)).fetchone()
            self.assertEqual((row["sender"], row["content"]), ("张姐", "苹果两箱"))
        finally:
            store.close()

    def test_real_content_is_never_overwritten(self):
        store, tenant = self._store()
        try:
            self._import(store, tenant, "张姐", "苹果两箱")
            self._import(store, tenant, "张姐", "橘子三箱")
            row = store.db.execute(
                "SELECT content FROM messages WHERE merchant_id=?",
                (tenant,)).fetchone()
            self.assertEqual(row["content"], "苹果两箱")
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
