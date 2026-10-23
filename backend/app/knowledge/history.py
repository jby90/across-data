"""Append-only approval/evidence snapshots and proposals; no model-written approval reuse."""

import hashlib
import json
import uuid
from datetime import date, datetime

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.models import KnowledgeAuditRecord, KnowledgeChangeProposal


def record_payload(record) -> dict:
    values = {column.key: getattr(record, column.key) for column in inspect(record).mapper.columns}
    return json.loads(
        json.dumps(
            values,
            default=lambda value: (
                value.isoformat() if isinstance(value, date | datetime) else str(value)
            ),
        )
    )


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def snapshot(session: AsyncSession, record, action: str, actor_id: uuid.UUID | None = None):
    payload = record_payload(record)
    audit = KnowledgeAuditRecord(
        target_type=record.__tablename__,
        target_id=record.id,
        action=action,
        payload=payload,
        content_hash=payload_hash(payload),
        actor_id=actor_id,
    )
    session.add(audit)
    return audit


async def propose(session: AsyncSession, record, payload: dict):
    digest = payload_hash(payload)
    proposal = await session.scalar(
        select(KnowledgeChangeProposal).where(
            KnowledgeChangeProposal.target_type == record.__tablename__,
            KnowledgeChangeProposal.target_id == record.id,
            KnowledgeChangeProposal.content_hash == digest,
        )
    )
    if proposal is None:
        proposal = KnowledgeChangeProposal(
            target_type=record.__tablename__,
            target_id=record.id,
            payload=payload,
            content_hash=digest,
            status="pending_review",
        )
        session.add(proposal)
    return proposal
