"""新加坡法条/指引真实版式的回归测试(夹具取自一期核对过的官方 PDF 的版式)。"""

from app.ingestion.clauses import split_clauses
from app.ingestion.parsing import strip_margin_line_numbers

BODY = [
    # 第 1 页:Part 标题 + 条标题 + 条文
    "\n".join(
        [
            "PART 6",
            "CARE OF PERSONAL DATA",
            "Transfer of personal data outside Singapore",
            "26.—(1) An organisation must not transfer any personal data to a",
            "country or territory outside Singapore except in accordance with",
            "requirements prescribed under this Act.",
            "(2) The Commission may, on the application of any organisation,",
            "by written notice exempt the organisation from any requirement.",
            "PART 6A",
            "NOTIFICATION OF DATA BREACHES",
            "Interpretation of this Part",
            "26A. In this Part, unless the context otherwise requires —",
            "“data breach” means the unauthorised access of personal data;",
            "Duty to conduct assessment of data breach",
            "26B.—(1) Where an organisation has reason to believe that a data",
            "breach affecting personal data has occurred, it must assess it.",
        ]
    ),
    # 第 2 页:Division + 日期页脚 + 行首小写交叉引用
    "\n".join(
        [
            "PART 4",
            "COLLECTION, USE AND DISCLOSURE OF",
            "PERSONAL DATA",
            "Division 1 — Consent",
            "Consent required",
            "27.—(1) An organisation must not collect personal data about an",
            "individual unless the individual gives consent, subject to",
            "section 24(1)(b) of the Act and the exceptions in the Schedule.",
            "Withdrawal of consent",
            "28.—(1) On giving reasonable notice to the organisation, an",
            "individual may at any time withdraw any consent given.",
            "31.12.2021",
        ]
    ),
]


def by_number(drafts, number, path_has=None):
    for d in drafts:
        if d.clause_number == number and (path_has is None or path_has in (d.section_path or "")):
            return d
    return None


def test_statute_sections_get_heading_path_and_verbatim_text():
    drafts, strategy = split_clauses(BODY)
    assert strategy == "numbered"
    assert [d.clause_number for d in drafts if d.clause_number] == ["26", "26A", "26B", "27", "28"]

    s26 = by_number(drafts, "26")
    assert s26.heading == "Transfer of personal data outside Singapore"
    assert s26.section_path == "PART 6 CARE OF PERSONAL DATA"
    assert s26.text.startswith(
        "Transfer of personal data outside Singapore\n26.—(1) An organisation"
    )
    assert "(2) The Commission may" in s26.text and "PART 6A" not in s26.text  # Part 标记不混进正文

    s26a = by_number(drafts, "26A")
    assert s26a.section_path == "PART 6A NOTIFICATION OF DATA BREACHES"
    assert s26a.heading == "Interpretation of this Part"
    # 条内的小标题行(以大写开头、无句号)只在紧邻下一条编号前才作为标题弹出
    assert by_number(drafts, "26B").heading == "Duty to conduct assessment of data breach"


def test_multiline_part_title_and_division_path():
    drafts, _ = split_clauses(BODY)
    s27 = by_number(drafts, "27")
    assert s27.section_path == (
        "PART 4 COLLECTION, USE AND DISCLOSURE OF PERSONAL DATA / Division 1 — Consent"
    )
    assert s27.heading == "Consent required"


def test_date_footer_is_not_a_section_and_lowercase_crossref_is_not_a_header():
    drafts, _ = split_clauses(BODY)
    assert by_number(drafts, "31") is None  # 31.12.2021 是日期页脚
    s27 = by_number(drafts, "27")
    assert "section 24(1)(b) of the Act" in s27.text  # 行首小写交叉引用留在正文里


def test_out_of_sequence_numbers_inside_text_are_not_new_clauses():
    pages = [
        "\n".join(
            [
                "5.—(1) The Commission may do the following —",
                "2. Something that looks like a list item but is out of sequence",
                "(a) first;",
                "6.—(1) The Commission must report each year.",
                "Article 29 of the regulation is mentioned here at a line start",
            ]
        )
    ]
    drafts, strategy = split_clauses(pages)
    assert strategy == "numbered"
    assert [d.clause_number for d in drafts if d.clause_number] == ["5", "6"]
    assert "2. Something that looks like a list item" in by_number(drafts, "5").text
    assert "Article 29 of the regulation" in by_number(drafts, "6").text


def test_table_of_contents_pages_are_merged_not_clauses():
    toc = "\n".join(
        [
            "ARRANGEMENT OF SECTIONS",
            "PART 1",
            "PRELIMINARY",
            "1. Short title",
            "2. Interpretation",
            "3. Purpose",
            "4. Application",
            "5. Commission",
            "6. Functions",
        ]
    )
    body = "\n".join(
        [
            "Short title",
            "1. This Act is the Data Act 2012.",
            "Interpretation",
            "2.—(1) In this Act, unless the context otherwise requires —",
            "“organisation” includes any individual, company or association.",
            "Purpose",
            "3. The purpose of this Act is to govern the collection of data.",
        ]
    )
    drafts, _ = split_clauses([toc, body])
    nums = [d.clause_number for d in drafts if d.clause_number]
    assert nums == ["1", "2", "3"]  # 正文条款唯一,目录没有变成重复的 1..6
    toc_block = next(d for d in drafts if d.heading == "目录(自动识别)")
    assert "6. Functions" in toc_block.text and toc_block.clause_number is None
    assert "This Act is the Data Act 2012." in by_number(drafts, "1").text


def test_guideline_paragraph_numbers_alone_on_a_line():
    pages = [
        "\n".join(
            [
                "19.3",
                "An organisation may transfer personal data overseas if conditions are met.",
                "19.4",
                "The examples below illustrate certain situations.",
                "Example:",
                "Organisation ABC is transferring personal data overseas.",
                "19.5",
                "Organisations should take reasonable steps.",
            ]
        )
    ]
    drafts, strategy = split_clauses(pages)
    assert strategy == "numbered"
    assert [d.clause_number for d in drafts] == ["19.3", "19.4", "19.5"]
    assert "Example:" in by_number(drafts, "19.4").text
    assert by_number(drafts, "19.4").text.startswith("19.4\nThe examples below")


def test_schedule_restarts_numbering_and_is_kept_apart_by_path():
    body = [
        "\n".join(f"{n}. Section {n} text of the regulation applies here." for n in range(1, 8)),
        "\n".join(
            [
                "THE SCHEDULE",
                "1. Any of the following matters is specified in the Schedule.",
                "2. Domestic abuse involving the individual.",
                "THE SCHEDULE — continued",
                "3. Another specified item that continues the list.",
            ]
        ),
    ]
    drafts, _ = split_clauses(body)
    schedule = [d for d in drafts if d.section_path == "THE SCHEDULE"]
    assert [d.clause_number for d in schedule] == ["1", "2", "3"]
    assert "continued" not in " ".join(d.text for d in drafts)  # 续页标记已丢弃
    body_items = [d for d in drafts if d.section_path is None and d.clause_number]
    assert [d.clause_number for d in body_items] == ["1", "2", "3", "4", "5", "6", "7"]


# ---------------- 页边行号 ----------------


def margin_page():
    return "\n".join(
        [
            "26.—(1) An organisation shall not transfer any personal data to a",
            "country or territory outside Singapore except in accordance with 5",
            "requirements prescribed under this Act to ensure that organisations",
            "provide a standard of protection to personal data so transferred that",
            "is comparable to the protection under this Act.",
            "(2) The Commission may, on the application of any organisation, by 10",
            "notice in writing exempt the organisation from any requirement",
            "prescribed pursuant to subsection (1) in respect of any transfer of",
            "personal data by that organisation.",
            "(3) An exemption under subsection (2) —",
            "(a) may be granted subject to such conditions as the Commission 15",
        ]
    )


def test_margin_line_numbers_are_stripped_only_when_they_form_a_chain():
    cleaned, removed = strip_margin_line_numbers([margin_page()])
    assert removed == 3
    assert "in accordance with\n" in cleaned[0] and not any(
        ln.endswith((" 5", " 10", " 15")) for ln in cleaned[0].splitlines()
    )


def test_margin_stripping_does_not_touch_real_references():
    page = "\n".join(
        [
            "The Commission may act under section 5",
            "and the organisation must comply with regulation 10",
            "or follow the guidance in paragraph 15",
            "random text line four",
            "random text line five",
            "random text line six",
            "random text line seven",
            "random text line eight",
            "see also item 20",
        ]
    )
    cleaned, removed = strip_margin_line_numbers([page])
    # 数字差与行距对不上,不是页边行号,正文里的引用号必须原样保留
    assert removed == 0 and cleaned[0] == page


def test_lone_margin_number_line_is_dropped():
    page = "\n".join(
        [
            "line one text here with words 5",
            "line two",
            "line three",
            "line four text",
            "PART VI",
            "10",
            "CARE OF PERSONAL DATA",
            "line eight more words here",
            "line nine",
            "line ten words 15",
        ]
    )
    cleaned, removed = strip_margin_line_numbers([page])
    assert removed == 3
    assert "PART VI\nCARE OF PERSONAL DATA" in cleaned[0]
