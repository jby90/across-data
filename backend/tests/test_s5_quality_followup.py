import json
from dataclasses import replace

import pytest

from app.analysis import pipeline
from app.analysis.evidence_selection import select_legal_quote, sentence_catalog
from app.analysis.llm import LlmResult
from app.analysis.models import AnalysisTask
from app.analysis.retrieval import ClauseIndex, normalize_index, task_query
from app.analysis.tools import ClauseCandidate
from app.analysis.workflow_runtime import baseline_nodes, execution_settings


def candidate(id, source, translation="", heading=""):
    return ClauseCandidate(
        id,
        "doc-" + id,
        "Unseen law",
        "v-" + id,
        1,
        "17",
        source,
        "https://official.example/source",
        "pending_review",
        "unknown",
        translated_text=translation,
        heading=heading,
    )


def test_index_normalization_is_separate_from_untouched_arabic_french_source():
    source = "\u200fالبيــانات الشَّخصية؛ durée de CONSERVATION"
    before = source
    assert "البيانات" in normalize_index(source)
    assert "duree de conservation" in normalize_index(source)
    assert source == before


@pytest.mark.parametrize("status", ["irrelevant", "out_of_scope", "needs_review"])
async def test_retrieval_respects_explicit_scope_without_removing_sources_or_reviews(
    session_factory, status
):
    from app.analysis.tools import ToolBox
    from app.ingestion.models import Clause

    from .review_s1_s2_contracts import make_document, make_edge

    async with session_factory() as s:
        doc, _, clauses = await make_document(s)
        doc.relevance = status
        edge, _, _ = await make_edge(s, clauses[0], "approved")
        await s.flush()
        before = (clauses[0].original_text, edge.evidence_quote, edge.review_status)
        candidates = await ToolBox(s, doc.country_code)._load_candidates()
        assert bool(candidates) is (status == "needs_review")
        assert await s.get(Clause, clauses[0].id) is clauses[0]
        assert before == (clauses[0].original_text, edge.evidence_quote, edge.review_status)


def test_saved_prompt_declares_json_for_provider_json_mode():
    from app.analysis.prompts import ANALYSIS_SYSTEM_PROMPT

    assert "json" in ANALYSIS_SYSTEM_PROMPT.lower()


@pytest.mark.parametrize("status", ["unknown", "in_force", "amended", "repealed"])
async def test_repealed_latest_source_is_excluded_without_guessing_unknown_status(
    session_factory, status
):
    from app.analysis.tools import ToolBox

    from .review_s1_s2_contracts import make_document

    async with session_factory() as s:
        _, version, _ = await make_document(s)
        version.legal_status = status
        await s.flush()
        found = await ToolBox(s, "SG")._load_candidates()
        assert bool(found) is (status != "repealed")
        if found:
            assert found[0].legal_status == status


async def test_repealed_current_version_never_falls_back_to_older_active_text(session_factory):
    import uuid

    from app.analysis.tools import ToolBox
    from app.ingestion.models import Clause, DocumentVersion

    from .review_s1_s2_contracts import make_document

    async with session_factory() as s:
        doc, version, clauses = await make_document(s)
        version.legal_status = "in_force"
        current = DocumentVersion(
            document_id=doc.id,
            version_no=2,
            content_hash="b" * 64,
            archive_path="unused",
            size_bytes=1,
            mime_type="text/plain",
            final_url=doc.source_url,
            state="parsed",
            legal_status="repealed",
        )
        s.add(current)
        await s.flush()
        s.add(
            Clause(
                version_id=current.id,
                ordinal=1,
                clause_number="1",
                language="en",
                original_text=clauses[0].original_text,
                text_hash=uuid.uuid4().hex * 2,
            )
        )
        await s.flush()
        assert await ToolBox(s, "SG")._load_candidates() == []
        assert clauses[0].original_text and version.legal_status == "in_force"


def test_grounding_errors_are_counted_without_rewriting_historical_audit_payloads():
    from app.analysis.evaluation import generation_diagnostics

    case = {
        "analysis_error": None,
        "findings": [{"_generation_error": "schema failed"}, {"_generation_gap": "damaged source"}],
    }
    updated = generation_diagnostics(case)
    assert updated["analysis_error"] == "schema failed" and updated["generation_error_count"] == 1
    assert updated["evidence_gap_count"] == 1 and case["analysis_error"] is None


def test_evaluation_keeps_rejected_first_selection_in_denominator_after_reselection():
    from app.analysis.evaluation import attempted_findings

    first = {"obligation": "", "_generation_gap": "缺少条件"}
    final = {"obligation": "有依据的规则"}
    result = {"findings": [final]}
    call = {"arguments": {"selection_retries": [{"findings": [first]}]}}
    findings = attempted_findings(result, call)
    assert len(findings) == 2 and findings[0]["_generation_gap"] == "缺少条件"
    findings[0]["_ok"] = False
    assert "_ok" not in first and result["findings"] == [final]


def test_actual_question_can_distinguish_two_issues_in_same_dimension_without_country_branches():
    rows = [
        candidate("x", "Ordinary security rule.", "必须采取安全保护措施防止未经授权访问"),
        candidate("y", "Accurate data shall be updated.", "个人数据应保持准确并及时更新"),
        candidate("z", "Consent is required.", "收集资料应征得有效同意"),
    ]
    index = ClauseIndex(rows)
    assert index.rank("security", "采取哪些安全保护措施")[0][0].clause_id == "x"
    assert index.rank("security", "如何确保数据准确更新")[0][0].clause_id == "y"
    changed = [replace(c, document_title="Entirely new title", label="unseen") for c in rows]
    assert [c.clause_id for c, _ in index.rank("security", "数据准确更新")] == [
        c.clause_id for c, _ in ClauseIndex(changed).rank("security", "数据准确更新")
    ]
    assert "具体问题" in task_query({"new_fact": "具体问题"}, "D9")
    assert task_query({"retention_and_blackbox": "留存三年"}, "D6") == ""


def test_chapeau_and_semicolon_list_stay_together_without_synthesizing_source():
    source = "Art. 17: If an application is accepted, the applicant shall provide:\n- the purpose;\n- the security measures."
    assert len(sentence_catalog(source)) == 1
    selected = select_legal_quote(source, [1])
    assert selected["quote"] == source
    assert selected["quote"] == source[selected["quote_start"] : selected["quote_end"]]


def test_orphan_list_item_keeps_its_intro_and_following_exception():
    source = "The following exemptions apply.\n- Personal activities are exempt. Except where disclosure is public."
    quoted = select_legal_quote(source, [2])
    assert quoted["selected_sentence_ids"] == [2]
    assert quoted["quote"] == source
    assert quoted["anchor_quote_start"] > 0 and quoted["quote_start"] == 0
    with pytest.raises(ValueError):
        select_legal_quote(source, [1, 3])


def test_glossary_semicolons_do_not_make_every_definition_one_giant_evidence_unit():
    source = "Definitions\n“operator” means a licensed provider;\n“data” means information about a person;\n“record” means a stored copy."
    catalog = sentence_catalog(source)
    assert len(catalog) >= 3
    data = next(s for s in catalog if s["text"].startswith("“data”"))
    evidence = select_legal_quote(source, [data["sentence_id"]])
    assert "“data”" in evidence["quote"] and "“operator”" not in evidence["quote"]
    assert evidence["quote"] in source


@pytest.mark.parametrize("country", ["BR", "KE", "AE", "FI"])
async def test_grounding_call_cannot_see_other_clauses_or_selection_stage_drafts(
    country, monkeypatch
):
    source = "Organisations must protect data with reasonable security arrangements."
    rows = [
        candidate("chosen", source),
        candidate("other", "Unrelated invented penalty of 999 dollars."),
    ]
    observed = []

    async def fake(messages, **kwargs):
        p = json.loads(messages[1]["content"])
        observed.append(p)
        if p["phase"] == "verify_source_support":
            assert p["quote"] == source and "999" not in messages[1]["content"]
            return LlmResult({"supported": "yes", "reason": "Exact fixture rule"}, 0, 0, 1, "fake")
        if p["phase"] == "select_evidence":
            assert p["output_schema"]["$defs"]["FindingSelection"]["properties"]["fact_key"][
                "enum"
            ] == ["operation"]
            data = {
                "findings": [
                    {
                        "clause_id": "chosen",
                        "sentence_ids": [1],
                        "fact_key": "operation",
                        "obligation": "不可信草稿999",
                        "risk_level": "HIGH",
                    }
                ]
            }
        else:
            assert p["evidence"]["quote"] == source
            assert set(p["evidence"]) == {"quote"}
            assert "999" not in messages[1]["content"]
            assert "candidate_clauses" not in p
            assert len(p["output_schema"]["oneOf"]) == 2
            data = {
                "obligation": "组织须采取合理安全措施保护数据",
                "risk_level": "MEDIUM",
                "gap": "",
            }
        return LlmResult(data, 11, 7, 1, "fake")

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    cfg = execution_settings(
        next(n for n in baseline_nodes() if n["node_id"] == "node_dimension_analyst")
    )
    task = AnalysisTask(
        country_code=country,
        vehicle_model="",
        scenario_type="test",
        facts_json={"operation": "处理资料"},
    )
    result, call = await pipeline._analyse_dimension(task, "D8", "安全", rows, None, cfg)
    assert pipeline._verify_dimension("D8", result, rows) == []
    assert result["tokens"] == 36 and call.arguments["request_attempts"] == 3
    assert len(observed) == 3 and "999" not in result["findings"][0]["obligation"]


async def test_numeric_generation_error_is_retried_with_exact_source_and_remains_in_ledger(
    monkeypatch,
):
    source = "The maximum permitted percentage for this activity is 2%."
    row = candidate("bound", source)
    calls = []

    async def fake(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        if payload["phase"] == "verify_source_support":
            return LlmResult({"supported": "yes", "reason": "Exact fixture rule"}, 0, 0, 1, "fake")
        calls.append(payload)
        assert payload["evidence"] == {"quote": source}
        if len(calls) == 1:
            data = {"obligation": "该活动最高比例为20%", "gap": "", "risk_level": "MEDIUM"}
        else:
            assert "20%" in payload["schema_feedback"]
            data = {"obligation": "该活动最高比例为2%", "gap": "", "risk_level": "MEDIUM"}
        return LlmResult(data, 2, 3, 1, "fake")

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    cfg = execution_settings(
        next(n for n in baseline_nodes() if n["node_id"] == "node_dimension_analyst")
    )
    finding, ledger = await pipeline._ground_selection(
        AnalysisTask(country_code="KE", facts_json={"q": "开展业务"}),
        "D1",
        "scope",
        {"clause_id": "bound", "sentence_ids": [1], "fact_key": "q"},
        [row],
        cfg,
    )
    assert finding["obligation"] == "该活动最高比例为2%"
    assert ledger["tokens"] == 10 and ledger["requests"] == 3
    assert ledger["failures"][0]["kind"] == "grounding_validation_error"


async def test_source_refusal_returns_to_selector_within_existing_budget_and_preserves_quotes(
    monkeypatch,
):
    rows = [
        candidate("fragment", "A subordinate list without a governing rule, subject or condition."),
        candidate("complete", "The operator shall protect personal data from unauthorised access."),
    ]
    seen = []

    async def fake(messages, **kwargs):
        payload = json.loads(messages[1]["content"])
        seen.append(payload)
        if payload["phase"] == "verify_source_support":
            return LlmResult({"supported": "yes", "reason": "Exact fixture rule"}, 0, 0, 1, "fake")
        if payload["phase"] == "select_evidence":
            retry = "verifier_feedback_must_fix" in payload
            data = {
                "findings": [
                    {
                        "clause_id": "complete" if retry else "fragment",
                        "sentence_ids": [1],
                        "fact_key": "q",
                        "obligation": "待依据生成",
                        "risk_level": "MEDIUM",
                    }
                ]
            }
        elif payload["evidence"]["quote"] == rows[0].text:
            data = {"obligation": "", "gap": "缺少主语和条件", "risk_level": "MEDIUM"}
        else:
            data = {
                "obligation": "运营者应保护个人数据免遭未经授权访问",
                "gap": "",
                "risk_level": "MEDIUM",
            }
        return LlmResult(data, 2, 3, 1, "fake")

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    cfg = execution_settings(
        next(n for n in baseline_nodes() if n["node_id"] == "node_dimension_analyst")
    )
    result, call = await pipeline._analyse_dimension(
        AnalysisTask(country_code="KE", facts_json={"q": "处理个人数据"}),
        "D8",
        "安全",
        rows,
        None,
        cfg,
    )
    assert pipeline._verify_dimension("D8", result, rows) == []
    assert len(seen) == 5 and result["tokens"] == 20
    assert call.arguments["schema_attempts"] == 2 and call.arguments["request_attempts"] == 5
    previous = call.arguments["selection_retries"][0]["findings"][0]
    assert previous["quote"] == rows[0].text and previous["_generation_gap"] == "缺少主语和条件"
    assert "缺少主语和条件" in str(seen[2]["verifier_feedback_must_fix"])


async def test_source_limited_refusal_is_preserved_and_never_counted_as_verified(monkeypatch):
    rows = [
        candidate(
            "a",
            "A damaged text leaves the responsible subject unspecified and cannot be completed.",
        )
    ]

    async def fake(messages, **kwargs):
        phase = json.loads(messages[1]["content"])["phase"]
        data = (
            {
                "findings": [
                    {
                        "clause_id": "a",
                        "sentence_ids": [1],
                        "fact_key": "q",
                        "obligation": "待依据生成",
                        "risk_level": "MEDIUM",
                    }
                ]
            }
            if phase == "select_evidence"
            else {"obligation": "", "gap": "主语和条件缺失", "risk_level": "MEDIUM"}
        )
        return LlmResult(data, 1, 1, 1, "fake")

    monkeypatch.setattr(pipeline, "call_llm_json", fake)
    cfg = execution_settings(
        next(n for n in baseline_nodes() if n["node_id"] == "node_dimension_analyst")
    )
    result, call = await pipeline._analyse_dimension(
        AnalysisTask(
            country_code="NZ", facts_json={"q": "未知业务"}, vehicle_model="", scenario_type="test"
        ),
        "D1",
        "适用范围",
        rows,
        None,
        cfg,
    )
    assert len(result["findings"]) == 1 and not call.verified
    assert pipeline._verify_dimension("D1", result, rows)
    assert result["findings"][0]["_ok"] is False
