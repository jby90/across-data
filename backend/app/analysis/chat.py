"""对话式信息采集:用户在聊天框里一问一答完成所有信息填写,齐备后启动分析。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.llm import LlmUnavailableError, call_llm_json
from app.analysis.models import AnalysisMessage, AnalysisTask
from app.analysis.workflow_engine import (
    COUNTRY_NAMES_ZH,
    REQUIRED_FACTS_SPEC,
    SCENARIO_LABELS,
    evaluate_fact_sufficiency,
)
from app.core.config import get_settings

PLACEHOLDER_COUNTRY = "--"
FACT_KEYS = [s["fact_key"] for s in REQUIRED_FACTS_SPEC]
FACT_SPEC = {s["fact_key"]: s for s in REQUIRED_FACTS_SPEC}
SLOT_LABEL = {
    "country_code": "目标国家",
    "vehicle_model": "车型/车队",
    "scenario_type": "业务场景",
    **{s["fact_key"]: f"{s['dimension_code']} 业务事实" for s in REQUIRED_FACTS_SPEC},
}
GREETING = (
    "您好,我是车研数合合规分析助手。我会通过几个问题了解您的业务场景,"
    "然后由多个智能体对照目标国现行法规,生成八维度合规评估报告,您可以直接在这里导出。"
    "所有回答只用于本次评估。"
)
SCENARIO_HINTS = [
    ("autonomous_trial", ("自动驾驶", "路测", "L3", "L4")),
    ("ota_diagnostics", ("OTA", "诊断", "升级")),
    ("cockpit_biometrics", ("座舱", "语音", "生物", "驾驶员状态", "DMS")),
    ("connected_vehicle_cloud", ("车联网", "云平台", "遥测", "云端")),
]

StartFn = Callable[[AsyncSession, AnalysisTask], Awaitable[None]]


def next_slot(task: AnalysisTask) -> str | None:
    if task.country_code not in COUNTRY_NAMES_ZH:
        return "country_code"
    if not (task.vehicle_model or "").strip():
        return "vehicle_model"
    if task.scenario_type not in SCENARIO_LABELS:
        return "scenario_type"
    facts = task.facts_json or {}
    for key in FACT_KEYS:
        if len(str(facts.get(key) or "").strip()) < 4:
            return key
    return None


def question_for(slot: str) -> tuple[str, list[str]]:
    if slot == "country_code":
        opts = [f"{name} ({code})" for code, name in COUNTRY_NAMES_ZH.items()]
        return "请问您计划把车辆或数据投放到哪个国家?(目前支持以下五国,可直接点选)", opts
    if slot == "vehicle_model":
        return "请问受评的车型或车队名称是什么?例如「某品牌 L4 自动驾驶 SUV」。", []
    if slot == "scenario_type":
        return "请选择最接近的业务场景:", list(SCENARIO_LABELS.values())
    spec = FACT_SPEC[slot]
    text = f"{spec['question_zh']}\n(为什么要问:{spec['why_needed']})"
    return text, list(spec["suggested_options"])


def match_country(text: str) -> str | None:
    upper = text.upper()
    for code, name in COUNTRY_NAMES_ZH.items():
        if name in text or re.search(rf"(?<![A-Z]){code}(?![A-Z])", upper):
            return code
    return None


def match_scenario(text: str) -> str | None:
    for key, label in SCENARIO_LABELS.items():
        if text.strip() == label or key in text:
            return key
    for key, words in SCENARIO_HINTS:
        if any(w.lower() in text.lower() for w in words):
            return key
    return None


def apply_text(task: AnalysisTask, slot: str, text: str) -> str | None:
    """把用户原话写入槽位;返回错误提示,成功返回 None。"""
    text = text.strip()
    if slot == "country_code":
        code = match_country(text)
        if code is None:
            return "没有识别出国家,请从下方选项中选择,或直接输入国家名称。"
        task.country_code = code
    elif slot == "vehicle_model":
        if not 2 <= len(text) <= 128:
            return "车型名称请控制在 2~128 个字符内。"
        task.vehicle_model = text
    elif slot == "scenario_type":
        key = match_scenario(text)
        if key is None:
            return "没有识别出业务场景,请从下方选项中选择最接近的一项。"
        task.scenario_type = key
    else:
        if len(text) < 6:
            return "这条信息有点简略,请再具体描述一下(至少几个字),这样才能对照法规分析。"
        facts = dict(task.facts_json or {})
        facts[slot] = text[:1000]
        task.facts_json = facts
    return None


def _refresh_state(task: AnalysisTask) -> None:
    score, missing = evaluate_fact_sufficiency(task.country_code, task.facts_json or {})
    task.sufficiency_score = score
    task.missing_facts_json = missing
    if task.country_code in COUNTRY_NAMES_ZH and (task.vehicle_model or "").strip():
        name = COUNTRY_NAMES_ZH[task.country_code]
        task.title = f"{name} · {task.vehicle_model.strip()} 合规评估"[:256]


async def add_message(
    session: AsyncSession,
    task_id: Any,
    role: str,
    content: str,
    options: list[str] | None = None,
    slot: str | None = None,
) -> None:
    last = (
        await session.execute(
            select(func.max(AnalysisMessage.seq_no)).where(AnalysisMessage.task_id == task_id)
        )
    ).scalar_one()
    session.add(
        AnalysisMessage(
            task_id=task_id,
            seq_no=(last or 0) + 1,
            role=role,
            content=content,
            options_json=options or [],
            slot=slot,
        )
    )
    await session.flush()


async def _ask(session: AsyncSession, task: AnalysisTask, slot: str, prefix: str = "") -> None:
    text, options = question_for(slot)
    await add_message(session, task.id, "assistant", (prefix + text).strip(), options, slot)


async def start_chat(session: AsyncSession, task: AnalysisTask) -> None:
    await add_message(session, task.id, "assistant", GREETING)
    slot = next_slot(task) or "country_code"
    await _ask(session, task, slot)
    await session.commit()


async def _llm_extract(text: str) -> dict[str, str]:
    """长句里可能一次回答了多个问题:让模型抽取原话片段,片段必须是用户原话的子串。"""
    if not get_settings().llm_api_key or len(text) < 40:
        return {}
    descriptions = {
        "country_code": "目标国家",
        "vehicle_model": "车型或车队名称",
        "scenario_type": "业务场景",
        **{k: v["question_zh"] for k, v in FACT_SPEC.items()},
    }
    messages = [
        {
            "role": "system",
            "content": (
                "从用户的一段话中抽取各槽位对应的原话片段。只能逐字复制用户原话里的连续片段,"
                "不得改写、翻译或补充;用户没有提到的槽位不要输出。"
                '只输出 JSON:{"slots":{"槽位名":"原话片段"}}'
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"slots": descriptions, "message": text}, ensure_ascii=False),
        },
    ]
    try:
        res = await call_llm_json(messages, max_tokens=1500)
    except LlmUnavailableError:
        return {}
    slots = res.data.get("slots")
    if not isinstance(slots, dict):
        return {}
    out: dict[str, str] = {}
    for key, snippet in slots.items():
        if key in descriptions and isinstance(snippet, str) and snippet.strip() in text:
            out[key] = snippet.strip()
    return out


def _brief(slot: str, task: AnalysisTask) -> str:
    if slot == "country_code":
        return f"目标国家:{COUNTRY_NAMES_ZH.get(task.country_code, task.country_code)}"
    if slot == "vehicle_model":
        return f"车型:{task.vehicle_model}"
    if slot == "scenario_type":
        return f"场景:{SCENARIO_LABELS.get(task.scenario_type, task.scenario_type)}"
    value = str((task.facts_json or {}).get(slot) or "")
    return f"{SLOT_LABEL[slot]}:{value[:36]}{'…' if len(value) > 36 else ''}"


async def post_user_message(
    session: AsyncSession, task: AnalysisTask, content: str, start: StartFn
) -> None:
    content = content.strip()
    await add_message(session, task.id, "user", content)

    if task.status == "running":
        await add_message(
            session, task.id, "assistant", "分析正在进行中,请稍候,进度会实时显示在下方。"
        )
        await session.commit()
        return
    if task.status in ("completed", "partial_sample"):
        await add_message(
            session,
            task.id,
            "assistant",
            "本次评估的报告已经生成,可以在上方直接查看和导出 Word / PDF。"
            "如需评估新的国家或场景,请点击左侧「新对话」。",
        )
        await session.commit()
        return
    if task.status == "failed":
        await add_message(session, task.id, "assistant", "好的,我重新启动分析。")
        await session.commit()
        await start(session, task)
        return

    slot = next_slot(task)
    if slot is None:
        await session.commit()
        await start(session, task)
        return

    applied: list[str] = []
    errors: list[str] = []
    extracted = await _llm_extract(content)
    for key, snippet in extracted.items():
        if apply_text(task, key, snippet) is None:
            applied.append(key)
    if not applied:
        err = apply_text(task, slot, content)
        if err is None:
            applied.append(slot)
        else:
            errors.append(err)
    _refresh_state(task)

    if not applied:
        await _ask(session, task, slot, prefix=f"{errors[0]}\n\n")
        await session.commit()
        return

    ack = "已记录:" + ";".join(_brief(s, task) for s in applied)
    follow = next_slot(task)
    if follow is not None:
        await add_message(session, task.id, "assistant", ack)
        await _ask(session, task, follow)
        task.status = "collecting_facts"
        await session.commit()
        return

    await add_message(
        session,
        task.id,
        "assistant",
        f"{ack}\n\n信息已经齐备(完备度 100%)。我现在启动多智能体分析:"
        "先检索该国现行法规条款,再逐维度分析,最后由程序核验每条引用是否真是原文。"
        "下面会实时显示每个智能体的进度。",
    )
    task.status = "ready_to_run"
    await session.commit()
    await start(session, task)
