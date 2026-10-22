import asyncio
import json

import pytest

from .conftest import bearer, login

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
        findings = []
        retry = "verifier_feedback_must_fix" in payload
        for c in payload["candidate_clauses"][:1]:
            quote = c["text"][:80]
            if bad_first and not retry:
                quote = "这段引用并不在条款原文里,应被程序核验拦截并退回"
            findings.append(
                {
                    "business_fact": "跨境回传",
                    "clause_id": c["clause_id"],
                    "obligation": "须确保境外接收方提供可比保护",
                    "risk_level": "HIGH",
                    "quote": quote,
                    "quote_zh": "(测试译文)",
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
            return r.json()
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
    body = {"dsl_payload": dsl, "change_note": "单测", "activate_immediately": True}
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
    assert d["findings"] == [] and d["step_traces"] == []


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
    assert back["is_backtrack"] is True and "连续子串" in back["backtrack_reason"]
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
    assert done["status"] == "partial_sample"
    assert done["findings"] == []
    report = done["report_versions"][0]
    assert all(d["citations"] == [] for d in report["dimensions"])
    assert any("模型调用失败" in (d["missing_gap_note"] or "") for d in report["dimensions"])


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

    ok, _ = verify_finding(
        {"quote": PENALTY_TEXT[40:140], "obligation": "最高罚款 10%"}, PENALTY_TEXT
    )
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
    assert "信息已经齐备" in _last_assistant(d)["content"]
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
