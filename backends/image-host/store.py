"""存储层 v2：SQLite 元数据 + 文件系统字节。

新增（第三部分）：
- 批量操作（tag / untag / keep / unkeep / delete / restore / purge）+ 只读预演
- 回收站（软删除 `deleted_at`）与 TTL 清理
- 变更日志 `change_log`（供插件增量同步）
- 标签词表

性能设计：
- 线程局部连接复用（不再每请求新建连接）
- `meta` 表维护 used_bytes 计数，避免每次写入都全表 SUM
- 可变热路径的元数据内存 LRU（`/i/<id>` 命中后零查询）
- 列表查询消除 N+1（tags / sources 各一条批查询）
- 批量写走 executemany + 显式事务
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import queue
import re
import sqlite3
import threading
import time

log = logging.getLogger("image-host.store")

SCHEMA_VERSION = 4
META_CACHE_MAX = 4096
MAX_IDS = 2000
TAG_MAX_LEN = 40
# 每张原图最多缓存的模型变体数（防止 /m?max= 任意取值把磁盘撑爆）
VARIANT_PER_BLOB_MAX = 8

PRAGMAS = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
"""

DDL_V1 = """
CREATE TABLE IF NOT EXISTS blob (
  id             TEXT PRIMARY KEY,
  format         TEXT NOT NULL,
  mime           TEXT NOT NULL,
  size           INTEGER NOT NULL,
  thumb_size     INTEGER NOT NULL DEFAULT 0,
  created_at     INTEGER NOT NULL,
  last_access_at INTEGER NOT NULL,
  name           TEXT,
  origin         TEXT NOT NULL CHECK (origin IN ('plugin','manual')),
  orphan_at      INTEGER,
  thumb_path     TEXT
);

CREATE TABLE IF NOT EXISTS ref (
  id         TEXT NOT NULL REFERENCES blob(id) ON DELETE CASCADE,
  ref        TEXT NOT NULL,
  platform   TEXT,
  group_id   TEXT,
  user_id    TEXT,
  message_id TEXT,
  session_id TEXT,
  at         INTEGER,
  raw_url    TEXT,
  PRIMARY KEY (id, ref)
);

CREATE TABLE IF NOT EXISTS tag (
  id  TEXT NOT NULL REFERENCES blob(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  PRIMARY KEY (id, tag)
);

CREATE INDEX IF NOT EXISTS idx_blob_access ON blob(last_access_at);
CREATE INDEX IF NOT EXISTS idx_blob_orphan ON blob(orphan_at);
CREATE INDEX IF NOT EXISTS idx_blob_origin ON blob(origin);
CREATE INDEX IF NOT EXISTS idx_ref_blob     ON ref(id);
CREATE INDEX IF NOT EXISTS idx_ref_ref      ON ref(ref);
CREATE INDEX IF NOT EXISTS idx_ref_group    ON ref(group_id, at);
CREATE INDEX IF NOT EXISTS idx_tag_tag      ON tag(tag);
"""

DDL_V2_REST = """
CREATE INDEX IF NOT EXISTS idx_blob_deleted ON blob(deleted_at);
CREATE TABLE IF NOT EXISTS change_log (
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  at      INTEGER NOT NULL,
  kind    TEXT NOT NULL,
  blob_id TEXT,
  ref     TEXT,
  extra   TEXT
);
CREATE INDEX IF NOT EXISTS idx_change_at ON change_log(at);
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY,
  v INTEGER NOT NULL
);
"""

# v3：ref 追加工具/服务器/入口三列（只追加，不改既有列）；搜索过滤走索引
DDL_V3_REST = """
CREATE INDEX IF NOT EXISTS idx_ref_tool ON ref(tool);
CREATE INDEX IF NOT EXISTS idx_ref_server ON ref(server);
CREATE INDEX IF NOT EXISTS idx_ref_platform ON ref(platform);
"""

# v4：模型变体图（/m 端点缓存）+ 原图尺寸
DDL_V4_REST = """
CREATE TABLE IF NOT EXISTS variant (
  blob_id    TEXT NOT NULL REFERENCES blob(id) ON DELETE CASCADE,
  max_side   INTEGER NOT NULL,
  fmt        TEXT NOT NULL,
  mime       TEXT NOT NULL,
  w          INTEGER NOT NULL,
  h          INTEGER NOT NULL,
  size       INTEGER NOT NULL,
  path       TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (blob_id, max_side, fmt)
);
"""

ACTIONS = ("tag", "untag", "keep", "unkeep", "delete", "restore", "purge")


def now_ms() -> int:
    return int(time.time() * 1000)


class DiskFull(Exception):
    def __init__(self, used: int, manual: int):
        super().__init__("disk full")
        self.used = used
        self.manual = manual


class CountMismatch(Exception):
    def __init__(self, expected: int, actual: int):
        super().__init__("count mismatch")
        self.expected = expected
        self.actual = actual


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _marks(n):
    return ",".join("?" * n)


# ---------------------------------------------------------------- 标签边界（类型/内容 vs 元数据）

# 来源/群/工具/服务器等元数据只写 ref 列，永不写成标签。
_METADATA_TAG_PREFIXES = (
    "来源:", "来源：", "群:", "群：", "工具:", "工具：", "mcp:", "mcp：",
    "类型:", "类型：", "服务器:", "服务器：", "平台:", "平台：",
    "时间:", "时间：", "用户:", "用户：", "会话:", "会话：",
)
# 产出工具/内置工具名不能当内容标签
KNOWN_TOOL_NAMES = frozenset({
    "image_search", "image_keep", "image_unkeep", "image_rename",
    "image_tag", "image_tags", "image_view", "image_to_text",
    "text_to_image", "meme_generator", "read_raw", "pub_read",
    "generate_audio", "call_ob11_api", "get_image",
})
_META_DIGITS_RE = re.compile(r"^\d+$")                       # 纯群号 / QQ号 / 时间戳
_META_DATE_RE = re.compile(
    r"^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:[ T]\d{1,2}[:：]\d{2}(?:[:：]\d{2})?)?$")
_META_CLOCK_RE = re.compile(r"^\d{1,2}[:：]\d{2}(?:[:：]\d{2})?$")
_META_FILE_RE = re.compile(
    r"^.*\.(?:png|jpe?g|gif|webp|bmp|svg|avif|heic|tiff?|mp4|mov|webm|mkv|mp3|wav|ogg|flac|pdf|zip|rar|7z|txt)$",
    re.IGNORECASE)
_META_URL_RE = re.compile(r"^(?:https?|ftp)://", re.IGNORECASE)
_HEX_LIKE_RE = re.compile(r"^[0-9a-fA-F]{32,}$")             # sha256 之类的 ID 不是标签


def _metadata_tag_reason(tag: str) -> str:
    """返回拒绝原因；空串 = 合法的类型/内容标签。"""
    low = tag.lower()
    for prefix in _METADATA_TAG_PREFIXES:
        if low.startswith(prefix.lower()):
            return "元数据前缀"
    if low in KNOWN_TOOL_NAMES:
        return "工具名"
    if _META_URL_RE.match(tag):
        return "URL"
    if _META_DIGITS_RE.match(tag):
        return "纯数字（群号/用户号/时间戳）"
    if _META_DATE_RE.match(tag) or _META_CLOCK_RE.match(tag):
        return "时间"
    if _META_FILE_RE.match(tag):
        return "文件名"
    if _HEX_LIKE_RE.match(tag):
        return "图片 ID"
    if len(tag) > TAG_MAX_LEN:
        return f"超过 {TAG_MAX_LEN} 字符"
    if any(ord(ch) < 32 for ch in tag):
        return "控制字符"
    return ""


def clean_tags(tags) -> tuple[list[str], list[str]]:
    """把输入标签分成 (合法类型/内容标签, 被拒绝的元数据式标签)。

    - 字符串按单值处理；数组逐项 strip、去重
    - 拒绝 ``来源:`` / ``群:`` / ``工具:`` / ``MCP:`` / ``类型:`` 等前缀，
      以及纯群号/工具名/时间/文件名/图片 ID 等元数据式标签
    - 空标签静默丢弃（不进 ignored）
    """
    valid: list[str] = []
    ignored: list[str] = []
    if tags is None:
        return valid, ignored
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, (list, tuple)):
        return valid, [str(tags)]
    for raw in tags:
        if not isinstance(raw, str):
            ignored.append(str(raw))
            continue
        tag = raw.strip()
        if not tag:
            continue
        if _metadata_tag_reason(tag):
            ignored.append(tag)
            continue
        if tag not in valid:
            valid.append(tag)
    return valid, ignored


# ---------------------------------------------------------------- 图片引用（CIR v1）解析

_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IMG_WRAP_RE = re.compile(r"^[\[［]\s*img\s*[:：](.*)[\]］]$", re.IGNORECASE | re.DOTALL)
_AVATAR_REF_RE = re.compile(r"^(?:user_avatar|group_avatar)\s*[:：]", re.IGNORECASE)
_HOSTED_URL_RE = re.compile(r"/i/([0-9a-fA-F]{64})(?:/b64)?")
_HASH_REF_RE = re.compile(r"^h([0-9a-fA-F]{64})$", re.IGNORECASE)


def _hosted_of(part: str):
    part = (part or "").strip()
    if _HEX64_RE.match(part):
        return part.lower()
    m = _HASH_REF_RE.match(part)
    if m:
        return m.group(1).lower()
    m = _HOSTED_URL_RE.search(part)
    if m:
        return m.group(1).lower()
    return None


def normalize_ref(raw) -> str:
    """CIR/ref → 规范化目标：图床 sha256（小写）或插件 ref 原文。

    兼容 ``[img:<ref>]`` / 全角括号、``h<sha>``、裸 sha、``/i/<sha>`` 地址、
    ``[img:<ref>:描述]``（描述不参与查找）、插件 ref。
    """
    s = str(raw or "").strip()
    m = _IMG_WRAP_RE.match(s)
    if m:
        s = m.group(1).strip()
    hit = _hosted_of(s)
    if hit:
        return hit
    if _AVATAR_REF_RE.match(s):
        return s
    parts = re.split(r"[:：]", s, maxsplit=1)
    if len(parts) == 2:
        hit = _hosted_of(parts[0])
        if hit:
            return hit
    return s


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class Store:
    def __init__(self, root: str, *, cap_bytes: int, thumb_max: int = 512,
                 trash_days: int = 7, change_keep_days: int = 30):
        self.root = root
        self.db_path = os.path.join(root, "image.db")
        self.shards = os.path.join(root, "shards")
        self.thumbs = os.path.join(root, "thumbs")
        self.tmp = os.path.join(root, "tmp")
        self.cap = cap_bytes
        self.thumb_max = thumb_max
        self.trash_days = max(0, trash_days)
        self.change_keep_days = max(1, change_keep_days)
        self._lock = threading.RLock()
        self._local = threading.local()
        # 连接池：Flask 是「每请求一个线程」，thread-local 等于每请求新建连接
        # （实测新建连接在常驻多连接时常要 5–19ms）。这里跨请求复用连接。
        self._pool: "queue.LifoQueue" = queue.LifoQueue(maxsize=16)
        self._pillow_ok = None
        self._meta_cache: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self._meta_lock = threading.Lock()
        self._last_prune = 0

    # ---------------------------------------------------------------- 基础设施

    def init(self) -> None:
        for d in (self.root, self.shards, self.thumbs, self.tmp):
            os.makedirs(d, exist_ok=True)
        for name in os.listdir(self.tmp):
            try:
                os.unlink(os.path.join(self.tmp, name))
            except OSError:
                pass
        con = self._conn()
        self._migrate(con)
        with self._lock:
            self._resync_used(con)
            self._maybe_prune(con, force=True)
        log.info("存储就绪 db=%s used=%s changes=%s",
                 self.db_path, self.used_bytes(), self.change_head())

    def _migrate(self, con) -> None:
        con.executescript(PRAGMAS)
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            con.executescript(DDL_V1)
            con.execute("PRAGMA user_version=1")
        if version < 2:
            cols = {row[1] for row in con.execute("PRAGMA table_info(blob)")}
            if "deleted_at" not in cols:
                con.execute("ALTER TABLE blob ADD COLUMN deleted_at INTEGER")
            con.executescript(DDL_V2_REST)
            con.execute("PRAGMA user_version=2")
        if version < 3:
            # 只追加列：工具图片的来源元数据（tool / server / via）
            cols = {row[1] for row in con.execute("PRAGMA table_info(ref)")}
            for col in ("tool", "server", "via"):
                if col not in cols:
                    con.execute(f"ALTER TABLE ref ADD COLUMN {col} TEXT")
            con.executescript(DDL_V3_REST)
            con.execute("PRAGMA user_version=3")
        if version < 4:
            # 只追加列：原图宽高（旧数据留 NULL，/m 首次请求时惰性回填）+ 变体缓存表
            cols = {row[1] for row in con.execute("PRAGMA table_info(blob)")}
            for col in ("w", "h"):
                if col not in cols:
                    con.execute(f"ALTER TABLE blob ADD COLUMN {col} INTEGER")
            con.executescript(DDL_V4_REST)
            con.execute("PRAGMA user_version=4")
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _new_conn(self):
        con = sqlite3.connect(self.db_path, timeout=15,
                              check_same_thread=False, isolation_level=None)
        con.row_factory = sqlite3.Row
        # 注意：synchronous / foreign_keys 是「每连接」设置，新建连接必须重设，
        # 否则退回默认 FULL，每次 COMMIT 都 fsync
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=8000")
        con.execute("PRAGMA cache_size=-16000")
        con.execute("PRAGMA temp_store=MEMORY")
        con.execute("PRAGMA mmap_size=268435456")
        return con

    def _conn(self):
        con = getattr(self._local, "con", None)
        if con is not None:
            return con
        try:
            con = self._pool.get_nowait()
        except queue.Empty:
            con = self._new_conn()
        self._local.con = con
        return con

    def release_conn(self) -> None:
        """请求结束时把连接还回池（由 Flask teardown_request 调用）。"""
        con = getattr(self._local, "con", None)
        self._local.con = None
        if con is None:
            return
        try:
            if con.in_transaction:
                con.execute("ROLLBACK")
        except Exception:
            pass
        try:
            self._pool.put_nowait(con)
        except queue.Full:
            try:
                con.close()
            except Exception:
                pass

    @contextlib.contextmanager
    def _tx(self, con):
        con.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")

    def _used(self, con) -> int:
        row = con.execute("SELECT v FROM meta WHERE k='used'").fetchone()
        return int(row[0]) if row else 0

    def _add_used(self, con, delta: int) -> None:
        con.execute("INSERT INTO meta(k,v) VALUES('used',?) "
                    "ON CONFLICT(k) DO UPDATE SET v=v+excluded.v", (delta,))

    def _resync_used(self, con) -> None:
        """重算 used：原图 + 缩略图 + 模型变体字节。"""
        blob_total = con.execute(
            "SELECT COALESCE(SUM(size+thumb_size),0) FROM blob").fetchone()[0]
        variant_total = con.execute(
            "SELECT COALESCE(SUM(size),0) FROM variant").fetchone()[0]
        total = int(blob_total) + int(variant_total)
        con.execute("INSERT INTO meta(k,v) VALUES('used',?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (total,))

    # ---------------------------------------------------------------- 元数据缓存

    def _cache_get(self, bid: str):
        with self._meta_lock:
            row = self._meta_cache.get(bid)
            if row is not None:
                self._meta_cache.move_to_end(bid)
            return row

    def _cache_put(self, bid: str, row: dict) -> None:
        with self._meta_lock:
            self._meta_cache[bid] = row
            self._meta_cache.move_to_end(bid)
            while len(self._meta_cache) > META_CACHE_MAX:
                self._meta_cache.popitem(last=False)

    def _cache_drop(self, bid: str) -> None:
        with self._meta_lock:
            self._meta_cache.pop(bid, None)

    def lookup(self, bid: str):
        """热路径：缓存命中即返回；未命中只查一次主键。"""
        cached = self._cache_get(bid)
        if cached is not None:
            return cached
        row = self._conn().execute(
            "SELECT id, format, mime, size, thumb_path, deleted_at, last_access_at,"
            "       origin, created_at, name, w, h FROM blob WHERE id=?", (bid,)).fetchone()
        if not row:
            return None
        data = dict(row)
        self._cache_put(bid, data)
        return data

    # ---------------------------------------------------------------- 路径

    def blob_path(self, bid: str, fmt: str) -> str:
        return os.path.join(self.shards, bid[:2], f"{bid}.{fmt}")

    def thumb_path(self, bid: str) -> str:
        return os.path.join(self.thumbs, bid[:2], f"{bid}.webp")

    def variant_path(self, bid: str, max_side, fmt: str) -> str:
        """变体落盘路径：``shards/<xx>/<id>.m<max>.<fmt>``。"""
        return os.path.join(self.shards, bid[:2],
                            f"{bid}.m{int(max_side)}.{fmt}")

    @staticmethod
    def _ensure_parent(path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)

    # ---------------------------------------------------------------- 容量

    def used_bytes(self) -> int:
        return self._used(self._conn())

    def manual_bytes(self, con=None) -> int:
        """长期图库口径：只算原图 + 缩略图，**不含**模型变体（变体走 used_bytes）。"""
        con = con or self._conn()
        row = con.execute(
            "SELECT COALESCE(SUM(size+thumb_size),0) FROM blob WHERE origin='manual'"
        ).fetchone()
        return int(row[0])

    def _victim(self, con):
        """淘汰顺序：回收站最旧 → 插件抓的零引用 → 插件抓的任意。manual 永不自动删。"""
        row = con.execute(
            "SELECT id FROM blob WHERE deleted_at IS NOT NULL "
            "ORDER BY deleted_at ASC LIMIT 1").fetchone()
        if row:
            return row["id"]
        row = con.execute(
            "SELECT id FROM blob b WHERE b.origin='plugin' AND b.deleted_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM ref r WHERE r.id=b.id) "
            "ORDER BY COALESCE(b.orphan_at, b.created_at) ASC LIMIT 1").fetchone()
        if row:
            return row["id"]
        row = con.execute(
            "SELECT id FROM blob WHERE origin='plugin' AND deleted_at IS NULL "
            "ORDER BY last_access_at ASC LIMIT 1").fetchone()
        return row["id"] if row else None

    def _ensure_capacity(self, con, need: int) -> int:
        dropped = 0
        while self._used(con) + need > self.cap * 0.9:
            victim = self._victim(con)
            if victim is None:
                raise DiskFull(self._used(con), self.manual_bytes(con))
            self._hard_delete(con, victim, reason="cap")
            dropped += 1
            log.info("容量淘汰 blob=%s（第 %d 条）", victim, dropped)
        return dropped

    def _hard_delete(self, con, bid: str, reason: str) -> None:
        row = con.execute(
            "SELECT format, thumb_path, size, thumb_size, deleted_at FROM blob WHERE id=?",
            (bid,)).fetchone()
        if not row:
            return
        # 变体缓存随原图一起清（FK ON DELETE CASCADE 管行，文件要显式删）
        variants = con.execute(
            "SELECT path, size FROM variant WHERE blob_id=?", (bid,)).fetchall()
        variant_size = sum(int(v["size"] or 0) for v in variants)
        # 还在图库里的图被强制删除（cap 压力）→ 要通知插件；回收站里的 → purge 事件
        if row["deleted_at"] is None:
            self._log_delete(con, bid)
        else:
            self._log(con, "purge_blob", bid, None, reason)
        con.execute("DELETE FROM variant WHERE blob_id=?", (bid,))
        con.execute("DELETE FROM blob WHERE id=?", (bid,))   # ref/tag CASCADE
        self._add_used(con, -(int(row["size"]) + int(row["thumb_size"]) + variant_size))
        self._cache_drop(bid)
        paths = [self.blob_path(bid, row["format"]), row["thumb_path"]]
        paths.extend(v["path"] for v in variants)
        for path in paths:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # ---------------------------------------------------------------- 变更日志

    def _log(self, con, kind: str, blob_id, ref, extra=None) -> None:
        con.execute(
            "INSERT INTO change_log(at,kind,blob_id,ref,extra) VALUES(?,?,?,?,?)",
            (now_ms(), kind, blob_id, ref, extra))

    def _log_delete(self, con, bid: str) -> None:
        refs = con.execute("SELECT ref FROM ref WHERE id=?", (bid,)).fetchall()
        if refs:
            ts = now_ms()
            con.executemany(
                "INSERT INTO change_log(at,kind,blob_id,ref) VALUES(?,?,?,?)",
                [(ts, "delete_ref", bid, r["ref"]) for r in refs])
        self._log(con, "delete_blob", bid, None)

    def change_head(self) -> int:
        row = self._conn().execute(
            "SELECT COALESCE(MAX(seq),0) FROM change_log").fetchone()
        return int(row[0])

    def changes(self, since: int, limit: int):
        con = self._conn()
        head = self.change_head()
        min_seq = con.execute("SELECT COALESCE(MIN(seq),0) FROM change_log").fetchone()[0]
        if since > 0 and min_seq and since < min_seq - 1:
            return {"cursor": head, "reset": True, "events": []}
        rows = con.execute(
            "SELECT seq, at, kind, blob_id, ref FROM change_log "
            "WHERE seq > ? ORDER BY seq LIMIT ?", (since, limit)).fetchall()
        events = [dict(r) for r in rows]
        cursor = int(rows[-1]["seq"]) if rows else head
        return {"cursor": cursor, "reset": False, "events": events}

    def _maybe_prune(self, con, force=False) -> None:
        ts = time.time()
        if not force and ts - self._last_prune < 3600:
            return
        self._last_prune = ts
        con.execute("DELETE FROM change_log WHERE at < ?",
                    (now_ms() - self.change_keep_days * 86400_000,))
        if self.trash_days > 0:
            expired = con.execute(
                "SELECT id FROM blob WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (now_ms() - self.trash_days * 86400_000,)).fetchall()
            for row in expired:
                self._hard_delete(con, row["id"], reason="trash-expired")

    # ---------------------------------------------------------------- 写入

    def write_blob(self, bid: str, fmt: str, data: bytes) -> str:
        """直接把字节写到终态路径（省掉 tmp 文件 + rename 两次文件操作）。"""
        dst = self.blob_path(bid, fmt)
        self._ensure_parent(dst)
        with open(dst, "wb") as fh:
            fh.write(data)
        return dst

    def put(self, tmp_path: str | None, bid: str, size: int, fmt: str, mime: str,
            ref: str, source: dict | None, origin: str = "plugin",
            name: str | None = None, file_ready: bool = False,
            tags: list | str | None = None,
            w: int | None = None, h: int | None = None):
        """落库。哈希命中只追加 ref，不写第二份字节。返回 (id, dedup)。

        - ``tmp_path=None`` + ``file_ready=False``：调用方已确认命中已有 blob，纯 DB 更新
        - ``file_ready=True``：调用方已用 ``write_blob`` 写好终态文件
        - 否则把 ``tmp_path`` 原子搬到终态
        - ``tags``：只允许类型/内容标签，与 blob/ref **同事务**写 tag 表；
          元数据式标签（来源:/群:/工具:/MCP:/类型: 前缀、纯群号/工具名/时间/文件名等）被过滤
        - ``source`` 可带 ``tool`` / ``server`` / ``via``（写 ref 列，不进标签）
        - ``w`` / ``h``：原图尺寸（调用方用 PIL 读；失败传 None，不阻断入库）；
          去重命中时旧行 NULL 会被本次值回填
        文件搬迁与缩略图生成在事务**之外**：CPU/IO 再慢也不占写锁。
        """
        source = source or {}
        with self._lock:
            con = self._conn()
            exists = con.execute(
                "SELECT id, deleted_at FROM blob WHERE id=?", (bid,)).fetchone()
            dedup = exists is not None
            pending_file = None
            if dedup:
                if tmp_path:
                    _safe_unlink(tmp_path)
            else:
                dst = self.blob_path(bid, fmt)
                if file_ready:
                    pending_file = dst
                else:
                    if not tmp_path:
                        raise ValueError("tmp_required")
                    self._ensure_parent(dst)
                    os.replace(tmp_path, dst)
                    pending_file = dst
                thumb, thumb_size = self._make_thumb(bid, dst, size)
                ts = now_ms()
            try:
                with self._tx(con):
                    if dedup:
                        # 同哈希命中：只加一条 ref，不写第二份字节。
                        # 清理适配：只要这次是「手动」来源（WebUI 上传 / 存图库），就把 blob 提升为
                        # manual —— 否则先是插件抓的、后来用户手动上传同一张图时，blob 仍是 plugin
                        # 来源，容量压力下会被自动淘汰，违反「手动上传的图永不自动删」。
                        con.execute(
                            "UPDATE blob SET last_access_at=?, name=COALESCE(name, ?),"
                            " origin=CASE WHEN ?='manual' THEN 'manual' ELSE origin END,"
                            " deleted_at=NULL, w=COALESCE(w, ?), h=COALESCE(h, ?) "
                            "WHERE id=?",
                            (now_ms(), name or None, origin, w, h, bid))
                        if exists["deleted_at"] is not None:   # 死而复生
                            self._log(con, "restore_blob", bid, None)
                        self._cache_drop(bid)
                    else:
                        self._ensure_capacity(con, size)
                        con.execute(
                            "INSERT INTO blob(id,format,mime,size,thumb_size,created_at,"
                            "last_access_at,name,origin,thumb_path,w,h) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (bid, fmt, mime, size, thumb_size, ts, ts,
                             (name or None), origin, thumb, w, h))
                        self._add_used(con, size + thumb_size)
                    con.execute(
                        "INSERT OR REPLACE INTO ref(id,ref,platform,group_id,user_id,"
                        "message_id,session_id,at,raw_url,tool,server,via) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (bid, ref, source.get("platform"), source.get("group_id"),
                         source.get("user_id"), source.get("message_id"),
                         source.get("session_id"), source.get("at"),
                         source.get("raw_url"), source.get("tool"),
                         source.get("server"), source.get("via")))
                    self._apply_tags(con, bid, tags)
                    self._maybe_prune(con)
            except BaseException:
                if pending_file:      # 事务失败：清掉刚落位的文件，避免孤儿
                    _safe_unlink(pending_file)
                raise
        return bid, dedup

    def _apply_tags(self, con, bid: str, tags) -> list[str]:
        """同事务写标签（幂等）；返回真正合法写入的标签。"""
        valid, ignored = clean_tags(tags)
        for tag in valid:
            con.execute("INSERT OR IGNORE INTO tag(id,tag) VALUES(?,?)", (bid, tag))
        if ignored:
            log.info("忽略元数据式标签 id=%s tags=%s", bid, ignored[:5])
        return valid

    def _make_thumb(self, bid: str, src: str, size: int = 0):
        """只对大图生成缩略图：小图直接发原图更快，也省一份文件。"""
        if self._pillow_ok is None:
            try:
                import PIL.Image  # noqa: F401
                self._pillow_ok = True
            except Exception:
                self._pillow_ok = False
                log.warning("Pillow 不可用，缩略图功能关闭")
        if not self._pillow_ok:
            return None, 0
        # 小文件直接发原图：连 Pillow 都不用开，省一次磁盘读（Windows 上可达数毫秒）
        if size <= 64 * 1024:
            return None, 0
        try:
            from PIL import Image

            with Image.open(src) as im:
                width, height = im.size
                if max(width, height) <= self.thumb_max:
                    return None, 0          # 比缩略图还小，没必要再存一份
                im.seek(0)
                if im.mode not in ("RGB", "RGBA"):
                    im = im.convert("RGBA")
                im.thumbnail((self.thumb_max, self.thumb_max), Image.LANCZOS)
                dst = self.thumb_path(bid)
                self._ensure_parent(dst)
                im.save(dst, "WEBP", quality=78, method=4)
            return dst, os.path.getsize(dst)
        except Exception as exc:
            log.warning("缩略图生成失败 %s: %s", bid, exc)
            return None, 0

    # ---------------------------------------------------------------- 模型变体缓存

    def set_size(self, bid: str, w: int, h: int) -> None:
        """回填旧数据的原图宽高（仅在缺值时写入）。"""
        try:
            w, h = int(w), int(h)
        except (TypeError, ValueError):
            return
        if w <= 0 or h <= 0:
            return
        with self._lock:
            con = self._conn()
            con.execute(
                "UPDATE blob SET w=?, h=? WHERE id=? AND (w IS NULL OR h IS NULL)",
                (w, h, bid))
            self._cache_drop(bid)

    def variant_lookup(self, bid: str, max_side) -> dict | None:
        """查该图在指定 ``max_side`` 下已缓存的变体（任一输出格式，取最新）。"""
        row = self._conn().execute(
            "SELECT blob_id, max_side, fmt, mime, w, h, size, path, created_at "
            "FROM variant WHERE blob_id=? AND max_side=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (bid, int(max_side))).fetchone()
        return dict(row) if row else None

    def variant_max(self, bid: str) -> list[int]:
        """该图已缓存变体的 ``max_side`` 列表（降序，去重）。"""
        rows = self._conn().execute(
            "SELECT DISTINCT max_side FROM variant WHERE blob_id=? "
            "ORDER BY max_side DESC", (bid,)).fetchall()
        return [int(r["max_side"]) for r in rows]

    def variants_of(self, bid: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT blob_id, max_side, fmt, mime, w, h, size, path, created_at "
            "FROM variant WHERE blob_id=? ORDER BY created_at, rowid", (bid,)).fetchall()
        return [dict(r) for r in rows]

    def save_variant(self, bid: str, max_side, fmt: str, mime: str,
                     w: int, h: int, data: bytes) -> dict | None:
        """写变体文件并落库，返回变体行；缓存不可用（磁盘满/原图已删）返回 None。

        - 同一 ``(bid, max_side, fmt)`` 已存在且文件在 → 直接复用，不重写；
        - 变体字节计入 ``meta.used``（与 ``stats()`` / 容量淘汰同口径），
          **不**计入 ``manual_bytes``（那是原图口径，避免重复计数）；
        - 每张原图最多保留 ``VARIANT_PER_BLOB_MAX`` 个变体，超出按旧→新清理文件。
        调用方（``/m``）即使拿不到缓存也必须能继续回响应字节，所以这里不抛业务异常。
        """
        fmt = str(fmt or "").strip().lower()
        max_side = int(max_side)
        payload = bytes(data or b"")
        size = len(payload)
        with self._lock:
            con = self._conn()
            if not con.execute("SELECT 1 FROM blob WHERE id=?", (bid,)).fetchone():
                return None
            existing = con.execute(
                "SELECT blob_id, max_side, fmt, mime, w, h, size, path, created_at "
                "FROM variant WHERE blob_id=? AND max_side=? AND fmt=?",
                (bid, max_side, fmt)).fetchone()
            if existing and os.path.exists(existing["path"]):
                return dict(existing)
            path = self.variant_path(bid, max_side, fmt)
            self._ensure_parent(path)
            part = path + ".part"
            try:
                with open(part, "wb") as fh:
                    fh.write(payload)
                os.replace(part, path)
            except OSError as exc:
                _safe_unlink(part)
                log.warning("变体落盘失败 id=%s max=%s fmt=%s: %s",
                            bid, max_side, fmt, exc)
                return None
            ts = now_ms()
            pruned: list = []
            try:
                with self._tx(con):
                    # 事务内再查一次：并发 /m 生成同一 (bid,max,fmt) 时不重复计容量
                    current = con.execute(
                        "SELECT size FROM variant WHERE blob_id=? AND max_side=? "
                        "AND fmt=?", (bid, max_side, fmt)).fetchone()
                    effective_old = int(current["size"]) if current else 0
                    con.execute(
                        "INSERT OR REPLACE INTO variant(blob_id, max_side, fmt, mime,"
                        " w, h, size, path, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (bid, max_side, fmt, mime, int(w), int(h), size, path, ts))
                    self._add_used(con, size - effective_old)
                    pruned = self._prune_variants(con, bid)
            except BaseException as exc:
                # 原图恰好被并发删除（FK 失败）或 DB 异常：缓存放弃，不影响本次响应
                _safe_unlink(path)
                log.info("变体入库失败 id=%s max=%s fmt=%s: %s",
                         bid, max_side, fmt, exc)
                return None
            for row in pruned:
                _safe_unlink(row["path"])
            if pruned:
                log.info("变体缓存清理 id=%s 条数=%d", bid, len(pruned))
            return {"blob_id": bid, "max_side": max_side, "fmt": fmt, "mime": mime,
                    "w": int(w), "h": int(h), "size": size, "path": path,
                    "created_at": ts}

    def _prune_variants(self, con, bid: str) -> list:
        """每张原图只保留最新 N 个变体；返回被清掉的行（事务内调用）。"""
        rows = con.execute(
            "SELECT path, size FROM variant WHERE blob_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (bid, VARIANT_PER_BLOB_MAX)).fetchall()
        for row in rows:
            con.execute("DELETE FROM variant WHERE blob_id=? AND path=?",
                        (bid, row["path"]))
            self._add_used(con, -int(row["size"]))
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 引用

    def release(self, refs):
        released = freed = 0
        with self._lock:
            con = self._conn()
            with self._tx(con):
                for ref in refs:
                    rows = con.execute("SELECT id FROM ref WHERE ref=?", (ref,)).fetchall()
                    for row in rows:
                        bid = row["id"]
                        cur = con.execute(
                            "DELETE FROM ref WHERE id=? AND ref=?", (bid, ref))
                        if cur.rowcount <= 0:
                            continue
                        released += 1
                        left = con.execute(
                            "SELECT COUNT(*) FROM ref WHERE id=?", (bid,)).fetchone()[0]
                        if left == 0:
                            con.execute(
                                "UPDATE blob SET orphan_at=? WHERE id=? AND orphan_at IS NULL",
                                (now_ms(), bid))
                            self._cache_drop(bid)
                            freed += 1
        return released, freed

    def missing(self, refs):
        if not refs:
            return []
        con = self._conn()
        present = set()
        for chunk in _chunks(refs, 400):
            rows = con.execute(
                f"SELECT DISTINCT ref FROM ref WHERE ref IN ({_marks(len(chunk))})",
                chunk).fetchall()
            present.update(r["ref"] for r in rows)
        return [r for r in refs if r not in present]

    # ---------------------------------------------------------------- 单张

    def get(self, bid: str):
        return self.lookup(bid)

    def touch(self, bid: str, row: dict | None = None, throttle_ms: int = 3_600_000):
        row = row or self.lookup(bid)
        if not row:
            return None
        ts = now_ms()
        if ts - int(row["last_access_at"]) >= throttle_ms:
            self._conn().execute(
                "UPDATE blob SET last_access_at=? WHERE id=?", (ts, bid))
            row["last_access_at"] = ts
            self._cache_put(bid, row)
        return row["format"]

    def patch(self, bid: str, *, name=None, add_tags=None, remove_tags=None,
              origin=None) -> bool:
        with self._lock:
            con = self._conn()
            with self._tx(con):
                if not con.execute("SELECT 1 FROM blob WHERE id=?", (bid,)).fetchone():
                    return False
                sets, args = [], []
                if name is not None:
                    sets.append("name=?")
                    args.append(name)
                if origin in ("plugin", "manual"):
                    sets.append("origin=?")
                    args.append(origin)
                if sets:
                    con.execute(
                        f"UPDATE blob SET {','.join(sets)} WHERE id=?", (*args, bid))
                    self._cache_drop(bid)
                clean_add, _ = clean_tags(add_tags)
                clean_rm, _ = clean_tags(remove_tags)
                for tag in clean_add:
                    con.execute(
                        "INSERT OR IGNORE INTO tag(id,tag) VALUES(?,?)", (bid, tag))
                for tag in clean_rm:
                    con.execute("DELETE FROM tag WHERE id=? AND tag=?", (bid, tag))
                return True

    def delete(self, bid: str) -> bool:
        return self.batch("delete", [bid])["affected"] > 0

    # ---------------------------------------------------------------- 列表

    def _filter_where(self, flt: dict, deleted: bool):
        where, args = [], []
        q = str(flt.get("q") or "").strip()
        if q:
            like = f"%{q}%"
            where.append(
                "(b.name LIKE ? "
                " OR EXISTS(SELECT 1 FROM ref r WHERE r.id=b.id AND "
                "(r.group_id LIKE ? OR r.user_id LIKE ? OR r.message_id LIKE ?)) "
                " OR EXISTS(SELECT 1 FROM tag t WHERE t.id=b.id AND t.tag LIKE ?))")
            args += [like, like, like, like, like]
        tag = str(flt.get("tag") or "").strip()
        if tag:
            where.append("EXISTS(SELECT 1 FROM tag t WHERE t.id=b.id AND t.tag=?)")
            args.append(tag)
        tags = flt.get("tags") or []
        if isinstance(tags, str):
            tags = [tags]
        for one in tags:
            one = str(one or "").strip()
            if one:
                where.append("EXISTS(SELECT 1 FROM tag t WHERE t.id=b.id AND t.tag=?)")
                args.append(one)
        origin = str(flt.get("origin") or "").strip()
        if origin in ("plugin", "manual"):
            where.append("b.origin=?")
            args.append(origin)
        # 来源元数据一律下推 SQL（platform / group_id / tool / server），total 才是过滤后的数
        for key, col in (("platform", "platform"), ("group_id", "group_id"),
                         ("tool", "tool"), ("server", "server")):
            val = str(flt.get(key) or "").strip()
            if val:
                where.append(
                    f"EXISTS(SELECT 1 FROM ref r WHERE r.id=b.id AND r.{col}=?)")
                args.append(val)
        since = flt.get("since")
        if since not in (None, ""):
            try:
                since_ms = int(since)
            except (TypeError, ValueError):
                since_ms = 0
            if since_ms > 0:
                where.append("b.created_at >= ?")
                args.append(since_ms)
        where.append("b.deleted_at IS NOT NULL" if deleted else "b.deleted_at IS NULL")
        return " AND ".join(where), args

    def list(self, *, q: str = "", tag: str = "", tags=None, origin: str = "",
             platform: str = "", group_id: str = "", tool: str = "",
             server: str = "", since=None, limit: int = 24, offset: int = 0,
             trash: bool = False):
        """三查询取全页：blob / tags / sources。

        ``tags`` 为多标签 AND；``since`` 为毫秒时间戳（按 ``blob.created_at``）。
        ``platform`` / ``group_id`` / ``tool`` / ``server`` 都下推 SQL，``total`` 正确。
        """
        clause, args = self._filter_where(
            {"q": q, "tag": tag, "tags": tags or [], "origin": origin,
             "platform": platform, "group_id": group_id, "tool": tool,
             "server": server, "since": since}, trash)
        con = self._conn()
        total = con.execute(
            f"SELECT COUNT(*) FROM blob b WHERE {clause}", args).fetchone()[0]
        rows = con.execute(
            f"SELECT b.*, (SELECT COUNT(*) FROM ref r WHERE r.id=b.id) AS ref_count "
            f"FROM blob b WHERE {clause} "
            f"ORDER BY b.created_at DESC, b.rowid DESC LIMIT ? OFFSET ?",
            (*args, int(limit), int(offset))).fetchall()
        if not rows:
            return int(total), []

        ids = [r["id"] for r in rows]
        marks = _marks(len(ids))
        tags_map: dict[str, list] = {}
        for t in con.execute(
                f"SELECT id, tag FROM tag WHERE id IN ({marks}) ORDER BY tag", ids):
            tags_map.setdefault(t["id"], []).append(t["tag"])
        src_map: dict[str, list] = {}
        for s in con.execute(
                f"SELECT id, platform, group_id, user_id, message_id, session_id, at, "
                f"raw_url, tool, server, via "
                f"FROM ref WHERE id IN ({marks}) ORDER BY at DESC", ids):
            src_map.setdefault(s["id"], []).append(
                {k: s[k] for k in ("platform", "group_id", "user_id", "message_id",
                                   "session_id", "at", "raw_url",
                                   "tool", "server", "via")})

        items = []
        for row in rows:
            bid = row["id"]
            items.append({
                "id": bid,
                "name": row["name"] or "",
                "tags": tags_map.get(bid, []),
                "origin": row["origin"],
                "format": row["format"],
                "mime": row["mime"],
                "size": row["size"],
                "thumb_size": row["thumb_size"] or 0,
                "created_at": row["created_at"],
                "last_access_at": row["last_access_at"],
                "deleted_at": row["deleted_at"],
                "ref_count": row["ref_count"],
                "has_thumb": bool(row["thumb_path"]),
                "sources": src_map.get(bid, []),
            })
        return int(total), items

    def tags(self, q: str = "", limit: int = 100, offset: int = 0,
             origin: str = "manual"):
        """标签清单（按使用次数降序，支持关键字过滤与分页）。返回 (total, tags)。

        - 默认只统计 `origin=manual`（长期图库，image_tags 口径）；``origin=""`` = 全部来源
        - 回收站里的图片不计数
        """
        con = self._conn()
        where = ["b.deleted_at IS NULL"]
        args: list = []
        if origin in ("plugin", "manual"):
            where.append("b.origin=?")
            args.append(origin)
        q = (q or "").strip()
        if q:
            where.append("t.tag LIKE ?")
            args.append(f"%{q}%")
        base = (f"FROM tag t JOIN blob b ON b.id=t.id WHERE {' AND '.join(where)}")
        total = con.execute(
            f"SELECT COUNT(DISTINCT t.tag) {base}", args).fetchone()[0]
        rows = con.execute(
            f"SELECT t.tag, COUNT(*) c {base} GROUP BY t.tag "
            f"ORDER BY c DESC, t.tag LIMIT ? OFFSET ?",
            (*args, int(limit), int(offset))).fetchall()
        return int(total), [{"tag": r["tag"], "count": int(r["c"])} for r in rows]

    def stats(self):
        con = self._conn()
        count = con.execute(
            "SELECT COUNT(*) FROM blob WHERE deleted_at IS NULL").fetchone()[0]
        trash = con.execute(
            "SELECT COUNT(*) FROM blob WHERE deleted_at IS NOT NULL").fetchone()[0]
        variant_count, variant_bytes = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM variant").fetchone()
        return {"count": int(count), "trash": int(trash),
                "used_bytes": self._used(con), "manual_bytes": self.manual_bytes(con),
                "variant_count": int(variant_count),
                "variant_bytes": int(variant_bytes),
                "changes_head": self.change_head()}

    # ---------------------------------------------------------------- 引用解析（CIR / 插件 ref）

    def resolve_refs(self, raws) -> list[str]:
        """把任意 CIR/ref 输入规范化为待解析目标（sha256 或插件 ref 原文），去重保序。"""
        if isinstance(raws, str):
            raws = [raws]
        out: list[str] = []
        for raw in raws or []:
            norm = normalize_ref(raw)
            if norm and norm not in out:
                out.append(norm)
        return out

    def lookup_ids(self, refs) -> tuple[list[str], list[str]]:
        """CIR/ref → blob id。返回 ``(ids, unmatched)``；``unmatched`` 为查不到的目标。"""
        con = self._conn()
        ids: list[str] = []
        unmatched: list[str] = []
        seen: set[str] = set()
        for ref in refs or []:
            if _HEX64_RE.match(ref):
                row = con.execute("SELECT id FROM blob WHERE id=?", (ref,)).fetchone()
                if row:
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        ids.append(row["id"])
                else:
                    unmatched.append(ref)
                continue
            rows = con.execute(
                "SELECT DISTINCT id FROM ref WHERE ref=? ORDER BY id", (ref,)).fetchall()
            if rows:
                for row in rows:
                    if row["id"] not in seen:
                        seen.add(row["id"])
                        ids.append(row["id"])
            else:
                unmatched.append(ref)
        return ids, unmatched

    # ---------------------------------------------------------------- 批量

    def resolve(self, body: dict):
        ids = body.get("ids")
        flt = body.get("filter")
        if ids:
            clean = [str(x) for x in ids if x]
            return list(dict.fromkeys(clean)), None
        if flt is not None:
            deleted = bool(flt.get("trash"))
            clause, args = self._filter_where(flt, deleted)
            rows = self._conn().execute(
                f"SELECT b.id FROM blob b WHERE {clause}", args).fetchall()
            return [r["id"] for r in rows], flt
        raise ValueError("need_ids_or_filter")

    @staticmethod
    def _existing(con, ids):
        out = {}
        for chunk in _chunks(ids, 400):
            for row in con.execute(
                    f"SELECT id, format, origin, deleted_at FROM blob "
                    f"WHERE id IN ({_marks(len(chunk))})", chunk):
                out[row["id"]] = dict(row)
        return out

    def preview(self, action: str, ids: list, body: dict | None = None) -> dict:
        """只读预演：返回真实会受影响的条数，供确认框显示。"""
        body = body or {}
        add, ignored_add = clean_tags(body.get("add_tags"))
        rm, ignored_rm = clean_tags(body.get("remove_tags"))
        ignored = ignored_add + [t for t in ignored_rm if t not in ignored_add]
        con = self._conn()
        existing = self._existing(con, ids)
        affected = 0
        refs_affected = 0

        for chunk in _chunks(list(existing), 400):
            marks = _marks(len(chunk))
            if action == "delete":
                affected += con.execute(
                    f"SELECT COUNT(*) FROM blob WHERE id IN ({marks}) "
                    f"AND deleted_at IS NULL", chunk).fetchone()[0]
                refs_affected += con.execute(
                    f"SELECT COUNT(*) FROM ref WHERE id IN ({marks})", chunk).fetchone()[0]
            elif action == "restore" or action == "purge":
                affected += con.execute(
                    f"SELECT COUNT(*) FROM blob WHERE id IN ({marks}) "
                    f"AND deleted_at IS NOT NULL", chunk).fetchone()[0]
            elif action == "keep":
                affected += con.execute(
                    f"SELECT COUNT(*) FROM blob WHERE id IN ({marks}) "
                    f"AND origin='plugin' AND deleted_at IS NULL", chunk).fetchone()[0]
            elif action == "unkeep":
                affected += con.execute(
                    f"SELECT COUNT(*) FROM blob WHERE id IN ({marks}) "
                    f"AND origin='manual' AND deleted_at IS NULL", chunk).fetchone()[0]
            elif action in ("tag", "untag"):
                add_l = add if action == "tag" else []
                rm_l = rm
                hit: set[str] = set()
                if add_l:
                    rows = con.execute(
                        f"SELECT b.id FROM blob b WHERE b.id IN ({marks}) "
                        f"AND b.deleted_at IS NULL AND (SELECT COUNT(*) FROM tag t "
                        f"WHERE t.id=b.id AND t.tag IN ({_marks(len(add_l))})) < ?",
                        (*chunk, *add_l, len(add_l))).fetchall()
                    hit.update(r["id"] for r in rows)
                if rm_l:
                    rows = con.execute(
                        f"SELECT DISTINCT b.id FROM blob b JOIN tag t ON t.id=b.id "
                        f"WHERE b.id IN ({marks}) AND b.deleted_at IS NULL "
                        f"AND t.tag IN ({_marks(len(rm_l))})",
                        (*chunk, *rm_l)).fetchall()
                    hit.update(r["id"] for r in rows)
                affected += len(hit)

        return {"requested": len(ids), "affected": int(affected),
                "skipped": max(0, len(ids) - int(affected)),
                "refs_affected": int(refs_affected), "ignored_tags": ignored,
                "sample": ids[:10]}

    def batch(self, action: str, ids: list, body: dict | None = None) -> dict:
        """批量操作（单事务）。

        返回统一结构：``requested`` / ``affected``（真正改变的图片数）/
        ``changed_ids`` / ``skipped``（存在但无需变更） / ``skipped_ids`` /
        ``failed``（not_found / gone） / ``ignored_tags``（被拒绝的元数据式标签）。
        """
        body = body or {}
        if action not in ACTIONS:
            raise ValueError("bad_action")
        if len(ids) > MAX_IDS:
            raise ValueError("too_many")
        add, ignored_add = clean_tags(body.get("add_tags"))
        rm, ignored_rm = clean_tags(body.get("remove_tags"))
        ignored_tags = ignored_add + [t for t in ignored_rm if t not in ignored_add]

        with self._lock:
            con = self._conn()
            changed: list[str] = []
            skipped: list[str] = []
            failed: list[dict] = []
            with self._tx(con):
                existing = self._existing(con, ids)
                live = []
                for bid in ids:
                    if bid not in existing:
                        failed.append({"id": bid, "reason": "not_found"})
                    else:
                        live.append(bid)

                if action in ("tag", "untag"):
                    add_l = add if action == "tag" else []
                    rm_l = rm
                    active = [b for b in live if existing[b]["deleted_at"] is None]
                    hit: set[str] = set()
                    if add_l:
                        for chunk in _chunks(active, 300):
                            rows = con.execute(
                                f"SELECT id, tag FROM tag WHERE id IN ({_marks(len(chunk))}) "
                                f"AND tag IN ({_marks(len(add_l))})",
                                (*chunk, *add_l)).fetchall()
                            have = {(r["id"], r["tag"]) for r in rows}
                            pairs = [(b, t) for b in chunk for t in add_l
                                     if (b, t) not in have]
                            if pairs:
                                con.executemany(
                                    "INSERT OR IGNORE INTO tag(id,tag) VALUES(?,?)", pairs)
                                hit.update({b for b, _t in pairs})
                    if rm_l:
                        for chunk in _chunks(active, 300):
                            rows = con.execute(
                                f"SELECT DISTINCT id FROM tag WHERE id IN "
                                f"({_marks(len(chunk))}) AND tag IN ({_marks(len(rm_l))})",
                                (*chunk, *rm_l)).fetchall()
                            gone = {r["id"] for r in rows}
                            if gone:
                                con.execute(
                                    f"DELETE FROM tag WHERE id IN ({_marks(len(chunk))}) "
                                    f"AND tag IN ({_marks(len(rm_l))})",
                                    (*chunk, *rm_l))
                                hit.update(gone)
                    changed = [b for b in active if b in hit]
                    skipped = [b for b in live if b not in hit]
                elif action in ("keep", "unkeep"):
                    want, src = ("manual", "plugin") if action == "keep" else ("plugin", "manual")
                    flipped: list[str] = []
                    for chunk in _chunks(live, 400):
                        rows = con.execute(
                            f"SELECT id FROM blob WHERE id IN ({_marks(len(chunk))}) "
                            f"AND origin=? AND deleted_at IS NULL",
                            (*chunk, src)).fetchall()
                        flipped.extend(r["id"] for r in rows)
                    flip = set(flipped)
                    changed = [b for b in live if b in flip]
                    skipped = [b for b in live if b not in flip]
                    if changed:
                        con.executemany("UPDATE blob SET origin=? WHERE id=?",
                                        [(want, b) for b in changed])
                        for b in changed:
                            self._cache_drop(b)
                elif action == "delete":
                    ts = now_ms()
                    for chunk in _chunks(live, 300):
                        rows = con.execute(
                            f"SELECT id FROM blob WHERE id IN ({_marks(len(chunk))}) "
                            f"AND deleted_at IS NULL", chunk).fetchall()
                        for row in rows:
                            bid = row["id"]
                            self._log_delete(con, bid)
                            con.execute("UPDATE blob SET deleted_at=? WHERE id=?", (ts, bid))
                            self._cache_drop(bid)
                            changed.append(bid)
                    skipped = [b for b in live if b not in set(changed)]
                elif action == "restore":
                    gone_ids: set[str] = set()
                    for chunk in _chunks(live, 300):
                        rows = con.execute(
                            f"SELECT id FROM blob WHERE id IN ({_marks(len(chunk))}) "
                            f"AND deleted_at IS NOT NULL", chunk).fetchall()
                        for row in rows:
                            bid = row["id"]
                            path = self.blob_path(bid, existing[bid]["format"])
                            if not os.path.exists(path):
                                failed.append({"id": bid, "reason": "gone"})
                                gone_ids.add(bid)
                                continue
                            con.execute("UPDATE blob SET deleted_at=NULL WHERE id=?", (bid,))
                            self._log(con, "restore_blob", bid, None)
                            self._cache_drop(bid)
                            changed.append(bid)
                    skipped = [b for b in live
                               if b not in set(changed) and b not in gone_ids]
                elif action == "purge":
                    for chunk in _chunks(live, 200):
                        rows = con.execute(
                            f"SELECT id FROM blob WHERE id IN ({_marks(len(chunk))}) "
                            f"AND deleted_at IS NOT NULL", chunk).fetchall()
                        for row in rows:
                            self._hard_delete(con, row["id"], reason="manual")
                            changed.append(row["id"])
                    skipped = [b for b in live if b not in set(changed)]

            order = {b: i for i, b in enumerate(ids)}
            changed = sorted(set(changed), key=lambda b: order.get(b, 1 << 30))
            skipped = sorted(set(skipped), key=lambda b: order.get(b, 1 << 30))
            return {"ok": True, "action": action, "requested": len(ids),
                    "affected": len(changed), "changed_ids": changed,
                    "skipped": len(skipped), "skipped_ids": skipped,
                    "failed": failed, "ignored_tags": ignored_tags,
                    "cursor": self.change_head()}
