"""Unseen countries/sources: rejected attempts and source gaps cannot become passes."""

import copy
import hashlib

import httpx
import pytest

from app.analysis.source_gap_audit import (
    pdf_source_profile,
    unresolved_cases,
    verify_retained_quote,
)
from app.ingestion.archive import ArchiveStore
from app.ingestion.source_research import ResearchLead, probe_leads

from .helpers import make_fetcher, make_pdf
from .test_ocr import make_scanned_pdf


def lead(url="https://authority.example/act.pdf", kind="national_authority"):
    return dict(
        url=url,
        publisher="Unknown new authority",
        source_kind=kind,
        discovery_url="https://authority.example/laws",
    )


@pytest.mark.parametrize("robots", [403, 500, "User-agent: *\nDisallow: /"])
async def test_research_keeps_gap_when_robots_denied_or_unknown(tmp_path, robots):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        assert request.url.path == "/robots.txt", "must not fetch a forbidden document"
        return (
            httpx.Response(robots) if isinstance(robots, int) else httpx.Response(200, text=robots)
        )

    result = await probe_leads(make_fetcher(handler), ArchiveStore(tmp_path), [lead()])
    row = result["records"][0]
    assert requested == ["/robots.txt"] and row["state"] == "blocked"
    assert not row["body_downloaded"] and not row["usable_as_legal_basis"]
    assert row["human_review"] is None and not list(tmp_path.iterdir())


async def test_archive_candidate_does_not_promote_secondary_or_approve(tmp_path):
    blob = make_pdf([["An unchanged legal source."]])

    def handler(request):
        return (
            httpx.Response(404)
            if request.url.path == "/robots.txt"
            else httpx.Response(200, content=blob, headers={"content-type": "application/pdf"})
        )

    archive = ArchiveStore(tmp_path)
    result = await probe_leads(make_fetcher(handler), archive, [lead(kind="secondary")])
    row = result["records"][0]
    assert archive.read(row["archive_path"]) == blob
    assert row["source_kind"] == "secondary" and row["state"] == "archived_candidate"
    assert row["review_status"] == "pending_review" and not row["active"]
    assert not row["usable_as_legal_basis"] and result["database_writes"] == 0


async def test_source_budget_not_silently_truncated(tmp_path):
    with pytest.raises(ValueError, match="budget"):
        await probe_leads(
            make_fetcher(lambda r: httpx.Response(500)),
            ArchiveStore(tmp_path),
            [lead()],
            max_sources=0,
        )


def test_credentials_and_private_targets_not_recorded_as_public_leads():
    for url in ["https://user:secret@example.org/act", "https://127.0.0.1/act"]:
        with pytest.raises(ValueError):
            ResearchLead.model_validate(lead(url))


def test_old_success_or_independent_judge_yes_cannot_approve_rejected_final():
    case = dict(
        country_code="XY",
        final_findings_offset=1,
        findings=[{"_ok": True}, {"_ok": False}],
        judgments=[{"index": 0, "supported": "yes"}, {"index": 1, "supported": "yes"}],
    )
    data = {"cases": [case]}
    before = copy.deepcopy(data)
    assert unresolved_cases(data, "XY") == [case]
    assert data == before and unresolved_cases(data, "YZ") == []
    case["findings"][1]["_ok"] = True
    assert unresolved_cases(data) == []
    assert (
        len(unresolved_cases({"cases": [dict(country_code="YZ", findings=[], judgments=[])]})) == 1
    )


def test_retained_offsets_and_source_hash_cannot_be_repaired_by_audit():
    text = "Original full source and exception."
    finding = dict(
        quote_start=0,
        quote_end=len(text),
        quote=text,
        source_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    verify_retained_quote(finding, text)
    for changed in [text + "x", text.replace("exception", "condition")]:
        with pytest.raises(ValueError, match="drift"):
            verify_retained_quote(finding, changed)
    finding["quote_start"] = True
    with pytest.raises(ValueError):
        verify_retained_quote(finding, text)


@pytest.mark.parametrize("offset", [-1, True, 4])
def test_invalid_final_attempt_offset_is_not_treated_as_empty_success(offset):
    with pytest.raises(ValueError, match="offset"):
        unresolved_cases(
            {
                "cases": [
                    dict(country_code="XY", findings=[], judgments=[], final_findings_offset=offset)
                ]
            }
        )


def test_pdf_profile_distinguishes_native_text_from_scan_without_running_ocr():
    native = pdf_source_profile(make_pdf([["The complete original condition."]]))
    assert native["pages"][0]["text_characters"] > 0
    scan = make_scanned_pdf([["A complete scan"], ["Second page"]])
    out = pdf_source_profile(scan)
    assert all(p["text_characters"] == 0 and len(p["images"]) == 1 for p in out["pages"])
    assert out["pages"][0]["images"][0]["width"] == 1654
    assert out["model_calls"] == out["ocr_runs"] == 0
    with pytest.raises(ValueError, match="budget"):
        pdf_source_profile(scan, max_pages=1)
