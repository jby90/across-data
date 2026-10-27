"""Read-time exclusion of superseded page cuts; no edits to source or approvals.

A pending page repair must not silently replace legal evidence. All old clauses on
that source page stay in history but are withheld from new graph/report evidence.
The append-only proposal is the safety marker; even an old approved clause is not
rewritten or reapproved by the repair.
"""

from sqlalchemy import String, cast, or_, select

from app.ingestion.models import Clause
from app.knowledge.models import KnowledgeChangeProposal


def clause_layout_available():
    revision = KnowledgeChangeProposal.payload["source_parse_revision"]
    same_page = (
        (revision["version_id"].as_string() == cast(Clause.version_id, String))
        & (revision["page_number"].as_integer() >= Clause.page_start)
        & (revision["page_number"].as_integer() <= Clause.page_end)
    )
    return (
        ~select(KnowledgeChangeProposal.id)
        .where(
            KnowledgeChangeProposal.target_type == "clauses",
            KnowledgeChangeProposal.status.in_(("pending_review", "approved")),
            revision["scope"].as_string() == "whole_source_page",
            or_(KnowledgeChangeProposal.target_id == Clause.id, same_page),
        )
        .correlate(Clause)
        .exists()
    )


async def layout_held_clause_ids(session, clause_ids):
    if not clause_ids:
        return []
    return list(
        (
            await session.scalars(
                select(Clause.id).where(Clause.id.in_(clause_ids), ~clause_layout_available())
            )
        ).all()
    )
