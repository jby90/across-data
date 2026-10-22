"""s3 document relevance and ocr review fields

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22 22:55:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "relevance",
            sa.String(length=16),
            server_default="relevant",
            nullable=False,
        ),
    )
    op.add_column(
        "documents",
        sa.Column("relevance_note", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_documents_relevance",
        "documents",
        "relevance IN ('relevant','irrelevant','needs_review')",
    )

    op.add_column(
        "document_versions",
        sa.Column(
            "ocr_required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "document_versions",
        sa.Column(
            "review_status",
            sa.String(length=24),
            server_default="pending_review",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_versions_review_status",
        "document_versions",
        "review_status IN ('pending_review','approved','rejected')",
    )

    op.add_column(
        "clauses",
        sa.Column(
            "ocr_required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "clauses",
        sa.Column(
            "needs_review",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )

    # Backfill existing rows
    op.execute(
        """
        UPDATE documents
        SET relevance = CASE
                WHEN document_type = 'FORM'
                  OR LOWER(source_url) LIKE '%formulaire%'
                  OR LOWER(title) LIKE '%formulaire%'
                THEN 'irrelevant'
                ELSE 'relevant'
            END,
            relevance_note = CASE
                WHEN document_type = 'FORM'
                  OR LOWER(source_url) LIKE '%formulaire%'
                  OR LOWER(title) LIKE '%formulaire%'
                THEN '办事申请表格/模板文件，非具有普遍约束力的法规或监管指引，采集阶段保留归档，供 S2 翻译与知识抽取阶段过滤'
                ELSE '属于目标法域数据保护/车联网/电信/自动驾驶相关立法或监管文件'
            END
        """
    )
    op.execute(
        """
        UPDATE document_versions
        SET ocr_required = (text_source = 'ocr' OR state = 'needs_ocr'),
            review_status = 'pending_review'
        """
    )
    op.execute(
        """
        UPDATE clauses
        SET ocr_required = true,
            needs_review = true
        WHERE version_id IN (
            SELECT id FROM document_versions WHERE text_source = 'ocr' OR state = 'needs_ocr'
        )
        """
    )


def downgrade() -> None:
    op.drop_column("clauses", "needs_review")
    op.drop_column("clauses", "ocr_required")
    op.drop_constraint("ck_versions_review_status", "document_versions", type_="check")
    op.drop_column("document_versions", "review_status")
    op.drop_column("document_versions", "ocr_required")
    op.drop_constraint("ck_documents_relevance", "documents", type_="check")
    op.drop_column("documents", "relevance_note")
    op.drop_column("documents", "relevance")
