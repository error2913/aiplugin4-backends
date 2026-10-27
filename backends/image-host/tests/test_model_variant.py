"""模型变体规则单测（纯逻辑，不依赖 Flask / SQLite）。

覆盖：格式归一、max clamp、尺寸读取、是否变体、缩放输出格式/尺寸/alpha、
GIF 首帧策略（静态/动图/透明、GIF_MODE=original 回退）。

运行：
  python -m unittest discover -s backends/image-host/tests -p "test_model_variant.py" -v
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

from PIL import Image  # noqa: E402

import model_variant as mv  # noqa: E402


def make_image(fmt="PNG", size=(100, 100), mode="RGB", color=None, **kwargs):
    color = color if color is not None else ((10, 20, 30, 255) if "A" in mode
                                             else (10, 20, 30))
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
    """上半透明、下半棋盘格实色的 GIF（P 模式 + transparency）。"""
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


class TestFormatAndClamp(unittest.TestCase):
    def test_canonical_format(self):
        self.assertEqual(mv.canonical_format("jpg"), "jpeg")
        self.assertEqual(mv.canonical_format("JPE"), "jpeg")
        self.assertEqual(mv.canonical_format("PNG"), "png")
        self.assertEqual(mv.canonical_format(None), "")
        self.assertEqual(mv.mime_of("jpg"), "image/jpeg")
        self.assertEqual(mv.mime_of("webp"), "image/webp")
        self.assertEqual(mv.mime_of("bmp"), "image/bmp")

    def test_clamp_max(self):
        self.assertEqual(mv.clamp_max(None), 2048)
        self.assertEqual(mv.clamp_max("1024"), 1024)
        self.assertEqual(mv.clamp_max(100), 512)            # 下限
        self.assertEqual(mv.clamp_max(99999), 4096)         # 上限
        self.assertEqual(mv.clamp_max("abc"), 2048)         # 非法
        self.assertEqual(mv.clamp_max(None, 1536), 1536)    # 自定义默认
        self.assertEqual(mv.clamp_max(0, 1024), 1024)       # 0 视为缺省

    def test_scaled_size(self):
        self.assertEqual(mv.scaled_size(4000, 1000, 2048), (2048, 512))
        self.assertEqual(mv.scaled_size(100, 50, 2048), (100, 50))
        self.assertEqual(mv.scaled_size(1000, 4000, 2048), (512, 2048))


class TestImageSize(unittest.TestCase):
    def test_bytes_and_path(self):
        data = make_image(size=(320, 240))
        self.assertEqual(mv.image_size(data), (320, 240))
        root = tempfile.mkdtemp(prefix="ih-mv-")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "a.png")
        with open(path, "wb") as fh:
            fh.write(data)
        self.assertEqual(mv.image_size(path), (320, 240))

    def test_unknown(self):
        self.assertEqual(mv.image_size(b"not an image"), (None, None))
        self.assertEqual(mv.image_size(None), (None, None))

    def test_has_alpha(self):
        self.assertTrue(mv.has_alpha(open_image(make_image("PNG", (5, 5), "RGBA"))))
        self.assertFalse(mv.has_alpha(open_image(make_image("PNG", (5, 5), "RGB"))))
        # 调色板 + transparency 也算 alpha
        buffer = io.BytesIO()
        Image.new("P", (5, 5)).save(buffer, "PNG", transparency=0)
        self.assertTrue(mv.has_alpha(open_image(buffer.getvalue())))

    def test_target_format(self):
        self.assertEqual(mv.target_format("png", True), "webp")
        self.assertEqual(mv.target_format("png", False), "jpeg")
        self.assertEqual(mv.target_format("jpg", False), "jpeg")
        self.assertEqual(mv.target_format("bmp", True), "webp")


class TestDecide(unittest.TestCase):
    def test_small_supported_is_passthrough(self):
        for fmt in ("png", "jpeg", "jpg", "webp", "gif"):
            plan = mv.decide(fmt, 800, 600, 2048)
            self.assertEqual(plan["mode"], mv.MODE_PASSTHROUGH, fmt)

    def test_oversized_is_variant(self):
        for fmt in ("png", "jpeg", "webp"):
            plan = mv.decide(fmt, 3000, 1000, 2048)
            self.assertEqual(plan["mode"], mv.MODE_VARIANT, fmt)
            self.assertEqual(plan["max_side"], 2048)

    def test_unsupported_format_small_still_variant(self):
        # bmp 不在模型白名单，即使尺寸很小也要转码
        self.assertEqual(mv.decide("bmp", 10, 10, 2048)["mode"], mv.MODE_VARIANT)
        self.assertEqual(mv.decide("", 10, 10, 2048)["mode"], mv.MODE_VARIANT)

    def test_gif_over_limit_is_first_frame_variant(self):
        plan = mv.decide("gif", 3000, 100, 2048)
        self.assertEqual(plan["mode"], mv.MODE_VARIANT)
        self.assertTrue(plan["first_frame"])
        self.assertEqual(plan["gif_mode"], mv.GIF_MODE_FIRST_FRAME)
        # 源 fmt 仍是 gif；变体实际输出 webp/jpeg 由 render 决定
        self.assertEqual(plan["fmt"], "gif")

    def test_gif_at_limit_is_passthrough(self):
        self.assertEqual(mv.decide("gif", 2048, 100, 2048)["mode"],
                         mv.MODE_PASSTHROUGH)
        self.assertEqual(mv.decide("gif", 2049, 100, 2048)["mode"],
                         mv.MODE_VARIANT)

    def test_gif_original_mode_keeps_legacy_unsupported(self):
        with gif_mode_env(mv.GIF_MODE_ORIGINAL):
            plan = mv.decide("gif", 3000, 100, 2048)
            self.assertEqual(plan["mode"], mv.MODE_UNSUPPORTED)
            self.assertEqual(plan["reason"], "gif_not_resizable")
            self.assertEqual(plan["gif_mode"], mv.GIF_MODE_ORIGINAL)
            # 极限内仍是 passthrough
            self.assertEqual(mv.decide("gif", 100, 100, 2048)["mode"],
                             mv.MODE_PASSTHROUGH)

    def test_unknown_size_is_variant_not_passthrough(self):
        self.assertEqual(mv.decide("png", None, None, 2048)["mode"],
                         mv.MODE_VARIANT)

    def test_max_is_clamped(self):
        self.assertEqual(mv.decide("png", 400, 400, 100)["max_side"], 512)
        self.assertEqual(mv.decide("png", 400, 400, 100)["mode"],
                         mv.MODE_PASSTHROUGH)
        self.assertEqual(mv.decide("png", 5000, 5000, 99999)["max_side"], 4096)

    def test_gif_unknown_size_is_variant_by_default(self):
        self.assertEqual(mv.decide("gif", None, None, 2048)["mode"],
                         mv.MODE_VARIANT)
        with gif_mode_env(mv.GIF_MODE_ORIGINAL):
            plan = mv.decide("gif", None, None, 2048)
            self.assertEqual(plan["mode"], mv.MODE_UNSUPPORTED)
            self.assertEqual(plan["reason"], "unknown_size")


class TestGifMode(unittest.TestCase):
    def test_default_is_first_frame(self):
        with gif_mode_env(None):
            self.assertEqual(mv.gif_mode(), mv.GIF_MODE_FIRST_FRAME)
            self.assertTrue(mv.gif_first_frame_enabled())

    def test_env_values(self):
        for value in ("original", "ORIGINAL", " orig ", "legacy"):
            with gif_mode_env(value):
                self.assertEqual(mv.gif_mode(), mv.GIF_MODE_ORIGINAL, value)
                self.assertFalse(mv.gif_first_frame_enabled())
        for value in ("first_frame", "first-frame", "FIRST_FRAME", "bogus", ""):
            with gif_mode_env(value):
                self.assertEqual(mv.gif_mode(), mv.GIF_MODE_FIRST_FRAME, value)
                self.assertTrue(mv.gif_first_frame_enabled())


class TestRender(unittest.TestCase):
    def test_scaled_rgb_to_jpeg(self):
        data = make_image("JPEG", (4000, 1000))
        out = mv.render(data, 2048)
        self.assertEqual(out["fmt"], "jpeg")
        self.assertEqual(out["mime"], "image/jpeg")
        self.assertEqual((out["w"], out["h"]), (2048, 512))
        self.assertTrue(out["scaled"])
        image = open_image(out["bytes"])
        self.assertEqual(image.size, (2048, 512))
        self.assertEqual(image.format, "JPEG")

    def test_alpha_png_to_webp_keeps_alpha(self):
        data = make_image("PNG", (3000, 3000), "RGBA", (1, 2, 3, 128))
        out = mv.render(data, 1024)
        self.assertEqual(out["fmt"], "webp")
        self.assertEqual((out["w"], out["h"]), (1024, 1024))
        image = open_image(out["bytes"])
        self.assertEqual(image.size, (1024, 1024))
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((10, 10))[3], 128)

    def test_small_image_only_converts(self):
        # bmp 小图：不缩放，只转码（无 alpha → jpeg）
        data = make_image("BMP", (120, 80))
        out = mv.render(data, 2048)
        self.assertEqual(out["fmt"], "jpeg")
        self.assertEqual((out["w"], out["h"]), (120, 80))
        self.assertFalse(out["scaled"])

    def test_gif_first_frame_no_alpha_to_jpeg(self):
        data = make_animated_gif((3000, 100))
        out = mv.render(data, 1024)
        self.assertEqual(out["fmt"], "jpeg")
        self.assertEqual(out["mime"], "image/jpeg")
        self.assertEqual((out["w"], out["h"]), (1024, 34))
        self.assertTrue(out["scaled"])
        self.assertTrue(out["first_frame"])
        self.assertEqual(out["gif_mode"], mv.GIF_MODE_FIRST_FRAME)
        image = open_image(out["bytes"])
        self.assertEqual(image.format, "JPEG")
        self.assertEqual(image.size, (1024, 34))

    def test_animated_gif_uses_first_frame(self):
        # 三帧颜色不同，输出应取第 0 帧（JPEG 有损，允许少量误差）
        data = make_animated_gif((600, 200))
        out = mv.render(data, 600)
        self.assertFalse(out["scaled"])
        pixel = open_image(out["bytes"]).getpixel((10, 10))
        for got, want in zip(pixel, (10, 20, 30)):
            self.assertLessEqual(abs(got - want), 25, (pixel, out["fmt"]))

    def test_small_gif_first_frame_converts_without_scaling(self):
        data = make_image("GIF", (120, 80))
        out = mv.render(data, 2048)
        self.assertEqual(out["fmt"], "jpeg")
        self.assertEqual((out["w"], out["h"]), (120, 80))
        self.assertFalse(out["scaled"])
        self.assertTrue(out["first_frame"])

    def test_gif_alpha_first_frame_to_webp(self):
        data = make_transparent_gif((3000, 100))
        out = mv.render(data, 1024)
        self.assertEqual(out["fmt"], "webp")
        self.assertEqual(out["mime"], "image/webp")
        self.assertEqual((out["w"], out["h"]), (1024, 34))
        self.assertTrue(out["first_frame"])
        image = open_image(out["bytes"])
        self.assertEqual(image.mode, "RGBA")
        # 首帧上半部透明（webp 的 alpha 通道无损）
        self.assertEqual(image.getpixel((10, 5))[3], 0)

    def test_gif_original_mode_refused(self):
        data = make_image("GIF", (100, 100))
        with gif_mode_env(mv.GIF_MODE_ORIGINAL):
            with self.assertRaises(mv.VariantError):
                mv.render(data, 50)

    def test_corrupt_refused(self):
        with self.assertRaises(mv.VariantError):
            mv.render(b"broken", 2048)


if __name__ == "__main__":
    unittest.main(verbosity=2)
