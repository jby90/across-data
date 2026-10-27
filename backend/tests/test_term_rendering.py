import pytest
from sqlalchemy import func, select

from app.knowledge.legal_terms import current_definitions, rebuild_library, translation_terms
from app.knowledge.models import LegalTermDefinition
from app.knowledge.term_rendering import Rendering, append_rendering

from .test_legal_terminology import source


async def test_chinese_reading_aid_is_append_only_and_keeps_unknown_original(session_factory):
    async with session_factory() as s:
        doc, version, clause = await source(
            s, "“routing party” means a person receiving information for 3 days."
        )
        await rebuild_library(s)
        old = (await current_definitions(s, "XY", doc.id))[0]
        value = Rendering(
            definition_id=old.id,
            target_zh="路由参与方",
            definition_zh="路由参与方是指接收信息达3天的人。",
        )
        row = await append_rendering(s, old, value)
        assert row.id != old.id and row.definition_original == old.definition_original
        assert row.registration_origin == "machine_translation" and old.definition_zh is None
        assert (await append_rendering(s, old, value)).id == row.id
        active = (await current_definitions(s, "XY", doc.id))[0]
        assert active.id == row.id and active.target_zh == "路由参与方"
        selected = await translation_terms(s, clause, "XY", [])
        assert next(t for t in selected if t.definition_id == str(row.id)).preserve_original
        assert version.review_status == "pending_review" and clause.needs_review
        assert await s.scalar(select(func.count()).select_from(LegalTermDefinition)) == 2


async def test_missing_number_and_unprescribed_rendering_never_enter_library(session_factory):
    async with session_factory() as s:
        doc, _, _ = await source(
            s,
            "“controller” means a person deciding the purposes of processing personal data within 3 days.",
        )
        await rebuild_library(s)
        old = (await current_definitions(s, "XY", doc.id))[0]
        with pytest.raises(ValueError, match="规范"):
            await append_rendering(
                s,
                old,
                Rendering(
                    definition_id=old.id,
                    target_zh="控制器",
                    definition_zh="控制器是指在3天内决定处理个人数据目的的人。",
                ),
            )
        with pytest.raises(ValueError, match="3"):
            await append_rendering(
                s,
                old,
                Rendering(
                    definition_id=old.id,
                    target_zh="数据控制者",
                    definition_zh="数据控制者是指决定处理个人数据目的的人。",
                ),
            )
        assert await s.scalar(select(func.count()).select_from(LegalTermDefinition)) == 1
