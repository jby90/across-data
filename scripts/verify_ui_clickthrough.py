#!/usr/bin/env python3
"""真实浏览器端到端手点模拟自检脚本(Playwright + 本机 Chrome)。

每次功能开发完成后运行本脚本:
- 真实打开 http://localhost:5174 (管理端) 与 http://localhost:5173 (用户端)
- 模拟用户输入账号密码登录、点击左侧菜单、切换页签、筛选国家、打开弹窗、点击复核按钮
- 全程监听:
  1. 任何 HTTP >= 400 响应直接报错
  2. 任何页面 JS 异常(pageerror)直接报错
  3. 任何 Element Plus 错误提示框(.el-message--error)直接报错
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import Page, async_playwright

DEV_CREDS = Path.home() / ".cyshuhe-dev" / "credentials.json"


async def _assert_no_error_toast(page: Page, step: str) -> None:
    err_loc = page.locator(".el-message--error")
    count = await err_loc.count()
    if count > 0:
        texts = await err_loc.all_inner_texts()
        raise RuntimeError(f"[{step}] 界面出现错误提示框: {texts}")


async def verify_admin_portal(browser, creds: dict, bad_responses: list[str], page_errors: list[str]) -> None:
    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    page = await context.new_page()
    page.on(
        "response",
        lambda r: bad_responses.append(f"ADMIN {r.status} {r.url}")
        if r.status >= 400 and "/api/" in r.url
        else None,
    )
    page.on("pageerror", lambda e: page_errors.append(f"ADMIN JS ERROR: {e}"))

    print("[1/8] 打开管理端登录页 http://localhost:5174/login ...")
    await page.goto("http://localhost:5174/login", wait_until="networkidle")
    inputs = page.locator("input")
    await inputs.nth(0).fill("admin@example.com")
    await inputs.nth(1).fill(creds["users"]["admin@example.com"])
    await page.get_by_role("button", name="登录").click()
    await page.wait_for_url("http://localhost:5174/", timeout=10000)
    await _assert_no_error_toast(page, "管理端登录")

    print("[2/8] 点击「采集站点」并验证站点列表与联动跳转 ...")
    await page.locator(".el-menu-item", has_text="采集站点").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(500)
    await _assert_no_error_toast(page, "采集站点页")
    site_rows = await page.locator(".el-table__body-wrapper tbody tr").count()
    assert site_rows >= 5, f"采集站点行数不足: {site_rows}"

    print("[3/8] 点击「法规文档库」并检查手动上传弹窗与条款抽屉 ...")
    await page.locator(".el-menu-item", has_text="法规文档库").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(600)
    await _assert_no_error_toast(page, "法规文档库页")
    doc_rows = await page.locator(".el-table__body-wrapper tbody tr").count()
    assert doc_rows >= 10, f"法规文档库行数不足: {doc_rows}"
    await page.get_by_role("button", name="手动上传官方原文").click()
    await page.wait_for_timeout(300)
    await page.get_by_role("button", name="取消").click()

    print("[4/8] 点击「术语与律所复核」并逐一点检三个页签与复核操作 ...")
    await page.locator(".el-menu-item", has_text="术语与律所复核").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(800)
    await _assert_no_error_toast(page, "术语与律所复核-页签1")
    tr_rows = await page.locator(".el-table__body-wrapper tbody tr").count()
    assert tr_rows >= 5, f"条款机翻列表为空: {tr_rows}"

    # 点击第一行"校订/复核"弹窗并确认通过
    await page.get_by_role("button", name="校订/复核").first.click()
    await page.wait_for_timeout(400)
    await page.get_by_role("button", name="复核通过 (作为有效法律依据)").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(500)
    await _assert_no_error_toast(page, "提交条款复核通过")

    # 切换到第 2 页签: 解读要点确认单
    await page.get_by_role("tab", name="2. 解读要点确认单 (仅少数关键争议项)").click()
    await page.wait_for_timeout(400)
    await _assert_no_error_toast(page, "解读要点确认单页签")
    await page.get_by_role("button", name="导出 / 批量回写确认单").click()
    await page.wait_for_timeout(500)
    await _assert_no_error_toast(page, "导出解读要点确认单")
    await page.get_by_role("button", name="批量导入律所回执").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(400)
    await _assert_no_error_toast(page, "批量回写确认单")

    # 切换到第 3 页签: 两层法律术语库
    await page.get_by_role("tab", name="3. 两层法律术语库 (通用概念 + 国家覆盖)").click()
    await page.wait_for_timeout(400)
    await _assert_no_error_toast(page, "两层法律术语库页签")

    print("[5/8] 点击「法规知识图谱」并验证六要素统计、十二关系筛选与图谱渲染 ...")
    await page.locator(".el-menu-item", has_text="法规知识图谱").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(800)
    await _assert_no_error_toast(page, "法规知识图谱页")
    stat_nums = [int(t.strip()) for t in await page.locator(".stat-num").all_inner_texts()]
    assert len(stat_nums) == 6 and all(n > 0 for n in stat_nums), f"六要素卡片存在 0 值: {stat_nums}"
    # ECharts 力导向图：等布局稳定后检查画布非空，并截图留档
    await page.wait_for_timeout(2500)
    box = await page.locator("[data-testid=kg-force-graph] canvas").first.bounding_box()
    assert box and box["width"] > 400 and box["height"] > 300, f"ECharts 画布尺寸异常: {box}"
    await page.screenshot(path=str(Path.home() / ".cyshuhe-dev" / "kg_force.png"))

    # 切换最多显示边数 -> 画布仍应正常
    await page.locator("[data-testid=kg-max-edges]").click()
    await page.locator(".el-select-dropdown__item", has_text="300").last.click()
    await page.wait_for_timeout(1200)

    # 测试「按节点类型显示/隐藏」：点击隐藏枢纽节点（国家/监管机构）并截图
    await page.locator("[data-testid=kg-hide-hub-btn]").click()
    await page.wait_for_timeout(1800)
    tools_text = await page.locator(".graph-tools").inner_text()
    assert "4/6 类节点" in tools_text, f"隐藏枢纽节点后未更新为 4/6 类节点: {tools_text}"
    await page.screenshot(path=str(Path.home() / ".cyshuhe-dev" / "kg_force_no_hubs.png"))

    # 点击六要素统计卡片切换「数据类型」显隐，再点击「显示全部 6 类要素」恢复
    await page.locator("[data-testid=kg-stat-card-DATA_CATEGORY]").click()
    await page.wait_for_timeout(400)
    tools_text_after_card = await page.locator(".graph-tools").inner_text()
    assert "3/6 类节点" in tools_text_after_card, f"点击卡片后未更新为 3/6 类节点: {tools_text_after_card}"
    await page.locator("[data-testid=kg-show-all-types-btn]").click()
    await page.wait_for_timeout(800)
    tools_text_restored = await page.locator(".graph-tools").inner_text()
    assert "6/6 类节点" in tools_text_restored, f"恢复全部要素后未更新为 6/6 类节点: {tools_text_restored}"

    # 点击关系筛选标签与图谱质量体检视图
    await page.locator("[data-testid=kg-quality-pending]").click()
    await page.wait_for_timeout(300)
    await page.locator("[data-testid=kg-quality-all]").click()
    await page.wait_for_timeout(300)
    attr_text = await page.locator("[data-testid=kg-eight-attributes-panel]").inner_text()
    assert "1. 适用行业" in attr_text and "8. 准入材料清单" in attr_text, f"八项属性未完整显示: {attr_text}"

    await page.locator(".el-tag", has_text="数据出境限制").first.click()
    await page.wait_for_timeout(300)
    await page.locator(".el-tag", has_text="全部").first.click()
    await page.wait_for_timeout(300)
    await _assert_no_error_toast(page, "图谱节点类型与关系筛选")

    print("[5.5/10] 点击管理端「多Agent编排工作台」(Dify 风格只读画布与导入导出) ...")
    await page.locator(".el-menu-item", has_text="多Agent编排工作台").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1200)
    await _assert_no_error_toast(page, "多Agent编排工作台加载")

    node_cards = await page.locator("[data-testid=dify-node-card]").count()
    assert node_cards == 7, f"Dify 画布节点数量应为 7，实际为: {node_cards}"
    lock_badge = await page.locator("[data-testid=agent-readonly-lock-badge]").inner_text()
    assert "只读锁定" in lock_badge

    # 点击「依据子串核验与退回质检 Agent」节点，查看右侧检查器与禁用参数
    await page.locator("[data-node-id=node_citation_verifier]").click()
    await page.wait_for_timeout(400)
    insp_title = await page.locator("[data-testid=inspector-node-title]").inner_text()
    assert "核验" in insp_title
    disabled_inputs = await page.locator(".readonly-form-grid input[disabled]").count()
    assert disabled_inputs >= 2, f"节点参数输入框未处于 disabled 禁用只读态: {disabled_inputs}"

    # 切换右侧检查器「MCP 工具」与「运行轨迹」Tab
    await page.get_by_role("tab", name="MCP 工具").click()
    await page.wait_for_timeout(300)
    await page.get_by_role("tab", name="运行轨迹").click()
    await page.wait_for_timeout(300)
    await page.get_by_role("tab", name="配置与连接详情").click()
    await page.wait_for_timeout(300)
    await page.screenshot(path=str(Path.home() / ".cyshuhe-dev" / "admin_agent_studio.png"))

    # 测试导出配置 (JSON DSL)
    await page.locator("[data-testid=btn-export-workflow]").click()
    await page.wait_for_timeout(500)
    dsl_val = await page.locator("textarea[data-testid=export-dsl-textarea], [data-testid=export-dsl-textarea] textarea").first.input_value()
    assert "cyshuhe.agent_workflow" in dsl_val
    await page.get_by_role("button", name="关闭", exact=True).click()
    await page.wait_for_timeout(300)

    # 测试导入配置生成新版本
    await page.locator("[data-testid=btn-import-workflow]").click()
    await page.wait_for_timeout(500)
    await page.locator("[data-testid=btn-confirm-import-workflow]").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(800)
    await _assert_no_error_toast(page, "多Agent配置导入")

    # 打开 10 道独立评测集看板
    await page.locator("[data-testid=btn-open-benchmarks]").click()
    await page.wait_for_timeout(800)
    await _assert_no_error_toast(page, "检索命中评测")

    await context.close()


async def verify_user_portal(browser, creds: dict, bad_responses: list[str], page_errors: list[str]) -> None:
    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    page = await context.new_page()
    page.on(
        "response",
        lambda r: bad_responses.append(f"USER {r.status} {r.url}")
        if r.status >= 400 and "/api/" in r.url
        else None,
    )
    page.on("pageerror", lambda e: page_errors.append(f"USER JS ERROR: {e}"))

    print("[6/8] 打开用户端登录页 http://localhost:5173/login ...")
    await page.goto("http://localhost:5173/login", wait_until="networkidle")
    inputs = page.locator("input")
    await inputs.nth(0).fill("member@example.com")
    await inputs.nth(1).fill(creds["users"]["member@example.com"])
    await page.get_by_role("button", name="登录").click()
    await page.wait_for_url("http://localhost:5173/", timeout=10000)
    await _assert_no_error_toast(page, "用户端登录")

    print("[7/8] 点击用户端「法规知识库」并验证三级目录树、八项属性、左右双栏与义务原文高亮 ...")
    await page.locator(".el-menu-item", has_text="法规知识库").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1200)
    await _assert_no_error_toast(page, "用户端法规知识库")
    cards = await page.locator(".country-card").count()
    assert cards == 5, f"五国概览卡片数不对: {cards}"

    # 验证左侧法规三级分类树与右侧八项属性画像
    reg_items = await page.locator("[data-testid=user-reg-tree] .reg-item").count()
    assert reg_items >= 5, f"左侧法规目录树条目数不足: {reg_items}"
    user_attr_text = await page.locator("[data-testid=user-eight-attributes]").inner_text()
    assert "1. 适用行业" in user_attr_text and "8. 准入材料清单" in user_attr_text

    # 点击某条条款的关联义务芯片，验证左侧原文出现黄色高亮 <mark class="quote-highlight">
    chip_count = await page.locator("[data-testid=clause-obligation-chip]").count()
    if chip_count > 0:
        await page.locator("[data-testid=clause-obligation-chip]").first.click()
        await page.wait_for_timeout(300)
        marks = await page.locator("mark.quote-highlight").count()
        assert marks >= 1, "点击条款关联义务芯片后，左侧原文未高亮依据片段"
    await page.screenshot(path=str(Path.home() / ".cyshuhe-dev" / "user_reg_detail.png"))

    # 点击摩洛哥卡片再切回新加坡
    await page.locator(".country-card", has_text="摩洛哥").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(400)
    await _assert_no_error_toast(page, "切换摩洛哥知识库")
    await page.locator(".country-card", has_text="新加坡").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(800)
    await _assert_no_error_toast(page, "切回新加坡知识库")

    print("[8/8] 切换用户端「六要素 · 十二关系合规图谱」页签并验证 ECharts 合规地图 ...")
    await page.get_by_role("tab", name="六要素 · 十二关系合规图谱").click()
    await page.wait_for_timeout(2000)
    await _assert_no_error_toast(page, "用户端合规图谱页签")
    ubox = await page.locator("[data-testid=user-kg-force-graph] canvas").first.bounding_box()
    assert ubox and ubox["width"] > 400 and ubox["height"] > 300, f"用户端合规地图画布异常: {ubox}"
    await page.locator("[data-testid=user-kg-hide-hub-btn]").click()
    await page.wait_for_timeout(1200)
    await page.screenshot(path=str(Path.home() / ".cyshuhe-dev" / "user_compliance_map.png"))

    print("[9/10] 用户端「合规评估助手」:全程在聊天框里回答问题 → 真实模型分析 ...")
    await page.locator(".el-menu-item", has_text="合规评估助手").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1000)
    await _assert_no_error_toast(page, "进入合规评估助手")

    assistant = page.locator("[data-testid=chat-msg-assistant]")
    send_btn = page.locator("[data-testid=chat-send]")
    box = page.locator("textarea[data-testid=chat-input], [data-testid=chat-input] textarea").first

    async def say(text: str) -> None:
        before = await assistant.count()
        await box.fill(text)
        await send_btn.click()
        for _ in range(60):
            await page.wait_for_timeout(500)
            if await assistant.count() > before:
                break
        await _assert_no_error_toast(page, f"发送「{text[:12]}」")

    async def last_text() -> str:
        return await assistant.last.inner_text()

    # 新建一次对话,助手先问候并提出第一个问题(目标国家)
    await page.locator("[data-testid=btn-new-chat]").click()
    await page.wait_for_timeout(1200)
    assert await assistant.count() >= 2, "新对话没有出现助手问候与首个问题"
    assert await page.locator("[data-testid=chat-option-chip]").count() == 5, "国家选项应为 5 个"
    # 无关的回答不会被当成国家
    await say("随便聊聊天气")
    assert "没有识别出国家" in await last_text()
    # 点选国家 → 直接发送
    await page.locator("[data-testid=chat-option-chip]", has_text="新加坡").click()
    for _ in range(20):
        await page.wait_for_timeout(500)
        if "车型" in await last_text():
            break
    assert "车型" in await last_text(), await last_text()
    await say("CYS-Pilot L4 自动驾驶 SUV")
    assert "场景" in await last_text()
    await page.locator("[data-testid=chat-option-chip]", has_text="自动驾驶公共道路测试").click()
    await page.wait_for_timeout(2500)
    # 事实类问题:太简短会被要求补充
    await say("有")
    assert "简略" in await last_text()
    # 示例回答只填入输入框,由用户改写后再发送
    for i in range(5):
        chip = page.locator("[data-testid=chat-option-chip]").first
        await chip.click()
        filled = await box.input_value()
        assert len(filled) > 6, "点击示例后输入框没有被填入"
        before = await assistant.count()
        await send_btn.click()
        for _ in range(60):
            await page.wait_for_timeout(500)
            if await assistant.count() > before:
                break
        await _assert_no_error_toast(page, f"回答第 {i + 1} 个事实问题")
    assert await page.locator("[data-testid=task-running-tag]").count() == 1, "未进入分析中状态"

    # 等待后台分析结束,聊天里出现报告卡片
    for _ in range(150):
        await page.wait_for_timeout(2000)
        if await page.locator("[data-testid=report-card]").count() == 1:
            break
    assert await page.locator("[data-testid=report-card]").count() == 1, "分析超时,没有出现报告卡片"
    await _assert_no_error_toast(page, "报告生成")

    print("[10/10] 在聊天里查看报告、溯源引用并导出 Word/PDF ...")
    chapters = await page.locator("[data-testid^=report-chapter-]").count()
    assert chapters == 8, f"报告应有 8 个维度: {chapters}"
    await page.locator("[data-testid=report-chapter-D3]").locator(".el-collapse-item__header").click()
    await page.wait_for_timeout(400)
    await page.locator("[data-testid=report-citation-badge]:visible").first.click()
    await page.wait_for_timeout(1200)
    await _assert_no_error_toast(page, "点击引用角标")
    drawer_text = await page.locator("[data-testid=citation-detail-drawer]").inner_text()
    assert "官方原文" in drawer_text and "术语受控中文对照" in drawer_text
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(400)
    async with page.expect_download() as dl:
        await page.locator("[data-testid=btn-export-docx]").click()
    assert (await dl.value).suggested_filename.endswith(".docx")
    async with page.expect_download() as dl2:
        await page.locator("[data-testid=btn-export-pdf]").click()
    assert (await dl2.value).suggested_filename.endswith(".pdf")
    await page.screenshot(path=str(Path.home() / ".cyshuhe-dev" / "user_chat_report.png"))
    # 报告生成后继续发消息,助手只提示报告已可导出
    await say("谢谢")
    assert "报告已经生成" in await last_text()

    await context.close()


async def main() -> int:
    creds = json.loads(DEV_CREDS.read_text(encoding="utf-8"))
    bad_responses: list[str] = []
    page_errors: list[str] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        try:
            await verify_admin_portal(browser, creds, bad_responses, page_errors)
            await verify_user_portal(browser, creds, bad_responses, page_errors)
        finally:
            await browser.close()

    if bad_responses:
        raise RuntimeError(f"检测到异常 API 响应: {bad_responses}")
    if page_errors:
        raise RuntimeError(f"检测到前端 JS 报错: {page_errors}")

    print("\n=== 浏览器模拟手点全链路验证全部通过 (0 HTTP 错误, 0 JS 报错, 0 错误弹窗) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
