"""Human approvals below exist only in synthetic fixtures, never the business archive."""

import asyncio
import copy
import io
import zipfile

import pytest
from sqlalchemy import select, update

from app.analysis import pipeline
from app.analysis.citation_policy import policy_label, review_overview, review_summary
from app.analysis.models import AnalysisTask, ReportVersion
from app.analysis.report_exporter import build_report_docx
from app.analysis.tools import ToolBox
from app.core.config import get_settings
from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.models import ClauseTranslation

from .conftest import bearer, login
from .test_analysis_p3 import FACTS, _fake_llm_factory, _seed_sg_corpus


@pytest.mark.parametrize("country", ["SG", "TN", "ZZ"])
async def test_review_policy_is_generic_and_translation_approval_does_not_approve_source(
    session_factory,
    country,
    client,
    seed,
):
    await _seed_sg_corpus(session_factory)
    async with session_factory() as s:
        doc = await s.scalar(select(Document))
        doc.country_code = country
        clause = await s.scalar(select(Clause).order_by(Clause.ordinal))
        version = await s.get(DocumentVersion, clause.version_id)
        tr = ClauseTranslation(
            clause_id=clause.id,
            translated_text="合成测试的已审译文",
            review_status="approved",
            quality_check_passed=True,
            is_current=True,
            glossary_hits=[],
            unmatched_terms=[],
            quality_issues=[],
            engine="synthetic",
            model_name="synthetic",
        )
        s.add(tr)
        await s.commit()
        before = copy.deepcopy(clause.original_text)
        candidates = await ToolBox(s, country)._load_candidates()
        hit = next(c for c in candidates if c.clause_id == str(clause.id))
        assert hit.review_status == "pending_review" and hit.translation_review_status == "approved"
        strict = ToolBox(s, country, citation_policy="reviewed_only")
        assert await strict._load_candidates() == []
        # Simulated human approval of this exact source, only in isolated test DB.
        version.review_status = "approved"
        clause.needs_review = False
        await s.commit()
        accepted = await ToolBox(s, country, citation_policy="reviewed_only")._load_candidates()
        assert [c.clause_id for c in accepted] == [str(clause.id)]
        assert accepted[0].review_status == "approved"
        assert clause.original_text == before and tr.review_status == "approved"
        duplicate = ClauseTranslation(
            clause_id=clause.id,
            translated_text="另一合成已审译文",
            engine="synthetic",
            model_name="synthetic",
            review_status="approved",
            quality_check_passed=True,
            is_current=True,
        )
        s.add(duplicate)
        await s.commit()
        assert await ToolBox(s, country, citation_policy="reviewed_only")._load_candidates() == []
        auth = bearer(await login(client, "user", "a-member@example.com"))
        response = await client.get(
            f"/api/analysis/clauses/{clause.id}/citation-card", headers=auth
        )
        assert response.status_code == 200, response.text
        card = response.json()
        assert card["translation_review_status"] == "ambiguous_current"
        assert card["usable_as_legal_basis"] is False and card["translated_text"] == ""
        assert card["source_integrity_alerts"] and card["original_text"] == before
        duplicate.is_current = False  # Fixture explicitly resolves its own active-row conflict.
        tr.quality_check_passed = False
        await s.commit()
        assert await ToolBox(s, country, citation_policy="reviewed_only")._load_candidates() == []


@pytest.mark.parametrize("policy", ["allow_machine_with_notice", "reviewed_only"])
async def test_pipeline_snapshots_policy_and_review_counts_without_changing_approvals(
    session_factory,
    seed,
    monkeypatch,
    client,
    policy,
):
    await _seed_sg_corpus(session_factory)
    monkeypatch.setattr(get_settings(), "citation_policy", policy)
    monkeypatch.setattr(pipeline, "call_llm_json", _fake_llm_factory(bad_first=False))
    async with session_factory() as s:
        task = AnalysisTask(
            org_id=seed["org_a"].id,
            created_by=seed["a_member"].id,
            title="S9 synthetic",
            country_code="SG",
            vehicle_model="synthetic",
            scenario_type="synthetic",
            facts_json=FACTS,
        )
        s.add(task)
        await s.commit()
        await pipeline.run_pipeline(s, task.id)
        rv = await s.scalar(select(ReportVersion).where(ReportVersion.task_id == task.id))
        assert rv.citation_policy == policy and task.citation_policy == policy
        counts = review_summary(rv.dimensions_json)
        assert counts["approved"] == 0
        if policy == "reviewed_only":
            assert counts["total"] == 0 and task.status == "partial_sample"
            assert all("待律所复核" in d["missing_gap_note"] for d in rv.dimensions_json)
        else:
            assert counts["pending"] > 0
            assert all(
                c["source_review_status"] == "pending_review"
                for d in rv.dimensions_json
                for c in d["citations"]
            )
            assert all(
                "待核对" in d["content_markdown"] for d in rv.dimensions_json if d["citations"]
            )
        assert "所有引用均为官方原文" not in rv.executive_summary
        assert all(
            v.review_status == "pending_review"
            for v in (await s.scalars(select(DocumentVersion))).all()
        )
        blob = build_report_docx(task, rv)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            text = z.read("word/document.xml").decode()
            assert "引用复核概览" in text and policy_label(policy) in text
        saved = copy.deepcopy(rv.dimensions_json)
    auth = bearer(await login(client, "user", "a-member@example.com"))
    detail = (await client.get(f"/api/analysis/tasks/{task.id}", headers=auth)).json()
    assert detail["report_versions"][0]["citation_policy"] == policy
    assert detail["report_versions"][0]["citation_review_summary"] == counts
    edited = await client.post(
        f"/api/analysis/tasks/{task.id}/reports/edit",
        headers=auth,
        json={"change_note": "synthetic edit", "executive_summary": "synthetic"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["citation_policy"] == policy
    async with session_factory() as s:
        old = await s.get(ReportVersion, rv.id)
        assert old.dimensions_json == saved


def test_legacy_translation_approval_is_not_source_approval_and_counts_dedupe():
    cite = {"clause_id": "c", "version_id": "v", "review_status": "approved"}
    dims = [{"citations": [cite]}, {"citations": [copy.deepcopy(cite)]}]
    assert review_summary(dims) == {"approved": 0, "pending": 1, "total": 1, "occurrences": 2}
    assert "待核对 1 条" in review_overview(dims)
    # The historic snapshot itself is not rewritten by deriving a safer view.
    assert dims[0]["citations"][0] == cite


async def test_readonly_policy_endpoint_requires_admin(client, seed):
    assert (await client.get("/api/admin/agent-studio/citation-policy")).status_code == 401
    auth = bearer(await login(client, "admin", "admin@example.com"))
    result = (await client.get("/api/admin/agent-studio/citation-policy", headers=auth)).json()
    assert result["readonly"] is True and result["policy"] == "allow_machine_with_notice"


async def test_review_revocation_during_analysis_is_rechecked_and_report_lock_is_short(
    session_factory,
    seed,
):
    await _seed_sg_corpus(session_factory)
    async with session_factory() as s:
        clause = await s.scalar(select(Clause).order_by(Clause.ordinal))
        version = await s.get(DocumentVersion, clause.version_id)
        version.review_status, clause.needs_review = "approved", False
        tr = ClauseTranslation(
            clause_id=clause.id,
            translated_text="合成复核样例",
            engine="synthetic",
            model_name="synthetic",
            review_status="approved",
            is_current=True,
            quality_check_passed=True,
        )
        task = AnalysisTask(
            org_id=seed["org_a"].id,
            created_by=seed["a_member"].id,
            title="S9 synthetic race",
            country_code="SG",
            vehicle_model="synthetic",
            scenario_type="synthetic",
            citation_policy="reviewed_only",
        )
        s.add_all([tr, task])
        await s.commit()
        cached = await ToolBox(s, "SG", citation_policy="reviewed_only")._load_candidates()
        locked = ToolBox(s, "SG", citation_policy="reviewed_only", lock_reviews=True)
        assert await locked._load_candidates()

        async def revoke():
            async with session_factory() as other:
                await other.execute(
                    update(ClauseTranslation)
                    .where(ClauseTranslation.id == tr.id)
                    .values(review_status="rejected")
                )
                await other.commit()

        pending = asyncio.create_task(revoke())
        try:
            await asyncio.sleep(0.05)
            assert not pending.done()  # Review cannot change inside the report commit window.
        finally:
            await s.commit()  # No model call or user wait occurs while holding the share lock.
        await asyncio.wait_for(pending, 2)
        with pytest.raises(ValueError, match="复核或版本状态"):
            await pipeline._compile(
                s,
                task,
                locked,
                {"D3": {"findings": [{"clause_id": str(clause.id), "_ok": True}]}},
                {"D3": cached},
                {"D3": "synthetic"},
            )
        assert not list((await s.scalars(select(ReportVersion))).all())
