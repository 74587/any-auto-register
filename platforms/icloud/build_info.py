"""iCloud 前端构建号探测。

Apple Web 服务会校验请求里的 clientBuildNumber / clientMasteringNumber。
构建号会随 iCloud 发布变化，因此从官方页面读取并按区域缓存，失败时回退到常量。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urlparse

from .constants import (
    FALLBACK_CLOUD_BUILD,
    FALLBACK_CLOUD_MASTERING,
    FALLBACK_MAIL_BUILD,
    FALLBACK_MAIL_MASTERING,
    endpoints_for,
    normalize_region,
)

CACHE_TTL_SECONDS = 300
MAX_PAGE_BYTES = 1 << 20
MAIL_BUILD_PAGE_PATH = "/applications/mail2/current/en-us/index.html?rootDomain=www"

_BUILD_ATTR = "data-cw-private-build-number"
_MASTERING_ATTR = "data-cw-private-mastering-number"


@dataclass(frozen=True)
class BuildInfo:
    cloud_build: str = FALLBACK_CLOUD_BUILD
    cloud_mastering: str = FALLBACK_CLOUD_MASTERING
    mail_build: str = FALLBACK_MAIL_BUILD
    mail_mastering: str = FALLBACK_MAIL_MASTERING


class _AppBuildParser(HTMLParser):
    """抓取第一个同时带有构建号与母版号属性的元素。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.build = ""
        self.mastering = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if self.build and self.mastering:
            return
        values = {name.lower(): (value or "").strip() for name, value in attrs}
        build = values.get(_BUILD_ATTR, "")
        mastering = values.get(_MASTERING_ATTR, "")
        if build and mastering:
            self.build, self.mastering = build, mastering


def parse_app_build(html: str) -> tuple[str, str]:
    parser = _AppBuildParser()
    parser.feed(html)
    parser.close()
    if not parser.build or not parser.mastering:
        raise ValueError("iCloud 页面缺少构建号属性")
    return parser.build, parser.mastering


def is_official_service_url(value: str) -> bool:
    host = urlparse(str(value or "").strip()).hostname or ""
    host = host.lower()
    return host in {"icloud.com", "icloud.com.cn"} or host.endswith((".icloud.com", ".icloud.com.cn"))


class BuildInfoCache:
    """按区域缓存构建号，避免每次请求都抓取 iCloud 页面。"""

    def __init__(self, ttl_seconds: float = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[BuildInfo, float]] = {}

    def get(self, http, region: str | None) -> BuildInfo:
        region = normalize_region(region)
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(region)
            if cached and now < cached[1]:
                return cached[0]

        info = self._discover(http, region)
        if info is not None:
            with self._lock:
                self._entries[region] = (info, time.monotonic() + self._ttl)
            return info

        with self._lock:
            cached = self._entries.get(region)
        return cached[0] if cached else BuildInfo()

    def _discover(self, http, region: str) -> Optional[BuildInfo]:
        origin = endpoints_for(region).origin
        cloud = self._fetch(http, origin + "/")
        mail = self._fetch(http, origin + MAIL_BUILD_PAGE_PATH)
        if cloud is None or mail is None:
            return None
        return BuildInfo(
            cloud_build=cloud[0],
            cloud_mastering=cloud[1],
            mail_build=mail[0],
            mail_mastering=mail[1],
        )

    @staticmethod
    def _fetch(http, url: str) -> Optional[tuple[str, str]]:
        try:
            response = http.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
            if not response.ok:
                return None
            return parse_app_build(response.text[:MAX_PAGE_BYTES])
        except Exception:
            return None
