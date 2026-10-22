"""从目录页发现原文链接,以及新加坡电子宪报(eGazette)公开检索发现。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from posixpath import splitext
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

# 新加坡电子宪报默认检索词(覆盖个人数据保护、智能网联汽车及配套司法/治理修订法案)
DEFAULT_EGAZETTE_QUERIES: tuple[str, ...] = (
    '"Personal Data Protection"',
    '"Road Traffic (Autonomous Motor Vehicles)"',
    '"Courts (Civil and Criminal Justice) Reform Act 2021"',
    '"Statutes (Miscellaneous Amendments) Act 2025"',
    '"Public Sector (Governance) (Amendment) Act 2026"',
)

_EGAZETTE_CATEGORIES = frozenset(
    {
        "Acts Supplement",
        "Revised Acts",
        "Subsidiary Legislation Supplement",
    }
)
# 从电子宪报目录页脚本中提取公开检索配置(绝不硬编码密钥)
_EGAZETTE_CONFIG_RE = re.compile(r'\\"config\\":(\{\\"type\\":\\"egazette-algolia\\"[^}]+\})')
_EGAZETTE_CONFIG_PLAIN_RE = re.compile(r'"config":(\{"type":"egazette-algolia"[^}]+\})')


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    document_type: str | None = None
    published_date: date | None = None
    official_number: str | None = None
    instrument_family: str | None = None
    instrument_kind: str | None = None


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


def parse_egazette_search_config(html: bytes | str) -> tuple[str, str, str]:
    """从新加坡电子宪报目录页解析公开检索配置。

    返回 (app_id, index_name, search_api_key)。配置由官方页面下发,代码不硬编码密钥。"""
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    raw_cfg: str | None = None
    m = _EGAZETTE_CONFIG_RE.search(text)
    if m:
        raw_cfg = m.group(1).replace('\\"', '"')
    else:
        m2 = _EGAZETTE_CONFIG_PLAIN_RE.search(text)
        if m2:
            raw_cfg = m2.group(1)
    if not raw_cfg:
        raise ValueError("电子宪报页面未找到公开检索配置(egazette-algolia)")
    cfg = json.loads(raw_cfg)
    app_id = str(cfg.get("appId") or "").strip()
    index_name = str(cfg.get("indexName") or "").strip()
    api_key = str(cfg.get("searchApiKey") or "").strip()
    if not app_id or not index_name or not api_key or not app_id.isalnum():
        raise ValueError("电子宪报公开检索配置不完整")
    return app_id, index_name, api_key


def _classify_egazette_hit(title: str, sub_category: str) -> tuple[str, str, str]:
    """返回 (document_type, instrument_family, instrument_kind)。绝不把法案草案当成生效法律。"""
    doc_type = "LAW" if sub_category in ("Acts Supplement", "Revised Acts") else "REGULATION"
    if "Road Traffic (Autonomous Motor Vehicles)" in title:
        family = "sg-av-rules"
    elif "Notification of Data Breaches" in title:
        family = "sg-data-breach"
    elif "(Enforcement)" in title:
        family = "sg-pdpa-enforcement"
    elif "Personal Data Protection" in title and "Regulations" in title:
        family = "sg-pdpr"
    else:
        family = "sg-pdpa"
    if "Commencement" in title:
        kind = "COMMENCEMENT"
    elif "Amendment" in title or "Reform Act" in title:
        kind = "AMENDMENT"
    elif sub_category == "Revised Acts":
        kind = "REVISED_ACT"
    else:
        kind = "BASE"
    return doc_type, family, kind


def parse_egazette_hits(payload: bytes | str) -> list[Candidate]:
    """解析电子宪报单次检索响应 JSON,只保留正式立法增刊与自动驾驶/个人数据相关文件。"""
    data = json.loads(payload)
    hits = data.get("hits")
    if not isinstance(hits, list):
        return []
    found: dict[str, Candidate] = {}
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "").strip()
        sub_category = str(hit.get("subCategory") or "").strip()
        file_url = normalise_url(str(hit.get("fileUrl") or ""))
        if not title or not file_url or sub_category not in _EGAZETTE_CATEGORIES:
            continue
        parts = urlsplit(file_url)
        if parts.hostname != "assets.egazette.gov.sg" or not parts.path.lower().endswith(".pdf"):
            continue
        pub_date: date | None = None
        raw_date = str(hit.get("publishDate") or "").strip()
        if raw_date:
            try:
                pub_date = datetime.strptime(raw_date, "%d/%m/%Y").date()
            except ValueError:
                pub_date = None
        notif = str(hit.get("notificationNum") or "").strip() or None
        doc_type, family, kind = _classify_egazette_hit(title, sub_category)
        found.setdefault(
            file_url,
            Candidate(
                url=file_url,
                title=title[:500],
                document_type=doc_type,
                published_date=pub_date,
                official_number=notif,
                instrument_family=family,
                instrument_kind=kind,
            ),
        )
    return list(found.values())
