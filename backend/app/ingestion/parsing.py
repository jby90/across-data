"""把原文字节解析成按页的文本。不做 OCR:没有文字层的 PDF 交给 OCR 步骤。

PDF 使用 PDFium(pypdfium2)提取文字层。实测对比:pdfminer 在版面复杂的法条页(如新加坡 2012 年版 PDPA)
会把句子顺序弄乱,法律文本不能接受;PDFium 保持内容流顺序,且快得多。
"""

from __future__ import annotations

import re
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

import pypdfium2 as pdfium
from bs4 import BeautifulSoup

from app.ingestion.source_layout import (
    is_english_page,
    is_reference_page,
    ltr_page,
    resolve_unmapped_hyphens,
)

# 平均每页字符数低于此值,判定为扫描件(没有文字层)
NEEDS_OCR_MIN_CHARS_PER_PAGE = 30

# PDFium 不是线程安全的,所有调用串行化
_PDFIUM_LOCK = threading.Lock()


class ParseError(Exception):
    pass


@dataclass
class ParsedDocument:
    kind: str  # pdf / html / text
    pages: list[str]
    layout_events: list[dict] = field(default_factory=list)

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


def _normalise_pdf_text(text: str) -> str:
    """统一换行,去掉空行(PDFium 在部分文件里每行后会多出空行,空行不可靠)。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines if line.strip())


def parse_pdf(content: bytes, *, layout_events: list[dict] | None = None) -> list[str]:
    try:
        with _PDFIUM_LOCK:
            doc = pdfium.PdfDocument(content)
            try:
                pages = []
                for i in range(len(doc)):
                    page = doc[i]
                    text_page = page.get_textpage()
                    try:
                        text = _normalise_pdf_text(text_page.get_text_range())
                        # Restrict geometric ordering to English bibliography pages;
                        # Arabic/mixed text and normative multi-column pages stay untouched.
                        if text.count("://https") >= 3 and is_english_page(text):
                            try:
                                layout = ltr_page(text_page)
                                if is_reference_page(layout["text"]):
                                    layout = resolve_unmapped_hyphens(page, text_page, layout)
                                    text = layout["text"]
                                    if layout_events is not None:
                                        layout_events.append({"page_number": i + 1, **layout})
                            except ValueError as exc:
                                raise ParseError(f"参考文献页 {i + 1} 版面核验失败: {exc}") from exc
                        pages.append(text)
                    finally:
                        text_page.close()
                        page.close()
                return pages
            finally:
                doc.close()
    except pdfium.PdfiumError as e:
        raise ParseError(f"PDF 解析失败: {e}") from e


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
    # 奇偶页页眉不同的文件里,每种页眉只出现在约一半的页面上,所以阈值取 40%
    threshold = max(3, int(len(pages) * 0.4))
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


_MARGIN_NO = re.compile(r"^(?:(?P<body>.*\S)\s+)?(?P<n>\d{1,2})$")  # 行尾数字,或单独成行的数字


def _longest_margin_chain(hits: list[tuple[int, int]]) -> list[tuple[int, int]]:
    best: list[tuple[int, int]] = []
    for start in range(len(hits)):
        chain = [hits[start]]
        for idx, n in hits[start + 1 :]:
            prev_idx, prev_n = chain[-1]
            if n > prev_n and abs((idx - prev_idx) - (n - prev_n)) <= 2:
                chain.append((idx, n))
        if len(chain) > len(best):
            best = chain
    return best


def strip_margin_line_numbers(pages: list[str]) -> tuple[list[str], int]:
    """去掉法条页边的行号(新加坡早期版式每 5 行在行尾带一个 5、10、15...)。

    保守规则(防止误删正文里的 "section 5" 之类):候选行以 5 的倍数结尾;必须形成至少 3 个的"链":
    后一个数字比前一个大,且行号之间的行距与数字差基本一致(误差不超过 2 行)。
    只清除链上的行号。返回 (处理后的页面, 被清除的行号个数)。
    """
    cleaned: list[str] = []
    removed = 0
    for page in pages:
        lines = page.splitlines()
        hits: list[tuple[int, int]] = []  # (行下标, 行号)
        for i, line in enumerate(lines):
            m = _MARGIN_NO.match(line)
            if m and int(m.group("n")) % 5 == 0 and int(m.group("n")) <= 60:
                hits.append((i, int(m.group("n"))))
        chain = _longest_margin_chain(hits)
        if len(chain) >= 3:
            drop: set[int] = set()
            for i, _ in chain:
                m = _MARGIN_NO.match(lines[i])
                assert m is not None
                if m.group("body"):
                    lines[i] = m.group("body")
                else:
                    drop.add(i)  # 整行只有行号:删除该行
            removed += len(chain)
            cleaned.append("\n".join(ln for i, ln in enumerate(lines) if i not in drop))
        else:
            cleaned.append(page)
    return cleaned, removed


_STOPWORDS = {
    "en": {
        "the",
        "of",
        "and",
        "to",
        "in",
        "a",
        "is",
        "be",
        "that",
        "or",
        "for",
        "by",
        "any",
        "this",
    },
    "fr": {
        "de",
        "la",
        "le",
        "et",
        "les",
        "des",
        "du",
        "en",
        "un",
        "une",
        "que",
        "est",
        "pour",
        "au",
    },
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _is_arabic(c: str) -> bool:
    return "\u0600" <= c <= "\u06ff" or "\u0750" <= c <= "\u077f" or "\ufb50" <= c <= "\ufefc"


def text_layer_garbled(pages: list[str], language: str) -> str | None:
    """PDF 有文字层但内容是乱码(字体编码缺失导致)时返回原因,否则返回 None。

    这类文件如果当成"已解析"会把乱码入库,所以要和"没有文字层"一样走 OCR。
    判断依据:控制字符比例;预期文字(阿拉伯文/拉丁文)占字母的比例;英法文的常用词命中率。
    """
    text = "\n".join(pages)
    if len(text.strip()) < 200:
        return None
    controls = sum(1 for c in text if unicodedata.category(c) == "Cc" and c not in "\n\r\t\f")
    if controls / len(text) > 0.01:
        return f"文字层含大量控制字符({controls} 个),疑似字体编码缺失导致的乱码"
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 100:
        return None
    lang = language.split("-")[0].lower()
    if lang == "ar":
        ratio = sum(1 for c in letters if _is_arabic(c)) / len(letters)
        if ratio < 0.6:
            return f"阿拉伯文文件的文字层里阿拉伯字母只占 {ratio:.0%},疑似乱码"
    elif lang in _STOPWORDS:
        latin = sum(1 for c in letters if unicodedata.name(c, "").startswith("LATIN")) / len(
            letters
        )
        if latin < 0.9:
            return f"拉丁字母只占 {latin:.0%},疑似乱码"
        words = [w.lower() for w in _WORD.findall(text)]
        if words:
            hit = sum(1 for w in words if w in _STOPWORDS[lang]) / len(words)
            if hit < 0.04:
                return f"常用词命中率仅 {hit:.1%},疑似乱码"
    return None


def parse_document(content: bytes, mime: str) -> ParsedDocument:
    kind = detect_kind(content, mime)
    if kind == "pdf":
        events = []
        return ParsedDocument(kind, parse_pdf(content, layout_events=events), events)
    if kind == "html":
        return ParsedDocument(kind, parse_html(content))
    return ParsedDocument(kind, [_decode(content)])
