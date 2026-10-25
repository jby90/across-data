"""Run the frozen real API evaluation. No credentials or API keys in outputs."""

import argparse
import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx


def percent(value):
    return "未评测" if value is None else f"{100 * value:.2f}%"


def write_report(result, path):
    lines = [
        "# S5 真实评测记录",
        "",
        f"批次：{result['batch_id']}",
        f"冻结集哈希：`{result['suite_sha256']}`",
        f"工作流：`{result['workflow_version_id']}`",
        "",
        result["note"],
        "",
        "|指标|结果|分子/分母|",
        "|---|---:|---:|",
        f"|检索 recall@5|{percent(result['recall_at_5'])}|{result['retrieval_hits']}/{result['retrieval_expected']}|",
        f"|引文逐字与数字忠实度|{percent(result['quote_fidelity'])}|{result['faithful_findings']}/{result['findings_total']}|",
        f"|引文支持结论（llm_judged）|{percent(result['fact_consistency'])}|{result['supported_findings']}/{result['judged_findings']}|",
        f"|Judge覆盖率|{percent(result['judge_coverage'])}|{result['judged_findings']}/{result['findings_total']}|",
        f"|人工复核|未经人工复核|0/{result['total_cases']}|",
        "",
        f"完成 {result['completed_cases']}/{result['total_cases']} 题；技术错误 {result['technical_error_cases']} 题。",
        f"其中 {sum(c['findings_total'] > 0 for c in result['cases'])} 题有分析项（含拒绝/失败尝试）；无输出不算引文通过。",
        f"已知 Token：{result['total_tokens']:,}；本次客户端耗时：{result['elapsed_seconds']:.2f} 秒。",
        "续跑时只统计本次客户端耗时；此前中断耗时未知。Token 含记录的缓存结果，不代表服务商计费账单。"
        if result.get("resumed_batch") else "",
        "",
        "标注按已归档原文定位建立，未根据检索前五结果挑选题目，也未用模型自评冒充人工核验。",
        "埃及部分原文OCR损伤、法源有效状态未知及源隔离造成的未命中均照实保留；不自动解除隔离。",
        "本评测包含节点预算内的局部重选，所有尝试均计入程序核验分母，不含工作流退回后的优化；结论要点供人工复核，不以关键词匹配冒充事实一致性。",
        "",
        "|题号|国家|维度|期望条款命中|引文通过|Judge支持|技术错误|人工复核|",
        "|---|---|---|---:|---:|---:|---|---|",
    ]
    for c in result["cases"]:
        lines.append(
            f"|{c['question_id']}|{c['country_code']}|{c['dimension_code']}|{c['retrieval_hits']}/{c['retrieval_expected']}|{c['faithful_findings']}/{c['findings_total']}|{c['supported_findings']}/{c['judged_findings']}|{'有' if c['analysis_error'] or c['judge_error'] or c.get('retrieval_error') else '无'}| |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run(args):
    started = time.perf_counter()
    batch = str(uuid.UUID(args.resume_batch)) if args.resume_batch else str(uuid.uuid4())
    if args.retry_technical_errors and not args.resume_batch:
        raise ValueError("技术重试必须明确指定原批次；不能把复测混称为独立新评测")
    async with httpx.AsyncClient(base_url=args.base_url, timeout=600) as client:
        r = await client.post(
            "/api/auth/admin/login",
            json={
                "email": os.environ["CYSH_EVAL_EMAIL"],
                "password": os.environ["CYSH_EVAL_PASSWORD"],
            },
        )
        r.raise_for_status()
        client.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        r = await client.get("/api/admin/agent-studio/evaluations/suite")
        r.raise_for_status()
        frozen = r.json()
        done = set()
        retried = []
        if args.resume_batch:
            r = await client.get("/api/admin/agent-studio/evaluations/latest")
            r.raise_for_status()
            previous = r.json()
            r = await client.get("/api/admin/agent-studio/workflow")
            r.raise_for_status()
            if (previous.get("batch_id") != batch
                or previous.get("suite_sha256") != frozen["suite_sha256"]
                or previous.get("workflow_version_id") != r.json()["id"]):
                raise RuntimeError("批次、冻结题集或执行版本已变更，拒绝混合续跑")
            # The server appends immutable attempts. Never replay semantic
            # rejections just to improve a score; explicit recovery is technical only.
            done = {case["question_id"] for case in previous["cases"]}
            if args.retry_technical_errors:
                retried = [case["question_id"] for case in previous["cases"] if any(
                    case.get(key) for key in ("analysis_error", "judge_error", "retrieval_error")
                )]
                done -= set(retried)
            print(f"Resuming {batch}; retaining {len(done)} recorded cases", flush=True)
            if retried:
                print(f"Technical retry only: {', '.join(retried)}; old audits retained", flush=True)
        gate = asyncio.Semaphore(args.concurrency)

        async def one(q):
            async with gate:
                r = await client.post(
                    f"/api/admin/agent-studio/evaluations/{batch}/cases/{q['id']}"
                )
                r.raise_for_status()
                c = r.json()
                print(
                    f"{q['id']} retrieval={c['retrieval_hits']}/{c['retrieval_expected']} quote={c['faithful_findings']}/{c['findings_total']} judge={c['supported_findings']}/{c['judged_findings']}",
                    flush=True,
                )

        await asyncio.gather(*(one(q) for q in frozen["cases"] if q["id"] not in done))
        r = await client.get("/api/admin/agent-studio/evaluations/latest")
        r.raise_for_status()
        result = r.json()
        if result.get("batch_id") != batch or result["completed_cases"] != len(
            frozen["cases"]
        ):
            raise RuntimeError("批次不完整或被其他批次切换，不能发布混合指标")
        result["elapsed_seconds"] = time.perf_counter() - started
        result["resumed_batch"] = bool(args.resume_batch)
        result["retried_technical_errors"] = retried
        if retried:
            result["note"] += (
                " 本次仅对技术失败追加尝试，其他题复用原记录；不是全题重新独立运行。"
                "原始整批结果另行保留，语义否定结果未为改善分数而重试。"
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_report(result, args.report)
        print(
            json.dumps(
                {k: v for k, v in result.items() if k != "cases"}, ensure_ascii=False
            )
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://127.0.0.1:18005")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--resume-batch", help="Resume the current unchanged batch without replaying records")
    p.add_argument("--retry-technical-errors", action="store_true",
                   help="Append retries only for recorded technical errors; preserve old audits")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--report",
        type=Path,
        default=Path(f"docs/architecture/eval_report_{datetime.now():%Y%m%d}.md"),
    )
    asyncio.run(run(p.parse_args()))
