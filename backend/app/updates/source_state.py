"""Latest observed bytes are distinct from legal validity and archive sequence numbers."""

from sqlalchemy import func, select

from app.ingestion.models import Document, DocumentVersion
from app.updates.models import SourceFetchObservation


def latest_observations_query(document_ids=None):
    query = select(
        SourceFetchObservation.document_id,
        SourceFetchObservation.version_id,
        func.row_number()
        .over(
            partition_by=SourceFetchObservation.document_id,
            order_by=SourceFetchObservation.id.desc(),
        )
        .label("position"),
    )
    if document_ids is not None:
        query = query.where(SourceFetchObservation.document_id.in_(document_ids))
    ranked = query.subquery()
    return select(ranked.c.document_id, ranked.c.version_id).where(ranked.c.position == 1)


def current_source_query():
    observed = latest_observations_query().subquery()
    ranked = select(
        DocumentVersion.document_id,
        DocumentVersion.id.label("version_id"),
        func.row_number()
        .over(partition_by=DocumentVersion.document_id, order_by=DocumentVersion.version_no.desc())
        .label("position"),
    ).subquery()
    return (
        select(
            Document.id.label("document_id"),
            func.coalesce(observed.c.version_id, ranked.c.version_id).label("version_id"),
        )
        .outerjoin(observed, observed.c.document_id == Document.id)
        .outerjoin(ranked, (ranked.c.document_id == Document.id) & (ranked.c.position == 1))
    )


def observed_version(document):
    return (
        select(SourceFetchObservation.version_id)
        .where(SourceFetchObservation.document_id == document.id)
        .order_by(SourceFetchObservation.id.desc())
        .limit(1)
        .correlate(Document)
        .scalar_subquery()
    )
