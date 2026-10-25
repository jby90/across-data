"""Loopback-only S6 UI fixture. Synthetic output is NOT a legal-quality evaluation.

Run from backend: python -m uvicorn --app-dir ../scripts s6_synthetic_llm:app
  --host 127.0.0.1 --port 18011
Point ONLY an isolated synthetic demo API at this endpoint. No provider key required.
The D3/Shanghai branch deliberately exercises a test scenario, never production routing.
"""

import json

from fastapi import FastAPI, HTTPException

app = FastAPI(title="S6 synthetic fixture — NOT legal advice")


@app.post("/chat/completions")
async def completions(body: dict):
    p = json.loads(body["messages"][1]["content"])
    phase = p["phase"]
    if phase == "verify_source_support":
        data = {"supported": "yes", "reason": "Synthetic test only; not a semantic evaluation"}
    elif phase == "write_from_evidence":
        if p.get("source_units"):
            data = {
                "units": [
                    {"unit_id": u["unit_id"], "meaning": u["text"]} for u in p["source_units"]
                ],
                "gap": "",
                "risk_level": "LOW",
            }
        else:
            data = {"obligation": p["evidence"]["quote"], "risk_level": "LOW", "gap": ""}
    elif phase == "select_evidence":
        code = p["dimension"]["code"]
        if code == "D3" and "上海" not in p["business_facts"].get("transfer_destination", ""):
            data = {
                "findings": [],
                "needs_user_fact": [
                    {
                        "fact_key": "transfer_destination",
                        "question_zh": "数据实际传输到哪个国家和接收机构？（合成流程演示）",
                        "why_needed": "接收地是业务事实，影响本维度的场景判断。",
                        "options": ["中国上海研发中心", "暂不确定，尚未决定"],
                    }
                ],
            }
        else:
            data = {
                "findings": [
                    {
                        "clause_id": p["candidate_clauses"][0]["clause_id"],
                        "sentence_ids": [1],
                        "fact_key": "transfer_destination"
                        if code == "D3"
                        else "consent_and_notice",
                        "obligation": "合成测试：由原文限定生成",
                        "risk_level": "LOW",
                    }
                ]
            }
    else:
        raise HTTPException(400, "Unsupported synthetic fixture phase")
    return {
        "model": "synthetic-s6-demo",
        "choices": [
            {
                "message": {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }
