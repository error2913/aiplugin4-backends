"""同哈希去重 / 引用计数 / 清理逻辑的回归测试（直接测 Store，不启动 HTTP 服务）。

运行：python tests/test_dedupe_cleanup.py
"""

import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import DiskFull, Store  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f" | {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def make_store(cap_bytes=64 * 1024 * 1024, trash_days=7):
    root = tempfile.mkdtemp(prefix="ih-dedupe-test-")
    store = Store(root, cap_bytes=cap_bytes, thumb_max=512,
                  trash_days=trash_days, change_keep_days=30)
    store.init()
    return store, root


def put_bytes(store, data, ref, origin="plugin", fmt="png"):
    """新字节写临时文件后落库；已存在时纯 DB 追加 ref。"""
    bid = hashlib.sha256(data).hexdigest()
    tmp = None
    if store.lookup(bid) is None:
        tmp = os.path.join(store.tmp, bid + ".part")
        with open(tmp, "wb") as fh:
            fh.write(data)
    return store.put(tmp, bid, len(data), fmt, "image/png", ref,
                     {"session_id": "test"}, origin)


def live_ids(store):
    _, items = store.list(limit=100)
    return {it["id"] for it in items}


def trash_ids(store):
    _, items = store.list(limit=100, trash=True)
    return {it["id"] for it in items}


def change_events(store):
    return store.changes(0, 500)["events"]


def main():
    # ---------------------------------------------------------------- 1. 同哈希只存一份
    store, root = make_store()
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"same-bytes" * 100
        bid1, dedup1 = put_bytes(store, data, "r1")
        bid2, dedup2 = put_bytes(store, data, "r2")
        check("同字节两次上传拿到同一 id", bid1 == bid2, (bid1, bid2))
        check("首次非去重 / 再次去重", dedup1 is False and dedup2 is True, (dedup1, dedup2))
        stats = store.stats()
        check("库内只有 1 张", stats["count"] == 1, stats["count"])
        check("只占一份空间", stats["used_bytes"] == len(data), (stats["used_bytes"], len(data)))
        files = [f for _, _, fs in os.walk(store.shards) for f in fs]
        check("磁盘上只有 1 个 blob 文件", len(files) == 1, files)

        # ------------------------------------------------------------ 2. 释放一条 ref：字节保留
        released, freed = store.release(["r1"])
        check("release 1 条", released == 1 and freed == 0, (released, freed))
        check("释放一条后 blob 还在", store.lookup(bid1) is not None)
        released, freed = store.release(["r2"])
        check("release 最后一条 → freed=1", released == 1 and freed == 1, (released, freed))
        check("零引用后 blob 仍保留（等容量淘汰/回收站）", store.lookup(bid1) is not None)
        check("空间未提前释放", store.stats()["used_bytes"] == len(data))

        # ------------------------------------------------------------ 3. 去重命中时「手动」提升 origin
        put_bytes(store, data, "m1", origin="manual")
        check("手动上传同字节 → 仍 dedup", store.lookup(bid1)["origin"] == "manual",
              store.lookup(bid1)["origin"])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # ---------------------------------------------------------------- 4. 容量压力下不淘汰手动图
    store, root = make_store(cap_bytes=3000)
    try:
        manual_data = b"M" * 1000
        plugin_data = b"P" * 1000
        manual_id, _ = put_bytes(store, manual_data, "m1", origin="manual")
        plugin_id, _ = put_bytes(store, plugin_data, "p1", origin="plugin")
        # cap*0.9 = 2700：再加 1000 必须淘汰一条；候选只有 plugin
        put_bytes(store, b"Q" * 1000, "p2", origin="plugin")
        check("容量压力下淘汰的是插件图", store.lookup(plugin_id) is None)
        check("手动图未被淘汰", store.lookup(manual_id) is not None)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 只剩手动图时：必须返回 DiskFull（507），不能删手动图
    store, root = make_store(cap_bytes=3000)
    try:
        ids = []
        raised = False
        for i in range(4):
            try:
                bid, _ = put_bytes(store, bytes([65 + i]) * 1000, f"m{i}", origin="manual")
                ids.append(bid)
            except DiskFull:
                raised = True
                break
        check("只剩手动图时返回 DiskFull（507）", raised)
        check("DiskFull 后已有手动图全部仍在",
              all(store.lookup(bid) is not None for bid in ids), ids)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # ---------------------------------------------------------------- 5. 共享 ref 的删除/恢复/彻底删
    store, root = make_store()
    try:
        data = b"\x89PNG\r\n\x1a\n" + b"shared" * 80
        bid, _ = put_bytes(store, data, "s1")
        put_bytes(store, data, "s2")
        check("共享图只有一个 blob", store.stats()["count"] == 1)

        store.batch("delete", [bid])
        check("软删后不在图库", bid not in live_ids(store))
        check("软删后在回收站", bid in trash_ids(store))
        kinds = {(e["kind"], e.get("ref")) for e in change_events(store) if e["blob_id"] == bid}
        check("变更日志有两条 delete_ref",
              ("delete_ref", "s1") in kinds and ("delete_ref", "s2") in kinds, kinds)

        store.batch("restore", [bid])
        check("恢复后回到图库", bid in live_ids(store))

        store.batch("delete", [bid])
        store.batch("purge", [bid])
        check("彻底删除后 blob 不存在", store.lookup(bid) is None)
        released, _ = store.release(["s1", "s2"])
        check("ref 随 blob 级联删除", released == 0, released)

        # 彻底删除后同字节再上传 = 新 blob
        store2 = store
        bid_again, dedup_again = put_bytes(store2, data, "s3")
        check("彻底删除后重新上传是新 blob（dedup=False）", dedup_again is False, dedup_again)
        check("新 blob 可查到", store2.lookup(bid_again) is not None)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILS:
        print("FAILED:", FAILS)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
