"""从目录页发现原文链接。"""

from __future__ import annotations

from dataclasses import dataclass
from posixpath import splitext
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str


def normalise_url(url: str) -> str | None:
    """去掉片段,只保留 http/https。"""
    url, _ = urldefrag(url.strip())
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return url


def extension_of(url: str) -> str:
    return splitext(urlsplit(url).path)[1].lower()


def _matches(url: str, title: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = f"{unquote(url)} {title}".lower()
    return any(k.lower() in haystack for k in keywords)


def extract_links(
    html: bytes | str,
    base_url: str,
    *,
    selector: str = "a[href]",
    extensions: list[str] | None = None,
    keywords: list[str] | None = None,
) -> list[Candidate]:
    exts = [e.lower() for e in (extensions or [".pdf"])]
    kws = keywords or []
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Candidate] = {}
    for a in soup.select(selector):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        url = normalise_url(urljoin(base_url, href))
        if url is None or extension_of(url) not in exts:
            continue
        title = " ".join(a.get_text(" ", strip=True).split())
        if not _matches(url, title, kws):
            continue
        found.setdefault(url, Candidate(url=url, title=title[:500]))
    return list(found.values())


def next_page_url(html: bytes | str, base_url: str, selector: str | None) -> str | None:
    if not selector:
        return None
    soup = BeautifulSoup(html, "html.parser")
    a = soup.select_one(selector)
    href = a.get("href") if a else None
    if not isinstance(href, str):
        return None
    return normalise_url(urljoin(base_url, href))
