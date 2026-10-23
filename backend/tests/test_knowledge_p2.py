import base64

from .conftest import bearer, login
from .helpers import make_pdf


def test_redact_sensitive_text_and_verify_translation():
    from app.knowledge.translator import redact_sensitive_text, verify_translation

    raw = "Contact dpo@company.sg or S1234567A or +65 6123-4567 within 3 calendar days."
    redacted = redact_sensitive_text(raw)
    assert "dpo@company.sg" not in redacted
    assert "S1234567A" not in redacted
    assert "6123-4567" not in redacted
    assert "[REDACTED_EMAIL]" in redacted and "[REDACTED_ID]" in redacted

    hits = [
        {
            "source_term": "personal data",
            "target_zh": "个人数据",
            "priority_level": 1,
        }
    ]
    ok, issues, _ = verify_translation(
        "An organisation must protect personal data within 3 days or pay $1,000,000.",
        "组织必须在 3 日内保护个人数据,否则处以 $1,000,000 罚款。",
        hits,
    )
    assert ok is True and issues == []

    bad_ok, bad_issues, _ = verify_translation(
        "An organisation must protect personal data within 3 days.",
        "组织必须保护私人资料。",
        hits,
    )
    assert bad_ok is False
    assert any("个人数据" in msg for msg in bad_issues)
    assert any("3" in msg for msg in bad_issues)

    # S1: 译文与原文完全相同 或 CJK 占比 < 30% 均应判定失败
    same_ok, same_issues, _ = verify_translation(
        "An organisation must protect personal data within 3 days.",
        "An organisation must protect personal data within 3 days.",
        hits,
    )
    assert same_ok is False
    assert any("相同" in msg for msg in same_issues)
    assert any("CJK" in msg for msg in same_issues)


async def test_s1_llm_translation_pass_retry_fail_and_unavailable(session_factory, monkeypatch):
    """S1 单元测试:
    1. 假 LLM 返回合格译文 -> machine_translated 且记录 tokens_in/out、duration_ms、prompt_version=translate-v2;
    2. 返回原文 / 漏数字 / 漏术语 -> 重译一次(共调用 2 次) -> 仍失败标 translation_failed;
    3. 首次失败但重译修正成功 -> machine_translated;
    4. LLM 不可用 -> not_translated 且 translated_text 为空;
    5. 长条款 (>1500 字符) 按句/款分块翻译并按序合并;
    6. 已有的旧 glossary_assisted 记录标为 superseded 并保留历史行。
    """
    import hashlib

    from sqlalchemy import select

    from app.ingestion.models import Clause, Document, DocumentVersion
    from app.knowledge import translator
    from app.knowledge.models import ClauseTranslation, GlossaryTerm
    from app.sources.models import SourceSite

    async with session_factory() as s:
        site = SourceSite(
            country_code="SG",
            publisher="PDPC",
            language="en",
            document_type="LAW",
            entry_url="https://sso.agc.gov.sg/Act/PDPA2012",
            strategy="DIRECT",
            terms_status="pending",
            enabled=False,
        )
        s.add(site)
        await s.flush()

        doc = Document(
            site_id=site.id,
            country_code="SG",
            title="Personal Data Protection Act 2012",
            document_type="LAW",
            language="en",
            source_url="https://sso.agc.gov.sg/Act/PDPA2012",
        )
        s.add(doc)
        await s.flush()

        ver = DocumentVersion(
            document_id=doc.id,
            version_no=1,
            content_hash="a" * 64,
            archive_path="SG/test.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            final_url="https://sso.agc.gov.sg/Act/PDPA2012",
            state="parsed",
        )
        s.add(ver)
        await s.flush()

        term = GlossaryTerm(
            concept_code="personal_data",
            country_code="SG",
            language="en",
            source_term="personal data",
            target_zh="个人数据",
            priority_level=1,
            statutory_definition="可识别特定个人的数据",
            review_status="approved",
        )
        s.add(term)

        orig = "Section 26: An organisation must protect personal data within 3 calendar days or pay $1,000,000."
        clause = Clause(
            version_id=ver.id,
            ordinal=1,
            clause_number="26",
            heading="Transfer limitation",
            language="en",
            original_text=orig,
            text_hash=hashlib.sha256(orig.encode()).hexdigest(),
        )
        s.add(clause)
        await s.flush()

        # 先插入一条历史 glossary_assisted 记录,验证重译后保留历史行并标为 superseded
        legacy_tr = ClauseTranslation(
            clause_id=clause.id,
            translated_text=orig,
            engine="glossary_assisted",
            model_name="glossary-constrained-v1",
            prompt_version="v1",
            glossary_hits=[],
            unmatched_terms=[],
            quality_check_passed=True,
            quality_issues=[],
            review_status="pending_review",
        )
        s.add(legacy_tr)
        await s.commit()

        # 场景 A: 假 LLM 返回合格译文 -> 通过并把旧记录标为 superseded
        async def fake_ok(text, lang, hits, unmatched, **kwargs):
            return (
                "第 26 条：组织必须在 3 个日历日内保护个人数据，否则处以 $1,000,000 罚款。",
                "deepseek-flash",
                120,
                48,
                350,
            )

        monkeypatch.setattr(translator, "_call_llm_translate", fake_ok)
        tr_ok = await translator.translate_clause(s, clause, "SG", [term])
        await s.commit()

        assert tr_ok.review_status == "machine_translated"
        assert tr_ok.status == "machine_translated"
        assert tr_ok.quality_check_passed is True
        assert tr_ok.engine == "llm"
        assert tr_ok.prompt_version == "translate-v7"
        assert tr_ok.tokens_in == 120 and tr_ok.tokens_out == 48 and tr_ok.duration_ms == 350

        all_rows = (
            (
                await s.execute(
                    select(ClauseTranslation).where(ClauseTranslation.clause_id == clause.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(
            r.engine == "glossary_assisted" and r.review_status == "superseded" for r in all_rows
        )

        # 场景 B: 假 LLM 返回漏数字/漏术语/原文 -> 重译 1 次(共 2 次调用) -> 仍失败标 translation_failed
        calls_fail = 0

        async def fake_bad(text, lang, hits, unmatched, **kwargs):
            nonlocal calls_fail
            calls_fail += 1
            # 故意漏掉数字 3 与 1,000,000 且不写“个人数据”
            return (
                "第 26 条：组织必须在若干日内保护私人资料，否则处以高额罚款。",
                "deepseek-flash",
                80,
                30,
                200,
            )

        monkeypatch.setattr(translator, "_call_llm_translate", fake_bad)
        tr_fail = await translator.translate_clause(s, clause, "SG", [term])
        await s.commit()

        assert calls_fail == 2
        assert tr_fail.review_status == "translation_failed"
        assert tr_fail.quality_check_passed is False
        assert any("个人数据" in iss for iss in tr_fail.quality_issues)
        assert any("3" in iss for iss in tr_fail.quality_issues)

        # 场景 C: 首次漏数字,重译时根据 retry_issues 修正成功 -> machine_translated
        calls_retry_ok = 0

        async def fake_retry_recover(text, lang, hits, unmatched, **kwargs):
            nonlocal calls_retry_ok
            calls_retry_ok += 1
            if calls_retry_ok == 1:
                return (
                    "第 26 条：组织必须保护个人数据，否则处以 $1,000,000 罚款。",
                    "deepseek-flash",
                    90,
                    35,
                    180,
                )
            assert kwargs.get("retry_issues")
            return (
                "第 26 条：组织必须在 3 个日历日内保护个人数据，否则处以 $1,000,000 罚款。",
                "deepseek-flash",
                110,
                42,
                210,
            )

        monkeypatch.setattr(translator, "_call_llm_translate", fake_retry_recover)
        tr_rec = await translator.translate_clause(s, clause, "SG", [term])
        await s.commit()
        assert calls_retry_ok == 2
        assert tr_rec.review_status == "machine_translated"
        assert tr_rec.quality_check_passed is True
        assert tr_rec.tokens_in == 200 and tr_rec.tokens_out == 77

        # 场景 D: LLM 不可用 -> not_translated 且 translated_text 为空
        async def fake_unavailable(text, lang, hits, unmatched, **kwargs):
            return None

        monkeypatch.setattr(translator, "_call_llm_translate", fake_unavailable)
        tr_unavail = await translator.translate_clause(s, clause, "SG", [term])
        await s.commit()
        assert tr_unavail.review_status == "not_translated"
        assert tr_unavail.translated_text == ""
        assert tr_unavail.quality_check_passed is False

        # 场景 E: 长条款 (>1500 字符) 分块并按序合并
        long_orig = (
            "1. An organisation must protect personal data in accordance with section 24. " * 25
            + "\n"
            + "2. Any breach must be notified within 3 days with a penalty up to $1,000,000. " * 25
        )
        chunks = translator.split_clause_into_chunks(long_orig, max_chars=1500)
        assert len(chunks) >= 2
        assert all(len(c) <= 1500 for c in chunks)
        long_clause = Clause(
            version_id=ver.id,
            ordinal=2,
            clause_number="27",
            language="en",
            original_text=long_orig,
            text_hash=hashlib.sha256(long_orig.encode()).hexdigest(),
        )
        s.add(long_clause)
        await s.flush()

        async def fake_chunk_llm(text, lang, hits, unmatched, **kwargs):
            if "24" in text and "1,000,000" not in text:
                return (
                    "第 1 款：组织必须依照第 24 条保护个人数据。",
                    "deepseek-flash",
                    100,
                    30,
                    150,
                )
            return (
                "第 1 款及第 2 款：任何泄露必须在 3 日内通报，最高处以 $1,000,000 罚款，并保护个人数据与第 24 条。",
                "deepseek-flash",
                100,
                35,
                160,
            )

        monkeypatch.setattr(translator, "_call_llm_translate", fake_chunk_llm)
        tr_long = await translator.translate_clause(s, long_clause, "SG", [term])
        await s.commit()
        assert tr_long.review_status == "machine_translated"
        assert "第 1 款" in tr_long.translated_text and "第 2 款" in tr_long.translated_text


async def test_s2_kg_type_constraints_verbatim_judge_and_out_of_scope(session_factory, monkeypatch):
    """S2 单元测试:
    1. 类型约束矩阵(validate_relation_type_constraints): 拒绝非法源/目标节点类型组合及纯生效日期通知;
    2. 证据逐字子串校验(align_verbatim_quote): 拒绝捏造/改写句子,对齐空白换行差异并保证为原文真实子串;
    3. 独立语义裁判(evaluate_kg_candidate): no 丢弃, partial 置信度降级并不入活跃图谱, yes 入图谱(pending_review);
    4. 文件级相关性(classify_document_relevance): Courts Reform Act 等标记为 out_of_scope 且不进入语义抽取;
    5. 旧 kg-rule-extractor-v1 边标记为 superseded。
    """
    import hashlib

    from sqlalchemy import select

    from app.ingestion.models import Clause, Document, DocumentVersion
    from app.knowledge import extractor, translator
    from app.knowledge.extractor import (
        ClauseExtractionPayload,
        SemanticJudgeVerdict,
        align_verbatim_quote,
        classify_document_relevance,
        evaluate_kg_candidate,
        validate_relation_type_constraints,
    )
    from app.knowledge.models import KgEdge, KgNode, NodeType, RelationType
    from app.sources.models import SourceSite

    # 1. 类型约束矩阵检查
    ok, _ = validate_relation_type_constraints(
        RelationType.PROHIBITS,
        NodeType.INSTRUMENT,
        NodeType.DATA_CATEGORY,
        "An organisation must not transfer personal data outside Singapore.",
    )
    assert ok is True

    bad_type, msg_type = validate_relation_type_constraints(
        RelationType.PROHIBITS,
        NodeType.INSTRUMENT,
        NodeType.PENALTY,
        "An organisation must not transfer personal data outside Singapore.",
    )
    assert bad_type is False and "违反类型约束矩阵" in msg_type

    bad_amend, msg_amend = validate_relation_type_constraints(
        RelationType.AMENDS,
        NodeType.INSTRUMENT,
        NodeType.INSTRUMENT,
        "The Minister appoints 1 February 2021 as the date on which the Act comes into operation.",
        source_name="Commencement Notification 2021",
        target_name="Personal Data Protection Act 2012",
    )
    assert bad_amend is False and "Commencement" in msg_amend

    # 2. 原文逐字子串检查
    orig_text = (
        "Section 26(1): An organisation must not transfer any personal data\n"
        "to a country or territory outside Singapore except in accordance with prescribed requirements."
    )
    aligned = align_verbatim_quote(
        orig_text,
        "An organisation must not transfer any personal data to a country or territory outside Singapore",
    )
    assert aligned is not None and aligned in orig_text
    assert align_verbatim_quote(orig_text, "Fabricated quote not in clause at all.") is None

    # 3. 语义裁判: no 丢弃 / partial 置信度降级且不入图谱 / yes 入图谱(pending_review)
    async def _judge_no(*args, **kwargs):
        return SemanticJudgeVerdict(supports="no", reason="证据片段与关系不匹配")

    monkeypatch.setattr(extractor, "_call_llm_judge", _judge_no)
    res_no = await evaluate_kg_candidate(
        relation_type=RelationType.RESTRICTS_CROSS_BORDER,
        source_type=NodeType.INSTRUMENT,
        source_name="Personal Data Protection Act 2012",
        target_type=NodeType.DATA_CATEGORY,
        target_name="个人数据",
        evidence_quote="An organisation must not transfer any personal data to a country or territory outside Singapore",
        clause_original_text=orig_text,
        document_title="Personal Data Protection Act 2012",
        confidence=0.9,
    )
    assert res_no.admitted is False and res_no.judge_verdict == "no"

    async def _judge_partial(*args, **kwargs):
        return SemanticJudgeVerdict(supports="partial", reason="仅间接提及")

    monkeypatch.setattr(extractor, "_call_llm_judge", _judge_partial)
    res_partial = await evaluate_kg_candidate(
        relation_type=RelationType.RESTRICTS_CROSS_BORDER,
        source_type=NodeType.INSTRUMENT,
        source_name="Personal Data Protection Act 2012",
        target_type=NodeType.DATA_CATEGORY,
        target_name="个人数据",
        evidence_quote="An organisation must not transfer any personal data to a country or territory outside Singapore",
        clause_original_text=orig_text,
        document_title="Personal Data Protection Act 2012",
        confidence=0.9,
    )
    assert res_partial.admitted is False
    assert res_partial.judge_verdict == "partial"
    assert res_partial.confidence <= 0.55

    async def _judge_yes(*args, **kwargs):
        return SemanticJudgeVerdict(supports="yes", reason="直接明确支撑")

    monkeypatch.setattr(extractor, "_call_llm_judge", _judge_yes)
    res_yes = await evaluate_kg_candidate(
        relation_type=RelationType.RESTRICTS_CROSS_BORDER,
        source_type=NodeType.INSTRUMENT,
        source_name="Personal Data Protection Act 2012",
        target_type=NodeType.DATA_CATEGORY,
        target_name="个人数据",
        evidence_quote="An organisation must not transfer any personal data to a country or territory outside Singapore",
        clause_original_text=orig_text,
        document_title="Personal Data Protection Act 2012",
        confidence=0.9,
    )
    assert res_yes.admitted is True
    assert res_yes.judge_verdict == "yes"
    assert res_yes.review_status == "pending_review"

    # 4. out_of_scope 文档不抽取 & 旧 kg-rule-extractor-v1 边标为 superseded
    async with session_factory() as s:
        site = SourceSite(
            country_code="SG",
            publisher="AGC",
            language="en",
            document_type="LAW",
            entry_url="https://sso.agc.gov.sg",
            strategy="DIRECT",
            terms_status="pending",
            enabled=False,
        )
        s.add(site)
        await s.flush()

        doc_bad = Document(
            site_id=site.id,
            country_code="SG",
            title="Courts (Civil and Criminal Justice) Reform Act 2021",
            document_type="LAW",
            language="en",
            source_url="https://sso.agc.gov.sg/Act/CCJRA2021",
        )
        s.add(doc_bad)
        await s.flush()

        ver_bad = DocumentVersion(
            document_id=doc_bad.id,
            version_no=1,
            content_hash="b" * 64,
            archive_path="SG/courts.pdf",
            mime_type="application/pdf",
            size_bytes=512,
            final_url="https://sso.agc.gov.sg/Act/CCJRA2021",
            state="parsed",
        )
        s.add(ver_bad)
        await s.flush()

        c_bad_txt = (
            "The High Court may order civil proceedings to be transferred from the State Courts."
        )
        c_bad = Clause(
            version_id=ver_bad.id,
            ordinal=1,
            clause_number="10",
            language="en",
            original_text=c_bad_txt,
            text_hash=hashlib.sha256(c_bad_txt.encode()).hexdigest(),
        )
        s.add(c_bad)
        await s.commit()

        rel_status, rel_reason = await classify_document_relevance(doc_bad, [c_bad])
        assert rel_status == "out_of_scope"
        assert "诉讼" in rel_reason or "Courts" in rel_reason or "无关" in rel_reason

        extract_called_for_bad = False

        async def _spy_extract(
            clause_text, clause_number, document_title, publisher_name, country_code
        ):
            nonlocal extract_called_for_bad
            if "Courts" in document_title:
                extract_called_for_bad = True
            return (ClauseExtractionPayload(entities=[], relations=[]), "deepseek-flash")

        async def _noop_tr(*args, **kwargs):
            return ("中文译文：法院可将民事诉讼移送。", "deepseek-flash", 50, 20, 100)

        monkeypatch.setattr(extractor, "_call_llm_extract_clause", _spy_extract)
        monkeypatch.setattr(translator, "_call_llm_translate", _noop_tr)
        await extractor.extract_knowledge_for_country(s, "SG")
        assert extract_called_for_bad is False
        # 范围外文件: 不产生任何语义边,但图谱节点与结构边保留(文档库与图谱口径一致)
        edges_bad = (
            (await s.execute(select(KgEdge).where(KgEdge.evidence_clause_id == c_bad.id)))
            .scalars()
            .all()
        )
        assert edges_bad
        assert all(e.is_structural for e in edges_bad)
        inst_bad = (
            await s.execute(select(KgNode).where(KgNode.document_id == doc_bad.id))
        ).scalar_one()
        assert inst_bad.review_status != "superseded"
        assert inst_bad.attributes["scope"] == "out_of_scope"


async def test_s2_relevance_tiers_manual_override_and_title_inference(monkeypatch):
    """三档相关性: 配套法规为 related(不被大模型单独判为范围外)、人工覆盖不可被自动流程改写、
    修正案标题推断 AMENDS 目标。"""
    from types import SimpleNamespace

    from app.knowledge import extractor
    from app.knowledge.extractor import (
        _NON_SUBSTANTIVE_LEAF_RE,
        _amendment_base_key,
        _resolve_instrument_target,
        classify_document_relevance,
    )

    async def _llm_says_out(*_a, **_k):
        return extractor.DocumentRelevanceVerdict(relevance="out_of_scope", reason="外围")

    monkeypatch.setattr(extractor, "_call_llm_doc_relevance", _llm_says_out)

    related = SimpleNamespace(
        title="Personal Data Protection Act 2012 - Personal Data Protection "
        "(Composition of Offences) Regulations 2021",
        document_type="REGULATION",
        source_url="https://sso.agc.gov.sg/x",
        country_code="SG",
        relevance="relevant",
        relevance_note=None,
        relevance_source="rule",
    )
    assert (await classify_document_relevance(related, []))[0] == "related"
    assert related.relevance_source == "llm"

    manual = SimpleNamespace(
        title="Personal Data Protection Act 2012",
        document_type="LAW",
        source_url="https://sso.agc.gov.sg/y",
        country_code="SG",
        relevance="out_of_scope",
        relevance_note="律所确认无关",
        relevance_source="manual",
    )
    assert (await classify_document_relevance(manual, []))[0] == "out_of_scope"
    assert manual.relevance_note == "律所确认无关"

    commencement = SimpleNamespace(
        title="X Act - X Act 2020 (Commencement) Notification 2021",
        document_type="REGULATION",
        source_url="https://sso.agc.gov.sg/z",
        country_code="SG",
        relevance="relevant",
        relevance_note=None,
        relevance_source="rule",
    )
    assert (await classify_document_relevance(commencement, []))[0] == "out_of_scope"

    assert _amendment_base_key(
        "Personal Data Protection (Enforcement) (Amendment) Regulations 2022"
    ) == _amendment_base_key("Personal Data Protection (Enforcement) Regulations 2014")

    base_node = SimpleNamespace(id="n1")
    index = [
        (
            "personal data protection act 2012",
            base_node,
            SimpleNamespace(title="Personal Data Protection Act 2012"),
        )
    ]
    assert _resolve_instrument_target("Personal Data Protection Act 2012", index) is base_node
    assert _resolve_instrument_target("Courts Reform Act 2021", index) is None
    # 生效通知不得被标题推断成修正案
    assert _NON_SUBSTANTIVE_LEAF_RE.search(
        "Personal Data Protection (Amendment) Act 2020 (Commencement) Notification 2021"
    )


async def test_s2_synthetic_rule_contracts_60_samples(monkeypatch):
    """离线规则夹具验证（未经人工确认，不能作为 S2 法律金标验收）:
    读取 backend/tests/fixtures/kg_gold.json (≥ 60 条,覆盖全部 12 类语义关系),
    要求:
    - 错误样例(expected_valid=False)被拒绝率 ≥ 90%;
    - 正确样例(expected_valid=True)通过率 ≥ 80%。
    """
    import json
    from pathlib import Path

    from app.knowledge import extractor
    from app.knowledge.extractor import evaluate_kg_candidate
    from app.knowledge.models import RelationType

    gold_path = Path(__file__).parent / "fixtures" / "kg_gold.json"
    samples = json.loads(gold_path.read_text(encoding="utf-8"))
    assert len(samples) >= 60

    relations_covered = {item["relation_type"] for item in samples}
    assert relations_covered == {r.value for r in RelationType}

    # 只检验离线规则实现，不宣称是独立模型或人工法律准确率。
    monkeypatch.setattr(extractor, "_call_llm_judge", lambda *a, **kw: None)

    pos_total = 0
    pos_passed = 0
    neg_total = 0
    neg_rejected = 0

    for item in samples:
        res = await evaluate_kg_candidate(
            relation_type=item["relation_type"],
            source_type=item["source_type"],
            source_name=item["source_name"],
            target_type=item["target_type"],
            target_name=item["target_name"],
            evidence_quote=item["evidence_quote"],
            clause_original_text=item["clause_original_text"],
            document_title=item["document_title"],
            confidence=0.9,
            use_llm_judge=False,
        )
        if item["expected_valid"]:
            pos_total += 1
            if res.admitted:
                pos_passed += 1
        else:
            neg_total += 1
            if not res.admitted:
                neg_rejected += 1

    assert pos_total >= 30 and neg_total >= 24
    pos_rate = pos_passed / pos_total
    neg_rejection_rate = neg_rejected / neg_total
    assert neg_rejection_rate >= 0.90, f"错误样例拒绝率不足 90%: {neg_rejection_rate:.2%}"
    assert pos_rate >= 0.80, f"正确样例通过率不足 80%: {pos_rate:.2%}"


async def test_knowledge_extraction_glossary_review_points_and_version_linkage(
    client, seed, tmp_path, monkeypatch
):
    from app.core.config import get_settings
    from app.knowledge import extractor, translator
    from app.knowledge.extractor import (
        ClauseExtractionPayload,
        ExtractedEntity,
        ExtractedRelation,
        SemanticJudgeVerdict,
    )
    from app.knowledge.models import NodeType, RelationType

    monkeypatch.setenv("CYSH_ARCHIVE_DIR", str(tmp_path / "archive"))
    get_settings.cache_clear()

    async def _fake_llm_translate(
        original_text, language, glossary_hits, unmatched_terms, **kwargs
    ):
        nums = translator.extract_numeric_tokens(original_text)
        terms_zh = "、".join(h["target_zh"] for h in glossary_hits) or "数据合规"
        nums_str = " / ".join(nums) if nums else ""
        # This fixture exercises persistence, not translation quality. Preserve units too.
        from app.knowledge.numeric_integrity import amounts, dates

        units = " / ".join(
            f"{'$' if currency == 'DOLLAR_UNSPECIFIED' else currency} {value}"
            for currency, value in amounts(original_text)
        )
        date_text = " / ".join(f"{y}年{m}月{d}日" for y, m, d in dates(original_text))
        zh = (
            f"根据本条规定，相关组织与数据处理主体应当严格履行【{terms_zh}】相关的跨境传输、"
            f"告知同意、安全保护与法定申报义务（保留法定数值与编号：{nums_str}；{units}；{date_text}）。"
        )
        return zh, "deepseek-flash", 110, 45, 220

    async def _fake_llm_extract(
        clause_text, clause_number, document_title, publisher_name, country_code
    ):
        low = clause_text.lower()
        entities: list[ExtractedEntity] = []
        relations: list[ExtractedRelation] = []
        if "data intermediary" in low:
            entities.extend(
                [
                    ExtractedEntity(
                        type=NodeType.DATA_CATEGORY,
                        name_zh="受托处理的个人数据",
                        name_orig="personal data",
                    ),
                    ExtractedEntity(
                        type=NodeType.OBLIGATION,
                        name_zh="数据中介机构受托处理豁免与保护义务",
                        name_orig="data intermediary obligation",
                    ),
                ]
            )
            relations.append(
                ExtractedRelation(
                    type=RelationType.EXEMPTS,
                    source=document_title,
                    target="数据中介机构受托处理豁免与保护义务",
                    evidence_quote="a data intermediary processes personal data on behalf of another organisation.",
                    confidence=0.91,
                )
            )
        if "transfer any personal data outside singapore" in low:
            entities.extend(
                [
                    ExtractedEntity(
                        type=NodeType.DATA_CATEGORY,
                        name_zh="个人数据(Personal Data)",
                        name_orig="personal data",
                    ),
                    ExtractedEntity(
                        type=NodeType.OBLIGATION,
                        name_zh="数据出境同等保护与告知同意义务",
                        name_orig="comparable protection and consent obligation",
                    ),
                ]
            )
            relations.extend(
                [
                    ExtractedRelation(
                        type=RelationType.RESTRICTS_CROSS_BORDER,
                        source=document_title,
                        target="个人数据(Personal Data)",
                        evidence_quote="An organisation must not transfer any personal data outside Singapore except in accordance with prescribed requirements to ensure comparable protection",
                        confidence=0.95,
                    ),
                    ExtractedRelation(
                        type=RelationType.REQUIRES,
                        source=document_title,
                        target="数据出境同等保护与告知同意义务",
                        evidence_quote="An organisation must not transfer any personal data outside Singapore except in accordance with prescribed requirements to ensure comparable protection, after obtaining consent of the individual.",
                        confidence=0.93,
                    ),
                ]
            )
        if "autonomous motor vehicle" in low:
            entities.extend(
                [
                    ExtractedEntity(
                        type=NodeType.DATA_CATEGORY,
                        name_zh="自动驾驶车载黑匣子与远程信息处理数据",
                        name_orig="black box telematics data recorder",
                    ),
                    ExtractedEntity(
                        type=NodeType.OBLIGATION,
                        name_zh="自动驾驶公共道路测试事前批准与黑匣子配备义务",
                        name_orig="prior approval and black box data recorder",
                    ),
                ]
            )
            relations.extend(
                [
                    ExtractedRelation(
                        type=RelationType.REQUIRES_ADMISSION,
                        source=document_title,
                        target="自动驾驶公共道路测试事前批准与黑匣子配备义务",
                        evidence_quote="No person shall undertake any trial of an autonomous motor vehicle with an autonomous system on any road without prior approval and a black box telematics data recorder.",
                        confidence=0.94,
                    ),
                    ExtractedRelation(
                        type=RelationType.PROHIBITS,
                        source=document_title,
                        target="自动驾驶公共道路测试事前批准与黑匣子配备义务",
                        evidence_quote="No person shall undertake any trial of an autonomous motor vehicle with an autonomous system on any road without prior approval",
                        confidence=0.92,
                    ),
                ]
            )
        if "notifiable data breach" in low:
            entities.extend(
                [
                    ExtractedEntity(
                        type=NodeType.OBLIGATION,
                        name_zh="数据泄露3个日历日内强制通报义务",
                        name_orig="notify the Commission within 3 calendar days",
                    ),
                    ExtractedEntity(
                        type=NodeType.PENALTY,
                        name_zh="违反通报义务最高处$1,000,000法定罚款",
                        name_orig="liable on conviction to a fine of $1,000,000",
                    ),
                ]
            )
            relations.extend(
                [
                    ExtractedRelation(
                        type=RelationType.REQUIRES,
                        source=document_title,
                        target="数据泄露3个日历日内强制通报义务",
                        evidence_quote="Where a notifiable data breach occurs, the organisation must notify the Commission within 3 calendar days",
                        confidence=0.95,
                    ),
                    ExtractedRelation(
                        type=RelationType.PENALIZES,
                        source=document_title,
                        target="违反通报义务最高处$1,000,000法定罚款",
                        evidence_quote="failing which it is liable on conviction to a fine of $1,000,000.",
                        confidence=0.94,
                    ),
                ]
            )
        if "amends the personal data protection act 2012" in low:
            relations.extend(
                [
                    ExtractedRelation(
                        type=RelationType.AMENDS,
                        source=document_title,
                        target="Personal Data Protection Act 2012",
                        evidence_quote="This Amendment Act amends the Personal Data Protection Act 2012 and repeals outdated transitional exemptions.",
                        confidence=0.96,
                    ),
                    ExtractedRelation(
                        type=RelationType.REPEALS,
                        source=document_title,
                        target="Personal Data Protection Act 2012",
                        evidence_quote="This Amendment Act amends the Personal Data Protection Act 2012 and repeals outdated transitional exemptions.",
                        confidence=0.91,
                    ),
                ]
            )
        return ClauseExtractionPayload(entities=entities, relations=relations), "deepseek-flash"

    async def _fake_llm_judge(*args, **kwargs):
        return SemanticJudgeVerdict(supports="yes", reason="证据片段直接明确支撑该关系")

    monkeypatch.setattr(translator, "_call_llm_translate", _fake_llm_translate)
    monkeypatch.setattr(extractor, "_call_llm_extract_clause", _fake_llm_extract)
    monkeypatch.setattr(extractor, "_call_llm_judge", _fake_llm_judge)

    async def _fake_doc_relevance(**kwargs):
        return extractor.DocumentRelevanceVerdict(relevance="relevant", reason="fixture scope")

    monkeypatch.setattr(extractor, "_call_llm_doc_relevance", _fake_doc_relevance)
    admin_hdr = bearer(await login(client, "admin", "admin@example.com"))
    reviewer_hdr = bearer(await login(client, "admin", "reviewer@example.com"))
    member_hdr = bearer(await login(client, "user", "a-member@example.com"))

    # 1. 创建新加坡站点并上传包含出境、同意、数据中介、自动驾驶路测、罚则与修订条款的真实结构 PDF
    site_resp = await client.post(
        "/api/admin/sites",
        headers=admin_hdr,
        json={
            "country_code": "SG",
            "publisher": "Personal Data Protection Commission (PDPC)",
            "language": "en",
            "document_type": "LAW",
            "entry_url": "https://www.pdpc.gov.sg/overview-of-pdpa",
            "strategy": "DIRECT",
        },
    )
    assert site_resp.status_code == 201
    site_id = site_resp.json()["id"]

    pdf_v1 = make_pdf(
        [
            [
                "1. Short title and definitions",
                "This Act may be cited as the Personal Data Protection Act 2012. "
                "In this Act, personal data means data about an individual who can be identified, "
                "and a data intermediary processes personal data on behalf of another organisation.",
            ],
            [
                "2. Transfer of personal data outside Singapore",
                "An organisation must not transfer any personal data outside Singapore except "
                "in accordance with prescribed requirements to ensure comparable protection, "
                "after obtaining consent of the individual.",
            ],
            [
                "3. Autonomous motor vehicle trial and telematics black box",
                "No person shall undertake any trial of an autonomous motor vehicle with an "
                "autonomous system on any road without prior approval and a black box telematics "
                "data recorder.",
            ],
            [
                "4. Notifiable data breach and financial penalty",
                "Where a notifiable data breach occurs, the organisation must notify the Commission "
                "within 3 calendar days, failing which it is liable on conviction to a fine of $1,000,000.",
            ],
        ]
    )
    up_v1 = await client.post(
        "/api/admin/documents/upload",
        headers=admin_hdr,
        json={
            "site_id": site_id,
            "source_url": "https://sso.agc.gov.sg/Act/PDPA2012/v1.pdf",
            "title": "Personal Data Protection Act 2012",
            "filename": "pdpa_v1.pdf",
            "content_base64": base64.b64encode(pdf_v1).decode("ascii"),
            "official_number": "No. 26 of 2012",
            "published_date": "2012-10-15",
        },
    )
    assert up_v1.status_code == 201
    doc_id = up_v1.json()["id"]

    # 再上传一份修订法以验证 AMENDS / REPEALS 语义边抽取
    pdf_amend = make_pdf(
        [
            [
                "1. Amendment and repeal of provisions",
                "This Amendment Act amends the Personal Data Protection Act 2012 and repeals "
                "outdated transitional exemptions.",
            ]
        ]
    )
    up_amend = await client.post(
        "/api/admin/documents/upload",
        headers=admin_hdr,
        json={
            "site_id": site_id,
            "source_url": "https://sso.agc.gov.sg/Act/PDPA-Amend-2020.pdf",
            "title": "Personal Data Protection (Amendment) Act 2020",
            "filename": "pdpa_amend.pdf",
            "content_base64": base64.b64encode(pdf_amend).decode("ascii"),
        },
    )
    assert up_amend.status_code == 201

    # 2. 律所复核员触发新加坡知识图谱抽取与术语受控翻译
    ext = await client.post(
        "/api/admin/knowledge/extract?country_code=SG&translate_sample_per_doc=10",
        headers=reviewer_hdr,
    )
    assert ext.status_code == 200
    ext_data = ext.json()
    assert ext_data["nodes_total"] >= 6
    assert ext_data["edges_total"] >= 8
    assert ext_data["clauses_translated"] >= 4

    # 3. 检查两层术语库与译文中的术语命中 + 未入库专业词悬浮解释(telematics / black box)
    glossary_resp = await client.get(
        "/api/admin/knowledge/glossary?country_code=SG", headers=reviewer_hdr
    )
    assert glossary_resp.status_code == 200
    terms = glossary_resp.json()
    assert any(
        t["concept_code"] == "data_intermediary" and t["country_code"] == "SG" for t in terms
    )

    tr_resp = await client.get(
        f"/api/admin/knowledge/translations?country_code=SG&document_id={doc_id}",
        headers=reviewer_hdr,
    )
    assert tr_resp.status_code == 200
    translations = tr_resp.json()
    assert len(translations) == 4
    # 未经律所复核前,一律标注"机器翻译，待核对",且 usable_as_legal_basis 为 False
    assert all(not t["usable_as_legal_basis"] for t in translations)
    assert all(t["status_badge"] == "机器翻译，待核对" for t in translations)
    assert all(t["quality_check_passed"] is True for t in translations)
    # 第 3 条包含未入库词 telematics / black box,应输出 unmatched_terms 供前端悬浮解释
    clause3_tr = next(t for t in translations if t["clause_number"] == "3")
    unmatched_names = {u["source_term"] for u in clause3_tr["unmatched_terms"]}
    assert "telematics" in unmatched_names and "black box" in unmatched_names

    # 4. 律所复核员复核通过第 2 条(跨境传输条款)译文
    clause2_tr = next(t for t in translations if t["clause_number"] == "2")
    rev_tr = await client.post(
        f"/api/admin/knowledge/translations/{clause2_tr['id']}/review",
        headers=reviewer_hdr,
        json={
            "decision": "approved",
            "reviewer_note": "已核对新加坡 PDPA 第 26 条个人数据出境同等保护义务译法",
        },
    )
    assert rev_tr.status_code == 200
    assert rev_tr.json()["usable_as_legal_basis"] is True
    assert rev_tr.json()["status_badge"] == "已复核(可作法律依据)"

    # 5. 检查《解读要点确认单》导出与律所回执批量导入
    exp_resp = await client.get(
        "/api/admin/knowledge/review-points/export?country_code=SG", headers=reviewer_hdr
    )
    assert exp_resp.status_code == 200
    assert "SG-RP-001" in exp_resp.json()["markdown"]

    imp_resp = await client.post(
        "/api/admin/knowledge/review-points/import",
        headers=reviewer_hdr,
        json={
            "items": [
                {
                    "point_code": "SG-RP-001",
                    "status": "confirmed",
                    "lawyer_reply": "确认:凡保留 VIN 且后台可关联车主的遥测与轨迹数据均须按个人数据履行第 26 条出境合规。",
                }
            ]
        },
    )
    assert imp_resp.status_code == 200
    assert imp_resp.json()["updated"] == 1

    # 6. 检查六要素+十二关系知识图谱(管理端与用户端)
    graph_resp = await client.get("/api/knowledge/graph?country_code=SG", headers=member_hdr)
    assert graph_resp.status_code == 200
    graph = graph_resp.json()
    for ntype in ("COUNTRY", "REGULATOR", "INSTRUMENT", "DATA_CATEGORY", "OBLIGATION", "PENALTY"):
        assert graph["node_counts_by_type"][ntype] >= 1
    assert len(graph["edges"]) >= 8
    # 每条语义边都必须带非空 evidence_clause_id 与 evidence_quote
    assert all(e["evidence_clause_id"] and e["evidence_quote"] for e in graph["edges"])

    # 7. 验证版本更新联动(上传该文档 v2:第 2 条未变,第 4 条罚款金额从 $1,000,000 改为 $2,000,000)
    pdf_v2 = make_pdf(
        [
            [
                "1. Short title and definitions",
                "This Act may be cited as the Personal Data Protection Act 2012. "
                "In this Act, personal data means data about an individual who can be identified, "
                "and a data intermediary processes personal data on behalf of another organisation.",
            ],
            [
                "2. Transfer of personal data outside Singapore",
                "An organisation must not transfer any personal data outside Singapore except "
                "in accordance with prescribed requirements to ensure comparable protection, "
                "after obtaining consent of the individual.",
            ],
            [
                "3. Autonomous motor vehicle trial and telematics black box",
                "No person shall undertake any trial of an autonomous motor vehicle with an "
                "autonomous system on any road without prior approval and a black box telematics "
                "data recorder.",
            ],
            [
                "4. Notifiable data breach and enhanced financial penalty",
                "Where a notifiable data breach occurs, the organisation must notify the Commission "
                "within 3 calendar days, failing which it is liable on conviction to a fine of $2,000,000.",
            ],
        ]
    )
    up_v2 = await client.post(
        "/api/admin/documents/upload",
        headers=admin_hdr,
        json={
            "document_id": doc_id,
            "filename": "pdpa_v2.pdf",
            "content_base64": base64.b64encode(pdf_v2).decode("ascii"),
            "version_label": "2024 Revised",
        },
    )
    assert up_v2.status_code == 201
    assert up_v2.json()["latest_version"]["version_no"] == 2

    # 检查译文列表:未变的第 2 条在新版本上自动复用了 approved 译文;被修改的旧第 4 条译文变为 stale
    tr_after = (
        await client.get(
            f"/api/admin/knowledge/translations?country_code=SG&document_id={doc_id}",
            headers=reviewer_hdr,
        )
    ).json()
    approved_v2 = [
        t for t in tr_after if t["clause_number"] == "2" and t["review_status"] == "approved"
    ]
    stale_v1 = [t for t in tr_after if t["clause_number"] == "4" and t["review_status"] == "stale"]
    assert len(approved_v2) >= 2  # v1 + 自动继承的 v2
    assert len(stale_v1) == 1
