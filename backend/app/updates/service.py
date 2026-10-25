"""Deterministic change tracking. A byte/text change is never a legal amendment verdict."""

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert

from app.analysis.models import AnalysisTask, ReportVersion
from app.iam.models import User
from app.ingestion.archive import ArchiveStore
from app.ingestion.models import Clause, Document, DocumentVersion
from app.knowledge.models import KgEdge
from app.updates.models import (
    RegulatoryDiscovery,
    SourceFetchObservation,
    UpdateImpact,
    UserNotification,
    VersionChange,
)

log = logging.getLogger(__name__)
REMINDER_WINDOW = timedelta(hours=72)


def now_utc():
    return datetime.now(UTC)


async def note_discovery(factory, site, candidate):
    """Commit discovery before downloading, so blocked/failed URLs remain measurable."""
    async with factory() as session:
        doc = await session.scalar(
            select(Document).where(
                Document.site_id == site.id, Document.source_url == candidate.url
            )
        )
        now = now_utc()
        await session.execute(
            insert(RegulatoryDiscovery)
            .values(
                id=uuid.uuid4(),
                site_id=site.id,
                country_code=site.country_code,
                source_url=candidate.url,
                title=(candidate.title or candidate.url)[:500],
                discovered_at=doc.discovered_at if doc else now,
                last_seen_at=now,
                document_id=doc.id if doc else None,
            )
            .on_conflict_do_update(constraint="uq_discovery_site_url", set_={"last_seen_at": now})
        )
        await session.commit()


async def observe_fetch(session, document, version, *, archived_previous=None, observed_at=None):
    # Ingestion updates document.last_checked_at first. Flushing serializes writers
    # of this source before selecting its preceding observed capture.
    await session.flush()
    prior = await session.scalar(
        select(SourceFetchObservation)
        .where(SourceFetchObservation.document_id == document.id)
        .order_by(SourceFetchObservation.id.desc())
        .limit(1)
    )
    if prior and prior.version_id == version.id:
        return None
    previous = prior.version_id if prior else archived_previous.id if archived_previous else None
    if previous == version.id:
        previous = None
    row = SourceFetchObservation(
        document_id=document.id,
        version_id=version.id,
        previous_version_id=previous,
        comparison_basis="previous_fetch"
        if prior
        else "archived_version"
        if previous
        else "first_capture",
        observed_at=observed_at or now_utc(),
    )
    session.add(row)
    await session.flush()
    return row


async def current_source_versions(session, document_ids=None):
    from app.updates.source_state import current_source_query

    query = current_source_query()
    if document_ids is not None:
        query = query.where(Document.id.in_(document_ids))
    return dict((await session.execute(query)).all())


def compare_clauses(old, new):
    """Align unique original labels; ambiguous or unnumbered groups stay explicit."""
    for clause in [*old, *new]:
        if ArchiveStore.sha256(clause.original_text.encode()) != clause.text_hash:
            return "source_integrity_pending", [
                {"kind": "clause_hash_mismatch", "clause_id": str(clause.id)}
            ]
    signature = lambda c: (c.section_path or "", c.clause_number or "", c.text_hash)  # noqa: E731
    if [signature(c) for c in old] == [signature(c) for c in new]:
        return "bytes_only", []
    groups = [defaultdict(list), defaultdict(list)]
    for index, clauses in enumerate((old, new)):
        for c in clauses:
            groups[index][(c.section_path or "", c.clause_number or "")].append(c)
    changes = []
    for key in sorted(set(groups[0]) | set(groups[1])):
        before, after = (g.get(key, []) for g in groups)
        if [c.text_hash for c in before] == [c.text_hash for c in after]:
            continue
        changes.append(
            {
                "kind": "added"
                if not before
                else "removed"
                if not after
                else "changed"
                if key[1] and len(before) == len(after) == 1
                else "alignment_pending",
                "section_path": key[0],
                "clause_number": key[1],
                "old": [{"clause_id": str(c.id), "sha256": c.text_hash} for c in before],
                "new": [{"clause_id": str(c.id), "sha256": c.text_hash} for c in after],
            }
        )
    return "text_change_pending_review", changes or [{"kind": "order_or_layout_changed"}]


async def create_change(session, old, new, *, origin_key, detected_at, observation_id=None):
    if old.document_id != new.document_id:
        raise ValueError("source versions must belong to the same document")
    a = list(
        (
            await session.scalars(
                select(Clause).where(Clause.version_id == old.id).order_by(Clause.ordinal)
            )
        ).all()
    )
    b = list(
        (
            await session.scalars(
                select(Clause).where(Clause.version_id == new.id).order_by(Clause.ordinal)
            )
        ).all()
    )
    if old.state == new.state == "parsed" and a and b:
        kind, changes = compare_clauses(a, b)
    else:
        kind, changes = (
            "parsing_pending",
            [{"kind": "parse_not_available", "old": old.state, "new": new.state}],
        )
    edge_ids = (
        list(
            (
                await session.scalars(
                    select(KgEdge.id).where(KgEdge.evidence_clause_id.in_([c.id for c in a]))
                )
            ).all()
        )
        if a
        else []
    )
    change = VersionChange(
        origin_key=origin_key,
        source_observation_id=observation_id,
        document_id=new.document_id,
        old_version_id=old.id,
        new_version_id=new.id,
        detected_at=detected_at,
        change_kind=kind,
        changes_json=changes,
        graph_edge_ids=[str(i) for i in edge_ids],
        content_hashes={"old": old.content_hash, "new": new.content_hash},
        summary="归档字节变化，已解析条款一致"
        if kind == "bytes_only"
        else ("来源或上传原文内容变化，是否构成法规修订及影响范围待核对；旧证据和审批记录保留。"),
    )
    session.add(change)
    await session.flush()
    return change


def cites_version(citation, version):
    return citation.get("version_id") == str(version.id) or (
        not citation.get("version_id")
        and citation.get("document_id") == str(version.document_id)
        and citation.get("version_no") == version.version_no
    )


async def link_report(session, task, report, changes=None):
    citations = [c for d in report.dimensions_json or [] for c in d.get("citations", [])]
    if not citations:
        return 0
    if changes is None:
        version_ids = [uuid.UUID(c["version_id"]) for c in citations if c.get("version_id")]
        changes = list(
            (
                await session.scalars(
                    select(VersionChange).where(
                        VersionChange.old_version_id.in_(version_ids),
                        VersionChange.change_kind != "bytes_only",
                    )
                )
            ).all()
        )
    if not changes:
        return 0
    current = await current_source_versions(session, [c.document_id for c in changes])
    pending = []
    for change in changes:
        old = await session.get(DocumentVersion, change.old_version_id)
        if old is None or change.change_kind == "bytes_only":
            continue
        # A later report may already incorporate a source's reversion to an older
        # identical archive. Keep the historical link without issuing a stale alarm.
        already_current = (
            current.get(change.document_id) == old.id
            and report.created_at
            and report.created_at >= change.detected_at
        )
        for dimension in report.dimensions_json or []:
            if any(cites_version(c, old) for c in dimension.get("citations", [])):
                pending.append(
                    {
                        "id": uuid.uuid4(),
                        "org_id": task.org_id,
                        "task_id": task.id,
                        "report_version_id": report.id,
                        "dimension_code": dimension["dimension_code"],
                        "change_id": change.id,
                        "detected_at": change.detected_at,
                        "due_at": change.detected_at + REMINDER_WINDOW,
                        "state": "resolved" if already_current else "open",
                        "resolved_report_id": report.id if already_current else None,
                        "resolved_at": now_utc() if already_current else None,
                    }
                )
    if not pending:
        return 0
    inserted = (
        await session.scalars(
            insert(UpdateImpact)
            .values(pending)
            .on_conflict_do_nothing(constraint="uq_report_change_impact")
            .returning(UpdateImpact.id)
        )
    ).all()
    return len(inserted)


async def reconcile_impacts(session, *, task_ids=None, now=None):
    """Set-based, bounded resolution and notification delivery; no full queue in RAM."""
    from sqlalchemy import String, cast
    from sqlalchemy.orm import aliased

    from app.updates.source_state import current_source_query

    now = now or now_utc()
    original, newest = aliased(ReportVersion), aliased(ReportVersion)
    latest = (
        select(ReportVersion.task_id, func.max(ReportVersion.version_no).label("number"))
        .group_by(ReportVersion.task_id)
        .subquery()
    )
    current = current_source_query().subquery()
    expected = func.jsonb_build_array(
        func.jsonb_build_object(
            "dimension_code",
            UpdateImpact.dimension_code,
            "status",
            "COMPLETE",
            "citations",
            func.jsonb_build_array(
                func.jsonb_build_object("version_id", cast(current.c.version_id, String))
            ),
        )
    )
    query = (
        select(UpdateImpact, newest.id)
        .join(original, original.id == UpdateImpact.report_version_id)
        .join(VersionChange, VersionChange.id == UpdateImpact.change_id)
        .join(latest, latest.c.task_id == UpdateImpact.task_id)
        .join(
            newest,
            (newest.task_id == UpdateImpact.task_id) & (newest.version_no == latest.c.number),
        )
        .join(current, current.c.document_id == VersionChange.document_id)
        .where(
            UpdateImpact.state == "open",
            newest.version_no > original.version_no,
            newest.status != "analysis_failed",
            newest.created_at >= VersionChange.detected_at,
            newest.dimensions_json.op("@>")(expected),
        )
    )
    if task_ids is not None:
        query = query.where(UpdateImpact.task_id.in_(task_ids))
    rows = (
        await session.execute(query.limit(500).with_for_update(of=UpdateImpact, skip_locked=True))
    ).all()
    for impact, report_id in rows:
        impact.state, impact.resolved_report_id, impact.resolved_at = "resolved", report_id, now
    await session.flush()
    anchors = select(
        UpdateImpact.id,
        UpdateImpact.org_id,
        UpdateImpact.task_id,
        UpdateImpact.change_id,
        func.row_number()
        .over(partition_by=(UpdateImpact.task_id, UpdateImpact.change_id), order_by=UpdateImpact.id)
        .label("position"),
    ).where(UpdateImpact.state == "open", UpdateImpact.org_id.is_not(None))
    if task_ids is not None:
        anchors = anchors.where(UpdateImpact.task_id.in_(task_ids))
    anchors = anchors.subquery()
    missing = (
        select(
            anchors.c.id,
            anchors.c.org_id,
            anchors.c.task_id,
            anchors.c.change_id,
            User.id.label("user_id"),
        )
        .join(User, User.org_id == anchors.c.org_id)
        .where(
            anchors.c.position == 1,
            User.is_active.is_(True),
            ~exists(
                select(UserNotification.id).where(
                    UserNotification.user_id == User.id,
                    UserNotification.task_id == anchors.c.task_id,
                    UserNotification.change_id == anchors.c.change_id,
                )
            ),
        )
        .limit(500)
    )
    recipients = (await session.execute(missing)).mappings().all()
    if not recipients:
        return 0
    inserted = (
        await session.scalars(
            insert(UserNotification)
            .values(
                [
                    {
                        "id": uuid.uuid4(),
                        "impact_id": r["id"],
                        "org_id": r["org_id"],
                        "task_id": r["task_id"],
                        "change_id": r["change_id"],
                        "user_id": r["user_id"],
                        "created_at": now,
                    }
                    for r in recipients
                ]
            )
            .on_conflict_do_nothing(constraint="uq_change_recipient")
            .returning(UserNotification.id)
        )
    ).all()
    return len(inserted)


async def monitor_once(factory, *, now=None, batch_size=50):
    """Recoverable database sweep, including changes from manual uploads/imports."""
    now = now or now_utc()
    async with factory() as session:
        if not await session.scalar(text("SELECT pg_try_advisory_xact_lock(64042010)")):
            return {"changes": 0, "impacts": 0, "notifications": 0, "busy": True}
        added = []
        observations = list(
            (
                await session.scalars(
                    select(SourceFetchObservation)
                    .where(
                        SourceFetchObservation.previous_version_id.is_not(None),
                        ~exists(
                            select(VersionChange.id).where(
                                VersionChange.source_observation_id == SourceFetchObservation.id
                            )
                        ),
                    )
                    .order_by(SourceFetchObservation.id)
                    .limit(batch_size)
                )
            ).all()
        )
        for observation in observations:
            old = await session.get(DocumentVersion, observation.previous_version_id)
            new = await session.get(DocumentVersion, observation.version_id)
            added.append(
                await create_change(
                    session,
                    old,
                    new,
                    origin_key=f"fetch:{observation.id}",
                    detected_at=observation.observed_at,
                    observation_id=observation.id,
                )
            )
        # Archives imported without fetch observations retain a clearly labelled
        # archive comparison; no historical online observation is fabricated.
        archived = list(
            (
                await session.scalars(
                    select(DocumentVersion)
                    .where(
                        DocumentVersion.version_no > 1,
                        ~exists(
                            select(SourceFetchObservation.id).where(
                                SourceFetchObservation.document_id == DocumentVersion.document_id
                            )
                        ),
                        ~exists(
                            select(VersionChange.id).where(
                                VersionChange.origin_key
                                == func.concat("archive:", DocumentVersion.id)
                            )
                        ),
                    )
                    .order_by(DocumentVersion.fetched_at)
                    .limit(max(0, batch_size - len(added)))
                )
            ).all()
        )
        for new in archived:
            old = await session.scalar(
                select(DocumentVersion)
                .where(
                    DocumentVersion.document_id == new.document_id,
                    DocumentVersion.version_no < new.version_no,
                )
                .order_by(DocumentVersion.version_no.desc())
                .limit(1)
            )
            if old:
                added.append(
                    await create_change(
                        session,
                        old,
                        new,
                        origin_key=f"archive:{new.id}",
                        detected_at=new.fetched_at,
                    )
                )
        linked = 0
        for change in added:
            if change.change_kind == "bytes_only":
                continue
            old = await session.get(DocumentVersion, change.old_version_id)
            rows = (
                await session.execute(
                    select(ReportVersion, AnalysisTask)
                    .join(AnalysisTask, AnalysisTask.id == ReportVersion.task_id)
                    .where(
                        or_(
                            ReportVersion.dimensions_json.contains(
                                [{"citations": [{"version_id": str(old.id)}]}]
                            ),
                            ReportVersion.dimensions_json.contains(
                                [
                                    {
                                        "citations": [
                                            {
                                                "document_id": str(old.document_id),
                                                "version_no": old.version_no,
                                            }
                                        ]
                                    }
                                ]
                            ),
                        )
                    )
                )
            ).all()
            for report, task in rows:
                linked += await link_report(session, task, report, [change])
        # Repair the race where a report committed while this change was still
        # uncommitted. Missing links are bounded and rediscovered on the next sweep.
        from sqlalchemy import String, cast

        reference = func.jsonb_build_array(
            func.jsonb_build_object(
                "citations",
                func.jsonb_build_array(
                    func.jsonb_build_object(
                        "version_id", cast(VersionChange.old_version_id, String)
                    )
                ),
            )
        )
        late = (
            await session.execute(
                select(ReportVersion, AnalysisTask, VersionChange)
                .select_from(ReportVersion)
                .join(AnalysisTask, AnalysisTask.id == ReportVersion.task_id)
                .join(VersionChange, ReportVersion.dimensions_json.op("@>")(reference))
                .where(
                    VersionChange.change_kind != "bytes_only",
                    ~exists(
                        select(UpdateImpact.id).where(
                            UpdateImpact.report_version_id == ReportVersion.id,
                            UpdateImpact.change_id == VersionChange.id,
                        )
                    ),
                )
                .order_by(ReportVersion.created_at, VersionChange.detected_at)
                .limit(batch_size)
            )
        ).all()
        for report, task, change in late:
            linked += await link_report(session, task, report, [change])
        notifications = await reconcile_impacts(session, now=now)
        await session.commit()
        return {
            "changes": len(added),
            "impacts": linked,
            "notifications": notifications,
            "busy": False,
        }


async def monitor_loop(factory, *, interval=60):
    while True:
        try:
            await monitor_once(factory)
        except Exception:
            log.exception("更新监测失败，保留原始记录，下轮重试")
        await asyncio.sleep(interval)


async def report_updates(session, report_id):
    rows = (
        await session.execute(
            select(UpdateImpact, VersionChange, Document)
            .join(VersionChange, VersionChange.id == UpdateImpact.change_id)
            .join(Document, Document.id == VersionChange.document_id)
            .where(UpdateImpact.report_version_id == report_id)
            .order_by(UpdateImpact.detected_at.desc())
        )
    ).all()
    return [
        {
            "id": str(i.id),
            "change_id": str(c.id),
            "dimension_code": i.dimension_code,
            "document_title": d.title,
            "source_url": d.source_url,
            "old_version_id": str(c.old_version_id),
            "new_version_id": str(c.new_version_id),
            "detected_at": i.detected_at.isoformat(),
            "due_at": i.due_at.isoformat(),
            "state": i.state,
            "summary": c.summary,
            "change_kind": c.change_kind,
            "human_review": "pending_review",
        }
        for i, c, d in rows
    ]
