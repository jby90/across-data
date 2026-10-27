"""Review policy and immutable-report review summaries; never infer human approval."""


def fully_reviewed(source_status, translation_status, language=""):
    return source_status == "approved" and (
        translation_status == "approved" or language.lower().startswith("zh")
    )


def review_summary(dimensions):
    unique = {}
    occurrences = 0
    for dimension in dimensions:
        for cite in dimension.get("citations", []):
            occurrences += 1
            key = (
                cite.get("clause_id") or cite.get("citation_tag"),
                cite.get("version_id") or cite.get("version_no"),
                cite.get("translation_id"),
            )
            # Legacy `review_status=approved` only referred to the translation.
            # Absent source approval cannot be upgraded to a legal review approval.
            approved = fully_reviewed(
                cite.get("source_review_status"),
                cite.get("translation_review_status"),
                cite.get("language", ""),
            )
            unique[key] = unique.get(key, True) and approved
    approved = sum(unique.values())
    return {
        "approved": approved,
        "pending": len(unique) - approved,
        "total": len(unique),
        "occurrences": occurrences,
    }


def review_label(cite):
    if fully_reviewed(
        cite.get("source_review_status"),
        cite.get("translation_review_status"),
        cite.get("language", ""),
    ):
        return "引用材料已复核（分析结论仍为机器草稿）"
    return "待核对（原文/译文尚未全部人工复核）"


def review_overview(dimensions):
    counts = review_summary(dimensions)
    return (
        f"引用复核概览：已复核 {counts['approved']} 条 / 待核对 {counts['pending']} 条"
        f"（按条款及版本去重，共 {counts['occurrences']} 处引用）；程序核验不等于人工法律复核。"
    )


def policy_label(policy):
    return {
        "allow_machine_with_notice": "允许机器内容进入草稿，逐条待核对",
        "reviewed_only": "仅引用已复核材料",
    }.get(policy, "历史未记录，不推定为已复核")


def review_detail(cite):
    labels = {
        "approved": "已复核",
        "pending_review": "待核对",
        "machine_translated": "机翻待核对",
        "not_translated": "未提供",
        "ambiguous_current": "现行译文有冲突，待核对",
    }
    return (
        "原文"
        + labels.get(cite.get("source_review_status"), "未记录，待核对")
        + " / 译文"
        + labels.get(cite.get("translation_review_status"), "未记录，待核对")
    )
