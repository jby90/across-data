"""多智能体分析使用的大模型调用(OpenAI 兼容接口)。

密钥只从环境变量 CYSH_LLM_API_KEY 读取,不写入代码、配置文件或数据库。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings


class LlmUnavailableError(RuntimeError):
    """未配置密钥或接口调用失败。"""


@dataclass
class LlmResult:
    data: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    model: str

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


async def call_llm_json(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 6000,
) -> LlmResult:
    """调用大模型并要求返回 JSON 对象;失败时抛出 LlmUnavailableError。"""
    settings = get_settings()
    if not settings.llm_api_key:
        raise LlmUnavailableError("未配置大模型密钥(环境变量 CYSH_LLM_API_KEY)")
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=settings.llm_analysis_timeout_seconds) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.llm_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    # 推理型模型默认会耗尽 max_tokens 而返回空内容,分析任务只需低强度推理
                    "reasoning_effort": "low",
                },
            )
        resp.raise_for_status()
        body = resp.json()
        choice = body["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("模型输出被长度上限截断")
        content = choice["message"]["content"]
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("模型未返回 JSON 对象")
    except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise LlmUnavailableError(f"大模型调用失败: {type(exc).__name__}: {exc}") from exc
    usage = body.get("usage") or {}
    return LlmResult(
        data=data,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        duration_ms=int((time.perf_counter() - started) * 1000),
        model=settings.llm_model,
    )
