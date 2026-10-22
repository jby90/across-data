"""通用条款切分。

支持:英文 Section/Article/Regulation/Rule N 与 1.1 多级编号、法文 Article N / premier、
中文 第N条、阿拉伯文 المادة N。原文逐字保留,不改写;识别不出编号时退化为按段落切分,
并在 strategy 里如实标明,供后续核对。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ARABIC_DIGITS = "٠-٩"
_CN_NUM = "一二三四五六七八九十百千零〇两"

# (名称, 正则, 是否关键词式标题——此类行剩余部分可作为小标题)
_HEADER_PATTERNS: list[tuple[str, re.Pattern[str], bool]] = [
    (
        "keyword",
        re.compile(
            r"^(?:Section|Article|Art\.|Regulation|Rule|Clause)\s+"
            r"(?P<num>\d+[A-Za-z]?(?:\.\d+)*|premier|1er)\b[\s.:—–-]*(?P<rest>.*)$",
            re.IGNORECASE,
        ),
        True,
    ),
    (
        "chinese",
        re.compile(rf"^第(?P<num>[{_CN_NUM}\d]+)条[\s、.:：]*(?P<rest>.*)$"),
        True,
    ),
    (
        "arabic",
        re.compile(
            rf"^(?:المادة|مادة)\s+(?P<num>[\d{_ARABIC_DIGITS}]+|\S+)[\s:：.\-–—]*(?P<rest>.*)$"
        ),
        True,
    ),
    (
        "decimal",
        re.compile(r"^(?P<num>\d{1,3}(?:\.\d{1,3})+)[.)]?\s+(?P<rest>\S.*)$"),
        False,
    ),
    (
        "simple",
        re.compile(r"^(?P<num>\d{1,3})[.)]\s+(?P<rest>[A-Z“\"(].*)$"),
        False,
    ),
]

_MAX_HEADING_LEN = 120


@dataclass
class ClauseDraft:
    ordinal: int
    clause_number: str | None
    heading: str | None
    text: str
    page_start: int
    page_end: int


def match_header(line: str) -> tuple[str, str | None] | None:
    """返回 (编号, 小标题) 或 None。"""
    stripped = line.strip()
    for _name, pattern, keyword_style in _HEADER_PATTERNS:
        m = pattern.match(stripped)
        if not m:
            continue
        number = m.group("num")
        rest = m.group("rest").strip()
        heading = rest if keyword_style and rest and len(rest) <= _MAX_HEADING_LEN else None
        return number, heading
    return None


def _build(drafts_raw: list[dict], strategy: str) -> tuple[list[ClauseDraft], str]:
    drafts: list[ClauseDraft] = []
    for raw in drafts_raw:
        text = "\n".join(raw["lines"]).strip()
        if not text:
            continue
        drafts.append(
            ClauseDraft(
                ordinal=len(drafts),
                clause_number=raw["number"],
                heading=raw["heading"],
                text=text,
                page_start=raw["page_start"],
                page_end=raw["page_end"],
            )
        )
    return drafts, strategy


def split_clauses(
    pages: list[str], *, paragraph_mode: str = "blank"
) -> tuple[list[ClauseDraft], str]:
    """返回 (条款列表, 策略名)。strategy: numbered / paragraph / line / empty。

    paragraph_mode: 退化为段落切分时的依据,blank=空行(PDF/文本),line=每行(HTML)。
    """
    raws: list[dict] = []
    current: dict | None = None
    headers = 0

    for page_no, page in enumerate(pages, start=1):
        for line in page.splitlines():
            if not line.strip():
                if current is not None:
                    current["lines"].append("")
                continue
            header = match_header(line)
            if header is not None:
                headers += 1
                current = {
                    "number": header[0],
                    "heading": header[1],
                    "lines": [line.rstrip()],
                    "page_start": page_no,
                    "page_end": page_no,
                }
                raws.append(current)
                continue
            if current is None:
                current = {
                    "number": None,
                    "heading": None,
                    "lines": [],
                    "page_start": page_no,
                    "page_end": page_no,
                }
                raws.append(current)
            current["lines"].append(line.rstrip())
            current["page_end"] = page_no

    if headers >= 2:
        return _build(raws, "numbered")

    # 退化:按段落切分
    paras: list[dict] = []
    block: dict | None = None
    for page_no, page in enumerate(pages, start=1):
        for line in page.splitlines():
            if not line.strip():
                if paragraph_mode == "blank":
                    block = None
                continue
            if block is None or paragraph_mode == "line":
                block = {
                    "number": None,
                    "heading": None,
                    "lines": [],
                    "page_start": page_no,
                    "page_end": page_no,
                }
                paras.append(block)
            block["lines"].append(line.rstrip())
            block["page_end"] = page_no
    drafts, _ = _build(paras, "paragraph")
    if not drafts:
        return [], "empty"
    return drafts, "paragraph" if paragraph_mode == "blank" else "line"
