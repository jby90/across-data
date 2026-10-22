import io
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from app.cli import reparse
from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Clause, DocumentVersion, PipelineEvent
from app.ingestion.ocr import OcrEngine

from .test_pipeline import Site, add_site, run

_DEFAULT_CMD = r"C:\Users\Jin\tools\tess-env\Library\bin\tesseract.exe"
_DEFAULT_DATA = r"C:\Users\Jin\tools\tess-env\Library\share\tessdata"
TESS_CMD = os.environ.get("CYSH_TEST_TESSERACT", _DEFAULT_CMD)
TESS_DATA = os.environ.get("CYSH_TEST_TESSDATA", _DEFAULT_DATA)

needs_tesseract = pytest.mark.skipif(
    not Path(TESS_CMD).exists(), reason="本机没有安装 Tesseract(设置 CYSH_TEST_TESSERACT)"
)


def make_scanned_pdf(pages: list[list[str]]) -> bytes:
    """把文字画成图片再存成纯图片 PDF,模拟扫描件(没有文字层)。"""
    font = ImageFont.load_default(size=44)
    images = []
    for lines in pages:
        img = Image.new("L", (1654, 2339), 255)  # A4,200dpi
        draw = ImageDraw.Draw(img)
        y = 160
        for line in lines:
            draw.text((140, y), line, fill=0, font=font)
            y += 90
        images.append(img)
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:], resolution=200)
    return buf.getvalue()


SCAN_PAGES = [
    [
        "Article 1 Scope",
        "This Act applies to all organisations that collect vehicle data.",
        "Article 2 Transfer",
        "An organisation must not transfer personal data outside the country.",
    ],
    [
        "Article 3 Penalties",
        "Any person who contravenes this Act shall be liable to a fine.",
    ],
]


def engine() -> OcrEngine:
    return OcrEngine(TESS_CMD, TESS_DATA, dpi=200)


@needs_tesseract
def test_ocr_engine_reads_scanned_english_pdf():
    result = engine().run(make_scanned_pdf(SCAN_PAGES), "en")
    text = "\n".join(result.pages)
    assert len(result.pages) == 2
    assert "Article 1 Scope" in text and "Article 3 Penalties" in text
    assert "transfer personal data" in text
    assert result.mean_confidence > 70 and result.languages == "eng"


def test_engine_unavailable_when_binary_missing():
    assert OcrEngine("C:/definitely/not/here/tesseract.exe", None).available is False


def test_language_mapping():
    e = OcrEngine(None, None)
    assert e.languages_for("ar") == "ara+eng" and e.languages_for("fr-FR") == "fra+eng"
    assert e.languages_for("xx") == "eng"


@needs_tesseract
async def test_pipeline_ocr_turns_scan_into_clauses_but_flags_machine_text(
    session_factory, tmp_path
):
    state = Site()
    state.pdf = make_scanned_pdf(SCAN_PAGES)
    site_id = await add_site(
        session_factory, strategy="DIRECT", entry="https://example.org/laws/act.pdf", config={}
    )
    from app.ingestion.pipeline import ingest_site

    from .helpers import make_fetcher

    archive = ArchiveStore(tmp_path / "a")
    await ingest_site(
        session_factory,
        make_fetcher(state.handler),
        archive,
        site_id,
        trigger="manual",
        ocr=engine(),
    )
    async with session_factory() as s:
        v = (await s.execute(select(DocumentVersion))).scalar_one()
        assert v.state == "parsed" and v.text_source == "ocr"
        assert v.ocr_confidence is not None and v.ocr_confidence > 70
        clauses = (
            (
                await s.execute(
                    select(Clause).where(Clause.version_id == v.id).order_by(Clause.ordinal)
                )
            )
            .scalars()
            .all()
        )
        assert [c.clause_number for c in clauses if c.clause_number] == ["1", "2", "3"]
        ev = (
            await s.execute(select(PipelineEvent).where(PipelineEvent.step == "ocr"))
        ).scalar_one()
        assert "人工核对" in ev.message  # OCR 文本必须被明确标注
        # 原始扫描 PDF 原样归档
        assert archive.read(v.archive_path) == state.pdf


async def test_scan_without_ocr_engine_stays_needs_ocr(session_factory, tmp_path):
    state = Site()
    state.pdf = make_scanned_pdf(SCAN_PAGES)
    site_id = await add_site(
        session_factory, strategy="DIRECT", entry="https://example.org/laws/act.pdf", config={}
    )
    await run(session_factory, tmp_path, state, site_id)
    async with session_factory() as s:
        v = (await s.execute(select(DocumentVersion))).scalar_one()
        assert v.state == "needs_ocr" and v.text_source == "text_layer" and v.ocr_confidence is None
        assert (await s.execute(select(Clause))).first() is None


@needs_tesseract
async def test_reparse_upgrades_needs_ocr_versions(session_factory, tmp_path, monkeypatch):
    state = Site()
    state.pdf = make_scanned_pdf(SCAN_PAGES)
    site_id = await add_site(
        session_factory, strategy="DIRECT", entry="https://example.org/laws/act.pdf", config={}
    )
    await run(session_factory, tmp_path, state, site_id)  # 先在没有 OCR 的情况下入库

    from app.core import config

    monkeypatch.setenv("CYSH_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("CYSH_TESSERACT_CMD", TESS_CMD)
    monkeypatch.setenv("CYSH_TESSDATA_DIR", TESS_DATA)
    config.get_settings.cache_clear()
    try:
        counts = await reparse("needs_ocr", None, session_factory)
    finally:
        config.get_settings.cache_clear()
    assert counts == {"parsed": 1}
    async with session_factory() as s:
        v = (await s.execute(select(DocumentVersion))).scalar_one()
        assert v.state == "parsed" and v.text_source == "ocr"
