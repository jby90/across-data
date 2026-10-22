"""冻结的 10 道检索评测题:自动检查「检索结果里是否真的出现了题目要求的原文词句」。

注意:这只是检索命中检查,不是法律准确率。法条定位是否正确、结论是否站得住,
仍须由人工对照原文核验(当前 0/10,未完成)。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.tools import ToolBox

FROZEN_QUESTIONS: list[dict[str, Any]] = [
    {
        "id": "H01",
        "cc": "SG",
        "dim": "D3",
        "q": "含人脸/车牌的路测视频回传境外研发中心,个人数据出境有何限制?",
        "need": ["transfer", "outside singapore"],
    },
    {
        "id": "H02",
        "cc": "SG",
        "dim": "D4",
        "q": "收集车主位置与座舱语音数据前,告知目的与取得同意的依据是什么?",
        "need": ["consent"],
    },
    {
        "id": "H03",
        "cc": "SG",
        "dim": "D5",
        "q": "诊断个人数据的收集目的达成后,停止留存或匿名化的义务依据是什么?",
        "need": ["retention"],
    },
    {
        "id": "H04",
        "cc": "SG",
        "dim": "D6",
        "q": "严重违规时行政罚款的金额上限(新元与营业额比例)依据是什么?",
        "need": ["10%", "turnover"],
    },
    {
        "id": "H05",
        "cc": "SG",
        "dim": "D7",
        "q": "在公共道路开展自动驾驶汽车测试需要满足哪些准入要求?",
        "need": ["autonomous"],
    },
    {
        "id": "H06",
        "cc": "SG",
        "dim": "D2",
        "q": "法规如何界定受保护的个人数据,车外摄像头影像是否纳入?",
        "need": ["personal data", "identif"],
    },
    {
        "id": "H07",
        "cc": "TN",
        "dim": "D3",
        "q": "个人数据跨境传输是否需要事先取得监管机构授权?",
        "need": ["transfert"],
    },
    {
        "id": "H08",
        "cc": "MA",
        "dim": "D3",
        "q": "个人数据向境外传输是否须经监管机构核准?",
        "need": ["transfert"],
    },
    {
        "id": "H09",
        "cc": "EG",
        "dim": "D7",
        "q": "车联网(IoT)服务的监管框架对终端入网有何要求?",
        "need": ["iot"],
    },
    {
        "id": "H10",
        "cc": "LB",
        "dim": "D1",
        "q": "个人数据收集处理的基本原则规定在哪里?",
        "need": ["personal data"],
    },
]


async def run_retrieval_benchmarks(session: AsyncSession) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    boxes: dict[str, ToolBox] = {}
    for q in FROZEN_QUESTIONS:
        tb = boxes.setdefault(q["cc"], ToolBox(session, q["cc"]))
        top = await tb.search_effective_clauses(q["dim"], top_k=5)
        hit = None
        snippet = ""
        for cand in top:
            low = cand.text.lower()
            if all(n in low for n in q["need"]):
                hit = cand
                pos = low.find(q["need"][0])
                snippet = cand.text[max(0, pos - 40) : pos + 160].replace("\n", " ")
                break
        cases.append(
            {
                "question_id": q["id"],
                "country_code": q["cc"],
                "dimension_code": q["dim"],
                "scenario_question": q["q"],
                "required_phrases": q["need"],
                "matched_document_title": hit.document_title if hit else "",
                "matched_clause_label": hit.label if hit else "",
                "passed": hit is not None,
                "evidence_snippet": snippet,
            }
        )
    hits = sum(1 for c in cases if c["passed"])
    return {
        "total_cases": len(cases),
        "hit_cases": hits,
        "retrieval_hit_rate_pct": round(hits / len(cases) * 100, 1) if cases else 0.0,
        "human_review_status": "0/10 未完成(法条定位与结论正确性须人工对照原文)",
        "note": "仅为检索命中检查(前 5 条候选里是否出现题目要求的原文词句),不是法律准确率。",
        "cases": cases,
    }
