"""礼貌抓取:遵守 robots.txt、限速、体积上限、防 SSRF、手动处理重定向。"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

Resolver = Callable[[str], Awaitable[list[str]]]


class BlockedError(Exception):
    """被策略拦截(robots、内网地址、协议不允许),不是网络故障,不应盲目重试。"""


class FetchError(Exception):
    """网络或 HTTP 错误。"""


@dataclass(frozen=True)
class FetchResult:
    url: str  # 最终 URL(跟随重定向之后)
    status: int
    content: bytes | None  # 304 时为 None
    mime: str
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def is_blocked_host(host: str) -> bool:
    """字面量判断:localhost、内网/回环/链路本地 IP、常见内部域名后缀。"""
    h = host.lower().rstrip(".").strip("[]")
    if h == "localhost" or h.endswith((".localhost", ".local", ".internal", ".lan", ".intranet")):
        return True
    try:
        return _ip_blocked(ipaddress.ip_address(h))
    except ValueError:
        return False


async def default_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def validate_public_http_url(url: str) -> str:
    """校验配置里的 URL:只允许 http/https 且主机不是内网字面量。返回规范化后的 URL。"""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("只允许 http/https 地址")
    if is_blocked_host(parts.hostname):
        raise ValueError("不允许内网或本机地址")
    return parts.geturl()


class PoliteFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        user_agent: str,
        delay_seconds: float = 2.0,
        max_bytes: int = 100 * 1024 * 1024,
        max_redirects: int = 5,
        resolver: Resolver = default_resolver,
        allow_resolved_cidrs: list[str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ua = user_agent
        self._ua_token = user_agent.split("/")[0].strip() or "*"
        self._delay = delay_seconds
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver
        # 仅放行「域名解析结果」落在这些网段的情况(如开发机代理软件的 fake-IP 198.18.0.0/15)。
        # 字面量内网地址、localhost 永远不放行;生产环境应保持为空。
        self._allowed_nets = [
            ipaddress.ip_network(c, strict=False) for c in (allow_resolved_cidrs or [])
        ]
        self._sleep = sleep
        self._clock = clock
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}  # None = 一律不允许

    async def _check_target(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BlockedError(f"协议或地址不被允许: {url}")
        host = parts.hostname
        if is_blocked_host(host):
            raise BlockedError(f"内网或本机地址被拦截: {host}")
        try:
            addresses = await self._resolver(host)
        except OSError as e:
            raise FetchError(f"域名解析失败: {host}: {e}") from e
        for addr in addresses:
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if _ip_blocked(ip) and not any(ip in net for net in self._allowed_nets):
                raise BlockedError(f"域名解析到内网地址被拦截: {host} -> {addr}")
        return host

    async def _throttle(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self._delay - (self._clock() - last)
            if wait > 0:
                await self._sleep(wait)
        self._last_hit[host] = self._clock()

    async def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        robots_url = f"{origin}/robots.txt"
        parser: RobotFileParser | None
        try:
            await self._throttle(parts.hostname or "")
            resp = await self._client.get(
                robots_url, headers={"User-Agent": self._ua}, follow_redirects=False, timeout=15
            )
            if resp.status_code == 200:
                parser = RobotFileParser()
                parser.parse(resp.text.splitlines())
            elif 400 <= resp.status_code < 500:
                parser = RobotFileParser()  # 没有 robots.txt:视为允许
                parser.parse([])
            else:
                parser = None  # 5xx:保守起见一律不抓
        except httpx.HTTPError:
            parser = None
        self._robots[origin] = parser
        return parser

    async def _robots_allowed(self, url: str) -> bool:
        parser = await self._robots_for(url)
        return parser is not None and parser.can_fetch(self._ua_token, url)

    async def get(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        current = url
        for _ in range(self._max_redirects + 1):
            host = await self._check_target(current)
            if not await self._robots_allowed(current):
                raise BlockedError(f"robots.txt 不允许抓取或无法确认: {current}")
            await self._throttle(host)

            headers = {"User-Agent": self._ua, "Accept": "*/*"}
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            try:
                async with self._client.stream(
                    "GET", current, headers=headers, follow_redirects=False
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError(f"重定向缺少 Location: {current}")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code == 304:
                        return FetchResult(
                            current, 304, None, "", etag, last_modified, not_modified=True
                        )
                    if resp.status_code >= 400:
                        raise FetchError(f"HTTP {resp.status_code}: {current}")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > self._max_bytes:
                        raise FetchError(f"文件过大({declared} 字节): {current}")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_bytes:
                            raise FetchError(f"文件超过大小上限: {current}")
                        chunks.append(chunk)
                    mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    return FetchResult(
                        current,
                        resp.status_code,
                        b"".join(chunks),
                        mime,
                        resp.headers.get("etag"),
                        resp.headers.get("last-modified"),
                    )
            except httpx.HTTPError as e:
                raise FetchError(f"网络错误: {current}: {e}") from e
        raise FetchError(f"重定向次数过多: {url}")
