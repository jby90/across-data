"""把原文字节解析成按页的文本。不做 OCR:没有文字层的 PDF 交给后续 OCR 步骤。"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass

from bs4 import BeautifulSoup
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer
from pdfminer.pdfparser import PDFSyntaxError

# 平均每页字符数低于此值,判定为扫描件(没有文字层)
NEEDS_OCR_MIN_CHARS_PER_PAGE = 30


class ParseError(Exception):
    pass


@dataclass
class ParsedDocument:
    kind: str  # pdf / html / text
    pages: list[str]

    @property
    def char_count(self) -> int:
        return sum(len(p.strip()) for p in self.pages)

    @property
    def looks_scanned(self) -> bool:
        if self.kind != "pdf" or not self.pages:
            return False
        return self.char_count / len(self.pages) < NEEDS_OCR_MIN_CHARS_PER_PAGE


def detect_kind(content: bytes, mime: str) -> str:
    head = content[:1024].lstrip()
    if head.startswith(b"%PDF"):
        return "pdf"
    lowered = head[:200].lower()
    if mime in ("text/html", "application/xhtml+xml") or lowered.startswith(
        (b"<!doctype html", b"<html")
    ):
        return "html"
    if mime.startswith("text/"):
        return "text"
    raise ParseError(f"暂不支持的文件类型: {mime or '未知'}")


def _decode(content: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ParseError("无法识别文本编码(需要 UTF-8)")


def parse_pdf(content: bytes) -> list[str]:
    pages: list[str] = []
    try:
        for layout in extract_pages(io.BytesIO(content)):
            parts = [el.get_text() for el in layout if isinstance(el, LTTextContainer)]
            pages.append("".join(parts))
    except (PDFSyntaxError, ValueError, KeyError, AttributeError) as e:
        raise ParseError(f"PDF 解析失败: {e}") from e
    return pages


def parse_html(content: bytes) -> list[str]:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    lines = [" ".join(line.split()) for line in soup.get_text("\n").splitlines()]
    return ["\n".join(line for line in lines if line)]


_DIGITS = re.compile(r"[0-9٠-٩]+")
_EDGE_LINES = 2  # 只看每页开头/结尾各 2 行
_MAX_FURNITURE_LEN = 120


def _norm_furniture(line: str) -> str:
    return _DIGITS.sub("#", " ".join(line.split())).strip()


def strip_running_headers(pages: list[str]) -> tuple[list[str], list[str]]:
    """去掉 PDF 里每页重复出现的页眉/页脚/页码(例如"Page 3 of 20"、文件名)。

    保守规则:至少 3 页;只看每页开头/结尾各 2 行;数字归一后,在不少于一半的页面上重复出现才算;
    长度不超过 120 字符。返回 (处理后的页面, 被移除的行模式),被移除内容会写进处理事件,便于核对。
    原文件始终保留在归档中,这里只影响条款文本。
    """
    if len(pages) < 3:
        return pages, []
    split_pages = [p.splitlines() for p in pages]
    counts: Counter[str] = Counter()
    for lines in split_pages:
        nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
        edge = nonblank[:_EDGE_LINES] + nonblank[-_EDGE_LINES:]
        seen = {
            _norm_furniture(lines[i])
            for i in set(edge)
            if len(lines[i].strip()) <= _MAX_FURNITURE_LEN
        }
        counts.update(s for s in seen if s)
    threshold = max(3, (len(pages) + 1) // 2)
    furniture = {s for s, c in counts.items() if c >= threshold}
    if not furniture:
        return pages, []

    cleaned: list[str] = []
    for lines in split_pages:
        nonblank = [i for i, ln in enumerate(lines) if ln.strip()]
        edge = set(nonblank[:_EDGE_LINES] + nonblank[-_EDGE_LINES:])
        keep = [
            ln for i, ln in enumerate(lines) if not (i in edge and _norm_furniture(ln) in furniture)
        ]
        cleaned.append("\n".join(keep))
    return cleaned, sorted(furniture)


def parse_document(content: bytes, mime: str) -> ParsedDocument:
    kind = detect_kind(content, mime)
    if kind == "pdf":
        return ParsedDocument(kind, parse_pdf(content))
    if kind == "html":
        return ParsedDocument(kind, parse_html(content))
    return ParsedDocument(kind, [_decode(content)])
