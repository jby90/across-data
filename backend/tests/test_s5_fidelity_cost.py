"""Unseen structures/languages; no frozen suite IDs or expected answers."""

import copy
import json
from types import SimpleNamespace

import pytest

from app.analysis import retrieval_cache, retrieval_planner
from app.analysis.llm import LlmResult
from app.analysis.retrieval import ClauseIndex
from app.analysis.source_context import fragment_links, include_context_roots

from .test_s5_quality_followup import candidate


@pytest.mark.parametrize("heading", ["Definitions", "Définitions", "تعاريف", "未知格式"])
def test_only_archive_proven_continuations_link_to_root(heading):
    a = candidate("root", "Opening source and first definition.", heading=heading)
    b = candidate("child", "Second full definition from continuation.", heading=heading)
    a.label, b.label = "17", "17.8"
    b.version_id = a.version_id
    a.section_path = b.section_path = "ANNEX B"
    draft = SimpleNamespace(clause_number="17", section_path="ANNEX B", text=a.text + "\n" + b.text)
    before = copy.deepcopy([a, b])
    assert fragment_links([a, b], [draft]) == {"child": "root"}
    # Real decimal numbering alone, another version/section or changed prefix proves nothing.
    assert fragment_links([a, b], []) == {}
    assert fragment_links([a, b], [draft, copy.deepcopy(draft)]) == {}
    assert [a, b] == before
    b.version_id = "other-version"
    assert fragment_links([a, b], [draft]) == {}
    b.version_id = a.version_id
    draft.text = "Different beginning " + b.text
    assert fragment_links([a, b], [draft]) == {}


def test_context_parent_uses_real_slot_and_never_substitutes_evidence():
    a, b, c = [candidate(x, x * 100) for x in ("a", "b", "c")]
    b.context_parent_id = "a"
    assert [r.clause_id for r, _ in include_context_roots([(b, 1), (c, 0.5)], [a, b, c], 2)] == [
        "b",
        "a",
    ]
    assert b.text == "b" * 100 and b.clause_id == "b"


async def test_cache_no_cross_scope_source_config_reuse_or_hidden_cost(monkeypatch):
    retrieval_cache._entries.clear()
    rows = [candidate("a", "Always protect confidential information.")]
    index = ClauseIndex(rows)
    config = dict(system_prompt="JSON", model="test", temperature=0.0, timeout=5.0)
    calls = []

    async def fake(messages, **kwargs):
        phase = json.loads(messages[1]["content"])["phase"]
        calls.append(phase)
        return LlmResult(
            {"queries": ["protect"], "heading_queries": ["protect"]}
            if phase == "plan"
            else {"clause_ids": ["a"]},
            20,
            10,
            1,
            "test",
        )

    monkeypatch.setattr(retrieval_planner, "call_llm_json", fake)

    async def run(scope="org-A", cfg=None):
        return await retrieval_planner.planned_search(
            index, "protect", "安全", "保护?", 1, cfg or config, cache_scope=scope
        )

    first, cold = await run()
    second, warm = await run()
    assert first == second and cold["tokens"] == 60
    assert warm["cache_hit"] and warm["tokens"] == warm["requests"] == 0
    assert warm["origin_tokens"] == 60 and warm["ledger"] == []
    assert len(calls) == 2
    await run("org-B")
    await run(cfg={**config, "temperature": 0.2})
    rows[0].review_status = "rejected"
    await run()
    assert len(calls) == 8
    key = cold["cache_key"]
    saved = retrieval_cache.get(key)
    saved[1]["tokens"] = 999
    assert retrieval_cache.get(key)[1]["tokens"] == 60
    monkeypatch.setattr(retrieval_cache, "TTL_SECONDS", -1)
    assert retrieval_cache.get(key) is None
