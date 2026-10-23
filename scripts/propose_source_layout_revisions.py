"""Append page-bound parsing proposals for two inspected source layouts; never activate them.

Only LTR English pages are reordered by PDF glyph positions. Arabic pages must never
use this algorithm. Original clauses, translations, evidence and approvals stay intact.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path

import pypdfium2 as pdfium

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import select

from app import models_registry  # noqa: F401
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.ingestion.models import Clause, DocumentVersion
from app.knowledge.history import payload_hash, propose
from app.knowledge.models import KnowledgeAuditRecord

TARGETS = {
    "f0fd255b-0f1f-4ce5-80bc-da26ae8f56cd": (
        16, "ac1b9cd1914d7c00df391323f3d0952e0381974dab951e7782b4f4b895b8f75f",
        "fee_schedule_and_signature", "read_original_text_order",
    ),
    "41ed474f-6426-49ea-9f61-d9d614bf74e5": (
        40, "e88076a37faf78473dcef5f67cf7ee7d34bd9960c38dcaf8d3a92e2f80785c06",
        "bibliography_ltr_reading_order", "ltr_pdf_glyph_positions_v1",
    ),
}


def extract_ltr_page(text_page):
    raw = text_page.get_text_range()
    letters = [char for char in raw if char.isalpha()]
    if not letters or sum(char.isascii() for char in letters) / len(letters) < 0.99:
        raise ValueError("this proposal algorithm is limited to inspected English-only pages")
    lines = []
    for index in range(text_page.count_chars()):
        char = text_page.get_text_range(index, 1)
        if char in ("", "\r", "\n"):
            continue
        left, bottom, right, top = text_page.get_charbox(index, loose=True)
        centre = (bottom + top) / 2
        line = next((row for row in lines if abs(row[0] - centre) < 3), None)
        if line is None:
            line = [centre, []]
            lines.append(line)
        line[1].append((left, right, char, index))
    reconstructed, positions = [], []
    for _, glyphs in sorted(lines, key=lambda row: -row[0]):
        glyphs.sort()
        text, end = "", None
        for left, right, char, index in glyphs:
            if end is not None and left - end > 2.5 and not char.isspace():
                text += " "
            text += char
            end = right
            positions.append(index)
        reconstructed.append(text.strip())
    return "\n".join(reconstructed), positions


async def run(output):
    settings = get_settings()
    if not settings.database_url.rsplit("/", 1)[-1].startswith("remediation_"):
        raise ValueError("isolated remediation copy required")
    if output.exists():
        raise ValueError("prior evidence must not be overwritten")
    results = []
    async with get_sessionmaker()() as session:
        for clause_id, (page_no, expected_hash, kind, engine) in TARGETS.items():
            clause = await session.get(Clause, uuid.UUID(clause_id))
            version = await session.get(DocumentVersion, clause.version_id)
            blob = (Path(settings.archive_dir) / version.archive_path).read_bytes()
            if hashlib.sha256(blob).hexdigest() != expected_hash or version.content_hash != expected_hash:
                raise ValueError("frozen archive identity differs")
            pdf = pdfium.PdfDocument(blob)
            text_page = pdf[page_no - 1].get_textpage()
            raw = text_page.get_text_range()
            if engine == "ltr_pdf_glyph_positions_v1":
                reconstructed, positions = extract_ltr_page(text_page)
                if not all(marker in reconstructed for marker in (
                    "The General Data Protection Regulation", "https://eur-lex.europa.eu/",
                    "2016/679", "14 -", "29 -",
                )):
                    raise ValueError("inspected page anchors missing")
            else:
                reconstructed, positions = raw, list(range(text_page.count_chars()))
            payload = dict(primary_clause_id=clause_id, version_id=str(version.id),
                page_number=page_no, pdf_content_hash=expected_hash, original_clause_text=clause.original_text,
                original_clause_text_hash=clause.text_hash, original_pdf_text=raw,
                proposed_page_text=reconstructed, pdf_glyph_indices=positions,
                parser_engine=engine, content_kind=kind, scope="whole_source_page",
                review_status="pending_review", active=False, usable_as_legal_basis=False,
                human_reviewed=False, standalone_translation=False,
                note="Page layout inspected by coding agent; this is not a human legal approval. "
                     "Retain full page, table headings, URLs, numbers and historical evidence.")
            proposal = await propose(session, clause, {"source_parse_revision": payload})
            digest = payload_hash(payload)
            record = await session.scalar(select(KnowledgeAuditRecord).where(
                KnowledgeAuditRecord.target_id == clause.id,
                KnowledgeAuditRecord.action == "source_parse_revision",
                KnowledgeAuditRecord.content_hash == digest))
            if record is None:
                record = KnowledgeAuditRecord(target_type="clauses", target_id=clause.id,
                    action="source_parse_revision", payload=payload, content_hash=digest)
                session.add(record)
            await session.flush()
            results.append(dict(clause_id=clause_id, proposal_id=str(proposal.id), audit_id=str(record.id),
                page_number=page_no, content_kind=kind, review_status="pending_review", active=False))
        await session.commit()
    output.write_text(json.dumps(dict(items=results, human_approved=0, source_overwrites=0),
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(proposals=len(results), active=False, human_approved=0), ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.json))
