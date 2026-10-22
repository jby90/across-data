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
        assert tr_ok.prompt_version == "translate-v2"
        assert tr_ok.tokens_in == 120 and tr_ok.tokens_out == 48 and tr_ok.duration_ms == 350

        all_rows = (
            await s.execute(
                select(ClauseTranslation).where(ClauseTranslation.clause_id == clause.id)
            )
        ).scalars().all()
        assert any(r.engine == "glossary_assisted" and r.review_status == "superseded" for r in all_rows)

        # 场景 B: 假 LLM 返回漏数字/漏术语/原文 -> 重译 1 次(共 2 次调用) -> 仍失败标 translation_failed
        calls_fail = 0

        async def fake_bad(text, lang, hits, unmatched, **kwargs):
            nonlocal calls_fail
            calls_fail += 1
            # 故意漏掉数字 3 与 1,000,000 且不写“个人数据”
            return ("第 26 条：组织必须在若干日内保护私人资料，否则处以高额罚款。", "deepseek-flash", 80, 30, 200)

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
                return ("第 26 条：组织必须保护个人数据，否则处以 $1,000,000 罚款。", "deepseek-flash", 90, 35, 180)
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
                return ("第 1 款：组织必须依照第 24 条保护个人数据。", "deepseek-flash", 100, 30, 150)
            return (
                "第 2 款：任何泄露必须在 3 日内通报，最高处以 $1,000,000 罚款，并保护个人数据与第 24 条。",
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


async def test_knowledge_extraction_glossary_review_points_and_version_linkage(
    client, seed, tmp_path, monkeypatch
):
    from app.core.config import get_settings
    from app.knowledge import translator

    monkeypatch.setenv("CYSH_ARCHIVE_DIR", str(tmp_path / "archive"))
    get_settings.cache_clear()

    async def _fake_llm_translate(original_text, language, glossary_hits, unmatched_terms, **kwargs):
        nums = translator.extract_numeric_tokens(original_text)
        terms_zh = "、".join(h["target_zh"] for h in glossary_hits) or "数据合规"
        nums_str = " / ".join(nums) if nums else ""
        zh = (
            f"根据本条规定，相关组织与数据处理主体应当严格履行【{terms_zh}】相关的跨境传输、"
            f"告知同意、安全保护与法定申报义务（保留法定数值与编号：{nums_str}）。"
        )
        return zh, "deepseek-flash", 110, 45, 220

    monkeypatch.setattr(translator, "_call_llm_translate", _fake_llm_translate)
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
