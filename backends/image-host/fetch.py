"""安全抓取：scheme / IP 校验、逐跳重定向、流式落盘、magic bytes 判定。

设计要点（docs/16 §5.10、§16.5）：

- `requests` 的 `allow_redirects=True` 不给逐跳校验的机会，``302 -> 127.0.0.1``
  可直接绕过 IP 拦截，因此这里必须手动循环并在**每一跳**重做全部校验。
- 流式落盘：内存占用与图片大小无关；超限立即中断并清理临时文件。
- magic bytes 判定格式，不信任响应头或客户端声明。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import tempfile
from urllib.parse import urljoin, urlparse

import requests

MAX_REDIRECTS = 3
CHUNK_SIZE = 64 * 1024

# (magic, format, mime)
MAGIC_FORMATS = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"BM", "bmp", "image/bmp"),
)

ALLOWED_FORMATS = ("png", "jpg", "jpeg", "gif", "webp", "bmp")


class FetchError(Exception):
    """抓取失败；kind 与 §16.9 的错误码一一对应。"""

    def __init__(self, kind: str, *, status: int = 0, reason: str = "",
                 size: int = 0, detail: str = ""):
        super().__init__(reason or detail or kind)
        self.kind = kind          # bad_url | unsupported_format | too_large | fetch_failed
        self.status = status      # 上游 HTTP 状态（fetch_failed 时透出给插件）
        self.reason = reason
        self.size = size
        self.detail = detail


def detect_format(head: bytes):
    """返回 (format, mime)；不认识则 (None, None)。"""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp", "image/webp"
    for magic, fmt, mime in MAGIC_FORMATS:
        if head.startswith(magic):
            return fmt, mime
    return None, None


def _is_blocked(ip) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _resolve_all(host: str, port: int):
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return out


def safe_fetch(url: str, *, max_bytes: int, timeout, tmp_dir: str,
               allow_private: bool = False):
    """下载到临时文件，返回 (tmp_path, sha256_hex, size, format, mime)。

    :raises FetchError: 分类见 ``FetchError.kind``
    """
    current = url

    for _hop in range(MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if parsed.scheme not in ("http", "https"):
            raise FetchError("bad_url", reason="scheme")

        host = parsed.hostname
        if not host:
            raise FetchError("bad_url", reason="no_host")

        if not allow_private:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            try:
                ips = _resolve_all(host, port)
            except OSError as exc:
                raise FetchError("bad_url", reason="dns", detail=str(exc))
            if not ips:
                raise FetchError("bad_url", reason="dns")
            for ip in ips:
                if _is_blocked(ip):
                    raise FetchError("bad_url", reason="private_ip", detail=str(ip))

        try:
            resp = requests.get(current, stream=True, timeout=timeout,
                                allow_redirects=False)
        except requests.RequestException as exc:
            raise FetchError("fetch_failed", status=0, detail=str(exc))

        # 手动跳转：下一轮会重做上面的全部校验
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            resp.close()
            if not location:
                raise FetchError("bad_url", reason="bad_redirect")
            current = urljoin(current, location)
            continue

        if resp.status_code >= 400:
            status = resp.status_code
            resp.close()
            raise FetchError("fetch_failed", status=status)

        fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, suffix=".part")
        digest = hashlib.sha256()
        total = 0
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in resp.iter_content(CHUNK_SIZE):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError("too_large", size=total)
                    digest.update(chunk)
                    out.write(chunk)
        except FetchError:
            _safe_unlink(tmp_path)
            raise
        except Exception as exc:  # 连接中断 / 读超时
            _safe_unlink(tmp_path)
            raise FetchError("fetch_failed", status=0, detail=str(exc))
        finally:
            resp.close()

        with open(tmp_path, "rb") as fh:
            head = fh.read(16)
        fmt, mime = detect_format(head)
        if not fmt:
            _safe_unlink(tmp_path)
            raise FetchError("unsupported_format")

        return tmp_path, digest.hexdigest(), total, fmt, mime

    raise FetchError("bad_url", reason="too_many_redirects")


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
