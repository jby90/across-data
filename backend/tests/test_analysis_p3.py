import asyncio
import json

import pytest

from .conftest import bearer, login
from .helpers import admin_task_detail

TRANSFER_TEXT = (
    "An organisation must not transfer any personal data to a country or territory outside "
    "Singapore except in accordance with requirements prescribed under this Act to ensure that "
    "organisations provide a standard of protection comparable to that under this Act."
)
PENALTY_TEXT = (
    "Where the Commission is satisfied that an organisation has intentionally or negligently "
    "contravened a data protection provision it may require the organisation to pay a financial "
    "penalty not exceeding 10% of the annual turnover of the organisation in Singapore or "
    "$1 million, whichever is higher."
)
TOC_TEXT = "Personal data .................... 11 Transfer .................... 36 " * 3

FACTS = {
    "exterior_video_and_gnss": "采集 8 路环视视频与 GNSS,车端对人脸/车牌实时模糊处理后回传",
    "cross_border_mechanism": "传输至中国研发中心,已签署跨境数据处理协议并完成评估",
    "consent_and_notice": "车机弹窗明示同意 + 车身告示 + 已任命本地数据保护官",
    "retention_and_blackbox": "黑匣子记录留存 3 年;常规个人数据 180 天后销毁",
    "local_admission_and_safety_operator": "已提交路测准入申请并配备安全员与保险",
}


async def _seed_sg_corpus(session_factory):
    from app.ingestion.models import Clause, Document, DocumentVersion
    from app.sources.models import SourceSite

    async with session_factory() as s:
        site = SourceSite(
            country_code="SG",
            publisher="PDPC",
            language="en",
            document_type="LAW",
            entry_url="https://example.sg/pdpa",
            strategy="DIRECT",
            config={},
        )
        s.add(site)
        await s.flush()
        doc = Document(
            site_id=site.id,
            country_code="SG",
            source_url="https://example.sg/pdpa.pdf",
            title="Personal Data Protection Act 2012",
            language="en",
            document_type="LAW",
        )
        s.add(doc)
        await s.flush()
        ver = DocumentVersion(
            document_id=doc.id,
            version_no=1,
            content_hash="a" * 64,
            archive_path="x",
            size_bytes=1,
            mime_type="application/pdf",
            final_url="https://example.sg/pdpa.pdf",
            state="parsed",
        )
        s.add(ver)
        await s.flush()
        for i, (num, text) in enumerate(
            [("26", TRANSFER_TEXT), ("48J", PENALTY_TEXT), (None, TOC_TEXT)]
        ):
            s.add(
                Clause(
                    version_id=ver.id,
                    ordinal=i,
                    clause_number=num,
                    original_text=text,
                    text_hash=str(i) * 64,
                    language="en",
                )
            )
        await s.commit()


def _fake_llm_factory(bad_first: bool):
    calls = {"n": 0}

    async def fake(messages, **_kw):
        from app.analysis.llm import LlmResult

        calls["n"] += 1
        payload = json.loads(messages[1]["content"])
        if payload.get("phase") == "verify_source_support":
            return LlmResult(
                {"supported": "yes", "reason": "Fixture source fully supports this rule"},
                100,
                50,
                7,
                "fake-model",
            )
        if payload.get("phase") == "write_from_evidence":
            return LlmResult(
                data={
                    "obligation": "须确保境外接收方提供可比保护",
                    "risk_level": "HIGH",
                    "gap": "",
                },
                prompt_tokens=100,
                completion_tokens=50,
                duration_ms=7,
                model="fake-model",
            )
        findings = []
        retry = "verifier_feedback_must_fix" in payload
        for c in payload["candidate_clauses"][:1]:
            sentence_ids = [999] if bad_first and not retry else [1]
            findings.append(
                {
                    "fact_key": next(iter(payload["business_facts"])),
                    "clause_id": c["clause_id"],
                    "obligation": "须确保境外接收方提供可比保护",
                    "risk_level": "HIGH",
                    "sentence_ids": sentence_ids,
                }
            )
        return LlmResult(
            data={"summary": "测试小结", "findings": findings, "recommendations": ["整改"]},
            prompt_tokens=100,
            completion_tokens=50,
            duration_ms=7,
            model="fake-model",
        )

    return fake


async def _wait_done(client, hdr, task_id, timeout=20):
    for _ in range(timeout * 10):
        r = await client.get(f"/api/analysis/tasks/{task_id}", headers=hdr)
        assert r.status_code == 200
        if r.json()["status"] != "running":
            assert "step_traces" not in r.json()
            return await admin_task_detail(client, task_id)
        await asyncio.sleep(0.1)
    raise AssertionError("分析任务超时未结束")


async def test_workflow_config_export_import_and_admin_only(client, seed):
    admin = bearer(await login(client, "admin", "admin@example.com"))
    reviewer = bearer(await login(client, "admin", "reviewer@example.com"))

    wf = (await client.get("/api/admin/agent-studio/workflow", headers=admin)).json()
    assert wf["readonly_locked"] is True and len(wf["nodes"]) == 7
    assert any(e["edge_type"] == "backtrack" for e in wf["edges"])
    # 节点说明必须和真实实现一致:不再声称调用未实现的工具
    names = {t["tool_name"] for n in wf["nodes"] for t in n["mcp_tools"]}
    assert "write_analysis_findings" not in names

    dsl = (await client.get("/api/admin/agent-studio/workflow/export", headers=admin)).json()
    dsl["workflow"]["graph"]["nodes"][0]["readonly_locked"] = False
    body = {"dsl_payload": dsl, "change_note": "单测", "activate_immediately": False}
    # 复核员可以查看,但不能导入或激活
    deny = await client.post("/api/admin/agent-studio/workflow/import", headers=reviewer, json=body)
    assert deny.status_code == 403
    ok = await client.post("/api/admin/agent-studio/workflow/import", headers=admin, json=body)
    assert ok.status_code == 201
    assert all(n["readonly_locked"] for n in ok.json()["nodes"])
    deny2 = await client.post(
        f"/api/admin/agent-studio/workflow/versions/{wf['version_no']}/activate",
        headers=reviewer,
    )
    assert deny2.status_code == 403


async def test_fact_followup_blocks_analysis_without_model(client, seed):
    hdr = bearer(await login(client, "user", "a-member@example.com"))
    r = await client.post(
        "/api/analysis/tasks",
        headers=hdr,
        json={
            "title": "追问验证",
            "country_code": "SG",
            "facts": {"exterior_video_and_gnss": "采集环视视频与 GNSS 轨迹数据"},
        },
    )
    assert r.status_code == 201
    d = r.json()
    assert d["status"] == "collecting_facts" and d["sufficiency_score"] == 20
    assert d["findings"] == [] and "step_traces" not in d


async def test_pipeline_real_verification_backtrack_and_tenant_isolation(
    client, seed, session_factory, monkeypatch
):
    from app.analysis import pipeline

    await _seed_sg_corpus(session_factory)
    monkeypatch.setattr(pipeline, "call_llm_json", _fake_llm_factory(bad_first=True))
    a_hdr = bearer(await login(client, "user", "a-member@example.com"))
    b_hdr = bearer(await login(client, "user", "b-admin@example.com"))

    created = await client.post(
        "/api/analysis/tasks",
        headers=a_hdr,
        json={"title": "SG 路测评估", "country_code": "SG", "facts": FACTS},
    )
    assert created.status_code == 201
    assert created.json()["status"] == "running"
    task_id = created.json()["id"]
    done = await _wait_done(client, a_hdr, task_id)

    assert done["status"] in ("completed", "partial_sample")
    traces = done["step_traces"]
    phases = [t["phase"] for t in traces]
    # 引用不是原文子串 → 真实触发退回 → 二次分析 → 二次核验
    assert "verification" in phases and "re_analysis" in phases and "re_verification" in phases
    back = next(t for t in traces if t["phase"] == "verification")
    assert back["is_backtrack"] is True and "sentence_id" in back["backtrack_reason"]
    assert done["backtrack_count"] == 1
    # Token 来自模型返回的真实用量,而不是写死的数字
    assert done["total_tokens"] > 0 and done["total_tokens"] % 150 == 0

    # 通过核验的结论,引用必须真是条款原文的子串,且条号来自真实条款
    assert done["findings"]
    for f in done["findings"]:
        assert f["verification_status"] == "VERIFIED_SUBSTRING"
        assert f["basis_quote"] in (TRANSFER_TEXT, PENALTY_TEXT) or any(
            f["basis_quote"] in t for t in (TRANSFER_TEXT, PENALTY_TEXT)
        )
        assert f["clause_number"] in ("26", "48J")
    # 目录页被过滤,不可能被引用
    assert all("......" not in f["basis_quote"] for f in done["findings"])

    report = done["report_versions"][0]
    assert len(report["dimensions"]) == 8
    # 缺依据的维度如实标注缺口,没有编造
    gaps = [d for d in report["dimensions"] if d["missing_gap_note"]]
    assert gaps and report["is_partial_sample"] is True
    assert report["stale_impacts"] == []

    # 另一家企业看不到、也打不开该任务
    assert (await client.get("/api/analysis/tasks", headers=b_hdr)).json() == []
    assert (await client.get(f"/api/analysis/tasks/{task_id}", headers=b_hdr)).status_code == 404

    # 导出
    docx = await client.get(f"/api/analysis/tasks/{task_id}/reports/1/export.docx", headers=a_hdr)
    assert docx.status_code == 200 and docx.content[:2] == b"PK"
    pdf = await client.get(f"/api/analysis/tasks/{task_id}/reports/1/export.pdf", headers=a_hdr)
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"


async def test_model_unavailable_is_reported_not_fabricated(client, seed, session_factory):
    await _seed_sg_corpus(session_factory)
    hdr = bearer(await login(client, "user", "a-member@example.com"))
    created = await client.post(
        "/api/analysis/tasks",
        headers=hdr,
        json={"title": "无模型", "country_code": "SG", "facts": FACTS},
    )
    done = await _wait_done(client, hdr, created.json()["id"])
    assert done["status"] == "failed"
    assert done["findings"] == []
    report = done["report_versions"][0]
    assert report["status"] == "analysis_failed" and not report["is_partial_sample"]
    assert all(d["citations"] == [] for d in report["dimensions"])
    assert any(d["gap_kind"] == "analysis_error" for d in report["dimensions"])
    for suffix in ("docx", "pdf"):
        response = await client.get(
            f"/api/analysis/tasks/{done['id']}/reports/1/export.{suffix}",
            headers=hdr,
        )
        assert response.status_code == 409
    edited = await client.post(
        f"/api/analysis/tasks/{done['id']}/reports/edit",
        headers=hdr,
        json={"executive_summary": "仅编辑文字不能解除技术失败", "change_note": "回归验证"},
    )
    assert edited.status_code == 200 and edited.json()["status"] == "analysis_failed"
    assert (
        await client.get(
            f"/api/analysis/tasks/{done['id']}/reports/2/export.docx",
            headers=hdr,
        )
    ).status_code == 409


async def test_retrieval_benchmark_is_honest(client, seed, session_factory):
    await _seed_sg_corpus(session_factory)
    admin = bearer(await login(client, "admin", "admin@example.com"))
    bm = (await client.get("/api/admin/agent-studio/benchmarks", headers=admin)).json()
    assert bm["total_cases"] == 10
    assert "人工" in bm["human_review_status"] and "不是法律准确率" in bm["note"]
    by_id = {c["question_id"]: c for c in bm["cases"]}
    assert by_id["H01"]["passed"] is True  # 迁移条款真实命中
    assert by_id["H04"]["passed"] is True  # 罚则条款真实命中
    assert by_id["H07"]["passed"] is False  # 种子库里没有突尼斯法源,不能"通过"


@pytest.mark.parametrize(
    "text", [TOC_TEXT, "short", "?!?! 12 34 .. -- ## @@ 99 88 77 66 55 44 33" * 3]
)
def test_unusable_clause_text_is_filtered(text):
    from app.analysis.tools import is_usable_clause_text

    assert is_usable_clause_text(text) is False


def test_verify_finding_checks_substring_and_numbers():
    from app.analysis.tools import verify_finding

    ok, _ = verify_finding({"quote": PENALTY_TEXT, "obligation": "最高罚款 10%"}, PENALTY_TEXT)
    assert ok is True
    bad, issues = verify_finding(
        {"quote": PENALTY_TEXT[40:140], "obligation": "最高罚款 500 万"}, PENALTY_TEXT
    )
    assert bad is False and any("500" in i for i in issues)
    bad2, issues2 = verify_finding(
        {"quote": "不存在的引用片段不存在的引用片段", "obligation": ""}, PENALTY_TEXT
    )
    assert bad2 is False and any("连续子串" in i for i in issues2)


async def _send(client, hdr, task_id, text):
    r = await client.post(
        f"/api/analysis/tasks/{task_id}/messages", headers=hdr, json={"content": text}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _last_assistant(detail):
    return [m for m in detail["messages"] if m["role"] == "assistant"][-1]


async def test_chat_collects_everything_then_runs_and_reports(
    client, seed, session_factory, monkeypatch
):
    from app.analysis import pipeline

    await _seed_sg_corpus(session_factory)
    monkeypatch.setattr(pipeline, "call_llm_json", _fake_llm_factory(bad_first=False))
    hdr = bearer(await login(client, "user", "a-member@example.com"))
    other = bearer(await login(client, "user", "b-admin@example.com"))

    created = await client.post("/api/analysis/chats", headers=hdr)
    assert created.status_code == 201
    d = created.json()
    tid = d["id"]
    assert d["status"] == "collecting_facts"
    assert [m["role"] for m in d["messages"]] == ["assistant", "assistant"]
    q = _last_assistant(d)
    assert q["slot"] == "country_code" and len(q["options"]) == 5

    # 答非所问:不推进,重新追问同一个问题
    d = await _send(client, hdr, tid, "随便聊聊天气")
    q = _last_assistant(d)
    assert q["slot"] == "country_code" and "没有识别出国家" in q["content"]

    d = await _send(client, hdr, tid, "新加坡 (SG)")
    assert d["country_code"] == "SG" and _last_assistant(d)["slot"] == "vehicle_model"
    d = await _send(client, hdr, tid, "CYS-Pilot L4 自动驾驶 SUV")
    assert _last_assistant(d)["slot"] == "scenario_type"
    d = await _send(client, hdr, tid, "L3/L4 自动驾驶公共道路测试与远程监控回传")
    assert d["scenario_type"] == "autonomous_trial"
    assert "CYS-Pilot" in d["title"]
    assert _last_assistant(d)["slot"] == "exterior_video_and_gnss"

    # 太简短的回答被要求补充,不会被当作事实
    d = await _send(client, hdr, tid, "有")
    assert "简略" in _last_assistant(d)["content"]
    assert d["facts"] == {}

    for text in FACTS.values():
        d = await _send(client, hdr, tid, text)
    assert d["status"] == "running"
    assert "信息已齐备" in _last_assistant(d)["content"]
    # 运行中再发消息:只提示进度,不重复启动
    d = await _send(client, hdr, tid, "好了吗")
    assert "进行中" in _last_assistant(d)["content"]

    done = await _wait_done(client, hdr, tid)
    assert done["status"] in ("completed", "partial_sample")
    assert done["report_versions"] and done["findings"]
    d = await _send(client, hdr, tid, "谢谢")
    assert "报告已经生成" in _last_assistant(d)["content"]

    # 其他企业无法读取或写入这个对话
    deny = await client.post(
        f"/api/analysis/tasks/{tid}/messages", headers=other, json={"content": "x"}
    )
    assert deny.status_code == 404


async def test_chat_llm_extraction_must_quote_user_words(client, seed, monkeypatch):
    import types

    from app.analysis import chat

    text = "我们打算把 CYS-Pilot L4 自动驾驶 SUV 拿到新加坡做公共道路自动驾驶路测,请帮忙评估合规"

    async def fake(messages, **_kw):
        from app.analysis.llm import LlmResult

        return LlmResult(
            data={
                "slots": {
                    "country_code": "新加坡",
                    "vehicle_model": "CYS-Pilot L4 自动驾驶 SUV",
                    "scenario_type": "公共道路自动驾驶路测",
                    "retention_and_blackbox": "这句话用户根本没说过",
                }
            },
            prompt_tokens=1,
            completion_tokens=1,
            duration_ms=1,
            model="fake",
        )

    monkeypatch.setattr(chat, "call_llm_json", fake)
    monkeypatch.setattr(chat, "get_settings", lambda: types.SimpleNamespace(llm_api_key="x"))
    hdr = bearer(await login(client, "user", "a-member@example.com"))
    created = (await client.post("/api/analysis/chats", headers=hdr)).json()
    d = await _send(client, hdr, created["id"], text)
    # 一句话里回答了 3 个问题,助手直接跳到第一个事实问题
    assert d["country_code"] == "SG" and d["scenario_type"] == "autonomous_trial"
    assert d["vehicle_model"] == "CYS-Pilot L4 自动驾驶 SUV"
    assert _last_assistant(d)["slot"] == "exterior_video_and_gnss"
    # 模型编造的、不在用户原话里的片段被丢弃
    assert "retention_and_blackbox" not in d["facts"]


def test_model_json_with_bad_backslash_escape_is_repaired():
    from app.analysis.llm import parse_model_json

    raw = '{"summary": "路径 C:\\data 与 \\u4e2d 文", "findings": []}'
    data = parse_model_json(raw)
    assert data["summary"].startswith("路径 C:\\data")
    assert parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}


async def test_conversation_id_admin_history_live_monitor_and_stage_outputs(
    client, seed, session_factory, monkeypatch
):
    from app.analysis import pipeline
    from app.analysis.schemas import compute_active_node

    await _seed_sg_corpus(session_factory)
    monkeypatch.setattr(pipeline, "call_llm_json", _fake_llm_factory(bad_first=True))

    user_hdr = bearer(await login(client, "user", "a-member@example.com"))
    admin_hdr = bearer(await login(client, "admin", "admin@example.com"))
    reviewer_hdr = bearer(await login(client, "admin", "reviewer@example.com"))

    # 1. 新建对话即返回唯一可读的 conversation_code,且问候消息中包含该对话 ID
    chat_res = (await client.post("/api/analysis/chats", headers=user_hdr)).json()
    conv_code = chat_res["conversation_code"]
    assert conv_code.startswith("CONV-") and len(conv_code) >= 18
    assert conv_code in chat_res["messages"][0]["content"]
    assert "current_active_node_id" not in chat_res
    internal = await admin_task_detail(client, chat_res["id"])
    assert internal["current_active_node_id"] == "node_fact_collector"

    # 2. 发起完整评估工作流并等待完成
    run_res = (
        await client.post(
            "/api/analysis/tasks",
            headers=user_hdr,
            json={"title": "管理端工作流监控测试", "country_code": "SG", "facts": FACTS},
        )
    ).json()
    run_id = run_res["id"]
    run_code = run_res["conversation_code"]
    assert run_code.startswith("CONV-")
    done = await _wait_done(client, user_hdr, run_id)
    assert done["status"] in ("completed", "partial_sample")

    # 3. 管理员端可检索与筛选对话工作流历史(按 conversation_code / 状态 / 国家)
    all_admin_tasks = (await client.get("/api/admin/agent-studio/tasks", headers=admin_hdr)).json()
    codes = {t["conversation_code"] for t in all_admin_tasks}
    assert conv_code in codes and run_code in codes

    filtered = (
        await client.get(f"/api/admin/agent-studio/tasks?keyword={run_code}", headers=admin_hdr)
    ).json()
    assert len(filtered) == 1 and filtered[0]["id"] == run_id
    assert filtered[0]["org_name"] != ""
    assert filtered[0]["creator_email"] == "a-member@example.com"
    assert filtered[0]["steps_count"] >= 6

    # 4. 展开查看每个环节的产出:验证各阶段 output_snapshot 包含完整结构化交付物
    detail = (await client.get(f"/api/admin/agent-studio/tasks/{run_id}", headers=admin_hdr)).json()
    by_phase = {tr["phase"]: tr for tr in detail["step_traces"]}
    assert "fact_check_items" in by_phase["fact_check"]["output_snapshot"]
    assert "retrieved_clauses_by_dimension" in by_phase["retrieval"]["output_snapshot"]
    assert "dimension_outputs" in by_phase["analysis"]["output_snapshot"]
    assert "verification_items" in by_phase["verification"]["output_snapshot"]
    assert "dimension_outputs" in by_phase["re_analysis"]["output_snapshot"]
    assert "dimensions_deliverables" in by_phase["compilation"]["output_snapshot"]
    assert "stale_impacts" in by_phase["update_linkage"]["output_snapshot"]

    # 5. 验证实时运行阶段活跃节点推导逻辑
    assert compute_active_node("running", [])[0] == "node_fact_collector"
    assert (
        compute_active_node("running", [{"phase": "fact_check", "status": "passed"}])[0]
        == "node_mcp_retriever"
    )
    assert (
        compute_active_node("running", [{"phase": "retrieval", "status": "passed"}])[0]
        == "node_dimension_analyst"
    )
    assert (
        compute_active_node("running", [{"phase": "analysis", "status": "passed"}])[0]
        == "node_citation_verifier"
    )
    assert (
        compute_active_node(
            "running",
            [{"phase": "verification", "status": "backtracked", "is_backtrack": True}],
        )[0]
        == "node_dimension_analyst"
    )

    # 6. 管理员可删除测试对话记录(复核员无删除权限)
    deny_del = await client.delete(
        f"/api/admin/agent-studio/tasks/{chat_res['id']}", headers=reviewer_hdr
    )
    assert deny_del.status_code == 403
    ok_del = await client.delete(
        f"/api/admin/agent-studio/tasks/{chat_res['id']}", headers=admin_hdr
    )
    assert ok_del.status_code == 200 and ok_del.json()["deleted"] is True
