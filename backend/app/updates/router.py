"""Authenticated station reminders. No email is sent or external messaging configured."""

import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.models import AnalysisTask
from app.core.db import get_session
from app.iam.deps import Principal, require_admin_side, require_user_side
from app.ingestion.models import Document, DocumentVersion
from app.updates.models import RegulatoryDiscovery, UpdateImpact, UserNotification, VersionChange
from app.updates.service import now_utc

user_router = APIRouter(prefix="/api/notifications", tags=["notifications"])
admin_router = APIRouter(prefix="/api/admin/updates", tags=["updates"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]
UserDep = Annotated[Principal, Depends(require_user_side)]
AdminDep = Annotated[Principal, Depends(require_admin_side)]


@user_router.get("")
async def list_notifications(
    user: UserDep,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    conditions = (UserNotification.user_id == user.user_id, UserNotification.org_id == user.org_id)
    unread = await session.scalar(
        select(func.count())
        .select_from(UserNotification)
        .where(*conditions, UserNotification.read_at.is_(None))
    )
    total = await session.scalar(
        select(func.count()).select_from(UserNotification).where(*conditions)
    )
    rows = (
        await session.execute(
            select(UserNotification, VersionChange, Document, AnalysisTask)
            .join(VersionChange, VersionChange.id == UserNotification.change_id)
            .join(Document, Document.id == VersionChange.document_id)
            .join(AnalysisTask, AnalysisTask.id == UserNotification.task_id)
            .where(*conditions, AnalysisTask.org_id == user.org_id)
            .order_by(UserNotification.created_at.desc(), UserNotification.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    items = []
    for notification, change, document, task in rows:
        impacts = list(
            (
                await session.scalars(
                    select(UpdateImpact).where(
                        UpdateImpact.task_id == task.id,
                        UpdateImpact.change_id == change.id,
                        UpdateImpact.org_id == user.org_id,
                    )
                )
            ).all()
        )
        items.append(
            {
                "id": str(notification.id),
                "created_at": notification.created_at,
                "read_at": notification.read_at,
                "task_id": str(task.id),
                "task_title": task.title,
                "document_title": document.title,
                "country_code": document.country_code,
                "source_url": document.source_url,
                "summary": change.summary,
                "change_id": str(change.id),
                "change_kind": change.change_kind,
                "dimension_codes": sorted({i.dimension_code for i in impacts}),
                "state": "open" if any(i.state == "open" for i in impacts) else "resolved",
                "due_at": min((i.due_at for i in impacts), default=None),
                "human_review": "pending_review",
            }
        )
    return {"items": items, "unread": unread, "total": total, "offset": offset, "limit": limit}


async def own_notification(session, principal, notification_id):
    row = await session.scalar(
        select(UserNotification)
        .where(
            UserNotification.id == notification_id,
            UserNotification.user_id == principal.user_id,
            UserNotification.org_id == principal.org_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "通知不存在")
    return row


@user_router.post("/{notification_id}/read")
async def read_notification(notification_id: uuid.UUID, user: UserDep, session: SessionDep):
    row = await own_notification(session, user, notification_id)
    row.read_at = row.read_at or now_utc()
    await session.commit()
    return {"id": str(row.id), "read_at": row.read_at}


@user_router.post("/{notification_id}/reevaluate")
async def reevaluate(notification_id: uuid.UUID, user: UserDep, session: SessionDep):
    from app.analysis.router import _start

    notification = await own_notification(session, user, notification_id)
    task = await session.scalar(
        select(AnalysisTask)
        .where(AnalysisTask.id == notification.task_id, AnalysisTask.org_id == user.org_id)
        .with_for_update()
    )
    if task is None:
        raise HTTPException(404, "分析任务不存在")
    if task.status in ("running", "awaiting_user_facts"):
        raise HTTPException(409, "请先完成正在运行的任务或当前业务追问")
    if task.sufficiency_score < 100:
        raise HTTPException(409, "请先补齐本任务的业务事实")
    notification.read_at = notification.read_at or now_utc()
    # Existing reports remain intact. A new version resolves the operational impact
    # only if it actually cites the currently observed source in the affected dimension.
    await _start(session, task)
    return {"task_id": str(task.id), "status": task.status}


def discovery_query():
    # Aggregate once in SQL; do not load every document/version into the API process.
    versions = (
        select(
            DocumentVersion.document_id,
            func.min(DocumentVersion.parsed_at)
            .filter(DocumentVersion.state == "parsed")
            .label("completed"),
            func.bool_or(
                (DocumentVersion.state == "parsed") & DocumentVersion.parsed_at.is_(None)
            ).label("unknown"),
        )
        .group_by(DocumentVersion.document_id)
        .subquery()
    )
    return (
        select(
            RegulatoryDiscovery.id,
            RegulatoryDiscovery.country_code,
            func.coalesce(Document.title, RegulatoryDiscovery.title).label("title"),
            RegulatoryDiscovery.source_url,
            RegulatoryDiscovery.discovered_at,
            versions.c.completed.label("first_parsed_at"),
            func.coalesce(versions.c.unknown, False).label("history_unknown"),
        )
        .outerjoin(
            Document,
            or_(
                Document.id == RegulatoryDiscovery.document_id,
                RegulatoryDiscovery.document_id.is_(None)
                & (Document.site_id == RegulatoryDiscovery.site_id)
                & (Document.source_url == RegulatoryDiscovery.source_url),
            ),
        )
        .outerjoin(versions, versions.c.document_id == Document.id)
    )


def discovery_items(rows, now):
    items = []
    for row in rows:
        item = dict(row)
        if item["history_unknown"]:
            item["first_parsed_at"] = None
        finished = item["first_parsed_at"]
        item["overdue"] = not item["history_unknown"] and (finished or now) > item[
            "discovered_at"
        ] + timedelta(days=30)
        item["elapsed_hours"] = (
            None
            if item["history_unknown"]
            else round(((finished or now) - item["discovered_at"]).total_seconds() / 3600, 2)
        )
        items.append(item)
    return items


@admin_router.get("/discoveries")
async def discoveries(
    _: AdminDep,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    country: str | None = Query(None, min_length=1, max_length=8),
):
    query = discovery_query()
    if country:
        query = query.where(RegulatoryDiscovery.country_code == country.upper())
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        (
            await session.execute(
                query.order_by(RegulatoryDiscovery.discovered_at.desc(), RegulatoryDiscovery.id)
                .offset(offset)
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )
    return {
        "items": discovery_items(rows, now_utc()),
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@admin_router.get("/sla")
async def sla(_: AdminDep, session: SessionDep):
    now = now_utc()
    groups = (
        select(
            UpdateImpact.task_id,
            UpdateImpact.change_id,
            func.bool_or(UpdateImpact.state == "open").label("is_open"),
            func.min(UpdateImpact.due_at).label("due"),
        )
        .group_by(UpdateImpact.task_id, UpdateImpact.change_id)
        .subquery()
    )
    counts = (
        (
            await session.execute(
                select(
                    func.count().filter(groups.c.is_open.is_(True)).label("pending_updates"),
                    func.count()
                    .filter(groups.c.is_open.is_(True) & (groups.c.due < now))
                    .label("overdue_72h"),
                    func.count().filter(groups.c.is_open.is_(False)).label("resolved_updates"),
                ).select_from(groups)
            )
        )
        .mappings()
        .one()
    )
    query = discovery_query()
    tracked = query.subquery()
    known = tracked.c.history_unknown.is_(False)
    intake = (
        (
            await session.execute(
                select(
                    func.count()
                    .filter(known & tracked.c.first_parsed_at.is_(None))
                    .label("pending"),
                    func.count()
                    .filter(known & tracked.c.first_parsed_at.is_not(None))
                    .label("completed"),
                    func.count()
                    .filter(
                        known
                        & (
                            func.coalesce(tracked.c.first_parsed_at, now)
                            > tracked.c.discovered_at + timedelta(days=30)
                        )
                    )
                    .label("overdue_30d"),
                    func.count()
                    .filter(tracked.c.history_unknown.is_(True))
                    .label("history_unknown"),
                ).select_from(tracked)
            )
        )
        .mappings()
        .one()
    )
    untracked = await session.scalar(
        select(func.count())
        .select_from(Document)
        .where(
            ~select(RegulatoryDiscovery.id)
            .where(
                or_(
                    RegulatoryDiscovery.document_id == Document.id,
                    (RegulatoryDiscovery.site_id == Document.site_id)
                    & (RegulatoryDiscovery.source_url == Document.source_url),
                )
            )
            .exists()
        )
    )
    late_notices = await session.scalar(
        select(func.count())
        .select_from(UserNotification)
        .join(UpdateImpact, UpdateImpact.id == UserNotification.impact_id)
        .where(UserNotification.created_at > UpdateImpact.due_at)
    )
    rows = (
        (await session.execute(query.order_by(RegulatoryDiscovery.discovered_at.desc()).limit(50)))
        .mappings()
        .all()
    )
    total = await session.scalar(select(func.count()).select_from(RegulatoryDiscovery))
    return {
        **dict(counts),
        "late_notification_records": late_notices,
        "intake": {
            **dict(intake),
            "untracked_documents": untracked,
            "definition": "发现至首次成功解析入库；与官方公布日期、人工审批分开，历史时间不推测",
        },
        "discoveries": discovery_items(rows, now),
        "discoveries_total": total,
        "countries": list(
            (
                await session.scalars(
                    select(RegulatoryDiscovery.country_code)
                    .distinct()
                    .order_by(RegulatoryDiscovery.country_code)
                )
            ).all()
        ),
        "human_review": "pending_review",
        "email_enabled": False,
    }


@admin_router.get("/changes")
async def changes(
    _: AdminDep,
    session: SessionDep,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    country: str | None = Query(None, min_length=1, max_length=8),
):
    query = select(VersionChange, Document).join(Document, Document.id == VersionChange.document_id)
    if country:
        query = query.where(Document.country_code == country.upper())
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await session.execute(
            query.order_by(VersionChange.detected_at.desc(), VersionChange.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    items = [
        {
            "id": str(c.id),
            "document_title": d.title,
            "country_code": d.country_code,
            "source_url": d.source_url,
            "old_version_id": str(c.old_version_id),
            "new_version_id": str(c.new_version_id),
            "detected_at": c.detected_at,
            "kind": c.change_kind,
            "summary": c.summary,
            "changes": c.changes_json,
            "graph_edge_ids": c.graph_edge_ids,
            "content_hashes": c.content_hashes,
            "human_review": "pending_review",
        }
        for c, d in rows
    ]
    return {"items": items, "total": total, "offset": offset, "limit": limit}
