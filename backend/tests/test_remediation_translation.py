import pytest
from sqlalchemy import func, select

from app.cli import batch_translate
from app.knowledge import translator
from app.knowledge.models import ClauseTranslation, KnowledgeAuditRecord

from .conftest import bearer, login
from .review_s1_s2_contracts import make_document


@pytest.mark.parametrize(
    "original,zh,expected",
    [
        ("A maximum of 10% applies.", "最高适用百分之10，应当依法计算并执行。", True),
        ("The fee is $500.", "费用为500欧元，应按规定缴纳相关费用。", False),
        ("The fee is SGD 500.", "费用为500新元，应按规定缴纳相关费用。", True),
        ("On 19 December 2022 it enters into force.", "于2022年11月19日起正式生效施行。", False),
        ("On 19 December 2022 it enters into force.", "于2022年12月19日起正式生效施行。", True),
        ("The number is ١٠.", "该项编号应当按照规定记录为10并予以保存。", True),
    ],
)
def test_numeric_units_and_dates(original, zh, expected):
    assert translator.verify_translation(original, zh, [])[0] is expected


async def test_failed_retry_keeps_good_current_text_and_all_attempt_costs(
    session_factory, monkeypatch
):
    async with session_factory() as session:
        _, _, clauses = await make_document(session)

        async def good(*args, **kwargs):
            return "组织必须保护个人数据并依法通知监管机构处理泄露。", "fake", 10, 20, 5

        monkeypatch.setattr(translator, "_call_llm_translate", good)
        first = await translator.translate_clause(session, clauses[0], "SG", [])
        await session.commit()
        text = first.translated_text

        async def bad(*args, **kwargs):
            return "not Chinese", "fake", 3, 4, 2

        monkeypatch.setattr(translator, "_call_llm_translate", bad)
        second = await translator.translate_clause(session, clauses[0], "SG", [])
        await session.commit()
        assert first.id != second.id
        assert first.is_current and first.translated_text == text
        assert not second.is_current and second.review_status == "translation_failed"
        assert second.tokens_in == 6 and second.tokens_out == 8
        assert await session.scalar(select(func.count()).select_from(ClauseTranslation)) == 2
        assert await session.scalar(select(func.count()).select_from(KnowledgeAuditRecord)) == 2


async def test_dry_run_full_missing_is_read_only(session_factory, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("dry run must not call model")

    monkeypatch.setattr(translator, "_call_llm_translate", forbidden)
    async with session_factory() as session:
        await make_document(session, [f"Clause {i}: Protect personal data." for i in range(21)])
        await session.commit()
    stats = await batch_translate("SG", sf=session_factory, dry_run=True)
    assert stats["total"] == 21 and not stats["limited"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ClauseTranslation)) == 0


async def test_translation_pagination_reaches_beyond_first_page(client, seed, session_factory):
    async with session_factory() as session:
        _, _, clauses = await make_document(session, [f"Clause {i}" for i in range(13)])
        for clause in clauses:
            session.add(
                ClauseTranslation(
                    clause_id=clause.id,
                    translated_text="个人数据依法保护。",
                    engine="llm",
                    model_name="test",
                    review_status="machine_translated",
                )
            )
        await session.commit()
    headers = bearer(await login(client, "admin", "admin@example.com"))
    first = await client.get("/api/admin/knowledge/translations?limit=10", headers=headers)
    second = await client.get(
        "/api/admin/knowledge/translations?limit=10&offset=10", headers=headers
    )
    assert first.headers["X-Total-Count"] == "13"
    assert len(first.json()) == 10 and len(second.json()) == 3
    assert not {row["id"] for row in first.json()} & {row["id"] for row in second.json()}


async def test_direct_translation_sends_explicit_mode_and_tracks_real_usage(monkeypatch):
    import json

    import httpx

    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-only")
    monkeypatch.setattr(settings, "llm_translation_thinking", "disabled")
    original_client = httpx.AsyncClient

    def handler(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in body
        return httpx.Response(
            200,
            json={
                "model": "actual-response-model",
                "choices": [{"message": {"content": "最高比例为10%，应依法履行规定。"}}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 45},
            },
        )

    monkeypatch.setattr(
        translator.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = await translator._call_llm_translate("The maximum percentage is 10%.", "en", [], [])
    assert result[1:4] == ("actual-response-model", 123, 45)


def test_privacy_placeholders_preserve_legal_reference_without_sending_original():
    text = "Reference 2020-1234; call +65 6123 4567; contact person@example.com."
    safe, mapping = translator._redact_with_mapping(text)
    assert "person@example.com" not in safe and "2020-1234" not in safe
    assert "__CYSH_PRIVATE_" in safe
    for marker, original in mapping.items():
        safe = safe.replace(marker, original)
    assert safe == text


@pytest.mark.parametrize("keep_marker", [True, False])
async def test_transport_restores_private_tokens_or_fails_quality(monkeypatch, keep_marker):
    import json

    import httpx

    settings = translator.get_settings()
    monkeypatch.setattr(settings, "llm_api_key", "test-only")
    original_client = httpx.AsyncClient
    original = "Contact person@example.com for lawful personal data protection."

    def handler(request):
        body = json.loads(request.content)
        assert "person@example.com" not in json.dumps(body)
        assert "__CYSH_PRIVATE_A__" in body["messages"][1]["content"]
        content = "请联系相关部门依法保护个人数据并履行保护义务。"
        if keep_marker:
            content += "__CYSH_PRIVATE_A__"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(
        translator.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    result = await translator._call_llm_translate(original, "en", [], [])
    assert ("person@example.com" in result[0]) is keep_marker
    assert translator.verify_translation(original, result[0], [])[0] is keep_marker
