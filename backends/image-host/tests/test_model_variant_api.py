"""image-host 1.0.16 端点测试：/m、/m/b64、/api/meta、health、入库尺寸与变体预热。

用 Flask ``test_client`` 跑（依赖 flask；纯逻辑测试见 test_model_variant.py）。
GIF 相关用例覆盖 D5 首帧策略与 ``AIPLUGIN4_IMAGE_HOST_GIF_MODE=original`` 回退。

运行：
  python -m unittest discover -s backends/image-host/tests -p "test_model_variant_api.py" -v
"""

import base64
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

TMP_ROOT = tempfile.mkdtemp(prefix="ih-model-api-")
os.environ["AIPLUGIN4_IMAGE_HOST_DIR"] = TMP_ROOT
os.environ["AIPLUGIN4_BACKEND_TOKEN"] = "test-token"
os.environ["AIPLUGIN4_IMAGE_HOST_WEBUI"] = "off"
os.environ["AIPLUGIN4_IMAGE_HOST_MODEL_MAX"] = "2048"

from PIL import Image  # noqa: E402

import imagehost  # noqa: E402
import model_variant as mv  # noqa: E402
from store import Store  # noqa: E402

HEADERS = {"Authorization": "Bearer test-token"}
MODEL_MAX = 2048


def make_image(fmt="PNG", size=(100, 100), mode="RGB", seed=0, **kwargs):
    color = ((seed % 200) + 1, 20, 30) if "A" not in mode else \
        ((seed % 200) + 1, 20, 30, 128)
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def make_animated_gif(size=(3000, 100), frames=3):
    images = [Image.new("RGB", size, (10 + i * 90, 20, 30)) for i in range(frames)]
    buffer = io.BytesIO()
    images[0].save(buffer, "GIF", save_all=True, append_images=images[1:],
                   duration=100, loop=0)
    return buffer.getvalue()


def make_transparent_gif(size=(3000, 100)):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(size[1] // 2, size[1]):
        for x in range(0, size[0], 2):
            im.putpixel((x, y), (255, 0, 0, 255))
    buffer = io.BytesIO()
    im.save(buffer, "GIF")
    return buffer.getvalue()


@contextlib.contextmanager
def gif_mode_env(value):
    """临时设置 ``AIPLUGIN4_IMAGE_HOST_GIF_MODE``；value=None 表示删除（用默认）。"""
    old = os.environ.get(mv.GIF_MODE_ENV)
    if value is None:
        os.environ.pop(mv.GIF_MODE_ENV, None)
    else:
        os.environ[mv.GIF_MODE_ENV] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(mv.GIF_MODE_ENV, None)
        else:
            os.environ[mv.GIF_MODE_ENV] = old


def open_image(data):
    return Image.open(io.BytesIO(data))


def _close_store(store):
    try:
        con = getattr(store._local, "con", None)
        if con is not None:
            con.close()
            store._local.con = None
    except Exception:
        pass
    while True:
        try:
            store._pool.get_nowait().close()
        except Exception:
            break


def setUpModule():
    # 与其它 Flask 用例隔离：本模块独占一份数据目录
    imagehost.store = Store(TMP_ROOT, cap_bytes=64 * 1024 * 1024, thumb_max=512,
                            trash_days=7, change_keep_days=30)
    imagehost.store.init()
    imagehost.MODEL_MAX_SIDE = MODEL_MAX
    imagehost.TOKEN = "test-token"


def tearDownModule():
    _close_store(imagehost.store)
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    for key in ("AIPLUGIN4_IMAGE_HOST_DIR", "AIPLUGIN4_BACKEND_TOKEN",
                "AIPLUGIN4_IMAGE_HOST_WEBUI", "AIPLUGIN4_IMAGE_HOST_MODEL_MAX",
                mv.GIF_MODE_ENV):
        os.environ.pop(key, None)


class ApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = imagehost.app.test_client()

    def upload(self, data, ref, **extra):
        body = {"base64": base64.b64encode(data).decode("ascii"), "ref": ref}
        body.update(extra)
        res = self.client.post("/api/upload", json=body, headers=HEADERS)
        self.assertEqual(res.status_code, 200, res.data)
        return res.get_json()

    def upload_multipart(self, data, ref, filename="a.png"):
        form = {"file": (io.BytesIO(data), filename), "ref": ref}
        res = self.client.post("/api/upload", data=form,
                               content_type="multipart/form-data", headers=HEADERS)
        self.assertEqual(res.status_code, 200, res.data)
        return res.get_json()

    def delete(self, bid):
        res = self.client.post("/api/batch", json={"action": "delete", "ids": [bid]},
                               headers=HEADERS)
        self.assertEqual(res.status_code, 200, res.data)
        return res.get_json()


class TestHealth(ApiBase):
    def test_capabilities_and_existing_fields(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["capabilities"],
                         ["model-variant", "meta", "variant-max",
                          "gif-first-frame"])
        self.assertEqual(body["gif_mode"], mv.GIF_MODE_FIRST_FRAME)
        self.assertEqual(body["model_max_side"], MODEL_MAX)
        self.assertEqual(body["version"], "1.0.16")
        for key in ("ok", "image_host", "cap_mb", "used_mb", "count", "trash",
                    "changes_head", "webui"):
            self.assertIn(key, body)

    def test_original_gif_mode_drops_capability(self):
        with gif_mode_env(mv.GIF_MODE_ORIGINAL):
            body = self.client.get("/api/health").get_json()
        self.assertEqual(body["capabilities"],
                         ["model-variant", "meta", "variant-max"])
        self.assertEqual(body["gif_mode"], mv.GIF_MODE_ORIGINAL)


class TestUploadSize(ApiBase):
    def test_upload_stores_size_and_prefetches_default_variant(self):
        data = make_image("PNG", (3000, 1000), seed=1)
        item = self.upload(data, "api-size-1")
        bid = item["id"]
        self.assertEqual((item["w"], item["h"]), (3000, 1000))
        self.assertEqual(item["model_url"], f"/m/{bid}")
        row = imagehost.store.lookup(bid)
        self.assertEqual((row["w"], row["h"]), (3000, 1000))
        variant = imagehost.store.variant_lookup(bid, MODEL_MAX)
        self.assertIsNotNone(variant)
        self.assertTrue(os.path.exists(variant["path"]))
        self.assertEqual(item["model_format"], variant["fmt"])
        self.assertIn(MODEL_MAX, imagehost.store.variant_max(bid))

    def test_multipart_upload_stores_size(self):
        data = make_image("PNG", (2000, 1000), seed=2)
        item = self.upload_multipart(data, "api-size-multipart")
        self.assertEqual((item["w"], item["h"]), (2000, 1000))
        row = imagehost.store.lookup(item["id"])
        self.assertEqual((row["w"], row["h"]), (2000, 1000))

    def test_small_image_has_no_prefetched_variant(self):
        data = make_image("PNG", (100, 50), seed=3)
        item = self.upload(data, "api-size-small")
        self.assertIsNone(imagehost.store.variant_lookup(item["id"], MODEL_MAX))

    def test_gif_upload_prefetches_first_frame_variant(self):
        # D5：first_frame 下超限 gif 也预热首帧变体
        data = make_image("GIF", (3000, 100), seed=40)
        item = self.upload(data, "api-size-gif")
        bid = item["id"]
        self.assertEqual((item["w"], item["h"]), (3000, 100))
        variant = imagehost.store.variant_lookup(bid, MODEL_MAX)
        self.assertIsNotNone(variant)
        self.assertTrue(os.path.exists(variant["path"]))
        self.assertIn(variant["fmt"], ("jpeg", "webp"))
        self.assertEqual(item["model_format"], variant["fmt"])
        self.assertIn(MODEL_MAX, imagehost.store.variant_max(bid))

    def test_gif_upload_skips_prefetch_in_original_mode(self):
        data = make_image("GIF", (3000, 100), seed=41)
        with gif_mode_env(mv.GIF_MODE_ORIGINAL):
            item = self.upload(data, "api-size-gif-orig")
            self.assertIsNone(
                imagehost.store.variant_lookup(item["id"], MODEL_MAX))

    def test_small_gif_has_no_prefetched_variant(self):
        data = make_image("GIF", (100, 50), seed=42)
        item = self.upload(data, "api-size-gif-small")
        self.assertIsNone(imagehost.store.variant_lookup(item["id"], MODEL_MAX))


class TestModelEndpoint(ApiBase):
    def test_small_original_is_passthrough(self):
        data = make_image("PNG", (10, 10), seed=10)
        item = self.upload(data, "api-m-pass")
        res = self.client.get(f"/m/{item['id']}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, data)
        self.assertEqual(res.headers["X-Model-Variant"], "0")
        self.assertEqual(res.headers["X-Image-Size"], "10x10")
        self.assertTrue(res.content_type.startswith("image/png"))
        self.assertIn("immutable", res.headers["Cache-Control"])

    def test_oversized_image_scaled_and_cached(self):
        data = make_image("PNG", (3000, 1000), seed=11)
        item = self.upload(data, "api-m-big")
        bid = item["id"]
        res = self.client.get(f"/m/{bid}", query_string={"max": "1024"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Model-Variant"], "1")
        self.assertTrue(res.content_type.startswith("image/jpeg"))
        image = open_image(res.data)
        self.assertEqual(max(image.size), 1024)
        self.assertEqual(res.headers["X-Image-Size"],
                         f"{image.size[0]}x{image.size[1]}")
        # 命中缓存：第二次逐字节一致，且未重复入库
        res2 = self.client.get(f"/m/{bid}", query_string={"max": "1024"})
        self.assertEqual(res2.data, res.data)
        self.assertEqual(res2.headers["X-Model-Variant"], "1")
        self.assertIn(1024, imagehost.store.variant_max(bid))
        # 默认目标（入库预热）也还在
        self.assertIn(MODEL_MAX, imagehost.store.variant_max(bid))

    def test_default_max_uses_prefetched_variant(self):
        data = make_image("PNG", (3000, 1000), seed=12)
        item = self.upload(data, "api-m-default")
        before = imagehost.store.stats()["variant_count"]
        res = self.client.get(f"/m/{item['id']}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Model-Variant"], "1")
        self.assertEqual(imagehost.store.stats()["variant_count"], before)

    def test_max_is_clamped(self):
        data = make_image("PNG", (3000, 1000), seed=13)
        item = self.upload(data, "api-m-clamp")
        # 下限 512
        res = self.client.get(f"/m/{item['id']}", query_string={"max": "100"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(max(open_image(res.data).size), 512)
        # 上限 4096：3000 已在范围内 → 退回原字节
        res = self.client.get(f"/m/{item['id']}", query_string={"max": "99999"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Model-Variant"], "0")
        self.assertEqual(res.data, data)

    def test_large_gif_returns_first_frame_variant(self):
        # D5：gif 超限不再 400，而是首帧变体（无 alpha → jpeg）
        data = make_image("GIF", (3000, 100), seed=14)
        item = self.upload(data, "api-m-gif-big")
        bid = item["id"]
        res = self.client.get(f"/m/{bid}")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.headers["X-Model-Variant"], "1")
        self.assertTrue(res.content_type.startswith("image/jpeg"))
        image = open_image(res.data)
        self.assertEqual(max(image.size), MODEL_MAX)
        self.assertEqual(res.headers["X-Image-Size"],
                         f"{image.size[0]}x{image.size[1]}")
        # 原图出口与文件不受影响（QQ 仍发原图）
        raw = self.client.get(f"/i/{bid}")
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.data, data)

    def test_animated_gif_first_frame_variant(self):
        data = make_animated_gif((3000, 100))
        item = self.upload(data, "api-m-gif-anim")
        res = self.client.get(f"/m/{item['id']}")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.headers["X-Model-Variant"], "1")
        image = open_image(res.data)
        self.assertEqual(max(image.size), MODEL_MAX)
        # 首帧颜色（JPEG 有损，允许少量误差）
        pixel = image.getpixel((10, 10))
        for got, want in zip(pixel, (10, 20, 30)):
            self.assertLessEqual(abs(got - want), 25, pixel)

    def test_transparent_gif_first_frame_variant_is_webp(self):
        data = make_transparent_gif((3000, 100))
        item = self.upload(data, "api-m-gif-alpha")
        res = self.client.get(f"/m/{item['id']}")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.headers["X-Model-Variant"], "1")
        self.assertTrue(res.content_type.startswith("image/webp"))
        image = open_image(res.data)
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((10, 5))[3], 0)

    def test_large_gif_is_unsupported_in_original_mode(self):
        data = make_image("GIF", (3000, 100), seed=23)
        with gif_mode_env(mv.GIF_MODE_ORIGINAL):
            item = self.upload(data, "api-m-gif-orig")
            for url in (f"/m/{item['id']}", f"/m/{item['id']}/b64"):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 400, (url, res.data))
                body = res.get_json()
                self.assertEqual(body["error"], "unsupported_for_model")
                self.assertEqual(body["format"], "gif")
                self.assertEqual(body["reason"], "gif_not_resizable")

    def test_small_gif_is_passthrough(self):
        data = make_image("GIF", (100, 80), seed=15)
        item = self.upload(data, "api-m-gif-small")
        res = self.client.get(f"/m/{item['id']}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Model-Variant"], "0")
        self.assertEqual(res.data, data)
        self.assertTrue(res.content_type.startswith("image/gif"))

    def test_bmp_is_converted(self):
        data = make_image("BMP", (120, 80), seed=16)
        item = self.upload(data, "api-m-bmp")
        res = self.client.get(f"/m/{item['id']}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Model-Variant"], "1")
        self.assertTrue(res.content_type.startswith("image/jpeg"))
        self.assertEqual(open_image(res.data).size, (120, 80))

    def test_unknown_and_trashed_are_404(self):
        self.assertEqual(self.client.get(f"/m/{'c' * 64}").status_code, 404)
        self.assertEqual(self.client.get("/m/not-a-valid-id").status_code, 404)
        data = make_image("PNG", (20, 20), seed=17)
        item = self.upload(data, "api-m-trash")
        self.assertEqual(self.client.get(f"/m/{item['id']}").status_code, 200)
        self.delete(item["id"])
        self.assertEqual(self.client.get(f"/m/{item['id']}").status_code, 404)
        self.assertEqual(self.client.get(f"/m/{item['id']}/b64").status_code, 404)

    def test_legacy_row_size_backfilled_on_request(self):
        data = make_image("PNG", (100, 50), seed=18)
        item = self.upload(data, "api-m-backfill")
        bid = item["id"]
        with imagehost.store._lock:
            imagehost.store._conn().execute(
                "UPDATE blob SET w=NULL,h=NULL WHERE id=?", (bid,))
        imagehost.store._cache_drop(bid)
        res = self.client.get(f"/m/{bid}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["X-Image-Size"], "100x50")
        row = imagehost.store.lookup(bid)
        self.assertEqual((row["w"], row["h"]), (100, 50))


class TestModelB64(ApiBase):
    def test_passthrough_b64(self):
        data = make_image("PNG", (10, 10), seed=20)
        item = self.upload(data, "api-b64-pass")
        res = self.client.get(f"/m/{item['id']}/b64")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(base64.b64decode(body["base64"]), data)
        self.assertEqual(body["format"], "png")
        self.assertEqual(body["mime"], "image/png")
        self.assertEqual((body["w"], body["h"]), (10, 10))
        self.assertEqual(body["id"], item["id"])
        self.assertFalse(body["variant"])

    def test_variant_b64(self):
        data = make_image("PNG", (3000, 1000), seed=21)
        item = self.upload(data, "api-b64-big")
        res = self.client.get(f"/m/{item['id']}/b64",
                              query_string={"max": "1024"})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["variant"])
        self.assertEqual(body["format"], "jpeg")
        self.assertEqual(body["mime"], "image/jpeg")
        image = open_image(base64.b64decode(body["base64"]))
        self.assertEqual(max(image.size), 1024)
        self.assertEqual((body["w"], body["h"]), image.size)

    def test_gif_b64_first_frame(self):
        # D5：/m/b64 对 gif 返回首帧变体的 format/mime/w/h
        data = make_image("GIF", (3000, 100), seed=22)
        item = self.upload(data, "api-b64-gif")
        res = self.client.get(f"/m/{item['id']}/b64")
        self.assertEqual(res.status_code, 200, res.data)
        body = res.get_json()
        self.assertTrue(body["variant"])
        self.assertEqual(body["format"], "jpeg")
        self.assertEqual(body["mime"], "image/jpeg")
        image = open_image(base64.b64decode(body["base64"]))
        self.assertEqual(max(image.size), MODEL_MAX)
        self.assertEqual((body["w"], body["h"]), image.size)


class TestMeta(ApiBase):
    def test_requires_token(self):
        res = self.client.get("/api/meta", query_string={"ids": "a" * 64})
        self.assertEqual(res.status_code, 401)

    def test_meta_reports_size_and_variants(self):
        data = make_image("PNG", (3000, 1000), seed=30)
        item = self.upload(data, "api-meta-1")
        bid = item["id"]
        missing = "d" * 64
        res = self.client.get("/api/meta",
                              query_string={"ids": f"{bid},{missing}"},
                              headers=HEADERS)
        self.assertEqual(res.status_code, 200, res.data)
        body = res.get_json()
        self.assertEqual(len(body), 2)
        first = body[0]
        self.assertEqual(first["id"], bid)
        self.assertTrue(first["found"])
        self.assertFalse(first["deleted"])
        self.assertEqual((first["w"], first["h"]), (3000, 1000))
        self.assertEqual(first["size"], len(data))
        self.assertEqual(first["format"], "png")
        self.assertEqual(first["mime"], "image/png")
        self.assertIn(MODEL_MAX, first["variant_max"])
        second = body[1]
        self.assertFalse(second["found"])
        self.assertFalse(second["deleted"])

        self.delete(bid)
        body = self.client.get("/api/meta", query_string={"ids": bid},
                               headers=HEADERS).get_json()
        self.assertTrue(body[0]["deleted"])

    def test_meta_rejects_empty_and_too_many(self):
        self.assertEqual(self.client.get("/api/meta", headers=HEADERS).status_code, 400)
        many = ",".join(f"{i:064x}" for i in range(201))
        res = self.client.get("/api/meta", query_string={"ids": many},
                              headers=HEADERS)
        self.assertEqual(res.status_code, 413)


if __name__ == "__main__":
    unittest.main(verbosity=2)
