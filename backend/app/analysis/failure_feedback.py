"""Safe reader-facing reasons derived from persisted results, without reclassifying them."""


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def failure_feedback(status, traces, reports, phase_labels=None):
    if status != "failed":
        return None
    # Read only required fields: do not copy model prompts, complete tool payloads
    # or report bodies just to calculate a short public status message.
    traces = [
        {
            "phase": _get(t, "phase"),
            "created_at": _get(t, "created_at"),
            "input_snapshot": _get(t, "input_snapshot", {}),
            "output_snapshot": _get(t, "output_snapshot", {}),
            "tool_calls": [
                {"arguments": _get(c, "arguments", {})} for c in _get(t, "tool_calls", [])
            ],
        }
        for t in traces
    ]
    reports = [
        {
            "version_no": _get(r, "version_no"),
            "created_at": _get(r, "created_at"),
            "dimensions": [
                {key: _get(d, key) for key in ("dimension_code", "status", "citations")}
                for d in _get(r, "dimensions", [])
            ],
        }
        for r in reports
    ]
    # A new attempt must not display an older report's failure as its own result.
    boundary = max((i for i, t in enumerate(traces) if t["phase"] == "fact_check"), default=0)
    current = traces[boundary:]
    latest = max(reports, key=lambda r: r["version_no"], default=None)
    if latest and current and latest["created_at"] < current[0]["created_at"]:
        latest = None
    errors = [t for t in current if t["phase"] == "error"]
    if errors and (not latest or errors[-1]["created_at"] > latest["created_at"]):
        phase = (errors[-1].get("input_snapshot") or {}).get("failed_phase")
        phase_label = (phase_labels or {}).get(phase, "评估")
        return {
            "title": "本次评估未完成",
            "summary": f"{phase_label}环节中断，当前对话和已填写回答已保存。",
            "issues": [],
            "next_action": "可将对话编号提供给管理员定位；修复后可在原对话重试。",
            "retained_citations": 0,
            "report_version": None,
        }
    outputs, kinds = {}, {}
    for trace in current:
        outputs.update((trace.get("output_snapshot") or {}).get("dimension_outputs", {}))
        for call in trace.get("tool_calls", []):
            args = call.get("arguments") or {}
            if args.get("dimension") and args.get("error_kind"):
                kinds[args["dimension"]] = args["error_kind"]
    issues = []
    technical = {code for code, r in outputs.items() if r.get("error")}
    groups = {}
    for code in sorted(technical):
        kind = kinds.get(code)
        label = (
            "分析输出未通过格式检查"
            if kind == "model_format_error"
            else "分析响应超时"
            if kind == "model_timeout"
            else "分析服务调用未完成"
        )
        groups.setdefault(label, []).append(code)
    issues.extend({"label": label, "dimension_codes": codes} for label, codes in groups.items())
    dimensions = latest.get("dimensions", []) if latest else []
    evidence = [
        d["dimension_code"]
        for d in dimensions
        if d["status"] != "COMPLETE" and d["dimension_code"] not in technical
    ]
    if evidence:
        issues.append(
            {"label": "依据未通过完整核验，请展开对应维度查看原因", "dimension_codes": evidence}
        )
    count = sum(len(d.get("citations", [])) for d in dimensions)
    return {
        "title": "本次评估未完成",
        "summary": f"已保留 {count} 处通过核验的引用，整份报告尚未通过检查。"
        if latest
        else "当前对话和已填写回答已保存。",
        "issues": issues,
        "next_action": "请先核对相应维度的原文与依据，补充后再继续分析。"
        if evidence
        else "可在原对话输入“重试”，无需重新填写已保存的回答。",
        "retained_citations": count,
        "report_version": latest["version_no"] if latest else None,
    }
