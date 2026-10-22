import pytest

from app.ingestion.clauses import match_header, split_clauses
from app.ingestion.discovery import extract_links, next_page_url, normalise_url
from app.ingestion.parsing import ParseError, detect_kind, parse_document

from .helpers import make_pdf

# ---------------- 链接发现 ----------------

INDEX_HTML = """
<html><body>
<a href="/laws/pdpa.pdf">Personal Data Protection Act</a>
<a href="/laws/pdpa.pdf#page=2">duplicate with fragment</a>
<a href="https://other.example.org/g/guide.PDF">Advisory Guideline</a>
<a href="/img/logo.png">logo</a>
<a href="mailto:x@example.org">mail</a>
<a href="javascript:void(0)">js</a>
<a href="/laws/road.pdf">Road Traffic Rules</a>
<a href="/next?p=2" class="next">next</a>
</body></html>
"""


def test_extract_links_filters_dedupes_and_resolves():
    links = extract_links(INDEX_HTML, "https://example.org/index")
    urls = [c.url for c in links]
    assert urls == [
        "https://example.org/laws/pdpa.pdf",
        "https://other.example.org/g/guide.PDF",
        "https://example.org/laws/road.pdf",
    ]
    assert links[0].title == "Personal Data Protection Act"


def test_extract_links_keywords_and_extensions():
    links = extract_links(INDEX_HTML, "https://example.org/", keywords=["personal data"])
    assert [c.url for c in links] == ["https://example.org/laws/pdpa.pdf"]
    png = extract_links(INDEX_HTML, "https://example.org/", extensions=[".png"])
    assert [c.url for c in png] == ["https://example.org/img/logo.png"]


def test_extract_links_selector_scopes_to_container():
    html = '<div id="a"><a href="/x.pdf">x</a></div><div id="b"><a href="/y.pdf">y</a></div>'
    links = extract_links(html, "https://example.org/", selector="#b a[href]")
    assert [c.url for c in links] == ["https://example.org/y.pdf"]


def test_next_page_url():
    assert (
        next_page_url(INDEX_HTML, "https://example.org/index", "a.next")
        == "https://example.org/next?p=2"
    )
    assert next_page_url(INDEX_HTML, "https://example.org/index", None) is None
    assert next_page_url(INDEX_HTML, "https://example.org/index", "a.missing") is None


def test_normalise_url():
    assert normalise_url("https://e.org/a#x") == "https://e.org/a"
    assert normalise_url("ftp://e.org/a") is None


# ---------------- 条款切分 ----------------


@pytest.mark.parametrize(
    "line,number",
    [
        ("Article 5 Data transfer", "5"),
        ("Section 26. Transfer of personal data", "26"),
        ("Regulation 3A", "3A"),
        ("Article premier", "premier"),
        ("Art. 12 – Sanctions", "12"),
        ("1.1 The PDPA governs the collection of data", "1.1"),
        ("12. The organisation must keep records.", "12"),
        ("第十二条 个人信息出境应当", "十二"),
        ("第12条 个人信息", "12"),
        ("المادة 7 نقل البيانات", "7"),
        ("المادة ٧", "٧"),
    ],
)
def test_match_header_positive(line, number):
    m = match_header(line)
    assert m is not None and m[0] == number


@pytest.mark.parametrize(
    "line",
    [
        "In 2021 the revised act came into force.",
        "2021 revision of the act",
        "see Article",
        "Fine not exceeding $2,000",
        "",
    ],
)
def test_match_header_negative(line):
    assert match_header(line) is None


def test_match_header_heading_only_for_keyword_style():
    assert match_header("Article 5 Data transfer") == ("5", "Data transfer")
    assert match_header("1.1 The PDPA governs the collection of data")[1] is None


def test_split_numbered_english_keeps_text_verbatim_and_pages():
    pages = [
        "Preamble text line\nArticle 1 Scope\nThis Act applies to all.\n",
        "Article 2 Definitions\n(a) personal data means data.\nmore\n",
    ]
    drafts, strategy = split_clauses(pages)
    assert strategy == "numbered"
    assert [d.clause_number for d in drafts] == [None, "1", "2"]
    assert drafts[1].text == "Article 1 Scope\nThis Act applies to all."
    assert drafts[1].heading == "Scope"
    assert drafts[2].page_start == 2 and drafts[0].page_start == 1
    assert [d.ordinal for d in drafts] == [0, 1, 2]


def test_split_chinese_and_arabic():
    zh, s1 = split_clauses(["第一条 为了保护个人信息。\n第二条 本法适用于境内。\n补充说明。"])
    assert s1 == "numbered" and [d.clause_number for d in zh] == ["一", "二"]
    assert zh[1].text.endswith("补充说明。")
    ar, s2 = split_clauses(["المادة 1 التعريفات\nنص\nالمادة 2 النطاق\nنص ثان"])
    assert s2 == "numbered" and [d.clause_number for d in ar] == ["1", "2"]


def test_split_falls_back_to_paragraphs_and_says_so():
    drafts, strategy = split_clauses(["First paragraph line one\nline two\n\nSecond paragraph\n"])
    assert strategy == "paragraph" and len(drafts) == 2
    assert (
        drafts[0].clause_number is None and drafts[0].text == "First paragraph line one\nline two"
    )


def test_split_line_mode_for_html():
    drafts, strategy = split_clauses(["alpha\nbeta\ngamma"], paragraph_mode="line")
    assert strategy == "line" and [d.text for d in drafts] == ["alpha", "beta", "gamma"]


def test_split_empty():
    assert split_clauses(["  \n \n"]) == ([], "empty")


def test_single_header_is_not_enough_for_numbered():
    _, strategy = split_clauses(["Article 1 Only one\ntext"])
    assert strategy == "paragraph"


# ---------------- 文档解析 ----------------


def test_parse_pdf_pages_and_text():
    pdf = make_pdf(
        [["Article 1 Scope", "This Act applies."], ["Article 2 Terms", "More text here."]]
    )
    doc = parse_document(pdf, "application/pdf")
    assert doc.kind == "pdf" and len(doc.pages) == 2
    assert "Article 1 Scope" in doc.pages[0] and "Article 2 Terms" in doc.pages[1]
    assert not doc.looks_scanned


def test_pdf_without_text_layer_looks_scanned():
    doc = parse_document(make_pdf([[], [], []]), "application/pdf")
    assert len(doc.pages) == 3 and doc.char_count == 0 and doc.looks_scanned


def test_detect_kind_by_magic_not_just_mime():
    assert detect_kind(b"%PDF-1.7 ...", "application/octet-stream") == "pdf"
    assert detect_kind(b"<!DOCTYPE html><html>", "") == "html"
    assert detect_kind(b"plain", "text/plain") == "text"
    with pytest.raises(ParseError):
        detect_kind(b"PK\x03\x04", "application/zip")


def test_parse_html_drops_scripts_and_keeps_text():
    html = b"<html><head><style>x{}</style></head><body><nav>menu</nav><script>evil()</script><h1>Title</h1><p>Body  text</p></body></html>"
    doc = parse_document(html, "text/html")
    assert doc.pages == ["Title\nBody text"]


def test_corrupt_pdf_raises_parse_error():
    with pytest.raises(ParseError):
        parse_document(b"%PDF-1.4\nthis is not a pdf at all", "application/pdf")
