from .conftest import bearer, login


async def test_admin_agent_studio_workflow_export_import_and_readonly_lock(client, seed):
    admin_hdr = bearer(await login(client, "admin", "admin@example.com"))

    # 1. 获取默认多智能体编排配置
    wf_resp = await client.get("/api/admin/agent-studio/workflow", headers=admin_hdr)
    assert wf_resp.status_code == 200
    wf = wf_resp.json()
    assert wf["readonly_locked"] is True
    assert wf["version_no"] >= 1
    assert len(wf["nodes"]) == 7
    assert len(wf["edges"]) == 7
    # 验证包含退回补证回流边
    assert any(e["edge_type"] == "backtrack" for e in wf["edges"])
    # 验证每个节点都处于只读锁定态
    assert all(n["readonly_locked"] is True for n in wf["nodes"])

    # 2. 导出 Dify 兼容 DSL 配置包
    exp_resp = await client.get("/api/admin/agent-studio/workflow/export", headers=admin_hdr)
    assert exp_resp.status_code == 200
    dsl = exp_resp.json()
    assert dsl["kind"] == "cyshuhe.agent_workflow"
    assert dsl["security_policy"]["readonly_locked"] is True
    assert len(dsl["workflow"]["graph"]["nodes"]) == 7

    # 3. 尝试在导入的配置包中将某个节点改为 readonly_locked=False，验证后端强制恢复为 True
    dsl["workflow"]["graph"]["nodes"][0]["readonly_locked"] = False
    dsl["app"]["name"] = "汽车数据跨境八维度合规评估多智能体工作流 (维护升级版)"
    imp_resp = await client.post(
        "/api/admin/agent-studio/workflow/import",
        headers=admin_hdr,
        json={
            "dsl_payload": dsl,
            "change_note": "单测导入新版本验证",
            "activate_immediately": True,
        },
    )
    assert imp_resp.status_code == 201
    imported = imp_resp.json()
    assert imported["version_no"] == wf["version_no"] + 1
    assert imported["is_active"] is True
    assert imported["readonly_locked"] is True
    assert all(n["readonly_locked"] is True for n in imported["nodes"])

    # 4. 查看版本列表并切回 v1
    vers_resp = await client.get("/api/admin/agent-studio/workflow/versions", headers=admin_hdr)
    assert vers_resp.status_code == 200
    assert len(vers_resp.json()) >= 2

    act_resp = await client.post(
        "/api/admin/agent-studio/workflow/versions/1/activate", headers=admin_hdr
    )
    assert act_resp.status_code == 200
    assert act_resp.json()["version_no"] == 1
    assert act_resp.json()["is_active"] is True


async def test_user_multi_agent_analysis_followup_backtrack_report_and_export(client, seed):
    member_hdr = bearer(await login(client, "user", "a-member@example.com"))
    admin_hdr = bearer(await login(client, "admin", "admin@example.com"))

    # 1. 普通企业成员列出任务（含预置新加坡完整报告、待追问任务与四国部分样稿）
    list_resp = await client.get("/api/analysis/tasks", headers=member_hdr)
    assert list_resp.status_code == 200
    tasks = list_resp.json()
    assert len(tasks) >= 6

    # 2. 创建一个仅填 1 项事实的新加坡任务，验证主动追问机制拦截（不直接给结论）
    create_inc = await client.post(
        "/api/analysis/tasks",
        headers=member_hdr,
        json={
            "title": "新加坡自动驾驶测试车队追问验证任务",
            "country_code": "SG",
            "vehicle_model": "CYS-AV-Test",
            "scenario_type": "autonomous_trial",
            "facts": {
                "exterior_video_and_gnss": "采集 8 路环视视频与厘米级 GNSS，车端对人脸/车牌实时模糊处理"
            },
            "auto_run_if_sufficient": True,
        },
    )
    assert create_inc.status_code == 201
    inc_data = create_inc.json()
    task_id = inc_data["id"]
    assert inc_data["status"] == "collecting_facts"
    assert inc_data["sufficiency_score"] == 20
    assert len(inc_data["findings"]) == 0
    assert sum(1 for m in inc_data["missing_facts"] if not m["is_fulfilled"]) == 4

    # 3. 补充剩余 4 项必填事实，验证达到 100% 后自动运行多智能体协同流水线
    fill_resp = await client.post(
        f"/api/analysis/tasks/{task_id}/facts",
        headers=member_hdr,
        json={
            "facts": {
                "cross_border_mechanism": "传输至中国研发中心，已签署对标 PDPA 第三附表保护标准的 DPA 协议",
                "consent_and_notice": "车机弹窗明示同意 + 车身张贴路测告示 + 已任命新加坡本地 DPO",
                "retention_and_blackbox": "LTA 黑匣子记录留存 3 年；常规个人数据满 180 天自动销毁",
                "local_admission_and_safety_operator": "已提交 LTA 路测准入申请、配备持证安全员并投保责任险",
            },
            "auto_run_if_sufficient": True,
        },
    )
    assert fill_resp.status_code == 200
    done_data = fill_resp.json()
    assert done_data["sufficiency_score"] == 100
    assert done_data["status"] in ("completed", "partial_sample")
    assert len(done_data["findings"]) == 6
    assert len(done_data["step_traces"]) == 7
    # 验证包含『编制/质检退回分析补充依据』真实记录
    assert any(st["is_backtrack"] is True for st in done_data["step_traces"])
    assert len(done_data["report_versions"]) == 1
    assert len(done_data["report_versions"][0]["dimensions"]) == 8

    # 4. 在线编辑报告章节生成 v2 版本，并对比 v1 与 v2 版本差异
    edit_resp = await client.post(
        f"/api/analysis/tasks/{task_id}/reports/edit",
        headers=member_hdr,
        json={
            "change_note": "增补 D3 跨境加密通道说明",
            "executive_summary": "修订后的执行摘要：已完成全链路合规核查。",
            "dimension_updates": {
                "D3": "### 3. 跨境回传研发云的合法路径（v2 人工复核增补版）\n已签署 ASEAN MCCs 并启用双向 mTLS 加密。"
            },
        },
    )
    assert edit_resp.status_code == 200
    assert edit_resp.json()["version_no"] == 2

    diff_resp = await client.get(
        f"/api/analysis/tasks/{task_id}/reports/diff?from_version=1&to_version=2",
        headers=member_hdr,
    )
    assert diff_resp.status_code == 200
    diff_data = diff_resp.json()
    assert diff_data["summary_changed"] is True
    assert any(
        d["dimension_code"] == "D3" and d["changed"] is True for d in diff_data["dimension_diffs"]
    )

    # 5. 验证导出 Word (.docx) 与 PDF (.pdf)
    docx_resp = await client.get(
        f"/api/analysis/tasks/{task_id}/reports/2/export.docx",
        headers=member_hdr,
    )
    assert docx_resp.status_code == 200
    assert docx_resp.content[:2] == b"PK"

    pdf_resp = await client.get(
        f"/api/analysis/tasks/{task_id}/reports/2/export.pdf",
        headers=member_hdr,
    )
    assert pdf_resp.status_code == 200
    assert pdf_resp.content[:4] == b"%PDF"

    # 6. 验证 10 道独立评测题接口
    bm_resp = await client.get("/api/admin/agent-studio/benchmarks", headers=admin_hdr)
    assert bm_resp.status_code == 200
    bm = bm_resp.json()
    assert bm["total_cases"] == 10
    assert bm["dimension_completeness_pct"] == 100.0
