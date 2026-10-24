"""Sequential real API runs in a dedicated evaluation database. Credentials from env only."""

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx

FACTS = {
    "exterior_video_and_gnss": "采集8路环视视频与GNSS；车端模糊人脸与车牌后回传",
    "cross_border_mechanism": "传输至中国研发中心，已签数据处理协议并完成评估",
    "consent_and_notice": "车机弹窗明示同意，车身告示，已任命本地数据保护官",
    "retention_and_blackbox": "黑匣子留存3年；常规个人数据180天后销毁",
    "local_admission_and_safety_operator": "已提交路测准入申请，配备安全员与保险",
}


async def run(args):
    rows = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60) as client:
        response = await client.post(
            "/api/auth/user/login",
            json={
                "email": os.environ["CYSH_EVAL_EMAIL"],
                "password": os.environ["CYSH_EVAL_PASSWORD"],
            },
        )
        response.raise_for_status()
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        for i in range(args.runs):
            started = time.perf_counter()
            r = await client.post(
                "/api/analysis/tasks",
                json={
                    "title": f"S5 隔离稳定性验证 {i + 1}",
                    "country_code": "SG",
                    "facts": FACTS,
                },
            )
            r.raise_for_status()
            task = r.json()
            while task["status"] == "running" and time.perf_counter() - started < 1200:
                await asyncio.sleep(2)
                r = await client.get(f"/api/analysis/tasks/{task['id']}")
                r.raise_for_status()
                task = r.json()
            first = next((t for t in task["step_traces"] if t["phase"] == "verification"), None)
            output = (first or {}).get("output_snapshot") or {}
            checks = output.get("verification_items") or []
            initial_calls = [
                c for t in task["step_traces"] if t["phase"] == "analysis" for c in t["tool_calls"]
            ]
            rows.append(
                {
                    "task_id": task["id"],
                    "status": task["status"],
                    "workflow_version_id": task.get("workflow_version_id"),
                    "first_evaluated": bool(checks),
                    "first_failed": bool(output.get("problems")) if checks else None,
                    "first_problems": output.get("problems", []),
                    "local_selection_retries": sum(
                        len(c["arguments"].get("selection_retries", [])) for c in initial_calls
                    ),
                    "local_grounding_failures": sum(
                        len(g.get("failures", []))
                        for c in initial_calls
                        for g in c["arguments"].get("grounding_calls", [])
                    ),
                    "backtrack_count": task["backtrack_count"],
                    "tokens": task["total_tokens"],
                    "elapsed_seconds": round(time.perf_counter() - started, 2),
                    "report_status": task["report_versions"][0]["status"]
                    if task["report_versions"]
                    else None,
                    "gap_kinds": dict(
                        Counter(
                            d.get("gap_kind") or "none"
                            for v in task["report_versions"][:1]
                            for d in v["dimensions"]
                        )
                    ),
                }
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps({"facts": FACTS, "runs": rows}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:18005")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))
