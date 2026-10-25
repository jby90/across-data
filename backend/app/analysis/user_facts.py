"""Durable business-fact follow-ups; never source repair or legal approval."""

import copy
import hashlib
from datetime import UTC, datetime

MAX_FACT_ROUNDS = 2


def candidate_binding(candidate):
    return {
        key: getattr(candidate, key)
        for key in (
            "clause_id",
            "document_id",
            "version_id",
            "version_no",
            "source_url",
            "label",
            "section_path",
            "review_status",
            "legal_status",
            "source_nature",
            "context_parent_id",
            "instrument_identity",
            "instrument_role",
            "text_source",
        )
    } | {"text_sha256": hashlib.sha256(candidate.text.encode()).hexdigest()}


def history_event(task, event, **data):
    task.fact_followup_history_json = [
        *(task.fact_followup_history_json or []),
        {
            "event": event,
            "at": datetime.now(UTC).isoformat(),
            "round": task.fact_followup_rounds,
            "workflow_version_id": str(task.workflow_version_id),
            **copy.deepcopy(data),
        },
    ]


async def ask_next(session, task):
    from app.analysis.chat import add_message

    question = task.pending_user_facts_json[0]
    await add_message(
        session,
        task.id,
        "assistant",
        f"分析中需要补充业务事实（第 {task.fact_followup_rounds}/{MAX_FACT_ROUNDS} 轮，"
        f"涉及 {'、'.join(question['dimension_codes'])}）：\n{question['question_zh']}\n"
        f"为什么需要：{question['why_needed']}\n请按实际情况回答；不确定可以如实说明。",
        question["options"] or ["暂不确定，当前无法补充"],
        question["fact_key"],
    )


async def answer_user_facts(session, task, answers, question_ids):
    """Caller must lock the task row. Exact question IDs prevent duplicate/stale replies."""
    from app.analysis.chat import add_message
    from app.analysis.pipeline import _add_trace

    if task.status != "awaiting_user_facts" or not task.pending_user_facts_json:
        raise ValueError("当前没有待回答的分析追问，请刷新对话")
    pending = {q["fact_key"]: q for q in task.pending_user_facts_json}
    if not answers or set(answers) - pending.keys() or set(question_ids) != set(answers):
        raise ValueError("只能回答本轮尚未完成的业务事实问题")
    for key, value in answers.items():
        if question_ids.get(key) != pending[key]["id"]:
            raise ValueError("追问已变化或已回答，请刷新后回答当前问题")
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000:
            raise ValueError("请提供1至2000字的实际业务回答，可以明确说明暂不确定")
    checkpoint = copy.deepcopy(task.analysis_checkpoint_json)
    if checkpoint.get("workflow_version_id") != str(task.workflow_version_id):
        raise ValueError("追问断点与执行版本不一致，禁止切换版本继续")
    facts = dict(task.facts_json or {})
    affected = set(checkpoint.get("resume_dimensions", []))
    for key, value in answers.items():
        facts[key] = value.strip()
        affected.update(pending[key]["dimension_codes"])
        for code, result in checkpoint["results"].items():
            if any(f.get("fact_key") == key for f in result.get("findings", [])):
                affected.add(code)
    task.facts_json = facts
    task.pending_user_facts_json = [
        q for q in task.pending_user_facts_json if q["fact_key"] not in answers
    ]
    checkpoint["resume_dimensions"] = sorted(affected)
    task.analysis_checkpoint_json = checkpoint
    history_event(
        task,
        "answered",
        answers=answers,
        question_ids=question_ids,
        affected_dimensions=sorted(affected),
    )
    await _add_trace(
        session,
        task,
        node="node_fact_collector",
        name="分析中业务事实回答",
        phase="user_fact_answer",
        status="passed",
        calls=[],
        summary=(
            f"已记录 {len(answers)} 项原话回答；涉及 {'、'.join(sorted(affected))}，不作法律批准"
        ),
        inp={"question_ids": question_ids},
        commit=False,
        out={
            "answers": answers,
            "affected_dimensions": sorted(affected),
            "pending_count": len(task.pending_user_facts_json),
        },
    )
    if task.pending_user_facts_json:
        await ask_next(session, task)
        await session.commit()
        return False
    task.status = "ready_to_run"
    await add_message(
        session,
        task.id,
        "assistant",
        "已记录本轮回答。将保留其他维度的已完成结果，仅重跑受影响维度；"
        "原文、证据或来源状态变化的维度也须重新核验。",
    )
    await session.commit()
    return True
