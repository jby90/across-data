"""把原文字节解析成按页的文本。不做 OCR:没有文字层的 PDF 交给后续 OCR 步骤。"""

from __future__ import annotations

import io
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


def parse_document(content: bytes, mime: str) -> ParsedDocument:
    kind = detect_kind(content, mime)
    if kind == "pdf":
        return ParsedDocument(kind, parse_pdf(content))
    if kind == "html":
        return ParsedDocument(kind, parse_html(content))
    return ParsedDocument(kind, [_decode(content)])
