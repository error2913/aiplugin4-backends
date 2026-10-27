"""store v4 单测：schema v3→v4 迁移、w/h 写入与回填、变体缓存容量/清理/级联。

运行：
  python -m unittest discover -s backends/image-host/tests -p "test_store_v4.py" -v
"""

import hashlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from PIL import Image  # noqa: E402

from store import (DDL_V1, DDL_V2_REST, DDL_V3_REST,  # noqa: E402
                   SCHEMA_VERSION, VARIANT_PER_BLOB_MAX, Store)

HEX64_A = "a" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def png_bytes(w: int, h: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (w, h), (10, 20, 30)).save(buffer, "PNG")
    return buffer.getvalue()


def make_v3_db(root: str) -> str:
    """造一个 schema v3 的旧库（含一条 blob/ref），供迁移测试。"""
    db = os.path.join(root, "image.db")
    con = sqlite3.connect(db)
    con.executescript(DDL_V1)
    con.execute("ALTER TABLE blob ADD COLUMN deleted_at INTEGER")
    con.executescript(DDL_V2_REST)
    cols = {row[1] for row in con.execute("PRAGMA table_info(ref)")}
    for col in ("tool", "server", "via"):
        if col not in cols:
            con.execute(f"ALTER TABLE ref ADD COLUMN {col} TEXT")
    con.executescript(DDL_V3_REST)
    con.execute(
        "INSERT INTO blob(id,format,mime,size,thumb_size,created_at,last_access_at,"
        "name,origin,orphan_at,thumb_path,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (HEX64_A, "png", "image/png", 123, 0, 1000, 1000, "旧图", "plugin",
         None, None, None))
    con.execute(
        "INSERT INTO ref(id,ref,platform,group_id,user_id,message_id,session_id,at,"
        "raw_url,tool,server,via) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (HEX64_A, "old-ref", "QQ", "111", "222", "m1", "s1", 1000,
         "http://x/y.png", None, None, None))
    con.execute("PRAGMA user_version=3")
    con.commit()
    con.close()
    return db


class StoreV4Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ih-store-v4-")
        self.store = Store(self.root, cap_bytes=64 * 1024 * 1024, thumb_max=512,
                           trash_days=7, change_keep_days=30)
        self.store.init()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, data: bytes, ref: str, origin: str = "plugin", w=None, h=None,
            fmt="png", mime="image/png"):
        bid = sha(data)
        tmp = None
        if self.store.lookup(bid) is None:
            tmp = os.path.join(self.store.tmp, bid + ".part")
            with open(tmp, "wb") as fh:
                fh.write(data)
        return self.store.put(tmp, bid, len(data), fmt, mime, ref, {},
                              origin, w=w, h=h)[0]

    def put_image(self, w=3000, h=1000, ref="ref-1", origin="plugin", **kwargs):
        data = png_bytes(w, h)
        bid = self.put(data, ref, origin, w=w, h=h, **kwargs)
        return bid, data


class TestSchemaV4(StoreV4Base):
    def test_schema_version_and_columns(self):
        con = self.store._conn()
        self.assertEqual(SCHEMA_VERSION, 4)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 4)
        blob_cols = {row[1] for row in con.execute("PRAGMA table_info(blob)")}
        self.assertTrue({"w", "h"}.issubset(blob_cols))
        variant_cols = {row[1] for row in con.execute("PRAGMA table_info(variant)")}
        self.assertEqual(variant_cols, {"blob_id", "max_side", "fmt", "mime",
                                        "w", "h", "size", "path", "created_at"})

    def test_migrate_from_v3_keeps_rows_and_adds_columns(self):
        root = tempfile.mkdtemp(prefix="ih-store-v3-")
        self.addCleanup(shutil.rmtree, root, True)
        make_v3_db(root)
        store = Store(root, cap_bytes=1024 * 1024)
        store.init()
        con = store._conn()
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 4)
        blob_cols = {row[1] for row in con.execute("PRAGMA table_info(blob)")}
        self.assertTrue({"w", "h"}.issubset(blob_cols))
        self.assertTrue(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='variant'"
        ).fetchone())
        row = store.lookup(HEX64_A)
        self.assertIsNotNone(row)
        self.assertIsNone(row["w"])
        self.assertIsNone(row["h"])
        self.assertEqual(con.execute(
            "SELECT ref FROM ref WHERE id=?", (HEX64_A,)).fetchone()["ref"],
            "old-ref")


class TestSize(StoreV4Base):
    def test_put_stores_size(self):
        bid, data = self.put_image(3000, 1000)
        row = self.store.lookup(bid)
        self.assertEqual((row["w"], row["h"]), (3000, 1000))

    def test_dedup_backfills_missing_size(self):
        data = png_bytes(640, 480)
        bid1 = self.put(data, "r1")                    # 模拟旧行：没有尺寸
        self.assertIsNone(self.store.lookup(bid1)["w"])
        bid2 = self.put(data, "r2", w=640, h=480)      # 去重命中，回填
        self.assertEqual(bid1, bid2)
        self.assertEqual((self.store.lookup(bid1)["w"],
                          self.store.lookup(bid1)["h"]), (640, 480))

    def test_set_size_only_fills_null(self):
        bid, _ = self.put_image(100, 50)
        self.store.set_size(bid, 999, 888)
        self.assertEqual((self.store.lookup(bid)["w"],
                          self.store.lookup(bid)["h"]), (100, 50))
        # 清空后再回填
        with self.store._lock:
            self.store._conn().execute("UPDATE blob SET w=NULL,h=NULL WHERE id=?", (bid,))
        self.store._cache_drop(bid)
        self.store.set_size(bid, 999, 888)
        self.assertEqual((self.store.lookup(bid)["w"],
                          self.store.lookup(bid)["h"]), (999, 888))


class TestVariantCache(StoreV4Base):
    def test_save_and_accounting(self):
        bid, data = self.put_image(3000, 1000, origin="manual")
        payload = b"variant-jpeg-bytes"
        row = self.store.save_variant(bid, 2048, "jpeg", "image/jpeg",
                                      2048, 683, payload)
        self.assertIsNotNone(row)
        self.assertTrue(os.path.exists(row["path"]))
        self.assertTrue(row["path"].endswith(".m2048.jpeg"))
        self.assertEqual(self.store.variant_lookup(bid, 2048)["size"], len(payload))
        self.assertEqual(self.store.variant_max(bid), [2048])
        self.assertEqual(len(self.store.variants_of(bid)), 1)

        stats = self.store.stats()
        # used = 原图 + 变体；manual 只算原图（不重复计变体）
        self.assertEqual(stats["used_bytes"], len(data) + len(payload))
        self.assertEqual(stats["manual_bytes"], len(data))
        self.assertEqual(stats["variant_count"], 1)
        self.assertEqual(stats["variant_bytes"], len(payload))

    def test_same_key_reuse_without_duplicating_bytes(self):
        bid, data = self.put_image(3000, 1000)
        self.store.save_variant(bid, 2048, "jpeg", "image/jpeg", 2048, 683, b"x" * 100)
        used = self.store.stats()["used_bytes"]
        again = self.store.save_variant(bid, 2048, "jpeg", "image/jpeg",
                                        2048, 683, b"y" * 100)
        self.assertIsNotNone(again)
        self.assertEqual(self.store.stats()["used_bytes"], used)
        with open(again["path"], "rb") as fh:
            self.assertEqual(fh.read(), b"x" * 100)

    def test_per_blob_limit_prunes_oldest(self):
        bid, _ = self.put_image(3000, 1000)
        sizes = []
        for i in range(VARIANT_PER_BLOB_MAX + 3):
            payload = bytes([65 + i]) * (100 + i)
            sizes.append(len(payload))
            self.store.save_variant(bid, 512 + i, "jpeg", "image/jpeg",
                                    512, 512, payload)
        variants = self.store.variants_of(bid)
        self.assertEqual(len(variants), VARIANT_PER_BLOB_MAX)
        # 保留的是最新的 N 个（max_side 最大）
        kept = {int(v["max_side"]) for v in variants}
        self.assertEqual(kept, {512 + i for i in
                                range(VARIANT_PER_BLOB_MAX + 3)[-VARIANT_PER_BLOB_MAX:]})
        for v in variants:
            self.assertTrue(os.path.exists(v["path"]))
        # 被清理的变体文件不在了
        stale = self.store.variant_path(bid, 512, "jpeg")
        self.assertFalse(os.path.exists(stale))
        stats = self.store.stats()
        blob_size = os.path.getsize(self.store.blob_path(bid, "png"))
        self.assertEqual(stats["used_bytes"],
                         blob_size + sum(sizes[-VARIANT_PER_BLOB_MAX:]))
        self.assertEqual(stats["variant_bytes"], sum(sizes[-VARIANT_PER_BLOB_MAX:]))

    def test_save_variant_for_missing_blob_returns_none(self):
        self.assertIsNone(self.store.save_variant(
            "b" * 64, 2048, "jpeg", "image/jpeg", 10, 10, b"x"))

    def test_purge_removes_variant_rows_and_files(self):
        bid, _ = self.put_image(3000, 1000)
        row = self.store.save_variant(bid, 2048, "jpeg", "image/jpeg",
                                      2048, 683, b"v" * 50)
        self.store.batch("delete", [bid])       # 软删：变体保留（可恢复）
        self.assertIsNotNone(self.store.variant_lookup(bid, 2048))
        self.store.batch("purge", [bid])
        self.assertIsNone(self.store.lookup(bid))
        self.assertEqual(self.store.variants_of(bid), [])
        self.assertFalse(os.path.exists(row["path"]))
        self.assertEqual(self.store.stats()["used_bytes"], 0)

    def test_capacity_eviction_removes_variant_bytes(self):
        root = tempfile.mkdtemp(prefix="ih-store-v4-cap-")
        self.addCleanup(shutil.rmtree, root, True)
        store = Store(root, cap_bytes=3000, thumb_max=512)
        store.init()

        def put(data, ref, origin="plugin"):
            bid = sha(data)
            tmp = os.path.join(store.tmp, bid + ".part")
            with open(tmp, "wb") as fh:
                fh.write(data)
            return store.put(tmp, bid, len(data), "png", "image/png", ref, {},
                             origin)[0]

        manual_id = put(b"M" * 1000, "m1", origin="manual")
        plugin_id = put(b"P" * 1000, "p1", origin="plugin")
        variant = store.save_variant(plugin_id, 2048, "jpeg", "image/jpeg",
                                     100, 100, b"V" * 500)
        self.assertIsNotNone(variant)
        # cap*0.9 = 2700；再写 1000 必须淘汰 plugin（连带变体）
        put(b"Q" * 1000, "p2", origin="plugin")
        self.assertIsNone(store.lookup(plugin_id))
        self.assertFalse(os.path.exists(variant["path"]))
        self.assertIsNotNone(store.lookup(manual_id))
        # 只剩 manual 1000 + 新 plugin 1000
        self.assertEqual(store.stats()["used_bytes"], 2000)

    def test_resync_used_includes_variants(self):
        bid, data = self.put_image(3000, 1000)
        self.store.save_variant(bid, 2048, "jpeg", "image/jpeg",
                                2048, 683, b"v" * 321)
        used = self.store.stats()["used_bytes"]
        self.store.release_conn()
        reopened = Store(self.root, cap_bytes=64 * 1024 * 1024)
        reopened.init()
        try:
            self.assertEqual(reopened.stats()["used_bytes"], used)
        finally:
            reopened.release_conn()


if __name__ == "__main__":
    unittest.main(verbosity=2)
