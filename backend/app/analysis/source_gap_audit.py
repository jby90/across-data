"""Audit retained failures without generating a new conclusion or repairing source text."""

import hashlib

import pypdfium2 as pdfium


def unresolved_cases(evaluation, country=None):
    rows = []
    for case in evaluation["cases"]:
        if country is not None and case["country_code"] != country:
            continue
        offset = case.get("final_findings_offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(case["findings"]):
            raise ValueError("invalid final-attempt offset")
        supported = False
        for judgment in case["judgments"]:
            index = judgment["index"]
            if type(index) is not int or not 0 <= index < len(case["findings"]):
                raise ValueError("judge refers to a missing finding")
            if index >= offset and judgment["supported"] == "yes":
                supported |= case["findings"][index].get("_ok") is True
        if not supported:
            rows.append(case)
    return rows


def verify_retained_quote(finding, original_text):
    start, end = finding.get("quote_start"), finding.get("quote_end")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(original_text)
        or original_text[start:end] != finding.get("quote")
        or hashlib.sha256(original_text.encode()).hexdigest() != finding.get("source_text_sha256")
    ):
        raise ValueError("retained source/quote drift; stop audit rather than repair it")


def pdf_source_profile(blob, *, max_pages=200):
    """Measure original image resolution; render DPI is not new source detail."""
    pages = []
    with pdfium.PdfDocument(blob) as document:
        if len(document) > max_pages:
            raise ValueError("page budget exceeded; do not silently truncate")
        for n in range(len(document)):
            page = document[n]
            try:
                text = page.get_textpage()
                try:
                    chars = text.count_chars()
                finally:
                    text.close()
                images = []
                for obj in page.get_objects():
                    if isinstance(obj, pdfium.PdfImage):
                        metadata = obj.get_metadata()
                        images.append(
                            {
                                "width": metadata.width,
                                "height": metadata.height,
                                "horizontal_dpi": metadata.horizontal_dpi,
                                "vertical_dpi": metadata.vertical_dpi,
                            }
                        )
                pages.append({"page": n + 1, "text_characters": chars, "images": images})
            finally:
                page.close()
    return {
        "source_sha256": hashlib.sha256(blob).hexdigest(),
        "pages": pages,
        "model_calls": 0,
        "ocr_runs": 0,
        "human_review": None,
        "note": "仅测量源 PDF 的文字层/嵌入图像；不能证明可读性、OCR 正确或法律适用。",
    }
