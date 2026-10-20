"""store v3 单测：ref 元数据迁移/写入、类型/内容标签边界、SQL 过滤与 total、keep/unkeep、CIR 解析。"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import (DDL_V1, SCHEMA_VERSION, Store,  # noqa: E402
                   clean_tags, normalize_ref)

HEX64_A = "a" * 64
HEX64_B = "b" * 64
HEX64_C = "c" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class StoreV3Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ih-store-v3-")
        self.store = Store(self.root, cap_bytes=64 * 1024 * 1024, thumb_max=512,
                           trash_days=7, change_keep_days=30)
        self.store.init()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, data: bytes, ref: str, origin: str = "plugin", tags=None,
            source=None):
        """写一张图，返回 (blob id, dedup)。"""
        bid = sha(data)
        source = source if source is not None else {}
        tmp = None
        if self.store.lookup(bid) is None:
            tmp = os.path.join(self.store.tmp, bid + ".part")
            with open(tmp, "wb") as fh:
                fh.write(data)
        return self.store.put(tmp, bid, len(data), "png", "image/png", ref,
                              source, origin, tags=tags)

    def source_of(self, bid: str):
        with self.store._lock:
            row = self.store._conn().execute(
                "SELECT platform, group_id, user_id, message_id, session_id, at,"
                " raw_url, tool, server, via FROM ref WHERE id=?", (bid,)).fetchone()
        return dict(row) if row else {}

    def tags_of(self, bid: str):
        _, items = self.store.list(limit=100)
        for item in items:
            if item["id"] == bid:
                return item["tags"]
        return []


class TestSchemaMigration(StoreV3Base):
    def test_schema_version_is_3_and_ref_has_metadata_columns(self):
        con = self.store._conn()
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0],
                         SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 3)
        cols = {row[1] for row in con.execute("PRAGMA table_info(ref)")}
        for name in ("tool", "server", "via"):
            self.assertIn(name, cols)
        # 不新增 blob.kind
        blob_cols = {row[1] for row in con.execute("PRAGMA table_info(blob)")}
        self.assertNotIn("kind", blob_cols)

    def test_migrate_from_v1_only_appends_and_keeps_rows(self):
        root = tempfile.mkdtemp(prefix="ih-store-v1-")
        self.addCleanup(shutil.rmtree, root, True)
        db = os.path.join(root, "image.db")
        con = sqlite3.connect(db)
        con.executescript(DDL_V1)
        con.execute("PRAGMA user_version=1")
        con.execute(
            "INSERT INTO blob(id,format,mime,size,thumb_size,created_at,last_access_at,"
            "name,origin,orphan_at,thumb_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (HEX64_A, "png", "image/png", 10, 0, 1000, 1000, "旧图", "plugin", None, None))
        con.execute(
            "INSERT INTO ref(id,ref,platform,group_id,user_id,message_id,session_id,at,raw_url)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (HEX64_A, "old-ref", "QQ", "111", "222", "m1", "s1", 1000, "http://x/y.png"))
        con.commit()
        con.close()

        store = Store(root, cap_bytes=1024 * 1024)
        store.init()
        con = store._conn()
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 3)
        cols = {row[1] for row in con.execute("PRAGMA table_info(ref)")}
        self.assertTrue({"tool", "server", "via"}.issubset(cols))
        row = con.execute("SELECT ref, platform, group_id, tool, server, via "
                          "FROM ref WHERE id=?", (HEX64_A,)).fetchone()
        self.assertEqual(row["ref"], "old-ref")
        self.assertEqual(row["platform"], "QQ")
        self.assertIsNone(row["tool"])
        self.assertIsNone(row["server"])
        self.assertIsNone(row["via"])
        self.assertIsNotNone(store.lookup(HEX64_A))


class TestPutMetadataAndTags(StoreV3Base):
    def test_put_writes_ref_metadata_and_filters_metadata_tags(self):
        source = {"platform": "QQ", "group_id": "123456", "user_id": "789",
                  "message_id": "m1", "session_id": "s1", "at": 1700000000000,
                  "raw_url": "http://x/y.png", "tool": "text_to_image",
                  "server": "image-host", "via": "tool"}
        tags = ["ai-generated", "猫", " 猫 ", "来源:QQ", "群:123456",
                "工具:text_to_image", "MCP:image-host", "类型:生成",
                "2025-01-01", "12:30", "image_search", "123456", "cat.png", "x" * 41]
        bid, dedup = self.put(b"png-bytes", "ref-1", tags=tags, source=source)
        self.assertFalse(dedup)
        self.assertEqual(self.source_of(bid), {
            "platform": "QQ", "group_id": "123456", "user_id": "789",
            "message_id": "m1", "session_id": "s1", "at": 1700000000000,
            "raw_url": "http://x/y.png", "tool": "text_to_image",
            "server": "image-host", "via": "tool"})
        self.assertEqual(self.tags_of(bid), ["ai-generated", "猫"])

    def test_dedup_keeps_tags_idempotent(self):
        bid1, _ = self.put(b"same-bytes", "r1", tags=["ai-generated"])
        bid2, dedup = self.put(b"same-bytes", "r2", tags=["ai-generated", "猫"])
        self.assertEqual(bid1, bid2)
        self.assertTrue(dedup)
        self.assertEqual(self.tags_of(bid1), ["ai-generated", "猫"])
        # 重复写入同一标签不重复
        self.put(b"same-bytes", "r3", tags=["猫"])
        self.assertEqual(self.tags_of(bid1), ["ai-generated", "猫"])

    def test_clean_tags_boundary(self):
        valid, ignored = clean_tags(["猫", "ai-generated", "猫", " 猫 "])
        self.assertEqual(valid, ["猫", "ai-generated"])
        self.assertEqual(ignored, [])
        valid, ignored = clean_tags("猫")
        self.assertEqual(valid, ["猫"])
        bad = ["来源:QQ", "群:1", "工具:x", "MCP:y", "类型:z", "123456",
               "2025-01-01", "12:30", "a.png", "image_search", HEX64_A, "x" * 41]
        valid, ignored = clean_tags(bad)
        self.assertEqual(valid, [])
        self.assertEqual(len(ignored), len(bad))
        self.assertFalse(clean_tags(None)[0])


class TestListFilters(StoreV3Base):
    def setUp(self):
        super().setUp()
        self.manual, _ = self.put(
            b"manual", "m1", origin="manual", tags=["cat", "ai-generated"],
            source={"platform": "QQ", "group_id": "g1", "user_id": "u1",
                    "at": 2000, "tool": "text_to_image", "server": "image-host",
                    "via": "tool"})
        self.plugin1, _ = self.put(
            b"plugin1", "p1", origin="plugin", tags=["meme"],
            source={"platform": "QQ", "group_id": "g2", "user_id": "u2",
                    "at": 3000, "tool": "meme_generator", "server": "image-host",
                    "via": "tool"})
        self.plugin2, _ = self.put(b"plugin2", "p2", origin="plugin",
                                    source={"session_id": "solo"})

    def test_sql_filters_count_total(self):
        total, items = self.store.list(origin="manual")
        self.assertEqual((total, len(items)), (1, 1))
        self.assertEqual(items[0]["id"], self.manual)

        for kwargs, expected in (
                ({"tool": "text_to_image"}, 1),
                ({"tool": "meme_generator"}, 1),
                ({"server": "image-host"}, 2),
                ({"platform": "QQ"}, 2),
                ({"group_id": "g2"}, 1),
                ({"origin": "plugin"}, 2),
                ({"tool": "nonexistent"}, 0),
        ):
            total, items = self.store.list(**kwargs)
            self.assertEqual(total, expected, kwargs)
            self.assertEqual(len(items), expected, kwargs)

    def test_tags_filter_is_and(self):
        total, _ = self.store.list(tags=["cat", "ai-generated"])
        self.assertEqual(total, 1)
        total, _ = self.store.list(tags=["cat", "meme"])
        self.assertEqual(total, 0)
        total, _ = self.store.list(tags=["meme"])
        self.assertEqual(total, 1)
        total, _ = self.store.list(tags=[])
        self.assertEqual(total, 3)

    def test_since_and_offset(self):
        with self.store._lock:
            con = self.store._conn()
            con.execute("UPDATE blob SET created_at=1000 WHERE id=?", (self.manual,))
            con.execute("UPDATE blob SET created_at=2000 WHERE id=?", (self.plugin1,))
            con.execute("UPDATE blob SET created_at=3000 WHERE id=?", (self.plugin2,))
        self.assertEqual(self.store.list(since=1500)[0], 2)
        self.assertEqual(self.store.list(since=2500)[0], 1)
        self.assertEqual(self.store.list(since=9999)[0], 0)

        total, page1 = self.store.list(limit=1, offset=0)
        self.assertEqual(total, 3)
        _, page2 = self.store.list(limit=1, offset=1)
        _, page3 = self.store.list(limit=1, offset=2)
        self.assertEqual(len(page1), 1)
        self.assertEqual(len(page2), 1)
        self.assertNotEqual(page1[0]["id"], page2[0]["id"])
        self.assertNotEqual(page2[0]["id"], page3[0]["id"])
        total, beyond = self.store.list(limit=1, offset=99)
        self.assertEqual((total, beyond), (3, []))

    def test_tags_gallery_only_and_trash_excluded(self):
        total, tags = self.store.tags()
        self.assertEqual(total, 2)
        self.assertEqual([t["tag"] for t in tags], ["ai-generated", "cat"])
        total, tags = self.store.tags(origin="")
        self.assertEqual(total, 3)
        self.assertIn("meme", [t["tag"] for t in tags])
        self.store.batch("delete", [self.manual])
        self.assertEqual(self.store.tags()[0], 0)
        self.assertNotIn("cat", [t["tag"] for t in self.store.tags(origin="")[1]])


class TestBatchReports(StoreV3Base):
    def test_keep_unkeep_reports_changed_skipped_failed(self):
        manual, _ = self.put(b"m", "m", origin="manual")
        plugin, _ = self.put(b"p", "p", origin="plugin")
        missing = HEX64_C
        res = self.store.batch("keep", [manual, plugin, missing])
        self.assertEqual(res["affected"], 1)
        self.assertEqual(res["changed_ids"], [plugin])
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(res["skipped_ids"], [manual])
        self.assertEqual([f["id"] for f in res["failed"]], [missing])
        self.assertEqual(self.store.lookup(plugin)["origin"], "manual")
        self.assertEqual(self.store.lookup(manual)["origin"], "manual")

        # 幂等：再 keep 不计 affected
        res = self.store.batch("keep", [plugin, manual])
        self.assertEqual((res["affected"], res["changed_ids"]), (0, []))
        self.assertEqual(res["skipped"], 2)

        # unkeep：manual → plugin
        res = self.store.batch("unkeep", [manual, plugin])
        self.assertEqual(res["affected"], 2)
        self.assertEqual(self.store.lookup(manual)["origin"], "plugin")
        self.assertEqual(self.store.lookup(plugin)["origin"], "plugin")
        res = self.store.batch("unkeep", [manual])
        self.assertEqual((res["affected"], res["skipped"]), (0, 1))

    def test_tag_affected_counts_images_not_rows(self):
        a, _ = self.put(b"a", "a")
        b, _ = self.put(b"b", "b")
        res = self.store.batch("tag", [a, b], {"add_tags": ["猫", "狗"]})
        self.assertEqual((res["affected"], len(res["changed_ids"])), (2, 2))
        # 幂等
        res = self.store.batch("tag", [a, b], {"add_tags": ["猫", "狗"]})
        self.assertEqual((res["affected"], res["skipped"]), (0, 2))
        # untag 两个标签 = 2 张图（行数 4）
        res = self.store.batch("untag", [a, b], {"remove_tags": ["猫", "狗"]})
        self.assertEqual(res["affected"], 2)
        # add+remove 同一张图只计 1 张
        self.store.batch("tag", [a], {"add_tags": ["旧"]})
        res = self.store.batch("tag", [a], {"add_tags": ["新"], "remove_tags": ["旧"]})
        self.assertEqual(res["affected"], 1)
        self.assertEqual(res["changed_ids"], [a])

    def test_metadata_tags_rejected_in_batch(self):
        a, _ = self.put(b"a", "a")
        res = self.store.batch("tag", [a], {"add_tags": ["来源:QQ", "群:123456"]})
        self.assertEqual(res["affected"], 0)
        self.assertEqual(len(res["ignored_tags"]), 2)
        self.assertEqual(self.tags_of(a), [])
        # 合法标签照写
        res = self.store.batch("tag", [a], {"add_tags": ["猫", "工具:image_tag"]})
        self.assertEqual(res["affected"], 1)
        self.assertEqual(self.tags_of(a), ["猫"])

    def test_tag_on_trash_is_skipped(self):
        a, _ = self.put(b"a", "a")
        self.store.batch("delete", [a])
        res = self.store.batch("tag", [a], {"add_tags": ["猫"]})
        self.assertEqual((res["affected"], res["skipped"]), (0, 1))
        self.assertEqual(self.tags_of(a), [])

    def test_preview_matches_batch(self):
        a, _ = self.put(b"a", "a", tags=["猫"])
        prev = self.store.preview("tag", [a], {"add_tags": ["猫"]})
        real = self.store.batch("tag", [a], {"add_tags": ["猫"]})
        self.assertEqual(prev["affected"], real["affected"])
        prev = self.store.preview("tag", [a], {"remove_tags": ["猫"]})
        real = self.store.batch("tag", [a], {"remove_tags": ["猫"]})
        self.assertEqual(prev["affected"], real["affected"])


class TestRefResolution(StoreV3Base):
    def test_normalize_ref_forms(self):
        self.assertEqual(normalize_ref(HEX64_A.upper()), HEX64_A)
        self.assertEqual(normalize_ref("h" + HEX64_A), HEX64_A)
        self.assertEqual(normalize_ref("[img:" + HEX64_A + "]"), HEX64_A)
        self.assertEqual(normalize_ref("[img:" + HEX64_A + ":群友发的猫]"), HEX64_A)
        self.assertEqual(normalize_ref("[img：h" + HEX64_A + "：猫]"), HEX64_A)
        self.assertEqual(normalize_ref("http://host:46680/i/" + HEX64_A), HEX64_A)
        self.assertEqual(normalize_ref("http://host:46680/i/" + HEX64_A + "/b64"),
                         HEX64_A)
        self.assertEqual(normalize_ref(" plugin-abc "), "plugin-abc")
        self.assertEqual(normalize_ref("user_avatar:123"), "user_avatar:123")

    def test_resolve_refs_and_lookup_ids(self):
        a, _ = self.put(b"a", "plugin-ref-a")
        refs = self.store.resolve_refs([
            "[img:" + a + ":描述]", "h" + a, "/i/" + a,
            "plugin-ref-a", "user_avatar:123", "[img:" + HEX64_B + "]"])
        self.assertEqual(refs, [a, "plugin-ref-a", "user_avatar:123", HEX64_B])
        ids, unmatched = self.store.lookup_ids(refs)
        self.assertEqual(ids, [a])
        self.assertEqual(unmatched, ["user_avatar:123", HEX64_B])


if __name__ == "__main__":
    unittest.main(verbosity=2)
