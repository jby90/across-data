"""多智能体分析使用的大模型调用(OpenAI 兼容接口)。

密钥只从环境变量 CYSH_LLM_API_KEY 读取,不写入代码、配置文件或数据库。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.redaction import Redactor, egress_context

MAX_ATTEMPTS = 2
# JSON 里合法的转义只有 \" \\ \/ \b \f \n \r \t \uXXXX;模型偶尔会输出裸反斜杠
_BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})')


class LlmUnavailableError(RuntimeError):
    """未配置密钥或接口调用失败。"""

    def __init__(self, message: str, *, kind="model_unavailable", tokens=0, failures=None):
        super().__init__(message)
        self.kind = kind
        self.tokens = tokens
        self.failures = failures or []


async def post_model(payload, *, timeout, purpose, protected=None):
    """The only model HTTP boundary. Audit must commit before transmitting.

    Restore only the response of this call, locally and before existing exact
    evidence/number validators. Restored response bodies are never audit metadata.
    """
    from app.core.db import get_sessionmaker
    from app.core.egress_models import LlmCallAudit

    settings = get_settings()
    ctx = egress_context.get()
    factory = ctx.session_factory or get_sessionmaker()
    redactor = Redactor(ctx.aliases)
    safe_payload = dict(payload)
    if settings.llm_redaction == "on":
        safe_payload["messages"] = []
        for original in payload.get("messages", []):
            message = dict(original)
            content = message.get("content", "")
            try:
                parsed = json.loads(content)
            except (ValueError, TypeError):
                message["content"] = redactor.tree(content)
            else:
                message["content"] = json.dumps(redactor.tree(parsed), ensure_ascii=False)
            safe_payload["messages"].append(message)
    fingerprint = hmac.new(
        settings.jwt_secret.encode(),
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode(),
        hashlib.sha256,
    ).hexdigest()
    hits = dict(redactor.counts)
    if protected:
        hits["PREPROTECTED"] = len(protected)
    meta = dict(
        call_id=uuid.uuid4(),
        org_id=ctx.org_id,
        purpose=purpose,
        model=str(payload.get("model", ""))[:200],
        input_hash=fingerprint,
        redaction_mode=settings.llm_redaction,
        redaction_hits=hits,
    )

    async def audit(stage, usage=None, duration=0):
        usage = usage or {}
        async with factory() as session:
            session.add(
                LlmCallAudit(
                    **meta,
                    stage=stage,
                    tokens_in=max(0, int(usage.get("prompt_tokens") or 0)),
                    tokens_out=max(0, int(usage.get("completion_tokens") or 0)),
                    duration_ms=duration,
                )
            )
            await session.commit()

    try:
        await audit("started")
    except Exception:
        raise LlmUnavailableError(
            "模型出站审计不可用，未发送请求", kind="egress_audit_error"
        ) from None
    started = time.perf_counter()
    usage, stage = {}, "transport_error"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                settings.llm_base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json=safe_payload,
            )
        stage = "success" if response.is_success else "http_error"
        if response.is_success:
            body = response.json()
            usage = body.get("usage") or {}
            for choice in body.get("choices") or []:
                message = choice.get("message") or {}
                content = message.get("content")
                if not isinstance(content, str):
                    continue
                try:
                    parsed = parse_model_json(content)
                except ValueError:
                    message["content"] = redactor.restore(content)
                else:
                    message["content"] = json.dumps(redactor.restore(parsed), ensure_ascii=False)
            response = httpx.Response(response.status_code, json=body, request=response.request)
        return response
    except (ValueError, KeyError):
        stage = "response_format_error"
        raise
    finally:
        # A failed completion audit leaves a durable started record for investigation;
        # it cannot make an unaudited response usable by the pipeline.
        try:
            await audit(stage, usage, int((time.perf_counter() - started) * 1000))
        except Exception:
            raise LlmUnavailableError("模型出站结果审计不可用", kind="egress_audit_error") from None


@dataclass
class LlmResult:
    data: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    model: str
    request_attempts: int = 1
    failures: list[dict] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def parse_model_json(content: str) -> dict[str, Any]:
    """解析模型返回的 JSON;容忍代码围栏和非法反斜杠转义(只修格式,不改内容)。"""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = json.loads(_BAD_ESCAPE.sub(r"\\\\", text))
    if not isinstance(data, dict):
        raise ValueError("模型未返回 JSON 对象")
    return data


async def call_llm_json(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 6000,
    model: str | None = None,
    timeout: float | None = None,
) -> LlmResult:
    """调用大模型并要求返回 JSON;格式异常或网络错误时重试一次,仍失败抛出 LlmUnavailableError。"""
    settings = get_settings()
    if not settings.llm_api_key:
        raise LlmUnavailableError("未配置大模型密钥(环境变量 CYSH_LLM_API_KEY)")
    requested_model = model or settings.llm_model
    request_timeout = timeout if timeout is not None else settings.llm_analysis_timeout_seconds
    last_error = ""
    error_kind = "model_unavailable"
    started = time.perf_counter()
    prompt_tokens = completion_tokens = 0
    failures = []
    for _attempt in range(MAX_ATTEMPTS):
        try:
            resp = await post_model(
                {
                    "model": requested_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    # 推理型模型默认会耗尽 max_tokens 而返回空内容,分析任务只需低强度推理
                    "reasoning_effort": "low",
                },
                timeout=request_timeout,
                purpose="analysis_json",
            )
            resp.raise_for_status()
            body = resp.json()
            usage = body.get("usage") or {}
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)
            choice = body["choices"][0]
            if choice.get("finish_reason") == "length":
                raise ValueError("模型输出被长度上限截断")
            data = parse_model_json(choice["message"]["content"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            error_kind = (
                "model_unavailable" if isinstance(exc, httpx.HTTPError) else "model_format_error"
            )
            # Do not retain provider URLs, request headers or arbitrary response bodies.
            last_error = (
                f"HTTP {exc.response.status_code}"
                if isinstance(exc, httpx.HTTPStatusError)
                else "output_truncated"
                if isinstance(exc, ValueError) and str(exc) == "模型输出被长度上限截断"
                else type(exc).__name__
            )
            failures.append({"attempt": _attempt + 1, "kind": error_kind, "reason": last_error})
            continue
        return LlmResult(
            data=data,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=int((time.perf_counter() - started) * 1000),
            model=str(body.get("model") or requested_model),
            request_attempts=_attempt + 1,
            failures=failures,
        )
    raise LlmUnavailableError(
        f"大模型调用失败(共 {MAX_ATTEMPTS} 次请求): {last_error}",
        kind=error_kind,
        tokens=prompt_tokens + completion_tokens,
        failures=failures,
    )
