"""Read-only documentation capture for operators/CI; no review or analysis writes.

The current Codex session captures through its built-in browser. This optional
standalone Playwright script reproduces those navigation steps outside that session.
Credentials stay in CYSH_CAPTURE_ADMIN_PASSWORD / CYSH_CAPTURE_USER_PASSWORD.
"""
import argparse
import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright


async def portal(browser, base, email, password, routes, output):
    context = await browser.new_context(viewport={"width": 1440, "height": 1000})
    page = await context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    pending = set()
    page.on("request", lambda req: pending.add(req)
            if "/api/" in req.url and not req.url.endswith("/events") else None)
    page.on("requestfinished", lambda req: pending.discard(req))
    page.on("requestfailed", lambda req: pending.discard(req))
    await page.goto(base.rstrip("/") + "/login")
    await page.get_by_role("textbox", name="邮箱", exact=True).fill(email)
    await page.get_by_role("textbox", name="密码", exact=True).fill(password)
    await page.get_by_role("button", name="登录工作台", exact=True).click()
    await page.wait_for_url(base.rstrip("/") + "/")
    for slug, name in routes:
        await page.goto(base.rstrip("/") + "/" + slug)
        await page.locator("main").wait_for(state="visible")
        for _ in range(200):
            await asyncio.sleep(0.1)
            if not pending:
                break
        else:
            raise RuntimeError("API did not settle for documentation capture")
        await page.evaluate("document.fonts.ready.then(() => true)")
        await page.screenshot(path=str(output / (name + ".png")), full_page=False)
    await context.close()
    if errors:
        raise RuntimeError(f"Browser errors: {errors}")


async def main(args):
    args.output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as engine:
        browser = await engine.chromium.launch(channel="chrome", headless=True)
        try:
            await portal(browser, args.admin_url, os.getenv("CYSH_CAPTURE_ADMIN_EMAIL",
                         "admin@example.com"), os.environ["CYSH_CAPTURE_ADMIN_PASSWORD"],
                         [("documents", "documents"), ("review-queue", "review-queue"),
                          ("knowledge-graph", "knowledge-graph"), ("agent-studio", "agent-studio"),
                          ("sites", "sites"), ("updates", "updates"), ("orgs", "orgs"),
                          ("users", "users"), ("licenses", "licenses")], args.output)
            await portal(browser, args.user_url, os.getenv("CYSH_CAPTURE_USER_EMAIL",
                         "org-admin@example.com"), os.environ["CYSH_CAPTURE_USER_PASSWORD"],
                         [("knowledge", "knowledge"), ("analysis", "analysis"),
                          ("notifications", "notifications")], args.output)
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--user-url", required=True)
    parser.add_argument("--output", type=Path, default=Path("docs/product/img"))
    asyncio.run(main(parser.parse_args()))
