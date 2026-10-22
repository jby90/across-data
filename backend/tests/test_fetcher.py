import httpx
import pytest

from app.ingestion.fetcher import (
    BlockedError,
    FetchError,
    PoliteFetcher,
    is_blocked_host,
    validate_public_http_url,
)

from .helpers import make_fetcher, public_resolver


def site_handler(routes: dict[str, httpx.Response], robots: str | int | None = "") -> callable:
    """robots: 字符串=返回 200 与该内容;整数=返回该状态码;None=网络错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            if robots is None:
                raise httpx.ConnectError("boom")
            if isinstance(robots, int):
                return httpx.Response(robots)
            return httpx.Response(200, text=robots)
        if path in routes:
            return routes[path]
        return httpx.Response(404)

    return handler


async def test_fetch_ok_returns_content_and_headers():
    routes = {
        "/a.pdf": httpx.Response(
            200, content=b"hello", headers={"content-type": "application/pdf", "etag": '"v1"'}
        )
    }
    f = make_fetcher(site_handler(routes))
    res = await f.get("https://example.org/a.pdf")
    assert res.content == b"hello" and res.mime == "application/pdf" and res.etag == '"v1"'


async def test_robots_disallow_blocks():
    routes = {
        "/private/x.pdf": httpx.Response(200, content=b"x"),
        "/ok.pdf": httpx.Response(200, content=b"y"),
    }
    f = make_fetcher(site_handler(routes, robots="User-agent: *\nDisallow: /private/"))
    with pytest.raises(BlockedError):
        await f.get("https://example.org/private/x.pdf")
    assert (await f.get("https://example.org/ok.pdf")).content == b"y"


async def test_robots_rule_for_our_bot_token():
    routes = {"/a.pdf": httpx.Response(200, content=b"x")}
    f = make_fetcher(site_handler(routes, robots="User-agent: CheyanShuheBot\nDisallow: /"))
    with pytest.raises(BlockedError):
        await f.get("https://example.org/a.pdf")


async def test_robots_404_means_allowed():
    routes = {"/a.pdf": httpx.Response(200, content=b"x")}
    f = make_fetcher(site_handler(routes, robots=404))
    assert (await f.get("https://example.org/a.pdf")).content == b"x"


@pytest.mark.parametrize("robots", [500, None])
async def test_robots_5xx_or_network_error_blocks_conservatively(robots):
    routes = {"/a.pdf": httpx.Response(200, content=b"x")}
    f = make_fetcher(site_handler(routes, robots=robots))
    with pytest.raises(BlockedError):
        await f.get("https://example.org/a.pdf")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.pdf",
        "http://localhost/a.pdf",
        "http://10.0.0.5/a.pdf",
        "http://192.168.1.1/a.pdf",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/a.pdf",
        "http://printer.local/a.pdf",
        "file:///etc/passwd",
        "ftp://example.org/a.pdf",
    ],
)
async def test_ssrf_targets_blocked(url):
    f = make_fetcher(site_handler({}))
    with pytest.raises(BlockedError):
        await f.get(url)


async def test_hostname_resolving_to_private_ip_blocked():
    async def rebinding_resolver(host: str) -> list[str]:
        return ["10.1.2.3"]

    client = httpx.AsyncClient(transport=httpx.MockTransport(site_handler({})))
    f = PoliteFetcher(
        client, user_agent="CheyanShuheBot/0.1", delay_seconds=0, resolver=rebinding_resolver
    )
    with pytest.raises(BlockedError):
        await f.get("https://evil.example.org/a.pdf")


async def test_redirect_to_internal_address_refused():
    routes = {"/a.pdf": httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})}
    f = make_fetcher(site_handler(routes))
    with pytest.raises(BlockedError):
        await f.get("https://example.org/a.pdf")


async def test_redirect_followed_with_relative_location():
    routes = {
        "/old.pdf": httpx.Response(301, headers={"location": "/new.pdf"}),
        "/new.pdf": httpx.Response(200, content=b"moved"),
    }
    f = make_fetcher(site_handler(routes))
    res = await f.get("https://example.org/old.pdf")
    assert res.content == b"moved" and res.url == "https://example.org/new.pdf"


async def test_redirect_loop_gives_up():
    routes = {"/a": httpx.Response(302, headers={"location": "/a"})}
    f = make_fetcher(site_handler(routes), max_redirects=3)
    with pytest.raises(FetchError):
        await f.get("https://example.org/a")


async def test_size_limit_by_content_length_header():
    routes = {"/big.pdf": httpx.Response(200, content=b"x" * 100)}
    f = make_fetcher(site_handler(routes), max_bytes=50)
    with pytest.raises(FetchError):
        await f.get("https://example.org/big.pdf")


async def test_size_limit_enforced_while_streaming_without_length():
    async def body():
        for _ in range(10):
            yield b"x" * 10

    routes = {"/big.pdf": httpx.Response(200, content=body())}
    f = make_fetcher(site_handler(routes), max_bytes=50)
    with pytest.raises(FetchError):
        await f.get("https://example.org/big.pdf")


async def test_http_error_status_raises():
    f = make_fetcher(site_handler({}))
    with pytest.raises(FetchError, match="404"):
        await f.get("https://example.org/missing.pdf")


async def test_conditional_request_and_304():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen["inm"] = request.headers.get("if-none-match")
        seen["ims"] = request.headers.get("if-modified-since")
        return httpx.Response(304)

    f = make_fetcher(handler)
    res = await f.get(
        "https://example.org/a.pdf", etag='"v1"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT"
    )
    assert res.not_modified and res.content is None
    assert seen == {"inm": '"v1"', "ims": "Wed, 21 Oct 2026 07:28:00 GMT"}


async def test_per_host_throttle_waits_between_requests():
    waits: list[float] = []
    now = [100.0]

    async def fake_sleep(s: float) -> None:
        waits.append(s)
        now[0] += s

    routes = {"/a": httpx.Response(200, content=b"1"), "/b": httpx.Response(200, content=b"2")}
    client = httpx.AsyncClient(transport=httpx.MockTransport(site_handler(routes)))
    f = PoliteFetcher(
        client,
        user_agent="CheyanShuheBot/0.1",
        delay_seconds=2.0,
        resolver=public_resolver,
        sleep=fake_sleep,
        clock=lambda: now[0],
    )
    await f.get("https://example.org/a")
    await f.get("https://example.org/b")
    assert waits and all(w > 0 for w in waits) and max(waits) <= 2.0


def test_is_blocked_host_literals():
    assert is_blocked_host("localhost") and is_blocked_host("127.0.0.1")
    assert is_blocked_host("fd00::1") and is_blocked_host("service.internal")
    assert not is_blocked_host("example.org") and not is_blocked_host("93.184.216.34")


def test_validate_public_http_url():
    assert validate_public_http_url("https://example.org/x") == "https://example.org/x"
    for bad in [
        "ftp://example.org",
        "http://localhost/x",
        "javascript:alert(1)",
        "https:///x",
        "http://10.0.0.1",
    ]:
        with pytest.raises(ValueError):
            validate_public_http_url(bad)


async def test_allow_resolved_cidrs_only_relaxes_the_listed_range():
    routes = {"/a": httpx.Response(200, content=b"ok")}

    def build(resolved_ip: str, allow: list[str]) -> PoliteFetcher:
        async def resolver(host: str) -> list[str]:
            return [resolved_ip]

        client = httpx.AsyncClient(transport=httpx.MockTransport(site_handler(routes)))
        return PoliteFetcher(
            client,
            user_agent="CheyanShuheBot/0.1",
            delay_seconds=0,
            resolver=resolver,
            allow_resolved_cidrs=allow,
        )

    # 默认:fake-IP 网段被拦
    with pytest.raises(BlockedError):
        await build("198.18.0.152", []).get("https://example.org/a")
    # 显式放行该网段:通过
    assert (
        await build("198.18.0.152", ["198.18.0.0/15"]).get("https://example.org/a")
    ).content == b"ok"
    # 放行 198.18/15 不会顺带放行别的内网网段(含云元数据地址)
    for ip in ["10.0.0.5", "192.168.1.1", "127.0.0.1", "169.254.169.254"]:
        with pytest.raises(BlockedError):
            await build(ip, ["198.18.0.0/15"]).get("https://example.org/a")


async def test_allow_resolved_cidrs_never_allows_literal_internal_urls():
    client = httpx.AsyncClient(transport=httpx.MockTransport(site_handler({})))
    f = PoliteFetcher(
        client,
        user_agent="CheyanShuheBot/0.1",
        delay_seconds=0,
        resolver=public_resolver,
        allow_resolved_cidrs=["127.0.0.0/8", "198.18.0.0/15"],
    )
    for url in ["http://127.0.0.1/a", "http://localhost/a", "http://198.18.0.5/a"]:
        with pytest.raises(BlockedError):
            await f.get(url)
