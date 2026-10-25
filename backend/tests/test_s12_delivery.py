"""Delivery checks use synthetic multilingual evidence, never a business database."""
import copy
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pypdfium2

from app.analysis.pdf_renderer import report_html, verified_fonts
from app.analysis.report_exporter import build_report_docx, build_report_pdf


def sample():
    task = SimpleNamespace(id=uuid.uuid4(), created_at=datetime.now(UTC), title="导出检查",
                           country_code="ZZ", vehicle_model="示例", findings_json=[])
    dims = [{"dimension_code": "D3", "dimension_title": "跨境与范围", "risk_level": "待核对",
             "content_markdown": "中文：业务事实不足；France : données à caractère personnel.",
             "citations": [{"document_title": "Synthetic evidence", "clause_number": "17",
                            "version_no": 4, "quote_original": "المادة ١٧: حماية البيانات 2026.",
                            "quote_zh": "第17条：数据保护。", "language": "ar",
                            "source_url": "https://example.invalid/<img src='file:///secret'>",
                            "source_review_status": "pending", "translation_review_status": "pending"}],
             "findings": []}]
    rv = SimpleNamespace(version_no=3, dimensions_json=dims, regulatory_updates=[],
                         executive_summary="未经人工复核 <script>alert(1)</script>",
                         citation_policy="allow_machine_with_notice", is_partial_sample=True)
    return task, rv


def test_pdf_selectable_multilingual_and_snapshot_unchanged():
    task, rv = sample()
    before = copy.deepcopy(rv.dimensions_json)
    blob = build_report_pdf(task, rv, "D3")
    pdf = pypdfium2.PdfDocument(blob)
    text = "".join(page.get_textpage().get_text_range() for page in pdf)
    assert len(pdf) >= 1 and "2026" in text and "Synthetic evidence" in text
    assert "数据保护" in text and "France" in text
    assert any("\u0600" <= c <= "\u06ff" for c in text)
    assert "D3" in text and "v3" in text and "待核对" in text
    assert rv.dimensions_json == before
    assert len(verified_fonts()) == 3
    assert build_report_docx(task, rv, "D3").startswith(b"PK")


def test_pdf_content_is_text_not_active_markup():
    task, rv = sample()
    html = report_html(task, rv, dimension="D3", context="fixture", country="未知",
                       notices=["待核对", "免责声明"])
    assert "<script>" not in html and "<img " not in html
    assert "المادة ١٧: حماية البيانات 2026." in html
    assert 'dir="rtl"' in html and 'dir="ltr"' in html and "hyphens:none" in html
    assert html.count("@font-face") == 3
