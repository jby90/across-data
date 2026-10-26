from pydantic import ConfigDict, Field, field_serializer

from app.knowledge.schemas import (
    RegulationClauseItemOut,
    RegulationDetailOut,
    RegulationSummaryOut,
)


class PublicRegulationSummary(RegulationSummaryOut):
    model_config = ConfigDict(from_attributes=True)
    sha256_hex: str | None = Field(default=None, exclude=True)
    edges_count: int = Field(exclude=True)
    eight_attributes: dict = Field(exclude=True)


class PublicClause(RegulationClauseItemOut):
    model_config = ConfigDict(from_attributes=True)
    glossary_hits: list = Field(exclude=True)
    unmatched_terms: list = Field(exclude=True)
    linked_edges: list = Field(exclude=True)
    status_badge: str = Field(exclude=True)


class PublicRegulationDetail(RegulationDetailOut):
    model_config = ConfigDict(from_attributes=True)
    regulation: PublicRegulationSummary
    clauses: list[PublicClause]
    mini_graph: object = Field(exclude=True)

    @field_serializer("versions")
    def business_versions(self, values):
        return [
            {key: row.get(key) for key in ("version_no", "legal_status", "fetched_at")}
            for row in values
        ]
