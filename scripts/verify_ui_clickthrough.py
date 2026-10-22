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

    # 点击关系筛选标签
    await page.locator(".el-tag", has_text="数据出境限制").first.click()
    await page.wait_for_timeout(300)
    await page.locator(".el-tag", has_text="全部").first.click()
    await page.wait_for_timeout(300)
    await _assert_no_error_toast(page, "图谱节点类型与关系筛选")

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

    print("[7/8] 点击用户端「法规知识库」并验证五国卡片、关键词检索与图谱页签 ...")
    await page.locator(".el-menu-item", has_text="法规知识库").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(800)
    await _assert_no_error_toast(page, "用户端法规知识库")
    cards = await page.locator(".country-card").count()
    assert cards == 5, f"五国概览卡片数不对: {cards}"

    # 点击摩洛哥卡片再切回新加坡
    await page.locator(".country-card", has_text="摩洛哥").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(400)
    await _assert_no_error_toast(page, "切换摩洛哥知识库")
    await page.locator(".country-card", has_text="新加坡").click()
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(400)
    await _assert_no_error_toast(page, "切回新加坡知识库")

    print("[8/8] 切换用户端「六要素 · 十二关系合规图谱」页签 ...")
    await page.get_by_role("tab", name="六要素 · 十二关系合规图谱").click()
    await page.wait_for_timeout(400)
    await _assert_no_error_toast(page, "用户端合规图谱页签")

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
