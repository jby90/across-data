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


async def test_knowledge_extraction_glossary_review_points_and_version_linkage(
    client, seed, tmp_path, monkeypatch
):
    from app.core.config import get_settings

    monkeypatch.setenv("CYSH_ARCHIVE_DIR", str(tmp_path / "archive"))
    get_settings.cache_clear()
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
