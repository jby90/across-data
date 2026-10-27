"""Per-run model usage; never log request headers, credentials or reasoning content."""

from contextvars import ContextVar

_run = ContextVar("kg_run_usage", default=None)


def begin_usage():
    usage = dict(requests=0, responses_with_usage=0, tokens_in=0, tokens_out=0, response_models={})
    return _run.set(usage), usage


def end_usage(token):
    _run.reset(token)


def request_started():
    if (usage := _run.get()) is not None:
        usage["requests"] += 1


def response_received(response):
    usage = _run.get()
    if usage is None:
        return
    reported = response.get("usage") or {}
    if reported.get("prompt_tokens") is not None and reported.get("completion_tokens") is not None:
        usage["responses_with_usage"] += 1
        usage["tokens_in"] += int(reported["prompt_tokens"])
        usage["tokens_out"] += int(reported["completion_tokens"])
    model = str(response.get("model") or "unreported")
    usage["response_models"][model] = usage["response_models"].get(model, 0) + 1
