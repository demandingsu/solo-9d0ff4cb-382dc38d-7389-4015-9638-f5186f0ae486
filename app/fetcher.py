"""HTTP 抓取层：带超时与有限重试。"""
import logging
import time

import httpx

from . import config

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """页面抓取失败（网络错误或非 2xx 响应）。"""


class DownloadTooLargeError(FetchError):
    """附件体积超过配置上限。"""


def fetch(url: str) -> str:
    """抓取页面文本。对网络错误和 5xx 做有限次重试，4xx 立即失败。"""
    last_error: Exception | None = None
    for attempt in range(config.FETCH_RETRIES + 1):
        try:
            resp = httpx.get(url, timeout=config.FETCH_TIMEOUT, follow_redirects=True)
            if resp.status_code >= 500:
                raise FetchError(f"服务端错误 HTTP {resp.status_code}")
            if resp.status_code != 200:
                # 404 等客户端错误不重试
                raise FetchError(f"HTTP {resp.status_code}")
            resp.encoding = resp.charset_encoding or "utf-8"
            return resp.text
        except (httpx.HTTPError, FetchError) as exc:
            last_error = exc
            retryable = isinstance(exc, httpx.HTTPError) or (
                isinstance(exc, FetchError) and "5" in str(exc)
            )
            if not retryable or attempt == config.FETCH_RETRIES:
                break
            sleep_for = config.RETRY_BACKOFF * (attempt + 1)
            logger.warning("抓取 %s 第 %d 次失败：%s，%.1fs 后重试",
                           url, attempt + 1, exc, sleep_for)
            time.sleep(sleep_for)

    raise FetchError(f"抓取 {url} 失败：{last_error}")


def fetch_bytes(url: str, max_bytes: int | None = None) -> bytes:
    """下载二进制内容（附件）。

    对网络错误与 5xx 做有限次重试，4xx 立即失败；流式读取并在超过
    ``max_bytes`` 时中止，抛出 :class:`DownloadTooLargeError`。
    """
    limit = config.ATTACHMENT_MAX_BYTES if max_bytes is None else max_bytes
    last_error: Exception | None = None
    for attempt in range(config.FETCH_RETRIES + 1):
        try:
            with httpx.stream(
                "GET", url, timeout=config.FETCH_TIMEOUT, follow_redirects=True
            ) as resp:
                if resp.status_code >= 500:
                    raise FetchError(f"服务端错误 HTTP {resp.status_code}")
                if resp.status_code != 200:
                    raise FetchError(f"HTTP {resp.status_code}")
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise DownloadTooLargeError(
                            f"附件 {url} 超过体积上限 {limit} 字节"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except (httpx.HTTPError, FetchError) as exc:
            last_error = exc
            retryable = isinstance(exc, httpx.HTTPError) or (
                isinstance(exc, FetchError) and "5" in str(exc)
            )
            if not retryable or attempt == config.FETCH_RETRIES:
                break
            sleep_for = config.RETRY_BACKOFF * (attempt + 1)
            logger.warning("下载 %s 第 %d 次失败：%s，%.1fs 后重试",
                           url, attempt + 1, exc, sleep_for)
            time.sleep(sleep_for)

    raise FetchError(f"下载 {url} 失败：{last_error}")
